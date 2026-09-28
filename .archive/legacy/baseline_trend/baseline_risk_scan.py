# -*- coding: utf-8 -*-
"""风险预算灵敏度扫描（#48）：冻结基线的单笔风险 × 账户级闸门（Codex #6 语义）。

回答：用多大风险换收益？账户闸门（高水位永不重置，回撤超限即熔断停止交易）
能否把「有界回撤」做出来、代价是什么？

维度：
- RISK_PER_TRADE ∈ {2%, 5%, 10%}（单笔风险 = 权益 × risk / 止损距离 → 仓位）
- 账户闸门 ∈ {无, 25%}（账户净值相对历史高水位回撤 ≤ -25% → 熔断：
  有仓按当根收盘价强平，之后禁开新仓 = 策略停止，直到外部重置）

口径与 baseline_trend.py 完全一致（EMA50 + 20日斜率 + ATR 止损 + next_open +
taker0.04%+滑点2bp/边 + 多头资金费率0.03%/日 + 独立正确账本）。行情每窗口预取一次，
9 窗口 × 6 组合全部在内存回放，零额外拉取。

用法：.venv/Scripts/python.exe baseline_risk_scan.py
"""
import os
import sys
import datetime as dt

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "llm_trading_agent"))

import pandas as pd

# ============ 策略参数（与冻结基线一致；全部窗口共用） ============
EMA_N = 50
SLOPE_LOOKBACK = 20
ATR_N = 14
STOP_ATR_MULT = 1.5
MAX_LEV = 3.0
FUNDING_PER_DAY = 0.0003
FEE_SLIP_RATE = 0.0006

RISKS = [0.02, 0.05, 0.10]      # 单笔风险档
GATES = [None, 25.0]            # 账户闸门：None=关；25.0=回撤25%熔断
RISK_DEFAULT = 0.02             # 冻结基线默认档（=baseline_trend_report.md 口径）

WINDOWS = [
    ("SOL_牛23",  "SOLUSDT", "2023-10-01", "2024-03-31"),
    ("SOL_主升浪", "SOLUSDT", "2024-08-01", "2025-01-31"),
    ("SOL_熊22",  "SOLUSDT", "2022-01-01", "2022-12-31"),
    ("BTC_牛23",  "BTCUSDT", "2023-10-01", "2024-03-31"),
    ("BTC_主升浪", "BTCUSDT", "2024-08-01", "2025-01-31"),
    ("BTC_熊22",  "BTCUSDT", "2022-01-01", "2022-12-31"),
    ("ETH_牛23",  "ETHUSDT", "2023-10-01", "2024-03-31"),
    ("ETH_主升浪", "ETHUSDT", "2024-08-01", "2025-01-31"),
    ("ETH_熊22",  "ETHUSDT", "2022-01-01", "2022-12-31"),
]


def fetch_df(symbol, start, end):
    """拉含预热区的整段数据 + 指标（数据只拉一次，各参数档共享）。"""
    from config import CFG
    import data_fetcher
    CFG.SYMBOL = symbol
    CFG.INTERVAL = "1d"
    warm = (pd.Timestamp(start) - pd.Timedelta(days=110)).strftime("%Y-%m-%d")
    df = data_fetcher.fetch_klines(symbol, "1d", start_date=warm, end_date=end)
    if df is None or df.empty:
        raise RuntimeError(f"fetch empty: {symbol}")
    df = df.reset_index(drop=True)
    df["ema"] = df["close"].ewm(span=EMA_N, adjust=False).mean()
    df["ema_slope"] = df["ema"] - df["ema"].shift(SLOPE_LOOKBACK)
    tr = pd.concat([df["high"] - df["low"],
                    (df["high"] - df["close"].shift()).abs(),
                    (df["low"] - df["close"].shift()).abs()], axis=1).max(axis=1)
    df["atr"] = tr.ewm(span=ATR_N, adjust=False).mean()
    return df


