# -*- coding: utf-8 -*-
"""动态风险基线 v2（#48 落地第二版 · A+B 组合）：EMA200 背景开关 + ATR 归一化斜率分档。

v1 证伪教训（见 dynamic_risk_report.md 结论节）：EMA20 斜率**绝对百分比**阈值跨市场不可比——
ETH 熊市 V 型反弹（2022-08，+7~+10.6%）比 BTC 慢牛（3-7%）更陡，恰好反向放大亏损；且短斜率
只能当动能不能当背景。v2 修复：
  A. **EMA200 背景开关**：prev close > prev ema200 才允许放大档；EMA200 下方一律 2%（掐死熊市反弹）。
     EMA200 需 320 天预热（ewm span200 收敛 ~2-3×span，原 110 天不足）。
  B. **ATR 归一化强度**：slope_norm = ema_slope / atr（EMA20 变化量是几倍当前 ATR，无量纲跨市场可比）。
     档位阈值由 751 个 EMA200 上方候选日分布标定（diag_v2）：p50=1.63 → ≥1.6 为 5% 档、
     p75=2.21 → ≥2.2 为 10% 档；EMA200 下方或 slope_norm<1.6 → 2% 档。

信号与成本口径与冻结基线逐位一致（EMA50+20日斜率入场、1.5×ATR 止损、next_open、taker+滑点、
资金费率、独立账本）。25% 闸门 = Codex #6 语义（高水位永不因平仓重置）。

用法：../llm_trading_agent/.venv/Scripts/python.exe baseline_dynamic_risk_v2.py
"""
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "llm_trading_agent"))

import pandas as pd

from baseline_risk_scan import (
    ATR_N, EMA_N, SLOPE_LOOKBACK, STOP_ATR_MULT, MAX_LEV,
    FUNDING_PER_DAY, FEE_SLIP_RATE, WINDOWS, run_one, fmt_pf,
)
from baseline_dynamic_risk_diag import EMA200_N, WARM_DAYS, fetch_df_v2

# ---- v2 分档（diag_v2 数据标定）----
NORM_5 = 1.6    # slope_norm ≥1.6 → 5%
NORM_10 = 2.2   # slope_norm ≥2.2 → 10%
RISK_HI, RISK_MID, RISK_LO = 0.10, 0.05, 0.02


def risk_v2(above200: bool, slope_norm: float) -> float:
    """A 开关（EMA200 下方强制 2%）+ B 归一化强度（≥2.2→10%、≥1.6→5%）。"""
    if not above200:
        return RISK_LO
    if slope_norm >= NORM_10:
        return RISK_HI
    if slope_norm >= NORM_5:
        return RISK_MID
    return RISK_LO


