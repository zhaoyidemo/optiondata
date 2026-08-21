import json
import io
import math
import random
import unittest
import zipfile
from array import array
from unittest.mock import Mock, patch

import requests

import grid_optimizer

from grid_optimizer import (
    DAY_MS,
    MINUTE_MS,
    WEEK_MINUTES,
    _classify_er,
    _compress_turning_path,
    _daily_structure,
    _get_json,
    _load_agg_trade_window,
    _load_completed_agg_archive,
    _profit_rate_range,
    _quantity_and_capital,
    analyze_snapshot,
    build_arithmetic_grid,
    count_grid_roundtrips,
    efficiency_ratio,
    optimize_grid,
    rolling_out_of_range_risk,
)
from server import app


class GridMathTests(unittest.TestCase):
    def test_requested_symbols_and_range_widths_are_configured(self):
        self.assertEqual(
            tuple(grid_optimizer.GRID_SYMBOL_CONFIGS),
            (
                "MUUSDT",
                "BMNRUSDT",
                "CRWVUSDT",
                "INTCUSDT",
                "AAOIUSDT",
                "SKHYUSDT",
                "GOOGLUSDT",
                "BTCUSDT",
                "ETHUSDT",
                "SPCXUSDT",
            ),
        )
        self.assertEqual(grid_optimizer.GRID_SYMBOL_CONFIGS["BTCUSDT"]["maxLeverage"], 125)
        self.assertEqual(grid_optimizer.GRID_SYMBOL_CONFIGS["ETHUSDT"]["maxLeverage"], 100)
        self.assertTrue(
            all(
                config["maxLeverage"] == 10
                for symbol, config in grid_optimizer.GRID_SYMBOL_CONFIGS.items()
                if symbol not in {"BTCUSDT", "ETHUSDT"}
            )
        )
        self.assertEqual(
            grid_optimizer.RANGE_CANDIDATES,
            (0.06, 0.08, 0.10, 0.12, 0.14, 0.16, 0.18, 0.20, 0.30),
        )

    def test_efficiency_ratio_distinguishes_trend_and_chop(self):
        self.assertEqual(efficiency_ratio([100, 101, 102, 103]), 1.0)
        self.assertAlmostEqual(efficiency_ratio([100, 101, 100, 101, 100]), 0.0)

    def test_one_way_grid_crossings_do_not_count_as_profit(self):
        levels = [100, 101, 102, 103]
        result = count_grid_roundtrips([100, 101, 102, 103], levels)
        self.assertEqual(result["roundTrips"], 0)
        self.assertEqual(result["traversedIntervals"], 3)

    def test_reversal_counts_only_completed_grid_pairs(self):
        levels = [100, 101, 102, 103]
        one_pair = count_grid_roundtrips([100, 101, 102, 103, 102], levels)
        all_pairs = count_grid_roundtrips([100, 101, 102, 103, 102, 101, 100], levels)
        self.assertEqual(one_pair["roundTrips"], 1)
        self.assertEqual(all_pairs["roundTrips"], 3)

    def test_repeated_oscillation_counts_each_completed_pair(self):
        result = count_grid_roundtrips([100, 101, 100, 101], [100, 101])
        self.assertEqual(result["roundTrips"], 2)
        self.assertEqual(result["roundTripsByInterval"], [2])

    def test_fast_crossing_counter_matches_reference_implementation(self):
        def reference(prices, levels):
            sides = [None] * (len(levels) - 1)
            directions = [None] * (len(levels) - 1)
            counts = [0] * (len(levels) - 1)
            epsilon = max(1e-12, abs(levels[-1] - levels[0]) * 1e-12)
            for price in prices:
                for index, (lower, upper) in enumerate(zip(levels, levels[1:])):
                    side = -1 if price <= lower + epsilon else 1 if price >= upper - epsilon else None
                    if side is None:
                        continue
                    if sides[index] is None:
                        sides[index] = side
                    elif sides[index] != side:
                        direction = 1 if side > sides[index] else -1
                        if directions[index] is not None and directions[index] != direction:
                            counts[index] += 1
                        directions[index] = direction
                        sides[index] = side
            return counts

        rng = random.Random(7)
        levels = [95 + index for index in range(11)]
        for _ in range(100):
            prices = [100.0]
            for _ in range(200):
                prices.append(prices[-1] + rng.choice([-2, -1, 0, 1, 2]))
            result = count_grid_roundtrips(prices, levels)
            self.assertEqual(result["roundTripsByInterval"], reference(prices, levels))
            compact_prices, _ = _compress_turning_path(prices, range(len(prices)))
            compact_result = count_grid_roundtrips(compact_prices, levels)
            self.assertEqual(
                compact_result["roundTripsByInterval"],
                result["roundTripsByInterval"],
            )

    def test_latest_window_uses_complete_aggtrade_path(self):
        start = 1_700_000_000_000
        candles = [
            {
                "openTime": start,
                "open": 100.0,
                "high": 102.0,
                "low": 100.0,
                "close": 102.0,
                "closeTime": start + MINUTE_MS - 1,
            },
            {
                "openTime": start + MINUTE_MS,
                "open": 102.0,
                "high": 102.0,
                "low": 100.0,
                "close": 102.0,
                "closeTime": start + 2 * MINUTE_MS - 1,
            },
        ]
        conservative = _daily_structure([candles], 0.02, 0.01, 0.01, 0.0002)
        fine_path = {
            "complete": True,
            "fromMs": start,
            "toMs": start + 2 * MINUTE_MS,
            "times": array("q", [start + 1, start + 30_000, start + 60_001]),
            "prices": array("d", [100.0, 102.0, 100.0]),
        }
        exact = _daily_structure(
            [candles], 0.02, 0.01, 0.01, 0.0002, fine_path
        )
        self.assertTrue(exact["finePathUsed"])
        self.assertEqual(exact["days"][0]["pathMethod"], "aggTrades")
        self.assertGreater(exact["totalRoundTrips"], conservative["totalRoundTrips"])

    def test_arithmetic_grid_converts_target_gap_to_binance_fields(self):
        grid = build_arithmetic_grid(100, 0.06, 0.01, 0.01)
        self.assertEqual(grid["lower"], 94.0)
        self.assertEqual(grid["upper"], 106.0)
        self.assertEqual(grid["numberOfGrids"], 12)
        self.assertAlmostEqual(grid["actualGapAtCenter"], 0.01)

    def test_profit_rate_deducts_both_sides_of_fee(self):
        rates = _profit_rate_range([100, 101], 0.0002)
        self.assertAlmostEqual(rates["average"], 0.0098)

    def test_quantity_respects_step_size_and_reports_investment(self):
        rules = {"stepSize": 0.01, "minQty": 0.01, "minNotional": 5.0}
        result = _quantity_and_capital(100_000, 2, 20, 1000, rules)
        self.assertEqual(result["quantityPerOrder"], 10.0)
        self.assertEqual(result["notionalPerGrid"], 10_000.0)
        self.assertEqual(result["estimatedTotalInvestment"], 100_000.0)
        self.assertTrue(result["feasible"])

    def test_cautious_er_still_uses_neutral_mode(self):
        result = _classify_er(0.30)
        self.assertEqual(result["mode"], "Neutral")
        self.assertEqual(result["level"], "caution")

    def test_range_risk_uses_only_complete_future_windows(self):
        candles = [{"close": 100.0, "high": 100.0, "low": 100.0}] * 2880
        result = rolling_out_of_range_risk(candles, 0.06)
        self.assertEqual(result["samples"], 4)
        self.assertEqual(result["risk"], 0.0)


