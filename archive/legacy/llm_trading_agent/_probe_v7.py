# -*- coding: utf-8 -*-
"""v7 三项优化综合探针：修复 _bar_counter 缺失 + _open_long 未接 ATR 后回归验证。
场景 A-F 独立 ex 实例，互不污染。
"""
import sys, os, logging
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
logging.disable(logging.CRITICAL)   # 探针只看返回值与状态，不刷日志

import config as _config_mod
CFG = _config_mod.CFG          # 注意：order_executor 是 `from config import CFG`，必须改实例属性而非模块命名空间
from order_executor import OrderExecutor

TS = datetime(2024, 1, 1, 12, 0)

# ---- 基线参数（运行时覆写，避免依赖 .env 现场）----
CFG.CAPITAL = 10000.0
CFG.STOP_LOSS_RATIO = 0.02
CFG.SINGLE_ORDER_PERCENT = 5
CFG.MAX_POSITION_PERCENT = 20
CFG.OVERALL_LOSS_LIMIT_PERCENT = 50.0   # 探针禁用回撤熔断干扰
CFG.DAILY_LOSS_LIMIT_PERCENT = 50.0
CFG.MAX_LEVERAGE = 10
CFG.ALLOW_SHORT = True
CFG.ATR_STOP_ENABLED = True
CFG.ATR_STOP_MULT = 1.5
CFG.TRAILING_TP_ACTIVATE = 0.02
CFG.TRAILING_TP_RATIO = 0.05
CFG.TRAILING_TP_INTRABAR = True

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

def buy(ex, price, atr_pct=None, ts=TS):
    """开多决策：趋势线放行（ema_slow=1 << price, MACD多头排列）。"""
    return ex.execute_decision(
        {"action": "BUY", "quantity_percent": 5.0, "leverage": 1,
         "reasoning_summary": "probe"},
        price, ts, ema_slow=1.0, macd_dif=1.0, macd_dea=0.0, atr_pct=atr_pct)

# =====================================================================
print("== 场景A：ATR 双指标止损价（_stop_price）==")
ex = new_ex()
sp = ex._stop_price("long", 100.0, None)
check("A1 无ATR信息→固定2%", abs(sp - 98.0) < 1e-9, f"long@100 atr=None => {sp:.2f} (期望98.00)")
sp = ex._stop_price("long", 100.0, 4.0)
check("A2 ATR4%×1.5=6%>2%→取6%", abs(sp - 94.0) < 1e-9, f"long@100 atr=4% => {sp:.2f} (期望94.00)")
sp = ex._stop_price("long", 100.0, 1.0)
check("A3 ATR1%×1.5=1.5%<2%→2%保底", abs(sp - 98.0) < 1e-9, f"long@100 atr=1% => {sp:.2f} (期望98.00)")
sp = ex._stop_price("short", 100.0, 4.0)
check("A4 空头ATR4%→106.0", abs(sp - 106.0) < 1e-9, f"short@100 atr=4% => {sp:.2f} (期望106.00)")
CFG.ATR_STOP_ENABLED = False
sp = ex._stop_price("long", 100.0, 8.0)
check("A5 ATR开关关闭→固定2%", abs(sp - 98.0) < 1e-9, f"ATR关闭 long@100 atr=8% => {sp:.2f} (期望98.00)")
CFG.ATR_STOP_ENABLED = True

# =====================================================================
print("== 场景F：多头开仓端到端接线（execute_decision → _open_long → _stop_price）==")
ex = new_ex()
r = buy(ex, 100.0, atr_pct=4.0)
p = ex.position
check("F1 开多成交且不TypeError", p is not None and p["side"] == "long", f"result={r['action']}")
if p is not None:
    check("F2 多头止损价=ATR放宽价94.0", abs(p["stop_price"] - 94.0) < 1e-9,
          f"stop_price={p['stop_price']:.2f} (期望94.00)")

# =====================================================================
print("== 场景B：移动止盈·盘中击穿检测（low≤触发线→按触发线价成交）==")
ex = new_ex()
buy(ex, 100.0)                                   # 开多 @100（同K线不做intrabar，镜像主循环顺序）
ret = ex.check_trailing_tp_intrabar(105.0, 103.0, TS)   # 新高105→激活；low103未击穿99.75
check("B1 激活但未击穿→持仓不动", ret is None and ex.position is not None,
      f"highest={ex.position['highest_price'] if ex.position else None}")
