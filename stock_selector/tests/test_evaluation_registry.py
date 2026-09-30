from __future__ import annotations

import csv
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from data_public.archive import sha256
from evaluation.performance import build_performance
from evaluation.registry import read_registry, register_evaluation
from prospective.package import REQUIRED_INPUT_FILES, create_prediction_package


class EvaluationRegistryTests(unittest.TestCase):
    def test_outcome_is_append_only_and_performance_is_insufficient_sample(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            prediction = create_prediction_package(
                prospective_root=root / "prospective", retrospective_root=root / "retrospective",
                signal_date="2026-10-15",
                generated_at=datetime(2026, 10, 15, 15, 1, tzinfo=ZoneInfo("Asia/Shanghai")),
                evidence_label="prospective",
                files={name: b"x\n" for name in REQUIRED_INPUT_FILES},
                source_manifest={"data_provider_version": "PUBLIC_V1",
                                 "sources": [{"raw_sha256": sha256(b"a"),
                                              "normalized_sha256": sha256(b"b")}]},
                signal={"scheduled_signal_date": "2026-10-15", "market_close_verified": True},
                quality={"primary_eligible": True},
                provenance={"git_commit": "a" * 40, "dirty": False})
            record = json.loads((prediction / "prediction_record.json").read_text())
            evaluation = root / "evaluation.json"
            evaluation.write_text(json.dumps({
                "evidence_label": "prospective_outcome", "prediction_id": record["prediction_id"],
                "prediction_seal_sha256": sha256((prediction / "prediction_seal.json").read_bytes()),
                "signal_date": "2026-10-15", "horizon_sessions": 20,
                "horizon_end_date": "2026-11-12", "evaluated_at_utc": "2026-11-12T08:00:00+00:00"}))
            registry = root / "prospective" / "evaluation_registry.csv"
            register_evaluation(prediction, evaluation, registry)
            self.assertEqual(len(read_registry(registry)), 1)
            with self.assertRaisesRegex(ValueError, "already registered"):
                register_evaluation(prediction, evaluation, registry)
            output = root / "prospective" / "prospective_performance.csv"
            row = build_performance(root / "prospective" / "prospective_registry.csv",
                                    registry, root / "prospective" / "shadow", output)
            self.assertEqual(row["status"], "insufficient_sample")
            self.assertEqual(row["evaluated_20d"], 1)
            self.assertEqual(row["cumulative_shadow_nav"], "")
            with output.open(newline="", encoding="utf-8") as handle:
                self.assertEqual(len(list(csv.DictReader(handle))), 1)
            text = registry.read_text(encoding="utf-8").replace("GENESIS", "BROKEN")
            registry.write_text(text, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "hash chain"):
                read_registry(registry)
