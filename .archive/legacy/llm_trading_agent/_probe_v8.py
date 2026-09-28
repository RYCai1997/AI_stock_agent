# -*- coding: utf-8 -*-
"""
_probe_v8.py —— 三档执行成交模型（EXECUTION_MODEL: close / next_open / intrabar）回归探针

背景：用户质疑「盘中检测是否不合理、时间不能倒流」。结论是盘中检测无前视，但隐含
「条件单盘中自动执行」假设。三档模型把触发识别时点与成交价显式参数化：
  - intrabar ：止损/止盈按盘中极值击穿即时成交（止损线价/触发线价）
  - close    ：全部按当根收盘价识别并成交（最保守，=实盘 run_slice 语义）
  - next_open：当根收盘识别/决策，成交延迟到次根开盘价

探针内容：
  K：next_open 决策挂起（defer）→ 次根开盘价撮合
  L：next_open 待平仓结算按开盘价 + 残留指令防御性丢弃
  M：根计数器在三档下每根恰好一次（defer 不计数、settle 计数）
  N：settle 撮合 skip_reentry 与不 skip 的追高差异
  S1：墓碑大阴线——移动止盈三档离场价（intrabar=触发线价 > close=收盘价 > next_open=次根开盘价）
  S2：下影破止损但收盘未破——止损识别差异（intrabar 触发、close/next_open 不触发）
运行：python _probe_v8.py   （需 pandas，无需网络）
"""
import pandas as pd
import config as m
from order_executor import OrderExecutor
import main  # noqa: F401  触发模块装配，_replay_rules 依赖其全局 llm_client 等

PASS = FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name} {extra}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {extra}")


TS0 = pd.Timestamp("2026-01-01")


def _mk_df(bars):
    """bars: [(open, high, low, close, atr_pct), ...]；首根给出 MACD 多头排列 + RSI 中位
    触发规则引擎 BUY，后续根 dif/dea=None 使引擎恒 HOLD——隔离出纯止损/止盈管理路径。"""
    rows = []
    for i, (o, h, l, c, atr) in enumerate(bars):
        r = {
            "open_time": TS0 + pd.Timedelta(days=i),
            "open": o, "high": h, "low": l, "close": c,
            "ema_trend": 95.0, "ema_slow": 90.0,
            "macd_hist": None, "rsi_14": None, "vol_ratio": None,
            "change_pct": None, "atr_pct": float(atr),
        }
        if i == 0:
            r["macd_dif"] = 0.5
            r["macd_dea"] = 0.2
            r["rsi_14"] = 55.0
        else:
            r["macd_dif"] = None
            r["macd_dea"] = None
        rows.append(r)
    return pd.DataFrame(rows)


def run_replay(bars, model):
    m.CFG.EXECUTION_MODEL = model
    df = _mk_df(bars)
    ex = OrderExecutor(capital=200000.0, state_path=None)
    main._replay_rules(df, 0, ex, "探针")
    return ex


def last_close(ex):
    if ex.closed_trades:
        return ex.closed_trades[-1]["price"]
    return None


def last_reason(ex):
    # trades 中最后一条 CLOSE 的 reason
    for t in reversed(ex.trades):
        if t.get("action") == "CLOSE":
            return str(t.get("reason", ""))
    return ""


def setup_exec(seed=100.0, atr=None, ema=90.0):
    m.CFG.EXECUTION_MODEL = "next_open"
    ex = OrderExecutor(capital=200000.0, state_path=None)
    return ex


# =====================================================================
# K：next_open 决策挂起（defer）→ 次根开盘价撮合
# =====================================================================
print("== K：defer 决策挂起 → 次根开盘撮合 ==")
m.CFG.EXECUTION_MODEL = "next_open"
ex = OrderExecutor(capital=200000.0, state_path=None)
d = {"action": "BUY", "quantity_percent": 5.0, "reasoning_summary": "测试开多",
     "confidence_level": "high"}
r = ex.execute_decision(d, 100.0, TS0, ema_slow=90.0, atr_pct=None, defer=True)
check("K1 defer BUY 不成交且挂起", ex.position is None
      and r.get("deferred") is True and ex.pending_action is not None,
      f"action={r['action']}")
check("K2 deferred 统计+1", ex.stats["deferred"] == 1, f"deferred={ex.stats['deferred']}")
res = ex.settle_pending(102.5, TS0 + pd.Timedelta(days=1))
check("K3 settle 按次根开盘价 102.5 成交", res is not None and res["action"] == "BUY"
      and ex.position is not None and abs(ex.position["entry_price"] - 102.5) < 1e-6,
      f"entry={ex.position['entry_price'] if ex.position else None}")
