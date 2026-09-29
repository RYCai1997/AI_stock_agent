"""One-parameter-at-a-time neighborhood checks; never rewrite frozen V1."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pandas as pd

from selector.strategy import OFFICIAL_STRATEGY

from .corporate_actions import CorporateAction
from .engine import BacktestEngine, DailyBar
from .fees import FeeModel
from .metrics import performance
from .official import OfficialSignalProvider, OfficialSnapshot


EMA_VALUES = (150, 175, 200, 225, 250)
STOP_VALUES = (.075, .10, .125, .15)
TOP_N_VALUES = (3, 5, 7, 10)


def _snapshot_with_ema(snapshot: OfficialSnapshot, as_of: str, span: int,
                       adjusted_stocks: pd.DataFrame,
                       adjusted_index: pd.DataFrame) -> OfficialSnapshot:
    stocks = adjusted_stocks.copy()
    index = adjusted_index.copy()
    stocks = stocks[stocks["date"].le(as_of)].sort_values("date")
    index = index[index["date"].le(as_of)].sort_values("date")
    if index.empty or len(index) < span:
        raise ValueError(f"insufficient adjusted benchmark history for EMA{span} on {as_of}")
    index_close = index["close"].astype(float)
    market_ema = index_close.ewm(span=span, adjust=False, min_periods=span).mean().iloc[-1]
    metrics = snapshot.metrics.copy()
    for row_index, row in metrics.iterrows():
        history = stocks[stocks["ticker"].eq(row["ticker"])]
        if len(history) < span:
            metrics.at[row_index, "ema200"] = float("nan")
            continue
        closes = history["close"].astype(float)
        metrics.at[row_index, "price"] = closes.iloc[-1]
        metrics.at[row_index, "ema200"] = closes.ewm(span=span, adjust=False,
                                                     min_periods=span).mean().iloc[-1]
    provider = dict(snapshot.provider)
    provider["market_trend"] = "up" if index_close.iloc[-1] > market_ema else "down"
    return OfficialSnapshot(metrics, provider)


def parameter_surface(*, snapshots: dict[str, OfficialSnapshot],
                      bars: list[DailyBar], actions: list[CorporateAction],
                      fee_model: FeeModel, initial_cash: float,
                      audit_dir: Path,
                      adjusted_stocks: pd.DataFrame | None = None,
                      adjusted_index: pd.DataFrame | None = None,
                      market_calendar: list[str] | None = None) -> pd.DataFrame:
    rows = []
    scenarios = ([('ema', value) for value in EMA_VALUES] +
                 [('stop', value) for value in STOP_VALUES] +
                 [('top_n', value) for value in TOP_N_VALUES])
    for parameter, value in scenarios:
        row = {"parameter": parameter, "value": value, "formal_v1_value": {
            "ema": 200, "stop": OFFICIAL_STRATEGY.stop_loss_fraction,
            "top_n": OFFICIAL_STRATEGY.max_new_positions_per_window}[parameter]}
        if parameter == "ema":
            if adjusted_stocks is None or adjusted_index is None:
                rows.append({**row, "status": "not_run_missing_adjusted_history"})
                continue
            try:
                scenario_snapshots = {
                    date: _snapshot_with_ema(snapshot, date, value,
                                             adjusted_stocks, adjusted_index)
                    for date, snapshot in snapshots.items()}
            except ValueError as exc:
                rows.append({**row, "status": f"not_run: {exc}"})
                continue
        else:
            scenario_snapshots = snapshots
        strategy = OFFICIAL_STRATEGY
        if parameter == "top_n":
            strategy = replace(strategy, max_new_positions_per_window=value,
                               fixed_position_fraction=.30 / value)
        provider = OfficialSignalProvider(
            scenario_snapshots, audit_dir / f"{parameter}_{value}", strategy=strategy)
        engine = BacktestEngine(
            initial_cash, fee_model=fee_model,
            stop_fraction=value if parameter == "stop" else OFFICIAL_STRATEGY.stop_loss_fraction,
        ).run(bars, [], actions, signal_provider=provider,
              market_calendar=market_calendar)
        measures = performance(engine.daily_nav, engine.trades)
        rows.append({**row, "status": "completed",
                     "cumulative_return": measures["cumulative_return"],
                     "maximum_drawdown": measures["maximum_drawdown"],
                     "turnover": measures["turnover"],
                     "transaction_costs": measures["total_transaction_costs"]})
    return pd.DataFrame(rows)