class GridMarketDataTests(unittest.TestCase):
    def test_fine_path_cache_keeps_only_recent_completed_symbols(self):
        with patch.dict(grid_optimizer._fine_path_states, {}, clear=True):
            for index, symbol in enumerate(("A", "B", "C", "D"), start=1):
                grid_optimizer._fine_path_states[symbol] = {
                    "thread": None,
                    "lastAccessAt": float(index),
                }
            with grid_optimizer._fine_path_lock:
                grid_optimizer._trim_fine_path_states_locked(preserve_symbol="D")

            self.assertEqual(set(grid_optimizer._fine_path_states), {"B", "C", "D"})

    @patch("grid_optimizer._attach_fine_path")
    @patch("grid_optimizer.fetch_server_time", side_effect=KeyError("serverTime"))
    @patch("grid_optimizer.time.time", return_value=100.0)
    def test_malformed_market_response_uses_recent_stale_snapshot(
        self, time_mock, server_time_mock, attach_mock
    ):
        symbol = "STALE_TEST"
        cached_snapshot = {"source": "cached"}
        attach_mock.side_effect = lambda snapshot, cache_hit, stale=False: {
            "snapshot": snapshot,
            "cacheHit": cache_hit,
            "stale": stale,
        }
        with grid_optimizer._market_cache_lock:
            grid_optimizer._market_cache[symbol] = (30.0, cached_snapshot)
        try:
            result = grid_optimizer.get_market_snapshot(symbol)
        finally:
            with grid_optimizer._market_cache_lock:
                grid_optimizer._market_cache.pop(symbol, None)
            with grid_optimizer._market_fetch_locks_lock:
                grid_optimizer._market_fetch_locks.pop(symbol, None)

        self.assertEqual(result["snapshot"], cached_snapshot)
        self.assertTrue(result["cacheHit"])
        self.assertTrue(result["stale"])

    @patch("grid_optimizer.threading.Thread")
    @patch("grid_optimizer.time.monotonic", return_value=100.0)
    def test_failed_incremental_fine_path_refresh_is_throttled(
        self, monotonic_mock, thread_mock
    ):
        symbol = "THROTTLE_TEST"
        existing = {
            "windowStart": 1_000,
            "windowEnd": 1_000,
            "prices": array("d"),
            "times": array("q"),
            "analysisPrices": array("d"),
            "analysisTimes": array("q"),
            "firstTradeTime": None,
            "lastTradeTime": None,
            "archiveDay": None,
            "archiveTradeCount": 0,
        }
        with grid_optimizer._fine_path_lock:
            grid_optimizer._fine_path_states[symbol] = {
                "status": "ready",
                "progress": 1.0,
                "pages": 1,
                "tradeCount": 0,
                "thread": None,
                "data": existing,
                "error": "temporary failure",
                "lastAttemptAt": 99.0,
            }
        try:
            result = grid_optimizer.get_fine_path_snapshot(symbol, 1_000, 61_000)
        finally:
            with grid_optimizer._fine_path_lock:
                grid_optimizer._fine_path_states.pop(symbol, None)

        self.assertTrue(result["refreshFailed"])
        thread_mock.assert_not_called()

    @patch("grid_optimizer.requests.get")
    def test_completed_official_archive_is_streamed_and_filtered(self, get_mock):
        archive_bytes = io.BytesIO()
        with zipfile.ZipFile(archive_bytes, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(
                "MUUSDT-aggTrades-1970-01-01.csv",
                "agg_trade_id,price,quantity,first_trade_id,last_trade_id,transact_time,is_buyer_maker\n"
                "1,100.0,1,1,1,1000,true\n"
                "2,101.0,1,2,2,2000,false\n",
            )
        response = Mock(status_code=200, content=archive_bytes.getvalue())
        response.raise_for_status.return_value = None
        get_mock.return_value = response
        result = _load_completed_agg_archive("MUUSDT", 1_500, DAY_MS + 5_000)
        self.assertIsNotNone(result)
        self.assertEqual(list(result["prices"]), [101.0])
        self.assertEqual(result["lastId"], 2)
        self.assertEqual(result["archiveDay"], "1970-01-01")

    @patch("grid_optimizer._load_completed_agg_archive", return_value=None)
    @patch("grid_optimizer._get_agg_trade_page")
    def test_full_aggtrade_window_paginates_and_filters_end_time(
        self, page_mock, archive_mock
    ):
        page_mock.side_effect = [
            [
                {"id": 10, "price": 100.0, "quantity": 1.0, "time": 1_000},
                {"id": 11, "price": 101.0, "quantity": 1.0, "time": 2_000},
            ],
            [
                {"id": 12, "price": 99.0, "quantity": 1.0, "time": 3_000},
                {"id": 13, "price": 102.0, "quantity": 1.0, "time": 6_000},
            ],
        ]
        result = _load_agg_trade_window("MUUSDT", 1_000, 5_000)
        self.assertEqual(result["tradeCount"], 3)
        self.assertEqual(list(result["prices"]), [100.0, 101.0, 99.0])
        self.assertEqual(page_mock.call_count, 2)
        self.assertEqual(page_mock.call_args_list[1].args[0]["fromId"], 12)
        archive_mock.assert_called_once()

    @patch("grid_optimizer.time.sleep")
    @patch("grid_optimizer.requests.get")
    def test_public_request_retries_a_transient_failure(self, get_mock, sleep_mock):
        failed = Mock()
        failed.headers = {}
        failed.raise_for_status.side_effect = requests.HTTPError(response=failed)
        succeeded = Mock()
        succeeded.raise_for_status.return_value = None
        succeeded.json.return_value = {"serverTime": 123}
        get_mock.side_effect = [failed, succeeded]
        self.assertEqual(_get_json("/fapi/v1/time"), {"serverTime": 123})
        self.assertEqual(get_mock.call_count, 2)
        sleep_mock.assert_called_once()

    @patch("grid_optimizer.analyze_snapshot")
    @patch("grid_optimizer.get_market_snapshot")
    def test_complete_fine_path_analysis_is_reused(self, snapshot_mock, analyze_mock):
        base_snapshot = {
            "serverTime": 1_700_000_000_000,
            "finePath": {
                "status": "ready",
                "complete": True,
                "toMs": 1_700_000_000_000,
                "tradeCount": 123_456,
                "refreshFailed": False,
            },
            "cacheHit": False,
            "marketDataStale": False,
        }
        cached_snapshot = {
            **base_snapshot,
            "cacheHit": True,
            "finePath": dict(base_snapshot["finePath"]),
        }
        snapshot_mock.side_effect = [base_snapshot, cached_snapshot]
        analyze_mock.return_value = {
            "symbol": "MUUSDT",
            "dataCoverage": {"cacheHit": False, "marketDataStale": False},
        }

        first = optimize_grid("MUUSDT", 12_345, 2, 0.0001)
        second = optimize_grid("MUUSDT", 12_345, 2, 0.0001)

        self.assertEqual(analyze_mock.call_count, 1)
        self.assertFalse(first["dataCoverage"]["cacheHit"])
        self.assertTrue(second["dataCoverage"]["cacheHit"])

    def test_main_recommendation_uses_harvest_score_and_is_json_safe(self):
        start = 1_700_000_000_000
        candles = []
        for index in range(WEEK_MINUTES):
            close = 100 + 2 * math.sin(index / 20)
            candles.append(
                {
                    "openTime": start + index * MINUTE_MS,
                    "open": 100 + 2 * math.sin((index - 1) / 20),
                    "high": close + 0.1,
                    "low": close - 0.1,
                    "close": close,
                    "volume": 1.0,
                    "closeTime": start + (index + 1) * MINUTE_MS - 1,
                    "trades": 1,
                }
            )
        snapshot = {
            "candles": candles,
            "rules": {
                "symbol": "MUUSDT",
                "status": "TRADING",
                "baseAsset": "MU",
                "quoteAsset": "USDT",
                "tickSize": 0.01,
                "stepSize": 0.01,
                "minQty": 0.01,
                "minNotional": 5.0,
                "pricePrecision": 2,
                "quantityPrecision": 2,
                "maxLeverage": 10,
            },
            "mark": {"markPrice": 100.0, "indexPrice": 100.0, "time": 0},
            "book": {
                "bid": 99.99,
                "ask": 100.01,
                "mid": 100.0,
                "spread": 0.02,
                "spreadRate": 0.0002,
                "time": 0,
            },
            "serverTime": candles[-1]["closeTime"] + 1,
            "finePath": {
                "status": "loading",
                "complete": False,
                "progress": 0.25,
                "pages": 2,
                "tradeCount": 1_000,
                "refreshFailed": False,
                "prices": (),
                "times": (),
                "fromMs": None,
                "toMs": None,
            },
            "cacheHit": False,
            "marketDataStale": False,
        }
        result = analyze_snapshot(snapshot, "MUUSDT", 100_000, 1, 0.0001)
        self.assertEqual(result["selection"]["recommendationBasis"], "dailyHarvestScore")
        self.assertEqual(
            result["recommendation"]["dailyHarvestScore"],
            result["rankings"][0]["dailyHarvestScore"],
        )
        self.assertFalse(result["assumptions"]["fundingIncluded"])
        self.assertIn("steadyAlternative", result)
        self.assertEqual(
            [row["rangeRate"] for row in result["rangeAnalysis"]],
            list(grid_optimizer.RANGE_CANDIDATES),
        )
        json.dumps(result, allow_nan=False)


class GridRouteTests(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()

    def test_grid_page_is_available(self):
        response = self.client.get("/grid")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        for symbol in grid_optimizer.GRID_SYMBOL_CONFIGS:
            self.assertIn(f'value="{symbol}"', html)
        self.assertIn("币安永续网格参数助手", html)
        self.assertIn("30%", html)
        self.assertIn("历史日均网格利润估算", html)
        self.assertIn("收益优先建议", html)
        self.assertIn("较少出界的参考方案", html)
        self.assertIn("兼顾出界的比较分", html)
        self.assertIn("停止时平掉全部仓位", html)
        self.assertIn("Auto-Add Margin on Bracket Change", html)
        self.assertIn("实际可用倍数还会受币安名义金额档位限制", html)
        self.assertNotIn("考虑出界后", html)
        self.assertNotIn("按历史未出界比例折算", html)
        self.assertNotIn("按未出界比例折算", html)
        self.assertNotIn("refresh=1", html)
        response.close()

    def test_unsupported_symbol_is_rejected_without_network_call(self):
        response = self.client.get("/api/grid-optimize?symbol=SOLUSDT")
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.get_json()["ok"])
        response.close()

    @patch("server.optimize_grid")
    def test_crypto_symbols_accept_their_configured_leverage_limits(self, optimize_mock):
        optimize_mock.side_effect = lambda **kwargs: {"symbol": kwargs["symbol"]}
        for symbol, leverage in (("BTCUSDT", 125), ("ETHUSDT", 100)):
            with self.subTest(symbol=symbol):
                response = self.client.get(
                    f"/api/grid-optimize?symbol={symbol}&leverage={leverage}"
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.get_json()["data"]["symbol"], symbol)
                response.close()

    @patch("server.optimize_grid")
    def test_grid_api_passes_binance_manual_inputs(self, optimize_mock):
        optimize_mock.return_value = {"symbol": "MUUSDT"}
        response = self.client.get(
            "/api/grid-optimize?symbol=MUUSDT&investment=100000&leverage=3&feeBps=1&refresh=1"
        )
        self.assertEqual(response.status_code, 200)
        optimize_mock.assert_called_once_with(
            symbol="MUUSDT",
            investment=100000.0,
            leverage=3,
            fee_rate=0.0001,
            force_refresh=False,
        )
        response.close()

    def test_invalid_numbers_are_rejected_before_market_data(self):
        for query in (
            "investment=nan",
            "investment=inf",
            "investment=0",
            "leverage=0",
            "leverage=11",
            "leverage=1.5",
            "feeBps=-1",
            "feeBps=100",
        ):
            with self.subTest(query=query):
                response = self.client.get(f"/api/grid-optimize?symbol=MUUSDT&{query}")
                self.assertEqual(response.status_code, 400)
                self.assertFalse(response.get_json()["ok"])
                response.close()


if __name__ == "__main__":
    unittest.main()
