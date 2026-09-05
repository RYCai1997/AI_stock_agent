# -*- coding: utf-8 -*-
"""v14 探针：funding 空仓断点、负费率、状态保存与闭合K线过滤。"""
import logging
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
logging.disable(logging.CRITICAL)

import data_fetcher
from config import CFG
from order_executor import OrderExecutor

CFG.TRADING_MODE = "futures"  # 本探针专测永续 funding；现货模式本应不计 funding

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {detail}")


def new_ex(rate=0.001, state_path=None):
    CFG.CAPITAL = 100000.0
    CFG.SINGLE_ORDER_PERCENT = 20
    CFG.MAX_POSITION_PERCENT = 30
    CFG.OVERALL_LOSS_LIMIT_PERCENT = 50.0
    CFG.EQUITY_DRAWDOWN_LIMIT_PERCENT = 50.0
    CFG.DAILY_LOSS_LIMIT_PERCENT = 50.0
    CFG.MAX_LEVERAGE = 10
    CFG.ALLOW_SHORT = True
    CFG.TREND_FILTER_LEVEL = "llm"
    CFG.POST_LOSS_COOLDOWN_BARS = 0
    CFG.SIMULATE_COSTS = False
    CFG.CHASE_DEV_START_PCT = 999.0
    CFG.FUNDING_RATE_PER_8H = rate
    ex = OrderExecutor(capital=100000.0, state_path=state_path)
    ex._bar_counter = 100
    return ex


def buy(ex, price, ts):
    return ex.execute_decision(
        {"action": "BUY", "quantity_percent": 5, "leverage": 2,
         "reasoning_summary": "probe"},
        price, ts, ema_slow=price - 1, macd_dif=1, macd_dea=0)


print("== F1 空仓期不得补收 funding ==")
t0 = datetime(2024, 1, 1, 0, 0)
ex = new_ex()
buy(ex, 100.0, t0)
ex.accrue_funding(t0 + timedelta(hours=9), 100.0)
ex.force_close(100.0, t0 + timedelta(hours=10), "probe")
first_paid = ex.stats["funding_paid"]
t1 = t0 + timedelta(days=30)
buy(ex, 100.0, t1)
second_notional = abs(ex.position["qty"] * 100.0)
ex.accrue_funding(t1 + timedelta(hours=9), 100.0)
expected_paid = first_paid + second_notional * 0.001
check("F1a 两次各只计1个周期", abs(ex.stats["funding_paid"] - expected_paid) < 1e-9,
      f"paid={ex.stats['funding_paid']}")
check("F1b 空仓后新仓锚点已重置", abs(first_paid - 10.0) < 1e-9)

print("== F2 负 funding 方向 ==")
ex = new_ex(rate=-0.001)
buy(ex, 100.0, t0)
ex.accrue_funding(t0 + timedelta(hours=9), 100.0)
check("F2a 负费率时多头收取", abs(ex.mark_to_market(100.0) - 100010.0) < 1e-9)
check("F2b 净成本以负数记录", abs(ex.stats["funding_paid"] + 10.0) < 1e-9)

print("== F3 save_state(None) 空仓权益不重复已实现盈亏 ==")
with tempfile.TemporaryDirectory() as td:
    state_path = Path(td) / "state.json"
    ex = new_ex(rate=0.0, state_path=state_path)
    buy(ex, 100.0, t0)
    ex.force_close(110.0, t0 + timedelta(days=1), "probe")
    ex.save_state()
    saved = __import__("json").loads(state_path.read_text(encoding="utf-8"))
    check("F3 权益等于cash=101000", saved["equity"] == 101000.0 == saved["cash"], str(saved))

print("== F4 只保留闭合K线 ==")
df = pd.DataFrame({
    "open_time": [pd.Timestamp("2024-01-01 08:00"), pd.Timestamp("2024-01-02 08:00")],
    "open": [1, 2], "high": [1, 2], "low": [1, 2], "close": [1, 2], "volume": [1, 2],
})
closed = data_fetcher.closed_klines_only(df, "1d", now=pd.Timestamp("2024-01-03 07:59"))
check("F4 未到闭合时点的最后一根被过滤", len(closed) == 1)

print("== F5 next-open 待办可跨重启恢复 ==")
with tempfile.TemporaryDirectory() as td:
    state_path = Path(td) / "state.json"
    ex = new_ex(rate=0.0, state_path=state_path)
    ex.execute_decision(
        {"action": "BUY", "quantity_percent": 5, "leverage": 2,
         "reasoning_summary": "probe pending"},
        100.0, pd.Timestamp(t0), ema_slow=99.0, macd_dif=1, macd_dea=0,
        defer=True)
    ex.set_pending_exit(pd.Timestamp(t0), "probe exit")
    ex.last_processed_bar = str(pd.Timestamp(t0))
    ex.save_state(100.0)
    restored = new_ex(rate=0.0, state_path=state_path)
    restored.load_state()
    check("F5a pending_action/pending_exit 均恢复",
          restored.pending_action is not None and restored.pending_exit is not None)
    check("F5b last_processed_bar 恢复", restored.last_processed_bar == str(pd.Timestamp(t0)))

print("== F6 单个历史 funding 结算点 ==")
ex = new_ex(rate=0.0)
buy(ex, 100.0, t0)
paid = ex.apply_funding_rate(0.001, 110.0, t0 + timedelta(hours=8))
check("F6a 按结算时点市值计费", abs(paid - 11.0) < 1e-9)
received = ex.apply_funding_rate(-0.001, 110.0, t0 + timedelta(hours=16))
check("F6b 负历史费率反向收取", abs(received + 11.0) < 1e-9)
check("F6c 正负结算后净成本归零", abs(ex.stats["funding_paid"]) < 1e-9)

print()
print(f"==== 结果：{passed} 通过 / {failed} 失败 ====")
sys.exit(1 if failed else 0)
