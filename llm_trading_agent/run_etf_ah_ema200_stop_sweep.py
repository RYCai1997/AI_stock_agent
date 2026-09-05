# -*- coding: utf-8 -*-
"""A/H exposure ETF sweep: EMA200 entry, 10% trailing exit, and fixed stops."""

import argparse
import json
from datetime import datetime
from pathlib import Path

import pandas as pd

from portfolio_core.backtest import buy_and_hold_metrics, equal_weight_metrics
from portfolio_core.ema_trailing import run_ema_trailing_portfolio


ETF_NAMES = {
    "510300.SS": "沪深300ETF",
    "159949.SZ": "创业板50ETF",
    "513180.SS": "恒生科技ETF",
    "510500.SS": "中证500ETF",
    "510880.SS": "红利ETF",
}
SYMBOLS = list(ETF_NAMES)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="A/H股方向ETF EMA200与2/3/5%止损比较")
    parser.add_argument("--prices", required=True, help="包含date及ETF代码列的复权收盘价CSV")
    parser.add_argument("--output-dir", default="runs")
    parser.add_argument("--cost-bps", type=float, default=10.0)
    return parser.parse_args()


def _format_optional(value, kind: str) -> str:
    if pd.isna(value):
        return "N/A"
    if kind == "count":
        return str(int(value))
    return f"{value:.2%}"


