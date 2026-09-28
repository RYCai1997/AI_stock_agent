# -*- coding: utf-8 -*-
"""Compare confidence sizing with a same-signal fixed-exposure spot baseline."""
import argparse
import logging
import statistics
from pathlib import Path

import config as m
import main
import run_strategy_ab as ab


def parse_args():
    p = argparse.ArgumentParser(description="现货信心仓位 vs 匹配持仓敞口的固定仓位对照")
    p.add_argument("--symbol", required=True)
    p.add_argument("--interval", default="1d")
    p.add_argument("--start", required=True)
    p.add_argument("--end", default=None)
    p.add_argument("--use-llm", action="store_true")
    p.add_argument("--temperature", type=float, default=0.0)
    p.add_argument("--tag", default="spot_sizing")
    p.add_argument("--fixed-target", type=float, default=None,
                   help="固定仓位%；省略时匹配信心腿的持仓时平均敞口")
    p.add_argument("--repeats", type=int, default=1,
                   help="每条腿重复次数；LLM实验建议5，偶数轮交换运行顺序")
    return p.parse_args()


def number(metrics, key):
    value = ab.metric_number(metrics.get(key))
    if value is None:
        raise RuntimeError(f"报告缺少指标: {key}")
    return value


def main_run():
    args = parse_args()
    m.CFG.TRADING_MODE = "spot"
    m.CFG.BACKTEST_USE_LLM = False
    main.setup_logging()
    logging.getLogger().setLevel(logging.WARNING)
    repeats = max(1, int(args.repeats))
    if not args.use_llm and repeats > 1:
        print("规则引擎确定性运行，repeats 收敛为1。")
        repeats = 1
    if repeats > 1 and args.fixed_target is None:
        raise ValueError("重复实验必须显式给 --fixed-target，才能交换两腿顺序")
    target = (float(args.fixed_target) if args.fixed_target is not None else None)
    pairs = []
    for rep in range(1, repeats + 1):
        suffix = "" if repeats == 1 else f"_r{rep:02d}"
        jobs = ["confidence", "fixed"]
        if rep % 2 == 0 and target is not None:
            jobs.reverse()
        reports = {}
        for kind in jobs:
            if kind == "confidence":
                m.CFG.SPOT_SIZING_MODE = "confidence"
                text = ab.run_leg(args, "spot_confidence", None,
                                  f"spot_confidence{suffix}")
                reports[kind] = ab.extract(text)
                if target is None:
                    target = number(reports[kind], "持仓时平均 / 最大敞口")
                    target = max(5.0, min(float(m.CFG.SPOT_MAX_EXPOSURE_PERCENT), target))
            else:
                m.CFG.SPOT_SIZING_MODE = "fixed"
                m.CFG.SPOT_FIXED_TARGET_PERCENT = target
                text = ab.run_leg(args, "spot_fixed_matched", None,
                                  f"spot_fixed_matched{suffix}")
                reports[kind] = ab.extract(text)
        pairs.append((reports["confidence"], reports["fixed"]))

    return_deltas = []
    dd_deltas = []
    exposure_deltas = []
    rows = []
    for idx, (conf, fixed) in enumerate(pairs, 1):
        cr, fr = number(conf, "总收益率"), number(fixed, "总收益率")
        cd, fd = number(conf, "最大回撤"), number(fixed, "最大回撤")
        ca, fa = number(conf, "平均名义敞口"), number(fixed, "平均名义敞口")
        ci, fi = (number(conf, "持仓时平均 / 最大敞口"),
                  number(fixed, "持仓时平均 / 最大敞口"))
        return_deltas.append(cr - fr)
        dd_deltas.append(cd - fd)
        exposure_deltas.append(ca - fa)
        rows.append(f"| {idx} | {cr:+.2f}% | {fr:+.2f}% | {cr-fr:+.2f}pp | "
                    f"{cd:.2f}% | {fd:.2f}% | {ca-fa:+.2f}pp | {ci-fi:+.2f}pp |")
    lines = [
        "# 现货仓位归因 A/B", "",
        f"- 标的：{args.symbol.upper()} {args.interval}",
        f"- 区间：{args.start} → {args.end or 'now'}",
        f"- 决策：{'真实LLM' if args.use_llm else '规则引擎'}；两腿均为现货、无杠杆、无funding",
        f"- 重复：每腿 {repeats} 次；temperature={args.temperature}",
        f"- 固定仓位目标：{target:.2f}%（匹配规则校准中的信心腿持仓时平均敞口）", "",
        "| 轮次 | 信心收益 | 固定收益 | 收益差 | 信心DD | 固定DD | 全时段敞口差 | 持仓敞口差 |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|",
        *rows, "",
        "| 差值统计（信心-固定） | 均值 | 中位数 | 标准差 | 最小 | 最大 |",
        "|---|---:|---:|---:|---:|---:|",
        f"| 收益率(pp) | {statistics.mean(return_deltas):+.3f} | {statistics.median(return_deltas):+.3f} | "
        f"{statistics.pstdev(return_deltas):.3f} | {min(return_deltas):+.3f} | {max(return_deltas):+.3f} |",
        f"| 最大回撤(pp) | {statistics.mean(dd_deltas):+.3f} | {statistics.median(dd_deltas):+.3f} | "
        f"{statistics.pstdev(dd_deltas):.3f} | {min(dd_deltas):+.3f} | {max(dd_deltas):+.3f} |",
        f"| 全时段平均敞口(pp) | {statistics.mean(exposure_deltas):+.3f} | "
        f"{statistics.median(exposure_deltas):+.3f} | {statistics.pstdev(exposure_deltas):.3f} | "
        f"{min(exposure_deltas):+.3f} | {max(exposure_deltas):+.3f} |", "",
        "> 若平均敞口接近而收益仍有差异，才更支持仓位路径本身有价值；若敞口差异较大，结论暂不成立。", "",
    ]
    out = Path(m.CFG.BASE_DIR) / "runs" / (
        f"ab_{args.tag}_{args.symbol.upper()}_{args.start}_{args.end or 'now'}_spot_sizing_summary.md")
    out.write_text("\n".join(lines), encoding="utf-8")
    print("\n" + "\n".join(lines))
    print(f"汇总已保存：{out}")


if __name__ == "__main__":
    main_run()
