"""Review existing holdings against the frozen monthly exit policy."""

from __future__ import annotations

import pandas as pd
import math

from .exit_policy import ExitDecision, evaluate_monthly_exit
from .strategy import OFFICIAL_STRATEGY, OfficialStrategy


REQUIRED_HOLDING_COLUMNS = ["ticker", "entry_date", "entry_price"]
REVIEW_COLUMNS = [
    "review_date", "ticker", "company", "entry_date", "entry_price",
    "quantity", "position_cost", "stop_loss_price", "current_price",
    "unrealized_return", "above_ema200", "currently_selected", "action",
    "reason", "signal_date", "suggested_execution", "nonselected_streak",
    "below_ema_streak", "approval_status",
    "price_as_of", "price_basis", "state_review_date",
]


def _streak(value: object) -> int:
    number = pd.to_numeric(value, errors="coerce")
    return int(number) if pd.notna(number) else 0


def review_holdings(
    holdings: pd.DataFrame,
    scored: pd.DataFrame,
    review_date: str,
    market_trend: str,
    strategy: OfficialStrategy = OFFICIAL_STRATEGY,
    member_codes: list[str] | None = None,
    quotes: dict | None = None,
    expected_price_date: str | None = None,
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
    if not clean["entry_price"].map(lambda x: math.isfinite(x) and x > 0).all():
        raise ValueError("holdings entry_price must be positive")
    current = scored.set_index(scored["ticker"].astype(str), drop=False)
    rows = []
    for _, holding in clean.iterrows():
        ticker = str(holding["ticker"])
        stock = current.loc[ticker] if ticker in current.index else None
        in_universe = ticker in member_codes if member_codes is not None else True
        nonselected = _streak(holding.get("nonselected_streak", 0))
        below_ema = _streak(holding.get("below_ema_streak", 0))
        last_review = pd.to_datetime(holding.get("last_review_date"), errors="coerce")
        current_review = pd.Timestamp(review_date)
        if pd.notna(last_review) and last_review > current_review:
            raise ValueError("holding state is from after review date; use a historical snapshot")
        same_review_month = (
            pd.notna(last_review)
            and last_review.to_period("M") == current_review.to_period("M")
        )
        # First successful observation per calendar month; missing months break continuity.
        if pd.notna(last_review) and current_review.to_period("M").ordinal - last_review.to_period("M").ordinal > 1:
            nonselected = below_ema = 0
        if same_review_month:
            saved_action = holding.get("last_action")
            saved_reason = holding.get("last_reason")
            decision = ExitDecision(str(saved_action) if pd.notna(saved_action) and saved_action else "hold",
                                    str(saved_reason) if pd.notna(saved_reason) and saved_reason else "monthly review already recorded",
                                    nonselected, below_ema)
        else:
            decision = evaluate_monthly_exit(stock, market_trend, in_universe, nonselected, below_ema)
        entry_price = float(holding["entry_price"])
        quantity = pd.to_numeric(holding.get("quantity"), errors="coerce")
        quantity = float(quantity) if pd.notna(quantity) else None
        quote = (quotes or {}).get(ticker, {})
        current_price = quote.get("price")
        price_date = quote.get("price_as_of", "")
        valid_quote = (quote.get("price_basis") == "unadjusted"
                       and current_price is not None and math.isfinite(float(current_price))
                       and float(current_price) > 0 and bool(price_date)
                       and str(holding["entry_date"].date()) <= price_date <= review_date
                       and (expected_price_date is None or price_date == expected_price_date))
        state_date = str(last_review.date()) if pd.notna(last_review) else ""
        stop_loss_price = entry_price * (1.0 - strategy.stop_loss_fraction)
        stop_triggered = valid_quote and not quote.get("basis_changed", True) and float(current_price) <= stop_loss_price
        action = "exit" if stop_triggered else decision.action
        reason = "latest close at or below stop loss threshold" if stop_triggered else decision.reason
        if not valid_quote or quote.get("basis_changed", True) or (stock is None and in_universe):
            action, reason = "review", "price or selection data unavailable"
            if valid_quote and quote.get("basis_changed", True):
                reason = "corporate action: reconcile cost and quantity"
            decision = ExitDecision(action, reason, _streak(holding.get("nonselected_streak")),
                                    _streak(holding.get("below_ema_streak")))
        elif not same_review_month:
            state_date = review_date
        if member_codes is not None and not in_universe:
            action, reason = "exit", "left CSI 300 universe"
            state_date = review_date
        if stop_triggered:
            action, reason = "exit", "latest close at or below stop loss threshold"
        # An unexecuted exit remains pending until the user records the actual sale.
        if holding.get("last_action") == "exit":
            action, reason = "exit", str(holding.get("last_reason") or "pending exit")
        saved_signal = holding.get("signal_date")
        signal_date = (str(saved_signal if pd.notna(saved_signal) and saved_signal else state_date or review_date)
                       if holding.get("last_action") == "exit" else review_date)
        rows.append({
            "review_date": review_date,
            "ticker": ticker,
            "company": (
                stock.get("company") if stock is not None
                else holding.get("company", "")
            ),
            "entry_date": str(holding["entry_date"].date()),
            "entry_price": entry_price,
            "quantity": quantity,
            "position_cost": entry_price * quantity if quantity is not None else None,
            "stop_loss_price": stop_loss_price,
            "current_price": current_price,
            "unrealized_return": (
                float(current_price) / entry_price - 1.0 if valid_quote and not quote.get("basis_changed", True) else None
            ),
            "above_ema200": stock.get("above_ema200") if stock is not None else None,
            "currently_selected": (
                stock.get("fundamental_candidate") if stock is not None else False
            ),
            "action": action,
            "reason": reason,
            "signal_date": signal_date if action == "exit" else "",
            "suggested_execution": ("NEXT_TRADABLE_OPEN" if valid_quote and quote.get("tradestatus") == "1"
                                    else "WAIT_FOR_TRADABILITY") if action == "exit" else "",
            "nonselected_streak": decision.nonselected_streak,
            "below_ema_streak": decision.below_ema_streak,
            "approval_status": "REQUIRES_HUMAN_APPROVAL",
            "price_as_of": price_date, "price_basis": quote.get("price_basis", "unknown"),
            "state_review_date": state_date,
        })
    return pd.DataFrame(rows, columns=REVIEW_COLUMNS)
