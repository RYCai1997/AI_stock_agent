"""Auditable interface between dated ETF holdings and LLM news assessments.

This module does not call an LLM and never produces target portfolio weights.
"""

import pandas as pd


HOLDING_COLUMNS = {"etf", "ticker", "company", "weight", "as_of", "source_url"}
ASSESSMENT_COLUMNS = {"ticker", "event_date", "severity", "confidence", "summary", "source_url"}


def validate_holding_snapshot(holdings: pd.DataFrame) -> pd.DataFrame:
    missing = HOLDING_COLUMNS - set(holdings.columns)
    if missing:
        raise ValueError(f"holding snapshot is missing columns: {sorted(missing)}")
    clean = holdings.copy()
    clean["as_of"] = pd.to_datetime(clean["as_of"], errors="raise")
    clean["weight"] = pd.to_numeric(clean["weight"], errors="raise")
    if ((clean["weight"] < 0) | (clean["weight"] > 1)).any():
        raise ValueError("holding weights must be decimals in [0, 1]")
    if clean["source_url"].astype(str).str.strip().eq("").any():
        raise ValueError("every holding row requires a source_url")
    return clean


def aggregate_news_risk(
    holdings: pd.DataFrame,
    assessments: pd.DataFrame,
    severe_event_threshold: float = 0.85,
    weighted_risk_threshold: float = 0.05,
) -> pd.DataFrame:
    """Aggregate supplied assessments into research-only ETF risk flags."""
    clean_holdings = validate_holding_snapshot(holdings)
    missing = ASSESSMENT_COLUMNS - set(assessments.columns)
    if missing:
        raise ValueError(f"news assessments are missing columns: {sorted(missing)}")
    news = assessments.copy()
    news["event_date"] = pd.to_datetime(news["event_date"], errors="raise")
    news["severity"] = pd.to_numeric(news["severity"], errors="raise")
    news["confidence"] = pd.to_numeric(news["confidence"], errors="raise")
    if ((news[["severity", "confidence"]] < 0) | (news[["severity", "confidence"]] > 1)).any().any():
        raise ValueError("severity and confidence must be in [0, 1]")

    # Reduce multiple articles about the same company to its strongest assessed event.
    # This prevents one holding weight from being counted repeatedly merely because
    # several outlets covered the same incident.
    news["event_risk"] = news["severity"] * news["confidence"]
    strongest = news.sort_values(["ticker", "event_risk"]).groupby("ticker", as_index=False).tail(1)
    joined = clean_holdings.merge(strongest, on="ticker", how="left", suffixes=("_holding", "_news"))
    joined["severity"] = joined["severity"].fillna(0.0)
    joined["confidence"] = joined["confidence"].fillna(0.0)
    joined["weighted_risk"] = joined["weight"] * joined["severity"] * joined["confidence"]
    joined["covered_weight"] = joined["weight"].where(joined["event_date"].notna(), 0.0)

    rows = []
    for etf, group in joined.groupby("etf"):
        total_risk = float(group["weighted_risk"].sum())
        severe = bool((group["severity"] >= severe_event_threshold).any())
        rows.append({
            "etf": etf,
            "holdings_as_of": group["as_of"].max(),
            "assessed_weight": float(group["covered_weight"].sum()),
            "weighted_risk": total_risk,
            "severe_event": severe,
            "risk_flag": bool(severe or total_risk >= weighted_risk_threshold),
            "action_scope": "risk_review_only",
        })
    return pd.DataFrame(rows).set_index("etf")
