from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from backtest.account import Account
from backtest.engine import DailyBar
from backtest.nested_attribution import (NESTED_LADDER, NestedSignalProvider,
                                         changed_flags, incremental_attribution)
from backtest.official import OfficialSnapshot
from test_selector import sample_frame


class NestedAttributionTests(unittest.TestCase):
    def test_each_adjacent_step_changes_one_mechanism(self):
        self.assertEqual([changed_flags(left, right) for left, right in
                          zip(NESTED_LADDER, NESTED_LADDER[1:])],
                         [["market_ema"], ["stock_ema"], ["quality"], ["value"],
                          ["stop_loss"], ["overheat_delay"], ["monthly_exit"]])

    def test_full_candidate_path_matches_frozen_official_adapter(self):
        date = "2025-07-15"
        metrics = sample_frame(100)
        snapshot = OfficialSnapshot(metrics, {
            "market_trend": "up", "built_rows": len(metrics), "errors": {},
            "member_codes": metrics.ticker.tolist(),
            "benchmark": {"return_20d": .12, "price_as_of": date},
        })
        day = {ticker: DailyBar(date, ticker, 10, 10, 10, 10) for ticker in metrics.ticker}
        with tempfile.TemporaryDirectory() as folder:
            pre_exit = NestedSignalProvider(NESTED_LADDER[-2], {date: snapshot}, Path(folder) / "R6")
            full = NestedSignalProvider(NESTED_LADDER[-1], {date: snapshot}, Path(folder) / "R7")
            signals_before = pre_exit(date, Account(100000), day)
            signals_full = full(date, Account(100000), day)
        self.assertEqual([(s.ticker, s.target_fraction, s.delay_sessions) for s in signals_before],
                         [(s.ticker, s.target_fraction, s.delay_sessions) for s in signals_full])

    def test_incremental_table_uses_only_adjacent_results(self):
        rows = [{"variant": layer.code, "cumulative_return": index / 100,
                 "maximum_drawdown": -index / 100} for index, layer in enumerate(NESTED_LADDER)]
        table = incremental_attribution(rows)
        self.assertEqual(len(table), 8)
        self.assertAlmostEqual(table.iloc[1]["incremental_cumulative_return"], .01)
        self.assertEqual(table.iloc[-1]["changed_layer"], "monthly confirmation exit")
