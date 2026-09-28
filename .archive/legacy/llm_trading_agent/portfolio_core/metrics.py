"""Portfolio-level performance and risk metrics."""

import math

import numpy as np
import pandas as pd


def drawdown(equity: pd.Series) -> pd.Series:
    return equity / equity.cummax() - 1.0


def performance_metrics(
    returns: pd.Series,
    equity: pd.Series,
    benchmark_returns: pd.Series,
    turnover: pd.Series,
    costs: pd.Series,
    exposure: pd.Series,
    annualization_days: int = 252,
) -> dict[str, float]:
    aligned = pd.concat([returns.rename("strategy"), benchmark_returns.rename("benchmark")], axis=1).dropna()
    observations = int(returns.notna().sum())
    years = observations / annualization_days if observations else 0.0
    total_return = float(equity.iloc[-1] - 1.0) if not equity.empty else 0.0
    cagr = (float(equity.iloc[-1]) ** (1.0 / years) - 1.0) if years > 0 and equity.iloc[-1] > 0 else float("nan")
    volatility = float(returns.std(ddof=0) * math.sqrt(annualization_days))
    sharpe = float(returns.mean() / returns.std(ddof=0) * math.sqrt(annualization_days)) if returns.std(ddof=0) > 0 else float("nan")
    max_drawdown = float(drawdown(equity).min()) if not equity.empty else 0.0
    calmar = cagr / abs(max_drawdown) if max_drawdown < 0 and not math.isnan(cagr) else float("nan")

    upside = aligned.loc[aligned["benchmark"] > 0]
    downside = aligned.loc[aligned["benchmark"] < 0]
    upside_capture = upside["strategy"].sum() / upside["benchmark"].sum() if not upside.empty else float("nan")
    downside_capture = downside["strategy"].sum() / downside["benchmark"].sum() if not downside.empty else float("nan")
    tail_count = max(1, int(math.ceil(len(returns.dropna()) * 0.05)))
    worst_5pct_mean = float(returns.dropna().nsmallest(tail_count).mean()) if observations else float("nan")

    return {
        "total_return": total_return,
        "cagr": cagr,
        "max_drawdown": max_drawdown,
        "annual_volatility": volatility,
        "sharpe_rf0": sharpe,
        "calmar": calmar,
        "upside_capture": float(upside_capture),
        "downside_capture": float(downside_capture),
        "worst_5pct_daily_mean": worst_5pct_mean,
        "average_exposure": float(exposure.mean()),
        "maximum_exposure": float(exposure.max()),
        "total_turnover": float(turnover.sum()),
        "transaction_cost": float(costs.sum()),
        "rebalance_count": float((turnover > 1e-12).sum()),
        "observations": float(observations),
    }
