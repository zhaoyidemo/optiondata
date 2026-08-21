"""Transparent Binance USD-M futures grid-structure analysis.

This module only consumes public market-data endpoints.  It never reads an
account and never creates, changes, or cancels an order.
"""

from __future__ import annotations

import math
import os
import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP, ROUND_UP
from typing import Iterable

import requests


BINANCE_FAPI_BASE = os.environ.get("BINANCE_FAPI_BASE", "https://fapi.binance.com")
SUPPORTED_GRID_SYMBOLS = ("MUUSDT",)
GAP_CANDIDATES = tuple(round(value / 1000, 3) for value in range(4, 13))
RANGE_CANDIDATES = (0.06, 0.08, 0.10)

DEFAULT_INVESTMENT = 100_000.0
DEFAULT_FEE_RATE = 0.0001  # One side: 0.01% (1 bp / 万一)
DEFAULT_LEVERAGE = 1
MAX_STOCK_PERP_LEVERAGE = 10

MINUTE_MS = 60_000
DAY_MINUTES = 24 * 60
WEEK_MINUTES = 7 * DAY_MINUTES
MARKET_CACHE_SECONDS = 60

_market_cache: dict[str, tuple[float, dict]] = {}
_market_cache_lock = threading.Lock()


class BinanceMarketDataError(RuntimeError):
    """Raised when public Binance market data is unavailable or malformed."""


def _get_json(path: str, params: dict | None = None, timeout: int = 20):
    url = f"{BINANCE_FAPI_BASE}{path}"
    try:
        response = requests.get(url, params=params, timeout=timeout)
        response.raise_for_status()
        return response.json()
    except (requests.RequestException, ValueError) as exc:
        raise BinanceMarketDataError(f"Binance public API request failed: {path}") from exc


def _iso_from_ms(timestamp_ms: int) -> str:
    value = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def _decimal_places(step: float) -> int:
    normalized = Decimal(str(step)).normalize()
    return max(0, -normalized.as_tuple().exponent)


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
        "maxLeverage": MAX_STOCK_PERP_LEVERAGE,
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


def fetch_recent_agg_trades(symbol: str, limit: int = 1000) -> list[dict]:
    payload = _get_json(
        "/fapi/v1/aggTrades",
        {"symbol": symbol, "limit": min(max(int(limit), 1), 1000)},
    )
    return [
        {
            "id": int(item["a"]),
            "price": float(item["p"]),
            "quantity": float(item["q"]),
            "time": int(item["T"]),
        }
        for item in payload
    ]


