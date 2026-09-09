"""Create a bounded, human-approved plan from actionable candidates."""

from __future__ import annotations

import pandas as pd

from .strategy import OFFICIAL_STRATEGY, OfficialStrategy


PLAN_COLUMNS = [
    "priority", "ticker", "company", "industry_l1", "industry_l2",
    "quality_score", "value_score", "momentum_score", "price", "ema200",
    "target_fraction", "entry_status", "entry_delay_sessions", "approval_status",
]


def build_portfolio_plan(
    scored: pd.DataFrame,
    strategy: OfficialStrategy = OFFICIAL_STRATEGY,
    overheated: bool = False,
) -> pd.DataFrame:
    """Return at most five fixed-6% ideas; this never places an order."""
    eligible = scored[scored["actionable_candidate"].astype(bool)].copy()
    eligible = eligible.sort_values(
        ["candidate_rank", "momentum_score", "ticker"],
        ascending=[True, False, True],
        na_position="last",
    ).head(strategy.max_new_positions_per_window)
    if eligible.empty:
        return pd.DataFrame(columns=PLAN_COLUMNS)
    eligible["priority"] = range(1, len(eligible) + 1)
    eligible["target_fraction"] = strategy.fixed_position_fraction
    eligible["entry_status"] = (
        f"WAIT_{strategy.overheat_delay_sessions}_SESSIONS"
        if overheated else "READY_AFTER_HUMAN_APPROVAL"
    )
    eligible["entry_delay_sessions"] = (
        strategy.overheat_delay_sessions if overheated else 0
    )
    eligible["approval_status"] = "REQUIRES_HUMAN_APPROVAL"
    plan = eligible[PLAN_COLUMNS].copy()
    if plan["target_fraction"].sum() > strategy.max_new_exposure_per_window + 1e-12:
        raise ValueError("portfolio plan exceeds the frozen per-window exposure cap")
    return plan
