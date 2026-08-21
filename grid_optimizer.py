"""Transparent Binance USD-M futures grid-structure analysis.

This module only consumes public market-data endpoints.  It never reads an
account and never creates, changes, or cancels an order.
"""

from __future__ import annotations

import csv
import io
import math
import os
import statistics
import threading
import time
import weakref
import zipfile
from array import array
from bisect import bisect_left, bisect_right
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP, ROUND_UP
from typing import Iterable

import requests


BINANCE_FAPI_BASE = os.environ.get("BINANCE_FAPI_BASE", "https://fapi.binance.com")
BINANCE_DATA_BASE = os.environ.get("BINANCE_DATA_BASE", "https://data.binance.vision")
GRID_SYMBOL_CONFIGS = {
    "MUUSDT": {"maxLeverage": 10, "assetClass": "stock"},
    "MSTRUSDT": {"maxLeverage": 10, "assetClass": "stock"},
    "BMNRUSDT": {"maxLeverage": 10, "assetClass": "stock"},
    "CRWVUSDT": {"maxLeverage": 10, "assetClass": "stock"},
    "INTCUSDT": {"maxLeverage": 10, "assetClass": "stock"},
    "AAOIUSDT": {"maxLeverage": 10, "assetClass": "stock"},
    "SKHYUSDT": {"maxLeverage": 10, "assetClass": "stock"},
    "GOOGLUSDT": {"maxLeverage": 10, "assetClass": "stock"},
    "BTCUSDT": {
        "maxLeverage": 125,
        "assetClass": "crypto",
        "maxAggTradePages": 5000,
    },
    "ETHUSDT": {
        "maxLeverage": 100,
        "assetClass": "crypto",
        "maxAggTradePages": 5000,
    },
    "SPCXUSDT": {"maxLeverage": 10, "assetClass": "stock"},
}
SUPPORTED_GRID_SYMBOLS = tuple(GRID_SYMBOL_CONFIGS)
GAP_CANDIDATES = tuple(round(value / 2000, 4) for value in range(1, 25))
RANGE_CANDIDATES = (0.06, 0.08, 0.10, 0.12, 0.14, 0.16, 0.18, 0.20, 0.30)

DEFAULT_INVESTMENT = 100_000.0
DEFAULT_FEE_RATE = 0.0001  # One side: 0.01% (1 bp / 万分之一)
DEFAULT_LEVERAGE = 1

MINUTE_MS = 60_000
DAY_MINUTES = 24 * 60
WEEK_MINUTES = 7 * DAY_MINUTES
MARKET_CACHE_SECONDS = 60
MARKET_STALE_SECONDS = 15 * 60
MARKET_CACHE_MAX_SYMBOLS = 16

HOUR_MS = 60 * MINUTE_MS
DAY_MS = DAY_MINUTES * MINUTE_MS
AGG_TRADE_PAGE_LIMIT = 1000
AGG_TRADE_MAX_PAGES = 1200
AGG_ARCHIVE_MAX_BYTES = 512 * 1024 * 1024
AGG_TRADE_MIN_REQUEST_INTERVAL = 0.55
FINE_PATH_REFRESH_MS = 60_000
FINE_PATH_RETRY_SECONDS = 60
FINE_PATH_MAX_SYMBOLS = 3
FINE_PATH_CONCURRENT_REFRESHES = 2

_market_cache: dict[str, tuple[float, dict]] = {}
_market_cache_lock = threading.Lock()
_market_fetch_locks: dict[str, threading.Lock] = {}
_market_fetch_locks_lock = threading.Lock()

_fine_path_states: dict[str, dict] = {}
_fine_path_lock = threading.Lock()
_fine_path_refresh_semaphore = threading.Semaphore(FINE_PATH_CONCURRENT_REFRESHES)
_agg_trade_request_lock = threading.Lock()
_last_agg_trade_request_at = 0.0

ANALYSIS_CACHE_MAX_ENTRIES = 64
_analysis_cache: dict[tuple, dict] = {}
_analysis_cache_lock = threading.Lock()
_analysis_locks = weakref.WeakValueDictionary()
_analysis_locks_lock = threading.Lock()


class BinanceMarketDataError(RuntimeError):
    """Raised when public Binance market data is unavailable or malformed."""


def _normalize_range_rate(range_rate: float | None) -> float | None:
    """Return the canonical configured range rate or reject unsupported input."""
    if range_rate is None:
        return None
    if isinstance(range_rate, bool) or not isinstance(range_rate, (int, float)):
        raise ValueError("Range rate must be a number")
    if not math.isfinite(range_rate):
        raise ValueError("Range rate must be finite")
    for candidate in RANGE_CANDIDATES:
        if math.isclose(float(range_rate), candidate, rel_tol=0.0, abs_tol=1e-9):
            return candidate
    choices = ", ".join(f"±{value * 100:.0f}%" for value in RANGE_CANDIDATES)
    raise ValueError(f"Range rate must be one of: {choices}")


def _get_json(path: str, params: dict | None = None, timeout=(4, 10), attempts: int = 2):
    url = f"{BINANCE_FAPI_BASE}{path}"
    last_error = None
    for attempt in range(max(1, attempts)):
        try:
            response = requests.get(url, params=params, timeout=timeout)
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError) as exc:
            last_error = exc
            if attempt + 1 < max(1, attempts):
                response = getattr(exc, "response", None)
                retry_after = response.headers.get("Retry-After") if response is not None else None
                try:
                    delay = float(retry_after) if retry_after else 0.4 * (2**attempt)
                except (TypeError, ValueError):
                    delay = 0.4 * (2**attempt)
                time.sleep(min(5.0, max(0.0, delay)))
    raise BinanceMarketDataError(f"Binance public API request failed: {path}") from last_error


def _iso_from_ms(timestamp_ms: int) -> str:
    value = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def _decimal_places(step: float) -> int:
    normalized = Decimal(str(step)).normalize()
    return max(0, -normalized.as_tuple().exponent)


def _format_gap_percent(rate: float) -> str:
    return f"{rate * 100:.2f}".rstrip("0").rstrip(".")


def _round_to_step(value: float, step: float, rounding) -> float:
    if step <= 0:
        return value
    value_decimal = Decimal(str(value))
    step_decimal = Decimal(str(step))
    units = (value_decimal / step_decimal).to_integral_value(rounding=rounding)
    return float(units * step_decimal)


def _round_down(value: float, step: float) -> float:
    return _round_to_step(value, step, ROUND_DOWN)


def _round_up(value: float, step: float) -> float:
    return _round_to_step(value, step, ROUND_UP)


def _round_nearest(value: float, step: float) -> float:
    return _round_to_step(value, step, ROUND_HALF_UP)


def fetch_server_time() -> int:
    payload = _get_json("/fapi/v1/time")
    return int(payload["serverTime"])


