from __future__ import annotations

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class HistoricalSchemaTests(unittest.TestCase):
    def test_required_endpoint_and_pit_schema_cover_replay_inputs(self):
        endpoints = json.loads((ROOT / "REQUIRED_TUSHARE_ENDPOINTS.json").read_text(encoding="utf-8"))
        schema = json.loads((ROOT / "stock_selector/data_history/schema/historical_pit_schema.json")
                            .read_text(encoding="utf-8"))
        names = {item["endpoint"] for item in endpoints["requirements"]}
        self.assertTrue({"daily", "adj_factor", "index_weight", "income", "cashflow",
                         "fina_indicator", "suspend_d", "stk_limit", "dividend",
                         "stock_basic", "namechange"} <= names)
        self.assertEqual(schema["data_provider_version"], "tushare_candidate_v1")
        self.assertLess(schema["price_warmup_start"], schema["account_period"][0])
        self.assertLess(schema["fundamental_warmup_start"], schema["price_warmup_start"])
        for table in ("unadjusted_daily", "adjusted_daily", "fundamental_filings",
                      "membership", "industry", "security_state", "corporate_actions"):
            self.assertIn("source_request_sha256", schema["tables"][table]["required"])
