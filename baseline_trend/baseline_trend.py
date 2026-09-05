# -*- coding: utf-8 -*-
"""机械趋势基线（Track B）：回答「纯 TA 参数组合是否够用 / LLM 是否增量」。

设计意图（对应 Codex #4 与用户目标函数「肥尾+有界回撤」）：
- 少参数、确定性、零 LLM：趋势跟随（EMA50 + 斜率过滤），只在趋势转强时进场，
  趋势破位才离场，不设固定小止盈 —— 让利润奔跑（吃主升浪）是唯一获利逻辑。
- 自带一份独立、正确的逐仓账本（2026-09-03 账本修复版语义：权益=cash+浮动，
  成本/资金费率直接扣 cash），与 llm_trading_agent 引擎完全解耦。
- 保守成本：taker 0.04% + 滑点 2bp /边；资金费率 0.03%/日（≈0.01%/8h×3，多头付）。
- walk-forward：SOL/BTC/ETH × 牛(23末-24初)/主升浪投诉窗(24-08~25-01)/熊(2022) 九窗口，
  规则参数在全部窗口固定（无任何单窗口拟合），全部为样本外。

用法：.venv/Scripts/python.exe baseline_trend.py   （输出 runs/baseline_*.md）
"""
import os, sys, io
import datetime as dt

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "llm_trading_agent"))

import pandas as pd

# ============ 策略参数（全部窗口共用，禁止按窗口拟合） ============
EMA_N = 50            # 趋势线（与主项目 EMA_TREND_SLOW_PERIOD=50 对齐）
SLOPE_LOOKBACK = 20   # 趋势线 20 日斜率 > 0 才视为「趋势转强」
ATR_N = 14
STOP_ATR_MULT = 1.5   # 初始止损 = 1.5 × ATR14（入场时定）
RISK_PER_TRADE = 0.02 # 单笔风险 2% 权益（止损距离换算仓位）
MAX_LEV = 3.0         # 名义敞口上限 = 3× 权益
FUNDING_PER_DAY = 0.0003   # 多头隔夜资金费率 0.03%/日
FEE_SLIP_RATE = 0.0006     # 单边成本 = 手续费0.04% + 滑点0.02%

