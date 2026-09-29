from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from data_history.store import disclosures_available, ingest_public_price_cache


class PitStoreTests(unittest.TestCase):
    def test_future_financial_publication_cannot_enter_snapshot(self):
        records = [{"statDate": "2024-12-31", "pubDate": "2025-04-20", "roeAvg": "0.10"},
                   {"statDate": "2024-09-30", "pubDate": "2024-10-25", "roeAvg": "0.08"}]
        self.assertEqual(disclosures_available(records, "2025-04-15"), records[1:])

    def test_price_ingest_preserves_source_and_fails_closed_on_missing_adjusted_data(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            responses = root / "responses"
            responses.mkdir()
            raw = json.dumps({"data": {"code": "600000", "klines": [
                "2025-06-23,12.98,13.31,13.36,12.88,100,1000,0,0,0,0"]}}).encode()
            (responses / "sh.600000_2025-06-01_2025-06-30.json").write_bytes(raw)
            audit = {"source": "public", "fqt": 0, "price_basis": "unadjusted",
                     "requested_start": "2025-06-01", "requested_end": "2025-06-30",
                     "requested_tickers": 1, "series": [{"ticker": "sh.600000", "status": "ok",
                      "sha256": hashlib.sha256(raw).hexdigest(), "rows": 1,
                      "first_date": "2025-06-23", "last_date": "2025-06-23"}]}
            source_audit = root / "source_audit.json"
            source_audit.write_text(json.dumps(audit), encoding="utf-8")
            report = ingest_public_price_cache(source_audit, responses, root / "store" / "prices" / "unadjusted")
            self.assertTrue(report["unadjusted_complete"])
            self.assertFalse(report["usable_for_full_v1"])
            self.assertFalse(report["adjusted_indicator_prices_complete"])
            contents = (root / "store" / "prices" / "unadjusted" / "sh.600000.csv").read_text()
            self.assertIn("Eastmoney hands", contents)
            self.assertIn("100.0,1000.0", contents)
