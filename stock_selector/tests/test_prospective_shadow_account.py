from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from backtest.engine import DailyBar
from data_public.archive import PublicSourceArchive, SourceResult
from prospective.calendar import signal_day_decision
from prospective.shadow_account import replay_continuous_shadow
from run_prospective import build_from_public_snapshot
from test_selector import sample_frame


FEE = Path(__file__).resolve().parents[2] / "PROSPECTIVE_EXECUTION_MODEL.json"


class ProspectiveShadowAccountTests(unittest.TestCase):
    def _package(self, root: Path, date: str):
        now = datetime.fromisoformat(date + "T15:01:00").replace(tzinfo=ZoneInfo("Asia/Shanghai"))
        metrics = sample_frame(300)
        metrics["universe_as_of"] = date
        metrics["membership_update_date"] = date
        metrics["financial_period"] = "2026-06-30"
        metrics["fundamental_as_of"] = "2026-09-30"
        metrics["price_as_of"] = date
        metrics["tradestatus"] = "1"
        metrics["is_st"] = "0"
        provider = {"as_of": date, "built_rows": 300, "original_members": 300,
                    "requested_members": 300, "errors": {}, "market_trend": "up",
                    "member_codes": metrics.ticker.tolist(),
                    "benchmark": {"price_as_of": date, "return_20d": 0.03}}
        archive = PublicSourceArchive(root / "public_raw")
        entry = archive.capture(SourceResult("baostock", "test", {"date": date}, b"raw",
                                             b"normalized", "1"),
                                requested_source="baostock", fallback_reason=None,
                                attempts=[{"source": "baostock", "status": "used"}])
        return build_from_public_snapshot(
            signal_date=date, generated_at=now,
            decision=signal_day_decision(now, [date]),
            sessions=[date, "2026-10-16" if date.startswith("2026-10") else "2026-11-17"],
            metrics=metrics, provider=provider, archive=archive, entries=[entry],
            output_root=root, provenance={"git_commit": "a" * 40,
                                          "working_tree_status": "", "dirty": False})

    def test_replay_keeps_prior_nav_and_position_across_months(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            october = self._package(root, "2026-10-15")
            dates = ["2026-10-15", "2026-10-16", "2026-11-16", "2026-11-17"]
            tickers = sample_frame(300).ticker.tolist()
            bars = [DailyBar(date, ticker, 100, 101, 99, 100)
                    for date in dates for ticker in tickers]
            shadow_dir = root / "prospective" / "shadow"
            first = replay_continuous_shadow(
                prediction_dirs=[october], bars=[b for b in bars if b.date <= dates[1]],
                actions=[], market_calendar=dates[:2], shadow_dir=shadow_dir,
                fee_config=FEE)
            self.assertGreater(len(first.account.positions), 0)
            old_nav = (shadow_dir / "shadow_nav.jsonl").read_text(encoding="utf-8")
            november = self._package(root, "2026-11-16")
            second = replay_continuous_shadow(
                prediction_dirs=[october, november], bars=bars,
                actions=[], market_calendar=dates, shadow_dir=shadow_dir,
                fee_config=FEE)
            self.assertTrue((shadow_dir / "shadow_nav.jsonl").read_text(encoding="utf-8")
                            .startswith(old_nav))
            self.assertGreater(len(second.account.positions), 0)
            self.assertLess(second.account.cash, 1000000)
            self.assertEqual(json.loads((shadow_dir / "shadow_manifest.json").read_text())
                             ["initial_cash"], 1000000)

    def test_order_drift_rejects_replay_before_appending_nav(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            prediction = self._package(root, "2026-10-15")
            tickers = sample_frame(300).ticker.tolist()
            dates = ["2026-10-15", "2026-10-16"]
            bars = [DailyBar(date, ticker, 100, 101, 99, 100)
                    for date in dates for ticker in tickers]
            shadow_dir = root / "prospective" / "shadow"
            replay_continuous_shadow(prediction_dirs=[prediction], bars=bars[:300],
                                     actions=[], market_calendar=dates[:1],
                                     shadow_dir=shadow_dir, fee_config=FEE)
            nav_before = (shadow_dir / "shadow_nav.jsonl").read_bytes()
            (shadow_dir / "shadow_orders.jsonl").write_text('{"tampered":true}\n')
            with self.assertRaisesRegex(ValueError, "history drift"):
                replay_continuous_shadow(prediction_dirs=[prediction], bars=bars,
                                         actions=[], market_calendar=dates,
                                         shadow_dir=shadow_dir, fee_config=FEE)
            self.assertEqual((shadow_dir / "shadow_nav.jsonl").read_bytes(), nav_before)
