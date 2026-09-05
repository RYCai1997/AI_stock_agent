# -*- coding: utf-8 -*-
"""Zero-LLM A/B: fixed 10% spot versus confirmed 10->25->50->75%."""
import argparse
import logging
from pathlib import Path

import config as m
import main
import run_strategy_ab as ab


def parse_args():
    p = argparse.ArgumentParser(description="固定10% vs 确认式逐级仓位（零LLM）")
    p.add_argument("--symbol", required=True)
    p.add_argument("--interval", default="1d")
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.add_argument("--tag", default="confirmed")
    p.add_argument("--risk-budget", type=float, default=2.0)
    p.add_argument("--no-trailing", action="store_true",
                   help="独立诊断：关闭移动止盈，判断其是否压制主升浪")
    return p.parse_args()


def val(metrics, key):
    value = ab.metric_number(metrics.get(key))
    return value if value is not None else 0.0


def main_run():
    args = parse_args()
    args.use_llm = False
    args.temperature = 0.0
    args.repeats = 1
    m.CFG.TRADING_MODE = "spot"
    m.CFG.BACKTEST_USE_LLM = False
    m.CFG.SPOT_FIXED_TARGET_PERCENT = 10.0
    m.CFG.SPOT_RISK_BUDGET_PERCENT = float(args.risk_budget)
    if args.no_trailing:
        m.CFG.TRAILING_TP_ACTIVATE = 10.0
        m.CFG.TRAILING_TP_RATIO = 10.0
        args.tag += "_no_trailing"
    main.setup_logging()
    logging.getLogger().setLevel(logging.WARNING)

    reports = {}
    for mode, label in (("fixed", "fixed10"), ("confirmed", "confirmed_pyramid")):
        m.CFG.SPOT_SIZING_MODE = mode
        text = ab.run_leg(args, label, None, label)
        reports[mode] = ab.extract(text)

    f, c = reports["fixed"], reports["confirmed"]
    metrics = [
        "总收益率", "最大回撤", "收益/最大回撤", "买入持有收益",
        "上涨 / 下跌K线捕获率", "最差5% K线平均仓位",
        "累计换手 / 成本磨损", "平均名义敞口", "持仓时平均 / 最大敞口",
        "成交动作", "移动止盈触发",
        "确认式加仓25 / 50 / 75", "确认式快线 / 慢线 / 信号减仓",
        "确认式清仓 / 风险预算压缩", "确认式加仓阻断 盈利/趋势/ATR/间隔",
        "确认式加仓阻断 突破/回踩",
    ]
    lines = [
        "# 固定10% vs 确认式逐级仓位 A/B", "",
        f"- 标的/周期：{args.symbol.upper()} {args.interval}",
        f"- 区间：{args.start} → {args.end}",
        "- 两腿使用同一内置规则引擎；BACKTEST_USE_LLM=false；现货、1x、long/flat。",
        f"- 确认式单仓风险预算：{args.risk_budget:.1f}%权益。",
        f"- 移动止盈：{'关闭（独立诊断）' if args.no_trailing else '保持现有参数'}", "",
        "| 指标 | 固定10% | 确认式 | 确认式-固定 |",
        "|---|---:|---:|---:|",
    ]
    for key in metrics:
        fv, cv = f.get(key, "?"), c.get(key, "?")
        fn, cn = ab.metric_number(fv), ab.metric_number(cv)
        delta = f"{cn-fn:+.2f}" if fn is not None and cn is not None else "—"
        lines.append(f"| {key} | {fv} | {cv} | {delta} |")
    lines += ["", "## 机械判定", ""]
    ret_delta = val(c, "总收益率") - val(f, "总收益率")
    dd_delta = val(c, "最大回撤") - val(f, "最大回撤")
    adds = sum(int(x) for x in __import__("re").findall(
        r"\d+", c.get("确认式加仓25 / 50 / 75", "")))
    if ret_delta > 0 and dd_delta >= -1.0:
        verdict = "该窗口确认式仓位提高收益，且回撤恶化不超过1个百分点。"
    else:
        verdict = "该窗口尚未证明确认式仓位优于固定10%；不应据此替换默认策略。"
    lines += [f"- {verdict}", f"- 确认式加仓触发合计：{adds} 次。", ""]
    out = Path(m.CFG.BASE_DIR) / "runs" / (
        f"ab_{args.tag}_{args.symbol.upper()}_{args.start}_{args.end}_summary.md")
    out.write_text("\n".join(lines), encoding="utf-8")
    print("\n" + "\n".join(lines))
    print(f"汇总已保存：{out}")


if __name__ == "__main__":
    main_run()
