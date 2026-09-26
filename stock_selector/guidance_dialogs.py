"""Account settings and actual-fill entry for the desktop tool."""
import json
import uuid
from datetime import date
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox
from selector.account_guidance import validate_account
from selector.holdings_store import latest_holdings_file, load_holdings, save_holdings
from selector.trade_journal import record_fill, journal_rows, export_latest_holdings


def account_dialog(owner, path):
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    w = tk.Toplevel(owner.root)
    w.title("账户设置（人民币）")
    w.transient(owner.root)
    w.grab_set()
    fields = [("as_of", "余额核对日期", str(date.today())), ("equity", "总资产（元）", ""),
              ("cash", "可用现金（元）", ""), ("max_exposure", "总仓位上限（0–1）", ""),
              ("max_industry", "行业上限（0–1）", ""), ("cost_buffer", "费用及价格缓冲（0–1）", "0.005")]
    variables = {}
    for i, (key, label, default) in enumerate(fields):
        ttk.Label(w, text=label).grid(row=i, column=0, padx=12, pady=6, sticky="w")
        variables[key] = tk.StringVar(value=str(data.get(key, default)))
        ttk.Entry(w, textvariable=variables[key], width=24).grid(row=i, column=1, padx=12, pady=6)
    def save():
        try:
            settings = validate_account({k: v.get() for k, v in variables.items()})
            path.parent.mkdir(parents=True, exist_ok=True)
            temp = path.with_suffix(".json.tmp")
            temp.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
            temp.replace(path)
            w.destroy()
        except (ValueError, OSError) as exc:
            messagebox.showerror("账户设置未保存", str(exc), parent=w)
    ttk.Button(w, text="保存", command=save).grid(row=len(fields), column=1, padx=12, pady=12)


def trade_dialog(owner, holdings_dir, db_path, account_path):
    history = journal_rows(db_path)
    w = tk.Toplevel(owner.root)
    w.title("实际成交登记")
    w.transient(owner.root)
    w.grab_set()
    identity = uuid.uuid4().hex
    source = latest_holdings_file(holdings_dir)
    values = {}
    fields = [("ticker", "股票代码", ""), ("company", "公司", ""),
              ("trade_date", "成交日期", str(date.today())), ("side", "方向 BUY / SELL", "BUY"),
              ("price", "实际成交均价", ""), ("quantity", "成交股数", ""), ("fees", "本笔全部费用", "0"),
              ("plan_id", "对应计划编号（可选）", str(owner.output_dir or "")),
              ("note", "备注", "")]
    for i, (key, label, default) in enumerate(fields):
        ttk.Label(w, text=label).grid(row=i, column=0, padx=12, pady=4, sticky="w")
        values[key] = tk.StringVar(value=default)
        ttk.Entry(w, textvariable=values[key], width=35).grid(row=i, column=1, padx=12, pady=4)
    def save():
        try:
            if latest_holdings_file(holdings_dir) != source:
                raise ValueError("持仓已经更新，请关闭本窗口后重新登记")
            holdings = load_holdings(source) if source else []
            if account_path.exists():
                settings = json.loads(account_path.read_text(encoding="utf-8"))
                settings["as_of"] = "2000-01-01"
                temp = account_path.with_suffix(".json.tmp")
                temp.write_text(json.dumps(settings, ensure_ascii=False), encoding="utf-8")
                temp.replace(account_path)
            record_fill(db_path, {k: v.get().strip() for k, v in values.items()} | {"id": identity}, holdings)
            recovery_button.configure(state="normal")
            # Journal is the durable source if CSV export fails. Recovery does not insert another fill.
            path = export_latest_holdings(db_path, holdings_dir)
            owner.holdings_var.set(str(path))
            owner._append_log(f"成交已登记，持仓已更新：{path}；请重新核对账户余额。\n")
            w.destroy()
        except Exception as exc:
            messagebox.showerror("登记结果", f"{exc}\n如账本已写入但快照导出失败，可点击恢复持仓快照。", parent=w)
    def recover():
        try:
            path = export_latest_holdings(db_path, holdings_dir)
            owner.holdings_var.set(str(path))
            messagebox.showinfo("恢复完成", "已从最新成交恢复快照，请重新核对账户余额。", parent=w)
            w.destroy()
        except Exception as exc:
            messagebox.showerror("恢复失败", str(exc), parent=w)
    ttk.Button(w, text="登记成交并更新持仓", command=save).grid(row=9, column=1, pady=10)
    recovery_button = ttk.Button(w, text="恢复持仓快照", command=recover, state="disabled")
    recovery_button.grid(row=9, column=0, pady=10)
    text = tk.Text(w, height=8, width=75)
    text.grid(row=10, column=0, columnspan=2, padx=12, pady=8)
    for r in history[-20:]:
        text.insert("end", f"{r['trade_date']} {r['ticker']} {r['side']} {r['quantity']}股 @ {r['price']} 费用{r['fees']} 已实现盈亏{r['realized_pnl']:.2f}\n")
    text.configure(state="disabled")
