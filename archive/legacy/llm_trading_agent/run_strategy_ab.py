# -*- coding: utf-8 -*-
"""V10 Guardrail 一键 A/B 对照：同币/同窗下「无护栏基线 vs V-C 护栏」两条腿各跑一次，自动出对照表。

用法（Generalized 策略自测入口，配套 STRATEGY_v10_guardrail.md）：
    python run_strategy_ab.py --symbol ETHUSDT --interval 1d \
        --start 2021-10-01 --end 2022-12-31            # 规则引擎档（零 API，确定性）
    python run_strategy_ab.py --symbol BTCUSDT ... --use-llm   # LLM 档（⚠️ 2×N 根 API，费用自负）

护栏腿固定为实跑验证配置：rl1=breach + cool=20 + rl2（见 STRATEGY 文档 §1）。
产出：runs/ab_{tag}_{SYM}_{start}_{end}_{baseline|v10}_report.md + 控制台对照表。

关键注意（探针覆写经典坑）：
- config.py = class Config（类属性）+ 模块级单例 CFG；业务代码读【实例】。
- 覆写必须 `import config as m; m.CFG.X = v`；且 .env 遗留 BACKTEST_USE_LLM=true 会被拾起烧 API
  → 非 --use-llm 时务必显式置 False。
"""
import argparse
import logging
import re
import shutil
import statistics
import types
from pathlib import Path

import config as m
import main

BASE = Path(m.CFG.BASE_DIR)
OUT = BASE / "runs"
OUT.mkdir(exist_ok=True)

# 护栏腿参数（实跑验证配置，勿改）
V10_CFG = {"rl1": "breach", "rl1_cool": 20, "rl2": True}


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="V10 Guardrail A/B 对照回测（护栏 on/off）")
    p.add_argument("--symbol", required=True, help="交易对，如 ETHUSDT/BTCUSDT/SOLUSDT")
    p.add_argument("--interval", default="1d", help="K线周期（默认 1d）")
    p.add_argument("--start", required=True, help="回测起始 YYYY-MM-DD（需 >60 根预热）")
    p.add_argument("--end", default=None, help="回测截止 YYYY-MM-DD（空=到最新）")
    p.add_argument("--use-llm", action="store_true",
                   help="LLM 档（烧 API：2×区间根数调用）。默认规则引擎（零 API 确定性）")
    p.add_argument("--temperature", type=float, default=0.0,
                   help="A/B 的 LLM 温度（默认0，减少两腿非策略随机差异）")
    p.add_argument("--repeats", type=int, default=1,
                   help="每条腿重复次数（LLM建议至少5；偶数轮自动交换AB运行顺序）")
    p.add_argument("--tag", default="", help="产物文件名标签（可选）")
    return p.parse_args()


def run_leg(args: argparse.Namespace, label: str, v10: dict | None,
            artifact_label: str | None = None) -> str:
    """跑一条腿并独立归档报告、repro、曲线与日志，返回报告全文。"""
    m.CFG.SYMBOL = args.symbol.strip().upper()
    m.CFG.INTERVAL = args.interval.strip().lower()
    m.CFG.START_DATE = args.start.strip()
    m.CFG.BACKTEST_END = (args.end or "").strip() or None
    m.CFG.BACKTEST_USE_LLM = bool(args.use_llm)
    if args.use_llm:
        m.CFG.LLM_TEMPERATURE = float(args.temperature)
    bt_args = types.SimpleNamespace(mode="backtest", once=False,
                                    use_llm=args.use_llm, plot=False, v10=v10)
    tag = f"{args.tag}_" if args.tag else ""
    file_label = artifact_label or label
    stem = f"ab_{tag}{m.CFG.SYMBOL}_{m.CFG.START_DATE}_{m.CFG.BACKTEST_END or 'now'}_{file_label}"
    log_path = OUT / f"{stem}.log"
    leg_handler = logging.FileHandler(log_path, mode="w", encoding="utf-8")
    leg_handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"))
    logging.getLogger().addHandler(leg_handler)
    print(f"\n######## {label}（{m.CFG.SYMBOL} {m.CFG.INTERVAL} "
          f"{m.CFG.START_DATE} → {m.CFG.BACKTEST_END or '最新'}）########", flush=True)
    try:
        main.run_backtest(bt_args)
    finally:
        logging.getLogger().removeHandler(leg_handler)
        leg_handler.close()
    src = BASE / "backtest_report.md"
    if not src.exists():
        raise RuntimeError(f"报告缺失: {src}（{label} 腿失败）")
    dst = OUT / f"{stem}_report.md"
    shutil.copy2(src, dst)
    artifacts = {
        BASE / "backtest_repro.json": OUT / f"{stem}_repro.json",
        BASE / "backtest_equity.csv": OUT / f"{stem}_equity.csv",
        OUT / "curve_backtest.csv": OUT / f"{stem}_curve.csv",
    }
    for artifact_src, artifact_dst in artifacts.items():
        if artifact_src.exists():
            shutil.copy2(artifact_src, artifact_dst)
    print(f"  -> 已独立归档 {stem} 的 report/repro/equity/curve/log", flush=True)
    return dst.read_text(encoding="utf-8")


def extract(report: str) -> dict:
    """从报告 md 提取关键指标 {指标: 数值}。"""
    kv: dict[str, str] = {}
    for line in report.splitlines():
        mm = re.match(r"^\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|$", line)
        if mm and mm.group(1).strip() != "指标":
            kv[mm.group(1).strip()] = mm.group(2).strip()
    return kv