def run_dynamic_v2(d, start, end, gate_pct: float | None, initial: float = 100_000.0,
                   collect_sizing: bool = False) -> dict:
    """信号与冻结基线逐位一致；仅开仓 risk 走 v2 分档（EMA200 背景 + ATR 归一化）。"""
    d = d[(d["open_time"].dt.date >= pd.Timestamp(start).date())
          & (d["open_time"].dt.date <= pd.Timestamp(end).date())].copy()
    if len(d) < EMA_N + SLOPE_LOOKBACK + 5:
        return {"error": f"bars={len(d)} too few"}

    cash = initial
    eq_peak = initial
    max_dd = 0.0
    pos = None
    rounds, wins, gross_win, gross_loss = [], 0, 0.0, 0.0
    fees = 0.0
    gate_triggered = False
    gate_dd = 0.0
    sizing = [] if collect_sizing else None

    for i in range(1, len(d)):
        row_t = d.iloc[i]
        prev = d.iloc[i - 1]
        price_now = float(row_t["close"])

        if pos is not None:                       # 持仓每日资金费率
            fund = pos["notional_est"] * FUNDING_PER_DAY
            cash -= fund
            fees += fund

        # ---- 决策（t-1 收盘信号 → t 开盘执行 = next_open）----
        open_next = float(d.iloc[min(i + 1, len(d) - 1)]["open"])
        ema_ok = (float(prev["close"]) > float(prev["ema"])
                  and float(prev["ema_slope"]) > 0)
        exit_sig = pos is not None and (
            float(prev["close"]) < float(prev["ema"])
            or float(prev["close"]) <= pos["stop"])
        if gate_triggered:
            exit_sig = False
        entry_sig = pos is None and ema_ok and not gate_triggered

        if exit_sig and pos is not None:
            notional_out = pos["qty"] * open_next
            pnl = (open_next - pos["entry"]) * pos["qty"]
            cost = notional_out * FEE_SLIP_RATE
            cash += pnl - cost
            fees += cost
            rounds.append(pnl - cost)
            if pnl - cost > 0:
                wins += 1; gross_win += pnl - cost
            else:
                gross_loss += abs(pnl - cost)
            pos = None
        elif entry_sig and pos is None:
            atr = float(prev["atr"])
            stop_dist = max(0.02, STOP_ATR_MULT * atr / float(prev["close"]))
            eq = cash
            # ── v2 分档：A 开关 + B 归一化 ──
            above200 = float(prev["close"]) > float(prev["ema200"])
            slope_norm = float(prev["ema_slope"]) / atr if atr > 1e-9 else 0.0
            risk = risk_v2(above200, slope_norm)
            notional_target = min(risk * eq / stop_dist, MAX_LEV * eq)
            qty = notional_target / open_next
            cost = notional_target * FEE_SLIP_RATE
            if cash - cost > 1000:
                cash -= cost
                fees += cost
                pos = {"qty": qty, "entry": open_next,
                       "stop": open_next * (1 - stop_dist),
                       "notional_est": notional_target}
                if sizing is not None:
                    sizing.append({"risk": risk, "slope_norm": round(slope_norm, 2),
                                   "above200": above200})

        # ---- 收盘市值权益 + 账户高水位/回撤 ----
        eq = cash + (pos["qty"] * (price_now - pos["entry"]) if pos else 0.0)
        if eq > eq_peak:
            eq_peak = eq
        dd = (eq / eq_peak - 1.0) * 100.0
        if dd < max_dd:
            max_dd = dd

        # ---- 账户闸门 ----
        if gate_pct is not None and not gate_triggered and dd <= -gate_pct:
            gate_triggered = True
            gate_dd = dd
            if pos is not None:
                pnl = (price_now - pos["entry"]) * pos["qty"]
                cost = pos["qty"] * price_now * FEE_SLIP_RATE
                cash += pnl - cost
                fees += cost
                rounds.append(pnl - cost)
                if pnl - cost > 0:
                    wins += 1; gross_win += pnl - cost
                else:
                    gross_loss += abs(pnl - cost)
                pos = None

    if pos is not None:                           # 窗口末仍持仓 → 收盘平掉
        last = float(d.iloc[-1]["close"])
        pnl = (last - pos["entry"]) * pos["qty"]
        cost = pos["qty"] * last * FEE_SLIP_RATE
        cash += pnl - cost
        fees += cost
        rounds.append(pnl - cost)

    ret = (cash / initial - 1.0) * 100.0
    pf = gross_win / gross_loss if gross_loss > 1e-9 else (float("inf") if gross_win > 0 else 0.0)
    calmar = ret / abs(max_dd) if abs(max_dd) > 1e-9 else 0.0
    res = {
        "ret": ret, "max_dd": max_dd, "calmar": calmar,
        "rounds": len(rounds), "wins": wins,
        "pf": pf, "fees": fees, "bars": len(d),
        "win_rate": wins / len(rounds) * 100 if rounds else 0.0,
        "gate": gate_triggered, "gate_dd": gate_dd,
    }
    if sizing is not None:
        res["sizing"] = dict(sorted(Counter(s["risk"] for s in sizing).items()))
    return res


