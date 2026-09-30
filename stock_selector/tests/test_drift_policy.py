from __future__ import annotations

import unittest
from pathlib import Path

import pandas as pd

from data_history.drift_policy import evaluate_equivalence


POLICY = Path(__file__).resolve().parents[2] / "DATA_PROVIDER_DRIFT_POLICY.json"


class DriftPolicyTests(unittest.TestCase):
    def test_missing_candidates_block_equivalence(self):
        rows = pd.DataFrame([{"signal_date": "2020-03-16",
                              "status": "blocked_by_missing_data_credentials"}])
        result = evaluate_equivalence(rows, POLICY)
        self.assertFalse(result["equivalent"])
        self.assertEqual(result["status"], "blocked_by_missing_data_credentials")

    def test_numeric_identity_does_not_bypass_semantic_gates(self):
        from data_history.provider_equivalence import REFERENCE_DATES
        from json import loads
        policy = loads(POLICY.read_text(encoding="utf-8"))
        rows = []
        for date in REFERENCE_DATES:
            row = {"signal_date": date, "status": "numeric_comparison_only",
                   "reference_member_count": 300, "candidate_member_count": 300,
                   "member_overlap_count": 300, "candidate_membership_not_future": True,
                   "top5_overlap_count": 5}
            for field in policy["maximum_absolute_difference"]:
                row[f"{field}_max_abs_diff"] = 0
                row[f"{field}_paired_count"] = 300
            for field in policy["minimum_spearman"]:
                row[f"{field}_spearman"] = 1
            for field in policy["minimum_pass_agreement"]:
                row[f"{field}_agreement"] = 1
            rows.append(row)
        result = evaluate_equivalence(pd.DataFrame(rows), POLICY)
        self.assertFalse(result["equivalent"])
        self.assertIn("unverified semantic gate", " ".join(result["issues"]))
        gates = {name: True for name in policy["mandatory_non_numeric_gates"]}
        self.assertTrue(evaluate_equivalence(pd.DataFrame(rows), POLICY, gates)["equivalent"])
