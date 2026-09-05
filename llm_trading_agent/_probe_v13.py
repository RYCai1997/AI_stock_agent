# -*- coding: utf-8 -*-
"""v13 探针：账户级回撤闸门（Codex #6）与交易门控峰值重置解耦验证。

关键语义（与 drawdown_pct 的区别）：
  - peak_equity（交易门控）：平仓/强平后重置为当前权益 → 防旧峰值永久锁死交易。
  - account_peak_equity（账户级闸门）：只随创新高上移，永不因平仓重置 →
    连续亏损不会被清零，超过 EQUITY_DRAWDOWN_LIMIT_PERCENT 禁开新仓并强制清仓。

场景：E1 解耦铁证（平仓后交易门控归零但账户闸门仍拦）、E2 持仓中触闸强制清仓、
      E3 创新高后闸门自动解除。运行：.venv/Scripts/python.exe _probe_v13.py
"""
import sys, os, logging
from datetime import datetime
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
logging.disable(logging.CRITICAL)

from config import CFG
from order_executor import OrderExecutor

CFG.TRADING_MODE = "futures"  # 本探针验证旧合约风控；不受现货默认模式影响

TS = datetime(2024, 1, 1, 12, 0)
passed = failed = 0

def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {detail}")

def new_ex(gate=2.0):
    CFG.CAPITAL = 100000.0
    CFG.SINGLE_ORDER_PERCENT = 20
    CFG.MAX_POSITION_PERCENT = 30
    CFG.OVERALL_LOSS_LIMIT_PERCENT = 5.0     # 旧闸门：5%
    CFG.EQUITY_DRAWDOWN_LIMIT_PERCENT = gate  # 账户闸门：2%（探针用小阈值驱动真实动态）
    CFG.DAILY_LOSS_LIMIT_PERCENT = 50.0
    CFG.MAX_LEVERAGE = 10
    CFG.ALLOW_SHORT = True
    CFG.TREND_FILTER_LEVEL = "llm"
    CFG.POST_LOSS_COOLDOWN_BARS = 3
    CFG.REENTRY_GATE_MODE = "trend"
    CFG.REENTRY_MAX_PREMIUM = 0.02
    CFG.SIMULATE_COSTS = False
    CFG.CHASE_DEV_START_PCT = 999.0          # 关乖离衰减，隔离闸门断言
    ex = OrderExecutor(capital=100000.0, state_path=None)
    ex._bar_counter = 100
    return ex

def buy(ex, price, ema, q=5.0, lev=2):
    return ex.execute_decision(
        {"action": "BUY", "quantity_percent": q, "leverage": lev,
         "reasoning_summary": "probe"},
        price, TS, ema_slow=ema, macd_dif=1.0, macd_dea=0.0, atr_pct=None)

print("== E1 解耦铁证：亏损平仓后交易门控归零、账户闸门仍拦 ==")
ex = new_ex()
buy(ex, 100.0, ema=95.0)                        # margin 5000 @2x, qty=100
ex.update_peak(120.0)                           # eq=102000 → 双轨峰值均 102000
ex.force_close(90.0, TS, reason="probe亏平")    # eq=99000；peak 重置=99000，account 保持 102000
ex._bar_counter += 10                            # 越过 POST_LOSS_COOLDOWN_BARS=3 冷却，让账户闸门被真正评估
check("E1a 交易门控已重置（drawdown_pct=0）",
      abs(ex.drawdown_pct(90.0)) < 1e-9, f"dd={ex.drawdown_pct(90.0):.4f}")
check("E1b 账户高水位未重置（account_dd=-2.94%）",
      abs(ex.account_dd_pct(90.0) - (99000.0/102000.0 - 1.0) * 100.0) < 1e-6,
      f"add={ex.account_dd_pct(90.0):.4f}")
r = buy(ex, 95.0, ema=90.0)                     # 账户 DD -2.94% ≤ -2% → 必须被拦
check("E1c 账户闸门拦截新开仓", r["action"] == "HOLD" and "账户级回撤" in r["reason"],
      f"action={r['action']} reason={r['reason'][:60]}")
check("E1d account_dd_block=1", ex.stats["account_dd_block"] == 1,
      f"block={ex.stats['account_dd_block']}")