def metric_number(value: str | None) -> float | None:
    if not value or value == "?":
        return None
    match = re.search(r"[-+]?[\d,.]+", value.replace(",", ""))
    return float(match.group()) if match else None


def print_comparison(b: dict, v: dict) -> None:
    print("\n==================== V10 Guardrail A/B 对照 ====================")
    keys = ["总收益率", "最大回撤", "收益/最大回撤", "夏普比率", "成交动作", "平仓回合", "胜率",
            "盈利因子", "累计已实现盈亏", "时间在场率", "平均名义敞口", "持仓时平均杠杆", "强平次数"]
    hdr = f"{'指标':<14}{'基线(无护栏)':>16}{'V-C护栏':>16}{'Δ(pp)':>12}"
    print(hdr)
    print("-" * len(hdr))
    for k in keys:
        x, y = b.get(k, "?"), v.get(k, "?")
        delta = ""
        nx, ny = metric_number(x), metric_number(y)
        if nx is not None and ny is not None:
            delta = f"{ny - nx:+.2f}pp" if "%" in x + y else f"{ny - nx:+.2f}"
        print(f"{k:<14}{x:>16}{y:>16}{delta:>12}")


def save_repeat_summary(args, pairs: list[tuple[dict, dict]]) -> Path:
    """保存多次 LLM A/B 的逐次结果与离散度，避免以单次随机输出作结论。"""
    metrics = {"总收益率": "return_pct", "最大回撤": "max_dd_pct", "盈利因子": "profit_factor"}
    tag = f"{args.tag}_" if args.tag else ""
    path = OUT / (f"ab_{tag}{args.symbol.upper()}_{args.start}_{args.end or 'now'}_"
                  "repeated_summary.md")
    lines = [
        "# LLM A/B 重复实验汇总", "",
        f"- 标的/周期：{args.symbol.upper()} {args.interval}",
        f"- 区间：{args.start} → {args.end or 'now'}",
        f"- 重复：每腿 {len(pairs)} 次；temperature={args.temperature}",
        "- 奇数轮 baseline→v10，偶数轮 v10→baseline，以减弱运行顺序影响。", "",
        "| 轮次 | baseline收益 | v10收益 | Δ收益(pp) | baseline DD | v10 DD | ΔDD(pp) |",
        "|---:|---:|---:|---:|---:|---:|---:|",
    ]
    deltas: dict[str, list[float]] = {name: [] for name in metrics}
    for idx, (b, v) in enumerate(pairs, 1):
        br, vr = metric_number(b.get("总收益率")), metric_number(v.get("总收益率"))
        bd, vd = metric_number(b.get("最大回撤")), metric_number(v.get("最大回撤"))
        lines.append(f"| {idx} | {br:+.2f}% | {vr:+.2f}% | {vr-br:+.2f} | "
                     f"{bd:.2f}% | {vd:.2f}% | {vd-bd:+.2f} |")
        for label in metrics:
            bv, vv = metric_number(b.get(label)), metric_number(v.get(label))
            if bv is not None and vv is not None:
                deltas[label].append(vv - bv)
    lines += ["", "## V10 - baseline 的重复分布", "",
              "| 指标差 | 均值 | 中位数 | 标准差 | 最小 | 最大 |",
              "|---|---:|---:|---:|---:|---:|"]
    for label, values in deltas.items():
        if values:
            lines.append(f"| {label} | {statistics.mean(values):+.3f} | "
                         f"{statistics.median(values):+.3f} | {statistics.pstdev(values):.3f} | "
                         f"{min(values):+.3f} | {max(values):+.3f} |")
    lines += ["", "> 单次结果只作探索；是否保留 v10 应看重复分布和未参与调参窗口。", ""]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def main_run() -> None:
    args = parse_args()
    m.CFG.BACKTEST_USE_LLM = False   # 先复位；run_leg 内按 --use-llm 再设
    main.setup_logging()
    logging.getLogger().setLevel(logging.WARNING)  # 静默 INFO，对照从 md 读
    logging.getLogger("httpx").setLevel(logging.ERROR)

    repeats = max(1, int(args.repeats))
    if not args.use_llm and repeats > 1:
        print("规则引擎是确定性的，--repeats 已收敛为1。")
        repeats = 1
    pairs: list[tuple[dict, dict]] = []
    for rep in range(1, repeats + 1):
        suffix = "" if repeats == 1 else f"_r{rep:02d}"
        jobs = [("baseline", None), ("v10", V10_CFG)]
        if rep % 2 == 0:
            jobs.reverse()
        reports = {}
        for label, guardrail in jobs:
            reports[label] = run_leg(args, label, guardrail,
                                     artifact_label=f"{label}{suffix}")
        b, v = extract(reports["baseline"]), extract(reports["v10"])
        pairs.append((b, v))
        print_comparison(b, v)
    print("\n注：规则引擎档为确定性结果；LLM 档存在状态分叉，护栏效果只能以此实跑为准（离线不可外推）。")
    b, v = pairs[-1]
    if repeats > 1:
        summary_path = save_repeat_summary(args, pairs)
        print(f"重复实验汇总已保存：{summary_path}")
    if not args.use_llm and b.get("总收益率") == v.get("总收益率"):
        print("提示：规则引擎档两腿相同 = 该窗护栏与规则引擎既有风控（破EMA50/弱市不BUY）完全重叠，")
        print("      无边际效应；若两腿不同（如 BTC 2022 熊市实测 +2.48pp 收益）则护栏补上了")
        print("      规则引擎漏走的时段。无论哪种，真实判决须加 --use-llm 实测。")


if __name__ == "__main__":
    main_run()
