from __future__ import annotations

import unittest

from backtest.diagnostics import mae_mfe
from backtest.engine import DailyBar


class MaeMfeTests(unittest.TestCase):
    def test_stop_path_and_counterfactual_horizon(self):
        bars = [DailyBar("2025-01-02", "A", 10, 11, 9.5, 10),
                DailyBar("2025-01-03", "A", 9, 9.2, 8.8, 9),
                DailyBar("2025-01-06", "A", 8.5, 9.1, 8, 8.5),
                DailyBar("2025-01-07", "A", 8.4, 8.7, 7.5, 8)]
        trades = [
            {"ticker": "A", "side": "buy", "actual_execution_date": "2025-01-02",
             "execution_price": 10},
            {"ticker": "A", "side": "sell", "actual_execution_date": "2025-01-03",
             "execution_price": 9, "reason": "stop loss"},
        ]
        row = mae_mfe(trades, bars, post_stop_horizon=2).iloc[0]
        self.assertAlmostEqual(row["mae"], -.12)
        self.assertAlmostEqual(row["mfe"], .1)
        self.assertAlmostEqual(row["post_stop_max_rebound"], 9.1 / 9 - 1)
        self.assertAlmostEqual(row["no_stop_horizon_return"], -.2)
        self.assertEqual(row["stop_saved_or_hurt"], "saved")

    def test_short_future_path_is_censored(self):
        bars = [DailyBar("2025-01-02", "A", 10, 10, 10, 10),
                DailyBar("2025-01-03", "A", 9, 9, 9, 9)]
        trades = [{"ticker": "A", "side": "buy", "actual_execution_date": "2025-01-02",
                   "execution_price": 10},
                  {"ticker": "A", "side": "sell", "actual_execution_date": "2025-01-03",
                   "execution_price": 9, "reason": "stop loss"}]
        row = mae_mfe(trades, bars).iloc[0]
        self.assertTrue(row["censored"])
        self.assertEqual(row["stop_saved_or_hurt"], "unresolved_censored")
