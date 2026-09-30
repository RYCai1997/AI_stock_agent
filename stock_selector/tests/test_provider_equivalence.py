from __future__ import annotations

import unittest

from data_history.provider_equivalence import compare_node
from test_selector import sample_frame


class ProviderEquivalenceTests(unittest.TestCase):
    def test_identical_snapshot_has_full_overlap_and_no_numeric_drift(self):
        date = "2025-07-15"
        metrics = sample_frame(100)
        metrics["return_20d"] = 0.03
        metadata = {"market_trend": "up", "membership_snapshot": date}
        row, detail = compare_node(date, (metrics, metadata), (metrics.copy(), metadata))
        self.assertEqual(row["member_overlap_count"], 100)
        self.assertEqual(row["price_max_abs_diff"], 0)
        self.assertEqual(row["quality_pass_agreement"], 1)
        self.assertEqual(row["top5_overlap_count"], 5)
        self.assertFalse(row["semantic_equivalence_verified"])
        self.assertGreater(len(detail), 0)

    def test_future_membership_is_rejected(self):
        date = "2025-07-15"
        metrics = sample_frame(100)
        with self.assertRaisesRegex(ValueError, "future"):
            compare_node(date, (metrics, {"market_trend": "up"}),
                         (metrics, {"market_trend": "up", "membership_snapshot": "2025-07-31"}))
