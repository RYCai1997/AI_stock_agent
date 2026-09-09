# -*- coding: utf-8 -*-
"""Run the deterministic ETF Rotation V1 research comparison."""

import argparse
import json
from datetime import date, datetime
from pathlib import Path

import pandas as pd

from portfolio_core.backtest import buy_and_hold_metrics, equal_weight_metrics, run_rotation_backtest
from portfolio_core.config import RotationConfig
from portfolio_core.providers import fetch_price_panel, load_price_directory, save_source_metadata


DEFAULT_UNIVERSE = ["SPY", "QQQ", "IWM", "EFA", "EEM", "TLT", "IEF", "GLD", "DBC", "VNQ", "BIL"]
METRIC_ORDER = [
    "total_return",
    "cagr",
    "max_drawdown",
    "annual_volatility",
    "sharpe_rf0",
    "calmar",
    "upside_capture",
    "downside_capture",
    "worst_5pct_daily_mean",
    "average_exposure",
    "maximum_exposure",
    "total_turnover",
    "transaction_cost",
    "rebalance_count",
    "observations",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="零LLM ETF动量+趋势轮动研究")
    parser.add_argument("--symbols", default=",".join(DEFAULT_UNIVERSE), help="逗号分隔的ETF代码")
    parser.add_argument("--benchmark", default="SPY", help="比较基准，必须包含在symbols中")
    parser.add_argument("--start", default="2010-01-01")
    parser.add_argument("--end", default=date.today().isoformat())
    parser.add_argument("--data-dir", help="可选：本地CSV目录，文件需为 SYMBOL.csv")
    parser.add_argument("--output-dir", default="runs", help="结果根目录")
    parser.add_argument("--cost-bps", type=float, default=10.0, help="单边换手成本，基点")
    parser.add_argument("--top-n", type=int, default=4)
    parser.add_argument("--max-asset-weight", type=float, default=0.30)
    return parser.parse_args()


def _fmt(value: float, percent: bool = False) -> str:
    if pd.isna(value):
        return "N/A"
    return f"{value:.2%}" if percent else f"{value:.3f}"


def write_report(
    output: Path,
    summary: pd.DataFrame,
    prices: pd.DataFrame,
    results: dict[str, object],
    args: argparse.Namespace,
) -> None:
    percent_metrics = {
        "total_return", "cagr", "max_drawdown", "annual_volatility",
        "worst_5pct_daily_mean", "average_exposure", "maximum_exposure", "transaction_cost",
    }
    lines = [
        "# ETF Rotation V1 回测报告",
        "",
        "## 判读边界",
        "",
        "- 这是零 LLM、只做多、现货组合研究，不发送订单。",
        "- 月末收盘后生成信号，下一交易日收盘成交；新仓位从再下一根收盘收益开始生效。",
        "- 使用复权收盘价；现金收益暂按 0，成本按单边换手计入。",
        "- ETF 成分股新闻层尚未进入本轮收益，避免把当前持仓或未来新闻带入历史。",
        "",
        "## 输入",
        "",
        f"- 日期：{prices.index.min().date()} 至 {prices.index.max().date()}",
        f"- 共同评价期：{next(iter(results.values())).returns.index.min().date()} 至 {next(iter(results.values())).returns.index.max().date()}（此前数据仅用于因子预热）",
        f"- 标的：{', '.join(prices.columns)}",
        f"- 基准：{args.benchmark}",
        f"- Top N：{args.top_n}；单标的上限：{args.max_asset_weight:.0%}",
        f"- 成本：{args.cost_bps:.1f} bps / 单边换手",
        "",
        "## 结果",
        "",
        "| 策略 | 总收益 | CAGR | 最大回撤 | Sharpe | Calmar | 平均仓位 | 总换手 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for name, row in summary.iterrows():
        lines.append(
            f"| {name} | {_fmt(row['total_return'], True)} | {_fmt(row['cagr'], True)} | "
            f"{_fmt(row['max_drawdown'], True)} | {_fmt(row['sharpe_rf0'])} | "
            f"{_fmt(row['calmar'])} | {_fmt(row['average_exposure'], True)} | "
            f"{_fmt(row['total_turnover'])} |"
        )
    lines.extend(["", "## 最新目标仓位", ""])
    for name, result in results.items():
        latest = result.signal_weights.iloc[-1]
        selected = latest[latest > 0].sort_values(ascending=False)
        allocation = "，".join(f"{symbol} {weight:.1%}" for symbol, weight in selected.items()) or "全部现金"
        lines.append(f"- {name}：{allocation}；现金 {1.0 - latest.sum():.1%}")
    lines.extend([
        "",
        "## 尚不能得出的结论",
        "",
        "- 不能仅凭一次全样本结果决定70%、90%或100%中哪档用于真实资金。",
        "- 尚未完成滚动样本外、参数稳定性、ETF退市/成立日期偏差和跨市场数据源核对。",
        "- 新闻风险层只定义了可审计接口，尚未证明能够提高收益或降低回撤。",
    ])
    (output / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    args = parse_args()
    symbols = list(dict.fromkeys(item.strip().upper() for item in args.symbols.split(",") if item.strip()))
    if args.benchmark not in symbols:
        raise SystemExit("benchmark必须包含在symbols中")
    if args.data_dir:
        prices, metadata = load_price_directory(args.data_dir, symbols)
    else:
        prices, metadata = fetch_price_panel(symbols, args.start, args.end)
    prices = prices.loc[pd.Timestamp(args.start):pd.Timestamp(args.end)]

    output = Path(args.output_dir) / f"etf_rotation_v1_{datetime.now():%Y%m%d_%H%M%S}"
    output.mkdir(parents=True, exist_ok=False)
    prices.to_csv(output / "adjusted_close.csv", index_label="date")
    save_source_metadata(output / "data_sources.json", metadata)

    results = {}
    rows = {}
    for exposure in (0.70, 0.90, 1.00):
        name = f"rotation_{int(exposure * 100)}"
        config = RotationConfig(
            target_exposure=exposure,
            max_asset_weight=args.max_asset_weight,
            top_n=args.top_n,
            transaction_cost_bps=args.cost_bps,
        )
        result = run_rotation_backtest(prices, args.benchmark, config)
        results[name] = result
        rows[name] = result.metrics
        curve = pd.DataFrame({
            "equity": result.equity,
            "return": result.returns,
            "turnover": result.turnover,
            "cost": result.costs,
            "exposure": result.weights.sum(axis=1),
        }).join(result.weights.add_prefix("weight_"))
        curve.to_csv(output / f"{name}_curve.csv", index_label="date")
        result.signal_weights.to_csv(output / f"{name}_signals.csv", index_label="signal_date")

    evaluation_start = next(iter(results.values())).returns.index.min()
    rows["static_equal_weight"] = equal_weight_metrics(prices, args.benchmark, evaluation_start)
    rows[f"buy_hold_{args.benchmark}"] = buy_and_hold_metrics(prices, args.benchmark, evaluation_start)
    summary = pd.DataFrame.from_dict(rows, orient="index").reindex(columns=METRIC_ORDER)
    summary.to_csv(output / "summary.csv", index_label="strategy")
    (output / "config.json").write_text(
        json.dumps({"args": vars(args), "configs": {key: value.config.to_dict() for key, value in results.items()}}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    write_report(output, summary, prices, results, args)
    print(summary[["total_return", "cagr", "max_drawdown", "sharpe_rf0", "average_exposure"]].to_string())
    print(f"\n结果已保存：{output.resolve()}")


if __name__ == "__main__":
    main()
