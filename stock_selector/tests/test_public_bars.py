from __future__ import annotations

import unittest
from pathlib import Path
import tempfile
import json

from collect_public_bars import output_name, parse_klines, request_with_retry, secid_for
from run_backtest import validate_replay_price_inputs


class PublicBarsTests(unittest.TestCase):
    def test_exchange_mapping_is_explicit(self):
        self.assertEqual(secid_for("sh.600000"), "1.600000")
        self.assertEqual(secid_for("sz.000001"), "0.000001")
        with self.assertRaises(ValueError):
            secid_for("600000")

    def test_rejects_mismatched_security_and_malformed_prices(self):
        payload = {"data": {"code": "600000", "klines": [
            "2025-06-23,12.98,13.31,13.36,12.88,100,1000,0,0,0,0"]}}
        rows = parse_klines(payload, "sh.600000", "2025-06-01", "2025-06-30")
        self.assertEqual(rows[0]["open"], 12.98)
        self.assertEqual(rows[0]["high"], 13.36)
        self.assertEqual(rows[0]["low"], 12.88)
        with self.assertRaises(ValueError):
            parse_klines(payload, "sz.000001", "2025-06-01", "2025-06-30")
        payload["data"]["klines"] = ["2025-06-23,12.98,13.31,12.00,12.88"]
        with self.assertRaises(ValueError):
            parse_klines(payload, "sh.600000", "2025-06-01", "2025-06-30")

    def test_missing_sessions_are_not_fabricated(self):
        payload = {"data": {"code": "600000", "klines": [
            "2025-06-23,12.98,13.31,13.36,12.88,100,1000,0,0,0,0",
            "2025-06-25,13.31,13.25,13.36,13.20,100,1000,0,0,0,0"]}}
        rows = parse_klines(payload, "sh.600000", "2025-06-01", "2025-06-30")
        self.assertEqual([row["date"] for row in rows], ["2025-06-23", "2025-06-25"])

    def test_incomplete_collection_uses_partial_file_names(self):
        self.assertEqual(output_name("bars", 358, 472), "bars.partial.csv")
        self.assertEqual(output_name("bars", 472, 472), "bars.csv")

    def test_transient_failure_has_bounded_exponential_retry(self):
        attempts = []
        pauses = []

        def request():
            attempts.append(1)
            if len(attempts) < 3:
                raise ConnectionError("temporary disconnect")
            return b"ok"

        self.assertEqual(request_with_retry(request, attempts=3,
                                            sleep=pauses.append, base_delay=0.5), b"ok")
        self.assertEqual(len(attempts), 3)
        self.assertEqual(pauses, [0.5, 1.0])

    def test_permanent_failure_stops_after_configured_attempts(self):
        attempts = []

        def request():
            attempts.append(1)
            raise ConnectionError("closed")

        with self.assertRaisesRegex(ConnectionError, "closed"):
            request_with_retry(request, attempts=2, sleep=lambda _: None)
        self.assertEqual(len(attempts), 2)

    def test_partial_public_bars_cannot_be_used_for_replay(self):
        with self.assertRaisesRegex(ValueError, "partial"):
            validate_replay_price_inputs(Path("bars.partial.csv"), None, {})
        with self.assertRaisesRegex(ValueError, "partial"):
            validate_replay_price_inputs(Path("bars.csv"), Path("benchmark_raw.partial.csv"), {})
        with tempfile.TemporaryDirectory() as folder:
            audit = Path(folder) / "audit.json"
            audit.write_text(json.dumps({"collection_complete": False,
                                         "complete_tickers": 465, "requested_tickers": 472}),
                             encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "partial"):
                validate_replay_price_inputs(Path("bars.csv"), Path("benchmark.csv"), {}, audit)
