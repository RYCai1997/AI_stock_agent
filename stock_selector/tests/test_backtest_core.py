from __future__ import annotations

import unittest

from backtest.account import Account
from backtest.engine import BacktestEngine, DailyBar, Signal


class BacktestCoreTests(unittest.TestCase):
    def test_realized_pnl_is_not_double_counted(self):
        account = Account(100000)
        account.buy("sh.600000", 100, 10)
        account.sell("sh.600000", 100, 11)
        state = account.mark({})
        self.assertEqual(state["total_equity"], 100100)
        self.assertEqual(state["realized_pnl"], 100)
        self.assertEqual(state["daily_nav"], 1.001)

    def test_signal_executes_at_next_open_and_identity_holds_daily(self):
        bars = [DailyBar("2025-01-02", "A", 10, 10, 10, 10),
                DailyBar("2025-01-03", "A", 12, 13, 11, 13),
                DailyBar("2025-01-06", "A", 14, 14, 13, 14)]
        result = BacktestEngine(100000).run(bars, [Signal("2025-01-02", "A", "buy", .06)])
        self.assertEqual(result.trades[0]["signal_date"], "2025-01-02")
        self.assertEqual(result.trades[0]["actual_execution_date"], "2025-01-03")
        self.assertEqual(result.trades[0]["execution_price"], 12)
        self.assertEqual(result.trades[0]["signal_price"], 10)
        for day in result.daily_nav:
            self.assertAlmostEqual(day["cash"] + day["market_value"], day["total_equity"])

    def test_missing_future_price_cannot_be_used_for_signal(self):
        bars = [DailyBar("2025-01-02", "A", 10, 10, 10, 10),
                DailyBar("2025-01-03", "B", 10, 10, 10, 10)]
        with self.assertRaisesRegex(ValueError, "point-in-time"):
            BacktestEngine(100000).run(bars, [Signal("2025-01-02", "B", "buy", .06)])

    def test_suspension_keeps_pending_until_tradable(self):
        bars = [DailyBar("2025-01-02", "A", 10, 10, 10, 10),
                DailyBar("2025-01-03", "A", 10, 10, 10, 10, False),
                DailyBar("2025-01-06", "A", 11, 11, 11, 11)]
        result = BacktestEngine(100000).run(bars, [Signal("2025-01-02", "A", "buy", .06)])
        self.assertEqual(result.trades[0]["actual_execution_date"], "2025-01-06")
        self.assertEqual(result.orders[0].status, "filled")
