import unittest
from unittest.mock import patch

from grid_optimizer import (
    _classify_er,
    _profit_rate_range,
    _quantity_and_capital,
    build_arithmetic_grid,
    count_grid_roundtrips,
    efficiency_ratio,
    rolling_out_of_range_risk,
)
from server import app


class GridMathTests(unittest.TestCase):
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


class GridRouteTests(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()

    def test_grid_page_is_available(self):
        response = self.client.get("/grid")
        self.assertEqual(response.status_code, 200)
        self.assertIn("MUUSDT", response.get_data(as_text=True))
        response.close()

    def test_unsupported_symbol_is_rejected_without_network_call(self):
        response = self.client.get("/api/grid-optimize?symbol=BTCUSDT")
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.get_json()["ok"])
        response.close()

    @patch("server.optimize_grid")
    def test_grid_api_passes_binance_manual_inputs(self, optimize_mock):
        optimize_mock.return_value = {"symbol": "MUUSDT"}
        response = self.client.get(
            "/api/grid-optimize?symbol=MUUSDT&investment=100000&leverage=3&feeBps=1"
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


if __name__ == "__main__":
    unittest.main()