check("E1e overall_block=0（旧 5% 闸门未误触发）", ex.stats["overall_block"] == 0,
      f"overall={ex.stats['overall_block']}")

print("== E2 持仓中账户回撤触闸 → 强制清仓 ==")
ex = new_ex()
buy(ex, 100.0, ema=95.0, q=20.0, lev=10)        # margin 20000 @10x, qty=2000
r = ex.execute_decision({"action": "HOLD", "quantity_percent": 0,
                         "leverage": 2, "reasoning_summary": "probe"},
                        99.0, TS, ema_slow=95.0, macd_dif=1.0, macd_dea=0.0)
# @99：未实现 -2000 → eq=98000 → 账户 DD -2.0% ≤ -2% → force_close
check("E2a 强制清仓触发", r["action"] == "CLOSE" and "账户级回撤" in r["reason"],
      f"action={r['action']} reason={r['reason'][:70]}")
check("E2b 已无持仓", ex.position is None)
check("E2c account_dd_force=1", ex.stats["account_dd_force"] == 1,
      f"force={ex.stats['account_dd_force']}")
check("E2d 清仓后权益=98000", abs(ex.mark_to_market(99.0) - 98000.0) < 1e-6,
      f"eq={ex.mark_to_market(99.0):.2f}")

print("== E3 连续亏损累计可见性（Codex #6 核心：账户回撤不被每回合平仓清零） ==")
ex = new_ex(gate=2.0)
buy(ex, 100.0, ema=95.0)            # margin 5000 @2x, qty=100, cash=95000
ex.update_peak(120.0)               # eq=102000 → 双轨峰值均 102000
# ---- 回合1：亏损平仓（账户 102000 → 99800）----
ex.force_close(98.0, TS, reason="回合1亏平")     # realized=-200 → eq=99800
check("E3a 回合1后交易门控峰值重置(99800)",
      abs(ex.peak_equity - 99800.0) < 1e-6, f"peak={ex.peak_equity:.2f}")
check("E3b 回合1后账户高水位保持102000（未清零）",
      abs(ex.account_peak_equity - 102000.0) < 1e-6, f"acc_peak={ex.account_peak_equity:.2f}")
check("E3c 旧判据 drawdown_pct 被平仓重置骗过=0%（Codex#6漏洞）",
      abs(ex.drawdown_pct(98.0)) < 1e-9, f"dd={ex.drawdown_pct(98.0):.4f}")
exp_dd = (99800.0 / 102000.0 - 1.0) * 100.0      # -2.157%
check("E3d 新判据 account_dd_pct 如实累计=-2.16%",
      abs(ex.account_dd_pct(98.0) - exp_dd) < 1e-6, f"add={ex.account_dd_pct(98.0):.4f}")
# ---- 回合2：越过冷却再进场 → 账户累计 -2.16% ≤ -2% → 闸门拦截（无需再亏一笔）----
ex._bar_counter += 10
r2 = buy(ex, 98.0, ema=93.0)
check("E3e 回合2开仓被账户闸门拦截（累计回撤≠单笔回撤）",
      ex.stats["account_dd_block"] == 1 and r2["action"] == "HOLD",
      f"block={ex.stats['account_dd_block']} action={r2['action']}")

print("== E3.2 恢复机制：持仓浮盈创新高 → 账户高水位上移 ==")
ex3 = new_ex(gate=2.0)
buy(ex3, 100.0, ema=95.0)           # margin 5000 @2x, qty=100, cash=95000
ex3.update_peak(110.0)              # eq=101000 → 高水位 101000
ex3.update_peak(125.0)              # 持仓浮盈 (25*100)=2500 → eq=102500 → 高水位上移
check("E3f 持仓浮盈创新高→账户高水位上移102500",
      abs(ex3.account_peak_equity - 102500.0) < 1e-6, f"acc_peak={ex3.account_peak_equity:.2f}")
check("E3g 新高处账户回撤=0（恢复基准已随高水位刷新）",
      abs(ex3.account_dd_pct(125.0)) < 1e-9, f"add={ex3.account_dd_pct(125.0):.4f}")

print()
print(f"==== 结果：{passed} 通过 / {failed} 失败 ====")
sys.exit(1 if failed else 0)