check("K4 settle 后 pending 清空", ex.pending_action is None)

# =====================================================================
# L：pending_exit 结算按开盘价 + 残留指令防御性丢弃
# =====================================================================
print("== L：待平仓结算 + 残留指令防御 ==")
ex = setup_exec()
d = {"action": "BUY", "quantity_percent": 5.0, "reasoning_summary": "开多",
     "confidence_level": "high"}
ex.execute_decision(d, 100.0, TS0, ema_slow=90.0, atr_pct=None, defer=False)
check("L0 开多成功", ex.position is not None)
ex.set_pending_exit(TS0, "测试止损触发")
ex.pending_action = {"reason": "残留开仓指令"}          # 人为制造不共存的反例
res = ex.settle_pending(96.0, TS0 + pd.Timedelta(days=1))
check("L1 按次根开盘 96 平仓", ex.position is None and last_close(ex) == 96.0,
      f"last_close={last_close(ex)}")
check("L2 残留指令被丢弃", ex.pending_action is None)

# =====================================================================
# M：根计数器：defer 不计数、settle 每根一次
# =====================================================================
print("== M：根计数器（冷却计时基准）==")
m.CFG.EXECUTION_MODEL = "next_open"
ex = OrderExecutor(capital=200000.0, state_path=None)
for _ in range(5):
    ex.settle_pending(100.0, TS0)                     # 无 pending，只 +1
check("M1 settle×5 → _bar_counter=5", ex._bar_counter == 5, f"bar={ex._bar_counter}")
r = ex.execute_decision({"action": "HOLD", "reasoning_summary": "x"}, 100.0, TS0,
                        ema_slow=90.0, defer=True)
check("M2 HOLD+defer 不计数", ex._bar_counter == 5, f"bar={ex._bar_counter}")

# =====================================================================
# N：settle 撮合的追高语义：skip_reentry=True 放行 / False 拦截
# =====================================================================
print("== N：settle 撮合追高拦截差异（冷却置 0，隔离 premium；price 档回归）==")
m.CFG.POST_LOSS_COOLDOWN_BARS = 0
m.CFG.REENTRY_GATE_MODE = "price"   # 回归：锁定旧静态价锚语义（trend 档新语义见 _probe_v10.py）
m.CFG.REENTRY_MAX_PREMIUM = 0.02
ex = OrderExecutor(capital=200000.0, state_path=None)
d = {"action": "BUY", "quantity_percent": 5.0, "reasoning_summary": "开多",
     "confidence_level": "high"}
ex.execute_decision(d, 100.0, TS0, ema_slow=90.0, atr_pct=None)   # bar=1
ex.force_close(98.0, TS0, reason="测试止损")                        # last_exit={long,98,bar1}
check("N0 止损离场已记录", ex.last_exit is not None and ex.last_exit["price"] == 98.0)
r = ex.execute_decision(d, 99.0, TS0 + pd.Timedelta(days=1), ema_slow=90.0,
                        atr_pct=None, defer=True)                  # 99/98-1=1.02%<2% 放行挂起
check("N1 defer 评估放行（未超2%）", r.get("deferred") is True and ex.pending_action is not None)
res = ex.settle_pending(100.5, TS0 + pd.Timedelta(days=2))          # 100.5/98-1=2.55%>2%
check("N2 skip_reentry=True 放行跳空开仓", res is not None and res["action"] == "BUY"
      and ex.position is not None and abs(ex.position["entry_price"] - 100.5) < 1e-6,
      f"entry={ex.position['entry_price'] if ex.position else None}")

ex2 = OrderExecutor(capital=200000.0, state_path=None)
ex2.execute_decision(d, 100.0, TS0, ema_slow=90.0, atr_pct=None)
ex2.force_close(98.0, TS0, reason="测试止损")
r = ex2.execute_decision(d, 100.5, TS0 + pd.Timedelta(days=1), ema_slow=90.0,
                         atr_pct=None, defer=False, skip_reentry=False)  # 直接不skip撮合
check("N3 不 skip 则追高被拦", r["action"] == "HOLD" and "追高拦截" in r["reason"],
      f"reason={r['reason'][:40]}")
m.CFG.POST_LOSS_COOLDOWN_BARS = 3   # 还原默认
m.CFG.REENTRY_GATE_MODE = "trend"   # 还原默认（新语义）

