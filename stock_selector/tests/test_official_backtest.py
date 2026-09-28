from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from backtest.engine import DailyBar
from backtest.official import OfficialSignalProvider, OfficialSnapshot
from test_selector import sample_frame

from backtest.account import Account


class OfficialBacktestTests(unittest.TestCase):
    def test_official_adapter_reuses_selector_plan_and_overheat_delay(self):
        metrics = sample_frame(100)
        date = "2025-07-15"
        provider = {"market_trend": "up", "built_rows": len(metrics), "errors": {},
                    "member_codes": metrics["ticker"].tolist(),
                    "benchmark": {"return_20d": .11, "price_as_of": date}}
        day = {row.ticker: DailyBar(date, row.ticker, 10, 10, 10, 10)
               for row in metrics.itertuples()}
        with tempfile.TemporaryDirectory() as folder:
            adapter = OfficialSignalProvider({date: OfficialSnapshot(metrics, provider)}, Path(folder))
            signals = adapter(date, Account(100000), day)
        self.assertLessEqual(len(signals), 5)
        self.assertGreater(len(signals), 0)
        self.assertTrue(all(signal.target_fraction == .06 for signal in signals))
        self.assertTrue(all(signal.delay_sessions == 5 for signal in signals))

    def test_incomplete_snapshot_fails_closed(self):
        metrics = sample_frame(1)
        date = "2025-07-15"
        adapter = OfficialSignalProvider({date: OfficialSnapshot(metrics, {
            "market_trend": "up", "built_rows": 0, "errors": {"x": "missing"}})}, Path("unused"))
        with self.assertRaisesRegex(ValueError, "incomplete"):
            adapter(date, Account(100000), {})

    def test_future_benchmark_metadata_is_blocked(self):
        metrics = sample_frame(10)
        date = "2025-07-15"
        provider = {"market_trend": "up", "built_rows": len(metrics), "errors": {},
                    "benchmark": {"price_as_of": "2025-07-16", "return_20d": 0}}
        day = {ticker: DailyBar(date, ticker, 10, 10, 10, 10)
               for ticker in metrics.ticker}
        with tempfile.TemporaryDirectory() as folder:
            adapter = OfficialSignalProvider({date: OfficialSnapshot(metrics, provider)}, Path(folder))
            with self.assertRaisesRegex(ValueError, "future benchmark"):
                adapter(date, Account(100000), day)
