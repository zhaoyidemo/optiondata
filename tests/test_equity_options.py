import unittest
from unittest.mock import patch

import equity_options as eo
from server import app


class EquityOptionsTests(unittest.TestCase):
    def setUp(self):
        eo._cache.clear()

    def test_catalog_uses_options_flag_not_overnight_and_keeps_etfs(self):
        raw = [{"s": "MSTR", "ac": "EQ_MSTR", "t": "STOCK", "opt": True},
               {"s": "SPY", "ac": "EQ_SPY", "t": "ETF", "opt": True},
               {"s": "NO", "ac": "EQ_NO", "t": "STOCK", "os": True}]
        with patch.object(eo, "_read", return_value=raw) as read:
            self.assertEqual([x["symbol"] for x in eo.catalog()["symbols"]], ["MSTR", "SPY"])
            eo.catalog()
            self.assertEqual(read.call_count, 1)

    def test_changed_catalog_schema_fails_instead_of_empty_success(self):
        with patch.object(eo, "_read", return_value=[{"s": "MSTR", "os": True}]):
            with self.assertRaises(eo.EquityDataError):
                eo.catalog()

    def test_chain_batches_all_contracts_and_preserves_missing_quote(self):
        contracts = [{"os": f"MSTR{i}", "sp": "125", "t": "PUT"} for i in range(204)]
        batches = []

        def read(path, params=None, body=None):
            if "chain-symbols" in path:
                return {"l": contracts}
            batch = body["occSymbols"]
            batches.append(len(batch))
            return [{"os": s, "m": "100", "e": 1794603600000, "bp": "4.9",
                     "ap": "5.45", "bs": "2"} for s in batch if s != "MSTR0"]

        overview = {"price": 130, "fetchedAt": 1, "expirations": [
            {"expirationDate": "2026-11-13", "rootSymbol": "MSTR"}]}
        with patch.object(eo, "overview", return_value=overview), patch.object(eo, "_read", side_effect=read), patch.object(eo, "market_calendar", return_value={"phase": "UNKNOWN"}):
            result = eo.chain("MSTR", "2026-11-13", "MSTR")
        self.assertEqual(sorted(batches), [4, 100, 100])
        self.assertEqual(len(result["rows"]), 204)
        self.assertEqual(result["missingCount"], 1)
        self.assertIsNone(result["rows"][0]["bid"])
        self.assertIsNone(result["rows"][0]["multiplier"])
        self.assertEqual(result["rows"][1]["bid"], 4.9)
        self.assertFalse(result["quoteTimestampAvailable"])

    def test_failure_in_any_batch_does_not_publish_partial_cache(self):
        overview = {"price": 130, "fetchedAt": 1, "expirations": [
            {"expirationDate": "2026-11-13", "rootSymbol": "MSTR"}]}
        with patch.object(eo, "overview", return_value=overview), patch.object(eo, "_read", side_effect=eo.EquityDataError("failed")):
            with self.assertRaises(eo.EquityDataError):
                eo.chain("MSTR", "2026-11-13", "MSTR")
        self.assertNotIn(("chain", "MSTR", "2026-11-13", "MSTR"), eo._cache)

    def test_arbitrary_expiry_rejected_without_chain_request(self):
        with patch.object(eo, "overview", return_value={"expirations": []}), patch.object(eo, "_read") as read:
            with self.assertRaises(ValueError):
                eo.chain("MSTR", "garbage", "MSTR")
            read.assert_not_called()

    def test_route_errors_are_safe_and_correct_status(self):
        client = app.test_client()
        with patch.object(eo, "overview", side_effect=ValueError("请选择股票")):
            self.assertEqual(client.get('/api/stock-options/overview?symbol=X').status_code, 400)
        with patch.object(eo, "catalog", side_effect=eo.EquityDataError("读取失败")):
            response = client.get('/api/stock-options/catalog')
            self.assertEqual(response.status_code, 502)
            self.assertNotIn("trace", response.json)
        with client.get('/stock-options') as response:
            self.assertEqual(response.status_code, 200)
        self.assertEqual(client.get('/api/stock-options/order').status_code, 404)


if __name__ == '__main__':
    unittest.main()
