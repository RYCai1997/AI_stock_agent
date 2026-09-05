# -*- coding: utf-8 -*-
"""v15: spot mode, confidence sizing, no-short/no-funding/no-liquidation probes."""
import logging
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
logging.disable(logging.CRITICAL)

from config import CFG
from order_executor import OrderExecutor
import prompt_builder
from spot_policy import apply_spot_sizing

CFG.TRADING_MODE = "spot"
CFG.SIMULATE_COSTS = False
CFG.TREND_FILTER_LEVEL = "llm"
CFG.SPOT_MAX_EXPOSURE_PERCENT = 75.0
CFG.SPOT_SINGLE_ORDER_PERCENT = 25.0

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


def raw(action="BUY", conf="中", qty=99, lev=10):
    return {"action": action, "quantity_percent": qty, "leverage": lev,
            "confidence_level": conf, "stop_loss_price": None,
            "reasoning_summary": "probe"}


ex = OrderExecutor(capital=100000, state_path=None)
check("S1 spot forces 1x and no short", ex.max_leverage == 1 and not ex.allow_short)

d = apply_spot_sizing(raw(conf="低"), ex, 100, 90, 2, 1)
check("S2 low-confidence BUY is HOLD", d["action"] == "HOLD" and d["quantity_percent"] == 0)

d = apply_spot_sizing(raw(conf="中"), ex, 100, 90, 2, 1)
check("S3 first medium/high entry is 10% probe", d["action"] == "BUY" and abs(d["quantity_percent"] - 10) < 1e-9)
ex.execute_decision(d, 100, ts, ema_slow=90, macd_dif=2, macd_dea=1)
check("S4 probe exposure is 10%", abs(ex.exposure_pct(100) - 10) < 1e-6)

d = apply_spot_sizing(raw(conf="高"), ex, 99, 90, 2, 1)
check("S5 losing position cannot add", d["action"] == "HOLD")

d = apply_spot_sizing(raw(conf="高"), ex, 110, 100, 2, 1)
ex.execute_decision(d, 110, ts + timedelta(days=1), ema_slow=100, macd_dif=2, macd_dea=1)
check("S6 confirmed high advances to 25%", abs(ex.exposure_pct(110) - 25) < 1e-3,
      f"exp={ex.exposure_pct(110)}")

d = apply_spot_sizing(raw(conf="高"), ex, 120, 105, 2, 1)
ex.execute_decision(d, 120, ts + timedelta(days=2), ema_slow=105, macd_dif=2, macd_dea=1)
check("S7 next confirmed high advances to 50%", abs(ex.exposure_pct(120) - 50) < 1e-3,
      f"exp={ex.exposure_pct(120)}")

d = apply_spot_sizing(raw("SELL", "低"), ex, 120, 105, 2, 1)
ex.execute_decision(d, 120, ts + timedelta(days=3), ema_slow=105)
check("S8 low-confidence SELL reduces 10pp", abs(ex.exposure_pct(120) - 40) < 1e-3,
      f"exp={ex.exposure_pct(120)}")

cash0 = ex.cash
paid = ex.apply_funding_rate(0.01, 120, ts + timedelta(days=3, hours=8))
ex.accrue_funding(ts + timedelta(days=4), 120)
check("S9 spot funding is always zero", paid == 0 and ex.cash == cash0)
check("S10 spot never liquidates", ex.check_liquidation(0.01, ts + timedelta(days=4)) is False and ex.position is not None)

d = apply_spot_sizing(raw("SELL", "高"), ex, 120, 105, 2, 1)
ex.execute_decision(d, 120, ts + timedelta(days=5), ema_slow=105)
check("S11 high-confidence SELL closes", ex.position is None)

d = apply_spot_sizing(raw("SELL", "高"), ex, 120, 105, 2, 1)
check("S12 flat SELL cannot open short", d["action"] == "HOLD")

constitution = prompt_builder._select_constitution()
check("S13 spot prompt removes futures semantics",
      "现货多头" in constitution and "不使用杠杆" in constitution and "资金费率" in constitution)

print(f"\nRESULT {passed}/{passed + failed} passed")
raise SystemExit(1 if failed else 0)
