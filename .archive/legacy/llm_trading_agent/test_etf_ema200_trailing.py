"""Regression tests for the EMA200 trailing-exit experiment."""

import unittest

import numpy as np
import pandas as pd

from portfolio_core.ema_trailing import run_ema_trailing_portfolio


class EMATrailingTests(unittest.TestCase):
    def prices(self) -> pd.DataFrame:
        index = pd.bdate_range("2020-01-01", periods=320)
        base = np.concatenate([
            np.linspace(100, 80, 210),
            np.linspace(81, 120, 60),
            np.linspace(119, 108, 50),
        ])
        return pd.DataFrame({name: base * scale for name, scale in {
            "SPY": 1.0, "QQQ": 1.1, "IWM": 0.9, "TLT": 0.8, "GLD": 1.2,
        }.items()}, index=index)

    def test_long_only_weight_limits_and_trailing_exit(self) -> None:
        symbols = ["SPY", "QQQ", "IWM", "TLT", "GLD"]
        result = run_ema_trailing_portfolio(self.prices(), symbols, ema_days=20)
        self.assertGreater(len(result.trades), 0)
        self.assertGreaterEqual(float(result.weights.min().min()), 0.0)
        self.assertLessEqual(float(result.weights.max().max()), 0.20 + 1e-12)
        self.assertLessEqual(float(result.weights.sum(axis=1).max()), 1.0 + 1e-12)

    def test_cost_matches_turnover(self) -> None:
        symbols = ["SPY", "QQQ", "IWM", "TLT", "GLD"]
        result = run_ema_trailing_portfolio(self.prices(), symbols, ema_days=20, transaction_cost_bps=10)
        self.assertAlmostEqual(float(result.costs.sum()), float(result.turnover.sum()) * 0.001, places=12)


if __name__ == "__main__":
    unittest.main()
