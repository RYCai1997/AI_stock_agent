"""EMA200 breakout plus close-based trailing-exit experiment."""

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .factors import validate_prices
from .metrics import performance_metrics


@dataclass
class EMATrailingResult:
    equity: pd.Series
    returns: pd.Series
    weights: pd.DataFrame
    turnover: pd.Series
    costs: pd.Series
    trades: pd.DataFrame
    metrics: dict[str, float]


def run_ema_trailing_portfolio(
    prices: pd.DataFrame,
    symbols: list[str],
    benchmark: str = "SPY",
    ema_days: int = 200,
    trailing_drawdown: float = 0.05,
    stop_loss: float | None = None,
    exit_below_ema: bool = False,
    target_exposure: float = 1.0,
    max_asset_weight: float = 0.20,
    transaction_cost_bps: float = 10.0,
    evaluation_start: str | pd.Timestamp | None = None,
) -> EMATrailingResult:
    """Run a long-only portfolio where each ETF is an independent fixed sleeve.

    Signals use the current adjusted close. Orders execute at the next adjusted
    close, and the resulting weights participate from the following close-to-close
    return. The trailing peak starts at the entry close, so an immediate 5% loss
    also exits even if the trade was never profitable.
    """
    if ema_days < 2:
        raise ValueError("ema_days must be at least 2")
    if not 0 < trailing_drawdown < 1:
        raise ValueError("trailing_drawdown must be in (0, 1)")
    if stop_loss is not None and not 0 < stop_loss < 1:
        raise ValueError("stop_loss must be in (0, 1) when supplied")
    if not 0 < target_exposure <= 1 or not 0 < max_asset_weight <= 1:
        raise ValueError("exposure and weight limits must be in (0, 1]")
    if transaction_cost_bps < 0:
        raise ValueError("transaction_cost_bps cannot be negative")

    clean = validate_prices(prices)[symbols]
    if benchmark not in clean.columns:
        raise ValueError("benchmark must be included in symbols")
    per_asset_weight = min(max_asset_weight, target_exposure / len(symbols))
    ema = clean.ewm(span=ema_days, adjust=False, min_periods=ema_days).mean()
    cross_above = (clean > ema) & (clean.shift(1) <= ema.shift(1))
    asset_returns = clean.pct_change(fill_method=None).fillna(0.0)

    held = pd.Series(0.0, index=symbols)
    peaks = pd.Series(np.nan, index=symbols)
    entries: dict[str, tuple[pd.Timestamp, float] | None] = {symbol: None for symbol in symbols}
    pending: dict[str, tuple[str, str]] = {}
    weight_rows: list[pd.Series] = []
    gross_rows: list[float] = []
    turnover_rows: list[float] = []
    trade_rows: list[dict] = []

    for current_date in clean.index:
        gross_rows.append(float((held * asset_returns.loc[current_date]).sum()))
        before = held.copy()
        executed = pending
        pending = {}

        for symbol, (action, reason) in executed.items():
            price = float(clean.loc[current_date, symbol])
            if action == "buy" and held[symbol] == 0:
                held[symbol] = per_asset_weight
                peaks[symbol] = price
                entries[symbol] = (current_date, price)
            elif action == "sell" and held[symbol] > 0:
                entry_date, entry_price = entries[symbol]
                trade_rows.append({
                    "symbol": symbol,
                    "entry_date": entry_date,
                    "exit_date": current_date,
                    "entry_price": entry_price,
                    "exit_price": price,
                    "gross_return": price / entry_price - 1.0,
                    "holding_days": int((current_date - entry_date).days),
                    "exit_reason": reason,
                })
                held[symbol] = 0.0
                peaks[symbol] = np.nan
                entries[symbol] = None

        turnover_rows.append(float((held - before).abs().sum()))
        weight_rows.append(held.copy())

        for symbol in symbols:
            price = clean.loc[current_date, symbol]
            ema_value = ema.loc[current_date, symbol]
            if pd.isna(price) or pd.isna(ema_value):
                continue
            if held[symbol] > 0:
                peaks[symbol] = max(float(peaks[symbol]), float(price))
                trailing_exit = float(price) <= float(peaks[symbol]) * (1.0 - trailing_drawdown)
                entry_price = entries[symbol][1]
                stop_exit = stop_loss is not None and float(price) <= float(entry_price) * (1.0 - stop_loss)
                trend_exit = exit_below_ema and float(price) < float(ema_value)
                if stop_exit:
                    pending[symbol] = ("sell", "stop_loss")
                elif trailing_exit:
                    pending[symbol] = ("sell", "trailing_drawdown")
                elif trend_exit:
                    pending[symbol] = ("sell", "below_ema")
            elif bool(cross_above.loc[current_date, symbol]):
                pending[symbol] = ("buy", "ema_cross_above")

    weights = pd.DataFrame(weight_rows, index=clean.index)
    gross = pd.Series(gross_rows, index=clean.index, name="gross_return")
    turnover = pd.Series(turnover_rows, index=clean.index, name="turnover")
    costs = turnover * (transaction_cost_bps / 10_000.0)
    net = gross - costs

    invested = weights.sum(axis=1) > 0
    if not invested.any():
        raise ValueError("no EMA breakout entry occurred")
    automatic_start = invested[invested].index[0]
    if evaluation_start is None:
        evaluation_start = automatic_start
    else:
        evaluation_start = max(clean.index.min(), pd.Timestamp(evaluation_start))
    weights = weights.loc[evaluation_start:]
    turnover = turnover.loc[evaluation_start:]
    costs = costs.loc[evaluation_start:]
    net = net.loc[evaluation_start:]
    equity = (1.0 + net).cumprod()
    benchmark_returns = asset_returns.loc[evaluation_start:, benchmark].copy()
    benchmark_returns.iloc[0] = 0.0
    metrics = performance_metrics(
        net,
        equity,
        benchmark_returns,
        turnover,
        costs,
        weights.sum(axis=1),
    )
    trades = pd.DataFrame(trade_rows)
    if not trades.empty:
        trades = trades.loc[pd.to_datetime(trades["exit_date"]) >= evaluation_start].reset_index(drop=True)
    metrics["closed_trades"] = float(len(trades))
    metrics["win_rate"] = float((trades["gross_return"] > 0).mean()) if not trades.empty else float("nan")
    metrics["average_trade_return"] = float(trades["gross_return"].mean()) if not trades.empty else float("nan")
    metrics["average_holding_days"] = float(trades["holding_days"].mean()) if not trades.empty else float("nan")
    return EMATrailingResult(equity, net, weights, turnover, costs, trades, metrics)
