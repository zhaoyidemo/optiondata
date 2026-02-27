import os
import time
import hmac
import hashlib
import math
import traceback
from datetime import datetime, timezone
from urllib.parse import urlencode

import requests
from flask import Flask, jsonify, request, send_from_directory

app = Flask(__name__, static_folder="static")

BINANCE_API_KEY = os.environ.get("BINANCE_API_KEY", "")
BINANCE_API_SECRET = os.environ.get("BINANCE_API_SECRET", "")

# 币安 API 基础 URL（可通过环境变量覆盖，用于代理或切换域名）
BINANCE_API_BASE = os.environ.get("BINANCE_API_BASE", "https://api.binance.com")
BINANCE_EAPI_BASE = os.environ.get("BINANCE_EAPI_BASE", "https://eapi.binance.com")

SUPPORTED_COINS = ["ETH", "BTC"]


# ── helpers ──────────────────────────────────────────────────────────────────

def _sign(params: dict) -> dict:
    """Add timestamp and HMAC-SHA256 signature to params."""
    params["timestamp"] = int(time.time() * 1000)
    query = urlencode(params)
    sig = hmac.new(
        BINANCE_API_SECRET.encode(), query.encode(), hashlib.sha256
    ).hexdigest()
    params["signature"] = sig
    return params


def _headers() -> dict:
    return {"X-MBX-APIKEY": BINANCE_API_KEY}


def _format_strike(price) -> str:
    """Format strike price as integer string (3500 not 3500.0)."""
    f = float(price)
    if f == int(f):
        return str(int(f))
    return str(f)


def _expiry_yymmdd(ts_ms) -> str:
    """Convert millisecond timestamp to YYMMDD string."""
    dt = datetime.fromtimestamp(int(ts_ms) / 1000, tz=timezone.utc)
    return dt.strftime("%y%m%d")


def _expiry_date(ts_ms) -> str:
    """Convert millisecond timestamp to YYYY-MM-DD string."""
    dt = datetime.fromtimestamp(int(ts_ms) / 1000, tz=timezone.utc)
    return dt.strftime("%Y-%m-%d")


def _days_until(ts_ms) -> float:
    """Days from now until the given millisecond timestamp."""
    now = datetime.now(timezone.utc)
    target = datetime.fromtimestamp(int(ts_ms) / 1000, tz=timezone.utc)
    delta = (target - now).total_seconds() / 86400
    return round(max(delta, 0.01), 2)


def _days_label(days: float) -> str:
    if days < 1:
        return f"{int(days * 24)}小时"
    return f"{math.ceil(days)}天"


# ── Binance API calls ───────────────────────────────────────────────────────

def fetch_spot_price(coin: str) -> float:
    url = f"{BINANCE_API_BASE}/api/v3/ticker/price?symbol={coin}USDT"
    r = requests.get(url, timeout=10)
    r.raise_for_status()
    return float(r.json()["price"])


def fetch_dual_products(coin: str) -> list:
    """Fetch dual investment products for both CALL and PUT."""
    base_url = f"{BINANCE_API_BASE}/sapi/v1/dci/product/list"
    products = []

    for opt_type, invest, exercised in [
        ("CALL", coin, "USDT"),
        ("PUT", "USDT", coin),
    ]:
        page = 1
        while True:
            params = _sign({
                "optionType": opt_type,
                "exercisedCoin": exercised,
                "investCoin": invest,
                "pageSize": 100,
                "pageIndex": page,
            })
            r = requests.get(base_url, params=params, headers=_headers(), timeout=15)
            r.raise_for_status()
            data = r.json()
            items = data.get("list") or data.get("data", {}).get("list", [])
            if not items:
                break
            for item in items:
                item["_optionType"] = opt_type
                item["_investCoin"] = invest
                item["_exercisedCoin"] = exercised
            products.extend(items)
            total = int(data.get("total", 0) or data.get("data", {}).get("total", 0))
            if page * 100 >= total:
                break
            page += 1

    return products


