"""Stateful monthly requalification policy for existing holdings."""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from .strategy import OFFICIAL_STRATEGY


@dataclass(frozen=True)
class ExitDecision:
    action: str
    reason: str
    nonselected_streak: int
    below_ema_streak: int


def evaluate_monthly_exit(
    row: pd.Series | None,
    market_trend: str,
    in_universe: bool,
    previous_nonselected_streak: int = 0,
    previous_below_ema_streak: int = 0,
) -> ExitDecision:
    """Exit only when loss of qualification is confirmed by a broken trend."""
    if not in_universe:
        return ExitDecision("exit", "left CSI 300 universe", 0, 0)
    if row is None:
        return ExitDecision(
            "review", "review data unavailable",
            previous_nonselected_streak, previous_below_ema_streak,
        )
    hard_checks = (
        ("security_eligible", "security not eligible"),
        ("model_supported", "model no longer supported"),
    )
    for column, reason in hard_checks:
        if not bool(row.get(column, False)):
            return ExitDecision("exit", reason, 0, 0)

    selected = bool(row.get("fundamental_candidate", False))
    nonselected_streak = 0 if selected else previous_nonselected_streak + 1
    above_ema = bool(row.get("above_ema200", False))
    below_ema_streak = 0 if above_ema else previous_below_ema_streak + 1

    if not above_ema and market_trend == "down":
        return ExitDecision("exit", "market and stock below EMA200", 0, 0)
    confirmations = OFFICIAL_STRATEGY.confirmation_reviews
    if not above_ema and (
        below_ema_streak >= confirmations or nonselected_streak >= confirmations
    ):
        return ExitDecision("exit", "selection and EMA200 break confirmed", 0, 0)
    if selected and above_ema:
        return ExitDecision("hold", "still qualified", 0, 0)
    return ExitDecision(
        "hold", "qualification warning; trend break not confirmed",
        nonselected_streak, below_ema_streak,
    )
