"""Regression test for a common evaluation start across staggered ETF listings."""

import unittest

import numpy as np
import pandas as pd

from portfolio_core.ema_trailing import run_ema_trailing_portfolio


class AHStopSweepTests(unittest.TestCase):
    def test_requested_evaluation_start_is_respected(self) -> None:
        index = pd.bdate_range("2020-01-01", periods=100)
        path = np.concatenate([np.linspace(100, 80, 30), np.linspace(81, 120, 40), np.linspace(119, 100, 30)])
        symbols = ["510300.SS", "159949.SZ", "513180.SS", "510500.SS", "510880.SS"]
        prices = pd.DataFrame({symbol: path for symbol in symbols}, index=index)
        requested = index[70]
        result = run_ema_trailing_portfolio(
            prices,
            symbols,
            benchmark="510300.SS",
            ema_days=10,
            trailing_drawdown=0.10,
            stop_loss=0.03,
            evaluation_start=requested,
        )
        self.assertEqual(result.returns.index.min(), requested)
        if not result.trades.empty:
            self.assertTrue((pd.to_datetime(result.trades["exit_date"]) >= requested).all())

    def test_explicit_start_can_precede_first_entry(self) -> None:
        index = pd.bdate_range("2020-01-01", periods=100)
        path = np.concatenate([np.linspace(100, 80, 50), np.linspace(81, 120, 50)])
        symbols = ["510300.SS", "159949.SZ", "513180.SS", "510500.SS", "510880.SS"]
        prices = pd.DataFrame({symbol: path for symbol in symbols}, index=index)
        result = run_ema_trailing_portfolio(
            prices,
            symbols,
            benchmark="510300.SS",
            ema_days=10,
            evaluation_start=index[20],
        )
        self.assertEqual(result.returns.index.min(), index[20])
        self.assertEqual(float(result.weights.loc[index[20]].sum()), 0.0)


if __name__ == "__main__":
    unittest.main()
