# -*- coding: utf-8 -*-
"""_probe_v10.py —— 再入场闸门双模式回归探针（REENTRY_GATE_MODE: trend / price）

背景（2026-09-03）：追高拦截旧语义 =「上次平仓价 ×1.02 静态永久锁」→ 牛市里止盈离场即
永久禁入，SOL 2024-10-15~2025-01 主升浪（150→270）95/96 天被引擎静默拦成 HOLD，
「一口没吃到」。参照 daily_stock_analysis「乖离分档 + 强势放宽」哲学把闸门升级为双模式：
  price（旧）：现价 vs 上次平仓价 ×(1+prem)，永久锁——仅作回归对照
  trend（新，默认）：以趋势线 EMA_SLOW 为界——
    * 价格 ≥ 趋势线（顺势/强势区）→ 放行顺势回补，吃主升浪（EMA 动态跟随，天然自回归）
    * 价格 < 趋势线（弱势区）且高于上次平仓价 prem → 仍拦，防「弱势反弹追回」锯齿
    * 冷却期（POST_LOSS_COOLDOWN_BARS）两模式都生效

场景：
  T1 trend 顺势放行：止盈离场后价格远高于平仓价但站上趋势线 → 放行（主升浪核心）
  T2 trend 弱势追高拦截：止损离场后价格在趋势线下方且高于平仓价 2% → 拦
  T3 trend 弱势平价放行：同上但价格回落到平仓价附近（未超 2%）→ 放行
  T4 trend 冷却优先：离场后 1 根内即使顺势也先冷却 → 拦
  T5 trend 空头镜像：跌破趋势线后放行补空
  T6 price 档回归对照：静态锚仍拦（防锯齿语义未丢）
  T7 trend 计数：reentry_trend_allow / reentry_block 各计其数
运行：python _probe_v10.py   （无网络、无 LLM，纯引擎层）
"""
import sys, os, logging
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
logging.disable(logging.CRITICAL)

from config import CFG        # 实例！覆写必须走实例，勿用 import config as CFG
from order_executor import OrderExecutor

TS = datetime(2024, 1, 1, 12, 0)

# ---- 基线参数（运行时覆写，避免依赖 .env 现场）----
CFG.CAPITAL = 10000.0
CFG.STOP_LOSS_RATIO = 0.02
CFG.SINGLE_ORDER_PERCENT = 5
CFG.MAX_POSITION_PERCENT = 20
CFG.OVERALL_LOSS_LIMIT_PERCENT = 50.0
CFG.DAILY_LOSS_LIMIT_PERCENT = 50.0
CFG.MAX_LEVERAGE = 10
CFG.ALLOW_SHORT = True
CFG.TREND_FILTER_LEVEL = "llm"     # 隔离：不测引擎趋势拦截，只测 reentry 闸门
CFG.ATR_STOP_ENABLED = True
CFG.ATR_STOP_MULT = 1.5
CFG.TRAILING_TP_ACTIVATE = 0.02
CFG.TRAILING_TP_RATIO = 0.05
CFG.TRAILING_TP_INTRABAR = True
CFG.POST_LOSS_COOLDOWN_BARS = 3

passed = failed = 0

def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  [PASS] {name} {detail}")
    else:
        failed += 1
        print(f"  [FAIL] {name} {detail}")

def new_ex():
    return OrderExecutor(capital=10000.0, state_path=None)

def buy(ex, price, ema_slow=1.0, atr_pct=None, ts=TS, qty=5.0):
    """开多决策：MACD 多头排列放行引擎层；返回 execute_decision 结果。"""
    return ex.execute_decision(
        {"action": "BUY", "quantity_percent": qty, "leverage": 1,
         "reasoning_summary": "probe"},
        price, ts, ema_slow=ema_slow, macd_dif=1.0, macd_dea=0.0, atr_pct=atr_pct)

def sell(ex, price, ema_slow=1.0, atr_pct=None, ts=TS, qty=5.0):
    """开空决策：MACD 空头排列放行引擎层。"""
    return ex.execute_decision(
        {"action": "SELL", "quantity_percent": qty, "leverage": 1,
         "reasoning_summary": "probe"},
        price, ts, ema_slow=ema_slow, macd_dif=-1.0, macd_dea=0.0, atr_pct=atr_pct)

