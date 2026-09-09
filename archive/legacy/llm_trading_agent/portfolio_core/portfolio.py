"""Monthly selection and deterministic portfolio construction."""

import pandas as pd

from .config import RotationConfig


def month_end_dates(index: pd.DatetimeIndex) -> list[pd.Timestamp]:
    """Return the final observed trading date in each calendar month."""
    series = pd.Series(index=index, data=index)
    return list(series.groupby(index.to_period("M")).last())


def build_signal_weights(
    score: pd.DataFrame,
    eligible: pd.DataFrame,
    config: RotationConfig,
) -> pd.DataFrame:
    """Build target weights at signal dates; unallocated exposure remains cash."""
    rows: dict[pd.Timestamp, pd.Series] = {}
    for signal_date in month_end_dates(score.index):
        valid = score.loc[signal_date].where(eligible.loc[signal_date]).dropna()
        selected = list(valid.nlargest(config.top_n).index)
        target = pd.Series(0.0, index=score.columns, dtype=float)
        if selected:
            per_asset = min(config.max_asset_weight, config.target_exposure / len(selected))
            target.loc[selected] = per_asset
        rows[signal_date] = target
    if not rows:
        return pd.DataFrame(index=pd.DatetimeIndex([]), columns=score.columns, dtype=float)
    return pd.DataFrame.from_dict(rows, orient="index").sort_index()


def schedule_close_execution(
    signal_weights: pd.DataFrame,
    trading_index: pd.DatetimeIndex,
) -> pd.DataFrame:
    """Schedule each month-end signal for the next observed trading-day close."""
    events = pd.DataFrame(float("nan"), index=trading_index, columns=signal_weights.columns)
    for signal_date, target in signal_weights.iterrows():
        position = trading_index.searchsorted(signal_date, side="right")
        if position < len(trading_index):
            events.loc[trading_index[position]] = target
    return events
