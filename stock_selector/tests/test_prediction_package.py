from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from data_public.archive import sha256
from prospective.package import REQUIRED_INPUT_FILES, create_prediction_package
from verify_prediction import verify_prediction


class PredictionPackageTests(unittest.TestCase):
    def _kwargs(self, root):
        files = {name: (name + "\n").encode() for name in REQUIRED_INPUT_FILES}
        files["raw/source.bin"] = b"raw"
        return dict(prospective_root=root / "prospective", retrospective_root=root / "retrospective",
                    signal_date="2026-10-15", generated_at=datetime(2026, 10, 15, 15, 1,
                                                                     tzinfo=ZoneInfo("Asia/Shanghai")),
                    evidence_label="prospective", files=files,
                    source_manifest={"data_provider_version": "PUBLIC_V1",
                                     "sources": [{"raw_sha256": sha256(b"raw"),
                                                  "normalized_sha256": sha256(b"normalized")}]},
                    signal={"scheduled_signal_date": "2026-10-15", "market_close_verified": True,
                            "selected_tickers": ["sh.600000"], "target_weights": [0.06]},
                    quality={"primary_eligible": True},
                    provenance={"git_commit": "a" * 40, "dirty": False,
                                "working_tree_status": ""})

    def test_primary_is_sealed_and_second_run_is_nonprimary_rerun(self):
        with tempfile.TemporaryDirectory() as folder:
            args = self._kwargs(Path(folder))
            primary = create_prediction_package(**args)
            self.assertTrue(verify_prediction(primary, args["prospective_root"] / "prospective_registry.csv"))
            original = (primary / "prediction_record.json").read_bytes()
            rerun = create_prediction_package(**args)
            self.assertIn("retrospective", str(rerun))
            self.assertTrue(rerun.name.startswith("rerun_"))
            self.assertEqual((primary / "prediction_record.json").read_bytes(), original)
            self.assertFalse(json.loads((rerun / "prediction_record.json").read_text())["prospective_primary"])
            self.assertEqual(len((args["prospective_root"] / "prospective_registry.csv").read_text().splitlines()), 2)
            (primary / "selected.csv").write_bytes(b"tampered")
            self.assertFalse(verify_prediction(primary, args["prospective_root"] / "prospective_registry.csv"))

    def test_dirty_git_and_past_generation_are_blocked_or_degraded(self):
        with tempfile.TemporaryDirectory() as folder:
            args = self._kwargs(Path(folder))
            args["provenance"]["dirty"] = True
            with self.assertRaisesRegex(ValueError, "clean git"):
                create_prediction_package(**args)
            path = create_prediction_package(**args, allow_dirty=True)
            record = json.loads((path / "prediction_record.json").read_text())
            self.assertEqual(record["research_quality"], "degraded")
            self.assertEqual(record["evidence_label"], "research_only")
            self.assertFalse(record["prospective_primary"])
            self.assertFalse((args["prospective_root"] / "prospective_registry.csv").exists())
        with tempfile.TemporaryDirectory() as folder:
            args = self._kwargs(Path(folder))
            args["generated_at"] = datetime(2027, 1, 20, 16, tzinfo=ZoneInfo("Asia/Shanghai"))
            with self.assertRaisesRegex(ValueError, "cannot be prospective"):
                create_prediction_package(**args)

    def test_research_date_reruns_never_append_primary_registry(self):
        with tempfile.TemporaryDirectory() as folder:
            args = self._kwargs(Path(folder))
            args["evidence_label"] = "retrospective_reconstruction"
            args["generated_at"] = datetime(2026, 10, 1, 15, 1,
                                            tzinfo=ZoneInfo("Asia/Shanghai"))
            first = create_prediction_package(**args)
            original = (first / "prediction_record.json").read_bytes()
            second = create_prediction_package(**args)
            self.assertNotEqual(first, second)
            self.assertEqual((first / "prediction_record.json").read_bytes(), original)
            self.assertTrue(verify_prediction(first))
            self.assertTrue(verify_prediction(second))
            self.assertFalse(json.loads((first / "prediction_record.json").read_text())
                             ["prospective_primary"])
            self.assertFalse((args["prospective_root"] / "prospective_registry.csv").exists())