def reentry_stats(ex):
    return (ex.stats.get("reentry_block", 0), ex.stats.get("reentry_trend_allow", 0))

# =====================================================================
print("== T1 trend 顺势放行：止盈离场后价格大涨仍可回补（主升浪核心）==")
CFG.POST_LOSS_COOLDOWN_BARS = 0     # 关冷却隔离追高判断（冷却语义由 T4 单独测）
CFG.REENTRY_GATE_MODE = "trend"
CFG.REENTRY_MAX_PREMIUM = 0.02
ex = new_ex()
buy(ex, 100.0)                                        # 开多 @100（bar=1）
ex.force_close(105.0, TS, reason="probe止盈")          # 止盈离场 last_exit{long,105}
r = buy(ex, 150.0, ema_slow=140.0)                    # 价150远高于平仓105，但 ≥ 趋势线140 → 顺势
check("T1a 价在趋势线上方→放行回补", ex.position is not None and r["action"] == "BUY",
      f"action={r['action']} reason={r['reason'][:50]}")
blk, allow = reentry_stats(ex)
check("T1b trend_allow 计数+1", allow == 1 and blk == 0, f"block={blk} allow={allow}")

# 对照：price 档同场景必拦（证明旧语义缺陷）
CFG.REENTRY_GATE_MODE = "price"
ex2 = new_ex()
buy(ex2, 100.0)
ex2.force_close(105.0, TS, reason="probe止盈")
r2 = buy(ex2, 150.0, ema_slow=140.0)                  # price 档不看趋势线 → 150>105×1.02 拦
check("T1c price 档同场景仍拦（对照缺陷）",
      r2["action"] == "HOLD" and "追高拦截" in r2["reason"],
      f"action={r2['action']} reason={r2['reason'][:40]}")
CFG.REENTRY_GATE_MODE = "trend"

# =====================================================================
print("== T2 trend 弱势追高拦截：止损后价在趋势线下方且高于平仓价2% ==")
ex = new_ex()
buy(ex, 100.0)
ex.force_close(98.0, TS, reason="probe止损")          # 止损离场 last_exit{long,98}
r = buy(ex, 100.5, ema_slow=150.0)                    # 价100.5 < 趋势线150（弱势），100.5>98×1.02=99.96 → 拦
check("T2 弱势区追高被拦", r["action"] == "HOLD" and ex.position is None
      and "趋势线" in r["reason"],
      f"action={r['action']} reason={r['reason'][:60]}")

# =====================================================================
print("== T3 trend 弱势平价放行：价格回落到平仓价附近（未超2%）==")
r = buy(ex, 99.5, ema_slow=150.0)                     # 99.5 ≤ 99.96 → 弱势但未追高 → 放行
check("T3 弱势但未超2%→放行", ex.position is not None and r["action"] == "BUY",
      f"action={r['action']} reason={r['reason'][:50]}")
ex.force_close(99.5, TS, reason="probe止损")

# =====================================================================
print("== T4 trend 冷却优先：离场后 1 根内即使顺势也先冷却 ==")
CFG.POST_LOSS_COOLDOWN_BARS = 3
ex = new_ex()
buy(ex, 100.0)                                        # bar=1
ex.force_close(98.0, TS, reason="probe止损")           # last_exit bar=1
r = buy(ex, 150.0, ema_slow=140.0)                    # 顺势（150≥140）但 bars_since=1 < 3 → 冷却优先
check("T4a 冷却期内顺势也被拦", r["action"] == "HOLD" and ex.position is None
      and "冷却" in r["reason"],
      f"action={r['action']} reason={r['reason'][:40]}")
r = buy(ex, 150.0, ema_slow=140.0, ts=TS.replace(hour=13))  # bar=2（同TS日期不影响bar计数）
check("T4b 第2根仍冷却", r["action"] == "HOLD" and "冷却" in r["reason"])
# 冷却结束后（bar 计数足够）再顺势 → 放行
ex._bar_counter = 10                                  # 模拟已过冷却期
r = buy(ex, 150.0, ema_slow=140.0)
check("T4c 冷却结束后顺势放行", ex.position is not None and r["action"] == "BUY",
      f"action={r['action']} reason={r['reason'][:40]}")