ret = ex.check_trailing_tp_intrabar(104.0, 101.0, TS)   # 104<105不更新基准；101>99.75不触发
check("B2 回撤3.8%未到5%→继续持有", ret is None and ex.position is not None)
ret = ex.check_trailing_tp_intrabar(102.0, 99.5, TS)    # low99.5 ≤ 触发线99.75 → 触发
closed = ex.closed_trades[-1] if ex.closed_trades else None
check("B3 击穿即触发离场", ex.position is None and ex.stats["take_profit"] == 1,
      f"take_profit={ex.stats['take_profit']}")
check("B4 按触发线价99.75成交(非市价99.5)", closed is not None and abs(closed["price"] - 99.75) < 1e-9,
      f"成交价={closed['price'] if closed else None} (期望99.75)")

# =====================================================================
print("== 场景C：关闭盘中检测后，收盘版 check_trailing_tp 仍可用 ==")
CFG.TRAILING_TP_INTRABAR = False
ex = new_ex()
buy(ex, 100.0)
ret = ex.check_trailing_tp(103.0, TS)            # 新高103 激活（浮盈3%≥2%）
check("C1 浮盈激活但未回撤→持有", ret is None and ex.position is not None)
ret = ex.check_trailing_tp(97.8, TS)             # 97.8/103-1=-5.05% ≤ -5% → 止盈
check("C2 收盘回撤超5%→止盈", ret is not None and ex.position is None,
      f"take_profit={ex.stats['take_profit']}")

# =====================================================================
print("== 场景D：止损冷却（POST_LOSS_COOLDOWN_BARS=3，追高拦截关闭）==")
CFG.TRAILING_TP_INTRABAR = True
CFG.POST_LOSS_COOLDOWN_BARS = 3
CFG.REENTRY_MAX_PREMIUM = 0.0
ex = new_ex()
buy(ex, 100.0)                                    # counter=1
ex.force_close(98.0, TS, reason="probe止损")      # last_exit{long,98,bar1}
r = buy(ex, 99.5)                                 # counter=2, bars_since=1 <3 → 拦
check("D1 止损后第1根K线追多被冷却", r["action"] == "HOLD" and ex.position is None,
      f"action={r['action']} reason={r['reason'][:40]}")
r = buy(ex, 99.8)                                 # counter=3, bars_since=2 <3 → 拦
check("D2 第2根K线仍被冷却", r["action"] == "HOLD" and ex.position is None,
      f"action={r['action']}")
r = buy(ex, 100.1)                                # counter=4, bars_since=3 → 放行
check("D3 第3根K线起冷却结束可开仓", ex.position is not None and r["action"] == "BUY",
      f"action={r['action']} reentry_block={ex.stats['reentry_block']}")
check("D4 拦截计数=2", ex.stats["reentry_block"] == 2,
      f"reentry_block={ex.stats['reentry_block']}")

# =====================================================================
print("== 场景E：追高拦截 price档回归（REENTRY_GATE_MODE=price, REENTRY_MAX_PREMIUM=2%，冷却关闭）==")
CFG.POST_LOSS_COOLDOWN_BARS = 0
CFG.REENTRY_GATE_MODE = "price"     # 回归：锁定旧静态价锚语义（trend 档新语义见 _probe_v10.py）
CFG.REENTRY_MAX_PREMIUM = 0.02
ex = new_ex()
buy(ex, 100.0)                                    # counter=1
ex.force_close(99.0, TS, reason="probe离场")      # last_exit{long,99,bar1}
r = buy(ex, 99.5)                                 # 99.5 ≤ 99×1.02=100.98 → 放行回补
check("E1 平价附近回补放行", ex.position is not None and r["action"] == "BUY",
      f"action={r['action']}")
ex.force_close(100.0, TS, reason="probe离场")     # last_exit{long,100,bar2}
r = buy(ex, 102.5)                                # 102.5 > 102 → 追高拦截
check("E2 高于离场价2%追多被拦", r["action"] == "HOLD" and ex.position is None,
      f"action={r['action']} reason={r['reason'][:40]}")
r = buy(ex, 101.5)                                # 101.5 ≤ 102 → 放行
check("E3 回落至阈值内放行", ex.position is not None and r["action"] == "BUY",
      f"action={r['action']} reentry_block={ex.stats['reentry_block']}")

# =====================================================================
print(f"\n==== 结果：{passed} 通过 / {failed} 失败 ====")
sys.exit(1 if failed else 0)