def fetch_week_1m_klines(symbol: str, server_time_ms: int) -> list[dict]:
    """Fetch exactly seven days of completed one-minute candles."""
    last_open_time = (server_time_ms // MINUTE_MS) * MINUTE_MS - MINUTE_MS
    first_open_time = last_open_time - (WEEK_MINUTES - 1) * MINUTE_MS
    cursor = first_open_time
    candles_by_time: dict[int, dict] = {}

    while cursor <= last_open_time:
        payload = _get_json(
            "/fapi/v1/klines",
            {
                "symbol": symbol,
                "interval": "1m",
                "startTime": cursor,
                "endTime": last_open_time + MINUTE_MS - 1,
                "limit": 1500,
            },
            timeout=25,
        )
        if not payload:
            break

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

        next_cursor = int(payload[-1][0]) + MINUTE_MS
        if next_cursor <= cursor:
            break
        cursor = next_cursor

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


def get_market_snapshot(symbol: str, force_refresh: bool = False) -> dict:
    now = time.time()
    with _market_cache_lock:
        cached = _market_cache.get(symbol)
        if cached and not force_refresh and now - cached[0] < MARKET_CACHE_SECONDS:
            snapshot = dict(cached[1])
            snapshot["cacheHit"] = True
            return snapshot

    server_time_ms = fetch_server_time()
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {
            "rules": executor.submit(fetch_symbol_rules, symbol),
            "book": executor.submit(fetch_book_ticker, symbol),
            "mark": executor.submit(fetch_mark_price, symbol),
            "aggTrades": executor.submit(fetch_recent_agg_trades, symbol, 1000),
            "candles": executor.submit(fetch_week_1m_klines, symbol, server_time_ms),
        }
        snapshot = {name: future.result() for name, future in futures.items()}

    if snapshot["rules"]["status"] != "TRADING":
        raise BinanceMarketDataError(
            f"{symbol} is not currently TRADING ({snapshot['rules']['status']})"
        )

    snapshot["serverTime"] = server_time_ms
    snapshot["cacheHit"] = False
    with _market_cache_lock:
        _market_cache[symbol] = (time.time(), snapshot)
    return dict(snapshot)


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
    price_values = [float(value) for value in prices if float(value) > 0]
    interval_count = max(0, len(levels) - 1)
    if len(price_values) < 2 or interval_count == 0:
        return {"roundTrips": 0, "traversedIntervals": 0, "utilization": 0.0}

    last_side: list[int | None] = [None] * interval_count
    last_direction: list[int | None] = [None] * interval_count
    traversed: set[int] = set()
    round_trips = 0
    epsilon = max(1e-12, abs(levels[-1] - levels[0]) * 1e-12)

    for price in price_values:
        for index in range(interval_count):
            lower = levels[index]
            upper = levels[index + 1]
            if price <= lower + epsilon:
                side = -1
            elif price >= upper - epsilon:
                side = 1
            else:
                continue

            previous_side = last_side[index]
            if previous_side is None:
                last_side[index] = side
                continue
            if side == previous_side:
                continue

            direction = 1 if side > previous_side else -1
            traversed.add(index)
            previous_direction = last_direction[index]
            if previous_direction is not None and direction != previous_direction:
                round_trips += 1
            last_direction[index] = direction
            last_side[index] = side

    return {
        "roundTrips": round_trips,
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


def _profit_rate_range(levels: list[float], round_trip_fee_rate: float) -> dict:
    rates = []
    for lower, upper in zip(levels, levels[1:]):
        if lower > 0:
            rates.append((upper - lower) / lower - round_trip_fee_rate)
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
    tail_prices: list[float] | None = None,
) -> dict:
    rows = []
    for day_index, day in enumerate(daily_candles):
        center = day[0]["open"]
        grid = build_arithmetic_grid(center, range_rate, target_gap_rate, tick_size)
        prices = [center] + [item["close"] for item in day]
        if day_index == len(daily_candles) - 1 and tail_prices:
            prices.extend(tail_prices)
        result = count_grid_roundtrips(prices, grid["levels"])
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
                "windowEnd": _iso_from_ms(day[-1]["closeTime"]),
                "roundTrips": result["roundTrips"],
                "utilization": result["utilization"],
                "breached": breached,
            }
        )

    trips = [row["roundTrips"] for row in rows]
    utilization = [row["utilization"] for row in rows]
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
        "stability": stability,
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


