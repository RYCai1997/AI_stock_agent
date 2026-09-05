# -*- coding: utf-8 -*-
"""
launcher_gui.pyw — AI 量化交易 Agent 图形启动面板（双击即用）

用途：
    双击 Launch_Agent.bat（或本文件）即可打开中文图形控制台，
    设置标的/切片周期/回测范围/模型API后，一键启动任务并实时查看
    权益曲线（回测一次性绘制；实盘从设定起点预热后逐切片实时刷新）。

模式：
    [实盘循环]       按所选切片周期自动决策，长驻
    [单次实盘切片]   main.py --mode live --once
    [全量回测]       main.py --mode backtest
    [回测+绘图]      main.py --mode backtest --plot（额外输出 PNG）

技术要点：
    * 依赖 tkinter（Python 标准库，Anaconda 自带），无任何第三方 GUI 依赖；
    * 图表内嵌 matplotlib（FigureCanvasTkAgg，延迟导入，缺失时显示提示而不崩）；
    * 任务统一用同目录 python.exe + CREATE_NO_WINDOW 后台启动，绝无黑框弹出；
    * 子进程 stdout 逐行转发到界面日志框，同时落盘 runs/ 目录，可回溯；
    * 顶部状态卡每 2 秒自动刷新 state.json，实时看到资金/权益/持仓变化；
    * 曲线数据：回测写 runs/curve_backtest.csv，实盘写 runs/live_curve.csv
      （time,equity,price；买入持有线由面板以首行价为基准绘制）。
"""

import os
import sys
import csv
import json
import queue
import base64
import subprocess
import threading
import datetime
from pathlib import Path

# ---------------------------------------------------------------- 路径与解释器
BASE_DIR = Path(__file__).resolve().parent                # 项目根目录
MAIN_PY  = BASE_DIR / "main.py"                           # 主程序
STATE_JSON = BASE_DIR / "state.json"
RUNS_DIR = BASE_DIR / "runs"                              # 每次任务的日志副本目录
CURVE_BACKTEST = RUNS_DIR / "curve_backtest.csv"          # 回测曲线（time,equity,price）
CURVE_LIVE     = RUNS_DIR / "live_curve.csv"              # 实盘曲线（time,equity,price）

# ---------------------------------------------------------------- 解释器与 tkinter 可用性
# pythonw 启动时没有任何控制台——任何启动期异常都必须写入 runs/launcher_error.log，
# 否则双击会「无反应」且无从排查。tkinter 导入失败也在此兜底记录。
_TK_OK = True
_TK_ERR: Exception | None = None
try:
    import tkinter as tk
    from tkinter import messagebox, ttk
except Exception as e:                     # noqa: BLE001 —— 兜底记录后优雅退出
    tk = None
    messagebox = None
    ttk = None
    _TK_OK = False
    _TK_ERR = e


def _log_crash(phase: str, exc: Exception) -> None:
    """把启动/运行期异常写入 runs/launcher_error.log（pythonw 无控制台，这是唯一排查口）。"""
    try:
        RUNS_DIR.mkdir(exist_ok=True)
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(RUNS_DIR / "launcher_error.log", "a", encoding="utf-8") as f:
            f.write("[%s] %s 崩溃：%r\n" % (ts, phase, exc))
            try:
                import traceback
                f.write(traceback.format_exc() + "\n")
            except Exception:
                pass
    except Exception:
        pass

# GUI 由 pythonw 启动，但子任务必须用同目录 python.exe（pythonw 的 stdout 不可靠）
_EXE_DIR = Path(sys.executable).resolve().parent
TASK_PY = _EXE_DIR / "python.exe"
TASK_PYW = _EXE_DIR / "pythonw.exe"
if not TASK_PY.exists():                                  # 兜底：直接退回当前解释器
    TASK_PY = Path(sys.executable)

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # Windows: 子进程不弹黑框

# ---------------------------------------------------------------- 深色科技感配色
BG     = "#0e1320"   # 窗口背景
PANEL  = "#151d30"   # 面板底
CARD   = "#1b2438"   # 状态卡底
ACCENT = "#3b82f6"   # 主操作蓝
ACCENT_H = "#5b9bfa" # 主按钮 hover
OK     = "#10b981"   # 实盘绿（本面板非行情语境，绿=安全/成功）
WARN   = "#f59e0b"   # 警示橙
DANG   = "#ef4444"   # 危险/停止红
TXT    = "#e6ebf4"   # 主文字
DIM    = "#8b95ab"   # 次要文字
LOG_BG = "#0a0e18"   # 日志框背景
LOG_FG = "#c9d4e4"   # 日志文字

F_TITLE = ("Microsoft YaHei UI", 15, "bold")
F_SUB   = ("Microsoft YaHei UI", 9)
F_BTN   = ("Microsoft YaHei UI", 11, "bold")
F_BTN_S = ("Microsoft YaHei UI", 9)
F_MONO  = ("Consolas", 9)

MODES = [  # (按钮文字, 子进程参数, 主题色, 提示)
    ("实盘循环 · 每4小时",      ["--mode", "live"],              ACCENT, "长驻运行：每 4 小时拉行情→算指标→LLM/规则引擎决策→自动执行（可随时停止）"),
    ("单次实盘切片",            ["--mode", "live", "--once"],    OK,     "立即拉最新行情并执行一次完整决策后退出（验证链路用）"),
    ("全量回测",                ["--mode", "backtest"],          WARN,   "用 START_DATE 以来全部历史 K 线回放策略，打印成交与绩效统计"),
    ("回测 + 权益曲线图",       ["--mode", "backtest", "--plot"], DANG,   "同全量回测，额外生成 backtest_equity_chart.png 权益曲线图"),
]

# ---- 可选交易标的 / 切片周期 / LLM 服务商（面板下拉用，可自由输入其它）----
SYMBOLS = ["BTCUSDT", "ETHUSDT", "DOGEUSDT", "SOLUSDT"]
INTERVALS = ["15m", "30m", "1h", "2h", "4h", "6h", "8h", "12h", "1d"]

# LLM 面板：模型名与 API 端点均由用户自由指定（OpenAI 兼容 /chat/completions 协议）。
# 这里只提供常见预设作为下拉候选，Combobox 允许直接手输任意模型名 / 任意 Base URL。
LLM_MODEL_PRESETS = [
    "deepseek-chat",
    "deepseek-reasoner",
    "deepseek-ai/DeepSeek-V4-Flash",    # 硅基流动
    "deepseek-ai/DeepSeek-V4",          # 硅基流动
]
LLM_BASE_URL_PRESETS = [
    "https://api.deepseek.com/v1",
    "https://api.deepseek.com",
    "https://api.siliconflow.cn/v1",                       # 硅基流动
    "https://dashscope.aliyuncs.com/compatible-mode/v1",  # 通义千问
    "http://localhost:11434/v1",                          # 本地 Ollama
]
LLM_KEY_ENV = "LLM_API_KEY"      # 密钥写入该字段（config 会向后兼容 DEEPSEEK_API_KEY）
LLM_DEF_MODEL = "deepseek-chat"


def _interval_cn(interval: str) -> str:
    """'4h'->'4小时'；'30m'->'30分钟'；'1d'->'1天'（按钮文案/日志用）。"""
    interval = (interval or "4h").strip().lower()
    try:
        n = int(interval[:-1]); u = interval[-1]
    except ValueError:
        n, u = 4, "h"
    return {"m": f"{n}分钟", "h": f"{n}小时", "d": f"{n}天"}.get(u, f"{n}小时")


