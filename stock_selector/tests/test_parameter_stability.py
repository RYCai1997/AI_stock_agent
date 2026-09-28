from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from backtest.engine import DailyBar
from backtest.fees import ZERO_COST_MODEL
from backtest.official import OfficialSnapshot
from backtest.parameter_stability import parameter_surface
from selector.strategy import OFFICIAL_STRATEGY
from test_selector import sample_frame


class ParameterStabilityTests(unittest.TestCase):
    def test_neighbors_do_not_mutate_formal_v1(self):
        metrics = sample_frame(100)
        dates = ["2025-07-15", "2025-07-16", "2025-07-17"]
        provider = {"market_trend": "up", "built_rows": 100, "errors": {},
                    "member_codes": metrics.ticker.tolist(),
                    "benchmark": {"return_20d": 0, "price_as_of": dates[0]}}
        bars = [DailyBar(date, ticker, 10, 10.1, 9.9, 10)
                for date in dates for ticker in metrics.ticker]
        before = OFFICIAL_STRATEGY.to_dict()
        with tempfile.TemporaryDirectory() as folder:
            table = parameter_surface(
                snapshots={dates[0]: OfficialSnapshot(metrics, provider)},
                bars=bars, actions=[], fee_model=ZERO_COST_MODEL,
                initial_cash=100000, audit_dir=Path(folder))
        self.assertEqual(len(table), 13)
        self.assertEqual((table.status == "completed").sum(), 8)
        self.assertEqual((table.status == "not_run_missing_adjusted_history").sum(), 5)
        self.assertEqual(OFFICIAL_STRATEGY.to_dict(), before)
        self.assertEqual(OFFICIAL_STRATEGY.stop_loss_fraction, .10)
        self.assertEqual(OFFICIAL_STRATEGY.max_new_positions_per_window, 5)