def fetch_option_tickers(coin: str) -> dict:
    """Fetch all option tickers and return dict keyed by symbol."""
    url = f"{BINANCE_EAPI_BASE}/eapi/v1/ticker"
    r = requests.get(url, timeout=15)
    r.raise_for_status()
    prefix = f"{coin}-"
    result = {}
    for t in r.json():
        sym = t.get("symbol", "")
        if sym.startswith(prefix):
            result[sym] = t
    return result


def fetch_option_depth(symbol: str) -> tuple:
    """Fetch order book and return (best bid price, best bid qty)."""
    url = f"{BINANCE_EAPI_BASE}/eapi/v1/depth?symbol={symbol}&limit=5"
    r = requests.get(url, timeout=10)
    r.raise_for_status()
    bids = r.json().get("bids", [])
    if bids:
        return float(bids[0][0]), float(bids[0][1])
    return 0.0, 0.0


# ── core comparison logic ────────────────────────────────────────────────────

def compare(coin: str) -> dict:
    invest_amount = 100000  # 固定投入金额 10万 USDT
    spot = fetch_spot_price(coin)
    products = fetch_dual_products(coin)
    tickers = fetch_option_tickers(coin)

    results = []
    unmatched = []

    for p in products:
        opt_type = p["_optionType"]
        strike = float(p["strikePrice"])
        settle_ts = int(p["settleDate"])
        dual_apr = float(p["apr"])

        strike_str = _format_strike(strike)
        yymmdd = _expiry_yymmdd(settle_ts)
        cp = "C" if opt_type == "CALL" else "P"
        option_symbol = f"{coin}-{yymmdd}-{strike_str}-{cp}"

        days = _days_until(settle_ts)
        expiry = _expiry_date(settle_ts)

        ticker = tickers.get(option_symbol)
        bid = 0.0
        bid_qty = 0.0
        if ticker:
            bid = float(ticker.get("bidPrice", 0) or 0)
            bid_qty = float(ticker.get("bidQty", 0) or 0)

        if bid <= 0:
            unmatched.append({
                "coin": coin,
                "type": opt_type,
                "typeLabel": f"{opt_type} ({'高卖' if opt_type == 'CALL' else '低买'})",
                "investCoin": p["_investCoin"],
                "strike": strike,
                "expiry": expiry,
                "days": days,
                "daysLabel": _days_label(days),
                "dualAPR": dual_apr,
                "optionSymbol": option_symbol,
                "reason": "no bid" if ticker else "no contract",
            })
            continue

        # 期权交易手续费 0.024%（按名义价值计算；行权费 0.015% 仅实值到期时收取，未计入）
        fee = spot * 0.00024
        net_bid = bid - fee

        if opt_type == "PUT":
            option_apr_gross = (bid / strike) * (365 / days)
            option_apr_net = (net_bid / strike) * (365 / days)
        else:
            option_apr_gross = (bid / spot) * (365 / days)
            option_apr_net = (net_bid / spot) * (365 / days)

        fee_apr = option_apr_gross - option_apr_net
        diff_apr = option_apr_net - dual_apr
        spread_pct = (diff_apr / option_apr_net * 100) if option_apr_net > 0 else 0

        # 10万U 实际利润计算
        period = days / 365
        dual_profit = invest_amount * dual_apr * period
        option_profit = invest_amount * option_apr_net * period
        extra_profit = option_profit - dual_profit

        # Bid 流动性（以 USDT 计）
        bid_notional = bid_qty * spot

        results.append({
            "coin": coin,
            "type": opt_type,
            "typeLabel": f"{opt_type} ({'高卖' if opt_type == 'CALL' else '低买'})",
            "investCoin": p["_investCoin"],
            "strike": strike,
            "expiry": expiry,
            "days": days,
            "daysLabel": _days_label(days),
            "spotPrice": spot,
            "dualAPR": round(dual_apr, 6),
            "optionBid": round(bid, 4),
            "bidQty": round(bid_qty, 4),
            "bidNotional": round(bid_notional, 2),
            "optionAPR": round(option_apr_gross, 6),
            "optionAPRNet": round(option_apr_net, 6),
            "feeAPR": round(fee_apr, 6),
            "diffAPR": round(diff_apr, 6),
            "spreadPct": round(spread_pct, 2),
            "dualProfit": round(dual_profit, 2),
            "optionProfit": round(option_profit, 2),
            "extraProfit": round(extra_profit, 2),
            "optionSymbol": option_symbol,
        })

    spreads = [r["spreadPct"] for r in results]
    diffs = [r["diffAPR"] for r in results]

    stats = {
        "count": len(results),
        "unmatched": len(unmatched),
        "avgSpread": round(sum(spreads) / len(spreads), 2) if spreads else 0,
        "maxSpread": round(max(spreads), 2) if spreads else 0,
        "minSpread": round(min(spreads), 2) if spreads else 0,
        "avgDiffAPR": round(sum(diffs) / len(diffs), 6) if diffs else 0,
    }

    return {
        "coin": coin,
        "spotPrice": spot,
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds") + "Z",
        "results": results,
        "unmatched": unmatched,
        "stats": stats,
    }


