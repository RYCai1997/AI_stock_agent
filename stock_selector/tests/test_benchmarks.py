from __future__ import annotations

import unittest

import pandas as pd

from backtest.benchmarks import benchmark_comparison


class BenchmarkTests(unittest.TestCase):
    def test_prior_exposure_prevents_same_day_lookahead(self):
        daily = [
            {"date": "2025-01-02", "daily_nav": 1.0, "actual_exposure": 0.0},
            {"date": "2025-01-03", "daily_nav": 1.0, "actual_exposure": .24},
            {"date": "2025-01-06", "daily_nav": 1.01, "actual_exposure": .24},
        ]
        index = pd.DataFrame([{"date": "2025-01-02", "close": 100},
                              {"date": "2025-01-03", "close": 110},
                              {"date": "2025-01-06", "close": 121}])
        table, metrics = benchmark_comparison(daily, index)
        self.assertAlmostEqual(table.iloc[1]["exposure_matched_csi300_nav"], 1.0)
        self.assertAlmostEqual(table.iloc[2]["exposure_matched_csi300_nav"], 1.024)
        self.assertAlmostEqual(metrics["full_csi300_cumulative_return"], .21)
        self.assertAlmostEqual(metrics["matched_csi300_cumulative_return"], .024)

    def test_date_mismatch_fails_instead_of_silent_alignment(self):
        daily = [{"date": "2025-01-02", "daily_nav": 1, "actual_exposure": 0},
                 {"date": "2025-01-03", "daily_nav": 1, "actual_exposure": 0}]
        index = pd.DataFrame([{"date": "2025-01-02", "close": 100},
                              {"date": "2025-01-06", "close": 100}])
        with self.assertRaisesRegex(ValueError, "exactly match"):
            benchmark_comparison(daily, index)
