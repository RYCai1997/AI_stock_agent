"""Full and exposure-matched CSI300 close-to-close research benchmarks."""

from __future__ import annotations

import math

import pandas as pd


def benchmark_comparison(strategy_daily_nav: list[dict],
                         index_prices: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    strategy = pd.DataFrame(strategy_daily_nav).sort_values("date").reset_index(drop=True)
    if not {"date", "close"} <= set(index_prices):
        raise ValueError("index prices require date and close")
    prices = index_prices[["date", "close"]].sort_values("date").reset_index(drop=True)
    if strategy["date"].tolist() != prices["date"].tolist():
        raise ValueError("benchmark dates do not exactly match strategy sessions")
    prices["close"] = pd.to_numeric(prices["close"], errors="raise")
    if len(prices) < 2 or prices["close"].isna().any() or (prices["close"] <= 0).any():
        raise ValueError("benchmark closes must be positive with at least two sessions")
    index_returns = prices["close"].pct_change().fillna(0)
    exposure = strategy["actual_exposure"].astype(float)
    if exposure.isna().any() or ((exposure < -1e-9) | (exposure > 1 + 1e-9)).any():
        raise ValueError("strategy exposure must be in [0,1]")
    prior_exposure = exposure.shift(1).fillna(0)
    matched_returns = prior_exposure * index_returns
    full_nav = (1 + index_returns).cumprod()
    matched_nav = (1 + matched_returns).cumprod()
    strategy_nav = strategy["daily_nav"].astype(float)
    result = pd.DataFrame({
        "date": strategy["date"], "strategy_nav": strategy_nav,
        "full_csi300_nav": full_nav,
        "exposure_matched_csi300_nav": matched_nav,
        "strategy_exposure": exposure,
        "benchmark_exposure_used": prior_exposure,
        "csi300_return": index_returns,
        "exposure_matched_return": matched_returns,
    })
    strategy_returns = strategy_nav.pct_change().fillna(0)

    def relative(comparison_returns: pd.Series) -> tuple[float, float, float]:
        active = (strategy_returns - comparison_returns).iloc[1:]
        error = float(active.std(ddof=1) * math.sqrt(252)) if len(active) > 1 else float("nan")
        ratio = float(active.mean() * 252 / error) if error > 0 else float("nan")
        variance = float(comparison_returns.iloc[1:].var(ddof=1))
        beta = float(strategy_returns.iloc[1:].cov(comparison_returns.iloc[1:]) / variance) if variance > 0 else float("nan")
        return error, ratio, beta

    full_te, full_ir, full_beta = relative(index_returns)
    matched_te, matched_ir, matched_beta = relative(matched_returns)
    metrics = {
        "strategy_cumulative_return": float(strategy_nav.iloc[-1] - 1),
        "full_csi300_cumulative_return": float(full_nav.iloc[-1] - 1),
        "matched_csi300_cumulative_return": float(matched_nav.iloc[-1] - 1),
        "excess_vs_full_csi300": float(strategy_nav.iloc[-1] - full_nav.iloc[-1]),
        "excess_vs_matched_csi300": float(strategy_nav.iloc[-1] - matched_nav.iloc[-1]),
        "tracking_error_vs_full": full_te,
        "information_ratio_vs_full": full_ir,
        "beta_vs_full": full_beta,
        "tracking_error_vs_matched": matched_te,
        "information_ratio_vs_matched": matched_ir,
        "beta_vs_matched": matched_beta,
    }
    return result, metrics
