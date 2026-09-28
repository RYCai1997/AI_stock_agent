from __future__ import annotations

import json
import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from backtest.engine import BacktestEngine, DailyBar, Signal
from backtest.report import write_report
from test_selector import sample_frame


class BacktestReportTests(unittest.TestCase):
    def test_report_writes_account_artifacts_and_quality_limits(self):
        dates = ["2025-01-02", "2025-01-03", "2025-01-06"]
        bars = [DailyBar(date, "A", 10, 10.1, 9.9, 10) for date in dates]
        engine = BacktestEngine(100000).run(bars, [Signal(dates[0], "A", "buy", .06)])
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder)
            write_report(engine, output, start=dates[0], end=dates[-1],
                         input_provenance={"bars_price_basis": "unadjusted",
                                           "corporate_actions_status": "unverified"},
                         snapshot_count=1)
            for name in ("summary.md", "daily_nav.csv", "orders.csv", "trades.csv",
                         "positions.csv", "performance.csv", "research_manifest.json",
                         "data_quality_report.md"):
                self.assertTrue((output / name).exists(), name)
            self.assertIn("confidence is degraded",
                          (output / "data_quality_report.md").read_text(encoding="utf-8"))
            manifest = json.loads((output / "research_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["strategy_version"], "1.0.0")
            self.assertEqual(len(pd.read_csv(output / "daily_nav.csv")), 3)

    def test_offline_cli_replays_actual_official_snapshot(self):
        import run_backtest

        dates = ["2025-07-15", "2025-07-16", "2025-07-17"]
        metrics = sample_frame(100)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            snapshot = root / "snapshots" / dates[0]
            snapshot.mkdir(parents=True)
            metrics.to_csv(snapshot / "raw_metrics.csv", index=False)
            (snapshot / "official_run_metadata.json").write_text(json.dumps({
                "provider": {"market_trend": "up", "built_rows": len(metrics), "errors": {},
                             "member_codes": metrics.ticker.tolist(),
                             "benchmark": {"return_20d": 0.0, "price_as_of": dates[0]}}
            }), encoding="utf-8")
            pd.DataFrame([{"date": date, "ticker": ticker,
                           "open": 10, "high": 10.1, "low": 9.9, "close": 10, "tradable": True}
                          for date in dates for ticker in metrics.ticker]).to_csv(root / "bars.csv", index=False)
            (root / "actions.csv").write_text("date,ticker,kind\n", encoding="utf-8")
            (root / "fees.json").write_text(json.dumps({"slippage": 0.001, "schedules": [{
                "effective_from": "2025-01-01", "effective_to": None,
                "commission_rate": 0.0003, "minimum_commission": 5,
                "stamp_duty": 0.0005, "transfer_fee": 0.00002}]}), encoding="utf-8")
            (root / "input.json").write_text(json.dumps({
                "bars_price_basis": "unadjusted", "corporate_actions_status": "unverified"}), encoding="utf-8")
            args = ["backtest", "--bars", str(root / "bars.csv"),
                    "--actions", str(root / "actions.csv"),
                    "--fee-config", str(root / "fees.json"),
                    "--input-manifest", str(root / "input.json"),
                    "--snapshots-dir", str(root / "snapshots"),
                    "--output", str(root / "report")]
            with patch.object(sys, "argv", args), contextlib.redirect_stdout(io.StringIO()):
                run_backtest.main()
            self.assertEqual(len(pd.read_csv(root / "report" / "daily_nav.csv")), 3)
            self.assertEqual(len(pd.read_csv(root / "report" / "trades.csv")), 5)
            pd.DataFrame([{"date": date, "open": 100, "close": 100, "ema200": 90}
                          for date in dates]).to_csv(root / "benchmark.csv", index=False)
            args += ["--all-variants", "--benchmark-bars", str(root / "benchmark.csv")]
            with patch.object(sys, "argv", args), contextlib.redirect_stdout(io.StringIO()):
                run_backtest.main()
            comparison = pd.read_csv(root / "report" / "variant_comparison.csv")
            self.assertEqual(comparison.variant.tolist(), list("ABCDEFGH"))
            self.assertTrue((root / "report" / "window_influence.csv").exists())
