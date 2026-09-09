# -*- coding: utf-8 -*-
"""v16: deterministic probes for confirmed spot pyramiding/de-risking."""
import logging
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
logging.disable(logging.CRITICAL)

from config import CFG
from order_executor import OrderExecutor
from spot_policy import apply_spot_sizing

CFG.TRADING_MODE = "spot"
CFG.SPOT_SIZING_MODE = "confirmed"
CFG.SIMULATE_COSTS = False
CFG.TREND_FILTER_LEVEL = "llm"
CFG.SPOT_MAX_EXPOSURE_PERCENT = 75.0
CFG.SPOT_SINGLE_ORDER_PERCENT = 25.0
CFG.SPOT_RISK_BUDGET_PERCENT = 100.0
CFG.SPOT_ADD_MIN_BARS = 2

passed = failed = 0
ts = datetime(2024, 1, 1)


def check(name, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        print(f"PASS {name}")
    else:
        failed += 1
        print(f"FAIL {name}: {detail}")


def raw(action="HOLD"):
    return {"action": action, "quantity_percent": 99, "leverage": 10,
            "confidence_level": "低", "stop_loss_price": None,
            "reasoning_summary": "probe"}


def market(close, **overrides):
    base = {"close": close, "prev_close": close - 1, "ema_trend": close - 3,
            "ema_trend_prev": close - 4, "ema_slow": close - 10,
            "ema_slow_prev": close - 11, "macd_dif": 2, "macd_dea": 1,
            "macd_hist": 2, "macd_hist_prev": 1.5, "atr14": 2,
            "atr_pct": 2 / close * 100, "high_prev": close - 1,
            "high_20_prev": close + 10, "low_10_prev": close - 3,
            "low_5_prev": close - 4}
    base.update(overrides)
    return base


def size(ex, action, price, m):
    return apply_spot_sizing(raw(action), ex, price,
                             ema_slow=m["ema_slow"],
                             macd_dif=m["macd_dif"], macd_dea=m["macd_dea"],
                             market=m)


ex = OrderExecutor(capital=100000, state_path=None)
d = size(ex, "HOLD", 100, market(100))
check("C1 flat HOLD stays flat", d["action"] == "HOLD")

d = size(ex, "BUY", 100, market(100))
check("C2 source BUY opens 10% probe",
      d["action"] == "BUY" and abs(d["quantity_percent"] - 10) < 1e-9)
ex.execute_decision(d, 100, ts, ema_slow=90, macd_dif=2, macd_dea=1, atr_pct=2)

ex._bar_counter = ex.position["last_add_bar"] + 2
d = size(ex, "HOLD", 99, market(99))
check("C3 losing position cannot add", d["action"] == "HOLD")

m = market(115, high_20_prev=120)
d = size(ex, "HOLD", 115, m)
check("C4 10 to 25 after confirmation",
      d["action"] == "BUY" and abs(d["spot_target_percent"] - 25) < 1e-9, d)
ex.execute_decision(d, 115, ts + timedelta(days=1), ema_slow=m["ema_slow"],
                    macd_dif=2, macd_dea=1, atr_pct=m["atr_pct"])

d = size(ex, "HOLD", 120, market(120, high_20_prev=119))
check("C5 minimum add interval blocks", d["action"] == "HOLD")

ex._bar_counter = ex.position["last_add_bar"] + 2
m = market(125, high_20_prev=124)
d = size(ex, "HOLD", 125, m)
check("C6 25 to 50 requires 20-bar breakout",
      d["action"] == "BUY" and abs(d["spot_target_percent"] - 50) < 1e-9, d)
ex.execute_decision(d, 125, ts + timedelta(days=2), ema_slow=m["ema_slow"],
                    macd_dif=2, macd_dea=1, atr_pct=m["atr_pct"])

ex._bar_counter = ex.position["last_add_bar"] + 2
m = market(135, high_prev=134, low_5_prev=130, ema_trend=132)
d = size(ex, "HOLD", 135, m)
check("C7 50 to 75 after pullback recovery",
      d["action"] == "BUY" and abs(d["spot_target_percent"] - 75) < 1e-9, d)
ex.execute_decision(d, 135, ts + timedelta(days=3), ema_slow=m["ema_slow"],
                    macd_dif=2, macd_dea=1, atr_pct=m["atr_pct"])

m = market(130, prev_close=129, ema_trend=131, ema_trend_prev=130,
           ema_slow=120, ema_slow_prev=119, macd_hist=0.5, macd_hist_prev=1.0,
           low_10_prev=110)
d = size(ex, "HOLD", 130, m)
check("C8 fast weakness reduces one level",
      d["action"] == "SELL" and abs(d["spot_target_percent"] - 50) < 1e-9, d)
ex.execute_decision(d, 130, ts + timedelta(days=4), ema_slow=120)

m = market(108, prev_close=109, ema_slow=110, ema_slow_prev=111,
           ema_trend=109, ema_trend_prev=110, low_10_prev=100, atr14=2)
d = size(ex, "HOLD", 108, m)
check("C9 two closes under EMA50 reduce to 10%",
      d["action"] == "SELL" and abs(d["spot_target_percent"] - 10) < 1e-9, d)
ex.execute_decision(d, 108, ts + timedelta(days=5), ema_slow=110)

m = market(95, low_10_prev=96, ema_slow=100, ema_slow_prev=101)
d = size(ex, "HOLD", 95, m)
check("C10 structural break closes", d["action"] == "CLOSE", d)
ex.execute_decision(d, 95, ts + timedelta(days=6), ema_slow=100)

CFG.SPOT_RISK_BUDGET_PERCENT = 2.0
risk_ex = OrderExecutor(capital=100000, state_path=None)
m = market(100, low_10_prev=60, ema_slow=90, ema_slow_prev=89, atr14=2)
d = size(risk_ex, "BUY", 100, m)
check("C11 risk budget compresses probe",
      d["action"] == "BUY" and abs(d["quantity_percent"] - 5) < 1e-9
      and risk_ex.stats["spot_confirmed_risk_cap"] == 1, d)

check("C12 spot stays 1x and long-only",
      d["leverage"] == 1 and risk_ex.max_leverage == 1 and not risk_ex.allow_short)
cash0 = risk_ex.cash
check("C13 no funding or liquidation in spot",
      risk_ex.apply_funding_rate(0.1, 100, ts) == 0
      and risk_ex.check_liquidation(0.01, ts) is False and risk_ex.cash == cash0)

CFG.SPOT_RISK_BUDGET_PERCENT = 100.0
sell_ex = OrderExecutor(capital=100000, state_path=None)
m = market(100)
d = size(sell_ex, "BUY", 100, m)
sell_ex.execute_decision(d, 100, ts, ema_slow=90, macd_dif=2, macd_dea=1, atr_pct=2)
sell_ex._bar_counter = sell_ex.position["last_add_bar"] + 2
m = market(115, high_20_prev=120)
d = size(sell_ex, "SELL", 115, m)
check("C14 overheat SELL cannot pre-empt healthy confirmed add",
      d["action"] == "BUY" and abs(d["spot_target_percent"] - 25) < 1e-9, d)

print(f"\nRESULT {passed}/{passed + failed} passed")
raise SystemExit(1 if failed else 0)
