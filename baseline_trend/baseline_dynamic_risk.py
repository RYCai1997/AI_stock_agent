# -*- coding: utf-8 -*-
"""动态风险基线实验（#48 结论落地）：风险随趋势强度自适应，验证「肥尾+有界回撤」。

#48 结论：趋势基线=牛市跟随器，固定档两头不讨好——固定 2% 吃不到牛市、
固定 10% 熊市回撤失控、固定 25% 闸门牛市误伤尾部利润。
→ 指向动态风险：趋势向上放大（5-10%）、无趋势/向下收窄（2%）。

本实验把风险从「固定档」改为「开仓时刻按趋势强度分档」（信号完全不变，
仅定仓 risk 自适应），并用同一 9 窗口 × 内存回放验证：
  R_fixed_2/5/10：固定档对照（直接复用 #48 已冻结口径，仅重跑取值）
  R_dyn：EMA20 斜率强度分档 2/5/10%
  R_dyn+G25：动态风险 + 25% 账户闸门（Codex #6 语义，高水位永不因平仓重置）

机制：开仓当日 ema_slope_pct = ema_slope/close（EMA 20日变化幅度，%）
  slope_pct >=  8 → 强趋势向上 → risk 10%（满仓参与牛市）
  slope_pct >=  3 → 温和向上   → risk  5%（半仓）
  其余（仍满足 slope>0 入场门槛，但弱）→ risk  2%（保守/熊市反弹假信号自动降档）

零 API。用法：.venv/Scripts/python.exe baseline_dynamic_risk.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "llm_trading_agent"))

import pandas as pd

from baseline_risk_scan import (  # noqa: E402  复用冻结口径：指标、窗口、成本常量
    ATR_N, EMA_N, SLOPE_LOOKBACK, STOP_ATR_MULT, MAX_LEV,
    FUNDING_PER_DAY, FEE_SLIP_RATE, WINDOWS, fetch_df,
)

# ---- 动态分档（主假设）----
SLOPE_HI = 8.0    # EMA20 变化 ≥8% → 强趋势（10%）
SLOPE_MID = 3.0   # EMA20 变化 ≥3% → 温和趋势（5%）；其余 2%
RISK_HI, RISK_MID, RISK_LO = 0.10, 0.05, 0.02


def risk_for_slope_pct(slope_pct: float) -> float:
    if slope_pct >= SLOPE_HI:
        return RISK_HI
    if slope_pct >= SLOPE_MID:
        return RISK_MID
    return RISK_LO


def run_dynamic(d, start, end, gate_pct: float | None, initial: float = 100_000.0,
                collect_sizing: bool = False) -> dict:
    """动态风险版 run_one：信号与冻结基线逐位一致，仅开仓 risk 随趋势强度分档。

    与 baseline_risk_scan.run_one 的差异点：
      1. entry 时 risk = risk_for_slope_pct(slope_pct)（slope_pct 用 prev 收盘口径）
      2. collect_sizing=True 时记录每次开仓的 (risk, slope_pct) 供审计
    其余（EMA50+斜率信号、1.5×ATR 止损、next_open、成本、资金费率、
    高水位永不重置的账户闸门）完全相同。
    """
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
            exit_sig = False                      # 熔断后不再新开仓
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
            # ── 动态风险：按开仓日趋势强度分档 ──
            slope_pct = float(prev["ema_slope"]) / float(prev["close"]) * 100.0
            risk = risk_for_slope_pct(slope_pct)
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
                    sizing.append({"risk": risk, "slope_pct": round(slope_pct, 2)})

        # ---- 收盘市值权益 + 账户高水位/回撤 ----
        eq = cash + (pos["qty"] * (price_now - pos["entry"]) if pos else 0.0)
        if eq > eq_peak:
            eq_peak = eq
        dd = (eq / eq_peak - 1.0) * 100.0
        if dd < max_dd:
            max_dd = dd

        # ---- 账户闸门：回撤触线 → 熔断 ----
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
        from collections import Counter
        c = Counter(s["risk"] for s in sizing)
        res["sizing"] = dict(sorted(c.items()))
        res["slope_range"] = (min(s["slope_pct"] for s in sizing),
                              max(s["slope_pct"] for s in sizing)) if sizing else None
    return res


def fmt_pf(p):
    return "∞" if p == float("inf") else f"{p:.2f}"


def main():
    cache = {}
    for label, sym, s, e in WINDOWS:
        cache[label] = fetch_df(sym, s, e)
        print(f"[行情] {label} {sym} {s}→{e} 预取完成 {len(cache[label])} 根", flush=True)

    from baseline_risk_scan import run_one  # 固定档复用（2% = 冻结基线口径）
    FIXED = [(0.02, "固定2%"), (0.05, "固定5%"), (0.10, "固定10%")]
    DYN = [("dyn", "动态2-5-10", None), ("dyn+g25", "动态+25%闸", 25.0)]

    out = []
    out.append("# 动态风险基线实验（#48 结论落地）\n")
    out.append("信号与冻结基线逐位一致（EMA50+20日斜率趋势跟随、1.5×ATR止损、next_open 成交、"
               "taker0.04%+滑点2bp/边、多头资金费率0.03%/日、独立账本）。\n")
    out.append("**动态分档**（开仓日 EMA20 变化幅度 slope_pct = ema_slope/close）："
               f"`≥{SLOPE_HI:.0f}%→10%`、`≥{SLOPE_MID:.0f}%→5%`、`其余→2%`"
               "——趋势强放大参与、弱/假信号自动降档。\n")
    out.append("25% 闸门 = Codex #6 语义：高水位**永不因平仓重置**，触线熔断停止交易。\n")

    agg_fixed = {0.02: [], 0.05: [], 0.10: []}   # 汇总收益(牛窗均值/熊窗均值)用
    agg_dyn = {"dyn": [], "dyn+g25": []}

    for label, sym, s, e in WINDOWS:
        d = cache[label]
        seg = d[(d["open_time"].dt.date >= pd.Timestamp(s).date())
                & (d["open_time"].dt.date <= pd.Timestamp(e).date())]
        bh = (float(seg.iloc[-1]["close"]) / float(seg.iloc[0]["close"]) - 1.0) * 100.0
        is_bear = "熊" in label
        out.append(f"\n### {label}（B&H {bh:+.0f}%）\n")
        out.append("| 配置 | 收益 | 最大回撤 | 收益/回撤 | 回合 | 胜率 | PF | 触发熔断 | 开仓档位分布 |")
        out.append("|---|---|---|---|---|---|---|---|---|")

        for risk, name in FIXED:
            r = run_one(d, s, e, risk, None)
            out.append(f"| {name} | {r['ret']:+.1f}% | {r['max_dd']:.1f}% | {r['calmar']:.2f} | "
                       f"{r['rounds']} | {r['win_rate']:.0f}% | {fmt_pf(r['pf'])} |  | — |")
            agg_fixed[risk].append(r["ret"])

        for tag, name, gate in DYN:
            r = run_dynamic(d, s, e, gate, collect_sizing=True)
            if "error" in r:
                out.append(f"| {name} | error: {r['error']} |")
                continue
            dist = " / ".join(f"{k*100:.0f}%×{v}" for k, v in sorted(r["sizing"].items()))
            trig = "是" if (gate and r["gate"]) else ""
            out.append(f"| **{name}** | {r['ret']:+.1f}% | {r['max_dd']:.1f}% | {r['calmar']:.2f} | "
                       f"{r['rounds']} | {r['win_rate']:.0f}% | {fmt_pf(r['pf'])} | {trig} | {dist} |")
            agg_dyn[tag].append(r["ret"])

    # ---------- 聚合对照 ----------
    def _mean(xs):
        return sum(xs) / len(xs)

    out.append("\n## 聚合对照（9 窗口收益均值）\n")
    out.append("| 配置 | 全部9窗 | 6牛市窗 | 3熊市窗 |")
    out.append("|---|---|---|---|")
    bull_i = [i for i, (lab, *_ ) in enumerate(WINDOWS) if "熊" not in lab]
    bear_i = [i for i, (lab, *_ ) in enumerate(WINDOWS) if "熊" in lab]
    for risk, name in FIXED:
        v = agg_fixed[risk]
        out.append(f"| {name} | {_mean(v):+.1f}% | {_mean([v[i] for i in bull_i]):+.1f}% | "
                   f"{_mean([v[i] for i in bear_i]):+.1f}% |")
    for tag, name, _g in DYN:
        v = agg_dyn[tag]
        out.append(f"| **{name}** | {_mean(v):+.1f}% | {_mean([v[i] for i in bull_i]):+.1f}% | "
                   f"{_mean([v[i] for i in bear_i]):+.1f}% |")

    body = "\n".join(out)
    print(body)
    runs = os.path.join(os.path.dirname(os.path.abspath(__file__)), "runs")
    os.makedirs(runs, exist_ok=True)
    with open(os.path.join(runs, "dynamic_risk_report.md"), "w", encoding="utf-8") as f:
        f.write(body + "\n")
    print(f"\n[done] runs/dynamic_risk_report.md")


if __name__ == "__main__":
    main()
