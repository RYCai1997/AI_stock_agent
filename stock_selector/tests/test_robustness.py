from __future__ import annotations

import unittest

from backtest.engine import DailyBar, Signal
from backtest.fees import ZERO_COST_MODEL
from backtest.robustness import leave_one_window_out


class RobustnessTests(unittest.TestCase):
    def test_leave_one_window_out_reruns_account_path(self):
        dates = ["2025-01-02", "2025-01-03", "2025-01-06"]
        bars = [DailyBar(date, ticker, 10, 12, 9.9,
                         (11 if ticker == "A" else 12) if date == dates[-1] else 10)
                for date in dates for ticker in ("A", "B")]

        def factory(_removed):
            def provider(date, _account, _day):
                return [Signal(date, "A", "buy", .06)] if date == dates[0] else (
                    [Signal(date, "B", "buy", .06)] if date == dates[1] else [])
            return provider

        result = leave_one_window_out(
            windows=dates[:2], bars=bars, actions=[], fee_model=ZERO_COST_MODEL,
            initial_cash=100000, provider_factory=factory)
        self.assertEqual(result.removed_window.tolist(), dates[:2])
        self.assertNotEqual(result.iloc[0]["cumulative_return"],
                            result.iloc[1]["cumulative_return"])
