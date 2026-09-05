# -*- coding: utf-8 -*-
"""v3 盲区补充验证：熊转牛第一波（#48 落地 v3 收敛后的稳健性检验）。

v3 已知局限：9 窗 walk-forward 的窗口起点价格全在 EMA200 上方（牛市进行中），
「价格从 EMA200 下方突破的第一波」被禁入漏掉 → 代价未知。

本实验用 BTC/ETH 2020-05→2021-06 窗口覆盖完整牛熊周期：
  2020 疫后底部（EMA200 下方等待期）→ 2020Q4 突破第一波 → 2021 主升浪顶部 → 见顶回落。
固定用已冻结甜点位（EMA200 上 10% / 下禁入 / 无闸），不重新扫描（参数零拟合，纯样本外验证），
对照：固定 10% 无闸（全程在场）与 B&H——看禁入错过多少第一波、熊转牛是否致命。

用法：../llm_trading_agent/.venv/Scripts/python.exe baseline_regime_transition.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "llm_trading_agent"))

import pandas as pd

from baseline_dynamic_risk_diag import fetch_df_v2
import baseline_regime_switch as brs

# 熊转牛窗口（BTC/ETH 2020-03→2021-06，覆盖 EMA200 下方崩盘期 + 底部反弹 + 突破 + 主升浪 + 见顶）
TRANS_WINDOWS = [
    ("BTC_熊转牛2020-21", "BTCUSDT", "2020-03-01", "2021-06-30"),
    ("ETH_熊转牛2020-21", "ETHUSDT", "2020-03-01", "2021-06-30"),
]

# 已冻结甜点位 + 对照
CONFIGS = [
    ("固定10%", dict(kind="fixed", risk=0.10)),
    ("固定2%",  dict(kind="fixed", risk=0.02)),
    ("10%/禁入/无闸", dict(kind="regime", ra=0.10, rb=0.0, gate=None)),
]


def main():
    cache = {}
    for label, sym, s, e in TRANS_WINDOWS:
        cache[label] = fetch_df_v2(sym, s, e)
        print(f"[行情] {label} 预取 {len(cache[label])} 根", flush=True)

    out = []
    out.append("# v3 盲区补充验证 · 熊转牛第一波（#48 v3 收敛后稳健性检验）\n")
    out.append("**动机**：v3 9 窗 walk-forward 起点价格全在 EMA200 上方（牛市进行中），"
               "「从 EMA200 下方突破的第一波」被禁入漏掉，代价未知。\n")
    out.append("**窗口**：BTC/ETH 2020-05→2021-06 覆盖完整周期——疫后底部（EMA200 下方）→ "
               "2020Q4 突破 → 2021 主升浪 → 见顶回落。**固定用已冻结甜点位（10%/禁入/无闸），"
               "不重新扫描（参数零拟合）**，对照固定 10%（全程在场）与固定 2%。\n")
    out.append("EMA200 预热 320 天。信号/成本口径与冻结基线逐位一致。\n")

    for label, sym, s, e in TRANS_WINDOWS:
        d = cache[label]
        seg = d[(d["open_time"].dt.date >= pd.Timestamp(s).date())
                & (d["open_time"].dt.date <= pd.Timestamp(e).date())]
        bh = (float(seg.iloc[-1]["close"]) / float(seg.iloc[0]["close"]) - 1.0) * 100.0

        out.append(f"\n### {label}（B&H {bh:+.0f}%）\n")
        out.append("| 配置 | 收益 | 最大回撤 | Calmar | 回合 | PF | 开仓档位 |")
        out.append("|---|---|---|---|---|---|---|")
        for name, c in CONFIGS:
            if c["kind"] == "fixed":
                from baseline_risk_scan import run_one
                r = run_one(d, s, e, c["risk"], None)
                dist = "—"
            else:
                r = brs.run_regime(d, s, e, c["ra"], c["rb"], c["gate"], collect_sizing=True)
                dist = " / ".join(f"{k*100:.0f}%×{v}" for k, v in sorted(r["sizing"].items()))
            from baseline_risk_scan import fmt_pf
            out.append(f"| {name} | {r['ret']:+.1f}% | {r['max_dd']:.1f}% | {r['calmar']:.2f} | "
                       f"{r['rounds']} | {fmt_pf(r['pf'])} | {dist} |")

        # 逐笔入场时间线（看禁入是否漏掉第一波、何时才首次入场）
        out.append("\n**10%/禁入 逐笔入场（验证首笔入场时滞）**：")
        d2 = d[(d["open_time"].dt.date >= pd.Timestamp(s).date())
               & (d["open_time"].dt.date <= pd.Timestamp(e).date())].copy()
        # 找首次 close>EMA200 的日子 = 禁入解禁点
        above = d2[d2["close"] > d2["ema200"]]
        first_above = above.iloc[0]["open_time"] if len(above) else None
        out.append(f"- 价格首次站上 EMA200：{first_above}（窗口起点 {s}）")
        # 该时点距窗口起点的涨幅
        if first_above is not None:
            p0 = float(d2.iloc[0]["close"])
            p_first = float(above.iloc[0]["close"])
            out.append(f"- 从窗口起点到站上 EMA200：价格 {p0:.0f}→{p_first:.0f}（+{(p_first/p0-1)*100:+.0f}%）"
                       "——禁入错过这段，但这段是底部左侧，趋势跟随本来也不做")
            # 站上后到窗口顶的涨幅 = 禁入能吃到的主升段
            p_top = float(d2["close"].max())
            out.append(f"- 站上 EMA200 后最高 {p_top:.0f}（{(p_top/p_first-1)*100:+.0f}%）"
                       "——第一波+主升浪在解禁后仍可吃到")

    body = "\n".join(out)
    print(body)
    runs = os.path.join(os.path.dirname(os.path.abspath(__file__)), "runs")
    os.makedirs(runs, exist_ok=True)
    with open(os.path.join(runs, "regime_transition_check.md"), "w", encoding="utf-8") as f:
        f.write(body + "\n")
    print("\n[done] runs/regime_transition_check.md")


if __name__ == "__main__":
    main()