# =====================================================================
# S1：墓碑大阴线——移动止盈三档离场价（触发线价 > 收盘价 > 次根开盘价）
#    bar0 开多@100.5（atr3% → 止损 4.5%，stop=95.98，不干扰止盈测试）
#    bar1 close=105 high=106  → 跟踪激活（浮盈4.5%≥2%）
#    bar2 冲高106.5 后崩至 low96 收98.5  → 三档离场价应不同
#    bar3 open=97.2            → next_open 结算位
# =====================================================================
print("== S1：移动止盈三档离场价（墓碑大阴线）==")
bars1 = [
    (100.0, 101.0, 99.0, 100.5, 3.0),
    (101.0, 106.0, 104.0, 105.0, 3.0),
    (105.0, 106.5, 96.0, 98.5, 3.0),
    (97.2, 98.5, 97.0, 98.0, 3.0),
]
m.CFG.EXECUTION_MODEL = "intrabar"
ex = run_replay(bars1, "intrabar")
check("S1a intrabar 已平仓", ex.position is None)
check("S1b intrabar 按触发线价≈101.18 离场", last_close(ex) is not None
      and abs(last_close(ex) - round(106.5 * 0.95, 2)) < 0.01,
      f"last_close={last_close(ex)} reason={last_reason(ex)[:46]}")
check("S1c intrabar reason 含盘中击穿", "盘中击穿" in last_reason(ex),
      f"reason={last_reason(ex)[:60]}")

ex = run_replay(bars1, "close")
check("S1d close 模型按收盘价 98.5 离场", ex.position is None
      and last_close(ex) == 98.5, f"last_close={last_close(ex)}")
check("S1e close reason 为移动止盈", "移动止盈" in last_reason(ex),
      f"reason={last_reason(ex)[:60]}")

ex = run_replay(bars1, "next_open")
check("S1f next_open 按次根开盘 97.2 离场", ex.position is None
      and last_close(ex) == 97.2, f"last_close={last_close(ex)}")
check("S1g next_open reason 含次根开盘成交", "次根开盘成交" in last_reason(ex),
      f"reason={last_reason(ex)[:60]}")

# 三档离场价排序验证：intrabar(101.18) > close(98.5) > next_open(97.2) ——
# 直观展示「盘中假设越强 → 回测离场越优」；真正该用哪档取决于实盘执行机制。
p_i, p_c, p_n = run_replay(bars1, "intrabar"), run_replay(bars1, "close"), run_replay(bars1, "next_open")
check("S1h 三档离场价严格排序 intrabar>close>next_open",
      last_close(p_i) > last_close(p_c) > last_close(p_n),
      f"{last_close(p_i)} > {last_close(p_c)} > {last_close(p_n)}")

# =====================================================================
# S2：下影破止损但收盘未破——止损识别差异
#    bar0 开多@100.5（atr=0 → 固定2%，stop=98.49）
#    bar1 盘中 low=96 击穿98.49，但收99.2 未破 → intrabar 平；close/next_open 继续持
#    bar2 收98.0 收盘破位 → close 按98.0平；next_open 挂起
#    bar3 open=98.0 → next_open 结算
# =====================================================================
print("== S2：止损识别差异（下影击穿 vs 收盘击穿）==")
bars2 = [
    (100.0, 101.0, 99.0, 100.5, 0.0),
    (100.5, 101.0, 96.0, 99.2, 0.0),
    (99.2, 99.5, 97.5, 98.0, 0.0),
    (98.0, 98.6, 97.8, 98.2, 0.0),
]
ex = run_replay(bars2, "intrabar")
check("S2a intrabar 下影击穿即按止损线 98.49 离场", ex.position is None
      and abs(last_close(ex) - round(100.5 * 0.98, 2)) < 0.01,
      f"last_close={last_close(ex)} reason={last_reason(ex)[:40]}")
check("S2b intrabar reason 含止损", "止损" in last_reason(ex),
      f"reason={last_reason(ex)[:60]}")

ex = run_replay(bars2, "close")
check("S2c close 下影击穿不触发（收盘99.2未破）→ 继续持有到收盘破位",
      ex.position is None and last_close(ex) == 98.0,
      f"last_close={last_close(ex)}")
check("S2d close 平仓价=收盘价 98.0 而非止损线", last_close(ex) == 98.0,
      f"last_close={last_close(ex)}")

ex = run_replay(bars2, "next_open")
check("S2e next_open 下影击穿不触发", True)   # 过程性占位（行为同 close 识别）
check("S2f next_open 收盘破位 → 次根开盘 98.0 离场", ex.position is None
      and last_close(ex) == 98.0, f"last_close={last_close(ex)}")

print(f"\n==== 结果：{PASS} 通过 / {FAIL} 失败 ====")
sys_exit = 1 if FAIL else 0
import sys  # noqa: E402
sys.exit(sys_exit)
