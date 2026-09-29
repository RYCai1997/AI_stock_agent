"""Fixed-seed, same-window random-selection diagnostic."""

from __future__ import annotations

import random

import pandas as pd

from selector.scoring import score_frame
from selector.strategy import OFFICIAL_STRATEGY

from .corporate_actions import CorporateAction
from .engine import BacktestEngine, DailyBar, Signal
from .fees import FeeModel
from .official import OfficialSnapshot


def selection_monte_carlo(*, windows: list[str], snapshots: dict[str, OfficialSnapshot],
                          bars: list[DailyBar], actions: list[CorporateAction],
                          fee_model: FeeModel, initial_cash: float,
                          actual_orders: list, iterations: int = 1000,
                          seed: int = 20260928, horizon_sessions: int = 20) -> pd.DataFrame:
    if iterations < 1000 or horizon_sessions <= 0:
        raise ValueError("at least 1000 iterations and positive horizon are required")
    dates = sorted({bar.date for bar in bars})
    bars_by_ticker: dict[str, list[DailyBar]] = {}
    for bar in bars:
        bars_by_ticker.setdefault(bar.ticker, []).append(bar)
    rng = random.Random(seed)
    rows = []
    for window in sorted(set(windows)):
        orders = [order for order in actual_orders if order.side == "buy"
                  and order.signal_date == window and order.status == "filled"]
        chosen = [order.ticker for order in orders]
        base = {"window": window, "selected_count": len(chosen),
                "iterations": iterations, "seed": seed,
                "horizon_sessions": horizon_sessions}
        if not chosen:
            rows.append({**base, "status": "not_run_no_filled_entries"})
            continue
        if window not in snapshots or window not in dates:
            rows.append({**base, "status": "not_run_missing_snapshot_or_session"})
            continue
        snapshot = snapshots[window]
        scored = score_frame(snapshot.metrics, OFFICIAL_STRATEGY.selector_config())
        eligible = sorted(scored.loc[scored["security_eligible"] & scored["model_supported"],
                                     "ticker"].astype(str).tolist())
        if len(eligible) < len(chosen) or not set(chosen) <= set(eligible):
            rows.append({**base, "status": "not_run_selected_outside_eligible_universe"})
            continue
        delay = OFFICIAL_STRATEGY.overheat_delay_sessions if (
            snapshot.provider.get("benchmark", {}).get("return_20d") is not None
            and snapshot.provider["benchmark"]["return_20d"] > OFFICIAL_STRATEGY.overheat_return_threshold
        ) else 0
        start = dates.index(window)
        buy_index = start + 1 + delay
        exit_index = buy_index + horizon_sessions
        if exit_index >= len(dates):
            rows.append({**base, "status": "not_run_insufficient_horizon"})
            continue
        segment = dates[start:exit_index + 1]
        required = set(segment)
        if any(not required <= {bar.date for bar in bars_by_ticker.get(ticker, [])}
               for ticker in eligible):
            rows.append({**base, "status": "not_run_incomplete_eligible_price_paths"})
            continue
        effects = {}
        for ticker in eligible:
            ticker_bars = [bar for bar in bars_by_ticker[ticker] if bar.date in required]
            ticker_actions = [action for action in actions if action.ticker == ticker
                              and action.date in required]
            engine = BacktestEngine(initial_cash, fee_model=fee_model, stop_enabled=False).run(
                ticker_bars,
                [Signal(window, ticker, "buy", OFFICIAL_STRATEGY.fixed_position_fraction,
                        reason="Monte Carlo fixed horizon", delay_sessions=delay),
                 Signal(segment[-2], ticker, "sell", reason="fixed horizon")],
                ticker_actions,
            )
            effects[ticker] = engine.daily_nav[-1]["daily_nav"] - 1
        actual = sum(effects[ticker] for ticker in chosen)
        random_returns = [sum(effects[ticker] for ticker in rng.sample(eligible, len(chosen)))
                          for _ in range(iterations)]
        rows.append({**base, "status": "completed", "eligible_count": len(eligible),
                     "actual_fixed_horizon_return": actual,
                     "random_median_return": float(pd.Series(random_returns).median()),
                     "random_mean_return": sum(random_returns) / iterations,
                     "actual_percentile_at_or_below":
                     100 * sum(value <= actual for value in random_returns) / iterations})
    return pd.DataFrame(rows)


def selection_monte_carlo_horizons(*, horizons: tuple[int, ...] = (20, 63, 126),
                                   **kwargs) -> pd.DataFrame:
    """Apply the same frozen sample and seed to predeclared trading-day horizons."""
    if not horizons or len(set(horizons)) != len(horizons) or any(day <= 0 for day in horizons):
        raise ValueError("horizons must be distinct positive trading-day counts")
    return pd.concat(
        [selection_monte_carlo(horizon_sessions=day, **kwargs) for day in horizons],
        ignore_index=True,
    )
