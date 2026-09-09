"""Review existing holdings against the frozen monthly exit policy."""

from __future__ import annotations

import pandas as pd

from .exit_policy import evaluate_monthly_exit
from .strategy import OFFICIAL_STRATEGY, OfficialStrategy


REQUIRED_HOLDING_COLUMNS = ["ticker", "entry_date", "entry_price"]
REVIEW_COLUMNS = [
    "review_date", "ticker", "company", "entry_date", "entry_price",
    "stop_loss_price", "current_price", "above_ema200", "currently_selected",
    "action", "reason", "nonselected_streak", "below_ema_streak",
    "approval_status",
]


def review_holdings(
    holdings: pd.DataFrame,
    scored: pd.DataFrame,
    review_date: str,
    market_trend: str,
    strategy: OfficialStrategy = OFFICIAL_STRATEGY,
) -> pd.DataFrame:
    missing = sorted(set(REQUIRED_HOLDING_COLUMNS) - set(holdings.columns))
    if missing:
        raise ValueError(f"holdings input is missing required columns: {missing}")
    if holdings["ticker"].astype(str).duplicated().any():
        raise ValueError("holdings input contains duplicate tickers")
    clean = holdings.copy()
    clean["entry_date"] = pd.to_datetime(clean["entry_date"], errors="raise")
    if clean["entry_date"].gt(pd.Timestamp(review_date)).any():
        raise ValueError("holdings input contains an entry date after the review date")
    clean["entry_price"] = pd.to_numeric(clean["entry_price"], errors="raise")
    if clean["entry_price"].le(0).any():
        raise ValueError("holdings entry_price must be positive")
    current = scored.set_index(scored["ticker"].astype(str), drop=False)
    rows = []
    for _, holding in clean.iterrows():
        ticker = str(holding["ticker"])
        in_universe = ticker in current.index
        stock = current.loc[ticker] if in_universe else None
        nonselected = int(holding.get("nonselected_streak", 0) or 0)
        below_ema = int(holding.get("below_ema_streak", 0) or 0)
        decision = evaluate_monthly_exit(
            stock, market_trend, in_universe, nonselected, below_ema
        )
        entry_price = float(holding["entry_price"])
        rows.append({
            "review_date": review_date,
            "ticker": ticker,
            "company": (
                stock.get("company") if stock is not None
                else holding.get("company", "")
            ),
            "entry_date": str(holding["entry_date"].date()),
            "entry_price": entry_price,
            "stop_loss_price": entry_price * (1.0 - strategy.stop_loss_fraction),
            "current_price": stock.get("price") if stock is not None else None,
            "above_ema200": stock.get("above_ema200") if stock is not None else None,
            "currently_selected": (
                stock.get("fundamental_candidate") if stock is not None else False
            ),
            "action": decision.action,
            "reason": decision.reason,
            "nonselected_streak": decision.nonselected_streak,
            "below_ema_streak": decision.below_ema_streak,
            "approval_status": "REQUIRES_HUMAN_APPROVAL",
        })
    return pd.DataFrame(rows, columns=REVIEW_COLUMNS)
