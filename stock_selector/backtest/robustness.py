"""Rerun-based influence diagnostics for predeclared opening windows."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

import pandas as pd

from .benchmarks import benchmark_comparison
from .corporate_actions import CorporateAction
from .engine import BacktestEngine, DailyBar, Signal
from .fees import FeeModel
from .metrics import performance


def leave_one_window_out(*, windows: list[str], bars: list[DailyBar],
                         actions: list[CorporateAction], fee_model: FeeModel,
                         initial_cash: float,
                         provider_factory: Callable[[str], Callable],
                         benchmark_prices: pd.DataFrame | None = None,
                         market_calendar: list[str] | None = None) -> pd.DataFrame:
    rows = []
    for removed in sorted(set(windows)):
        provider = provider_factory(removed)

        def filtered(date: str, account, day) -> list[Signal]:
            generated = provider(date, account, day)
            return [signal for signal in generated
                    if not (date == removed and signal.side == "buy")]

        engine = BacktestEngine(initial_cash, fee_model=fee_model).run(
            bars, [], actions, signal_provider=filtered, market_calendar=market_calendar)
        measures = performance(engine.daily_nav, engine.trades)
        row = {"removed_window": removed,
               "cumulative_return": measures["cumulative_return"],
               "maximum_drawdown": measures["maximum_drawdown"],
               "cagr": measures["cagr"],
               "total_transaction_costs": measures["total_transaction_costs"]}
        if benchmark_prices is not None:
            _, relative = benchmark_comparison(engine.daily_nav, benchmark_prices)
            row["excess_vs_full_csi300"] = relative["excess_vs_full_csi300"]
            row["excess_vs_matched_csi300"] = relative["excess_vs_matched_csi300"]
        rows.append(row)
    columns = ["removed_window", "cumulative_return", "maximum_drawdown", "cagr",
               "total_transaction_costs"]
    if benchmark_prices is not None:
        columns += ["excess_vs_full_csi300", "excess_vs_matched_csi300"]
    return pd.DataFrame(rows, columns=columns)
