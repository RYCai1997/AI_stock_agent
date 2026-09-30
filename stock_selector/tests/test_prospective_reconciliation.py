from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from data_public.archive import sha256
from prospective.package import REQUIRED_INPUT_FILES, create_prediction_package
from prospective.reconciliation import reconcile_execution
from verify_prediction import verify_prediction


class ProspectiveReconciliationTests(unittest.TestCase):
    def test_next_open_is_separate_and_limit_up_blocks_fill(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            files = {name: b"x\n" for name in REQUIRED_INPUT_FILES}
            prediction = create_prediction_package(
                prospective_root=root / "prospective", retrospective_root=root / "retrospective",
                signal_date="2026-10-15",
                generated_at=datetime(2026, 10, 15, 15, 1, tzinfo=ZoneInfo("Asia/Shanghai")),
                evidence_label="prospective", files=files,
                source_manifest={"data_provider_version": "PUBLIC_V1",
                                 "sources": [{"raw_sha256": sha256(b"x"),
                                              "normalized_sha256": sha256(b"x")}]},
                signal={"scheduled_signal_date": "2026-10-15", "market_close_verified": True,
                        "intended_execution_date": [{"ticker": "T0", "date": "2026-10-16"}]},
                quality={"primary_eligible": True},
                provenance={"git_commit": "a" * 40, "dirty": False,
                            "working_tree_status": ""})
            original = (prediction / "prediction_record.json").read_bytes()
            output = reconcile_execution(
                prediction_dir=prediction,
                observations={"T0": {"date": "2026-10-16", "open": 11,
                                     "tradable": True, "limit_up": 11, "limit_down": 9}},
                source_manifest={"source": "public_fixture", "raw_sha256": sha256(b"bar")},
                evaluated_at=datetime(2026, 10, 16, 16, tzinfo=ZoneInfo("Asia/Shanghai")),
                slippage=.001, output_dir=root / "reconciliation")
            row = json.loads(output.read_text())["observations"][0]
            self.assertFalse(row["simulated_fill"])
            self.assertEqual(row["fill_block_reason"], "limit_up_buy_block")
            self.assertEqual((prediction / "prediction_record.json").read_bytes(), original)
            self.assertTrue(verify_prediction(prediction))
