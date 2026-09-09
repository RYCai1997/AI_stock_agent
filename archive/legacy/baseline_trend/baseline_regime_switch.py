# -*- coding: utf-8 -*-
"""牛熊状态切换基线（#48 落地第三版）：EMA200 背景开关定风险档（v3，替代 v1/v2 斜率分档）。

两轮证伪沉淀：
- v1（EMA20 斜率绝对阈值 8/3%）：ETH 熊市 V 型反弹斜率比 BTC 慢牛更陡 → 恰好反向放大。
- v2（A=EMA200 开关 + B=ATR 归一化 slope_norm≥2.2/1.6 分档）：B 是纯负贡献——趋势跟随低换手
  单仓长持，开仓永远发生在趋势刚启动（slope 刚转正=强度最低点），强趋势段发生在持仓中，
  开仓日分档永远落最低档 → v2 退化成固定 2%（9窗 +18.2%）。
- 决定性对照：A-only（EMA200 上 10% / 下 2%）9 窗 +133.8% > 固定 10% +125.3%，
  熊市 -21.6% vs 固定 10% -50.0%（回撤减半）→ **正确形态 = 牛熊两档状态切换，不要连续分档**。
  残余弱点：EMA200 上方横盘（ETH_主升浪 B&H+1%）A-only -15.0% vs 固定10% -6.0% → 10% 档仍有误伤。

本脚本参数化扫描：risk_above ∈ {5,10,15%}（EMA200 上方）× risk_below ∈ {0,2%}（下方/禁入），
× 25% 账户闸门开/关，9 窗 walk-forward 找稳健档位（目标：牛市≈固定10%收益、熊市回撤≤~15-20%）。

信号/成本口径与冻结基线逐位一致（EMA50+20日斜率入场、1.5×ATR止损、next_open、taker+滑点、
资金费率、独立账本）。EMA200 预热 320 天。

用法：../llm_trading_agent/.venv/Scripts/python.exe baseline_regime_switch.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "llm_trading_agent"))

import pandas as pd

from baseline_risk_scan import (
    ATR_N, EMA_N, SLOPE_LOOKBACK, STOP_ATR_MULT, MAX_LEV,
    FUNDING_PER_DAY, FEE_SLIP_RATE, WINDOWS, run_one, fmt_pf,
)
from baseline_dynamic_risk_diag import fetch_df_v2

RISKS_ABOVE = [0.05, 0.10, 0.15]
RISKS_BELOW = [0.0, 0.02]
GATES = [None, 25.0]


def run_regime(d, start, end, risk_above, risk_below, gate_pct=None,
               initial=100_000.0, collect_sizing=False) -> dict:
    """EMA200 开关两档风险。risk_below=0 → EMA200 下方禁入（不满足不持仓）。"""
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

        if pos is not None:
            fund = pos["notional_est"] * FUNDING_PER_DAY
            cash -= fund
            fees += fund

        open_next = float(d.iloc[min(i + 1, len(d) - 1)]["open"])
        ema_ok = (float(prev["close"]) > float(prev["ema"])
                  and float(prev["ema_slope"]) > 0)
        above200 = float(prev["close"]) > float(prev["ema200"])
        exit_sig = pos is not None and (
            float(prev["close"]) < float(prev["ema"])
            or float(prev["close"]) <= pos["stop"])
        if gate_triggered:
            exit_sig = False
        entry_sig = pos is None and ema_ok and above200 and not gate_triggered
        if risk_below > 0:                        # 下方允许开仓（低档）——两档都开
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
            risk = risk_above if above200 else risk_below
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
                    sizing.append({"risk": risk, "above200": above200})

        eq = cash + (pos["qty"] * (price_now - pos["entry"]) if pos else 0.0)
        if eq > eq_peak:
            eq_peak = eq
        dd = (eq / eq_peak - 1.0) * 100.0
        if dd < max_dd:
            max_dd = dd

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

    if pos is not None:
        last = float(d.iloc[-1]["close"])
        pnl = (last - pos["entry"]) * pos["qty"]
        cost = pos["qty"] * last * FEE_SLIP_RATE
        cash += pnl - cost
        fees += cost
        rounds.append(pnl - cost)
        if pnl - cost > 0:
            wins += 1; gross_win += pnl - cost
        else:
            gross_loss += abs(pnl - cost)

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
        from collections import Counter
        res["sizing"] = dict(sorted(Counter(round(s["risk"], 3) for s in sizing).items()))
    return res


def main():
    cache = {}
    for label, sym, s, e in WINDOWS:
        cache[label] = fetch_df_v2(sym, s, e)
        print(f"[行情] {label} 预取 {len(cache[label])} 根", flush=True)

    bull_i = [i for i, (lab, *_) in enumerate(WINDOWS) if "熊" not in lab]
    bear_i = [i for i, (lab, *_) in enumerate(WINDOWS) if "熊" in lab]

    out = []
    out.append("# 牛熊状态切换基线 v3（#48 落地第三版）\n")
    out.append("**形态**：EMA200 背景开关定两档风险（非连续分档）。EMA200 上方 = 牛/强势区用高档，"
               "下方 = 熊/弱势区用低档或禁入。\n")
    out.append("**两轮证伪**：v1 EMA20 斜率绝对阈值（熊市反弹比慢牛陡→反向放大）；v2 A+B 组合"
               "（ATR 归一化分档在低换手趋势跟随上是负贡献——开仓永远在趋势启动点，强度天然最低，"
               "B 把高档开仓全降成低档）。A-only 决定性对照：EMA200 上 10%/下 2% 9窗 +133.8% "
               "> 固定 10% +125.3%，熊市 -21.6% vs -50.0%。\n")
    out.append("本版参数化扫描 risk_above × risk_below × 25%闸门，9 窗 walk-forward 定档。\n")
    out.append("EMA200 预热 320 天。信号/成本与冻结基线逐位一致。\n")

    # 每组合聚合
    combos = []
    for ra in RISKS_ABOVE:
        for rb in RISKS_BELOW:
            for g in GATES:
                combos.append((ra, rb, g))

    rows = []
    for ra, rb, g in combos:
        rets, dds, calms, gates = [], [], [], []
        for label, sym, s, e in WINDOWS:
            d = cache[label]
            r = run_regime(d, s, e, ra, rb, g)
            rets.append(r["ret"]); dds.append(r["max_dd"]); calms.append(r["calmar"])
            gates.append(r["gate"])
        n_trig = sum(gates)
        rows.append({
            "ra": ra, "rb": rb, "gate": g, "rets": rets, "dds": dds, "calms": calms,
            "mean9": sum(rets) / 9, "mean_bull": sum(rets[i] for i in bull_i) / 6,
            "mean_bear": sum(rets[i] for i in bear_i) / 3,
            "worst_bear_dd": min(dds[i] for i in bear_i),
            "mean_calmar": sum(calms) / 9, "n_trig": n_trig,
        })

    # 排序：先看 9 窗 Calmar，再收益
    rows.sort(key=lambda x: (-x["mean_calmar"], -x["mean9"]))
    out.append("\n## 组合扫描（按 9 窗 Calmar 均值排序）\n")
    out.append("| 上方档 | 下方档 | 闸门 | 9窗收益 | 牛市收益 | 熊市收益 | 熊市最差回撤 | 9窗Calmar | 触发闸门 |")
    out.append("|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        ra_txt = f"{r['ra']*100:.0f}%"
        rb_txt = "禁入" if r["rb"] == 0 else f"{r['rb']*100:.0f}%"
        g_txt = "25%闸" if r["gate"] else "无"
        out.append(f"| {ra_txt} | {rb_txt} | {g_txt} | {r['mean9']:+.1f}% | {r['mean_bull']:+.1f}% | "
                   f"{r['mean_bear']:+.1f}% | {r['worst_bear_dd']:.1f}% | {r['mean_calmar']:.2f} | "
                   f"{r['n_trig']} |")

    # 最优档细表
    best = rows[0]
    out.append(f"\n## 最优档细表（{best['ra']*100:.0f}% / "
               f"{'禁入' if best['rb']==0 else str(best['rb']*100)+'%'} / "
               f"{'25%闸' if best['gate'] else '无闸'}）\n")
    out.append("| 窗口 | 收益 | 最大回撤 | Calmar | 触发闸门 |")
    out.append("|---|---|---|---|---|")
    for i, (label, sym, s, e) in enumerate(WINDOWS):
        d = cache[label]
        r = run_regime(d, s, e, best["ra"], best["rb"], best["gate"], collect_sizing=True)
        trig = "是" if r["gate"] else ""
        out.append(f"| {label} | {r['ret']:+.1f}% | {r['max_dd']:.1f}% | {r['calmar']:.2f} | {trig} |")

    body = "\n".join(out)
    print(body)
    runs = os.path.join(os.path.dirname(os.path.abspath(__file__)), "runs")
    os.makedirs(runs, exist_ok=True)
    with open(os.path.join(runs, "regime_switch_report.md"), "w", encoding="utf-8") as f:
        f.write(body + "\n")
    print("\n[done] runs/regime_switch_report.md")


if __name__ == "__main__":
    main()