def fetch_symbol_rules(symbol: str) -> dict:
    payload = _get_json("/fapi/v1/exchangeInfo")
    symbol_info = next(
        (item for item in payload.get("symbols", []) if item.get("symbol") == symbol),
        None,
    )
    if not symbol_info:
        raise BinanceMarketDataError(f"Symbol is not available on Binance Futures: {symbol}")

    filters = {item.get("filterType"): item for item in symbol_info.get("filters", [])}
    price_filter = filters.get("PRICE_FILTER", {})
    lot_filter = filters.get("LOT_SIZE", {})
    notional_filter = filters.get("MIN_NOTIONAL", {})

    return {
        "symbol": symbol,
        "status": symbol_info.get("status"),
        "contractType": symbol_info.get("contractType"),
        "baseAsset": symbol_info.get("baseAsset"),
        "quoteAsset": symbol_info.get("quoteAsset"),
        "tickSize": float(price_filter.get("tickSize") or 0.01),
        "stepSize": float(lot_filter.get("stepSize") or 0.01),
        "minQty": float(lot_filter.get("minQty") or 0.01),
        "minNotional": float(notional_filter.get("notional") or 5.0),
        "pricePrecision": int(symbol_info.get("pricePrecision", 2)),
        "quantityPrecision": int(symbol_info.get("quantityPrecision", 2)),
        "maxLeverage": GRID_SYMBOL_CONFIGS[symbol]["maxLeverage"],
        "assetClass": GRID_SYMBOL_CONFIGS[symbol]["assetClass"],
    }


def fetch_book_ticker(symbol: str) -> dict:
    payload = _get_json("/fapi/v1/ticker/bookTicker", {"symbol": symbol})
    bid = float(payload["bidPrice"])
    ask = float(payload["askPrice"])
    mid = (bid + ask) / 2
    spread = max(0.0, ask - bid)
    return {
        "bid": bid,
        "ask": ask,
        "mid": mid,
        "spread": spread,
        "spreadRate": spread / mid if mid > 0 else 0.0,
        "time": int(payload.get("time") or 0),
    }


def fetch_mark_price(symbol: str) -> dict:
    payload = _get_json("/fapi/v1/premiumIndex", {"symbol": symbol})
    return {
        "markPrice": float(payload["markPrice"]),
        "indexPrice": float(payload["indexPrice"]),
        "time": int(payload.get("time") or 0),
    }


def _parse_agg_trade_payload(payload: list[dict]) -> list[dict]:
    return [
        {
            "id": int(item["a"]),
            "price": float(item["p"]),
            "quantity": float(item["q"]),
            "time": int(item["T"]),
        }
        for item in payload
    ]


def _compress_turning_path(prices: Iterable[float], times: Iterable[int]) -> tuple[array, array]:
    """Keep only local extrema; monotonic interior trades cannot change grid crossings."""
    compact_prices = array("d")
    compact_times = array("q")
    direction = 0
    for price_value, time_value in zip(prices, times):
        price = float(price_value)
        timestamp = int(time_value)
        if not compact_prices:
            compact_prices.append(price)
            compact_times.append(timestamp)
            continue
        if price == compact_prices[-1]:
            compact_times[-1] = timestamp
            continue
        next_direction = 1 if price > compact_prices[-1] else -1
        if direction == 0:
            compact_prices.append(price)
            compact_times.append(timestamp)
            direction = next_direction
        elif next_direction == direction:
            compact_prices[-1] = price
            compact_times[-1] = timestamp
        else:
            compact_prices.append(price)
            compact_times.append(timestamp)
            direction = next_direction
    return compact_prices, compact_times


def _get_agg_trade_page(params: dict) -> list[dict]:
    """Read one public aggTrades page while respecting its IP weight."""
    global _last_agg_trade_request_at
    with _agg_trade_request_lock:
        elapsed = time.monotonic() - _last_agg_trade_request_at
        delay = AGG_TRADE_MIN_REQUEST_INTERVAL - elapsed
        if delay > 0:
            time.sleep(delay)
        _last_agg_trade_request_at = time.monotonic()
    payload = _get_json(
        "/fapi/v1/aggTrades",
        params,
        timeout=(4, 10),
        attempts=3,
    )
    return _parse_agg_trade_payload(payload)


