# -*- coding: utf-8 -*-
"""动态风险 v2 诊断：EMA200 背景 + ATR 归一化斜率的阈值标定（#48 落地第二版前置）。

v1 证伪教训：EMA20 斜率绝对阈值跨市场不可比（ETH 熊市 V 型反弹 +10.6% vs BTC 慢牛 3-7%，
绝对档恰好反向放大）；且真正要区分的是牛熊背景而非短期动能。
v2 = A 开关（close>EMA200 才允许放大档）+ B 归一化（ema_slope/atr 无量纲强度，跨市场可比）。

本脚本只打印证据不跑回测：
  1. 每窗口满足入场信号(close>EMA50 & ema_slope>0)的候选日分布（默认信号 vs 叠加 EMA200 开关）
  2. 候选日的 slope_norm = ema_slope/atr 分位数 → 据此定 2/5/10% 档阈值
  3. 熊市反弹日是否被 EMA200 开关全部挡在 2%（验证 A 的有效性）

用法：../llm_trading_agent/.venv/Scripts/python.exe baseline_dynamic_risk_diag.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "llm_trading_agent"))

import numpy as np
import pandas as pd

from baseline_risk_scan import ATR_N, EMA_N, SLOPE_LOOKBACK, WINDOWS

EMA200_N = 200
WARM_DAYS = 320          # EMA200 ewm 收敛需 ~2-3×span 根，320 天日线才够（原 110 不够）


def fetch_df_v2(symbol, start, end):
    from config import CFG
    import data_fetcher
    CFG.SYMBOL = symbol
    CFG.INTERVAL = "1d"
    warm = (pd.Timestamp(start) - pd.Timedelta(days=WARM_DAYS)).strftime("%Y-%m-%d")
    df = data_fetcher.fetch_klines(symbol, "1d", start_date=warm, end_date=end)
    if df is None or df.empty:
        raise RuntimeError(f"fetch empty: {symbol}")
    df = df.reset_index(drop=True)
    df["ema"] = df["close"].ewm(span=EMA_N, adjust=False).mean()
    df["ema_slope"] = df["ema"] - df["ema"].shift(SLOPE_LOOKBACK)
    df["ema200"] = df["close"].ewm(span=EMA200_N, adjust=False).mean()
    tr = pd.concat([df["high"] - df["low"],
                    (df["high"] - df["close"].shift()).abs(),
                    (df["low"] - df["close"].shift()).abs()], axis=1).max(axis=1)
    df["atr"] = tr.ewm(span=ATR_N, adjust=False).mean()
    return df


def main():
    rows = []
    for label, sym, s, e in WINDOWS:
        d = fetch_df_v2(sym, s, e)
        seg = d[(d["open_time"].dt.date >= pd.Timestamp(s).date())
                & (d["open_time"].dt.date <= pd.Timestamp(e).date())].copy()
        is_bear = "熊" in label

        pc, pe, ps = seg["close"].shift(), seg["ema"].shift(), seg["ema_slope"].shift()
        pa, pe2 = seg["atr"].shift(), seg["ema200"].shift()
        cand = (pc > pe) & (ps > 0)
        # slope_norm = ema_slope/atr（无量纲：EMA20 变化量是几倍当前 ATR）
        seg["slope_norm"] = ps / pa
        seg["above200"] = pc > pe2

        both = cand & seg["above200"].fillna(False)
        n_cand = int(cand.sum()); n_above = int((cand & seg["above200"].fillna(False)).sum())
        vals = seg.loc[both.fillna(False), "slope_norm"].dropna()

        # 熊市里被 EMA200 挡掉的候选占比（验证 A 开关有效性）
        blocked = ""
        if is_bear and n_cand:
            n_blocked = n_cand - n_above
            blocked = f"挡掉{n_blocked}/{n_cand}"
        q = (np.percentile(vals, [25, 50, 75, 90]) if len(vals) else [np.nan] * 4)
        rows.append((label, is_bear, n_cand, n_above, blocked, q, vals))
        print(f"[{label}] 候选入场日 {n_cand}（EMA200上方 {n_above}）"
              + (f" {blocked}" if blocked else "")
              + (f" | slope_norm p25/50/75/90 = {q[0]:.1f}/{q[1]:.1f}/{q[2]:.1f}/{q[3]:.1f}"
                 if len(vals) else " | 无 EMA200 上方候选"))

    # 汇总：全部窗口合并分位数（EMA200 上方候选）
    allv = np.concatenate([r[6].values for r in rows if len(r[6])])
    if len(allv):
        print(f"\n[合并] EMA200 上方候选 slope_norm：p25={np.percentile(allv,25):.2f} "
              f"p50={np.percentile(allv,50):.2f} p75={np.percentile(allv,75):.2f} "
              f"p90={np.percentile(allv,90):.2f} n={len(allv)}")
        print("建议档位（按数据分布，非拍脑袋）：p50≈5%档起点、p75≈10%档起点")


if __name__ == "__main__":
    main()
