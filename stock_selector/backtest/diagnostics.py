"""Trade-path MAE/MFE and bounded post-stop counterfactual observations."""

from __future__ import annotations

import pandas as pd

from .corporate_actions import CorporateAction
from .engine import DailyBar


MAE_MFE_COLUMNS = ["ticker", "entry_date", "exit_date", "entry_price", "exit_price",
                   "exit_reason", "mae", "mfe", "post_stop_sessions",
                   "post_stop_max_rebound", "post_stop_further_decline",
                   "no_stop_horizon_return", "stop_saved_or_hurt",
                   "censored", "data_confidence_degraded"]


def mae_mfe(trades: list[dict], bars: list[DailyBar],
            actions: list[CorporateAction] | None = None,
            post_stop_horizon: int = 20) -> pd.DataFrame:
    if post_stop_horizon <= 0:
        raise ValueError("post_stop_horizon must be positive")
    paths: dict[str, list[DailyBar]] = {}
    for bar in sorted(bars, key=lambda item: item.date):
        paths.setdefault(bar.ticker, []).append(bar)
    entries: dict[str, dict] = {}
    rows = []
    for trade in trades:
        ticker = trade["ticker"]
        if trade["side"] == "buy":
            entries[ticker] = trade
            continue
        if trade["side"] != "sell" or ticker not in entries:
            continue
        entry = entries.pop(ticker)
        buy_date = entry["actual_execution_date"]
        sell_date = trade["actual_execution_date"]
        entry_price = float(entry["execution_price"])
        held = [bar for bar in paths.get(ticker, []) if buy_date <= bar.date <= sell_date]
        if not held:
            raise ValueError("missing daily path for closed trade")
        affected = any(action.ticker == ticker and buy_date <= action.date <= sell_date
                       for action in actions or [])
        stopped = trade.get("reason") == "stop loss"
        following = [bar for bar in paths.get(ticker, []) if bar.date > sell_date][:post_stop_horizon]
        affected |= any(action.ticker == ticker and sell_date < action.date <= following[-1].date
                        for action in actions or []) if following else False
        censored = stopped and len(following) < post_stop_horizon
        label = "not_a_stop"
        if stopped:
            if affected:
                label = "manual_audit"
            elif censored:
                label = "unresolved_censored"
            else:
                label = "saved" if following[-1].close < trade["execution_price"] else "hurt"
        rows.append({
            "ticker": ticker, "entry_date": buy_date, "exit_date": sell_date,
            "entry_price": entry_price, "exit_price": trade["execution_price"],
            "exit_reason": trade.get("reason", ""),
            "mae": min(bar.low / entry_price - 1 for bar in held),
            "mfe": max(bar.high / entry_price - 1 for bar in held),
            "post_stop_sessions": len(following) if stopped else 0,
            "post_stop_max_rebound": max(bar.high / trade["execution_price"] - 1 for bar in following)
            if stopped and following else None,
            "post_stop_further_decline": min(bar.low / trade["execution_price"] - 1 for bar in following)
            if stopped and following else None,
            "no_stop_horizon_return": following[-1].close / entry_price - 1
            if stopped and following and not affected else None,
            "stop_saved_or_hurt": label, "censored": censored,
            "data_confidence_degraded": affected,
        })
    return pd.DataFrame(rows, columns=MAE_MFE_COLUMNS)
