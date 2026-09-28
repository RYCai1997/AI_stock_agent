from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from backtest.engine import BacktestEngine, DailyBar
from backtest.fees import FeeModel, FeeSchedule
from backtest.official import OfficialSnapshot
from backtest.variants import VARIANTS, VariantSignalProvider
from selector.strategy import OFFICIAL_STRATEGY
from test_selector import sample_frame


class BacktestContractTests(unittest.TestCase):
    def test_f_and_h_share_fee_and_open_execution_for_same_snapshot(self):
        metrics = sample_frame(100)
        dates = ["2025-07-15", "2025-07-16"]
        snapshot = OfficialSnapshot(metrics, {
            "market_trend": "up", "built_rows": 100, "errors": {},
            "member_codes": metrics.ticker.tolist(),
            "benchmark": {"return_20d": 0, "price_as_of": dates[0]},
        })
        bars = [DailyBar(date, ticker, 10, 10.1, 9.9, 10)
                for date in dates for ticker in metrics.ticker]
        fees = FeeModel((FeeSchedule("2025-01-01", None, .0003, 5, .0005, .00002),),
                        slippage=.001)
        with tempfile.TemporaryDirectory() as folder:
            outcomes = {}
            for code in ("F", "H"):
                provider = VariantSignalProvider(
                    VARIANTS[code], {dates[0]: snapshot}, Path(folder) / code)
                outcomes[code] = BacktestEngine(
                    100000, fee_model=fees, stop_enabled=VARIANTS[code].stop_loss
                ).run(bars, [], signal_provider=provider)
        f_buys = [(trade["ticker"], trade["actual_execution_date"],
                   trade["execution_price"], trade["fee"])
                  for trade in outcomes["F"].trades if trade["side"] == "buy"]
        h_buys = [(trade["ticker"], trade["actual_execution_date"],
                   trade["execution_price"], trade["fee"])
                  for trade in outcomes["H"].trades if trade["side"] == "buy"]
        self.assertGreater(len(f_buys), 0)
        self.assertEqual(f_buys, h_buys)

    def test_frozen_v1_contract(self):
        strategy = OFFICIAL_STRATEGY
        self.assertEqual(strategy.strategy_id, "A_CSI300_QVM_TIMING_V1")
        self.assertEqual(strategy.strategy_version, "1.0.0")
        self.assertEqual(strategy.fixed_position_fraction, .06)
        self.assertEqual(strategy.max_new_exposure_per_window, .30)
        self.assertEqual(strategy.max_new_positions_per_window, 5)
        self.assertEqual(strategy.overheat_return_sessions, 20)
        self.assertEqual(strategy.overheat_return_threshold, .10)
        self.assertEqual(strategy.overheat_delay_sessions, 5)
        self.assertEqual(strategy.stop_loss_fraction, .10)
        self.assertEqual(strategy.confirmation_reviews, 2)
        self.assertEqual(strategy.selector_config().quality_quantile, .50)
        self.assertEqual(strategy.selector_config().value_min_score, 20.0)
        self.assertEqual(strategy.selector_config().momentum_top_fraction, .20)
