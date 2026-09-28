from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import runpy
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import pandas as pd

from selector.providers.a_baostock import build_a_metrics, fetch_holding_quotes
from selector.run_feedback import (
    PROGRESS_PREFIX, emit_progress, format_run_summary, parse_progress,
    progress_label, progress_percent,
)
from test_selector import sample_frame


APP_DIR = Path(__file__).resolve().parents[1]


def example_metadata():
    return {
        "provider": {"requested_members": 300, "built_rows": 300, "errors": {},
                     "cache_hits": 64, "market_trend": "down", "benchmark": {
                         "price_as_of": "2026-09-24"}, "membership_snapshot": "2026-09-21"},
        "selector": {"as_of": "2026-09-27", "counts": {
            "quality_complete": 239, "quality_pass": 120, "value_pass": 117,
            "fundamental_candidates": 24, "actionable_candidates": 0},
            "data_date_ranges": {"price_as_of": {"min": "2026-09-24", "max": "2026-09-24"}}},
        "plan": {"ideas": 0}, "holding_review": {}, "quote_errors": {},
    }


class FeedbackTests(unittest.TestCase):
    def test_progress_roundtrip_and_validation(self):
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            emit_progress(2, 64, 300, "缓存完成")
        event = parse_progress(stream.getvalue())
        self.assertAlmostEqual(progress_percent(event), 100 * 64 / 300)
        self.assertIn("2/5", progress_label(event))
        self.assertIn("64/300，21.3%", progress_label(event))
        for text in ("log line", PROGRESS_PREFIX + "{", PROGRESS_PREFIX + "[]",
                     PROGRESS_PREFIX + '{}'):
            self.assertIsNone(parse_progress(text))
        for update in ({"stage": 6}, {"completed": -1}, {"completed": 301},
                       {"total": 0}, {"total": True}, {"detail": 42}):
            self.assertIsNone(parse_progress(PROGRESS_PREFIX + json.dumps({**event, **update})))

    def test_summary_explains_market_gate_and_dates(self):
        summary = format_run_summary(example_metadata())
        for expected in ("300只股票都已完成取数", "24只通过Q/V/M", "可执行候选0只",
                         "建仓计划0只", "暂停新建仓", "2026-09-24", "并非实时行情",
                         "不等于全部卖出", "质量指标齐全239只"):
            self.assertIn(expected, summary)

    def test_summary_distinguishes_empty_watchlist_timing_errors_and_budget(self):
        meta = example_metadata()
        meta["provider"]["market_trend"] = "up"
        self.assertIn("没有股票通过个股趋势确认", format_run_summary(meta))
        meta["selector"]["counts"]["fundamental_candidates"] = 0
        self.assertIn("没有股票通过Q/V/M筛选", format_run_summary(meta))
        meta["provider"].update(built_rows=299, errors={"test": "failed"}, market_trend="unknown")
        meta["provider"]["benchmark"] = {}
        meta["account_guidance"] = {"positive_quantity_plans": 0,
                                   "zero_quantity_reasons": {"请填写账户": 5}}
        summary = format_run_summary(meta)
        for expected in ("成功构建299只", "取数错误1只", "建议买入股数已置零",
                         "趋势数据不足或未知", "行情日期：未知", "5只未生成买入股数"):
            self.assertIn(expected, summary)
        self.assertNotIn("没有记录下载错误", summary)

    def test_provider_counts_cache_success_and_failure(self):
        universe = [{"code": f"sh.60000{i}", "code_name": f"Company {i}",
                     "updateDate": f"2025-07-0{i + 1}"} for i in range(3)]
        bs = SimpleNamespace(login=Mock(return_value=SimpleNamespace(error_code="0")),
                             logout=Mock(), query_hs300_stocks=Mock(return_value=universe),
                             query_stock_industry=Mock(return_value=[]))
        events = []
        cached = sample_frame(1).iloc[0].to_dict()
        cached["ticker"] = "sh.600000"
        quality = {"profit": {"pubDate": "2025-04-01", "roeAvg": ".1"},
                   "cash": {"pubDate": "2025-04-01", "CFOToOR": ".1"},
                   "statDate": "2025-03-31"}
        price = {"price_as_of": "2025-07-15", "mom_12_1": .2, "tradestatus": "1", "is_st": "0"}
        with tempfile.TemporaryDirectory() as folder, patch.dict(sys.modules, {"baostock": bs}), \
                patch("selector.providers.a_baostock._rows", side_effect=lambda rows: rows), \
                patch("selector.providers.a_baostock._market_trend", return_value=("up", {"price_as_of": "2025-07-15"})), \
                patch("selector.providers.a_baostock._latest_quality", side_effect=[(quality, []), RuntimeError("offline")]), \
                patch("selector.providers.a_baostock._price_metrics", return_value=price):
            (Path(folder) / "2025-07-15_sh.600000.json").write_text(json.dumps({
                "cache_version": 3, "as_of": "2025-07-15", "row": cached}), encoding="utf-8")
            rows, meta = build_a_metrics("2025-07-15", cache_dir=Path(folder), progress=lambda *e: events.append(e))
        self.assertEqual(meta["cache_hits"], 1)
        self.assertEqual(meta["membership_update_min"], "2025-07-01")
        self.assertEqual(meta["membership_update_max"], "2025-07-03")
        self.assertEqual(meta["membership_update_unique_count"], 3)
        self.assertEqual(meta["membership_updates_by_ticker"]["sh.600001"], "2025-07-02")
        self.assertEqual(rows.set_index("ticker").loc["sh.600000", "membership_update_date"], "2025-07-01")
        self.assertEqual(meta["built_rows"], 2)
        self.assertEqual(len(meta["errors"]), 1)
        completed = [e[1] for e in events if e[0] == 2]
        self.assertEqual(sorted(set(completed)), [0, 1, 2, 3])
        self.assertEqual(completed, sorted(completed))
        self.assertIn("取数失败", events[-1][3])
        bs.logout.assert_called_once()

    def test_quotes_report_partial_errors_and_login_failure(self):
        holdings = pd.DataFrame([{"ticker": "sh.600001", "entry_date": "2025-07-15"},
                                 {"ticker": "sh.600002", "entry_date": "2025-07-15"}])
        bs = SimpleNamespace(login=Mock(return_value=SimpleNamespace(error_code="0")),
                             logout=Mock(), query_history_k_data_plus=Mock(return_value=[]))
        events = []
        with patch.dict(sys.modules, {"baostock": bs}), \
                patch("selector.providers.a_baostock._rows", side_effect=lambda r: r), \
                patch("selector.providers.a_baostock.quote_from_frames", side_effect=[{"price": 10}, ValueError("no data")]):
            quotes = fetch_holding_quotes(holdings, "2025-07-15", progress=lambda *e: events.append(e))
        self.assertIn("error", quotes["sh.600002"])
        self.assertEqual(events[-1][:3], (4, 2, 4))
        bs.login.return_value = SimpleNamespace(error_code="1", error_msg="offline")
        with patch.dict(sys.modules, {"baostock": bs}):
            quotes = fetch_holding_quotes(holdings, "2025-07-15", progress=lambda *e: events.append(e))
        self.assertEqual(len(quotes), 2)
        self.assertEqual(events[-1][:3], (4, 2, 4))

    def test_runner_writes_summary_and_finishes_only_after_files(self):
        import run_official_strategy as runner
        frame = sample_frame(100)
        provider = {"market_trend": "down", "benchmark": {"price_as_of": "2025-07-15"},
                    "member_codes": frame["ticker"].tolist(), "errors": {},
                    "requested_members": 100, "built_rows": 100}
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(sys, "argv", ["runner", "--as-of", "2025-07-15", "--output", folder]), \
                patch.object(runner, "build_a_metrics", return_value=(frame, provider)), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            runner.main()
            meta = json.loads((Path(folder) / "official_run_metadata.json").read_text(encoding="utf-8"))
            summary = (Path(folder) / "run_summary.txt").read_text(encoding="utf-8")
            self.assertEqual(summary, format_run_summary(meta))
            self.assertEqual(meta["account_guidance"]["positive_quantity_plans"], 0)
            for name in ("portfolio_plan.csv", "holding_review.csv", "account_guidance.csv"):
                self.assertTrue((Path(folder) / name).exists())
        events = [event for line in output.getvalue().splitlines() if (event := parse_progress(line))]
        self.assertEqual(events[-1]["stage"], 5)
        self.assertEqual(progress_percent(events[-1]), 100)
        self.assertTrue(any(e["stage"] == 4 and e["total"] == 2 for e in events))


class GuiFeedbackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = runpy.run_path(str(APP_DIR / "gui.pyw"))
        cls.gui_class = cls.module["StockSelectorGUI"]

    def make_gui(self):
        gui = self.gui_class.__new__(self.gui_class)
        for name in ("progress", "run_button", "cancel_button", "status_var", "_set_summary", "_append_log"):
            setattr(gui, name, Mock())
        gui.cancel_requested = False
        gui.running = True
        gui._load_results = Mock()
        return gui

    def test_success_failure_cancel_do_not_fabricate_completion(self):
        gui = self.make_gui()
        gui._finish_run(0)
        gui.progress.configure.assert_called_once_with(value=100)
        gui = self.make_gui()
        gui.cancel_requested = True
        gui._finish_run(-1)
        gui.progress.configure.assert_not_called()
        gui._load_results.assert_not_called()
        self.assertIn("结果不完整", gui._set_summary.call_args.args[0])
        for code, error in ((1, None), (0, ValueError("missing output"))):
            gui = self.make_gui()
            gui._load_results.side_effect = error
            with patch.object(self.module["messagebox"], "showerror"):
                gui._finish_run(code)
            gui.progress.configure.assert_not_called()
            self.assertIn("本次筛选未完成", gui._set_summary.call_args.args[0])

    def test_hidden_gui_real_widgets_progress_summary_and_reset(self):
        import tkinter as tk
        root = tk.Tk()
        root.withdraw()
        try:
            with tempfile.TemporaryDirectory() as folder:
                # Never load or update the user's real holdings in GUI tests.
                globals_ = self.gui_class.__init__.__globals__
                with patch.dict(globals_, {"HOLDINGS_DIR": Path(folder)}):
                    gui = self.gui_class(root)
                    self.assertEqual(str(gui.progress["mode"]), "determinate")
                    gui.running = True
                    gui.run_started = gui.last_progress_at = time.monotonic() - 40
                    gui.progress_message = "等待数据"
                    gui.events.put(("progress", {"stage": 2, "completed": 64,
                                                 "total": 300, "detail": "已读取缓存"}))
                    gui._poll_events()
                    self.assertAlmostEqual(float(gui.progress["value"]), 100 * 64 / 300)
                    self.assertIn("64/300", gui.status_var.get())
                    gui.last_progress_at -= 40
                    gui._poll_events()
                    self.assertIn("无新进度", gui.status_var.get())
                    self.assertAlmostEqual(float(gui.progress["value"]), 100 * 64 / 300)
                    gui._set_summary(format_run_summary(example_metadata()))
                    self.assertIn("24只通过Q/V/M", gui.summary_text.get("1.0", "end"))
                    gui._clear_results()
                    self.assertNotIn("24只通过Q/V/M", gui.summary_text.get("1.0", "end"))
                    self.assertIn("尚无本次结论", gui.summary_text.get("1.0", "end"))
                    gui.running = False
        finally:
            root.destroy()


if __name__ == "__main__":
    unittest.main()
