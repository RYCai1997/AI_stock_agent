# -*- coding: utf-8 -*-
"""_probe_v11.py —— 高乖离追高仓位衰减回归探针（CHASE_DEV_*）

背景（2026-09-03）：浪末深止损三次 -7~-10%（-669/-904/-797 USDT）源于「宽止损×满仓」。
校准 SOL 1d 主升浪 13 个真实买点证明：乖离硬顶无法区分赢家/输家（11-15 @+25%乖离仍
赚 +5.4%；11-27 @+21% 深亏 -9.8%），拦截会误杀最大赢单 → 按参考仓库「强势可追但轻仓」
哲学改为仓位衰减：乖离超 CHASE_DEV_START_PCT 不拦方向、只强制降敞口
（单仓保证金 × CHASE_DEV_DECAY_FACTOR、杠杆 ≤ CHASE_DEV_MAX_LEVERAGE）。

场景：
  D1 未超阈值：原尺寸开仓，不衰减
  D2 乖离 20% > 8%：qty 减半、杠杆压 ≤2、stats 计数 +1
  D3 边界 =8%：不衰减（严格大于才触发）
  D4 参数覆写：factor=0.3 / max_lev=1 生效
  D5 空头镜像：价格远低于趋势线的追空同样衰减
  D6 skip_reentry 路径：settle 阶段不二次衰减（决策根已衰减过）
  D7 加仓不衰减：持仓中 BUY 走加仓分支，不受衰减
  D8 分层顺序：冷却拦截优先（衰减不触发）→ 冷却后顺势放行且衰减
运行：python _probe_v11.py   （无网络、无 LLM，纯引擎层）
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
CFG.SINGLE_ORDER_PERCENT = 20     # 放宽单笔上限，让探针 qty=5 不被截断
CFG.MAX_POSITION_PERCENT = 30
CFG.OVERALL_LOSS_LIMIT_PERCENT = 50.0
CFG.DAILY_LOSS_LIMIT_PERCENT = 50.0
CFG.MAX_LEVERAGE = 10
CFG.ALLOW_SHORT = True
CFG.TREND_FILTER_LEVEL = "llm"     # 隔离：不测引擎趋势拦截
CFG.ATR_STOP_ENABLED = True
CFG.ATR_STOP_MULT = 1.5
CFG.TRAILING_TP_ACTIVATE = 0.02
CFG.TRAILING_TP_RATIO = 0.05
CFG.TRAILING_TP_INTRABAR = True
CFG.POST_LOSS_COOLDOWN_BARS = 0
CFG.REENTRY_GATE_MODE = "trend"
CFG.REENTRY_MAX_PREMIUM = 0.02
# 本次测试对象：乖离衰减三参数（默认 8.0 / 0.5 / 2）
CFG.CHASE_DEV_START_PCT = 8.0
CFG.CHASE_DEV_DECAY_FACTOR = 0.5
CFG.CHASE_DEV_MAX_LEVERAGE = 2

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

def buy(ex, price, ema_slow=1.0, qty=5.0, lev=3, ts=TS, skip_reentry=False,
        macd_dif=1.0):
    """开多决策：MACD 多头排列放行引擎层。"""
    return ex.execute_decision(
        {"action": "BUY", "quantity_percent": qty, "leverage": lev,
         "reasoning_summary": "probe"},
        price, ts, ema_slow=ema_slow, macd_dif=macd_dif, macd_dea=0.0,
        atr_pct=None, skip_reentry=skip_reentry)

def sell(ex, price, ema_slow=1.0, qty=5.0, lev=3, ts=TS, skip_reentry=False):
    """开空决策：MACD 空头排列放行引擎层。"""
    return ex.execute_decision(
        {"action": "SELL", "quantity_percent": qty, "leverage": lev,
         "reasoning_summary": "probe"},
        price, ts, ema_slow=ema_slow, macd_dif=-1.0, macd_dea=0.0,
        atr_pct=None, skip_reentry=skip_reentry)

def decay_stat(ex):
    return ex.stats.get("chase_dev_decay", 0)

# =====================================================================
print("== D1 乖离未超阈值（+4% < 8%）：原尺寸开仓，不衰减 ==")
ex = new_ex()
r = buy(ex, 104.0, ema_slow=100.0, qty=5.0, lev=3)
check("D1a BUY 正常成交", r["action"] == "BUY" and ex.position is not None,
      f"action={r['action']}")
check("D1b 保证金=5% 原尺寸（≈500）", abs(ex.position["margin"] - 500.0) < 1e-6,
      f"margin={ex.position['margin']:.2f}")
check("D1c 杠杆=3 不变", ex.position.get("leverage") == 3,
      f"lev={ex.position.get('leverage')}")
check("D1d 衰减计数=0", decay_stat(ex) == 0, f"chase={decay_stat(ex)}")

# =====================================================================
print("== D2 乖离 20% > 8%：qty 5→2.5、杠杆 3→2、计数+1 ==")
ex = new_ex()
r = buy(ex, 120.0, ema_slow=100.0, qty=5.0, lev=3)
check("D2a BUY 仍成交（不拦方向）", r["action"] == "BUY" and ex.position is not None,
      f"action={r['action']} reason={r['reason'][:70]}")
check("D2b 保证金减半=2.5%（≈250）", abs(ex.position["margin"] - 250.0) < 1e-6,
      f"margin={ex.position['margin']:.2f}")
check("D2c 杠杆压到 ≤2", ex.position.get("leverage") == 2,
      f"lev={ex.position.get('leverage')}")
check("D2d 衰减计数=1", decay_stat(ex) == 1, f"chase={decay_stat(ex)}")
check("D2e reason 注明衰减", "衰减" in r["reason"],
      f"reason={r['reason'][:80]}")

# =====================================================================
print("== D3 阈值边界：略低于 8% 不衰减、略高于 8% 衰减 ==")
ex = new_ex()
r = buy(ex, 107.9, ema_slow=100.0, qty=5.0, lev=3)    # dev 7.9% < 8%
check("D3a 7.9% 不衰减", abs(ex.position["margin"] - 500.0) < 1e-6
      and decay_stat(ex) == 0,
      f"margin={ex.position['margin']:.2f} chase={decay_stat(ex)}")
ex2 = new_ex()
r2 = buy(ex2, 108.3, ema_slow=100.0, qty=5.0, lev=3)  # dev 8.3% > 8%
check("D3b 8.3% 衰减", abs(ex2.position["margin"] - 250.0) < 1e-6
      and decay_stat(ex2) == 1,
      f"margin={ex2.position['margin']:.2f} chase={decay_stat(ex2)}")

# =====================================================================
print("== D4 参数覆写生效：factor=0.3 / max_lev=1 ==")
CFG.CHASE_DEV_DECAY_FACTOR = 0.3
CFG.CHASE_DEV_MAX_LEVERAGE = 1
ex = new_ex()
r = buy(ex, 120.0, ema_slow=100.0, qty=5.0, lev=5)
check("D4a qty 5→1.5、杠杆 5→1", abs(ex.position["margin"] - 150.0) < 1e-6
      and ex.position.get("leverage") == 1,
      f"margin={ex.position['margin']:.2f} lev={ex.position.get('leverage')}")
CFG.CHASE_DEV_DECAY_FACTOR = 0.5
CFG.CHASE_DEV_MAX_LEVERAGE = 2

# =====================================================================
print("== D5 空头镜像：追空同样衰减 ==")
ex = new_ex()
r = sell(ex, 80.0, ema_slow=100.0, qty=5.0, lev=3)    # dev_short=20% → 衰减
check("D5a 追空衰减（80 vs 趋势线100）",
      ex.position is not None and abs(ex.position["margin"] - 250.0) < 1e-6
      and decay_stat(ex) == 1,
      f"action={r['action']} margin={ex.position['margin'] if ex.position else 0:.2f}")
ex2 = new_ex()
r2 = sell(ex2, 97.0, ema_slow=100.0, qty=5.0, lev=3)  # dev_short=3% → 不衰减
check("D5b 追空小幅（3%）不衰减", ex2.position is not None
      and abs(ex2.position["margin"] - 500.0) < 1e-6 and decay_stat(ex2) == 0,
      f"margin={ex2.position['margin']:.2f} chase={decay_stat(ex2)}")

# =====================================================================
print("== D6 skip_reentry 路径：settle 阶段不二次衰减 ==")
ex = new_ex()
r = buy(ex, 120.0, ema_slow=100.0, qty=5.0, lev=3, skip_reentry=True)
# 模拟 next_open settle（决策根已衰减并存了衰减后尺寸）→ 不重复衰减
check("D6 skip_reentry 下不衰减（原尺寸 500/3x）",
      ex.position is not None and abs(ex.position["margin"] - 500.0) < 1e-6
      and ex.position.get("leverage") == 3 and decay_stat(ex) == 0,
      f"margin={ex.position['margin']:.2f} lev={ex.position.get('leverage')} "
      f"chase={decay_stat(ex)}")

# =====================================================================
print("== D7 加仓分支不衰减（持仓中 BUY 不受影响）==")
ex = new_ex()
buy(ex, 104.0, ema_slow=100.0, qty=5.0, lev=3)        # 先开多 @104（dev 4%）
before = ex.position["margin"]
r = buy(ex, 120.0, ema_slow=100.0, qty=5.0, lev=3)    # 持仓中再加仓（dev 20%，走加仓分支）
check("D7a 加仓成功", r["action"] == "BUY")
check("D7b 加仓不衰减（保证金全额增加 ~500，计数=0）",
      ex.position["margin"] > before + 450 and decay_stat(ex) == 0,
      f"margin {before:.0f}→{ex.position['margin']:.0f} chase={decay_stat(ex)}")

# =====================================================================
print("== D8 分层顺序：冷却拦截优先 → 冷却后顺势放行且衰减 ==")
CFG.POST_LOSS_COOLDOWN_BARS = 3
ex = new_ex()
buy(ex, 104.0, ema_slow=100.0)                         # bar=1 开多
ex.force_close(100.0, TS, reason="probe止损")          # last_exit{long,100} bar=1
r = buy(ex, 120.0, ema_slow=100.0, qty=5.0, lev=3)    # 冷却期内（bar_since=0<3）→ 拦
check("D8a 冷却拦截优先于衰减（衰减不触发）",
      r["action"] == "HOLD" and "冷却" in r["reason"] and decay_stat(ex) == 0,
      f"action={r['action']} reason={r['reason'][:40]} chase={decay_stat(ex)}")
ex._bar_counter = 10                                   # 模拟已过冷却期
r = buy(ex, 120.0, ema_slow=100.0, qty=5.0, lev=3)    # 顺势(120≥100) 放行 + dev20% 衰减
m = ex.position["margin"] if ex.position else 0.0
check("D8b 冷却后顺势放行且衰减生效",
      ex.position is not None and abs(m - 250.0) < 25.0     # 权益因止损微降，容差 ±25
      and decay_stat(ex) == 1 and "衰减" in r["reason"],
      f"action={r['action']} margin={m:.2f} "
      f"chase={decay_stat(ex)} reason={r['reason'][:60]}")
CFG.POST_LOSS_COOLDOWN_BARS = 0

# =====================================================================
print(f"\n==== 结果：{passed} 通过 / {failed} 失败 ====")
sys.exit(1 if failed else 0)
