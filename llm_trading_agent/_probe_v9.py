# -*- coding: utf-8 -*-
"""v9 探针：TREND_FILTER_LEVEL 两档行为 + 战情简报 K 线窗口渲染。

场景:
  A. engine 档：价格<ema_slow 的 BUY 被引擎硬拦（ema_intercept+1），返回 HOLD
  B. llm 档  ：同条件 BUY 放行（记录 llm_countertrend_buy+1），进入 _open_long 后续仓位检查
  C. llm 档  ：价格>ema_slow 的 BUY 正常放行（不记 countertrend）
  D. 战情简报：LLM_KLINE_WINDOW=24 时渲染近期窗口含 L(低点)字段；0 时退化为无窗口
  E. _hard_block_text：engine 档返回"宪法禁止做多"硬约束；llm 档不返回该条
"""
import logging
import pandas as pd
import sys

logging.disable(logging.CRITICAL)
from config import CFG          # 注意：必须是单例实例（from import），不是 import config 模块！
import order_executor as oe
import prompt_builder as pb
import main as M

TS0 = pd.Timestamp("2026-09-01 08:00:00")
PASS = FAIL = 0

def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name} {detail}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {detail}")

def mk_ex():
    return oe.OrderExecutor(capital=100000.0, state_path=None)

# ---------- A/B/C: 两档趋势过滤 ----------
print("== A：engine 档（价格<ema 的 BUY 应被硬拦）==")
CFG.TREND_FILTER_LEVEL = "engine"
ex = mk_ex()
d = {"action": "BUY", "quantity_percent": 3.0, "leverage": 3,
     "confidence_level": "中", "stop_loss_price": None, "reasoning_summary": "试探"}
r = ex.execute_decision(d, 100.0, TS0, ema_slow=105.0, macd_dif=1.0, macd_dea=0.0)
check("A1 返回 HOLD", r["action"] == "HOLD", f"action={r['action']}")
check("A2 ema_intercept 计数", ex.stats["ema_intercept"] == 1, f"={ex.stats['ema_intercept']}")
check("A3 llm_countertrend 为 0（engine 档不记录）", ex.stats.get("llm_countertrend_buy", 0) == 0, "")

print("== B：llm 档（同条件 BUY 应放行 + 记录 countertrend）==")
CFG.TREND_FILTER_LEVEL = "llm"
ex = mk_ex()
r = ex.execute_decision(d, 100.0, TS0, ema_slow=105.0, macd_dif=1.0, macd_dea=0.0)
check("B1 放行开多（不再硬拦）", r["action"] == "BUY", f"action={r['action']} reason={r.get('reason','')[:30]}")
# 100 权益 5% 保证金 → 仓位检查后应成功开多 3000 保证金
check("B2 llm_countertrend_buy 计数", ex.stats.get("llm_countertrend_buy", 0) == 1, "")
check("B3 ema_intercept 不增", ex.stats["ema_intercept"] == 0, f"={ex.stats['ema_intercept']}")

print("== C：llm 档（价格>ema 的 BUY 正常，不记 countertrend）==")
ex = mk_ex()
d2 = dict(d)
r = ex.execute_decision(d2, 100.0, TS0, ema_slow=95.0, macd_dif=1.0, macd_dea=0.0)
check("C1 放行开多", r["action"] == "BUY", f"action={r['action']} reason={r.get('reason','')[:30]}")
check("C2 countertrend 不增", ex.stats.get("llm_countertrend_buy", 0) == 0, "")

print("== D：战情简报 K 线窗口渲染 ==")
CFG.LLM_KLINE_WINDOW = 24
rows = [{"time": f"2026-08-{d:02d} 08:00", "close": 100.0 + d, "low": 99.0 + d,
         "change_pct": 0.5 if d % 2 else -0.3} for d in range(1, 30)]
row = {"close": 125.0, "ema_slow": 120.0, "ema_trend": 122.0, "ema_200": 118.0,
       "rsi_14": 55.0, "bb_upper": 130.0, "bb_lower": 110.0,
       "macd_dif": 1.0, "macd_dea": 0.5, "vol_ratio": 1.2, "change_pct": 1.0,
       "open_time": pd.Timestamp("2026-08-30 08:00:00"), "_recent": rows}
msg = pb.build_user_prompt(row, 0.0, 0.0, 0, equity=100000.0, cash=100000.0)
check("D1 窗口含 24 根", "近期24根" in msg, "")
check("D2 含低点 L 字段", "L" in msg and "C1" in msg, "")
check("D3 无窗口时退化", "近期24根" not in pb.build_user_prompt(
    {**row, "_recent": []}, 0.0, 0.0, 0, equity=100000.0, cash=100000.0), "")
check("D4 宪法含 v9 分析职责", "你的分析职责" in pb._select_constitution(), "")
check("D5 宪法不再机械引导", "只在价格站上长周期趋势线" not in pb._select_constitution(), "")

print("== E：_hard_block_text 两档差异 ==")
snap = {"daily_pnl_pct": 0.0, "drawdown_pct": 0.0}
row_ema_below = {"close": 100.0, "ema_slow": 105.0, "ema_trend": 103.0}
CFG.TREND_FILTER_LEVEL = "engine"
t1 = M._hard_block_text(row_ema_below, snap)
check("E1 engine 档返回宪法禁多", t1 is not None and "宪法禁止做多" in t1, f"t1={t1}")
CFG.TREND_FILTER_LEVEL = "llm"
t2 = M._hard_block_text(row_ema_below, snap)
check("E2 llm 档不返回禁多硬约束", t2 is None or "禁止做多" not in t2, f"t2={t2}")
CFG.TREND_FILTER_LEVEL = "llm"
snap2 = {"daily_pnl_pct": -2.5, "drawdown_pct": 0.0}
t3 = M._hard_block_text(row_ema_below, snap2)
check("E3 熔断仍强制（llm 档也返回）", t3 is not None and "熔断" in t3, f"t3={t3}")

print(f"\n==== 结果：{PASS} 通过 / {FAIL} 失败 ====")
sys.exit(1 if FAIL else 0)
