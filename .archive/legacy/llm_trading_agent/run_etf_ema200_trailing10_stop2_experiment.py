# -*- coding: utf-8 -*-
"""Five-ETF EMA200 breakout, 10% trailing exit, and 2% stop-loss experiment."""

import argparse
import json
from datetime import datetime
from pathlib import Path

import pandas as pd

from portfolio_core.backtest import buy_and_hold_metrics, equal_weight_metrics
from portfolio_core.ema_trailing import run_ema_trailing_portfolio


SYMBOLS = ["SPY", "QQQ", "IWM", "TLT", "GLD"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="五ETF EMA200突破+10%峰值回撤+2%止损实验")
    parser.add_argument("--prices", required=True, help="包含date及ETF代码列的复权收盘价CSV")
    parser.add_argument("--output-dir", default="runs")
    parser.add_argument("--cost-bps", type=float, default=10.0)
    return parser.parse_args()


def _count(value) -> str:
    return "N/A" if pd.isna(value) else str(int(value))


def _percent(value) -> str:
    return "N/A" if pd.isna(value) else f"{value:.2%}"


def main() -> None:
    args = parse_args()
    prices = pd.read_csv(args.prices, parse_dates=["date"]).set_index("date")[SYMBOLS]
    variants = {
        "trailing10_stop2": 0.02,
        "trailing10_no_stop": None,
    }
    results = {
        name: run_ema_trailing_portfolio(
            prices,
            SYMBOLS,
            trailing_drawdown=0.10,
            stop_loss=stop_loss,
            transaction_cost_bps=args.cost_bps,
        )
        for name, stop_loss in variants.items()
    }
    common_start = max(result.returns.index.min() for result in results.values())
    rows = {name: result.metrics for name, result in results.items()}
    rows["static_equal_weight_5etf"] = equal_weight_metrics(prices, "SPY", common_start)
    rows["buy_hold_SPY"] = buy_and_hold_metrics(prices, "SPY", common_start)
    summary = pd.DataFrame.from_dict(rows, orient="index")

    output = Path(args.output_dir) / f"etf_ema200_trailing10_stop2_{datetime.now():%Y%m%d_%H%M%S}"
    output.mkdir(parents=True, exist_ok=False)
    summary.to_csv(output / "summary.csv", index_label="strategy")
    for name, result in results.items():
        curve = pd.DataFrame({
            "equity": result.equity,
            "return": result.returns,
            "turnover": result.turnover,
            "cost": result.costs,
            "exposure": result.weights.sum(axis=1),
        }).join(result.weights.add_prefix("weight_"))
        curve.to_csv(output / f"{name}_curve.csv", index_label="date")
        result.trades.to_csv(output / f"{name}_trades.csv", index=False)
        if not result.trades.empty:
            by_symbol = result.trades.groupby("symbol").agg(
                trades=("gross_return", "size"),
                win_rate=("gross_return", lambda values: float((values > 0).mean())),
                total_compound_return=("gross_return", lambda values: float((1.0 + values).prod() - 1.0)),
                average_trade_return=("gross_return", "mean"),
                median_trade_return=("gross_return", "median"),
                average_holding_days=("holding_days", "mean"),
            )
            by_symbol.to_csv(output / f"{name}_by_symbol.csv")
            result.trades.groupby("exit_reason").agg(
                trades=("gross_return", "size"),
                win_rate=("gross_return", lambda values: float((values > 0).mean())),
                average_return=("gross_return", "mean"),
            ).to_csv(output / f"{name}_by_exit_reason.csv")

    report = [
        "# 五ETF EMA200突破 + 10%峰值回撤 + 2%亏损止损",
        "",
        f"- 标的：{', '.join(SYMBOLS)}；每只固定20%，总仓位最高100%。",
        "- 上穿EMA200后下一交易日收盘买入。",
        "- 相对买入价收盘亏损2%触发止损；相对持仓最高收盘价回撤10%触发移动退出。",
        "- 所有退出均在下一交易日收盘执行，因此跳空时实际亏损可能超过2%。",
        f"- 共同评价期：{common_start.date()} 至 {prices.index.max().date()}；单边换手成本{args.cost_bps:.1f} bps。",
        "",
        "| 策略 | 最终累计盈亏 | CAGR | 最大回撤 | Sharpe | Calmar | 平均仓位 | 完成交易 | 胜率 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for name, row in summary.iterrows():
        report.append(
            f"| {name} | {row['total_return']:.2%} | {row['cagr']:.2%} | {row['max_drawdown']:.2%} | "
            f"{row['sharpe_rf0']:.3f} | {row['calmar']:.3f} | {row['average_exposure']:.2%} | "
            f"{_count(row.get('closed_trades', float('nan')))} | {_percent(row.get('win_rate', float('nan')))} |"
        )
    report.extend([
        "",
        "## 边界",
        "",
        "- 胜率按已经完成的单ETF交易计算，不含期末仍持有的仓位。",
        "- 本轮不调用LLM、不使用新闻、不改变原策略或实盘配置。",
    ])
    (output / "report.md").write_text("\n".join(report) + "\n", encoding="utf-8")
    (output / "config.json").write_text(json.dumps({
        "symbols": SYMBOLS,
        "ema_days": 200,
        "trailing_drawdown": 0.10,
        "stop_loss": 0.02,
        "weight_per_etf": 0.20,
        "transaction_cost_bps": args.cost_bps,
        "execution": "signal_close_then_next_close",
        "price_input": str(Path(args.prices).resolve()),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(summary[["total_return", "cagr", "max_drawdown", "sharpe_rf0", "average_exposure", "closed_trades", "win_rate"]].to_string())
    print(f"\n结果已保存：{output.resolve()}")


if __name__ == "__main__":
    main()
