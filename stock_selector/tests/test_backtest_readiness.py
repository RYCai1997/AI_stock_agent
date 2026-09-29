from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from audit_backtest_readiness import audit_snapshots


class BacktestReadinessTests(unittest.TestCase):
    def test_missing_month_and_incomplete_node_are_reported(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for date in ("2025-01-15", "2025-03-15"):
                node = root / date
                node.mkdir()
                pd.DataFrame([{"ticker": "A"}]).to_csv(node / "raw_metrics.csv", index=False)
                (node / "official_run_metadata.json").write_text(json.dumps({
                    "provider": {"as_of": date, "built_rows": 1,
                                 "requested_members": 1, "errors": {}}
                }), encoding="utf-8")
            (root / "2025-04-15").mkdir()
            report = audit_snapshots(root)
            self.assertEqual(report["complete_snapshots"], 2)
            self.assertEqual(report["missing_signal_months"], ["2025-02"])
            self.assertEqual(report["expected_signal_months_between_first_and_last"], 3)
            self.assertFalse(report["monthly_signal_coverage_complete"])
            self.assertIn("2025-04-15", report["snapshot_issues"][0])

