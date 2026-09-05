from __future__ import annotations

import numpy as np
import pandas as pd

from .config import SelectorConfig


QUALITY_COLUMNS = ["roic", "fcf_margin", "eps_growth_std"]
VALUE_COLUMNS = ["earnings_yield", "fcf_yield", "book_to_price"]
MOMENTUM_COLUMNS = ["mom_6_1", "mom_12_1"]


def percentile(series: pd.Series, higher_is_better: bool = True) -> pd.Series:
    return series.rank(pct=True, method="average", ascending=higher_is_better) * 100.0


def score_frame(frame: pd.DataFrame, config: SelectorConfig) -> pd.DataFrame:
    """Score one market and one as-of date using only supplied point-in-time rows."""
    scored = frame.copy()
    financial = scored["industry_l1"].astype(str).str.lower().str.contains(
        r"financial|finance|金融|银行|保险|券商", regex=True, na=False
    )
    scored["model_supported"] = ~financial if config.exclude_financials else True

    quality_complete = scored[QUALITY_COLUMNS].notna().all(axis=1)
    scored["quality_complete"] = scored["model_supported"] & quality_complete
    qmask = scored["quality_complete"]
    scored.loc[qmask, "roic_rank"] = percentile(scored.loc[qmask, "roic"])
    scored.loc[qmask, "fcf_margin_rank"] = percentile(scored.loc[qmask, "fcf_margin"])
    scored.loc[qmask, "stability_rank"] = percentile(
        scored.loc[qmask, "eps_growth_std"], higher_is_better=False
    )
    scored["quality_score"] = (
        0.40 * scored["roic_rank"]
        + 0.30 * scored["fcf_margin_rank"]
        + 0.30 * scored["stability_rank"]
    )
    cutoff = scored.loc[qmask, "quality_score"].quantile(config.quality_quantile) if qmask.any() else np.nan
    scored["quality_pass"] = qmask & scored["quality_score"].ge(cutoff)

    value_complete = scored[VALUE_COLUMNS + ["industry_l1", "industry_l2"]].notna().all(axis=1)
    scored["value_complete"] = value_complete
    vmask = scored["quality_pass"] & value_complete
    scored["value_group"] = pd.NA
    if vmask.any():
        l2_counts = scored.loc[vmask].groupby("industry_l2")["ticker"].transform("size")
        scored.loc[vmask, "value_group"] = "L2:" + scored.loc[vmask, "industry_l2"].astype(str)
        small = pd.Series(False, index=scored.index)
        small.loc[vmask] = l2_counts.lt(config.minimum_industry_group).to_numpy()
        scored.loc[small, "value_group"] = "L1:" + scored.loc[small, "industry_l1"].astype(str)
        for column in VALUE_COLUMNS:
            scored.loc[vmask, f"{column}_rank"] = (
                scored.loc[vmask].groupby("value_group")[column].rank(pct=True, method="average") * 100.0
            )
    scored["value_score"] = (
        0.40 * scored["earnings_yield_rank"]
        + 0.40 * scored["fcf_yield_rank"]
        + 0.20 * scored["book_to_price_rank"]
    )
    scored["value_pass"] = vmask & scored["value_score"].gt(config.value_min_score)

    momentum_complete = scored[MOMENTUM_COLUMNS].notna().all(axis=1)
    scored["momentum_complete"] = momentum_complete
    mmask = scored["value_pass"] & momentum_complete
    scored.loc[mmask, "mom_6_1_rank"] = percentile(scored.loc[mmask, "mom_6_1"])
    scored.loc[mmask, "mom_12_1_rank"] = percentile(scored.loc[mmask, "mom_12_1"])
    has_relative_strength = (
        config.use_relative_strength
        and "relative_strength" in scored
        and scored.loc[mmask, "relative_strength"].notna().all()
    )
    if has_relative_strength:
        scored.loc[mmask, "relative_strength_rank"] = percentile(scored.loc[mmask, "relative_strength"])
        scored["momentum_score"] = (
            0.30 * scored["mom_6_1_rank"]
            + 0.40 * scored["mom_12_1_rank"]
            + 0.30 * scored["relative_strength_rank"]
        )
        scored["momentum_formula"] = "30% 6-1M + 40% 12-1M + 30% relative strength"
    else:
        scored["momentum_score"] = 0.50 * scored["mom_6_1_rank"] + 0.50 * scored["mom_12_1_rank"]
        scored["momentum_formula"] = "50% 6-1M + 50% 12-1M"
    selected_quantile = 1.0 - config.momentum_top_fraction
    momentum_cutoff = scored.loc[mmask, "momentum_score"].quantile(selected_quantile) if mmask.any() else np.nan
    scored["fundamental_candidate"] = mmask & scored["momentum_score"].ge(momentum_cutoff)
    scored["candidate_rank"] = scored.loc[scored["fundamental_candidate"], "momentum_score"].rank(
        ascending=False, method="min"
    )
    return scored