def check_deps():
    """检查运行 main.py 所需的三方依赖是否齐全；返回缺失列表（空=齐全）。

    覆盖链路：行情 requests / 指标 pandas,numpy / LLM 调 DeepSeek 走 requests /
    实盘调度 schedule / .env 解析 dotenv。matplotlib 仅 GUI 图表需要，
    缺失时图表自动降级（_ensure_chart），故不列入必装。
    """
    import importlib.util
    missing = []
    for name in ("requests", "pandas", "numpy", "schedule", "dotenv"):
        if importlib.util.find_spec(name) is None:
            missing.append(name)
    return missing


def read_state():
    """读取 state.json，容错返回 dict；文件不存在/损坏时返回空 dict。"""
    try:
        with open(STATE_JSON, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def read_env_brief():
    """读 .env 里与展示相关的键（不打印密钥本身，只打印是否配置）。"""
    keys = {}
    try:
        for line in (BASE_DIR / ".env").read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            keys[k.strip()] = v.strip().strip('"').strip("'")
    except Exception:
        pass
    return keys


def env_get_bool(key: str, default: bool = False) -> bool:
    """读 .env 中某个布尔键（缺失时返回 default）。"""
    return read_env_brief().get(key, "true" if default else "false").strip().lower() in ("1", "true", "yes")


def env_upsert(key: str, value: str) -> None:
    """把 key=value 写回 .env（已存在则原位替换，否则追加到文件尾）。

    仅改目标键那一行，其它行（含 API 密钥与中文注释）原样保留。
    """
    p = BASE_DIR / ".env"
    lines = p.read_text(encoding="utf-8").splitlines() if p.exists() else []
    target = key + "="
    idx = None
    for i, ln in enumerate(lines):
        if ln.strip().startswith(target):
            idx = i
            break
    if idx is None:
        lines.append(target + value)
    else:
        lines[idx] = target + value
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ================================================================ 主窗口
class LauncherApp:
    def __init__(self, root):
        self.root = root
        self.root.title("AI 量化交易 Agent · 控制台")
        self.root.geometry("1280x880")
        self.root.minsize(1120, 780)
        self.root.configure(bg=BG)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        # 运行状态
        self._proc = None            # 当前后台子进程（同一时刻只允许一个任务）
        self._busy = False
        self._closed = False
        self._task_kind = None       # "live" / "backtest" / "deps"
        self._q = queue.Queue()      # reader 线程 -> UI 主线程的日志行队列
        self._cur_log = None         # 本次任务日志副本文件句柄

        # 图表状态
        self._mpl_ok = False
        self._fig = None
        self._ax_top = None
        self._ax_bot = None
        self._canvas = None
        self._last_live_mtime = 0.0
        self._last_chart_kind = None  # "live" / "backtest" / None

        # 顶部状态卡变量（每 2s 刷新）
        self.v_capital = tk.StringVar(value="--")
        self.v_equity  = tk.StringVar(value="--")
        self.v_pos     = tk.StringVar(value="--")
        self.v_pnl     = tk.StringVar(value="--")
        self.v_daily   = tk.StringVar(value="--")
        self.v_provider = tk.StringVar(value="--")
        self.v_model    = tk.StringVar(value="--")
        self.v_key      = tk.StringVar(value="--")
        self.v_status   = tk.StringVar(value="就绪")

        self._build_ui()
        self._refresh_status()
        # 启动后延迟载入最近一次曲线（回测 csv 优先、其次实盘 csv），窗口先画出再导库
        self.root.after(200, self._manual_refresh_chart)
        self.root.after(120, self._drain)
        self.root.after(2000, self._status_tick)

    # ------------------------------------------------------------ 界面搭建
    def _build_ui(self):
        # 标题区
        head = tk.Frame(self.root, bg=BG)
        head.pack(fill="x", padx=18, pady=(14, 6))
        tk.Label(head, text="AI 量化交易 Agent", bg=BG, fg=TXT,
                 font=F_TITLE).pack(anchor="w")
        tk.Label(head, text="LLM 驱动 · 多标的 · 切片自动决策 · 纸张模拟盘（paper trading）",
                 bg=BG, fg=DIM, font=F_SUB).pack(anchor="w", pady=(2, 0))

        # ---- 顶部状态卡 ----
        card = tk.Frame(self.root, bg=CARD, highlightthickness=1,
                        highlightbackground="#27324d")
        card.pack(fill="x", padx=18, pady=8)
        grid = tk.Frame(card, bg=CARD)
        grid.pack(fill="x", padx=12, pady=10)
        grid.columnconfigure(tuple(range(6)), weight=1, uniform="c")
        cells = [
            ("初始资金", self.v_capital), ("当前权益", self.v_equity),
            ("持仓(BTC)", self.v_pos),    ("已实现盈亏", self.v_pnl),
            ("今日盈亏%", self.v_daily),  ("状态", self.v_status),
        ]
        for i, (title, var) in enumerate(cells):
            col = tk.Frame(grid, bg=CARD)
            col.grid(row=0, column=i, sticky="ew", padx=2)
            lbl = tk.Label(col, text=title, bg=CARD, fg=DIM, font=F_SUB)
            if title.startswith("持仓"):
                self._pos_title_lbl = lbl       # 供随所选标的动态更新币种名
            lbl.pack(anchor="w")
            tk.Label(col, textvariable=var, bg=CARD, fg=TXT,
                     font=("Microsoft YaHei UI", 11, "bold")).pack(anchor="w")
        info = tk.Frame(card, bg=CARD)
        info.pack(fill="x", padx=12, pady=(0, 8))
        tk.Label(info, text="LLM 供应商", bg=CARD, fg=DIM, font=F_SUB).pack(side="left")
        tk.Label(info, textvariable=self.v_provider, bg=CARD, fg=TXT, font=F_SUB).pack(side="left")
        tk.Label(info, text="  |  模型", bg=CARD, fg=DIM, font=F_SUB).pack(side="left")
        tk.Label(info, textvariable=self.v_model, bg=CARD, fg=TXT, font=F_SUB).pack(side="left")
        tk.Label(info, text="  |  API 密钥", bg=CARD, fg=DIM, font=F_SUB).pack(side="left")
        tk.Label(info, textvariable=self.v_key, bg=CARD, fg=TXT, font=F_SUB).pack(side="left")

        # ---- 依赖缺失警示条（默认隐藏）----
        self.warn_bar = tk.Frame(self.root, bg="#3a1f1f")
        self.warn_label = tk.Label(self.warn_bar, text="", bg="#3a1f1f",
                                   fg="#ffb4a2", font=F_SUB, anchor="w")
        self.warn_label.pack(side="left", fill="x", expand=True, padx=8, pady=4)

        # ---- 依赖缺失警示条（body 之前出现，整行醒目）----
        missing = self._refresh_deps_bar()
        if missing:
            tk.Button(self.warn_bar, text="安装依赖", command=self._install_deps,
                      bg=WARN, fg="#141a26", activebackground="#ffc04d",
                      activeforeground="#141a26", relief="flat", bd=0,
                      font=F_BTN_S, cursor="hand2").pack(side="right", padx=8, pady=3)

        # ================================================================
        # 主内容区：左栏=任务/参数/模型设置；右栏=图表+日志
        # ================================================================
        body = tk.Frame(self.root, bg=BG)
        body.pack(fill="both", expand=True, padx=18, pady=(2, 0))

        left = tk.Frame(body, bg=BG, width=430)
        left.pack(side="left", fill="y")
        left.pack_propagate(False)

        right = tk.Frame(body, bg=BG)
        right.pack(side="right", fill="both", expand=True)
        right.grid_columnconfigure(0, weight=1)
        right.grid_rowconfigure(0, weight=1)    # 图表行占满剩余高度
        right.grid_rowconfigure(1, weight=0)    # 日志行固定高度

        # ---- 左栏 ① 主模式按钮 ----
        tk.Label(left, text="运行任务", bg=BG, fg=DIM, font=F_SUB).pack(anchor="w", padx=2)
        btns = tk.Frame(left, bg=BG)
        btns.pack(fill="x", pady=(0, 2))
        btns.grid_columnconfigure(0, weight=1)
        btns.grid_columnconfigure(1, weight=1)
        self.mode_btns = []
        for i, (text, args, color, tip) in enumerate(MODES):
            r, c = divmod(i, 2)
            btn = tk.Button(btns, text=text, bg=color,
                            command=lambda a=args, t=text: self._launch(a, t),
                            fg="#ffffff", activebackground=color,
                            activeforeground="#ffffff", relief="flat", bd=0,
                            cursor="hand2")
            btn.grid(row=r, column=c, sticky="ew", padx=4, pady=4, ipady=9)
            btn.config(font=F_BTN)
            btn.bind("<Enter>", lambda e, tt=tip: self._set_status("提示：" + tt))
            btn.bind("<Leave>", lambda e: (self._set_status("空闲"),
                                           self._refresh_status()))
            self.mode_btns.append(btn)

        tk.Button(left, text="■ 停止当前任务", command=self._stop_task,
                  bg="#251a24", fg="#ff7d7d", activebackground="#331f2b",
                  activeforeground="#ff9c9c", relief="flat", bd=0,
                  font=F_BTN_S, cursor="hand2").pack(fill="x", padx=4, pady=(2, 8))

        # ---- 左栏 ② 运行参数 / ③ 模型设置 / ④ 交易选项 ----
        self._build_params_panel(left)
        self._build_model_panel(left)
        self._build_trade_panel(left)

        # ---- 左栏 ⑤ 工具行 ----
        tools = tk.Frame(left, bg=BG)
        tools.pack(fill="x", padx=2, pady=(4, 0))
        for text, cmd in [("回测报告", self._open_report), ("权益PNG", self._open_chart),
                          ("日志", self._open_log), ("装依赖", self._install_deps),
                          ("快捷方式", self._make_shortcut)]:
            tk.Button(tools, text=text, command=cmd,
                      bg="#1b2438", fg=TXT, activebackground="#263250",
                      activeforeground="#ffffff", relief="flat", bd=0,
                      font=F_BTN_S, cursor="hand2").pack(side="left", padx=2)

        # ---- 右栏 ① 内嵌图表 ----
        chart_panel = tk.Frame(right, bg=PANEL, highlightthickness=1,
                               highlightbackground="#27324d")
        chart_panel.grid(row=0, column=0, sticky="nsew", pady=(0, 6))
        chart_top = tk.Frame(chart_panel, bg=PANEL)
        chart_top.pack(fill="x", padx=10, pady=(6, 2))
        tk.Label(chart_top, text="策略图表", bg=PANEL, fg=TXT, font=F_BTN).pack(side="left")
        self.v_chart_hint = tk.StringVar(value="权益 / 买入持有 / 价格 —— 暂无数据")
        tk.Label(chart_top, textvariable=self.v_chart_hint, bg=PANEL, fg=DIM,
                 font=F_SUB).pack(side="left", padx=8)
        tk.Button(chart_top, text="导出PNG", command=self._export_chart_png,
                  bg="#1b2438", fg=TXT, activebackground="#263250",
                  activeforeground="#ffffff", relief="flat", bd=0,
                  font=F_BTN_S, cursor="hand2").pack(side="right")
        tk.Button(chart_top, text="刷新图表", command=self._manual_refresh_chart,
                  bg="#1b2438", fg=TXT, activebackground="#263250",
                  activeforeground="#ffffff", relief="flat", bd=0,
                  font=F_BTN_S, cursor="hand2").pack(side="right", padx=4)
        self.chart_host = tk.Frame(chart_panel, bg="#0a0e18")
        self.chart_host.pack(fill="both", expand=True, padx=6, pady=(0, 6))
        self.chart_placeholder = tk.Label(
            self.chart_host,
            text="运行「全量回测」→ 自动绘制 权益 / 买入持有 / 价格 三线图\n"
                 "运行「实盘循环」并设置实盘起点 → 预热补齐历史曲线，此后每来一个新切片实时刷新",
            bg="#0a0e18", fg="#5a6478", font=("Microsoft YaHei UI", 10), justify="left")
        self.chart_placeholder.pack(expand=True)

        # ---- 右栏 ② 运行日志 ----
        log_wrap = tk.Frame(right, bg=BG)
        log_wrap.grid(row=1, column=0, sticky="nsew")
        log_lab = tk.Frame(log_wrap, bg=BG)
        log_lab.pack(fill="x", pady=(0, 2))
        tk.Label(log_lab, text="运行日志", bg=BG, fg=DIM,
                 font=F_SUB).pack(side="left")
        tk.Label(log_lab, text="（后台运行，可随时停止；副本存于 runs/ 目录）",
                 bg=BG, fg="#5a6478", font=F_SUB).pack(side="right")
        log_box = tk.Frame(log_wrap, bg=LOG_BG, height=170)
        log_box.pack(fill="both")
        log_box.pack_propagate(False)
        self._text = tk.Text(log_box, bg=LOG_BG, fg=LOG_FG, font=F_MONO,
                             wrap="word", relief="flat", bd=0,
                             insertbackground=LOG_FG, selectbackground="#2b3a55")
        sb = tk.Scrollbar(log_box, command=self._text.yview, bg=PANEL,
                          troughcolor=LOG_BG)
        self._text.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self._text.pack(side="left", fill="both", expand=True)
        self._text.tag_configure("time", foreground="#5a6478")

        # ---- 底部状态栏 ----
        bar = tk.Frame(self.root, bg="#0b101a")
        bar.pack(fill="x", side="bottom")
        self._py_label = tk.Label(bar, bg="#0b101a", fg=DIM, font=F_SUB,
                                  anchor="w")
        self._py_label.pack(fill="x", padx=18, pady=4)
        self._py_label.config(text="任务解释器: %s" % TASK_PY)

        self._refresh_mode_label()
        self._log("启动器就绪。Python = %s" % sys.executable, show=True)

    # ------------------------------------------------------------ 通用小组件
    @staticmethod
    def _sec(parent, title):
        """带标题的深色面板容器。"""
        f = tk.Frame(parent, bg=PANEL, highlightthickness=1,
                     highlightbackground="#27324d")
        f.pack(fill="x", pady=(0, 8))
        tk.Label(f, text=title, bg=PANEL, fg=DIM,
                 font=F_SUB).pack(anchor="w", padx=10, pady=(6, 0))
        return f

    @staticmethod
    def _dark_entry(parent, width=20, show=None):
        return tk.Entry(parent, bg="#0a0e18", fg=TXT, insertbackground=TXT,
                        relief="flat", bd=0, highlightthickness=1,
                        highlightbackground="#2b3a55", highlightcolor="#3b82f6",
                        font=("Consolas", 9), width=width, show=show)

    @staticmethod
    def _combobox(parent, values, cur, width=12, readonly=False):
        cb = ttk.Combobox(parent, values=values, width=width,
                          state="readonly" if readonly else "normal")
        try:
            cb.set(cur)
        except Exception:
            pass
        return cb

    # ------------------------------------------------------------ 运行参数面板
    def _build_params_panel(self, parent):
        env = read_env_brief()
        f = self._sec(parent, "运行参数 · 标的 / 切片 / 回测范围 / 实盘起点")
        box = tk.Frame(f, bg=PANEL)
        box.pack(fill="x", padx=10, pady=(2, 8))

        r1 = tk.Frame(box, bg=PANEL); r1.pack(fill="x", pady=2)
        tk.Label(r1, text="标的", bg=PANEL, fg=DIM, font=F_SUB).pack(side="left")
        self.cb_symbol = self._combobox(r1, SYMBOLS, env.get("SYMBOL", "BTCUSDT").upper())
        self.cb_symbol.pack(side="left", padx=(4, 12))
        tk.Label(r1, text="切片间隔", bg=PANEL, fg=DIM, font=F_SUB).pack(side="left")
        self.cb_interval = self._combobox(r1, INTERVALS, env.get("INTERVAL", "4h").lower())
        self.cb_interval.pack(side="left", padx=(4, 0))

        r2 = tk.Frame(box, bg=PANEL); r2.pack(fill="x", pady=2)
        tk.Label(r2, text="回测起点", bg=PANEL, fg=DIM, font=F_SUB).pack(side="left")
        self.e_bt_start = self._dark_entry(r2, 18)
        self.e_bt_start.pack(side="left", padx=(4, 6))
        self.e_bt_start.insert(0, env.get("START_DATE", "2025-08-10"))
        tk.Label(r2, text="截止", bg=PANEL, fg=DIM, font=F_SUB).pack(side="left")
        self.e_bt_end = self._dark_entry(r2, 12)
        self.e_bt_end.pack(side="left", padx=(4, 0))
        self.e_bt_end.insert(0, env.get("BACKTEST_END", ""))

        r3 = tk.Frame(box, bg=PANEL); r3.pack(fill="x", pady=2)
        tk.Label(r3, text="实盘起点", bg=PANEL, fg=DIM, font=F_SUB).pack(side="left")
        self.e_live_start = self._dark_entry(r3, 18)
        self.e_live_start.pack(side="left", padx=(4, 0))
        self.e_live_start.insert(0, env.get("LIVE_START", ""))
        tk.Label(r3, text="(回测≤%s根自动截取最近段)" % env.get("MAX_BACKTEST_BARS", "1000"),
                 bg=PANEL, fg="#5a6478", font=("Microsoft YaHei UI", 8)).pack(side="left", padx=6)

        tk.Button(box, text="应用运行参数", command=self._apply_params,
                  bg="#1b2438", fg="#9db4e8", activebackground="#263250",
                  activeforeground="#ffffff", relief="flat", bd=0,
                  font=F_BTN_S, cursor="hand2").pack(anchor="w", pady=(2, 0))

    def _build_model_panel(self, parent):
        env = read_env_brief()
        f = self._sec(parent, "模型 / API · 模型名 · 端点 · 密钥（均可自由指定）")
        box = tk.Frame(f, bg=PANEL)
        box.pack(fill="x", padx=10, pady=(2, 8))

        r0 = tk.Frame(box, bg=PANEL); r0.pack(fill="x", pady=2)
        tk.Label(r0, text="模型名", bg=PANEL, fg=DIM, font=F_SUB).pack(side="left")
        self.cb_model = ttk.Combobox(r0, width=28, state="normal")
        self.cb_model.pack(side="left", padx=(4, 10))
        self._fill_model_fields(env)

        r1 = tk.Frame(box, bg=PANEL); r1.pack(fill="x", pady=2)
        tk.Label(r1, text="API 端点", bg=PANEL, fg=DIM, font=F_SUB).pack(side="left")
        self.e_base_url = ttk.Combobox(r1, width=52, state="normal")
        self.e_base_url.pack(side="left", padx=(4, 0))
        self.e_base_url["values"] = LLM_BASE_URL_PRESETS
        base_url = (env.get("LLM_BASE_URL") or env.get("DEEPSEEK_BASE_URL")
                    or LLM_BASE_URL_PRESETS[0]).strip()
        try:
            self.e_base_url.set(base_url)
        except Exception:
            pass

        r2 = tk.Frame(box, bg=PANEL); r2.pack(fill="x", pady=2)
        tk.Label(r2, text="API Key", bg=PANEL, fg=DIM, font=F_SUB).pack(side="left")
        self.e_key = self._dark_entry(r2, 34, show="*")
        self.e_key.pack(side="left", padx=(4, 4))
        self._key_show_var = tk.BooleanVar(value=False)

        def _toggle_key_show():
            self.e_key.config(show="" if self._key_show_var.get() else "*")
        tk.Checkbutton(r2, text="显示", variable=self._key_show_var, command=_toggle_key_show,
                       bg=PANEL, fg=DIM, activebackground=PANEL, activeforeground=TXT,
                       selectcolor="#1b2438", font=F_SUB, cursor="hand2").pack(side="left")

        bt = tk.Frame(box, bg=PANEL); bt.pack(fill="x", pady=(2, 0))
        tk.Button(bt, text="保存模型设置", command=self._save_model_settings,
                  bg="#1b2438", fg="#9db4e8", activebackground="#263250",
                  activeforeground="#ffffff", relief="flat", bd=0,
                  font=F_BTN_S, cursor="hand2").pack(side="left")
        self.v_llm_hint = tk.StringVar(value="")
        tk.Label(bt, textvariable=self.v_llm_hint, bg=PANEL, fg=DIM,
                 font=F_SUB).pack(side="left", padx=8)

    def _fill_model_fields(self, env=None):
        """填充模型名 / API 端点 / 密钥框初值（模型名与端点均支持自由输入）。"""
        env = env or read_env_brief()
        self.cb_model["values"] = LLM_MODEL_PRESETS
        model = (env.get("LLM_MODEL") or "").strip() or LLM_DEF_MODEL
        try:
            self.cb_model.set(model)
        except Exception:
            pass
        if hasattr(self, "e_key"):
            self.e_key.delete(0, "end")
            self.e_key.insert(0, env.get(LLM_KEY_ENV, "") or env.get("DEEPSEEK_API_KEY", ""))
            self.v_llm_hint.set("已配置" if (env.get(LLM_KEY_ENV) or env.get("DEEPSEEK_API_KEY")) else "未配置 → 运行走规则引擎")

    def _build_trade_panel(self, parent):
        f = self._sec(parent, "交易选项 · 做空 / 真实 LLM（默认均关）")
        box = tk.Frame(f, bg=PANEL)
        box.pack(fill="x", padx=10, pady=(2, 8))

        row = tk.Frame(box, bg=PANEL)
        row.pack(fill="x", pady=2)
        self._allow_short_var = tk.BooleanVar(value=env_get_bool("ALLOW_SHORT", False))
        self._short_hint_var = tk.StringVar(value="")
        tk.Checkbutton(row, text="允许做空", variable=self._allow_short_var,
                       command=self._on_toggle_short,
                       bg=PANEL, fg=TXT, activebackground=PANEL,
                       activeforeground=TXT, selectcolor="#1b2438",
                       font=("Microsoft YaHei UI", 10, "bold"),
                       cursor="hand2").pack(side="left")
        self._short_hint = tk.Label(row, textvariable=self._short_hint_var,
                                    bg=PANEL, fg=DIM, font=F_SUB, justify="left")
        self._short_hint.pack(side="left", padx=(8, 0))
        self._refresh_short_hint()

        row2 = tk.Frame(box, bg=PANEL)
        row2.pack(fill="x", pady=2)
        self._use_llm_var = tk.BooleanVar(value=env_get_bool("BACKTEST_USE_LLM", False))
        self._use_llm_hint_var = tk.StringVar(value="")
        tk.Checkbutton(row2, text="使用真实 LLM", variable=self._use_llm_var,
                       command=self._on_toggle_use_llm,
                       bg=PANEL, fg=TXT, activebackground=PANEL,
                       activeforeground=TXT, selectcolor="#1b2438",
                       font=("Microsoft YaHei UI", 10, "bold"),
                       cursor="hand2").pack(side="left")
        self._use_llm_hint = tk.Label(row2, textvariable=self._use_llm_hint_var,
                                      bg=PANEL, fg=DIM, font=F_SUB, justify="left")
        self._use_llm_hint.pack(side="left", padx=(8, 0))
        self._refresh_use_llm_hint()

        # 第三行：最大做多杠杆（1~10 倍，LLM 在 1~N 内自动选择）
        row3 = tk.Frame(box, bg=PANEL)
        row3.pack(fill="x", pady=2)
        tk.Label(row3, text="最大杠杆", bg=PANEL, fg=TXT,
                 font=("Microsoft YaHei UI", 10, "bold")).pack(side="left")
        self._leverage_var = tk.StringVar(value=self._read_leverage())
        self._leverage_cb = ttk.Combobox(row3, width=6, state="readonly",
                                         values=["1", "2", "3", "5", "10"],
                                         textvariable=self._leverage_var,
                                         font=F_SUB)
        self._leverage_cb.pack(side="left", padx=(8, 4))
        tk.Label(row3, text="倍（LLM 在 1~N 倍内自主选择；1=现货无杠杆）",
                 bg=PANEL, fg=DIM, font=F_SUB).pack(side="left")
        self._leverage_cb.bind("<<ComboboxSelected>>", self._on_change_leverage)

    # ------------------------------------------------------------ 设置落盘
    def _apply_params(self, silent: bool = False) -> bool:
        """把运行参数面板写入 .env（启动任务前自动调用）。"""
        try:
            sym = self.cb_symbol.get().strip().upper() or "BTCUSDT"
            interval = self.cb_interval.get().strip().lower() or "4h"
            env_upsert("SYMBOL", sym)
            env_upsert("INTERVAL", interval)
            env_upsert("START_DATE", self.e_bt_start.get().strip() or "2025-08-10")
            env_upsert("BACKTEST_END", self.e_bt_end.get().strip())
            env_upsert("LIVE_START", self.e_live_start.get().strip())
        except Exception as e:
            messagebox.showerror("写入失败", "应用运行参数失败：%s" % e)
            return False
        self._refresh_mode_label()
        self._log("---- 运行参数已应用：%s / %s / 回测 %s→%s / 实盘起点 %s ----"
                  % (sym, interval,
                     self.e_bt_start.get().strip() or "(默认)",
                     self.e_bt_end.get().strip() or "现在",
                     self.e_live_start.get().strip() or "(不回放)"))
        return True

    def _save_model_settings(self, silent: bool = False) -> bool:
        """把模型面板写入 .env（模型名 / API 端点 / 密钥；留空项不覆盖，避免误删已有配置）。"""
        try:
            env_upsert("LLM_PROVIDER", "deepseek")
            model = self.cb_model.get().strip()
            if model:
                env_upsert("LLM_MODEL", model)
            base_url = getattr(self, "e_base_url", None)
            if base_url is not None:
                bu = base_url.get().strip()
                if bu:
                    env_upsert("LLM_BASE_URL", bu)
            key = self.e_key.get().strip()
            if key:                                  # 空则不动，防止清掉手工配置的密钥
                env_upsert(LLM_KEY_ENV, key)
            self.v_llm_hint.set("已保存（%s）" % (model or LLM_DEF_MODEL))
        except Exception as e:
            messagebox.showerror("写入失败", "保存模型设置失败：%s" % e)
            return False
        self._log("---- 模型设置已保存：模型 %s / 端点 %s / %s ----"
                  % (model or LLM_DEF_MODEL,
                     (bu if 'bu' in dir() else "(未改)"),
                     "密钥已写" if key else "密钥未改动"))
        self._refresh_status()
        return True

    def _apply_all_settings(self) -> bool:
        """启动任务前统一落盘当前面板（保证子进程按界面设置运行）。"""
        if not self._apply_params(silent=True):
            return False
        return self._save_model_settings(silent=True)

    def _refresh_mode_label(self):
        """把第一个模式按钮的文案同步为当前切片周期；并把「持仓」标题改为所选币种。"""
        try:
            env = read_env_brief()
            interval = env.get("INTERVAL", "4h").strip().lower() or "4h"
            if self.mode_btns:
                self.mode_btns[0].config(text="实盘循环 · 每" + _interval_cn(interval))
            sym = (self.cb_symbol.get().strip().upper()
                   if hasattr(self, "cb_symbol")
                   else env.get("SYMBOL", "BTCUSDT").strip().upper())
            base = sym[:-4] if sym.endswith("USDT") and len(sym) > 4 else sym
            if self._pos_title_lbl is not None:
                self._pos_title_lbl.config(text="持仓(%s)" % base)
        except Exception:
            pass

    # ------------------------------------------------------------ 内嵌图表
    def _ensure_chart(self) -> bool:
        if self._mpl_ok:
            return True
        try:
            import matplotlib
            matplotlib.use("TkAgg")
            from matplotlib.figure import Figure
            from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
            matplotlib.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei"]
            matplotlib.rcParams["axes.unicode_minus"] = False
            self._Figure, self._FigureCanvasTkAgg = Figure, FigureCanvasTkAgg
            self._mpl_ok = True
            return True
        except Exception as e:                       # noqa: BLE001
            self._log("matplotlib 不可用，图表禁用：%s" % e, show=True)
            return False

    @staticmethod
    def _style_ax(ax):
        ax.set_facecolor("#0e1320")
        for s in ax.spines.values():
            s.set_color("#263250")
        ax.tick_params(colors="#8b95ab", labelsize=8)
        ax.grid(color="#1d2940", lw=0.5, alpha=0.8)

    @staticmethod
    def _load_curve_csv(path) -> list:
        """读取曲线 CSV → [(time, equity, price), ...]；空/缺失返回 []。"""
        rows = []
        try:
            if not path.exists():
                return []
            with open(path, "r", encoding="utf-8-sig", newline="") as f:
                rd = csv.reader(f)
                next(rd, None)          # 表头
                for line in rd:
                    if len(line) >= 3 and line[0].strip():
                        rows.append((line[0].strip(), line[1].strip(), line[2].strip()))
        except Exception:                # noqa: BLE001
            return []
        return rows

    def _plot_rows(self, rows, kind: str):
        """把 (time,equity,price) 绘制成 上:权益vs买入持有 / 下:价格 两联图。"""
        self._last_chart_kind = kind
        if not rows:
            self.v_chart_hint.set("暂无曲线数据——先运行回测或实盘任务")
            if self._canvas is not None:
                self._canvas.get_tk_widget().pack_forget()
            if not self.chart_placeholder.winfo_manager():
                self.chart_placeholder.pack(expand=True)
            return
        if not self._ensure_chart():
            return

        # 首次：创建 figure/canvas
        if self._canvas is None:
            try:
                w = max(self.chart_host.winfo_width(), 640)
                h = max(self.chart_host.winfo_height(), 360)
            except Exception:
                w, h = 720, 400
            self.chart_placeholder.pack_forget()
            self._fig = self._Figure(figsize=(w / 100.0, h / 100.0), dpi=100,
                                     facecolor="#0e1320")
            self._ax_top = self._fig.add_subplot(2, 1, 1)
            self._ax_bot = self._fig.add_subplot(2, 1, 2, sharex=self._ax_top)
            self._canvas = self._FigureCanvasTkAgg(self._fig, master=self.chart_host)
            # 画布背景与 figure 同色：即使某一帧尺寸尚未对齐，也绝不露出白色底块
            self._canvas.get_tk_widget().config(bg="#0e1320", highlightthickness=0)

            def _chart_align(_evt=None):
                """窗口尺寸变化时把 figure 像素尺寸对齐画布 widget，保证铺满无白边。"""
                try:
                    if self._fig is None or self._canvas is None:
                        return
                    w = self._canvas.get_tk_widget().winfo_width()
                    h = self._canvas.get_tk_widget().winfo_height()
                    if w > 80 and h > 80:
                        dpi = float(self._fig.dpi)
                        cw, ch = self._fig.get_size_inches()
                        if abs(cw * dpi - w) > 2 or abs(ch * dpi - h) > 2:
                            self._fig.set_size_inches(w / dpi, h / dpi, forward=False)
                            self._canvas.draw_idle()
                except Exception:                # noqa: BLE001
                    pass
            # add="+"：保留 TkAgg 内建 handler，避免替换掉其自身尺寸处理
            self._canvas.get_tk_widget().bind("<Configure>", _chart_align, add="+")
            self._canvas.get_tk_widget().pack(fill="both", expand=True)
        elif not self._canvas.get_tk_widget().winfo_manager():
            # 画布已存在但曾被「无数据」分支隐藏 → 重新铺回并收起占位文案
            self.chart_placeholder.pack_forget()
            self._canvas.get_tk_widget().pack(fill="both", expand=True)

        try:
            times = [datetime.datetime.strptime(t, "%Y-%m-%d %H:%M") for t, _, _ in rows]
        except ValueError:
            times = list(range(len(rows)))
        eq = [float(x) for _, x, _ in rows]
        px = [float(p) for _, _, p in rows]
        try:
            cap = float(read_env_brief().get("CAPITAL") or 100000.0)
        except (TypeError, ValueError):
            cap = 100000.0
        bh = [cap * v / px[0] for v in px] if px and px[0] else [cap] * len(px)

        a1, a2 = self._ax_top, self._ax_bot
        a1.clear(); a2.clear()
        self._style_ax(a1); self._style_ax(a2)
        lg = dict(facecolor="#151d30", edgecolor="#263250", labelcolor="#e6ebf4", fontsize=8)
        a1.plot(times, eq, color="#ff6b6b", lw=1.4, label="策略权益(USDT)")
        a1.plot(times, bh, color="#4fc3f7", lw=1.1, ls="--", alpha=0.9, label="买入持有(USDT)")
        a1.axhline(cap, color="#8b95ab", lw=0.7, ls=":", alpha=0.9)
        a1.legend(loc="upper left", ncol=2, **lg)
        a1.set_title("策略权益 vs 买入持有" if kind == "backtest"
                     else "实盘权益 vs 买入持有（起点预热 + 实时）",
                     color="#e6ebf4", fontsize=10)
        a1.tick_params(labelbottom=False)
        a2.plot(times, px, color="#ffd166", lw=1.1, label="收盘价(USDT)")
        a2.legend(loc="upper left", **lg)
        a2.set_xlabel("时间（北京时间）", color="#8b95ab", fontsize=8)
        try:
            from matplotlib.dates import AutoDateLocator
            a2.xaxis.set_major_locator(AutoDateLocator(maxticks=8))
        except Exception:
            pass
        try:
            self._fig.tight_layout()
        except Exception:
            pass
        # 重绘前把 figure 像素尺寸对齐画布 widget（单一尺寸来源，防白边/显示不全）
        try:
            w = self._canvas.get_tk_widget().winfo_width()
            h = self._canvas.get_tk_widget().winfo_height()
            if w > 80 and h > 80:
                dpi = float(self._fig.dpi)
                self._fig.set_size_inches(w / dpi, h / dpi, forward=False)
        except Exception:
            pass
        self._canvas.draw_idle()

        ret_s = (eq[-1] / cap - 1.0) * 100.0 if cap else 0.0
        bh_s = (px[-1] / px[0] - 1.0) * 100.0 if px[0] else 0.0
        self.v_chart_hint.set("%d 节点 | 策略 %+.2f%% | 买入持有 %+.2f%%"
                              % (len(eq), ret_s, bh_s))

    def _load_by_kind(self, kind: str) -> list:
        path = CURVE_LIVE if kind == "live" else CURVE_BACKTEST
        rows = self._load_curve_csv(path)
        self._plot_rows(rows, kind)
        return rows

    def _manual_refresh_chart(self):
        """手动刷新：实盘运行中优先刷 live，否则回测 csv（有哪个刷哪个）。"""
        if self._busy and self._task_kind == "live":
            rows = self._load_by_kind("live")
        elif CURVE_BACKTEST.exists():
            rows = self._load_by_kind("backtest")
        elif CURVE_LIVE.exists():
            rows = self._load_by_kind("live")
        else:
            self.v_chart_hint.set("暂无曲线文件——先运行回测或实盘任务")
            return
        if rows:
            self._log("---- 图表已刷新：%d 个节点（起点 %s）----" % (len(rows), rows[0][0]))

    def _export_chart_png(self):
        """把当前内嵌图表导出为 PNG 并打开。"""
        if self._fig is None:
            messagebox.showinfo("无图表", "当前没有可导出的图表。")
            return
        kind = self._last_chart_kind or "chart"
        out = RUNS_DIR / ("chart_%s.png" % kind)
        try:
            RUNS_DIR.mkdir(exist_ok=True)
            self._fig.savefig(out, dpi=130, facecolor="#0e1320", bbox_inches="tight")
        except Exception as e:
            messagebox.showerror("导出失败", "%s" % e)
            return
        self._log("---- 图表已导出：%s ----" % out)
        try:
            os.startfile(str(out))
        except Exception:
            pass

    def _maybe_live_refresh(self):
        """实盘运行中每 2s 轮询 live_curve.csv，有新增节点就实时重绘。"""
        if not (self._busy and self._task_kind == "live"):
            return
        try:
            m = CURVE_LIVE.stat().st_mtime if CURVE_LIVE.exists() else 0.0
        except OSError:
            m = 0.0
        if m and m != self._last_live_mtime:
            self._last_live_mtime = m
            self._load_by_kind("live")

    def _btn(self, parent, text, color, cmd):
        """统一的扁平主按钮。"""
        return tk.Button(parent, text=text, command=cmd, bg=color,
                         fg="#ffffff", activebackground=color,
                         activeforeground="#ffffff", relief="flat", bd=0,
                         cursor="hand2")

    # ------------------------------------------------------------ 日志
    def _log(self, line, show=False):
        """向日志框追加一行（带时间戳）；行数超限自动裁剪，防止长驻运行卡死界面。"""
        if self._closed:
            return
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        try:
            self._text.insert("end", "%-8s" % ts, "time")
            self._text.insert("end", line + "\n")
            # 行数上限：超过 1200 行则删除最早 400 行
            n = int(self._text.index("end-1c").split(".")[0])
            if n > 1200:
                self._text.delete("1.0", "401.0")
            if show:
                self._text.see("end")
        except tk.TclError:
            pass

    def _drain(self):
        """定时把 reader 线程队列里的日志行灌进界面。"""
        try:
            while True:
                item = self._q.get_nowait()
                if item is None:            # 哨兵：子进程管道已 EOF
                    self._finish_task()
                else:
                    self._log(item)
                    if self._text.yview()[1] > 0.98:   # 在底部才自动跟随
                        self._text.see("end")
        except queue.Empty:
            pass
        if not self._closed:
            self.root.after(120, self._drain)

    # ------------------------------------------------------------ 任务控制
    @staticmethod
    def _task_kind_of(args) -> str:
        """从子进程参数推断任务类型：'--mode' 的取值（live/backtest）。"""
        try:
            return args[args.index("--mode") + 1]
        except (ValueError, IndexError):
            return "backtest"

    def _task_title(self, args, fallback: str) -> str:
        """生成日志/状态栏里的任务标题（优先用面板当前切片周期，避免按钮文案过期）。"""
        kind = self._task_kind_of(args)
        if kind == "live":
            if "--once" in args:
                return "单次实盘切片"
            try:
                iv = self.cb_interval.get().strip().lower() or "4h"
            except Exception:
                iv = "4h"
            return "实盘循环 · 每" + _interval_cn(iv)
        if kind == "backtest":
            return "回测 + 权益曲线图" if "--plot" in args else "全量回测"
        return fallback

    def _launch(self, args, title):
        """后台启动一个任务：先落盘面板设置（含模型/密钥/参数），再启子进程。"""
        if self._busy:
            messagebox.showinfo("任务进行中", "已有任务在运行，请先停止当前任务。")
            return
        missing = check_deps()
        if missing:
            messagebox.showwarning("缺少依赖", "缺少: %s\n请先点击「安装依赖」或运行 install_deps.bat。"
                                   % ", ".join(missing))
            return
        # 面板 -> .env 落盘（子进程只读 .env，必须保证界面与任务一致）
        if not self._apply_all_settings():
            messagebox.showwarning("设置未保存",
                                   "面板设置写入 .env 失败，已取消启动。\n"
                                   "请检查项目目录下 .env 是否被占用或只读。")
            return
        self._task_kind = self._task_kind_of(args)
        disp = self._task_title(args, title)
        # 实盘启动：重置图表轮询基线，预热写满历史段后 _maybe_live_refresh 自动接续
        if self._task_kind == "live":
            self._last_live_mtime = 0.0
            self.v_chart_hint.set("实盘启动中 · 预热补齐历史曲线后实时刷新…")
        RUNS_DIR.mkdir(exist_ok=True)
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        safe = disp.replace("·", "_").replace("+", "p").replace(" ", "")
        log_path = RUNS_DIR / ("%s_%s.log" % (safe, ts))
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"   # 强制子进程按 UTF-8 输出，避免管道解码乱码
        env["PYTHONUTF8"] = "1"
        try:
            self._cur_log = open(log_path, "w", encoding="utf-8")
        except Exception:
            self._cur_log = None
        self._proc = subprocess.Popen(
            [str(TASK_PY), str(MAIN_PY)] + args,
            cwd=str(BASE_DIR), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            bufsize=1, creationflags=CREATE_NO_WINDOW,
        )
        self._busy = True
        self._set_status("运行中 · " + disp)
        self._set_buttons_state("disabled")
        self._log("==== 启动任务: %s  参数: python main.py %s ===="
                  % (disp, " ".join(args)), show=True)
        self._log("     日志副本: %s" % log_path)
        threading.Thread(target=self._reader, args=(self._proc,),
                         daemon=True).start()

    def _reader(self, proc):
        """子进程输出逐行转发到队列；EOF 后发哨兵。"""
        try:
            for line in proc.stdout:
                line = line.rstrip("\r\n")
                if line:
                    self._q.put(line)
        except Exception as e:
            self._q.put("[读取子进程输出失败] %s" % e)
        finally:
            try:
                if self._cur_log is not None:
                    self._cur_log.close()
            except Exception:
                pass
            self._q.put(None)

    def _finish_task(self):
        """子进程结束后的收尾：读取退出码、恢复按钮、自动载入本次曲线。"""
        if not self._busy:
            return
        rc = None
        try:
            if self._proc is not None:
                rc = self._proc.poll()
        except Exception:
            pass
        kind = self._task_kind
        self._busy = False
        self._proc = None
        self._set_buttons_state("normal")
        self._set_status("空闲")
        self._log("==== 任务结束（退出码 %s）==== 可启动新任务" % rc, show=True)
        # 任务结束自动把结果画进右侧图表：回测→curve_backtest.csv；实盘→live_curve.csv
        try:
            if kind == "deps":
                self._refresh_deps_bar()
            elif kind == "backtest":
                rows = self._load_by_kind("backtest")
                if rows:
                    self._log("---- 回测曲线已载入图表：%d 个节点（起点 %s）----"
                              % (len(rows), rows[0][0]), show=True)
            elif kind == "live":
                rows = self._load_by_kind("live")
                if rows:
                    self._log("---- 实盘曲线已载入图表：%d 个节点（起点 %s）----"
                              % (len(rows), rows[0][0]), show=True)
                else:
                    self.v_chart_hint.set("本次实盘未产生曲线节点——若需历史曲线，请在左栏设置「实盘起点」再跑实盘循环")
        except Exception:                # noqa: BLE001
            pass
        self._task_kind = None

    def _stop_task(self):
        """停止当前任务：先温和 terminate，1.5 秒后仍存活则强制 kill。"""
        if not self._busy or self._proc is None:
            messagebox.showinfo("无运行任务", "当前没有正在运行的任务。")
            return
        if not messagebox.askyesno("停止任务", "确定要停止当前运行的任务吗？"):
            return
        self._log("---- 收到停止指令，正在终止子进程 ... ----")
        try:
            self._proc.terminate()
        except Exception:
            pass

        def _hard_kill():
            try:
                if self._proc is not None and self._proc.poll() is None:
                    self._proc.kill()
                    self._log("---- 进程未在 1.5s 内退出，已强制结束 ----")
            except Exception:
                pass

        threading.Timer(1.5, _hard_kill).start()

    def _refresh_deps_bar(self):
        """按当前环境重查依赖：仍缺则更新警示条内容，齐了则收起整条。"""
        missing = check_deps()
        try:
            if missing:
                if not self.warn_bar.winfo_manager():
                    self.warn_bar.pack(fill="x", padx=18, pady=2)
                self.warn_label.config(
                    text="⚠ 缺少依赖: %s —— 点左栏「装依赖」或先运行 install_deps.bat"
                         % ", ".join(missing))
            else:
                self.warn_bar.pack_forget()
        except Exception:                # noqa: BLE001
            pass
        return missing

    # ------------------------------------------------------------ 状态刷新
    def _status_tick(self):
        if self._closed:
            return
        self._maybe_live_refresh()     # 实盘运行中每 2s 轮询 live_curve.csv，有新增即重绘
        self._refresh_status()
        self.root.after(2000, self._status_tick)

    def _refresh_status(self):
        """读 state.json + .env，刷新顶部状态卡（纯 UI 操作，绝不阻塞）。"""
        try:
            st = read_state()
            env = read_env_brief()
            self.v_capital.set("%.0f" % (st.get("capital") or 0))
            self.v_equity.set("%.0f" % (st.get("equity") or st.get("capital") or 0))
            self.v_pos.set("%.6f" % (st.get("btc") or 0.0))
            self.v_pnl.set("%.2f" % (st.get("realized_pnl") or 0.0))
            daily = st.get("daily_pnl") or 0.0
            day = st.get("day_key") or "—"
            self.v_daily.set("%+.2f%% (%s)" % (daily, day))
            # LLM 配置摘要（只显示是否配置，绝不显示密钥本体）
            model = env.get("LLM_MODEL") or LLM_DEF_MODEL
            key_ok = bool(env.get(LLM_KEY_ENV) or env.get("DEEPSEEK_API_KEY"))
            self.v_provider.set("LLM")
            self.v_model.set(model)
            self.v_key.set("已配置" if key_ok else "未配置（将走内置规则引擎）")
        except Exception:
            pass

    def _set_status(self, s):
        self.v_status.set(s)

    def _set_buttons_state(self, state):
        for b in self.mode_btns:
            try:
                b.config(state=state)
            except Exception:
                pass

    # ------------------------------------------------------------ 交易选项
    def _refresh_short_hint(self):
        """同步做空开关的提示文案与颜色。"""
        on = bool(self._allow_short_var.get())
        if on:
            self._short_hint_var.set("已开启：无多头时 SELL 可在 EMA200 下方开空、BUY 回补，空头同受 5%/20%/熔断/止损约束")
            self._short_hint.config(fg=WARN)
        else:
            self._short_hint_var.set("默认：纯现货做多，SELL 仅减仓、永不做空")
            self._short_hint.config(fg=DIM)

    def _on_toggle_short(self):
        """切换「允许做空」并写回 .env（新任务启动时生效）。"""
        val = "true" if self._allow_short_var.get() else "false"
        try:
            env_upsert("ALLOW_SHORT", val)
        except Exception as e:
            messagebox.showerror("写入失败", "更新 .env 失败：%s" % e)
            self._allow_short_var.set(not self._allow_short_var.get())
        self._refresh_short_hint()
        self._log("---- 交易选项变更：ALLOW_SHORT = %s（下次启动任务生效）----" % val,
                  show=True)

    def _refresh_use_llm_hint(self):
        """同步「使用真实 LLM」开关的提示文案与颜色。"""
        on = bool(self._use_llm_var.get())
        if on:
            self._use_llm_hint_var.set(
                "已开启：回测逐根K线调用真实 LLM 决策（每根一次 API，约1000根≈1000次请求，费用/耗时自负）")
            self._use_llm_hint.config(fg=WARN)
        else:
            self._use_llm_hint_var.set(
                "默认：内置规则引擎（宪法机械执行，不读 API、不产生费用）")
            self._use_llm_hint.config(fg=DIM)

    def _on_toggle_use_llm(self):
        """切换「使用真实 LLM」并写回 .env（新任务启动时生效）。"""
        val = "true" if self._use_llm_var.get() else "false"
        try:
            env_upsert("BACKTEST_USE_LLM", val)
        except Exception as e:
            messagebox.showerror("写入失败", "更新 .env 失败：%s" % e)
            self._use_llm_var.set(not self._use_llm_var.get())
        self._refresh_use_llm_hint()
        self._log("---- 交易选项变更：BACKTEST_USE_LLM = %s（下次启动任务生效）----" % val,
                  show=True)

    def _read_leverage(self) -> str:
        """读取 .env 的 MAX_LEVERAGE（1~10，非法回退 10）。"""
        raw = read_env_brief().get("MAX_LEVERAGE", "10")
        try:
            v = float(raw)
        except (TypeError, ValueError):
            v = 10.0
        v = max(1.0, min(10.0, v))
        return str(int(v)) if v == int(v) else str(v)

    def _on_change_leverage(self, event=None):
        """切换「最大杠杆」并写回 .env（新任务启动时生效）。"""
        val = self._leverage_var.get().strip() or "10"
        try:
            lev = max(1.0, min(10.0, float(val)))
            val = str(int(lev)) if lev == int(lev) else str(lev)
        except ValueError:
            val = "10"
        try:
            env_upsert("MAX_LEVERAGE", val)
        except Exception as e:
            messagebox.showerror("写入失败", "更新 .env 失败：%s" % e)
        self._leverage_var.set(val)
        self._log("---- 交易选项变更：MAX_LEVERAGE = %s（LLM 将在 1~%s 倍内自主选择，下次启动任务生效）----"
                  % (val, val), show=True)

    # ------------------------------------------------------------ 工具动作
    @staticmethod
    def _open(path, desc):
        if path and Path(path).exists():
            os.startfile(str(path))
        else:
            messagebox.showinfo("文件不存在", "%s 尚未生成：\n%s\n\n请先运行对应任务。" % (desc, path))

    def _open_report(self):
        self._open(BASE_DIR / "backtest_report.md", "回测报告")

    def _open_chart(self):
        self._open(BASE_DIR / "backtest_equity_chart.png", "权益曲线图")

    def _open_log(self):
        self._open(BASE_DIR / "trading.log", "运行日志")

    def _install_deps(self):
        """一键安装依赖：后台跑 `python -m pip install -r requirements.txt`，
        输出同样实时显示在日志面板（可监控进度）。"""
        if self._busy:
            messagebox.showinfo("任务进行中", "请先停止当前任务再安装依赖。")
            return
        RUNS_DIR.mkdir(exist_ok=True)
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        log_path = RUNS_DIR / ("install_%s.log" % ts)
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        try:
            self._cur_log = open(log_path, "w", encoding="utf-8")
        except Exception:
            self._cur_log = None
        self._proc = subprocess.Popen(
            [str(TASK_PY), "-m", "pip", "install", "-r",
             str(BASE_DIR / "requirements.txt"),
             "-i", "https://pypi.tuna.tsinghua.edu.cn/simple"],
            cwd=str(BASE_DIR), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            bufsize=1, creationflags=CREATE_NO_WINDOW,
        )
        self._busy = True
        self._set_status("运行中 · 安装依赖")
        self._set_buttons_state("disabled")
        self._log("==== 安装依赖: pip install -r requirements.txt ====", show=True)
        self._log("     日志副本: %s" % log_path)
        threading.Thread(target=self._reader, args=(self._proc,),
                         daemon=True).start()

    def _make_shortcut(self):
        """在桌面创建指向本面板的快捷方式（PowerShell WSH，UTF-16 编码防中文乱码）。"""
        target = str(TASK_PYW) if TASK_PYW.exists() else str(TASK_PY)
        pyw_path = str(BASE_DIR / "launcher_gui.pyw")
        script = (
            "$ws = New-Object -ComObject WScript.Shell\n"
            "$desktop = [Environment]::GetFolderPath('Desktop')\n"
            "$lnk = $ws.CreateShortcut($desktop + '\\AI量化交易Agent.lnk')\n"
            "$lnk.TargetPath = '%s'\n"
            "$lnk.Arguments = '\"%s\"'\n"
            "$lnk.WorkingDirectory = '%s'\n"
            "$lnk.Description = 'AI 量化交易 Agent 控制台'\n"
            "$lnk.Save()\n"
            "Write-Output 'OK'\n" % (target, pyw_path, str(BASE_DIR))
        )
        try:
            enc = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
            r = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", enc],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=30,
            )
        except Exception as e:
            messagebox.showerror("创建失败", "调用 PowerShell 失败：%s" % e)
            return
        if r.returncode == 0:
            messagebox.showinfo("完成", "已在桌面创建「AI量化交易Agent」快捷方式。\n"
                                        "以后双击它即可打开本面板。")
        else:
            messagebox.showerror("创建失败", "PowerShell 返回错误：\n%s" % r.stderr)

    def _on_close(self):
        if self._busy:
            if not messagebox.askyesno("任务运行中",
                                       "有任务正在后台运行，关闭面板不会终止它。\n"
                                       "确定关闭窗口吗？"):
                return
        self._closed = True
        self.root.destroy()


def main():
    # DPI 感知：避免高分屏下界面模糊
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    root = tk.Tk()
    app = LauncherApp(root)        # 定时刷新均经 root.after 挂载，保存引用防回收
    root.mainloop()


def selftest():
    """无头自检：构建全部控件后立即销毁，供自动化验证（不进入主循环）。"""
    root = tk.Tk()
    root.withdraw()
    app = LauncherApp(root)
    root.update()
    app._closed = True
    root.destroy()
    print("SELFTEST OK")


if __name__ == "__main__":
    if not _TK_OK:                       # tkinter 不可用：pythonw 无控制台，落盘后退出
        _log_crash("启动期(tkinter导入失败)", _TK_ERR)
        sys.exit(2)
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        selftest()
    else:
        try:
            main()
        except Exception as e:           # 运行期兜底：写崩溃日志，避免静默死亡
            _log_crash("运行期", e)
            raise
