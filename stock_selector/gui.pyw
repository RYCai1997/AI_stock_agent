"""Chinese desktop interface for the official CSI 300 stock selector."""

from __future__ import annotations

import csv
from datetime import date, datetime
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk


ROOT_DIR = Path(__file__).resolve().parent.parent
APP_DIR = Path(__file__).resolve().parent
RUNNER = APP_DIR / "run_official_strategy.py"
DEFAULT_OUTPUT_ROOT = APP_DIR / "outputs" / "official"
DEFAULT_CACHE = APP_DIR / "outputs" / "official_provider_cache"
GUI_VERSION = "1.0.1"


def _percent(value: object, digits: int = 1) -> str:
    try:
        return f"{float(value) * 100:.{digits}f}%"
    except (TypeError, ValueError):
        return "—"


def _number(value: object, digits: int = 1) -> str:
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return "—"


def _runner_python() -> str:
    """Use console Python for the worker even when the GUI uses pythonw.exe."""
    executable = Path(sys.executable)
    if executable.name.lower() == "pythonw.exe":
        console_python = executable.with_name("python.exe")
        if console_python.exists():
            return str(console_python)
    return str(executable)


class StockSelectorGUI:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title(f"A股现货择时选股系统 · GUI {GUI_VERSION}")
        self.root.geometry("1120x760")
        self.root.minsize(940, 660)
        self.root.configure(bg="#f3f5f8")
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.process: subprocess.Popen[str] | None = None
        self.running = False
        self.cancel_requested = False
        self.output_dir: Path | None = None
        self._configure_style()
        self._build_page()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(100, self._poll_events)

    def _configure_style(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        style.configure("Page.TFrame", background="#f3f5f8")
        style.configure("Card.TFrame", background="#ffffff", relief="flat")
        style.configure("Title.TLabel", background="#f3f5f8", foreground="#172033", font=("Microsoft YaHei UI", 20, "bold"))
        style.configure("Subtitle.TLabel", background="#f3f5f8", foreground="#667085", font=("Microsoft YaHei UI", 9))
        style.configure("CardTitle.TLabel", background="#ffffff", foreground="#667085", font=("Microsoft YaHei UI", 9))
        style.configure("CardValue.TLabel", background="#ffffff", foreground="#172033", font=("Microsoft YaHei UI", 16, "bold"))
        style.configure("Primary.TButton", font=("Microsoft YaHei UI", 10, "bold"), padding=(18, 9))
        style.configure("TButton", font=("Microsoft YaHei UI", 9), padding=(10, 7))
        style.configure("TLabel", font=("Microsoft YaHei UI", 9))
        style.configure("Treeview", rowheight=29, font=("Microsoft YaHei UI", 9))
        style.configure("Treeview.Heading", font=("Microsoft YaHei UI", 9, "bold"))

    def _build_page(self) -> None:
        page = ttk.Frame(self.root, style="Page.TFrame", padding=(24, 18))
        page.pack(fill="both", expand=True)

        header = ttk.Frame(page, style="Page.TFrame")
        header.pack(fill="x")
        ttk.Label(header, text="A股现货择时选股系统", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            header,
            text="沪深300 · Quality / Value / Momentum · Top 5 × 6% · 仅生成计划，不会下单",
            style="Subtitle.TLabel",
        ).pack(anchor="w", pady=(3, 14))

        controls = ttk.Frame(page, style="Card.TFrame", padding=16)
        controls.pack(fill="x", pady=(0, 12))
        controls.columnconfigure(1, weight=1)
        controls.columnconfigure(4, weight=2)
        ttk.Label(controls, text="筛选日期").grid(row=0, column=0, sticky="w")
        self.date_var = tk.StringVar(value=str(date.today()))
        ttk.Entry(controls, textvariable=self.date_var, width=14).grid(row=0, column=1, sticky="w", padx=(8, 22))
        ttk.Label(controls, text="持仓文件（可选）").grid(row=0, column=2, sticky="w")
        self.holdings_var = tk.StringVar()
        ttk.Entry(controls, textvariable=self.holdings_var).grid(row=0, column=3, columnspan=2, sticky="ew", padx=8)
        ttk.Button(controls, text="选择…", command=self._choose_holdings).grid(row=0, column=5, padx=(0, 12))
        self.run_button = ttk.Button(controls, text="开始筛选", style="Primary.TButton", command=self._start_run)
        self.run_button.grid(row=0, column=6)
        self.cancel_button = ttk.Button(controls, text="停止", command=self._cancel_run, state="disabled")
        self.cancel_button.grid(row=0, column=7, padx=(8, 0))

        status_row = ttk.Frame(page, style="Page.TFrame")
        status_row.pack(fill="x", pady=(0, 12))
        self.card_values: dict[str, tk.StringVar] = {}
        cards = [
            ("market", "市场趋势", "尚未运行"),
            ("candidates", "可执行候选", "—"),
            ("ideas", "计划数量", "—"),
            ("exposure", "新增仓位", "—"),
            ("overheat", "过热检查", "—"),
        ]
        for index, (key, title, initial) in enumerate(cards):
            status_row.columnconfigure(index, weight=1)
            card = ttk.Frame(status_row, style="Card.TFrame", padding=(14, 10))
            card.grid(row=0, column=index, sticky="nsew", padx=(0 if index == 0 else 5, 0 if index == len(cards) - 1 else 5))
            ttk.Label(card, text=title, style="CardTitle.TLabel").pack(anchor="w")
            value = tk.StringVar(value=initial)
            ttk.Label(card, textvariable=value, style="CardValue.TLabel").pack(anchor="w", pady=(3, 0))
            self.card_values[key] = value

        action_row = ttk.Frame(page, style="Page.TFrame")
        action_row.pack(fill="x", pady=(0, 8))
        self.progress = ttk.Progressbar(action_row, mode="indeterminate")
        self.progress.pack(side="left", fill="x", expand=True)
        self.status_var = tk.StringVar(value="请选择日期后开始筛选。首次获取完整数据可能需要几分钟。")
        ttk.Label(action_row, textvariable=self.status_var, style="Subtitle.TLabel").pack(side="left", padx=12)
        self.open_candidates_button = ttk.Button(action_row, text="完整候选表", command=lambda: self._open_result("candidates.csv"), state="disabled")
        self.open_candidates_button.pack(side="right")
        self.open_folder_button = ttk.Button(action_row, text="打开结果目录", command=self._open_output, state="disabled")
        self.open_folder_button.pack(side="right", padx=(0, 8))

        notebook = ttk.Notebook(page)
        notebook.pack(fill="both", expand=True)
        plan_tab = ttk.Frame(notebook, padding=8)
        review_tab = ttk.Frame(notebook, padding=8)
        log_tab = ttk.Frame(notebook, padding=8)
        notebook.add(plan_tab, text="  新建仓计划  ")
        notebook.add(review_tab, text="  持仓复核  ")
        notebook.add(log_tab, text="  运行日志  ")
        self.plan_tree = self._make_tree(
            plan_tab,
            ["priority", "ticker", "company", "industry", "quality", "value", "momentum", "weight", "entry"],
            ["序号", "代码", "公司", "行业", "质量", "估值", "动量", "仓位", "入场状态"],
            [52, 92, 110, 170, 70, 70, 70, 65, 180],
        )
        self.review_tree = self._make_tree(
            review_tab,
            ["ticker", "company", "price", "stop", "selected", "action", "reason"],
            ["代码", "公司", "现价", "止损价", "仍在候选", "建议", "原因"],
            [100, 120, 80, 80, 90, 80, 310],
        )
        self.log_text = tk.Text(
            log_tab,
            wrap="word",
            relief="flat",
            bg="#101828",
            fg="#d0d5dd",
            insertbackground="#ffffff",
            font=("Consolas", 9),
        )
        self.log_text.pack(fill="both", expand=True)
        self.log_text.insert("end", "等待运行。\n")
        self.log_text.configure(state="disabled")

        ttk.Label(
            page,
            text="重要：页面只提供研究筛选和人工复核计划，不连接券商，也不会自动买卖。",
            style="Subtitle.TLabel",
        ).pack(anchor="w", pady=(10, 0))

    def _make_tree(
        self,
        parent: ttk.Frame,
        columns: list[str],
        headings: list[str],
        widths: list[int],
    ) -> ttk.Treeview:
        tree = ttk.Treeview(parent, columns=columns, show="headings")
        scrollbar = ttk.Scrollbar(parent, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scrollbar.set)
        tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        for column, heading, width in zip(columns, headings, widths):
            tree.heading(column, text=heading)
            tree.column(column, width=width, minwidth=50, anchor="center" if width < 100 else "w")
        return tree

    def _choose_holdings(self) -> None:
        selected = filedialog.askopenfilename(
            title="选择持仓CSV",
            filetypes=[("CSV文件", "*.csv"), ("所有文件", "*.*")],
            initialdir=str(APP_DIR / "examples"),
        )
        if selected:
            self.holdings_var.set(selected)

    def _validated_date(self) -> str | None:
        value = self.date_var.get().strip()
        try:
            parsed = datetime.strptime(value, "%Y-%m-%d").date()
        except ValueError:
            messagebox.showerror("日期格式错误", "请输入YYYY-MM-DD格式，例如2025-07-15。")
            return None
        if parsed > date.today():
            messagebox.showerror("日期不可用", "筛选日期不能晚于今天。")
            return None
        return value

    def _start_run(self) -> None:
        if self.running:
            return
        as_of = self._validated_date()
        if as_of is None:
            return
        holdings_text = self.holdings_var.get().strip().strip('"')
        holdings = Path(holdings_text) if holdings_text else None
        if holdings and (not holdings.exists() or not holdings.is_file()):
            messagebox.showerror("持仓文件不存在", "请选择一个有效的CSV文件。")
            return
        self.output_dir = DEFAULT_OUTPUT_ROOT / as_of
        command = [
            _runner_python(),
            "-X",
            "utf8",
            str(RUNNER),
            "--as-of",
            as_of,
            "--cache-dir",
            str(DEFAULT_CACHE),
            "--output",
            str(self.output_dir),
        ]
        if holdings:
            command.extend(["--holdings", str(holdings.resolve())])
        self._clear_results()
        self.running = True
        self.cancel_requested = False
        self._append_log(f"开始运行：{as_of}\n")
        self.status_var.set("正在获取并计算数据，请稍候…")
        self.run_button.configure(state="disabled")
        self.cancel_button.configure(state="normal")
        self.progress.start(12)
        threading.Thread(target=self._run_process, args=(command,), daemon=True).start()

    def _run_process(self, command: list[str]) -> None:
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            self.process = subprocess.Popen(
                command,
                cwd=ROOT_DIR,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=creation_flags,
            )
            if self.cancel_requested:
                self.process.terminate()
            assert self.process.stdout is not None
            for line in self.process.stdout:
                self.events.put(("log", line))
            return_code = self.process.wait()
            self.events.put(("done", return_code))
        except Exception as exc:
            self.events.put(("error", str(exc)))

    def _poll_events(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "log":
                    self._append_log(str(payload))
                elif kind == "done":
                    self._finish_run(int(payload))
                elif kind == "error":
                    self._finish_run(-1, str(payload))
        except queue.Empty:
            pass
        self.root.after(100, self._poll_events)

    def _finish_run(self, return_code: int, error: str | None = None) -> None:
        self.process = None
        self.running = False
        self.progress.stop()
        self.run_button.configure(state="normal")
        self.cancel_button.configure(state="disabled")
        if self.cancel_requested:
            self.cancel_requested = False
            self.status_var.set("筛选已停止，未生成任何订单。")
            return
        if return_code == 0:
            try:
                self._load_results()
                self.status_var.set("筛选完成。请逐项人工复核后再决定是否执行。")
                self._append_log("\n运行完成。未生成任何订单。\n")
                return
            except Exception as exc:
                error = f"结果读取失败：{exc}"
        message = error or f"运行失败，退出代码：{return_code}"
        self.status_var.set("运行失败，请查看日志。")
        self._append_log(f"\n{message}\n")
        messagebox.showerror("运行失败", message)

    def _load_results(self) -> None:
        if self.output_dir is None:
            raise ValueError("输出目录未设置")
        metadata_path = self.output_dir / "official_run_metadata.json"
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        trend = metadata["provider"]["market_trend"]
        trend_labels = {"up": "上涨", "down": "下跌", "unknown": "未知"}
        plan_meta = metadata["plan"]
        selector = metadata["selector"]
        self.card_values["market"].set(trend_labels.get(trend, trend))
        self.card_values["candidates"].set(str(selector["counts"]["actionable_candidates"]))
        self.card_values["ideas"].set(str(plan_meta["ideas"]))
        self.card_values["exposure"].set(_percent(plan_meta["planned_new_exposure"], 0))
        self.card_values["overheat"].set(
            f"等待{plan_meta['entry_delay_sessions']}日" if plan_meta["overheated"] else "正常"
        )
        self._fill_plan(self.output_dir / "portfolio_plan.csv")
        self._fill_review(self.output_dir / "holding_review.csv")
        self.open_folder_button.configure(state="normal")
        self.open_candidates_button.configure(state="normal")

    def _fill_plan(self, path: Path) -> None:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        for row in rows:
            entry = "等待5个交易日" if row["entry_delay_sessions"] == "5" else "人工批准后可执行"
            self.plan_tree.insert("", "end", values=(
                row["priority"], row["ticker"], row["company"], row["industry_l1"],
                _number(row["quality_score"]), _number(row["value_score"]),
                _number(row["momentum_score"]), _percent(row["target_fraction"], 0), entry,
            ))

    def _fill_review(self, path: Path) -> None:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        action_labels = {"hold": "继续持有", "exit": "退出复核", "review": "人工检查"}
        reason_labels = {
            "left CSI 300 universe": "已离开沪深300成分池",
            "review data unavailable": "复核数据不可用",
            "security not eligible": "证券状态不再合格",
            "model no longer supported": "当前模型不再适用",
            "market and stock below EMA200": "大盘与个股均跌破EMA200",
            "selection and EMA200 break confirmed": "失去筛选资格且EMA200破位已确认",
            "still qualified": "仍符合持有条件",
            "qualification warning; trend break not confirmed": "筛选资格预警，但趋势破位尚未确认",
        }
        for row in rows:
            selected = "是" if row.get("currently_selected", "").lower() == "true" else "否"
            self.review_tree.insert("", "end", values=(
                row["ticker"], row["company"], _number(row["current_price"], 2),
                _number(row["stop_loss_price"], 2), selected,
                action_labels.get(row["action"], row["action"]),
                reason_labels.get(row["reason"], row["reason"]),
            ))

    def _clear_results(self) -> None:
        for tree in (self.plan_tree, self.review_tree):
            tree.delete(*tree.get_children())
        for key, value in self.card_values.items():
            value.set("运行中" if key == "market" else "—")
        self.open_folder_button.configure(state="disabled")
        self.open_candidates_button.configure(state="disabled")

    def _append_log(self, text: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text)
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _cancel_run(self) -> None:
        if self.running and messagebox.askyesno("停止运行", "确定停止当前筛选吗？"):
            self.cancel_requested = True
            if self.process is not None:
                self.process.terminate()
            self._append_log("\n用户请求停止运行。不会删除已有结果。\n")

    def _open_output(self) -> None:
        if self.output_dir and self.output_dir.exists():
            os.startfile(self.output_dir)  # type: ignore[attr-defined]

    def _open_result(self, filename: str) -> None:
        if self.output_dir:
            path = self.output_dir / filename
            if path.exists():
                os.startfile(path)  # type: ignore[attr-defined]

    def _on_close(self) -> None:
        if self.running:
            if not messagebox.askyesno("退出", "筛选仍在运行，确定停止并退出吗？"):
                return
            self.cancel_requested = True
            if self.process is not None:
                self.process.terminate()
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    StockSelectorGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
