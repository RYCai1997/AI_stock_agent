from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from data_public.archive import PublicSourceArchive, SourceResult
from prospective.calendar import signal_day_decision
from run_prospective import build_from_public_snapshot, make_baostock_recorder
from test_selector import sample_frame
from verify_prediction import verify_prediction


class RunProspectiveTests(unittest.TestCase):
    def test_structural_eps_gap_uses_frozen_quality_mask_but_suspicious_gap_blocks(self):
        from run_prospective import assess_snapshot
        from selector.scoring import score_frame
        from selector.strategy import OFFICIAL_STRATEGY
        frame = sample_frame(300)
        frame["membership_update_date"] = "2026-09-01"
        frame["fundamental_as_of"] = "2026-08-01"
        frame["price_as_of"] = "2026-09-30"
        frame["tradestatus"] = "1"
        frame["is_st"] = "0"
        frame["factor_data_status"] = "complete"
        frame.loc[0, "eps_growth_std"] = float("nan")
        frame.loc[0, "mom_12_1"] = float("nan")
        frame.loc[0, "factor_data_status"] = "structurally_incomplete"
        scored = score_frame(frame, OFFICIAL_STRATEGY.selector_config())
        self.assertFalse(bool(scored.loc[0, "quality_complete"]))
        self.assertFalse(bool(scored.loc[0, "quality_pass"]))
        provider = {"original_members": 300, "requested_members": 300, "errors": {}}
        quality = assess_snapshot(frame, scored, provider, "2026-09-30", [])
        self.assertTrue(quality["primary_eligible"])
        frame.loc[0, "factor_data_status"] = "suspicious_missing"
        quality = assess_snapshot(frame, scored, provider, "2026-09-30", [])
        self.assertFalse(quality["primary_eligible"])
        self.assertIn("qvm_factors", quality["missing_core_fields"])
        provider["requested_members"] = 299
        quality = assess_snapshot(frame, scored, provider, "2026-09-30", [])
        self.assertIn("historical_csi300_membership", quality["missing_core_fields"])

    def test_unconfigured_official_parser_is_not_reported_as_endpoint_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            entries = []
            recorder = make_baostock_recorder(PublicSourceArchive(Path(folder)), entries)
            recorder("query_hs300_stocks", {"date": "2026-09-30"},
                     ["code"], [{"code": "sh.600000"}])
            self.assertIsNone(entries[0]["fallback_reason"])
            self.assertEqual(entries[0]["source_selection_reason"], "official_parser_not_configured")
            self.assertEqual(entries[0]["attempts"][0]["status"], "not_attempted")
            self.assertEqual(entries[0]["attempts"][1]["status"], "used")

    def test_verified_legacy_cache_correction_does_not_claim_official_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            archive = PublicSourceArchive(Path(folder))
            entry = archive.capture(SourceResult("baostock", "query_profit_data",
                                                 {"code": "sh.600000"}, b"raw", b"normalized", "1"),
                                    requested_source="cninfo",
                                    fallback_reason="official_parser_not_configured",
                                    attempts=[{"source": "cninfo", "status": "not_attempted",
                                               "reason": "official_parser_not_configured"},
                                              {"source": "baostock", "status": "used"}])
            entries = []
            recorder = make_baostock_recorder(archive, entries)
            self.assertTrue(recorder.restore([entry]))
            self.assertIsNone(entries[0]["fallback_reason"])
            self.assertEqual(entries[0]["source_selection_reason"],
                             "official_parser_not_configured")
            self.assertTrue(entries[0]["legacy_metadata_corrected"])
            self.assertTrue(archive.verify(entries[0]))
            (archive.root / entry["raw_path"]).write_bytes(b"tampered")
            fresh_entries = []
            self.assertFalse(make_baostock_recorder(archive, fresh_entries).restore([entry]))
            self.assertEqual(fresh_entries, [])

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
