"""Chinese desktop interface for the official CSI 300 stock selector."""

from __future__ import annotations

import csv
from datetime import date, datetime
import json
import math
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from guidance_dialogs import account_dialog, trade_dialog
from selector.run_feedback import format_run_summary, parse_progress, progress_label, progress_percent

from selector.holdings_store import (
    latest_holdings_file,
    load_holdings,
    normalize_ticker,
    reviewed_snapshot,
    save_holdings,
    validate_holdings,
)


ROOT_DIR = Path(__file__).resolve().parent.parent
APP_DIR = Path(__file__).resolve().parent
RUNNER = APP_DIR / "run_official_strategy.py"
DEFAULT_OUTPUT_ROOT = APP_DIR / "outputs" / "official"
DEFAULT_CACHE = APP_DIR / "outputs" / "official_provider_cache"
HOLDINGS_DIR = APP_DIR / "user_data" / "holdings"
ACCOUNT_PATH = APP_DIR / "user_data" / "account.json"
JOURNAL_PATH = APP_DIR / "user_data" / "trades.sqlite3"
GUI_VERSION = "1.2.1"


def _percent(value: object, digits: int = 1) -> str:
    try:
        number = float(value)
        return f"{number * 100:.{digits}f}%" if math.isfinite(number) else "—"
    except (TypeError, ValueError):
        return "—"


def _number(value: object, digits: int = 1) -> str:
    try:
        number = float(value)
        return f"{number:.{digits}f}" if math.isfinite(number) else "—"
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


