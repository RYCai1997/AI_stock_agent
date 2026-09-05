"""Tests for the 10% trailing exit and 2% loss stop."""

import unittest

import numpy as np
import pandas as pd

from portfolio_core.ema_trailing import run_ema_trailing_portfolio


class EMAStopLossTests(unittest.TestCase):
    def test_two_percent_stop_is_recorded(self) -> None:
        path = np.array([100, 99, 98, 97, 96, 97, 99, 100, 97, 96] + [95] * 20)
        index = pd.bdate_range("2020-01-01", periods=len(path))
        prices = pd.DataFrame({symbol: path for symbol in ["SPY", "QQQ", "IWM", "TLT", "GLD"]}, index=index)
        result = run_ema_trailing_portfolio(
            prices,
            list(prices.columns),
            ema_days=5,
            trailing_drawdown=0.50,
            stop_loss=0.02,
        )
        self.assertGreater(len(result.trades), 0)
        self.assertIn("stop_loss", set(result.trades["exit_reason"]))

    def test_invalid_stop_is_rejected(self) -> None:
        index = pd.bdate_range("2020-01-01", periods=20)
        prices = pd.DataFrame({symbol: np.arange(20) + 100 for symbol in ["SPY", "QQQ", "IWM", "TLT", "GLD"]}, index=index)
        with self.assertRaises(ValueError):
            run_ema_trailing_portfolio(prices, list(prices.columns), ema_days=5, stop_loss=0)


if __name__ == "__main__":
    unittest.main()
