"""Read-only adapter for the public endpoints used by Binance Stocks Options.

These are website endpoints, not a version-guaranteed trading API. Never attach
account credentials. Reject changed schemas rather than silently invent quotes.
"""

import math
import re
import threading
import time
import weakref
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

import requests


BASE = "https://www.binance.com/bapi/equity/v1"
_cache = OrderedDict()
_cache_lock = threading.RLock()
_key_locks = weakref.WeakValueDictionary()
_requests = threading.BoundedSemaphore(3)


class EquityDataError(RuntimeError):
    pass


def _read(path, params=None, body=None):
    try:
        with _requests:
            if body is None:
                response = requests.get(BASE + path, params=params, timeout=(5, 15))
            else:
                # POST is the upstream batch quote lookup, never an order endpoint.
                response = requests.post(BASE + path, json=body, timeout=(5, 15))
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or payload.get("success") is not True:
            raise EquityDataError("币安暂未返回有效行情，请稍后重试。")
        return payload["data"]
    except (requests.RequestException, ValueError, KeyError) as exc:
        raise EquityDataError("币安行情读取失败，请稍后重试。") from exc


def _cached(key, ttl, loader):
    with _cache_lock:
        lock = _key_locks.setdefault(key, threading.RLock())
    with lock:
        with _cache_lock:
            entry = _cache.get(key)
            if entry and time.monotonic() - entry[0] < ttl:
                _cache.move_to_end(key)
                return entry[1]
        value = loader()
        with _cache_lock:
            _cache[key] = (time.monotonic(), value)
            _cache.move_to_end(key)
            while len(_cache) > 128:
                _cache.popitem(last=False)
        return value


def _number(value):
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (ValueError, TypeError):
        return None


def catalog():
    def load():
        raw = _read("/public/equity/symbol/get-symbols-static")
        if (not isinstance(raw, list) or not raw or
                not all(isinstance(x, dict) for x in raw) or not any("opt" in x for x in raw)):
            raise EquityDataError("币安股票目录格式已变化，暂不能确认期权支持范围。")
        symbols = []
        for item in raw:
            # os means overnight stock trading, not options support.
            if item.get("opt") is not True:
                continue
            symbol, asset = item.get("s"), item.get("ac")
            if not symbol or not asset or not item.get("t"):
                raise EquityDataError("币安股票目录缺少必要字段。")
            symbols.append({"symbol": symbol, "assetCode": asset,
                            "name": item.get("n", symbol), "type": item["t"]})
        return {"symbols": sorted(symbols, key=lambda x: x["symbol"]),
                "fetchedAt": int(time.time() * 1000)}
    return _cached("catalog", 3600, load)


def _stock(symbol):
    if not re.fullmatch(r"[A-Z0-9.\-]{1,20}", symbol):
        raise ValueError("请选择币安目录中的股票。")
    match = next((x for x in catalog()["symbols"] if x["symbol"] == symbol), None)
    if match is None:
        raise ValueError("该股票不在币安支持期权的目录中。")
    return match


def overview(symbol):
    stock = _stock(symbol)

    def load():
        data = _read("/friendly/equity/option/get",
                     {"symbol": symbol, "assetCode": stock["assetCode"]})
        if not isinstance(data, dict) or not isinstance(data.get("expirations"), list):
            raise EquityDataError("币安期权到期日数据格式已变化。")
        expirations = data["expirations"]
        if any(not isinstance(x, dict) or not x.get("rootSymbol") or not re.fullmatch(r"\d{4}-\d{2}-\d{2}",
               x.get("expirationDate", "")) for x in expirations):
            raise EquityDataError("币安期权到期日缺少必要字段。")
        return {"stock": stock, "price": _number(data.get("price")),
                "expirations": expirations, "fetchedAt": int(time.time() * 1000)}
    return _cached(("overview", symbol), 30, load)


def market_calendar():
    def load():
        data = _read("/public/equity/calendar/periods")
        return {"phase": data.get("currentPhase", "UNKNOWN")}
    try:
        return _cached("calendar", 30, load)
    except (EquityDataError, AttributeError):
        return {"phase": "UNKNOWN"}


def chain(symbol, expiration, root):
    info = overview(symbol)
    if not any(x["expirationDate"] == expiration and x["rootSymbol"] == root
               for x in info["expirations"]):
        raise ValueError("该到期日或合约系列已不可用，请重新选择。")

    def load():
        raw = _read("/public/equity/option/chain-symbols",
                    {"symbol": symbol, "rootSymbol": root, "expirationDate": expiration})
        if not isinstance(raw, dict) or not isinstance(raw.get("l"), list):
            raise EquityDataError("币安期权链格式已变化。")
        contracts = raw["l"]
        if any(not isinstance(x, dict) or not x.get("os") or x.get("t") not in ("CALL", "PUT") for x in contracts):
            raise EquityDataError("币安期权合约缺少必要字段。")
        symbols = list(dict.fromkeys(x["os"] for x in contracts))
        batches = [symbols[i:i + 100] for i in range(0, len(symbols), 100)]

        def quotes(batch):
            data = _read("/friendly/equity/option/get-option-quote", body={"occSymbols": batch})
            if not isinstance(data, list) or not all(isinstance(x, dict) and x.get("os") for x in data):
                raise EquityDataError("币安期权报价格式已变化。")
            return data

        with ThreadPoolExecutor(max_workers=3) as pool:
            batches_data = list(pool.map(quotes, batches))
        quote_map = {x.get("os"): x for batch in batches_data for x in batch}
        rows = []
        for contract in contracts:
            q = quote_map.get(contract["os"], {})
            rows.append({
                "symbol": contract["os"], "type": contract["t"],
                "strike": _number(contract.get("sp")),
                "bid": _number(q.get("bp")), "ask": _number(q.get("ap")),
                "bidSize": _number(q.get("bs")), "askSize": _number(q.get("as")),
                "multiplier": _number(q.get("m")), "expiresAt": _number(q.get("e")),
                "volume": _number(q.get("v")), "openInterest": _number(q.get("oi")),
                "iv": _number(q.get("iv")), "delta": _number(q.get("de")),
                "adjusted": root != symbol, "quoteMissing": not bool(q),
            })
        return {"rows": rows, "fetchedAt": int(time.time() * 1000),
                "missingCount": sum(x["quoteMissing"] for x in rows)}

    result = _cached(("chain", symbol, expiration, root), 30, load)
    return {**result, "symbol": symbol, "expiration": expiration, "root": root,
            "price": info["price"], "priceFetchedAt": info["fetchedAt"],
            "market": market_calendar(), "quoteTimestampAvailable": False}


def contract_details(symbol, option_symbol):
    _stock(symbol)
    if not re.fullmatch(r"[A-Z0-9.\-]{1,20}\d{6}[CP]\d{8}", option_symbol):
        raise ValueError("期权合约代码无效。")

    def load():
        data = _read("/public/equity/option/get-exchange-info", {"symbol": option_symbol})
        if not isinstance(data, dict) or not isinstance(data.get("syms"), list):
            raise EquityDataError("币安合约规则格式已变化。")
        syms = data.get("syms", [])
        if len(syms) != 1 or syms[0].get("us") != symbol or syms[0].get("s") != option_symbol:
            raise ValueError("该合约不属于所选股票。")
        return {"contract": syms[0], "fetchedAt": int(time.time() * 1000)}
    return _cached(("contract", symbol, option_symbol), 60, load)
