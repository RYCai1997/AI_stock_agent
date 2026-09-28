from __future__ import annotations

import unittest

from backtest.metrics import performance


class MetricsTests(unittest.TestCase):
    def test_metrics_use_continuous_equity_and_costs(self):
        daily = [{"date": "2025-01-02", "total_equity": 100000,
                  "actual_exposure": 0, "realized_pnl": 0, "unrealized_pnl": 0, "fees": 0},
                 {"date": "2025-01-03", "total_equity": 99000,
                  "actual_exposure": .3, "realized_pnl": 0, "unrealized_pnl": -1000, "fees": 10},
                 {"date": "2025-01-06", "total_equity": 101000,
                  "actual_exposure": 0, "realized_pnl": 1000, "unrealized_pnl": 0, "fees": 20}]
        result = performance(daily, [])
        self.assertAlmostEqual(result["cumulative_return"], .01)
        self.assertAlmostEqual(result["maximum_drawdown"], -.01)
        self.assertEqual(result["total_transaction_costs"], 20)
        self.assertAlmostEqual(result["average_exposure"], .1)