def _load_completed_agg_archive(
    symbol: str, start_time_ms: int, end_time_ms: int
) -> dict | None:
    """Read the completed UTC day from Binance's official public ZIP archive."""
    start_day_ms = (start_time_ms // DAY_MS) * DAY_MS
    end_day_ms = (end_time_ms // DAY_MS) * DAY_MS
    if start_day_ms >= end_day_ms:
        return None

    day_text = datetime.fromtimestamp(
        start_day_ms / 1000, tz=timezone.utc
    ).strftime("%Y-%m-%d")
    url = (
        f"{BINANCE_DATA_BASE}/data/futures/um/daily/aggTrades/{symbol}/"
        f"{symbol}-aggTrades-{day_text}.zip"
    )
    response = None
    for attempt in range(2):
        try:
            response = requests.get(url, timeout=(5, 30))
            if response.status_code == 404:
                return None
            response.raise_for_status()
            break
        except requests.RequestException:
            if attempt == 1:
                return None
            time.sleep(0.5)
    if response is None or len(response.content) > AGG_ARCHIVE_MAX_BYTES:
        return None

    prices = array("d")
    times = array("q")
    last_id = None
    archive_end = min(end_time_ms, start_day_ms + DAY_MS - 1)
    try:
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            names = [name for name in archive.namelist() if name.lower().endswith(".csv")]
            if len(names) != 1:
                return None
            info = archive.getinfo(names[0])
            if info.file_size > AGG_ARCHIVE_MAX_BYTES:
                return None
            with archive.open(info) as raw_file:
                text_file = io.TextIOWrapper(raw_file, encoding="utf-8", newline="")
                for row in csv.reader(text_file):
                    if not row or not row[0].isdigit():
                        continue
                    trade_time = int(row[5])
                    if trade_time > 100_000_000_000_000:
                        trade_time //= 1000
                    if start_time_ms <= trade_time <= archive_end:
                        prices.append(float(row[1]))
                        times.append(trade_time)
                        last_id = int(row[0])
    except (OSError, ValueError, IndexError, zipfile.BadZipFile):
        return None

    return {
        "prices": prices,
        "times": times,
        "lastId": last_id,
        "archiveDay": day_text,
        "archiveEnd": archive_end,
        "tradeCount": len(prices),
    }


def _load_agg_trade_window(
    symbol: str,
    start_time_ms: int,
    end_time_ms: int,
    progress_callback=None,
) -> dict:
    """Fetch every aggregate trade in a bounded window, in ascending ID order."""
    max_pages = GRID_SYMBOL_CONFIGS.get(symbol, {}).get(
        "maxAggTradePages", AGG_TRADE_MAX_PAGES
    )
    archive_data = _load_completed_agg_archive(symbol, start_time_ms, end_time_ms)
    prices = array("d", archive_data["prices"] if archive_data else ())
    times = array("q", archive_data["times"] if archive_data else ())
    pages = 0
    last_id = archive_data["lastId"] if archive_data else None
    archive_trade_count = len(prices)
    search_start = (
        archive_data["archiveEnd"] + 1 if archive_data else start_time_ms
    )
    page: list[dict] = []

    if archive_data and progress_callback:
        progress_callback(search_start, pages, len(prices))

    while search_start <= end_time_ms:
        page_end = min(end_time_ms, search_start + HOUR_MS - 1)
        page = _get_agg_trade_page(
            {
                "symbol": symbol,
                "startTime": search_start,
                "endTime": page_end,
                "limit": AGG_TRADE_PAGE_LIMIT,
            }
        )
        pages += 1
        if page:
            break
        search_start = page_end + 1
        if progress_callback:
            progress_callback(search_start, pages, 0)

    bounded_page = True
    while page:
        if pages > max_pages:
            raise BinanceMarketDataError("24h aggTrades exceeded the safety page limit")
        page.sort(key=lambda item: item["id"])
        for item in page:
            if start_time_ms <= item["time"] <= end_time_ms:
                prices.append(item["price"])
                times.append(item["time"])
                last_id = item["id"]

        last_page_id = page[-1]["id"]
        last_page_time = page[-1]["time"]
        if progress_callback:
            progress_callback(min(last_page_time, end_time_ms), pages, len(prices))
        if last_page_time >= end_time_ms:
            break
        if not bounded_page and len(page) < AGG_TRADE_PAGE_LIMIT:
            break

        page = _get_agg_trade_page(
            {
                "symbol": symbol,
                "fromId": last_page_id + 1,
                "limit": AGG_TRADE_PAGE_LIMIT,
            }
        )
        pages += 1
        bounded_page = False

    analysis_prices, analysis_times = _compress_turning_path(prices, times)
    return {
        "windowStart": start_time_ms,
        "windowEnd": end_time_ms,
        "prices": prices,
        "times": times,
        "analysisPrices": analysis_prices,
        "analysisTimes": analysis_times,
        "firstTradeTime": int(times[0]) if times else None,
        "lastTradeTime": int(times[-1]) if times else None,
        "lastId": last_id,
        "tradeCount": len(prices),
        "pages": pages,
        "archiveDay": archive_data["archiveDay"] if archive_data else None,
        "archiveEnd": archive_data["archiveEnd"] if archive_data else None,
        "archiveTradeCount": archive_trade_count,
    }


def _extend_agg_trade_window(
    symbol: str,
    existing: dict,
    start_time_ms: int,
    end_time_ms: int,
    progress_callback=None,
) -> dict:
    """Increment an existing complete window and discard data older than 24h."""
    if (
        existing.get("lastId") is None
        or start_time_ms < existing["windowStart"]
        or end_time_ms - existing["windowEnd"] >= 47 * HOUR_MS
    ):
        return _load_agg_trade_window(
            symbol, start_time_ms, end_time_ms, progress_callback
        )

    prices = array("d", existing["prices"])
    times = array("q", existing["times"])
    last_id = existing["lastId"]
    pages = existing.get("pages", 0)
    cursor_id = last_id + 1
    added_pages = 0
    max_pages = GRID_SYMBOL_CONFIGS.get(symbol, {}).get(
        "maxAggTradePages", AGG_TRADE_MAX_PAGES
    )

    while True:
        if added_pages >= max_pages:
            raise BinanceMarketDataError("aggTrades refresh exceeded the safety page limit")
        page = _get_agg_trade_page(
            {
                "symbol": symbol,
                "fromId": cursor_id,
                "limit": AGG_TRADE_PAGE_LIMIT,
            }
        )
        pages += 1
        added_pages += 1
        if not page:
            break
        page.sort(key=lambda item: item["id"])
        for item in page:
            if item["time"] <= end_time_ms:
                prices.append(item["price"])
                times.append(item["time"])
                last_id = item["id"]
        last_page_id = page[-1]["id"]
        last_page_time = page[-1]["time"]
        if progress_callback:
            progress_callback(min(last_page_time, end_time_ms), pages, len(prices))
        if last_page_time >= end_time_ms or len(page) < AGG_TRADE_PAGE_LIMIT:
            break
        if last_page_id < cursor_id:
            raise BinanceMarketDataError("aggTrades pagination did not advance")
        cursor_id = last_page_id + 1

    keep_from = bisect_left(times, start_time_ms)
    if keep_from:
        prices = array("d", prices[keep_from:])
        times = array("q", times[keep_from:])
    archive_end = existing.get("archiveEnd")
    archive_trade_count = (
        bisect_right(times, archive_end) if archive_end is not None else 0
    )
    analysis_prices, analysis_times = _compress_turning_path(prices, times)
    return {
        "windowStart": start_time_ms,
        "windowEnd": end_time_ms,
        "prices": prices,
        "times": times,
        "analysisPrices": analysis_prices,
        "analysisTimes": analysis_times,
        "firstTradeTime": int(times[0]) if times else None,
        "lastTradeTime": int(times[-1]) if times else None,
        "lastId": last_id,
        "tradeCount": len(prices),
        "pages": pages,
        "archiveDay": existing.get("archiveDay"),
        "archiveEnd": archive_end,
        "archiveTradeCount": archive_trade_count,
    }


def _update_fine_path_progress(
    symbol: str,
    start_time_ms: int,
    end_time_ms: int,
    current_time_ms: int,
    pages: int,
    trade_count: int,
) -> None:
    span = max(1, end_time_ms - start_time_ms)
    progress = min(0.999, max(0.0, (current_time_ms - start_time_ms) / span))
    with _fine_path_lock:
        state = _fine_path_states.get(symbol)
        if state:
            state.update(progress=progress, pages=pages, tradeCount=trade_count)


def _trim_fine_path_states_locked(preserve_symbol: str | None = None) -> None:
    """Keep only the most recently used completed fine paths in memory."""
    while len(_fine_path_states) > FINE_PATH_MAX_SYMBOLS:
        candidates = [
            (candidate_symbol, candidate_state)
            for candidate_symbol, candidate_state in _fine_path_states.items()
            if candidate_symbol != preserve_symbol
            and candidate_state.get("thread") is None
        ]
        if not candidates:
            return
        oldest_symbol, _ = min(
            candidates,
            key=lambda item: item[1].get(
                "lastAccessAt", item[1].get("completedAt", 0.0)
            ),
        )
        del _fine_path_states[oldest_symbol]


def _run_fine_path_refresh(symbol: str, start_time_ms: int, end_time_ms: int) -> None:
    with _fine_path_refresh_semaphore:
        _perform_fine_path_refresh(symbol, start_time_ms, end_time_ms)


def _perform_fine_path_refresh(
    symbol: str, start_time_ms: int, end_time_ms: int
) -> None:
    callback = lambda current, pages, count: _update_fine_path_progress(
        symbol, start_time_ms, end_time_ms, current, pages, count
    )
    with _fine_path_lock:
        existing = _fine_path_states.get(symbol, {}).get("data")
    try:
        if existing:
            data = _extend_agg_trade_window(
                symbol, existing, start_time_ms, end_time_ms, callback
            )
        else:
            data = _load_agg_trade_window(
                symbol, start_time_ms, end_time_ms, callback
            )
    except Exception as exc:
        with _fine_path_lock:
            state = _fine_path_states.setdefault(symbol, {})
            state.update(
                status="ready" if existing else "error",
                thread=None,
                error=str(exc),
                lastAttemptAt=time.monotonic(),
            )
            _trim_fine_path_states_locked(preserve_symbol=symbol)
        return

    with _fine_path_lock:
        state = _fine_path_states.setdefault(symbol, {})
        state.update(
            status="ready",
            progress=1.0,
            pages=data["pages"],
            tradeCount=data["tradeCount"],
            data=data,
            thread=None,
            error=None,
            lastAttemptAt=time.monotonic(),
            completedAt=time.time(),
        )
        _trim_fine_path_states_locked(preserve_symbol=symbol)


def get_fine_path_snapshot(
    symbol: str, start_time_ms: int, end_time_ms: int
) -> dict:
    """Return cached 24h trades immediately and refresh them in one background job."""
    thread_to_start = None
    with _fine_path_lock:
        state = _fine_path_states.setdefault(
            symbol,
            {
                "status": "idle",
                "progress": 0.0,
                "pages": 0,
                "tradeCount": 0,
                "thread": None,
                "data": None,
                "error": None,
                "lastAttemptAt": 0.0,
                "lastAccessAt": 0.0,
            },
        )
        state["lastAccessAt"] = time.monotonic()
        _trim_fine_path_states_locked(preserve_symbol=symbol)
        data = state.get("data")
        running = state.get("thread") is not None
        needs_refresh = data is None or (
            start_time_ms < data["windowStart"]
            or end_time_ms - data["windowEnd"] >= FINE_PATH_REFRESH_MS
        )
        retry_ready = (
            not state.get("error")
            or time.monotonic() - state.get("lastAttemptAt", 0) >= FINE_PATH_RETRY_SECONDS
        )
        if needs_refresh and not running and retry_ready:
            state["status"] = "refreshing" if data else "loading"
            state["progress"] = 1.0 if data else 0.0
            state["error"] = None
            thread_to_start = threading.Thread(
                target=_run_fine_path_refresh,
                args=(symbol, start_time_ms, end_time_ms),
                daemon=True,
                name=f"{symbol}-24h-aggTrades",
            )
            state["thread"] = thread_to_start

        result = {
            "status": state.get("status", "idle"),
            "complete": data is not None,
            "progress": state.get("progress", 0.0),
            "pages": state.get("pages", 0),
            "tradeCount": state.get("tradeCount", 0),
            "refreshFailed": bool(state.get("error") and data),
            "prices": data["prices"] if data else (),
            "times": data["times"] if data else (),
            "analysisPrices": data["analysisPrices"] if data else (),
            "analysisTimes": data["analysisTimes"] if data else (),
            "fromMs": data["windowStart"] if data else None,
            "toMs": data["windowEnd"] if data else None,
            "firstTradeTime": data["firstTradeTime"] if data else None,
            "lastTradeTime": data["lastTradeTime"] if data else None,
            "archiveDay": data.get("archiveDay") if data else None,
            "archiveTradeCount": data.get("archiveTradeCount", 0) if data else 0,
            "analysisPointCount": len(data["analysisPrices"]) if data else 0,
        }

    if thread_to_start:
        thread_to_start.start()
    return result


def fetch_week_1m_klines(symbol: str, server_time_ms: int) -> list[dict]:
    """Fetch exactly seven days of completed one-minute candles."""
    last_open_time = (server_time_ms // MINUTE_MS) * MINUTE_MS - MINUTE_MS
    first_open_time = last_open_time - (WEEK_MINUTES - 1) * MINUTE_MS
    candles_by_time: dict[int, dict] = {}
    page_size = 1500
    page_starts = list(
        range(first_open_time, last_open_time + 1, page_size * MINUTE_MS)
    )

    def fetch_page(page_start: int):
        page_end = min(
            last_open_time + MINUTE_MS - 1,
            page_start + page_size * MINUTE_MS - 1,
        )
        return _get_json(
            "/fapi/v1/klines",
            {
                "symbol": symbol,
                "interval": "1m",
                "startTime": page_start,
                "endTime": page_end,
                "limit": page_size,
            },
            timeout=(4, 10),
            attempts=2,
        )

    with ThreadPoolExecutor(max_workers=len(page_starts)) as executor:
        payloads = list(executor.map(fetch_page, page_starts))

    for payload in payloads:
        for item in payload:
            open_time = int(item[0])
            if first_open_time <= open_time <= last_open_time:
                candles_by_time[open_time] = {
                    "openTime": open_time,
                    "open": float(item[1]),
                    "high": float(item[2]),
                    "low": float(item[3]),
                    "close": float(item[4]),
                    "volume": float(item[5]),
                    "closeTime": int(item[6]),
                    "trades": int(item[8]),
                }

    candles = [candles_by_time[key] for key in sorted(candles_by_time)]
    if len(candles) != WEEK_MINUTES:
        raise BinanceMarketDataError(
            f"Incomplete 1m kline history: received {len(candles)} of {WEEK_MINUTES} candles"
        )
    for index, candle in enumerate(candles):
        expected_open_time = first_open_time + index * MINUTE_MS
        if candle["openTime"] != expected_open_time:
            raise BinanceMarketDataError(
                "Incomplete 1m kline history: one or more minute intervals are missing"
            )
    return candles


def _get_market_fetch_lock(symbol: str) -> threading.Lock:
    with _market_fetch_locks_lock:
        return _market_fetch_locks.setdefault(symbol, threading.Lock())


def _attach_fine_path(snapshot: dict, cache_hit: bool, stale: bool = False) -> dict:
    result = dict(snapshot)
    candles = result["candles"]
    fine_start = candles[-DAY_MINUTES]["openTime"]
    result["finePath"] = get_fine_path_snapshot(
        result["rules"]["symbol"], fine_start, result["serverTime"]
    )
    result["cacheHit"] = cache_hit
    result["marketDataStale"] = stale
    return result


def get_market_snapshot(symbol: str, force_refresh: bool = False) -> dict:
    """Return a coalesced 60-second snapshot; public refresh cannot bypass it."""
    now = time.time()
    with _market_cache_lock:
        cached = _market_cache.get(symbol)
    if cached and now - cached[0] < MARKET_CACHE_SECONDS:
        return _attach_fine_path(cached[1], cache_hit=True)

    fetch_lock = _get_market_fetch_lock(symbol)
    with fetch_lock:
        now = time.time()
        with _market_cache_lock:
            cached = _market_cache.get(symbol)
        if cached and now - cached[0] < MARKET_CACHE_SECONDS:
            return _attach_fine_path(cached[1], cache_hit=True)

        try:
            server_time_ms = fetch_server_time()
            with ThreadPoolExecutor(max_workers=4) as executor:
                futures = {
                    "rules": executor.submit(fetch_symbol_rules, symbol),
                    "book": executor.submit(fetch_book_ticker, symbol),
                    "mark": executor.submit(fetch_mark_price, symbol),
                    "candles": executor.submit(
                        fetch_week_1m_klines, symbol, server_time_ms
                    ),
                }
                snapshot = {name: future.result() for name, future in futures.items()}
            if snapshot["rules"]["status"] != "TRADING":
                raise BinanceMarketDataError(
                    f"{symbol} is not currently TRADING ({snapshot['rules']['status']})"
                )
        except (BinanceMarketDataError, KeyError, TypeError, ValueError, OverflowError) as exc:
            if cached and now - cached[0] <= MARKET_STALE_SECONDS:
                return _attach_fine_path(cached[1], cache_hit=True, stale=True)
            if isinstance(exc, BinanceMarketDataError):
                raise
            raise BinanceMarketDataError(
                "Binance public API returned malformed market data"
            ) from exc

        snapshot["serverTime"] = server_time_ms
        with _market_cache_lock:
            _market_cache[symbol] = (time.time(), snapshot)
            while len(_market_cache) > MARKET_CACHE_MAX_SYMBOLS:
                oldest_symbol = min(
                    _market_cache, key=lambda item: _market_cache[item][0]
                )
                del _market_cache[oldest_symbol]
        return _attach_fine_path(snapshot, cache_hit=False)


def efficiency_ratio(prices: Iterable[float]) -> float:
    values = [float(value) for value in prices]
    if len(values) < 2:
        return 0.0
    path = sum(abs(current - previous) for previous, current in zip(values, values[1:]))
    if path <= 0:
        return 0.0
    return abs(values[-1] - values[0]) / path


def resample_closes(candles: list[dict], minutes: int = 5) -> list[float]:
    if minutes <= 1:
        return [candle["close"] for candle in candles]
    return [
        candles[index]["close"]
        for index in range(minutes - 1, len(candles), minutes)
    ]


def build_arithmetic_grid(
    center: float,
    range_rate: float,
    target_gap_rate: float,
    tick_size: float,
) -> dict:
    lower = _round_down(center * (1 - range_rate), tick_size)
    upper = _round_up(center * (1 + range_rate), tick_size)
    target_step = center * target_gap_rate
    number_of_grids = int(round((upper - lower) / target_step)) if target_step > 0 else 2
    number_of_grids = min(1000, max(2, number_of_grids))
    raw_step = (upper - lower) / number_of_grids

    levels = [
        _round_nearest(lower + raw_step * index, tick_size)
        for index in range(number_of_grids + 1)
    ]
    levels[0] = lower
    levels[-1] = upper

    return {
        "lower": lower,
        "upper": upper,
        "numberOfGrids": number_of_grids,
        "priceStep": raw_step,
        "actualGapAtCenter": raw_step / center if center > 0 else 0.0,
        "levels": levels,
    }


def count_grid_roundtrips(prices: Iterable[float], levels: list[float]) -> dict:
    """Count completed traversals without treating a one-way move as profit.

    For each adjacent grid interval, the first complete traversal only arms the
    interval.  A later traversal in the opposite direction completes one
    effective buy/sell pair.  Further alternating traversals each add one.
    """
    interval_count = max(0, len(levels) - 1)
    empty_result = {
        "roundTrips": 0,
        "roundTripsByInterval": [0] * interval_count,
        "traversedIntervals": 0,
        "utilization": 0.0,
    }
    if interval_count == 0:
        return empty_result

    values = (float(value) for value in prices if float(value) > 0)
    try:
        previous_price = next(values)
    except StopIteration:
        return empty_result

    last_side: list[int | None] = [None] * interval_count
    last_direction: list[int | None] = [None] * interval_count
    traversed: set[int] = set()
    round_trips_by_interval = [0] * interval_count
    epsilon = max(1e-12, abs(levels[-1] - levels[0]) * 1e-12)

    for index in range(interval_count):
        if previous_price <= levels[index] + epsilon:
            last_side[index] = -1
        elif previous_price >= levels[index + 1] - epsilon:
            last_side[index] = 1

    def apply_side(index: int, side: int) -> None:
        previous_side = last_side[index]
        if previous_side is None:
            last_side[index] = side
            return
        if previous_side == side:
            return
        direction = 1 if side > previous_side else -1
        traversed.add(index)
        previous_direction = last_direction[index]
        if previous_direction is not None and direction != previous_direction:
            round_trips_by_interval[index] += 1
        last_direction[index] = direction
        last_side[index] = side

    has_segment = False
    for current_price in values:
        has_segment = True
        if current_price > previous_price:
            first_level = max(1, bisect_right(levels, previous_price + epsilon))
            after_last = min(
                len(levels), bisect_right(levels, current_price + epsilon)
            )
            for level_index in range(first_level, after_last):
                apply_side(level_index - 1, 1)
        elif current_price < previous_price:
            first_level = max(0, bisect_left(levels, current_price - epsilon))
            after_last = min(
                interval_count, bisect_left(levels, previous_price - epsilon)
            )
            for level_index in range(first_level, after_last):
                apply_side(level_index, -1)
        previous_price = current_price

    if not has_segment:
        return empty_result

    return {
        "roundTrips": sum(round_trips_by_interval),
        "roundTripsByInterval": round_trips_by_interval,
        "traversedIntervals": len(traversed),
        "utilization": len(traversed) / interval_count,
    }


def _split_daily_candles(candles: list[dict]) -> list[list[dict]]:
    usable = candles[-WEEK_MINUTES:]
    return [
        usable[index : index + DAY_MINUTES]
        for index in range(0, len(usable), DAY_MINUTES)
        if len(usable[index : index + DAY_MINUTES]) >= int(DAY_MINUTES * 0.95)
    ]


def rolling_out_of_range_risk(
    candles: list[dict],
    range_rate: float,
    window_minutes: int = DAY_MINUTES,
    stride_minutes: int = 360,
) -> dict:
    if len(candles) < window_minutes + 1:
        return {"risk": 0.0, "breaches": 0, "samples": 0}

    breaches = 0
    samples = 0
    # Every sample must have a complete future 24h path after its center point.
    last_start = len(candles) - window_minutes - 1
    for start in range(0, last_start + 1, stride_minutes):
        center = candles[start]["close"]
        lower = center * (1 - range_rate)
        upper = center * (1 + range_rate)
        window = candles[start + 1 : start + window_minutes + 1]
        if not window:
            continue
        samples += 1
        if min(item["low"] for item in window) < lower or max(
            item["high"] for item in window
        ) > upper:
            breaches += 1

    return {
        "risk": breaches / samples if samples else 0.0,
        "breaches": breaches,
        "samples": samples,
    }


def _profit_rate_values(
    levels: list[float], round_trip_fee_rate: float
) -> list[float]:
    rates = []
    for lower, upper in zip(levels, levels[1:]):
        if lower > 0:
            rates.append((upper - lower) / lower - round_trip_fee_rate)
    return rates


def _profit_rate_range(levels: list[float], round_trip_fee_rate: float) -> dict:
    rates = _profit_rate_values(levels, round_trip_fee_rate)
    if not rates:
        return {"min": 0.0, "max": 0.0, "average": 0.0}
    return {
        "min": min(rates),
        "max": max(rates),
        "average": sum(rates) / len(rates),
    }


def _daily_structure(
    daily_candles: list[list[dict]],
    range_rate: float,
    target_gap_rate: float,
    tick_size: float,
    round_trip_fee_rate: float,
    fine_path: dict | None = None,
) -> dict:
    rows = []
    for day_index, day in enumerate(daily_candles):
        center = day[0]["open"]
        grid = build_arithmetic_grid(center, range_rate, target_gap_rate, tick_size)
        path_method = "1mClose"
        window_end = day[-1]["closeTime"]
        prices = [center] + [item["close"] for item in day]
        fine_usable = bool(
            day_index == len(daily_candles) - 1
            and fine_path
            and fine_path.get("complete")
            and fine_path.get("fromMs") is not None
            and fine_path.get("toMs") is not None
            and fine_path["fromMs"] <= day[0]["openTime"]
            and fine_path["toMs"] > day[0]["openTime"]
            and fine_path.get("times")
        )
        if fine_usable:
            fine_times = fine_path.get("analysisTimes") or fine_path["times"]
            fine_prices = fine_path.get("analysisPrices") or fine_path["prices"]
            first_trade = bisect_left(fine_times, day[0]["openTime"])
            last_trade = bisect_right(fine_times, fine_path["toMs"])
            exact_prices = fine_prices[first_trade:last_trade]
            if exact_prices:
                prices = [center]
                prices.extend(exact_prices)
                conservative_tail = [
                    item["close"]
                    for item in day
                    if item["closeTime"] > fine_path["toMs"]
                ]
                prices.extend(conservative_tail)
                path_method = (
                    "aggTradesWith1mTail" if conservative_tail else "aggTrades"
                )
                window_end = max(day[-1]["closeTime"], fine_path["toMs"])
        result = count_grid_roundtrips(prices, grid["levels"])
        net_rates = _profit_rate_values(grid["levels"], round_trip_fee_rate)
        profit_rate_units = sum(
            count * max(0.0, rate)
            for count, rate in zip(result["roundTripsByInterval"], net_rates)
        )
        lower = grid["lower"]
        upper = grid["upper"]
        breached = min(item["low"] for item in day) < lower or max(
            item["high"] for item in day
        ) > upper
        rows.append(
            {
                "date": datetime.fromtimestamp(
                    day[0]["openTime"] / 1000, tz=timezone.utc
                ).strftime("%Y-%m-%d %H:%M"),
                "windowStart": _iso_from_ms(day[0]["openTime"]),
                "windowEnd": _iso_from_ms(window_end),
                "roundTrips": result["roundTrips"],
                "utilization": result["utilization"],
                "breached": breached,
                "pathMethod": path_method,
                "profitRateUnits": profit_rate_units,
            }
        )

    trips = [row["roundTrips"] for row in rows]
    utilization = [row["utilization"] for row in rows]
    profit_rate_units = [row["profitRateUnits"] for row in rows]
    mean_trips = sum(trips) / len(trips) if trips else 0.0
    if len(trips) > 1 and mean_trips > 0:
        stability = max(0.0, 1 - statistics.pstdev(trips) / mean_trips)
    else:
        stability = 1.0 if mean_trips > 0 else 0.0

    return {
        "days": rows,
        "totalRoundTrips": sum(trips),
        "averageRoundTripsPerDay": mean_trips,
        "medianRoundTripsPerDay": statistics.median(trips) if trips else 0.0,
        "minRoundTripsPerDay": min(trips) if trips else 0,
        "maxRoundTripsPerDay": max(trips) if trips else 0,
        "averageUtilization": sum(utilization) / len(utilization) if utilization else 0.0,
        "averageProfitRateUnits": (
            sum(profit_rate_units) / len(profit_rate_units)
            if profit_rate_units
            else 0.0
        ),
        "totalProfitRateUnits": sum(profit_rate_units),
        "stability": stability,
        "finePathUsed": any(row["pathMethod"].startswith("aggTrades") for row in rows),
    }


def _quantity_and_capital(
    investment: float,
    leverage: int,
    grid_count: int,
    mark_price: float,
    rules: dict,
) -> dict:
    target_notional_per_grid = investment * leverage / grid_count
    raw_quantity = target_notional_per_grid / mark_price if mark_price > 0 else 0.0
    quantity = _round_down(raw_quantity, rules["stepSize"])
    notional = quantity * mark_price
    feasible = quantity >= rules["minQty"] and notional >= rules["minNotional"]
    estimated_total_investment = (
        notional * grid_count / leverage if leverage > 0 else 0.0
    )
    return {
        "quantityPerOrder": quantity,
        "notionalPerGrid": notional,
        "targetNotionalPerGrid": target_notional_per_grid,
        "estimatedTotalInvestment": estimated_total_investment,
        "feasible": feasible,
    }


def _classify_er(value: float) -> dict:
    if value <= 0.20:
        return {"state": "震荡", "mode": "Neutral", "level": "good"}
    if value <= 0.35:
        return {"state": "混合（谨慎）", "mode": "Neutral", "level": "caution"}
    return {"state": "趋势", "mode": "不建议开网格", "level": "avoid"}


def _get_analysis_lock(key: tuple) -> threading.Lock:
    """Coalesce identical CPU-heavy analyses without retaining locks forever."""
    with _analysis_locks_lock:
        lock = _analysis_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _analysis_locks[key] = lock
        return lock


def _analysis_cache_key(
    snapshot: dict,
    symbol: str,
    investment: float,
    leverage: int,
    fee_rate: float,
    range_rate: float | None,
) -> tuple | None:
    fine_path = snapshot.get("finePath") or {}
    if not fine_path.get("complete"):
        return None
    return (
        symbol,
        float(investment),
        int(leverage),
        float(fee_rate),
        range_rate,
        int(snapshot["serverTime"]),
        fine_path.get("status"),
        fine_path.get("toMs"),
        fine_path.get("tradeCount"),
        bool(fine_path.get("refreshFailed")),
        bool(snapshot.get("marketDataStale")),
    )


def _with_current_snapshot_flags(result: dict, snapshot: dict) -> dict:
    """Keep request-specific cache diagnostics accurate on a reused result."""
    copied = dict(result)
    coverage = dict(result.get("dataCoverage", {}))
    coverage["cacheHit"] = snapshot.get("cacheHit", False)
    coverage["marketDataStale"] = snapshot.get("marketDataStale", False)
    copied["dataCoverage"] = coverage
    return copied


def _analyze_with_cache(
    snapshot: dict,
    symbol: str,
    investment: float,
    leverage: int,
    fee_rate: float,
    range_rate: float | None,
) -> dict:
    """Reuse one exact analysis for identical market data and user inputs."""
    key = _analysis_cache_key(
        snapshot, symbol, investment, leverage, fee_rate, range_rate
    )
    if key is None:
        return analyze_snapshot(
            snapshot, symbol, investment, leverage, fee_rate, range_rate
        )

    analysis_lock = _get_analysis_lock(key)
    with analysis_lock:
        with _analysis_cache_lock:
            cached = _analysis_cache.get(key)
        if cached is None:
            cached = analyze_snapshot(
                snapshot, symbol, investment, leverage, fee_rate, range_rate
            )
            with _analysis_cache_lock:
                _analysis_cache[key] = cached
                while len(_analysis_cache) > ANALYSIS_CACHE_MAX_ENTRIES:
                    oldest_key = next(iter(_analysis_cache))
                    del _analysis_cache[oldest_key]
        return _with_current_snapshot_flags(cached, snapshot)


def analyze_snapshot(
    snapshot: dict,
    symbol: str,
    investment: float,
    leverage: int,
    fee_rate: float,
    range_rate: float | None = None,
) -> dict:
    selected_range_rate = _normalize_range_rate(range_rate)
    candles = snapshot["candles"][-WEEK_MINUTES:]
    daily_candles = _split_daily_candles(candles)
    rules = snapshot["rules"]
    mark_price = snapshot["mark"]["markPrice"]
    tick_size = rules["tickSize"]
    round_trip_fee_rate = fee_rate * 2
    fine_path = snapshot.get("finePath") or {}

    closes_5m = resample_closes(candles, 5)
    er_7d = efficiency_ratio(closes_5m)
    er_24h = efficiency_ratio(closes_5m[-288:])
    regime = _classify_er(er_24h)

    recent_24h = candles[-DAY_MINUTES:]
    high_24h = max(item["high"] for item in recent_24h)
    low_24h = min(item["low"] for item in recent_24h)
    price_range_24h = (high_24h - low_24h) / mark_price if mark_price > 0 else 0.0

    risk_by_range = {
        range_rate: rolling_out_of_range_risk(candles, range_rate)
        for range_rate in RANGE_CANDIDATES
    }

    combinations = []
    for range_rate in RANGE_CANDIDATES:
        rolling_risk = risk_by_range[range_rate]
        for target_gap_rate in GAP_CANDIDATES:
            current_grid = build_arithmetic_grid(
                mark_price, range_rate, target_gap_rate, tick_size
            )
            daily = _daily_structure(
                daily_candles,
                range_rate,
                target_gap_rate,
                tick_size,
                round_trip_fee_rate,
                fine_path,
            )
            profit_rates = _profit_rate_range(
                current_grid["levels"], round_trip_fee_rate
            )
            capital = _quantity_and_capital(
                investment,
                leverage,
                current_grid["numberOfGrids"],
                mark_price,
                rules,
            )
            daily_score = (
                daily["averageProfitRateUnits"] * capital["notionalPerGrid"]
            )
            total_7d_score = (
                daily["totalProfitRateUnits"] * capital["notionalPerGrid"]
            )
            risk_adjusted_score = daily_score * (1 - rolling_risk["risk"])

            combinations.append(
                {
                    "rangeRate": range_rate,
                    "targetGapRate": target_gap_rate,
                    "actualGapAtMark": current_grid["actualGapAtCenter"],
                    "lower": current_grid["lower"],
                    "upper": current_grid["upper"],
                    "numberOfGrids": current_grid["numberOfGrids"],
                    "priceStep": current_grid["priceStep"],
                    "profitGridMin": profit_rates["min"],
                    "profitGridMax": profit_rates["max"],
                    "profitGridAverage": profit_rates["average"],
                    **capital,
                    **daily,
                    "outOfRangeRisk": rolling_risk["risk"],
                    "riskBreaches": rolling_risk["breaches"],
                    "riskSamples": rolling_risk["samples"],
                    "dailyHarvestScore": daily_score,
                    "total7dHarvestScore": total_7d_score,
                    "riskAdjustedScore": risk_adjusted_score,
                    "latest24hRoundTrips": (
                        daily["days"][-1]["roundTrips"] if daily["days"] else 0
                    ),
                    "latest24hPathMethod": (
                        daily["days"][-1]["pathMethod"]
                        if daily["days"]
                        else "1mClose"
                    ),
                }
            )

    eligible = [
        item
        for item in combinations
        if item["feasible"] and item["profitGridMin"] > 0
    ]
    if not eligible:
        raise BinanceMarketDataError("No feasible grid combination for the selected investment")
    recommendation_candidates = (
        eligible
        if selected_range_rate is None
        else [item for item in eligible if item["rangeRate"] == selected_range_rate]
    )
    if not recommendation_candidates:
        raise BinanceMarketDataError(
            "No feasible grid combination for the selected investment and range"
        )
    all_harvest_ranked = sorted(
        eligible,
        key=lambda item: (
            item["dailyHarvestScore"],
            item["total7dHarvestScore"],
            item["stability"],
        ),
        reverse=True,
    )
    steady_ranked = sorted(
        recommendation_candidates,
        key=lambda item: (
            item["riskAdjustedScore"],
            item["dailyHarvestScore"],
            item["stability"],
        ),
        reverse=True,
    )
    harvest_ranked = sorted(
        recommendation_candidates,
        key=lambda item: (
            item["dailyHarvestScore"],
            item["total7dHarvestScore"],
            item["stability"],
        ),
        reverse=True,
    )

    best = harvest_ranked[0]
    steady_alternative = steady_ranked[0]
    recommended_mode = regime["mode"]
    if best["averageRoundTripsPerDay"] <= 0 or best["profitGridMin"] <= 0:
        recommended_mode = "不建议开网格"

    gap_items = sorted(
        [item for item in eligible if item["rangeRate"] == best["rangeRate"]],
        key=lambda item: item["targetGapRate"],
    )
    range_items = []
    for range_rate in RANGE_CANDIDATES:
        candidates = [
            item for item in all_harvest_ranked if item["rangeRate"] == range_rate
        ]
        if candidates:
            range_items.append(candidates[0])

    def without_daily_rows(item: dict) -> dict:
        return {key: value for key, value in item.items() if key != "days"}

    gap_rows = [without_daily_rows(item) for item in gap_items]
    range_rows = [without_daily_rows(item) for item in range_items]
    ranking_rows = [without_daily_rows(item) for item in harvest_ranked[:15]]

    price_decimals = _decimal_places(rules["tickSize"])
    quantity_decimals = _decimal_places(rules["stepSize"])
    book = snapshot["book"]

    if regime["level"] == "good":
        trend_explanation = "价格整体更接近来回震荡，适合观察中性网格机会。"
    elif regime["level"] == "caution":
        trend_explanation = "震荡和单边走势并存，仍可观察中性网格，但需要更谨慎。"
    else:
        trend_explanation = "单边走势比较明显，中性网格容易在一侧持续积累仓位。"

    if best["outOfRangeRisk"] == 0:
        risk_explanation = "历史滚动检测中没有出现 24 小时跑出该区间的情况。"
    else:
        risk_explanation = (
            f"历史上约 {best['outOfRangeRisk'] * 100:.1f}% 的 24 小时窗口"
            "曾跑出这个价格区间。"
        )

    if best["finePathUsed"]:
        path_explanation = (
            "最近一个窗口已使用官方逐笔聚合成交路径；增量更新期间的极短"
            "尾部及其余六个窗口使用 1 分钟收盘路径作为偏保守的历史参照。"
        )
    elif fine_path.get("status") == "error":
        path_explanation = (
            "24 小时精细成交路径暂时读取失败，当前先使用 7 天 1 分钟"
            "收盘路径保守测算。"
        )
    else:
        path_explanation = (
            "24 小时精细成交路径正在后台准备，当前先使用 7 天 1 分钟"
            "收盘路径保守测算，完成后会自动更新。"
        )

    range_explanation = (
        f"你选择了当前价格上下 ±{selected_range_rate * 100:.0f}% 的区间；在该区间内，"
        if selected_range_rate is not None
        else (
            "系统比较全部可选区间后，选择了当前价格上下 "
            f"±{best['rangeRate'] * 100:.0f}% 的区间；"
        )
    )
    reason = (
        f"近24小时单边趋势强度为 {er_24h * 100:.1f}%，{trend_explanation}"
        f"使用过去7个独立24小时窗口测算，{range_explanation}"
        f"每格约 {_format_gap_percent(best['targetGapRate'])}% 的参数，"
        f"平均每天完成 {best['averageRoundTripsPerDay']:.1f} 次有效往返。"
        f"{path_explanation}{risk_explanation}"
        f"历史网格利润估算已按单边万分之 {fee_rate * 10000:g} 扣除买卖两边手续费。"
    )
    if recommended_mode == "不建议开网格":
        reason += " 因此下方参数目前只供观察，不建议立即创建。"

    if steady_alternative is best:
        steady_reason = "收益优先方案同时也是本次较少出界的参考方案。"
    else:
        steady_reason = (
            f"如果更重视少出界，可参考上下 ±{steady_alternative['rangeRate'] * 100:.0f}%、"
            f"每格约 {_format_gap_percent(steady_alternative['targetGapRate'])}% 的组合。"
            "它只用于比较历史出界情况，不替代按历史网格利润估算得到的主推荐。"
        )

    return {
        "symbol": symbol,
        "generatedAt": _iso_from_ms(snapshot["serverTime"]),
        "officialManualUrl": (
            f"https://www.binance.com/en/trading-bots/futures/grid/{symbol}"
        ),
        "market": {
            "markPrice": mark_price,
            "indexPrice": snapshot["mark"]["indexPrice"],
            "bid": book["bid"],
            "ask": book["ask"],
            "spread": book["spread"],
            "spreadRate": book["spreadRate"],
            "high24h": high_24h,
            "low24h": low_24h,
            "priceRange24h": price_range_24h,
        },
        "assumptions": {
            "investment": investment,
            "leverage": leverage,
            "selectedRangeRate": selected_range_rate,
            "oneSideFeeRate": fee_rate,
            "roundTripFeeRate": round_trip_fee_rate,
            "fundingIncluded": False,
            "direction": "Neutral",
            "marginMode": "Cross",
            "gridTrigger": "Immediate",
            "trailingUp": False,
            "trailingDown": False,
            "takeProfitStopLoss": False,
            "closeAllPositionsOnStop": False,
            "autoAddMarginOnBracketChange": False,
            "quantityCalculation": "equalNotionalEstimate",
        },
        "trend": {
            "er24h": er_24h,
            "er7d": er_7d,
            "state": regime["state"],
            "level": regime["level"],
            "recommendedMode": recommended_mode,
            "thresholds": {"neutral": 0.20, "avoid": 0.35},
        },
        "recommendation": {
            **{key: value for key, value in best.items() if key != "days"},
            "days": best["days"],
            "mode": recommended_mode,
            "reason": reason,
        },
        "steadyAlternative": {
            **without_daily_rows(steady_alternative),
            "sameAsRecommendation": steady_alternative is best,
            "reason": steady_reason,
        },
        "gapAnalysis": gap_rows,
        "rangeAnalysis": range_rows,
        "rankings": ranking_rows,
        "selection": {
            "rangeSelectionMode": (
                "manual" if selected_range_rate is not None else "automatic"
            ),
            "selectedRangeRate": selected_range_rate,
            "harvestRankingBasis": "dailyHarvestScore",
            "recommendationBasis": "dailyHarvestScore",
            "steadyAlternativeBasis": (
                "riskAdjustedScore = dailyHarvestScore * (1 - outOfRangeRisk)"
            ),
        },
        "contractRules": {
            **rules,
            "priceDecimals": price_decimals,
            "quantityDecimals": quantity_decimals,
        },
        "dataCoverage": {
            "klineInterval": "1m",
            "klineCount": len(candles),
            "expectedKlineCount": WEEK_MINUTES,
            "completeness": min(1.0, len(candles) / WEEK_MINUTES),
            "from": _iso_from_ms(candles[0]["openTime"]),
            "to": _iso_from_ms(candles[-1]["closeTime"]),
            "dailyWindows": len(daily_candles),
            "erResampleInterval": "5m (derived from 1m)",
            "finePathStatus": fine_path.get("status", "idle"),
            "finePathComplete": bool(fine_path.get("complete")),
            "finePathUsed": best["finePathUsed"],
            "finePathProgress": fine_path.get("progress", 0.0),
            "finePathTradeCount": fine_path.get("tradeCount", 0),
            "finePathAnalysisPointCount": fine_path.get("analysisPointCount", 0),
            "finePathPages": fine_path.get("pages", 0),
            "finePathArchiveDay": fine_path.get("archiveDay"),
            "finePathArchiveTradeCount": fine_path.get("archiveTradeCount", 0),
            "finePathFrom": (
                _iso_from_ms(fine_path["fromMs"])
                if fine_path.get("fromMs") is not None
                else None
            ),
            "finePathTo": (
                _iso_from_ms(fine_path["toMs"])
                if fine_path.get("toMs") is not None
                else None
            ),
            "finePathRefreshFailed": fine_path.get("refreshFailed", False),
            "cacheHit": snapshot.get("cacheHit", False),
            "marketDataStale": snapshot.get("marketDataStale", False),
            "method": (
                "The latest window uses complete 24h aggTrades when ready; older windows "
                "use conservative 1m close paths. High/low is used for range breaches."
            ),
        },
    }


def optimize_grid(
    symbol: str = "MUUSDT",
    investment: float = DEFAULT_INVESTMENT,
    leverage: int = DEFAULT_LEVERAGE,
    fee_rate: float = DEFAULT_FEE_RATE,
    force_refresh: bool = False,
    range_rate: float | None = None,
) -> dict:
    normalized_symbol = symbol.upper().strip()
    if normalized_symbol not in SUPPORTED_GRID_SYMBOLS:
        raise ValueError(f"Unsupported grid symbol: {normalized_symbol}")
    if not math.isfinite(investment) or investment <= 0:
        raise ValueError("Investment must be a positive number")
    max_leverage = GRID_SYMBOL_CONFIGS[normalized_symbol]["maxLeverage"]
    if not isinstance(leverage, int) or not 1 <= leverage <= max_leverage:
        raise ValueError(f"Leverage must be an integer from 1 to {max_leverage}")
    if not math.isfinite(fee_rate) or not 0 <= fee_rate < 0.01:
        raise ValueError("One-side fee rate must be between 0 and 1%")
    selected_range_rate = _normalize_range_rate(range_rate)

    snapshot = get_market_snapshot(normalized_symbol, force_refresh=force_refresh)
    return _analyze_with_cache(
        snapshot,
        normalized_symbol,
        float(investment),
        leverage,
        float(fee_rate),
        selected_range_rate,
    )
