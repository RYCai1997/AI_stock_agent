from __future__ import annotations

import unittest

from backtest.engine import BacktestEngine, DailyBar, Signal
from backtest.fees import FeeModel, FeeSchedule


class FeeTests(unittest.TestCase):
    def setUp(self):
        self.model = FeeModel((
            FeeSchedule("2020-01-01", "2023-08-27", .0003, 5, .001, .00002),
            FeeSchedule("2023-08-28", None, .0003, 5, .0005, .00002),
        ), slippage=.001)

    def test_minimum_commission_stamp_direction_transfer_and_dates(self):
        buy = self.model.fee("2023-08-28", "buy", 100, 10)
        sell = self.model.fee("2023-08-28", "sell", 100, 10)
        older = self.model.fee("2023-08-27", "sell", 100, 10)
        self.assertEqual(buy.commission, 5)
        self.assertEqual(buy.stamp_duty, 0)
        self.assertAlmostEqual(buy.transfer_fee, .02)
        self.assertAlmostEqual(sell.stamp_duty, .5)
        self.assertAlmostEqual(older.stamp_duty, 1)
        self.assertAlmostEqual(self.model.execution_price(10, "buy"), 10.01)
        self.assertAlmostEqual(self.model.execution_price(10, "sell"), 9.99)

    def test_fee_schedule_gap_fails_loudly(self):
        with self.assertRaisesRegex(ValueError, "no unique fee schedule"):
            self.model.schedule_for("2019-12-31")

    def test_engine_charges_costs_into_cash_and_nav(self):
        bars = [DailyBar("2025-01-02", "A", 10, 10, 10, 10),
                DailyBar("2025-01-03", "A", 10, 10.1, 9.9, 10)]
        result = BacktestEngine(100000, fee_model=self.model).run(
            bars, [Signal("2025-01-02", "A", "buy", .06)])
        self.assertGreater(result.trades[0]["fee"], 0)
        self.assertAlmostEqual(result.daily_nav[-1]["fees"], result.trades[0]["fee"])
        self.assertAlmostEqual(result.daily_nav[-1]["cash"] + result.daily_nav[-1]["market_value"],
                               result.daily_nav[-1]["total_equity"])

    def test_commission_does_not_raise_formal_stop_above_fill_price_basis(self):
        bars = [DailyBar("2025-01-02", "A", 10, 10, 10, 10),
                DailyBar("2025-01-03", "A", 10, 10, 10, 10),
                DailyBar("2025-01-06", "A", 9.5, 9.6, 9.005, 9.4)]
        result = BacktestEngine(100000, fee_model=FeeModel(self.model.schedules, slippage=0)).run(
            bars, [Signal("2025-01-02", "A", "buy", .06)])
        self.assertEqual(len(result.trades), 1)
        self.assertIn("A", result.account.positions)