def run_one(d, start, end, risk: float, gate_pct: float | None,
            initial: float = 100_000.0) -> dict:
    """单窗口单参数回放。gate_pct=None → 行为与冻结基线完全一致。

    账户闸门语义（与主项目 #43 / Codex #6 对齐）：高水位 eq_peak 永不因平仓重置；
    净值相对高水位回撤 ≤ -gate → 熔断：有仓按当根收盘价强平，之后禁开新仓（停止交易）。
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
            exit_sig = False                      # 熔断后不再新开仓（持仓已在触发根平掉）
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
            notional_target = min(risk * eq / stop_dist, MAX_LEV * eq)
            qty = notional_target / open_next
            cost = notional_target * FEE_SLIP_RATE
            if cash - cost > 1000:
                cash -= cost
                fees += cost
                pos = {"qty": qty, "entry": open_next,
                       "stop": open_next * (1 - stop_dist),
                       "notional_est": notional_target}

        # ---- 收盘市值权益 + 账户高水位/回撤（高水位永不因平仓重置）----
        eq = cash + (pos["qty"] * (price_now - pos["entry"]) if pos else 0.0)
        if eq > eq_peak:
            eq_peak = eq
        dd = (eq / eq_peak - 1.0) * 100.0
        if dd < max_dd:
            max_dd = dd

        # ---- 账户闸门：回撤触线 → 熔断（有仓按收盘强平，之后停止交易）----
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
    return {
        "ret": ret, "max_dd": max_dd, "calmar": calmar,
        "rounds": len(rounds), "wins": wins,
        "pf": pf, "fees": fees, "bars": len(d),
        "win_rate": wins / len(rounds) * 100 if rounds else 0.0,
        "gate": gate_triggered, "gate_dd": gate_dd,
    }


def fmt_pf(p):
    return "∞" if p == float("inf") else f"{p:.2f}"


def main():
    cache = {}
    for label, sym, s, e in WINDOWS:
        cache[label] = fetch_df(sym, s, e)
        print(f"[行情] {label} {sym} {s}→{e} 预取完成 {len(cache[label])} 根", flush=True)

    out = []
    out.append("# 风险预算灵敏度 · 冻结基线（#48）\n")
    out.append("口径与冻结基线一致：EMA50+20日斜率趋势跟随、1.5×ATR止损、next_open 成交、"
               "taker0.04%+滑点2bp/边、多头资金费率0.03%/日、独立正确账本。\n")
    out.append("账户闸门 = Codex #6 语义：高水位**永不因平仓重置**；净值回撤触线即熔断"
               "（有仓按收盘价强平，之后停止交易直到外部重置）。\n")
    out.append("| 窗口 | 风险 | 闸门 | 收益 | 最大回撤 | 收益/回撤 | 回合 | 胜率 | PF | 触发熔断 |")
    out.append("|---|---|---|---|---|---|---|---|---|---|")

    for label, sym, s, e in WINDOWS:
        d = cache[label]
        bh = None
        seg = d[(d["open_time"].dt.date >= pd.Timestamp(s).date())
                & (d["open_time"].dt.date <= pd.Timestamp(e).date())]
        bh = (float(seg.iloc[-1]["close"]) / float(seg.iloc[0]["close"]) - 1.0) * 100.0
        res = {}
        for risk in RISKS:
            for gate in GATES:
                key = (risk, gate)
                r = run_one(d, s, e, risk, gate)
                res[key] = r
                gate_txt = "25%熔断" if gate else "无"
                trig = "是" if (gate and r["gate"]) else ""
                out.append(
                    f"| {label} | {risk*100:.0f}% | {gate_txt} | {r['ret']:+.1f}% | "
                    f"{r['max_dd']:.1f}% | {r['calmar']:.2f} | {r['rounds']} | "
                    f"{r['win_rate']:.0f}% | {fmt_pf(r['pf'])} | {trig} |")
        # 窗口内聚合摘要
        r_base = res[(RISK_DEFAULT, None)]
        out.append("")
        out.append(f"**{label} 解读**（B&H {bh:+.0f}%）：2%基准 {r_base['ret']:+.1f}%/"
                   f"回撤{r_base['max_dd']:.1f}%；"
                   + "；".join(
                       f"{risk*100:.0f}%无闸 {res[(risk, None)]['ret']:+.1f}%/"
                       f"DD{res[(risk, None)]['max_dd']:.1f}%"
                       + (f"→加25%闸 {res[(risk, gate25)]['ret']:+.1f}%/"
                          f"DD{res[(risk, gate25)]['max_dd']:.1f}%"
                          + ("🔥熔断" if res[(risk, gate25)]["gate"] else "")
                          if risk >= 0.05 else "")
                       for risk in RISKS
                       for gate25 in [25.0]
                   )
                   + "。")
        out.append("")

    body = "\n".join(out)
    print(body)
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "runs", "risk_scan_report.md"), "w", encoding="utf-8") as f:
        f.write(body)
    return 0


if __name__ == "__main__":
    sys.exit(main())
