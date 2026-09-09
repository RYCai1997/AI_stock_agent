# -*- coding: utf-8 -*-
"""v12 探针：账本余额守恒回归（Codex #1 修复验证）。

核心不变量：
  I1. 开仓瞬间（price=entry、无费）权益不变：cash+margin == 初始本金。
  I2. 权益 = cash + margin + 未实现（不再叠加 realized_pnl —— 曾致重复计数）。
  I3. 全平后权益 = cash（margin+realized 已并回）。
  I4. 任何时刻 权益(price) == 初始本金 + 已实现净额(含资金费率/成本，若启用)。
覆盖：开多/开空守恒、盈利平、亏损平、部分减仓、加仓、强平、资金费率、多回合累计。
运行：.venv/Scripts/python.exe _probe_v12.py
"""
import sys, os, logging
from datetime import datetime, timedelta
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
logging.disable(logging.CRITICAL)

from config import CFG
from order_executor import OrderExecutor

CFG.TRADING_MODE = "futures"  # 本探针验证旧合约账本；不受现货默认模式影响

TS = datetime(2024, 1, 1, 12, 0)
TS8 = TS + timedelta(hours=9)          # 跨越 1 个 8h funding 周期

passed = failed = 0

def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {detail}")

def new_ex():
    CFG.CAPITAL = 100000.0
    CFG.SINGLE_ORDER_PERCENT = 20
    CFG.MAX_POSITION_PERCENT = 30
    CFG.OVERALL_LOSS_LIMIT_PERCENT = 50.0
    CFG.DAILY_LOSS_LIMIT_PERCENT = 50.0
    CFG.MAX_LEVERAGE = 10
    CFG.ALLOW_SHORT = True
    CFG.TREND_FILTER_LEVEL = "llm"
    CFG.POST_LOSS_COOLDOWN_BARS = 3
    CFG.REENTRY_GATE_MODE = "trend"
    CFG.REENTRY_MAX_PREMIUM = 0.02
    CFG.FUNDING_RATE_PER_8H = 0.0     # 默认关费；专项测试单独开
    CFG.SIMULATE_COSTS = False        # 本探针专注账本守恒（成本在 v13 探针）
    CFG.CHASE_DEV_START_PCT = 999.0   # 关掉乖离衰减（v11 已验证），隔离账本断言
    CFG.CHASE_DEV_DECAY_FACTOR = 0.5
    CFG.CHASE_DEV_MAX_LEVERAGE = 2
    ex = OrderExecutor(capital=100000.0, state_path=None)
    ex._bar_counter = 100             # 越过冷却期
    return ex

def buy(ex, price, ema, q=5.0, lev=2):
    return ex.execute_decision(
        {"action": "BUY", "quantity_percent": q, "leverage": lev,
         "reasoning_summary": "probe"},
        price, TS, ema_slow=ema, macd_dif=1.0, macd_dea=0.0, atr_pct=None)

def sell(ex, price, ema, q=5.0, lev=2):
    return ex.execute_decision(
        {"action": "SELL", "quantity_percent": q, "leverage": lev,
         "reasoning_summary": "probe"},
        price, TS, ema_slow=ema, macd_dif=-1.0, macd_dea=0.0, atr_pct=None)

print("== B1 开多守恒：开仓瞬间权益不变（Codex 场景） ==")
ex = new_ex()
r = buy(ex, 100.0, ema=95.0)                       # margin=5%*100k=5000 @2x
m = ex.position["margin"] if ex.position else 0.0
check("B1a 开仓成功", r["action"] == "BUY", f"action={r['action']}")
check("B1b margin=5000", abs(m - 5000.0) < 1e-6, f"margin={m}")
check("B1c 开仓瞬间权益守恒=100000",
      abs(ex.mark_to_market(100.0) - 100000.0) < 1e-6,
      f"eq={ex.mark_to_market(100.0):.4f}")

print("== B2 盈利全平（Codex 数字：应 101000，非 102000） ==")
ex = new_ex()
buy(ex, 100.0, ema=95.0)
ex.force_close(110.0, TS, reason="probe止盈")
check("B2a 平仓后权益=101000",
      abs(ex.mark_to_market(110.0) - 101000.0) < 1e-6,
      f"eq={ex.mark_to_market(110.0):.2f}  cash={ex.cash:.2f}")
check("B2b realized_pnl=1000（纯统计）", abs(ex.realized_pnl - 1000.0) < 1e-6,
      f"realized={ex.realized_pnl}")
check("B2c 权益==cash（无仓不再叠加 realized）",
      abs(ex.mark_to_market(110.0) - ex.cash) < 1e-6,
      f"eq={ex.mark_to_market(110.0):.2f} cash={ex.cash:.2f}")

print("== B3 亏损全平 ==")
ex = new_ex()
buy(ex, 100.0, ema=95.0)
ex.force_close(90.0, TS, reason="probe止损")
check("B3 亏损平仓权益=99000",
      abs(ex.mark_to_market(90.0) - 99000.0) < 1e-6,
      f"eq={ex.mark_to_market(90.0):.2f}")

