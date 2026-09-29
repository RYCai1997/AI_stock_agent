from __future__ import annotations

import unittest

from backtest.engine import BacktestEngine, DailyBar, Signal
from backtest.corporate_actions import CorporateAction


def bar(date: str, ticker: str, price: float, tradable: bool = True) -> DailyBar:
    return DailyBar(date, ticker, price, price, price, price, tradable)


class SuspensionNormalizationTests(unittest.TestCase):
    def test_missing_held_bar_carries_only_valuation_and_blocks_order(self):
        bars = [bar("2025-01-02", "A", 10), bar("2025-01-03", "A", 10),
                bar("2025-01-06", "B", 20), bar("2025-01-07", "A", 10.5)]
        signals = [Signal("2025-01-02", "A", "buy", .06),
                   Signal("2025-01-03", "A", "sell")]
        result = BacktestEngine(100000, stop_enabled=False).run(bars, signals)
        missing_day = next(row for row in result.daily_nav if row["date"] == "2025-01-06")
        held = next(row for row in result.positions if row["date"] == "2025-01-06")
        self.assertEqual(missing_day["stale_positions"], 1)
        self.assertEqual(held["valuation_price"], 10)
        self.assertTrue(held["valuation_stale"])
        self.assertEqual(held["valuation_price_date"], "2025-01-03")
        self.assertEqual(result.orders[-1].block_history[0]["reason"], "missing_security_bar")
        self.assertEqual(result.trades[-1]["actual_execution_date"], "2025-01-07")

    def test_resume_limit_uses_last_actual_close_not_previous_market_day(self):
        bars = [bar("2025-01-02", "A", 10), bar("2025-01-03", "A", 10),
                bar("2025-01-06", "B", 20), bar("2025-01-07", "A", 11),
                bar("2025-01-08", "A", 10.8)]
        result = BacktestEngine(100000, stop_enabled=False).run(
            bars, [Signal("2025-01-03", "A", "buy", .06)])
        self.assertEqual([x["reason"] for x in result.orders[0].block_history],
                         ["missing_security_bar", "limit_up_buy_block"])
        self.assertEqual(result.trades[0]["actual_execution_date"], "2025-01-08")

    def test_explicit_market_calendar_preserves_empty_bar_day(self):
        bars = [bar("2025-01-02", "A", 10), bar("2025-01-03", "A", 10),
                bar("2025-01-07", "A", 10)]
        calendar = ["2025-01-02", "2025-01-03", "2025-01-06", "2025-01-07"]
        result = BacktestEngine(100000, stop_enabled=False).run(
            bars, [Signal("2025-01-02", "A", "buy", .06)], market_calendar=calendar)
        self.assertEqual([x["date"] for x in result.daily_nav], calendar)
        self.assertEqual(result.daily_nav[2]["stale_positions"], 1)

    def test_corporate_action_day_without_adjusted_limit_reference_blocks_fill(self):
        bars = [bar("2025-01-02", "A", 10), bar("2025-01-03", "A", 10),
                bar("2025-01-06", "A", 5.5), bar("2025-01-07", "A", 5.4)]
        result = BacktestEngine(100000, stop_enabled=False).run(
            bars, [Signal("2025-01-03", "A", "buy", .06)],
            [CorporateAction("2025-01-06", "A", "split", share_ratio=1)])
        self.assertEqual(result.orders[0].block_history[0]["reason"],
                         "corporate_action_limit_reference_unverified")
        self.assertEqual(result.trades[0]["actual_execution_date"], "2025-01-07")
