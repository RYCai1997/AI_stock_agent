from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from data_public.archive import PublicSourceArchive, SourceResult
from prospective.calendar import signal_day_decision
from run_prospective import build_from_public_snapshot
from test_selector import sample_frame
from verify_prediction import verify_prediction


class RunProspectiveTests(unittest.TestCase):
    def test_public_snapshot_creates_sealed_primary_without_tushare(self):
        date = "2026-10-15"
        now = datetime(2026, 10, 15, 15, 1, tzinfo=ZoneInfo("Asia/Shanghai"))
        sessions = ["2026-10-14", date, "2026-10-16", "2026-10-19"]
        metrics = sample_frame(300)
        metrics["universe_as_of"] = "2026-10-01"
        metrics["membership_update_date"] = "2026-10-01"
        metrics["fundamental_as_of"] = "2026-09-30"
        metrics["financial_period"] = "2026-06-30"
        metrics["price_as_of"] = date
        metrics["tradestatus"] = "1"
        metrics["is_st"] = "0"
        provider = {"as_of": date, "built_rows": 300, "original_members": 300,
                    "requested_members": 300, "errors": {}, "market_trend": "up",
                    "benchmark": {"price_as_of": date, "return_20d": 0.03}}
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            archive = PublicSourceArchive(root / "public_raw")
            entry = archive.capture(SourceResult("baostock", "test", {}, b"raw", b"normalized", "1"),
                                    requested_source="baostock", fallback_reason=None,
                                    attempts=[{"source": "baostock", "status": "used"}])
            output = build_from_public_snapshot(
                signal_date=date, generated_at=now,
                decision=signal_day_decision(now, sessions), sessions=sessions,
                metrics=metrics, provider=provider, archive=archive, entries=[entry],
                output_root=root, provenance={"git_commit": "a" * 40,
                                              "working_tree_status": "", "dirty": False})
            record = json.loads((output / "prediction_record.json").read_text())
            self.assertEqual(record["data_provider_version"], "PUBLIC_V1")
            self.assertTrue(record["prospective_primary"])
            self.assertTrue(verify_prediction(output, root / "prospective" / "prospective_registry.csv"))

    def test_missing_core_industry_fails_closed(self):
        date = "2026-10-15"
        metrics = sample_frame(300)
        metrics["industry_l2"] = "\ufffd"
        from run_prospective import assess_snapshot
        from selector.pipeline import run_selection
        from selector.strategy import OFFICIAL_STRATEGY
        with tempfile.TemporaryDirectory() as folder:
            scored, _ = run_selection(metrics, "A", "2026-10-15", Path(folder), "up",
                                      OFFICIAL_STRATEGY.selector_config())
        provider = {"original_members": 300, "requested_members": 300, "errors": {}}
        quality = assess_snapshot(metrics, scored, provider, date, [])
        self.assertFalse(quality["primary_eligible"])
        self.assertIn("historical_industry", quality["missing_core_fields"])