def main():
    cache = {}
    for label, sym, s, e in WINDOWS:
        cache[label] = fetch_df_v2(sym, s, e)
        print(f"[行情] {label} 预取 {len(cache[label])} 根（含 {WARM_DAYS} 天预热）", flush=True)

    FIXED = [(0.02, "固定2%"), (0.05, "固定5%"), (0.10, "固定10%")]
    DYN = [("v2", "v2动态2-5-10", None), ("v2+g25", "v2动态+25%闸", 25.0)]

    out = []
    out.append("# 动态风险基线 v2（#48 落地第二版 · A+B 组合）\n")
    out.append("信号与冻结基线逐位一致。**A 背景开关**：`close>EMA200` 才允许放大档（EMA200 下方一律 "
               "2%，掐死熊市反弹）；**B 归一化分档**：`slope_norm = ema_slope/atr`（无量纲，EMA20 变化量"
               f"为几倍当前 ATR）`≥{NORM_10}→10%`、`≥{NORM_5}→5%`、`其余→2%`。阈值由 751 个 EMA200 "
               "上方候选日分布标定（p50=1.63/p75=2.21）。EMA200 预热 320 天。\n")
    out.append("25% 闸门 = Codex #6 语义：高水位**永不因平仓重置**，触线熔断停止交易。\n")

    agg = {}
    for label, sym, s, e in WINDOWS:
        d = cache[label]
        seg = d[(d["open_time"].dt.date >= pd.Timestamp(s).date())
                & (d["open_time"].dt.date <= pd.Timestamp(e).date())]
        bh = (float(seg.iloc[-1]["close"]) / float(seg.iloc[0]["close"]) - 1.0) * 100.0
        out.append(f"\n### {label}（B&H {bh:+.0f}%）\n")
        out.append("| 配置 | 收益 | 最大回撤 | 收益/回撤 | 回合 | 胜率 | PF | 触发熔断 | 开仓档位分布 |")
        out.append("|---|---|---|---|---|---|---|---|---|")

        for risk, name in FIXED:
            r = run_one(d, s, e, risk, None)
            out.append(f"| {name} | {r['ret']:+.1f}% | {r['max_dd']:.1f}% | {r['calmar']:.2f} | "
                       f"{r['rounds']} | {r['win_rate']:.0f}% | {fmt_pf(r['pf'])} |  | — |")
            agg.setdefault(name, []).append((label, r["ret"], r["max_dd"], r["calmar"]))

        for tag, name, gate in DYN:
            r = run_dynamic_v2(d, s, e, gate, collect_sizing=True)
            if "error" in r:
                out.append(f"| {name} | error: {r['error']} |")
                continue
            dist = " / ".join(f"{k*100:.0f}%×{v}" for k, v in sorted(r["sizing"].items()))
            trig = "是" if (gate and r["gate"]) else ""
            out.append(f"| **{name}** | {r['ret']:+.1f}% | {r['max_dd']:.1f}% | {r['calmar']:.2f} | "
                       f"{r['rounds']} | {r['win_rate']:.0f}% | {fmt_pf(r['pf'])} | {trig} | {dist} |")
            agg.setdefault(name, []).append((label, r["ret"], r["max_dd"], r["calmar"]))

    # 聚合
    out.append("\n## 聚合对照\n")
    out.append("| 配置 | 9窗收益均值 | 6牛市收益均值 | 3熊市收益均值 | 9窗Calmar均值 | 熊市最差回撤 |")
    out.append("|---|---|---|---|---|---|")
    bull_i = [i for i, (lab, *_) in enumerate(WINDOWS) if "熊" not in lab]
    bear_i = [i for i, (lab, *_) in enumerate(WINDOWS) if "熊" in lab]
    for name, rows in agg.items():
        rs = [x[1] for x in rows]
        cs = [x[3] for x in rows]
        bear_dd = [x[2] for x in rows if "熊" in x[0]]
        out.append(f"| {name} | {sum(rs)/9:+.1f}% | {sum(rs[i] for i in bull_i)/6:+.1f}% | "
                   f"{sum(rs[i] for i in bear_i)/3:+.1f}% | {sum(cs)/9:.2f} | "
                   f"{min(bear_dd):.1f}% |")

    body = "\n".join(out)
    print(body)
    runs = os.path.join(os.path.dirname(os.path.abspath(__file__)), "runs")
    os.makedirs(runs, exist_ok=True)
    with open(os.path.join(runs, "dynamic_risk_v2_report.md"), "w", encoding="utf-8") as f:
        f.write(body + "\n")
    print(f"\n[done] runs/dynamic_risk_v2_report.md")


if __name__ == "__main__":
    main()