# =====================================================================
print("== T5 trend 空头镜像：跌破趋势线后放行补空 ==")
CFG.POST_LOSS_COOLDOWN_BARS = 0
ex = new_ex()
sell(ex, 100.0, ema_slow=150.0)                        # 100<150 顺势开空（保证 force_close 真设 last_exit）
ex.force_close(103.0, TS, reason="probe止损")          # 空头止损（价涨）last_exit{short,103}
r = sell(ex, 98.0, ema_slow=150.0)                    # 98 < 趋势线150 → 顺势放行补空
check("T5 空头顺势（价<趋势线）放行", ex.position is not None
      and ex.position["side"] == "short" and r["action"] == "SELL",
      f"action={r['action']} reason={r['reason'][:40]}")
ex.force_close(98.0, TS, reason="probe平空")
# 空头弱势区追空分层：reentry 在 execute_decision 入口先于 _open_short 宪法执行——
# 止损离场后价格在趋势线上方（弱势区）且低于上次平仓价 → reentry 追空拦截（trend 档语义）
ex3 = new_ex()
sell(ex3, 100.0, ema_slow=150.0)                      # 100<150 顺势开空
ex3.force_close(103.0, TS, reason="probe空头止损")     # last_exit{short,103}（价涨止损）
r = sell(ex3, 99.0, ema_slow=95.0)                    # 99>95 趋势线上方且 99<103×0.98=100.94 → reentry 追空拦
check("T5b 空头弱势区追空被 reentry 拦",
      r["action"] == "HOLD" and ex3.position is None
      and "追空拦截" in r["reason"],
      f"action={r['action']} reason={r['reason'][:60]}")
check("T5b2 该拦计入 reentry_block（先于宪法执行）",
      ex3.stats.get("reentry_block", 0) == 1,
      f"reentry_block={ex3.stats.get('reentry_block',0)}")
# 分层对照：弱势区但不追空（价格≥上次平仓价98×0.98）→ reentry 放行 → 落到 _open_short 宪法拦
r = sell(ex3, 101.0, ema_slow=95.0)                   # 101>95 弱势但 101>100.94 不追空 → reentry 放
check("T5b3 reentry 放行非追空 → 宪法拦截兜底",
      r["action"] == "HOLD" and ex3.position is None
      and "禁止做空" in r["reason"],
      f"action={r['action']} reason={r['reason'][:50]}")
# 对照：价格跌破趋势线后（顺势区）引擎放行 → 放行补空
r = sell(ex3, 92.0, ema_slow=95.0)                    # 92 < 95 → 顺势放行
check("T5c 空头跌破趋势线后顺势放行", ex3.position is not None
      and ex3.position["side"] == "short" and r["action"] == "SELL",
      f"action={r['action']} reason={r['reason'][:40]}")

# =====================================================================
print("== T6 price 档回归对照：静态锚语义未丢 ==")
CFG.REENTRY_GATE_MODE = "price"
ex = new_ex()
buy(ex, 100.0)
ex.force_close(98.0, TS, reason="probe止损")           # last_exit{long,98}
r = buy(ex, 100.5, ema_slow=150.0)                    # 100.5 > 99.96 → 静态锚拦（不看趋势线）
check("T6a price 档追高拦截保留", r["action"] == "HOLD" and "追高拦截" in r["reason"],
      f"action={r['action']} reason={r['reason'][:40]}")
r = buy(ex, 99.5, ema_slow=150.0)                     # 99.5 ≤ 99.96 → 放行
check("T6b price 档平价放行", ex.position is not None and r["action"] == "BUY",
      f"action={r['action']}")

# =====================================================================
print("== T7 混合计数验证 ==")
CFG.REENTRY_GATE_MODE = "trend"
ex = new_ex()
buy(ex, 100.0)
ex.force_close(98.0, TS, reason="probe止损")
buy(ex, 100.5, ema_slow=150.0)                        # 弱势追高 → block
buy(ex, 150.0, ema_slow=140.0)                        # 顺势 → allow（此刻空仓，顺势放行开仓）
blk, allow = reentry_stats(ex)
check("T7 block/allow 分类计数", blk == 1 and allow == 1,
      f"block={blk} allow={allow}")

# =====================================================================
print(f"\n==== 结果：{passed} 通过 / {failed} 失败 ====")
sys.exit(1 if failed else 0)