# ── routes ───────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/options")
def options_page():
    return send_from_directory("static", "options.html")


@app.route("/api/health")
def health():
    return jsonify({
        "ok": True,
        "apiKeyConfigured": bool(BINANCE_API_KEY and BINANCE_API_SECRET),
    })


@app.route("/api/spot-price")
def api_spot_price():
    coin = request.args.get("coin", "ETH").upper()
    if coin not in SUPPORTED_COINS:
        return jsonify({"ok": False, "error": f"Unsupported coin: {coin}"}), 400
    try:
        price = fetch_spot_price(coin)
        return jsonify({"ok": True, "coin": coin, "price": price})
    except Exception as e:
        tb = traceback.format_exc()
        print(f"[ERROR] /api/spot-price?coin={coin}\n{tb}")
        return jsonify({"ok": False, "error": str(e), "trace": tb}), 500


@app.route("/api/dual-products")
def api_dual_products():
    coin = request.args.get("coin", "ETH").upper()
    if coin not in SUPPORTED_COINS:
        return jsonify({"ok": False, "error": f"Unsupported coin: {coin}"}), 400
    try:
        products = fetch_dual_products(coin)
        return jsonify({"ok": True, "coin": coin, "count": len(products), "products": products})
    except Exception as e:
        tb = traceback.format_exc()
        print(f"[ERROR] /api/dual-products?coin={coin}\n{tb}")
        return jsonify({"ok": False, "error": str(e), "trace": tb}), 500


@app.route("/api/options-tickers")
def api_options_tickers():
    coin = request.args.get("coin", "ETH").upper()
    if coin not in SUPPORTED_COINS:
        return jsonify({"ok": False, "error": f"Unsupported coin: {coin}"}), 400
    try:
        tickers = fetch_option_tickers(coin)
        return jsonify({"ok": True, "coin": coin, "count": len(tickers), "tickers": tickers})
    except Exception as e:
        tb = traceback.format_exc()
        print(f"[ERROR] /api/options-tickers?coin={coin}\n{tb}")
        return jsonify({"ok": False, "error": str(e), "trace": tb}), 500


@app.route("/api/compare")
def api_compare():
    coin = request.args.get("coin", "ETH").upper()
    if coin not in SUPPORTED_COINS:
        return jsonify({"ok": False, "error": f"Unsupported coin: {coin}"}), 400
    try:
        data = compare(coin)
        return jsonify({"ok": True, "data": data})
    except Exception as e:
        tb = traceback.format_exc()
        print(f"[ERROR] /api/compare?coin={coin}\n{tb}")
        return jsonify({"ok": False, "error": str(e), "trace": tb}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"API Key configured: {bool(BINANCE_API_KEY)}")
    print(f"Starting server on http://localhost:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)
