from __future__ import annotations

import unittest
from types import SimpleNamespace

from backtest.engine import DailyBar
from backtest.fees import ZERO_COST_MODEL
from backtest.monte_carlo import selection_monte_carlo, selection_monte_carlo_horizons
from backtest.official import OfficialSnapshot
from test_selector import sample_frame


class MonteCarloTests(unittest.TestCase):
    def test_predeclared_horizons_remain_separate_and_report_short_paths(self):
        metrics = sample_frame(10)
        window = "2025-01-02"
        dates = [f"2025-01-{day:02d}" for day in range(2, 26)]
        bars = [DailyBar(date, ticker, 10, 10, 10, 10)
                for date in dates for ticker in metrics.ticker]
        orders = [SimpleNamespace(side="buy", signal_date=window, status="filled",
                                  ticker=metrics.ticker.iloc[0])]
        result = selection_monte_carlo_horizons(
            windows=[window], snapshots={window: OfficialSnapshot(
                metrics, {"benchmark": {"return_20d": 0}})},
            bars=bars, actions=[], fee_model=ZERO_COST_MODEL, initial_cash=100000,
            actual_orders=orders, iterations=1000, seed=7)
        self.assertEqual(result.horizon_sessions.tolist(), [20, 63, 126])
        self.assertEqual(result.status.tolist(),
                         ["completed", "not_run_insufficient_horizon",
                          "not_run_insufficient_horizon"])
        self.assertEqual(result.iloc[0]["iterations"], 1000)
    def test_fixed_seed_and_same_window_count(self):
        metrics = sample_frame(10)
        window = "2025-01-02"
        dates = [f"2025-01-{day:02d}" for day in range(2, 26)]
        bars = [DailyBar(date, ticker, 10, 10.1, 9.9,
                         10 + (int(ticker[-1]) % 3) * .01 * index)
                for index, date in enumerate(dates) for ticker in metrics.ticker]
        selected = metrics.ticker.iloc[:2].tolist()
        orders = [SimpleNamespace(side="buy", signal_date=window, status="filled", ticker=ticker)
                  for ticker in selected]
        kwargs = dict(windows=[window], snapshots={window: OfficialSnapshot(
            metrics, {"benchmark": {"return_20d": 0}})}, bars=bars, actions=[],
            fee_model=ZERO_COST_MODEL, initial_cash=100000, actual_orders=orders,
            iterations=1000, seed=7, horizon_sessions=2)
        first = selection_monte_carlo(**kwargs)
        second = selection_monte_carlo(**kwargs)
        self.assertEqual(first.to_dict("records"), second.to_dict("records"))
        self.assertEqual(first.iloc[0]["selected_count"], 2)
        self.assertEqual(first.iloc[0]["iterations"], 1000)
        self.assertEqual(first.iloc[0]["status"], "completed")

    def test_missing_horizon_reports_no_percentile(self):
        metrics = sample_frame(10)
        window = "2025-01-02"
        bars = [DailyBar(window, ticker, 10, 10, 10, 10) for ticker in metrics.ticker]
        orders = [SimpleNamespace(side="buy", signal_date=window, status="filled",
                                  ticker=metrics.ticker.iloc[0])]
        result = selection_monte_carlo(
            windows=[window], snapshots={window: OfficialSnapshot(metrics, {})},
            bars=bars, actions=[], fee_model=ZERO_COST_MODEL, initial_cash=100000,
            actual_orders=orders)
        self.assertEqual(result.iloc[0]["status"], "not_run_insufficient_horizon")
        self.assertNotIn("actual_percentile_at_or_below", result)