def analyze_snapshot(
    snapshot: dict,
    symbol: str,
    investment: float,
    leverage: int,
    fee_rate: float,
) -> dict:
    candles = snapshot["candles"][-WEEK_MINUTES:]
    daily_candles = _split_daily_candles(candles)
    rules = snapshot["rules"]
    mark_price = snapshot["mark"]["markPrice"]
    tick_size = rules["tickSize"]
    round_trip_fee_rate = fee_rate * 2
    agg_trades = snapshot["aggTrades"]
    latest_close_time = candles[-1]["closeTime"]
    agg_after_close = [item for item in agg_trades if item["time"] > latest_close_time]
    current_tail_prices = [item["price"] for item in agg_after_close]

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
                current_tail_prices,
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
                daily["averageRoundTripsPerDay"]
                * capital["notionalPerGrid"]
                * max(0.0, profit_rates["average"])
            )
            total_7d_score = (
                daily["totalRoundTrips"]
                * capital["notionalPerGrid"]
                * max(0.0, profit_rates["average"])
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
                }
            )

    eligible = [
        item
        for item in combinations
        if item["feasible"] and item["profitGridMin"] > 0
    ]
    recommendation_ranked = sorted(
        eligible,
        key=lambda item: (
            item["riskAdjustedScore"],
            item["dailyHarvestScore"],
            item["stability"],
        ),
        reverse=True,
    )
    if not recommendation_ranked:
        raise BinanceMarketDataError("No feasible grid combination for the selected investment")

    harvest_ranked = sorted(
        eligible,
        key=lambda item: (
            item["dailyHarvestScore"],
            item["total7dHarvestScore"],
            item["stability"],
        ),
        reverse=True,
    )

    best = recommendation_ranked[0]
    recommended_mode = regime["mode"]
    if best["averageRoundTripsPerDay"] <= 0 or best["profitGridMin"] <= 0:
        recommended_mode = "不建议开网格"

    gap_items = sorted(
        [item for item in combinations if item["rangeRate"] == best["rangeRate"]],
        key=lambda item: item["targetGapRate"],
    )
    range_items = []
    for range_rate in RANGE_CANDIDATES:
        candidates = [
            item for item in recommendation_ranked if item["rangeRate"] == range_rate
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

    reason = (
        f"最近24小时 5m ER 为 {er_24h:.3f}，当前属于{regime['state']}状态。"
        f"在过去7个独立24小时窗口中，±{best['rangeRate'] * 100:.0f}% / "
        f"目标 Gap {best['targetGapRate'] * 100:.1f}% 的组合日均完成 "
        f"{best['averageRoundTripsPerDay']:.1f} 次有效往返，"
        f"滚动24小时出界风险为 {best['outOfRangeRisk'] * 100:.1f}%。"
        f"单边手续费 {fee_rate * 100:.3f}% 已按双边扣除。"
    )
    if recommended_mode == "不建议开网格":
        reason += " 当前趋势效率超过 Neutral Grid 阈值，因此参数仅供观察，不建议立即创建。"

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
            "oneSideFeeRate": fee_rate,
            "roundTripFeeRate": round_trip_fee_rate,
            "fundingIncluded": False,
            "direction": "Neutral",
            "marginMode": "Cross",
            "gridTrigger": "Immediate",
            "trailingUp": False,
            "trailingDown": False,
            "takeProfitStopLoss": False,
            "autoAddMargin": False,
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
        "gapAnalysis": gap_rows,
        "rangeAnalysis": range_rows,
        "rankings": ranking_rows,
        "selection": {
            "harvestRankingBasis": "dailyHarvestScore",
            "recommendationBasis": (
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
            "aggTradeCount": len(agg_trades),
            "aggTradeFrom": _iso_from_ms(agg_trades[0]["time"]) if agg_trades else None,
            "aggTradeTo": _iso_from_ms(agg_trades[-1]["time"]) if agg_trades else None,
            "aggTradesAfterLastClose": len(agg_after_close),
            "cacheHit": snapshot.get("cacheHit", False),
            "method": (
                "Seven completed 24h windows use 1m close paths. High/low is only used "
                "for range breaches. Trades after the last completed candle supplement the "
                "current unfinished minute from the latest 1,000 aggTrades."
            ),
        },
    }


def optimize_grid(
    symbol: str = "MUUSDT",
    investment: float = DEFAULT_INVESTMENT,
    leverage: int = DEFAULT_LEVERAGE,
    fee_rate: float = DEFAULT_FEE_RATE,
    force_refresh: bool = False,
) -> dict:
    normalized_symbol = symbol.upper().strip()
    if normalized_symbol not in SUPPORTED_GRID_SYMBOLS:
        raise ValueError(f"Unsupported grid symbol: {normalized_symbol}")
    if not math.isfinite(investment) or investment <= 0:
        raise ValueError("Investment must be a positive number")
    if not isinstance(leverage, int) or not 1 <= leverage <= MAX_STOCK_PERP_LEVERAGE:
        raise ValueError(f"Leverage must be an integer from 1 to {MAX_STOCK_PERP_LEVERAGE}")
    if not math.isfinite(fee_rate) or not 0 <= fee_rate < 0.01:
        raise ValueError("One-side fee rate must be between 0 and 1%")

    snapshot = get_market_snapshot(normalized_symbol, force_refresh=force_refresh)
    return analyze_snapshot(
        snapshot,
        normalized_symbol,
        float(investment),
        leverage,
        float(fee_rate),
    )