def main() -> None:
    args = parse_args()
    prices = pd.read_csv(args.prices, parse_dates=["date"]).set_index("date")[SYMBOLS]
    common_start = max(prices[symbol].first_valid_index() for symbol in SYMBOLS)
    variants = {
        "stop_2pct": 0.02,
        "stop_3pct": 0.03,
        "stop_5pct": 0.05,
        "no_fixed_stop": None,
    }
    results = {
        name: run_ema_trailing_portfolio(
            prices,
            SYMBOLS,
            benchmark="510300.SS",
            trailing_drawdown=0.10,
            stop_loss=stop_loss,
            transaction_cost_bps=args.cost_bps,
            evaluation_start=common_start,
        )
        for name, stop_loss in variants.items()
    }
    rows = {name: result.metrics for name, result in results.items()}
    rows["static_equal_weight_5etf"] = equal_weight_metrics(prices, "510300.SS", common_start)
    rows["buy_hold_510300"] = buy_and_hold_metrics(prices, "510300.SS", common_start)
    summary = pd.DataFrame.from_dict(rows, orient="index")

    standalone_rows = []
    for symbol in SYMBOLS:
        for variant, stop_loss in variants.items():
            standalone = run_ema_trailing_portfolio(
                prices[[symbol]],
                [symbol],
                benchmark=symbol,
                trailing_drawdown=0.10,
                stop_loss=stop_loss,
                target_exposure=1.0,
                max_asset_weight=1.0,
                transaction_cost_bps=args.cost_bps,
                evaluation_start=common_start,
            )
            standalone_rows.append({
                "symbol": symbol,
                "name": ETF_NAMES[symbol],
                "variant": variant,
                **standalone.metrics,
            })
        standalone_rows.append({
            "symbol": symbol,
            "name": ETF_NAMES[symbol],
            "variant": "buy_hold",
            **buy_and_hold_metrics(prices, symbol, common_start),
        })
    standalone_summary = pd.DataFrame(standalone_rows)

    output = Path(args.output_dir) / f"etf_ah_ema200_stop_sweep_{datetime.now():%Y%m%d_%H%M%S}"
    output.mkdir(parents=True, exist_ok=False)
    prices.to_csv(output / "input_adjusted_close.csv", index_label="date")
    summary.to_csv(output / "summary.csv", index_label="strategy")
    standalone_summary.to_csv(output / "standalone_summary.csv", index=False)
    for name, result in results.items():
        pd.DataFrame({
            "equity": result.equity,
            "return": result.returns,
            "turnover": result.turnover,
            "cost": result.costs,
            "exposure": result.weights.sum(axis=1),
        }).join(result.weights.add_prefix("weight_")).to_csv(output / f"{name}_curve.csv", index_label="date")
        result.trades.to_csv(output / f"{name}_trades.csv", index=False)
        if not result.trades.empty:
            result.trades.groupby("symbol").agg(
                trades=("gross_return", "size"),
                win_rate=("gross_return", lambda values: float((values > 0).mean())),
                average_trade_return=("gross_return", "mean"),
                median_trade_return=("gross_return", "median"),
                average_holding_days=("holding_days", "mean"),
            ).to_csv(output / f"{name}_by_symbol.csv")
            result.trades.groupby("exit_reason").agg(
                trades=("gross_return", "size"),
                win_rate=("gross_return", lambda values: float((values > 0).mean())),
                average_return=("gross_return", "mean"),
            ).to_csv(output / f"{name}_by_exit_reason.csv")

    report = [
        "# A/H股方向ETF：EMA200突破、10%峰值回撤、固定止损比较",
        "",
        "- 标的：沪深300、创业板50、恒生科技、中证500、红利ETF；每只固定20%。",
        "- 上穿EMA200后下一交易日收盘买入；峰值回撤10%后下一交易日收盘退出。",
        "- 比较2%、3%、5%买入价止损和不设固定止损，均按下一交易日收盘执行。",
        f"- 共同评价期：{common_start.date()}至{prices.index.max().date()}，由最晚成立的恒生科技ETF决定。",
        f"- 单边换手成本：{args.cost_bps:.1f} bps。",
        "",
        "| 策略 | 累计盈亏 | 每10万元期末权益 | CAGR | 最大回撤 | Sharpe | 平均仓位 | 交易数 | 胜率 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for name, row in summary.iterrows():
        report.append(
            f"| {name} | {row['total_return']:.2%} | {100000 * (1 + row['total_return']):,.0f} | "
            f"{row['cagr']:.2%} | {row['max_drawdown']:.2%} | {row['sharpe_rf0']:.3f} | "
            f"{row['average_exposure']:.2%} | {_format_optional(row.get('closed_trades', float('nan')), 'count')} | "
            f"{_format_optional(row.get('win_rate', float('nan')), 'percent')} |"
        )
    report.extend([
        "",
        "## 各ETF独立适用性",
        "",
        "详细的每只ETF、每档止损独立结果保存在 `standalone_summary.csv`；独立测试按单标的100%仓位计算，用于比较规则适用性，不代表建议集中持仓。",
        "",
        "## 数据与结论边界",
        "",
        "- 使用Yahoo复权收盘价，日期按各ETF交易所时区转换。",
        "- 共同样本只有约五年，且包含恒生科技深度熊市，不能单独代表完整市场周期。",
        "- 胜率只统计评价期内已完成交易，不含期末持仓。",
        "- 未计入A股最低佣金、申赎、跨境ETF溢价或涨跌停无法成交等微观限制。",
        "- 本轮不调用LLM、不改变任何实盘配置。",
    ])
    (output / "report.md").write_text("\n".join(report) + "\n", encoding="utf-8")
    (output / "config.json").write_text(json.dumps({
        "etfs": ETF_NAMES,
        "ema_days": 200,
        "trailing_drawdown": 0.10,
        "fixed_stops": [0.02, 0.03, 0.05, None],
        "weight_per_etf": 0.20,
        "common_evaluation_start": str(common_start.date()),
        "transaction_cost_bps": args.cost_bps,
        "execution": "signal_close_then_next_close",
        "price_input_original": str(Path(args.prices).resolve()),
        "price_input_archived": "input_adjusted_close.csv",
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    after_common = prices.loc[common_start:]
    daily_returns = after_common.pct_change(fill_method=None)
    (output / "data_quality.json").write_text(json.dumps({
        "status": "usable_with_caveat",
        "grain": "one adjusted close per mainland-listed ETF per trading date",
        "common_start": str(common_start.date()),
        "last_date": str(after_common.index.max().date()),
        "rows_after_common_start": int(len(after_common)),
        "duplicate_dates": int(after_common.index.duplicated().sum()),
        "weekend_dates": int((after_common.index.dayofweek >= 5).sum()),
        "missing_prices": {symbol: int(after_common[symbol].isna().sum()) for symbol in SYMBOLS},
        "nonpositive_prices": int((after_common <= 0).sum().sum()),
        "maximum_absolute_daily_return": {symbol: float(daily_returns[symbol].abs().max()) for symbol in SYMBOLS},
        "note": "513180.SS has two missing rows immediately after listing and is forward-filled by the core for at most three rows. Limit-move observations are plausible but require a second source before final selection.",
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(summary[["total_return", "cagr", "max_drawdown", "sharpe_rf0", "average_exposure", "closed_trades", "win_rate"]].to_string())
    print(f"\n结果已保存：{output.resolve()}")


if __name__ == "__main__":
    main()