class HoldingsEditor:
    """Small editor for local holdings snapshots; it never places orders."""

    def __init__(self, owner: "StockSelectorGUI") -> None:
        self.owner = owner
        self.rows: dict[str, dict[str, object]] = {}
        self.selected_ticker: str | None = None
        self.window = tk.Toplevel(owner.root)
        self.window.title("当前持仓记录")
        self.window.geometry("900x590")
        self.window.minsize(780, 520)
        self.window.transient(owner.root)
        self.window.grab_set()
        self.load_failed = False
        self._build()
        self._load_current()

    def _build(self) -> None:
        page = ttk.Frame(self.window, padding=16)
        page.pack(fill="both", expand=True)
        ttk.Label(page, text="当前持仓", font=("Microsoft YaHei UI", 16, "bold")).pack(anchor="w")
        ttk.Label(
            page,
            text="记录实际买入均价和股数。保存后，下次筛选会自动使用最新记录进行退出复核。",
        ).pack(anchor="w", pady=(2, 12))

        form = ttk.Frame(page)
        form.pack(fill="x", pady=(0, 10))
        self.form_vars = {
            "ticker": tk.StringVar(), "company": tk.StringVar(),
            "entry_date": tk.StringVar(value=self.owner.date_var.get()),
            "entry_price": tk.StringVar(), "quantity": tk.StringVar(),
            "cost_basis_date": tk.StringVar(),
        }
        fields = [
            ("股票代码", "ticker", 14), ("公司", "company", 18),
            ("买入日期", "entry_date", 13), ("买入均价", "entry_price", 11),
            ("持有股数", "quantity", 11),
            ("成本核对日期", "cost_basis_date", 13),
        ]
        for column, (label, key, width) in enumerate(fields):
            box = ttk.Frame(form)
            box.grid(row=0, column=column, sticky="ew", padx=(0, 8))
            form.columnconfigure(column, weight=1)
            ttk.Label(box, text=label).pack(anchor="w")
            ttk.Entry(box, textvariable=self.form_vars[key], width=width).pack(fill="x", pady=(3, 0))
        ttk.Button(form, text="新增／更新", command=self._upsert).grid(row=0, column=6, sticky="s", padx=(4, 0))

        table_frame = ttk.Frame(page)
        table_frame.pack(fill="both", expand=True)
        columns = ("ticker", "company", "date", "price", "quantity", "cost", "review", "state")
        headings = ("代码", "公司", "买入日期", "买入均价", "股数", "成本金额", "最近复核", "最近状态")
        widths = (100, 120, 100, 90, 80, 105, 100, 110)
        self.tree = ttk.Treeview(table_frame, columns=columns, show="headings")
        vertical = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        horizontal = ttk.Scrollbar(table_frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        table_frame.columnconfigure(0, weight=1)
        table_frame.rowconfigure(0, weight=1)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        for column, heading, width in zip(columns, headings, widths):
            self.tree.heading(column, text=heading)
            self.tree.column(column, width=width, minwidth=65, anchor="center")
        self.tree.bind("<<TreeviewSelect>>", self._select_row)

        actions = ttk.Frame(page)
        actions.pack(fill="x", pady=(10, 0))
        ttk.Button(actions, text="导入最近Top 5", command=self._import_plan).pack(side="left")
        ttk.Button(actions, text="删除选中", command=self._remove).pack(side="left", padx=8)
        ttk.Button(actions, text="清空输入", command=self._clear_form).pack(side="left")
        ttk.Button(actions, text="取消", command=self.window.destroy).pack(side="right")
        ttk.Button(actions, text="保存持仓记录", style="Primary.TButton", command=self._save).pack(side="right", padx=8)

    def _load_current(self) -> None:
        text = self.owner.holdings_var.get().strip().strip('"')
        path = Path(text) if text else latest_holdings_file(HOLDINGS_DIR)
        if path and path.exists():
            try:
                self.rows = {str(row["ticker"]): row for row in load_holdings(path)}
            except Exception as exc:
                self.load_failed = True
                messagebox.showerror("持仓读取失败", str(exc), parent=self.window)
        self._render()

    def _render(self) -> None:
        self.tree.delete(*self.tree.get_children())
        state_labels = {"hold": "继续持有", "exit": "退出复核", "review": "人工检查"}
        for ticker, row in self.rows.items():
            try:
                cost = float(row.get("entry_price", 0)) * float(row.get("quantity", 0))
                cost_text = f"{cost:,.2f}" if cost else "—"
            except (TypeError, ValueError):
                cost_text = "—"
            self.tree.insert("", "end", iid=ticker, values=(
                ticker, row.get("company", ""), row.get("entry_date", ""),
                row.get("entry_price", ""), row.get("quantity", "待填写"), cost_text,
                row.get("last_review_date", ""),
                state_labels.get(str(row.get("last_action", "")), row.get("last_action", "")),
            ))

    def _select_row(self, _event: object = None) -> None:
        selected = self.tree.selection()
        if not selected:
            return
        ticker = selected[0]
        self.selected_ticker = ticker
        row = self.rows[ticker]
        for key in self.form_vars:
            self.form_vars[key].set(str(row.get(key, "")))

    def _clear_form(self) -> None:
        self.selected_ticker = None
        for key, variable in self.form_vars.items():
            variable.set(self.owner.date_var.get() if key == "entry_date" else "")
        self.tree.selection_remove(*self.tree.selection())

    def _upsert(self) -> None:
        source = {key: variable.get().strip() for key, variable in self.form_vars.items()}
        try:
            normalized = normalize_ticker(source["ticker"])
            existing = self.rows.get(self.selected_ticker or normalized, {})
            if self.selected_ticker and normalized != self.selected_ticker:
                if normalized in self.rows:
                    raise ValueError("该代码已经存在，请选中原记录修改")
                existing = {}
            row = validate_holdings([{**existing, **source, "ticker": normalized}])[0]
        except ValueError as exc:
            messagebox.showerror("持仓输入错误", str(exc), parent=self.window)
            return
        ticker = str(row["ticker"])
        if self.selected_ticker and self.selected_ticker != ticker:
            self.rows.pop(self.selected_ticker, None)
        self.rows[ticker] = row
        self._clear_form()
        self._render()

    def _remove(self) -> None:
        selected = self.tree.selection()
        if not selected:
            return
        for ticker in selected:
            self.rows.pop(ticker, None)
        self._clear_form()
        self._render()

    def _latest_plan(self) -> Path | None:
        if self.owner.output_dir:
            current = self.owner.output_dir / "portfolio_plan.csv"
            if current.exists():
                return current
        plans = list((DEFAULT_OUTPUT_ROOT / "current").rglob("portfolio_plan.csv"))
        return max(plans, key=lambda path: path.stat().st_mtime_ns) if plans else None

    def _import_plan(self) -> None:
        path = self._latest_plan()
        if path is None:
            messagebox.showinfo("没有筛选计划", "请先完成一次筛选，再导入Top 5。", parent=self.window)
            return
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            plan = list(csv.DictReader(handle))
        imported = 0
        for source in plan:
            ticker = normalize_ticker(source["ticker"])
            if ticker in self.rows:
                continue
            self.rows[ticker] = {
                "ticker": ticker, "company": source.get("company", ""),
                "entry_date": str(date.today()), "entry_price": "",
                "quantity": "", "nonselected_streak": 0, "below_ema_streak": 0,
                "last_review_date": "", "last_action": "", "last_reason": "",
            }
            imported += 1
        self._render()
        messagebox.showinfo(
            "导入完成",
            f"已导入{imported}只。请逐只填写实际买入均价、日期和持有股数后保存。",
            parent=self.window,
        )

    def _save(self) -> None:
        if self.load_failed:
            messagebox.showerror("无法保存", "原持仓读取失败，请修复原文件后重试。", parent=self.window)
            return
        try:
            path = save_holdings(self.rows.values(), HOLDINGS_DIR)
        except (ValueError, OSError) as exc:
            messagebox.showerror("无法保存", str(exc), parent=self.window)
            return
        self.owner.holdings_var.set(str(path))
        self.owner._append_log(f"持仓记录已保存：{path}\n")
        messagebox.showinfo("保存成功", f"已保存{len(self.rows)}只持仓。\n下次运行会自动复核此记录。", parent=self.window)
        self.window.destroy()


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
        self._refresh_latest_holdings()
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
        ttk.Label(controls, text="持仓记录（自动最近）").grid(row=0, column=2, sticky="w")
        self.holdings_var = tk.StringVar()
        ttk.Entry(controls, textvariable=self.holdings_var).grid(row=0, column=3, columnspan=2, sticky="ew", padx=8)
        ttk.Button(controls, text="选择…", command=self._choose_holdings).grid(row=0, column=5, padx=(0, 12))
        ttk.Button(controls, text="当前持仓…", command=self._open_holdings_editor).grid(row=0, column=6, padx=(0, 12))
        self.run_button = ttk.Button(controls, text="开始筛选", style="Primary.TButton", command=self._start_run)
        self.run_button.grid(row=0, column=7)
        self.cancel_button = ttk.Button(controls, text="停止", command=self._cancel_run, state="disabled")
        self.cancel_button.grid(row=0, column=8, padx=(8, 0))
        self.mode_var = tk.StringVar(value="当前账户")
        ttk.Combobox(controls, textvariable=self.mode_var, values=["当前账户", "历史研究"],
                     state="readonly", width=12).grid(row=1, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Label(controls, text="历史研究不更新当前持仓；成本核对日期留空时以买入日为准。"
                  ).grid(row=1, column=2, columnspan=7, sticky="w", pady=(8, 0))
        ttk.Button(controls, text="账户设置", command=self._account_settings).grid(row=2, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Button(controls, text="登记实际成交", command=self._record_trade).grid(row=2, column=2, columnspan=2, sticky="w", pady=(8, 0))

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
        ttk.Label(action_row, text="当前步骤进度").pack(side="left", padx=(0, 8))
        self.progress = ttk.Progressbar(action_row, mode="determinate", maximum=100)
        self.progress.pack(side="left", fill="x", expand=True)
        self.status_var = tk.StringVar(value="请选择日期后开始筛选。首次获取完整数据可能需要几分钟。")
        ttk.Label(page, textvariable=self.status_var, style="Subtitle.TLabel",
                  wraplength=1100).pack(fill="x", pady=(0, 8))
        self.open_candidates_button = ttk.Button(action_row, text="完整候选表", command=lambda: self._open_result("candidates.csv"), state="disabled")
        self.open_candidates_button.pack(side="right")
        self.open_folder_button = ttk.Button(action_row, text="打开结果目录", command=self._open_output, state="disabled")
        self.open_folder_button.pack(side="right", padx=(0, 8))

        notebook = ttk.Notebook(page)
        self.notebook = notebook
        notebook.pack(fill="both", expand=True)
        plan_tab = ttk.Frame(notebook, padding=8)
        review_tab = ttk.Frame(notebook, padding=8)
        log_tab = ttk.Frame(notebook, padding=8)
        account_tab = ttk.Frame(notebook, padding=8)
        self.summary_tab = ttk.Frame(notebook, padding=8)
        notebook.add(self.summary_tab, text="  筛选汇报  ")
        self.summary_text = tk.Text(self.summary_tab, wrap="word", relief="flat",
                                    font=("Microsoft YaHei UI", 11), padx=12, pady=12)
        summary_scroll = ttk.Scrollbar(self.summary_tab, command=self.summary_text.yview)
        summary_scroll.pack(side="right", fill="y")
        self.summary_text.configure(yscrollcommand=summary_scroll.set)
        self.summary_text.pack(fill="both", expand=True)
        self._set_summary("运行结束后，这里会解释取数情况、筛选数量及没有建仓计划的原因。")
        notebook.add(account_tab, text="  今日交易指导  ")
        notebook.add(plan_tab, text="  新建仓计划  ")
        notebook.add(review_tab, text="  持仓复核  ")
        notebook.add(log_tab, text="  运行日志  ")
        self.guidance_tree = self._make_tree(account_tab,
            ["ticker", "company", "qty", "amount", "risk", "date", "reason"],
            ["代码", "公司", "建议股数", "参考金额", "止损风险估计", "数据节点", "操作依据"],
            [100, 120, 90, 100, 120, 110, 380])
        self.plan_tree = self._make_tree(
            plan_tab,
            ["priority", "ticker", "company", "industry", "quality", "value", "momentum", "weight", "entry"],
            ["序号", "代码", "公司", "行业", "质量", "估值", "动量", "仓位", "入场状态"],
            [52, 92, 110, 170, 70, 70, 70, 65, 180],
        )
        self.review_tree = self._make_tree(
            review_tab,
            ["ticker", "company", "quantity", "price", "pnl", "stop", "selected", "action", "timing", "reason"],
            ["代码", "公司", "股数", "现价", "浮动收益", "止损价", "仍在候选", "建议", "退出节点", "原因"],
            [100, 120, 70, 80, 85, 80, 90, 80, 190, 280],
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
        vertical = ttk.Scrollbar(parent, orient="vertical", command=tree.yview)
        horizontal = ttk.Scrollbar(parent, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(0, weight=1)
        tree.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
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

    def _refresh_latest_holdings(self) -> None:
        latest = latest_holdings_file(HOLDINGS_DIR)
        if latest is not None:
            self.holdings_var.set(str(latest))

    def _open_holdings_editor(self) -> None:
        if self.running:
            messagebox.showinfo("正在运行", "请等待本次复核完成后再编辑持仓。")
            return
        HoldingsEditor(self)

    def _account_settings(self) -> None:
        if not self.running:
            try:
                account_dialog(self, ACCOUNT_PATH)
            except (ValueError, OSError) as exc:
                messagebox.showerror("账户设置读取失败", str(exc))

    def _record_trade(self) -> None:
        if not self.running:
            try:
                trade_dialog(self, HOLDINGS_DIR, JOURNAL_PATH, ACCOUNT_PATH)
            except Exception as exc:
                messagebox.showerror("成交日志读取失败", str(exc))

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
        mode = "current" if self.mode_var.get() == "当前账户" else "research"
        if mode == "current" and as_of != str(date.today()):
            messagebox.showerror("请选择历史研究", "当前账户使用今天日期；回顾旧日期请选择历史研究。")
            return
        latest = latest_holdings_file(HOLDINGS_DIR)
        current_text = self.holdings_var.get().strip().strip('"')
        current = Path(current_text) if current_text else None
        if latest is not None and (
            current is None or current.parent.resolve() == HOLDINGS_DIR.resolve()
        ):
            self.holdings_var.set(str(latest))
        holdings_text = self.holdings_var.get().strip().strip('"')
        holdings = Path(holdings_text) if holdings_text else None
        if holdings and (not holdings.exists() or not holdings.is_file()):
            messagebox.showerror("持仓文件不存在", "请选择一个有效的CSV文件。")
            return
        if holdings:
            try:
                rows = load_holdings(holdings)
                if any(str(row["entry_date"]) > as_of or str(row.get("last_review_date", "")) > as_of for row in rows):
                    raise ValueError("这份持仓包含节点之后的交易或状态，请选择该节点当时的持仓快照。")
            except (ValueError, OSError) as exc:
                messagebox.showerror("持仓无法用于本次复核", str(exc))
                return
        self.run_holdings_source = holdings
        self.run_latest_source = latest_holdings_file(HOLDINGS_DIR)
        self.output_dir = DEFAULT_OUTPUT_ROOT / mode / as_of / datetime.now().strftime("%H%M%S_%f")
        command = [
            _runner_python(),
            "-u",
            "-X",
            "utf8",
            str(RUNNER),
            "--mode", mode,
            "--as-of",
            as_of,
            "--cache-dir",
            str(DEFAULT_CACHE),
            "--output",
            str(self.output_dir),
        ]
        if holdings:
            command.extend(["--holdings", str(holdings.resolve())])
        if mode == "current" and ACCOUNT_PATH.exists():
            command.extend(["--account", str(ACCOUNT_PATH)])
        self._clear_results()
        self.running = True
        self.cancel_requested = False
        self._append_log(f"开始运行：{as_of}\n")
        self.run_started = time.monotonic()
        self.last_progress_at = self.run_started
        self.progress_message = "正在启动筛选程序，等待实际进度…"
        self.status_var.set(self.progress_message)
        self.run_button.configure(state="disabled")
        self.cancel_button.configure(state="normal")
        self.progress.configure(value=0)
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
                event = parse_progress(line)
                self.events.put(("progress", event) if event else ("log", line))
            return_code = self.process.wait()
            self.events.put(("done", return_code))
        except Exception as exc:
            self.events.put(("error", str(exc)))

    def _poll_events(self) -> None:
        try:
            for _ in range(200):
                kind, payload = self.events.get_nowait()
                if kind == "log":
                    self._append_log(str(payload))
                elif kind == "progress":
                    self.progress.configure(value=progress_percent(payload))
                    self.progress_message = progress_label(payload)
                    self.last_progress_at = time.monotonic()
                    self._append_log(self.progress_message + "\n")
                elif kind == "done":
                    self._finish_run(int(payload))
                elif kind == "error":
                    self._finish_run(-1, str(payload))
        except queue.Empty:
            pass
        if self.running:
            now = time.monotonic()
            elapsed = int(now - self.run_started)
            quiet = int(now - self.last_progress_at)
            waiting = f" · 已{quiet}秒无新进度，正在等待数据／计算；可停止" if quiet >= 30 else ""
            self.status_var.set(f"{self.progress_message} · 已用时{elapsed // 60}分{elapsed % 60}秒{waiting}")
        self.root.after(100, self._poll_events)

    def _finish_run(self, return_code: int, error: str | None = None) -> None:
        self.process = None
        self.running = False
        self.run_button.configure(state="normal")
        self.cancel_button.configure(state="disabled")
        if self.cancel_requested:
            self.cancel_requested = False
            self.status_var.set("筛选已停止，未生成任何订单。")
            self._set_summary("本次筛选已停止，结果不完整，不能判断是否有合适标的。\n进度条保留停止时的实际步骤进度。")
            return
        if return_code == 0:
            try:
                self._load_results()
                self.progress.configure(value=100)
                self.status_var.set("筛选完成。请逐项人工复核后再决定是否执行。")
                self._append_log("\n运行完成。未生成任何订单。\n")
                return
            except Exception as exc:
                error = f"结果读取失败：{exc}"
        message = error or f"运行失败，退出代码：{return_code}"
        self.status_var.set("运行失败，请查看日志。")
        self._set_summary(f"本次筛选未完成：{message}\n不能把失败或空结果理解为没有投资机会。")
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
        guidance_path = self.output_dir / "account_guidance.csv"
        if guidance_path.exists():
            with guidance_path.open(encoding="utf-8-sig", newline="") as handle:
                for row in csv.DictReader(handle):
                    self.guidance_tree.insert("", "end", values=(row["ticker"], row["company"],
                        row["suggested_quantity"], _number(row["estimated_amount"], 2),
                        _number(row["estimated_stop_risk"], 2), row["valid_as_of"], row["guidance"]))
        holding_input = metadata.get("holding_review", {}).get("input")
        if (metadata.get("mode") == "current" and holding_input
                and metadata["holding_review"].get("holdings_reviewed", 0)
                and latest_holdings_file(HOLDINGS_DIR) == getattr(self, "run_latest_source", None)):
            updated = reviewed_snapshot(
                Path(holding_input), self.output_dir / "holding_review.csv", HOLDINGS_DIR, current_run=True
            )
            if updated is not None:
                self.holdings_var.set(str(updated))
                self._append_log(f"持仓复核状态已保存为最新记录：{updated}\n")
        self.open_folder_button.configure(state="normal")
        self.open_candidates_button.configure(state="normal")
        self._set_summary(format_run_summary(metadata))
        self.notebook.select(self.summary_tab)

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
            "latest close at or below stop loss threshold": "最新收盘价已达到-10%止损线",
            "still qualified": "仍符合持有条件",
            "price or selection data unavailable": "行情或筛选数据缺失／过期，暂不能判断",
            "corporate action: reconcile cost and quantity": "价格口径发生变化，请核对除权分红后的成本与股数",
            "monthly review already recorded": "本月已完成趋势复核",
            "qualification warning; trend break not confirmed": "筛选资格预警，但趋势破位尚未确认",
        }
        for row in rows:
            selected = "是" if row.get("currently_selected", "").lower() == "true" else "否"
            timing = (
                f"{row.get('signal_date', '')}信号；下一交易日开盘人工处理"
                if row.get("suggested_execution") == "NEXT_TRADABLE_OPEN"
                else ("等待恢复交易／核对可交易性" if row.get("suggested_execution") == "WAIT_FOR_TRADABILITY" else "—")
            )
            self.review_tree.insert("", "end", values=(
                row["ticker"], row["company"], _number(row.get("quantity"), 0),
                _number(row["current_price"], 2), _percent(row.get("unrealized_return"), 1),
                _number(row["stop_loss_price"], 2), selected,
                action_labels.get(row["action"], row["action"]),
                timing,
                reason_labels.get(row["reason"], row["reason"]),
            ))

    def _clear_results(self) -> None:
        self._set_summary("筛选正在进行中，尚无本次结论。完成后将自动显示结果汇报。")
        for tree in (self.plan_tree, self.review_tree, self.guidance_tree):
            tree.delete(*tree.get_children())
        for key, value in self.card_values.items():
            value.set("运行中" if key == "market" else "—")
        self.open_folder_button.configure(state="disabled")
        self.open_candidates_button.configure(state="disabled")

    def _set_summary(self, text: str) -> None:
        self.summary_text.configure(state="normal")
        self.summary_text.delete("1.0", "end")
        self.summary_text.insert("end", text)
        self.summary_text.configure(state="disabled")

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