WINDOWS = [
    # (label, symbol, start, end)
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

def fetch(symbol, start, end):
    """复用项目 data_fetcher（只读行情，不含任何策略逻辑）。"""
    from config import CFG
    import data_fetcher
    CFG.SYMBOL = symbol
    CFG.INTERVAL = "1d"
    warm = (pd.Timestamp(start) - pd.Timedelta(days=110)).strftime("%Y-%m-%d")
    df = data_fetcher.fetch_klines(symbol, "1d", start_date=warm, end_date=end)
    if df is None or df.empty:
        raise RuntimeError(f"fetch empty: {symbol}")
    return df.reset_index(drop=True)

def indicators(df):
    d = df.copy()
    d["ema"] = d["close"].ewm(span=EMA_N, adjust=False).mean()
    d["ema_slope"] = d["ema"] - d["ema"].shift(SLOPE_LOOKBACK)   # >0 视为趋势向上
    tr = pd.concat([d["high"] - d["low"],
                    (d["high"] - d["close"].shift()).abs(),
                    (d["low"] - d["close"].shift()).abs()], axis=1).max(axis=1)
    d["atr"] = tr.ewm(span=ATR_N, adjust=False).mean()
    return d

def run_window(label, symbol, start, end):
    df = indicators(fetch(symbol, start, end))
    d = df[(df["open_time"].dt.date >= pd.Timestamp(start).date())
           & (df["open_time"].dt.date <= pd.Timestamp(end).date())].copy()
    if len(d) < EMA_N + SLOPE_LOOKBACK + 5:
        return {"label": label, "symbol": symbol, "error": f"bars={len(d)} too few"}

    cash = 100_000.0
    eq_peak = cash
    peak_eq_curve = cash
    max_dd = 0.0
    pos = None            # dict: qty, entry, stop, notional
    rounds, wins, gross_win, gross_loss = [], 0, 0.0, 0.0
    fees = 0.0
    eq_curve = []

    # 信号只在「已收盘」的 t 根产生，成交在 t+1 开盘 → 语义 = next_open
    for i in range(1, len(d)):
        row_t = d.iloc[i]                       # 当前收盘根（信号/离场判断根）
        prev = d.iloc[i - 1]
        price_now = float(row_t["close"])

        # ---- 持仓中：每日资金费率（多头付，按昨收名义估算） ----
        if pos is not None:
            fund = pos["notional_est"] * FUNDING_PER_DAY
            cash -= fund
            fees += fund

        # ---- 信号评估（用 t 收盘 → t+1 开盘执行） ----
        ema_ok = float(prev["close"]) > float(prev["ema"]) and float(prev["ema_slope"]) > 0
        # 离场：趋势破位（收盘跌破 EMA50）或 收盘跌破初始止损
        exit_sig = pos is not None and (
            float(prev["close"]) < float(prev["ema"]) or
            float(prev["close"]) <= pos["stop"])
        entry_sig = pos is None and ema_ok

        open_next = float(d.iloc[min(i + 1, len(d) - 1)]["open"])
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
            notional_target = min(RISK_PER_TRADE * eq / stop_dist,
                                  MAX_LEV * eq)
            qty = notional_target / open_next
            cost = notional_target * FEE_SLIP_RATE
            if cash - cost > 1000:
                cash -= cost
                fees += cost
                pos = {"qty": qty, "entry": open_next,
                       "stop": open_next * (1 - stop_dist),
                       "notional_est": notional_target}

        # ---- 每根按收盘市值，账户级真实回撤（高水位永不因平仓重置） ----
        eq = cash + (pos["qty"] * (price_now - pos["entry"]) if pos else 0.0)
        eq_curve.append(eq)
        if eq > eq_peak:
            eq_peak = eq
        dd = (eq / eq_peak - 1.0) * 100.0
        if dd < max_dd:
            max_dd = dd

    # 若窗口末尾仍持仓：按最后收盘平掉（避免把未了结浮盈算进结果）
    if pos is not None:
        last = float(d.iloc[-1]["close"])
        pnl = (last - pos["entry"]) * pos["qty"]
        cost = pos["qty"] * last * FEE_SLIP_RATE
        cash += pnl - cost
        fees += cost
        rounds.append(pnl - cost)

    ret = (cash / 100_000.0 - 1.0) * 100.0
    pf = gross_win / gross_loss if gross_loss > 1e-9 else (float("inf") if gross_win > 0 else 0.0)
    calmar = ret / abs(max_dd) if abs(max_dd) > 1e-9 else 0.0
    bh = (float(d.iloc[-1]["close"]) / float(d.iloc[0]["close"]) - 1.0) * 100.0
    return {
        "label": label, "symbol": symbol,
        "ret": ret, "bh": bh, "max_dd": max_dd, "calmar": calmar,
        "rounds": len(rounds), "wins": wins,
        "pf": pf, "fees": fees, "bars": len(d),
        "win_rate": wins / len(rounds) * 100 if rounds else 0.0,
    }

def fmt_pf(pf):
    return "∞" if pf == float("inf") else f"{pf:.2f}"

def main():
    out = sys.stdout
    rows, errs = [], []
    for label, sym, s, e in WINDOWS:
        try:
            r = run_window(label, sym, s, e)
            if "error" in r:
                errs.append(f"{label}: {r['error']}")
                continue
            rows.append(r)
            print(f"[{label}] 策略 {r['ret']:+.1f}% | B&H {r['bh']:+.1f}% | "
                  f"maxDD {r['max_dd']:.1f}% | 回合 {r['rounds']} | PF {fmt_pf(r['pf'])}",
                  file=out)
        except Exception as e:
            errs.append(f"{label}: {e}")
            print(f"[{label}] ERROR {e}", file=out)

    lines = [
        "# 机械趋势基线 · 多市场 × 多牛熊 walk-forward（样本外）",
        "",
        "规则（全部窗口共用，无拟合）：收盘>EMA50 且 EMA50 20日斜率为正 → 次根开盘进场；",
        "收盘<EMA50 或 收盘<初始止损(1.5×ATR14) → 次根开盘离场；不设固定止盈（让利润奔跑）。",
        "仓位：单笔风险 2% 权益 ÷ 止损距离，名义敞口 ≤3× 权益。",
        "成本：taker 0.04% + 滑点 2bp/边；多头隔夜资金费率 0.03%/日。账本=2026-09-03 修复语义。",
        "",
        "| 窗口 | 标的 | 策略收益 | 买入持有 | 差值 | 最大回撤 | 收益/回撤 | 回合 | 胜率 | 盈利因子 |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        diff = r["ret"] - r["bh"]
        lines.append(
            f"| {r['label']} | {r['symbol']} | {r['ret']:+.1f}% | {r['bh']:+.1f}% | "
            f"{diff:+.1f}% | {r['max_dd']:.1f}% | {r['calmar']:.2f} | {r['rounds']} | "
            f"{r['win_rate']:.0f}% | {fmt_pf(r['pf'])} |")
    agg_wins = sum(1 for r in rows if r["ret"] > r["bh"])
    agg_win = sum(r["ret"] for r in rows)
    agg_bh = sum(r["bh"] for r in rows)
    lines += [
        "",
        f"汇总：{len(rows)} 个窗口，策略跑赢买入持有 {agg_wins} 个；",
        f"策略收益合计 {agg_win:+.0f}% vs 买入持有合计 {agg_bh:+.0f}%。",
        "",
    ]
    if errs:
        lines += ["失败窗口："] + [f"- {e}" for e in errs]
    body = "\n".join(lines)
    print("\n" + body)
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "runs", "baseline_trend_report.md"), "w", encoding="utf-8") as f:
        f.write(body)
    return 1 if errs else 0

if __name__ == "__main__":
    sys.exit(main())
