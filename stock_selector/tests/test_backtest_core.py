from __future__ import annotations

import unittest

from backtest.account import Account
from backtest.engine import BacktestEngine, DailyBar, Signal
from backtest.execution import LimitContext, fill_block_reason


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
                DailyBar("2025-01-03", "A", 10.5, 11, 10, 11),
                DailyBar("2025-01-06", "A", 11.2, 11.3, 11, 11.2)]
        result = BacktestEngine(100000).run(bars, [Signal("2025-01-02", "A", "buy", .06)])
        self.assertEqual(result.trades[0]["signal_date"], "2025-01-02")
        self.assertEqual(result.trades[0]["actual_execution_date"], "2025-01-03")
        self.assertEqual(result.trades[0]["execution_price"], 10.5)
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
                DailyBar("2025-01-06", "A", 10.5, 10.6, 10.4, 10.5)]
        result = BacktestEngine(100000).run(bars, [Signal("2025-01-02", "A", "buy", .06)])
        self.assertEqual(result.trades[0]["actual_execution_date"], "2025-01-06")
        self.assertEqual(result.orders[0].status, "filled")

    def test_open_limit_up_blocks_buy_then_fills_on_resume(self):
        bars = [DailyBar("2025-01-02", "A", 10, 10, 10, 10),
                DailyBar("2025-01-03", "A", 11, 11, 11, 11),
                DailyBar("2025-01-06", "A", 10.8, 11, 10.5, 10.8)]
        result = BacktestEngine(100000).run(bars, [Signal("2025-01-02", "A", "buy", .06)])
        self.assertEqual(result.trades[0]["actual_execution_date"], "2025-01-06")

    def test_consecutive_limit_down_blocks_sell(self):
        bars = [DailyBar("2025-01-02", "A", 10, 10, 10, 10),
                DailyBar("2025-01-03", "A", 10, 10, 10, 10),
                DailyBar("2025-01-06", "A", 9, 9, 9, 9),
                DailyBar("2025-01-07", "A", 8.1, 8.1, 8.1, 8.1),
                DailyBar("2025-01-08", "A", 8.3, 8.5, 8.2, 8.4)]
        signals = [Signal("2025-01-02", "A", "buy", .06),
                   Signal("2025-01-03", "A", "sell")]
        result = BacktestEngine(100000).run(bars, signals)
        self.assertEqual(result.trades[-1]["actual_execution_date"], "2025-01-08")
        self.assertEqual(result.orders[-1].status, "filled")

    def test_gap_below_stop_uses_worse_open(self):
        bars = [DailyBar("2025-01-02", "A", 10, 10, 10, 10),
                DailyBar("2025-01-03", "A", 10, 10, 9.5, 9.5),
                DailyBar("2025-01-06", "A", 8.8, 8.9, 8.7, 8.8)]
        result = BacktestEngine(100000).run(bars, [Signal("2025-01-02", "A", "buy", .06)])
        stop = result.trades[-1]
        self.assertEqual(stop["planned_stop"], 9)
        self.assertEqual(stop["execution_price"], 8.8)
        self.assertAlmostEqual(stop["gap_loss"], .2 * stop["quantity"])

    def test_board_and_st_limit_rules(self):
        context = LimitContext(previous_close=10, is_st=True)
        self.assertEqual(fill_block_reason("buy", date="2025-01-03", ticker="sh.600000",
                                           open_price=10.5, tradable=True, context=context),
                         "limit_up_buy_block")
        context = LimitContext(previous_close=10)
        self.assertEqual(fill_block_reason("buy", date="2025-01-03", ticker="sh.688001",
                                           open_price=12, tradable=True, context=context),
                         "limit_up_buy_block")

    def test_overheat_delay_waits_five_full_sessions(self):
        dates = ["2025-01-02", "2025-01-03", "2025-01-06", "2025-01-07",
                 "2025-01-08", "2025-01-09", "2025-01-10"]
        bars = [DailyBar(date, "A", 10, 10, 10, 10) for date in dates]
        result = BacktestEngine(100000).run(
            bars, [Signal(dates[0], "A", "buy", .06, delay_sessions=5)])
        self.assertEqual(result.trades[0]["actual_execution_date"], dates[6])
