"""Causal ETF factor calculations."""

import pandas as pd

from .config import RotationConfig


def validate_prices(prices: pd.DataFrame) -> pd.DataFrame:
    """Return clean, ordered adjusted-close prices without silently backfilling history."""
    if prices.empty:
        raise ValueError("price table is empty")
    clean = prices.copy()
    clean.index = pd.to_datetime(clean.index, errors="raise")
    clean = clean[~clean.index.duplicated(keep="last")].sort_index()
    clean = clean.apply(pd.to_numeric, errors="coerce")
    clean = clean.where(clean > 0)
    clean = clean.dropna(axis=1, how="all")
    if clean.empty:
        raise ValueError("price table has no positive numeric series")
    # A short forward fill aligns occasional exchange holidays. It never fills pre-listing rows.
    return clean.ffill(limit=3)


def calculate_factors(prices: pd.DataFrame, config: RotationConfig) -> dict[str, pd.DataFrame]:
    """Calculate momentum and trend using information available at each row only."""
    clean = validate_prices(prices)
    return_6m = clean / clean.shift(config.momentum_6m_days) - 1.0
    return_12_1 = clean.shift(config.skip_recent_days) / clean.shift(config.momentum_12m_days) - 1.0
    score = (
        config.momentum_6m_weight * return_6m
        + config.momentum_12_1_weight * return_12_1
    )
    moving_average = clean.rolling(config.trend_days, min_periods=config.trend_days).mean()
    trend_ok = clean > moving_average
    eligible = trend_ok & score.notna()
    if config.require_positive_momentum:
        eligible &= score > 0
    return {
        "prices": clean,
        "return_6m": return_6m,
        "return_12_1": return_12_1,
        "score": score,
        "moving_average": moving_average,
        "eligible": eligible,
    }
