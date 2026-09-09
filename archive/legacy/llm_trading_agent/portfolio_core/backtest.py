"""Close-to-close ETF rotation backtester with delayed execution."""

from dataclasses import dataclass

import pandas as pd

from .config import RotationConfig
from .factors import calculate_factors
from .metrics import performance_metrics
from .portfolio import build_signal_weights, schedule_close_execution


@dataclass
class BacktestResult:
    config: RotationConfig
    equity: pd.Series
    returns: pd.Series
    weights: pd.DataFrame
    signal_weights: pd.DataFrame
    turnover: pd.Series
    costs: pd.Series
    metrics: dict[str, float]
    factors: dict[str, pd.DataFrame]


def run_rotation_backtest(
    prices: pd.DataFrame,
    benchmark: str,
    config: RotationConfig,
) -> BacktestResult:
    """Run a causal backtest using adjusted closes and next-close execution."""
    factors = calculate_factors(prices, config)
    clean = factors["prices"]
    if benchmark not in clean.columns:
        raise ValueError(f"benchmark {benchmark!r} is not in the price table")

    signal_weights = build_signal_weights(factors["score"], factors["eligible"], config)
    execution_events = schedule_close_execution(signal_weights, clean.index)
    active_weights = execution_events.ffill().fillna(0.0)

    nonzero_events = execution_events.fillna(0.0).abs().sum(axis=1) > 0
    if not nonzero_events.any():
        raise ValueError("no eligible ETF was selected; use a longer history or inspect the universe")
    evaluation_start = nonzero_events[nonzero_events].index[0]

    asset_returns = clean.pct_change(fill_method=None).fillna(0.0)
    # A signal observed at month-end is executed at the next close. The new weights
    # therefore participate in returns only after that execution close.
    gross_returns = (active_weights.shift(1).fillna(0.0) * asset_returns).sum(axis=1)
    turnover = active_weights.diff().abs().sum(axis=1)
    if not active_weights.empty:
        turnover.iloc[0] = active_weights.iloc[0].abs().sum()
    costs = turnover * config.cost_rate
    net_returns = gross_returns - costs
    net_returns = net_returns.loc[evaluation_start:]
    active_weights = active_weights.loc[evaluation_start:]
    turnover = turnover.loc[evaluation_start:]
    costs = costs.loc[evaluation_start:]
    equity = (1.0 + net_returns).cumprod()
    benchmark_returns = asset_returns.loc[evaluation_start:, benchmark].copy()
    benchmark_returns.iloc[0] = 0.0
    exposure = active_weights.sum(axis=1)
    metrics = performance_metrics(
        net_returns,
        equity,
        benchmark_returns,
        turnover,
        costs,
        exposure,
        config.annualization_days,
    )
    return BacktestResult(
        config=config,
        equity=equity,
        returns=net_returns,
        weights=active_weights,
        signal_weights=signal_weights,
        turnover=turnover,
        costs=costs,
        metrics=metrics,
        factors=factors,
    )


def buy_and_hold_metrics(prices: pd.DataFrame, symbol: str, start_date=None) -> dict[str, float]:
    """Return directly comparable metrics for a fully invested benchmark."""
    series = prices[symbol].dropna()
    if start_date is not None:
        series = series.loc[pd.Timestamp(start_date):]
    returns = series.pct_change(fill_method=None).fillna(0.0)
    equity = (1.0 + returns).cumprod()
    zeros = pd.Series(0.0, index=returns.index)
    ones = pd.Series(1.0, index=returns.index)
    return performance_metrics(returns, equity, returns, zeros, zeros, ones)


def equal_weight_metrics(prices: pd.DataFrame, benchmark: str, start_date=None) -> dict[str, float]:
    """Metrics for a static, fully invested equal-weight universe baseline."""
    panel = prices.copy()
    if start_date is not None:
        panel = panel.loc[pd.Timestamp(start_date):]
    panel = panel.dropna(axis=1, how="any")
    if panel.empty:
        raise ValueError("no complete price series are available for the equal-weight baseline")
    asset_returns = panel.pct_change(fill_method=None).fillna(0.0)
    returns = asset_returns.mean(axis=1)
    equity = (1.0 + returns).cumprod()
    benchmark_returns = prices.loc[returns.index, benchmark].pct_change(fill_method=None).fillna(0.0)
    zeros = pd.Series(0.0, index=returns.index)
    ones = pd.Series(1.0, index=returns.index)
    return performance_metrics(returns, equity, benchmark_returns, zeros, zeros, ones)
