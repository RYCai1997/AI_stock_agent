from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from .config import SelectorConfig
from .scoring import MOMENTUM_COLUMNS, factor_profile, score_frame


MARKETS = {"A", "HK", "US"}
DATE_COLUMNS = ["universe_as_of", "fundamental_as_of", "price_as_of"]
IDENTITY_COLUMNS = ["market", "ticker", "company", "industry_l1", "industry_l2"]
RISK_COLUMNS = ["price", "ema200", "volatility_1y", "max_drawdown_6m", "avg_daily_turnover"]


def validate_input(
    frame: pd.DataFrame, market: str, as_of: str, config: SelectorConfig | None = None
) -> pd.DataFrame:
    active_config = config or SelectorConfig()
    profile = factor_profile(active_config.factor_profile)
    factor_columns = profile["quality"] + profile["value"] + MOMENTUM_COLUMNS
    required_columns = IDENTITY_COLUMNS + DATE_COLUMNS + factor_columns + RISK_COLUMNS
    normalized_market = market.upper()
    if normalized_market not in MARKETS:
        raise ValueError(f"unsupported market: {market}; choose A, HK or US")
    missing = sorted(set(required_columns) - set(frame.columns))
    if missing:
        raise ValueError(f"input is missing required columns: {missing}")
    clean = frame.copy()
    clean["market"] = clean["market"].astype(str).str.upper()
    clean = clean[clean["market"] == normalized_market].copy()
    if clean.empty:
        raise ValueError(f"input has no rows for market {normalized_market}")
    if clean["ticker"].duplicated().any():
        duplicates = sorted(clean.loc[clean["ticker"].duplicated(keep=False), "ticker"].unique())
        raise ValueError(f"duplicate tickers for one snapshot: {duplicates}")
    cutoff = pd.Timestamp(as_of)
    for column in DATE_COLUMNS:
        clean[column] = pd.to_datetime(clean[column], errors="raise")
        future = clean[column] > cutoff
        if future.any():
            tickers = clean.loc[future, "ticker"].astype(str).tolist()
            raise ValueError(f"look-ahead blocked: {column} is after {as_of} for {tickers}")
    numeric = factor_columns + RISK_COLUMNS
    if "relative_strength" in clean:
        numeric.append("relative_strength")
    clean[numeric] = clean[numeric].apply(pd.to_numeric, errors="coerce")
    return clean


def add_timing_and_reasons(frame: pd.DataFrame, market_trend: str) -> pd.DataFrame:
    if market_trend not in {"up", "down", "unknown"}:
        raise ValueError("market_trend must be up, down or unknown")
    result = frame.copy()
    result["market_trend"] = market_trend
    result["above_ema200"] = result["price"].notna() & result["ema200"].notna() & result["price"].gt(result["ema200"])
    result["timing_status"] = "unknown"
    result.loc[(market_trend == "up") & result["above_ema200"], "timing_status"] = "confirmed"
    result.loc[(market_trend == "down") | ~result["above_ema200"], "timing_status"] = "cautious"
    result["actionable_candidate"] = result["fundamental_candidate"] & result["timing_status"].eq("confirmed")

    def reason(row: pd.Series) -> str:
        reasons = []
        if not row["security_eligible"]:
            reasons.append("security not eligible")
        elif not row["model_supported"]:
            reasons.append("financial-sector model not implemented")
        elif not row["quality_complete"]:
            reasons.append("incomplete quality data")
        elif not row["quality_pass"]:
            reasons.append("quality below market median")
        elif not row["value_complete"]:
            reasons.append("incomplete value data")
        elif not row["value_pass"]:
            reasons.append("value in industry bottom band")
        elif not row["momentum_complete"]:
            reasons.append("incomplete momentum data")
        elif not row["fundamental_candidate"]:
            reasons.append("momentum outside selected top fraction")
        if row["fundamental_candidate"] and row["timing_status"] != "confirmed":
            reasons.append("trend not confirmed")
        return "; ".join(reasons) if reasons else "selected"

    result["selection_reason"] = result.apply(reason, axis=1)
    return result


def run_selection(
    frame: pd.DataFrame,
    market: str,
    as_of: str,
    output_dir: Path,
    market_trend: str = "unknown",
    config: SelectorConfig | None = None,
) -> tuple[pd.DataFrame, dict]:
    active_config = config or SelectorConfig()
    clean = validate_input(frame, market, as_of, active_config)
    result = add_timing_and_reasons(score_frame(clean, active_config), market_trend)
    result = result.sort_values(
        ["actionable_candidate", "fundamental_candidate", "momentum_score"],
        ascending=[False, False, False],
        na_position="last",
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    export = result.copy()
    for column in DATE_COLUMNS:
        export[column] = export[column].dt.strftime("%Y-%m-%d")
    export.to_csv(output_dir / "candidates.csv", index=False)
    export[export["fundamental_candidate"]].to_csv(output_dir / "selected.csv", index=False)
    export[export["actionable_candidate"]].to_csv(output_dir / "actionable.csv", index=False)
    metadata = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "market": market.upper(),
        "as_of": as_of,
        "market_trend": market_trend,
        "config": active_config.to_dict(),
        "factor_definitions": factor_profile(active_config.factor_profile),
        "counts": {
            "universe": int(len(result)),
            "quality_complete": int(result["quality_complete"].sum()),
            "quality_pass": int(result["quality_pass"].sum()),
            "value_pass": int(result["value_pass"].sum()),
            "fundamental_candidates": int(result["fundamental_candidate"].sum()),
            "actionable_candidates": int(result["actionable_candidate"].sum()),
        },
        "data_date_ranges": {
            column: {
                "min": str(result[column].min().date()),
                "max": str(result[column].max().date()),
            }
            for column in DATE_COLUMNS
        },
        "boundaries": [
            "research only; no broker connection or order execution",
            "deterministic rules only; no LLM dependency in the execution path",
            "financial-sector Q/V model is not implemented and is excluded by default",
            "technical price fields are timing and risk context, not Q/V/M score inputs",
        ],
    }
    (output_dir / "metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return result, metadata
