from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from backtest.account import Account
from backtest.engine import BacktestEngine, DailyBar, Signal
from backtest.official import OfficialSnapshot
from backtest.variants import VARIANTS, VariantSignalProvider, variant_candidates, run_index_variant
from test_selector import sample_frame


class VariantTests(unittest.TestCase):
    def test_ablation_layers_are_predeclared_and_v1_frozen(self):
        self.assertEqual(sorted(VARIANTS), list("ABCDEFGH"))
        self.assertFalse(VARIANTS["F"].stop_loss)
        self.assertTrue(VARIANTS["G"].stop_loss)
        self.assertTrue(VARIANTS["H"].monthly_exit)
        self.assertTrue(VARIANTS["H"].overheat_delay)

    def test_market_gate_changes_candidates_without_changing_input(self):
        metrics = sample_frame(100)
        date = "2025-07-15"
        self.assertGreater(len(variant_candidates(metrics, date, "down", VARIANTS["C"])), 0)
        self.assertEqual(len(variant_candidates(metrics, date, "down", VARIANTS["D"])), 0)
        self.assertEqual(len(metrics), 100)

    def test_all_stock_variants_use_same_engine_type(self):
        metrics = sample_frame(100)
        date = "2025-07-15"
        snapshot = OfficialSnapshot(metrics, {"market_trend": "up", "built_rows": 100,
                                              "errors": {}, "member_codes": metrics.ticker.tolist(),
                                              "benchmark": {"return_20d": 0}})
        day = {ticker: DailyBar(date, ticker, 10, 10, 10, 10) for ticker in metrics.ticker}
        with tempfile.TemporaryDirectory() as folder:
            for code, spec in VARIANTS.items():
                if spec.kind == "index":
                    continue
                provider = VariantSignalProvider(spec, {date: snapshot}, Path(folder) / code)
                signals = provider(date, Account(100000), day)
                self.assertIsInstance(BacktestEngine(100000, stop_enabled=spec.stop_loss), BacktestEngine)
                self.assertLessEqual(len(signals), 5)
            f = VariantSignalProvider(VARIANTS["F"], {date: snapshot}, Path(folder) / "F2")
            h = VariantSignalProvider(VARIANTS["H"], {date: snapshot}, Path(folder) / "H2")
            self.assertEqual([s.ticker for s in f(date, Account(100000), day)],
                             [s.ticker for s in h(date, Account(100000), day)])

    def test_index_ablation_uses_prior_close_and_next_open(self):
        import pandas as pd

        frame = pd.DataFrame([
            {"date": "2025-01-02", "open": 100, "close": 100, "ema200": 110},
            {"date": "2025-01-03", "open": 90, "close": 95, "ema200": 90},
            {"date": "2025-01-06", "open": 95, "close": 100, "ema200": 90},
        ])
        buy_hold = run_index_variant(frame, VARIANTS["A"], 100000)
        timed = run_index_variant(frame, VARIANTS["B"], 100000)
        self.assertEqual(buy_hold.trades[0]["actual_execution_date"], "2025-01-03")
        self.assertEqual(timed.trades[0]["actual_execution_date"], "2025-01-06")
        self.assertNotEqual(buy_hold.daily_nav[-1]["total_equity"],
                            timed.daily_nav[-1]["total_equity"])
