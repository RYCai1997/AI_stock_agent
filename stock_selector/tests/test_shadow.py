from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from selector.shadow import append_next_open_observation, write_shadow_record


class ShadowTests(unittest.TestCase):
    def test_signal_log_is_immutable_and_observation_is_separate(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "raw_metrics.csv").write_text("ticker\nA\n", encoding="utf-8")
            (root / "candidates.csv").write_text("ticker\nA\n", encoding="utf-8")
            plan = pd.DataFrame([{"ticker": "A", "execution_price": 10,
                                  "target_fraction": .06}])
            record_path = write_shadow_record(
                as_of="2025-01-02", output=root,
                metadata={"git": {"commit": "abc", "tracked_worktree_dirty": False},
                          "data_provider": {"name": "Baostock"}},
                plan=plan, actionable=pd.DataFrame({"ticker": ["A"]}),
                account={"cash": 10000}, shadow_dir=root / "shadow")
            before = record_path.read_bytes()
            reconciliation = append_next_open_observation(record_path, {
                "A": {"date": "2025-01-03", "open": 10.5,
                      "limit_up": 11, "tradable": True}}, source="manual quote")
            self.assertEqual(record_path.read_bytes(), before)
            record = json.loads(before)
            self.assertEqual(record["strategy_version"], "1.0.0")
            self.assertEqual(len(record["data_hash"]["raw_metrics_sha256"]), 64)
            observed = json.loads(reconciliation.read_text(encoding="utf-8"))
            self.assertEqual(observed["observations"][0]["execution_feasibility"],
                             "feasible_under_opening_rule")
            self.assertAlmostEqual(observed["observations"][0]["execution_difference"], .05)
            self.assertEqual(observed["orders_placed"], 0)