print("== B4 部分减仓守恒（同价下减仓前后权益连续） ==")
ex = new_ex()
buy(ex, 100.0, ema=95.0)                            # margin 5000, notional 10000
eq_before = ex.mark_to_market(110.0)
r = ex.execute_decision({"action": "SELL", "quantity_percent": 2.5,
                         "leverage": 2, "reasoning_summary": "probe减"},
                        110.0, TS, ema_slow=95.0, macd_dif=1.0, macd_dea=0.0,
                        atr_pct=None)
eq_after = ex.mark_to_market(110.0)
check("B4a 减仓成功", r["action"] == "SELL", f"action={r['action']}")
check("B4b 减仓瞬间权益连续", abs(eq_after - eq_before) < 1e-6,
      f"before={eq_before:.2f} after={eq_after:.2f}")
check("B4c 减仓后仍有仓", ex.position is not None)

print("== B5 加仓（顺势追高，非摊均价）守恒 ==")
ex = new_ex()
buy(ex, 100.0, ema=95.0)
buy(ex, 110.0, ema=95.0)                            # 110>100 允许加仓
# 第二仓在 price=110 开仓瞬间权益不跳变：= 加仓前 eq(110)=101000（首仓浮盈已计）
eq = ex.mark_to_market(110.0)
check("B5 加仓瞬间权益守恒=101000", abs(eq - 101000.0) < 1e-6,
      f"eq={eq:.4f}")

print("== B6 强平：保证金全损、权益正确 ==")
ex = new_ex()
buy(ex, 100.0, ema=95.0)                            # 2x → liq≈50
hit = ex.check_liquidation(49.0, TS)
eq = ex.mark_to_market(49.0)
check("B6a 强平触发", hit)
check("B6b 强平后权益=95000（保证金5000全损）", abs(eq - 95000.0) < 1e-6,
      f"eq={eq:.2f}")
check("B6c realized_pnl=-5000（统计）", abs(ex.realized_pnl + 5000.0) < 1e-6,
      f"realized={ex.realized_pnl:.2f}")

print("== B7 空头镜像：开空守恒 + 下跌盈利平仓 ==")
ex = new_ex()
r = sell(ex, 100.0, ema=150.0)                      # price<ema_slow 空头宪法放行
check("B7a 开空成功", r["action"] == "SELL", f"action={r['action']}")
check("B7b 开空瞬间权益守恒",
      abs(ex.mark_to_market(100.0) - 100000.0) < 1e-6,
      f"eq={ex.mark_to_market(100.0):.2f}")
ex.force_close(90.0, TS, reason="probe空止盈")
check("B7c 空头盈利平仓=101000",
      abs(ex.mark_to_market(90.0) - 101000.0) < 1e-6,
      f"eq={ex.mark_to_market(90.0):.2f}")

print("== B8 资金费率守恒：权益精确扣费 ==")
ex = new_ex()
ex.funding_rate_8h = 0.001                            # 0.1%/8h，notional 10000 → 10
buy(ex, 100.0, ema=95.0)                             # notional=10000
ex.accrue_funding(TS, 100.0)                          # 首次仅设锚点，不收费
ex.accrue_funding(TS8, 100.0)                         # 9h → 1 周期，应扣 10
check("B8a 计提后权益=99990",
      abs(ex.mark_to_market(100.0) - 99990.0) < 1e-6,
      f"eq={ex.mark_to_market(100.0):.4f}")
check("B8b funding_paid=10", abs(ex.stats["funding_paid"] - 10.0) < 1e-6,
      f"paid={ex.stats['funding_paid']}")
ex.force_close(100.0, TS, reason="probe平价平")
check("B8c 平仓后权益=99990（资金费率已从现金扣除）",
      abs(ex.mark_to_market(100.0) - 99990.0) < 1e-6,
      f"eq={ex.mark_to_market(100.0):.4f}")
CFG.FUNDING_RATE_PER_8H = 0.0

print("== B9 多回合累计：权益 == 本金 + Σ净已实现（通用不变量） ==")
ex = new_ex()
buy(ex, 100.0, ema=95.0)
ex.force_close(110.0, TS, reason="r1")               # +1000
ex._bar_counter += 10
buy(ex, 120.0, ema=95.0)
ex.force_close(114.0, TS, reason="r2")               # 亏损
ex._bar_counter += 10
sell(ex, 100.0, ema=150.0)
ex.force_close(95.0, TS, reason="r3")                # 盈利
eq = ex.mark_to_market(95.0)
check("B9a 权益 == 本金 + realized_pnl（无费用时）",
      abs(eq - (100000.0 + ex.realized_pnl)) < 1e-6,
      f"eq={eq:.2f} realized={ex.realized_pnl:.2f}")
sum_realized = sum(t["realized_usdt"] for t in ex.closed_trades)
check("B9b realized_pnl == Σ平仓记录 realized_usdt（±2dp 舍入容差）",
      abs(ex.realized_pnl - sum_realized) < 0.1,
      f"realized={ex.realized_pnl:.2f} sum={sum_realized:.2f}")

print()
print(f"==== 结果：{passed} 通过 / {failed} 失败 ====")
sys.exit(1 if failed else 0)
