"""Regression tests for ETF Rotation V1. Run with: python -m unittest test_etf_rotation.py"""

import unittest

import numpy as np
import pandas as pd

from portfolio_core.backtest import run_rotation_backtest
from portfolio_core.config import RotationConfig
from portfolio_core.factors import calculate_factors
from portfolio_core.news_overlay import aggregate_news_risk
from portfolio_core.portfolio import build_signal_weights, schedule_close_execution
from portfolio_core.providers import _timestamps_to_exchange_dates


class ETFRotationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.index = pd.bdate_range("2020-01-01", periods=340)
        x = np.arange(len(self.index), dtype=float)
        self.prices = pd.DataFrame({
            "UP": 100.0 * np.exp(x * 0.0015),
            "SLOW": 100.0 * np.exp(x * 0.0005),
            "DOWN": 100.0 * np.exp(x * -0.0010),
            "BENCH": 100.0 * np.exp(x * 0.0007),
        }, index=self.index)

    def config(self, exposure: float = 0.70) -> RotationConfig:
        return RotationConfig(
            target_exposure=exposure,
            max_asset_weight=0.30,
            top_n=3,
            transaction_cost_bps=10,
        )

    def test_factors_are_causal(self) -> None:
        config = self.config()
        original = calculate_factors(self.prices, config)["score"]
        changed = self.prices.copy()
        changed.iloc[-1, changed.columns.get_loc("UP")] *= 100
        revised = calculate_factors(changed, config)["score"]
        pd.testing.assert_frame_equal(original.iloc[:-1], revised.iloc[:-1])

    def test_weight_caps_and_cash(self) -> None:
        config = self.config(0.70)
        factors = calculate_factors(self.prices, config)
        weights = build_signal_weights(factors["score"], factors["eligible"], config)
        self.assertLessEqual(float(weights.max().max()), 0.30 + 1e-12)
        self.assertLessEqual(float(weights.sum(axis=1).max()), 0.70 + 1e-12)

    def test_signal_executes_at_next_close(self) -> None:
        columns = ["UP"]
        signals = pd.DataFrame([[0.3]], index=[self.index[20]], columns=columns)
        events = schedule_close_execution(signals, self.index)
        self.assertTrue(pd.isna(events.loc[self.index[20], "UP"]))
        self.assertEqual(float(events.loc[self.index[21], "UP"]), 0.3)

    def test_cost_and_exposure_are_accounted(self) -> None:
        result = run_rotation_backtest(self.prices, "BENCH", self.config())
        self.assertGreater(float(result.turnover.sum()), 0)
        self.assertAlmostEqual(float(result.costs.sum()), float(result.turnover.sum()) * 0.001, places=12)
        self.assertLessEqual(float(result.weights.sum(axis=1).max()), 0.70 + 1e-12)

    def test_news_overlay_never_returns_position(self) -> None:
        holdings = pd.DataFrame([{
            "etf": "ETF", "ticker": "AAA", "company": "A", "weight": 0.1,
            "as_of": "2026-09-01", "source_url": "https://example.test/holdings",
        }])
        assessments = pd.DataFrame([{
            "ticker": "AAA", "event_date": "2026-09-02", "severity": 0.9,
            "confidence": 0.9, "summary": "material event", "source_url": "https://example.test/news",
        }])
        result = aggregate_news_risk(holdings, assessments)
        self.assertTrue(bool(result.loc["ETF", "risk_flag"]))
        self.assertEqual(result.loc["ETF", "action_scope"], "risk_review_only")
        self.assertNotIn("target_weight", result.columns)

    def test_duplicate_news_does_not_duplicate_holding_weight(self) -> None:
        holdings = pd.DataFrame([{
            "etf": "ETF", "ticker": "AAA", "company": "A", "weight": 0.1,
            "as_of": "2026-09-01", "source_url": "https://example.test/holdings",
        }])
        assessments = pd.DataFrame([
            {"ticker": "AAA", "event_date": "2026-09-02", "severity": 0.4, "confidence": 1.0,
             "summary": "first", "source_url": "https://example.test/1"},
            {"ticker": "AAA", "event_date": "2026-09-03", "severity": 0.8, "confidence": 1.0,
             "summary": "second", "source_url": "https://example.test/2"},
        ])
        result = aggregate_news_risk(holdings, assessments)
        self.assertAlmostEqual(float(result.loc["ETF", "assessed_weight"]), 0.1)
        self.assertAlmostEqual(float(result.loc["ETF", "weighted_risk"]), 0.08)

    def test_market_dates_use_exchange_timezone(self) -> None:
        timestamp = int(pd.Timestamp("2026-09-04 01:30:00", tz="UTC").timestamp())
        shanghai = _timestamps_to_exchange_dates([timestamp], "Asia/Shanghai")
        new_york = _timestamps_to_exchange_dates([timestamp], "America/New_York")
        self.assertEqual(str(shanghai[0].date()), "2026-09-04")
        self.assertEqual(str(new_york[0].date()), "2026-09-03")


if __name__ == "__main__":
    unittest.main()
