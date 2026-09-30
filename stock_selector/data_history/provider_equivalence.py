"""Offline comparison of Tushare candidate snapshots with nine frozen Baostock nodes."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from selector.pipeline import add_timing_and_reasons, validate_input
from selector.portfolio_plan import build_portfolio_plan
from selector.scoring import score_frame
from selector.strategy import OFFICIAL_STRATEGY


PRICE_FIELDS = ("price", "ema200", "return_20d", "mom_6_1", "mom_12_1")
FACTOR_FIELDS = ("roe", "cfo_to_revenue", "eps_growth_std", "earnings_yield",
                 "net_cashflow_yield", "book_to_price")
SCORE_FIELDS = ("quality_score", "value_score", "momentum_score")
PASS_FIELDS = ("quality_pass", "value_pass", "fundamental_candidate", "actionable_candidate")
REFERENCE_DATES = ("2020-03-16", "2020-07-15", "2021-05-17", "2021-12-15",
                   "2022-05-16", "2022-10-17", "2023-03-15", "2024-05-15", "2025-07-15")


def _read_snapshot(root: Path, date: str) -> tuple[pd.DataFrame, dict]:
    node = root / date
    metrics = pd.read_csv(node / "raw_metrics.csv", dtype={"ticker": str})
    provider = json.loads((node / "official_run_metadata.json").read_text(encoding="utf-8"))["provider"]
    if provider.get("as_of") != date or provider.get("built_rows") != len(metrics):
        raise ValueError(f"inconsistent provider date or row count for {date}")
    if metrics.ticker.duplicated().any():
        raise ValueError(f"duplicate ticker for {date}")
    return metrics, provider


def _scored(metrics: pd.DataFrame, provider: dict, date: str) -> pd.DataFrame:
    clean = validate_input(metrics, OFFICIAL_STRATEGY.market, date,
                           OFFICIAL_STRATEGY.selector_config())
    return add_timing_and_reasons(score_frame(clean, OFFICIAL_STRATEGY.selector_config()),
                                  provider["market_trend"]).set_index("ticker")


def compare_node(date: str, reference: tuple[pd.DataFrame, dict],
                 candidate: tuple[pd.DataFrame, dict]) -> tuple[dict, list[dict]]:
    old, old_meta = reference
    new, new_meta = candidate
    old_codes, new_codes = set(old.ticker.astype(str)), set(new.ticker.astype(str))
    common = sorted(old_codes & new_codes)
    summary = {"signal_date": date, "status": "numeric_comparison_only",
               "reference_provider": "baostock", "candidate_provider": "tushare_pro",
               "reference_member_count": len(old_codes), "candidate_member_count": len(new_codes),
               "member_overlap_count": len(common), "missing_members": ";".join(sorted(old_codes - new_codes)),
               "extra_members": ";".join(sorted(new_codes - old_codes)),
               "candidate_membership_date": new_meta.get("membership_snapshot"),
               "candidate_membership_not_future": bool(new_meta.get("membership_snapshot")
                                                        and new_meta["membership_snapshot"] <= date)}
    if not summary["candidate_membership_not_future"]:
        raise ValueError(f"Tushare membership as-of missing or future for {date}")
    if "membership_update_date" in new:
        updates = pd.to_datetime(new["membership_update_date"], errors="coerce")
        if updates.isna().any() or updates.gt(pd.Timestamp(date)).any():
            raise ValueError(f"Tushare constituent update missing or future for {date}")
    old_scored = _scored(old, old_meta, date)
    new_scored = _scored(new, new_meta, date)
    details = []
    for field in (*PRICE_FIELDS, *FACTOR_FIELDS, *SCORE_FIELDS):
        a = pd.to_numeric(old_scored.reindex(common)[field], errors="coerce")
        b = pd.to_numeric(new_scored.reindex(common)[field], errors="coerce")
        paired = pd.DataFrame({"reference": a, "candidate": b}).dropna()
        summary[f"{field}_paired_count"] = len(paired)
        summary[f"{field}_max_abs_diff"] = float((paired.candidate - paired.reference).abs().max()) if len(paired) else None
        # Pearson correlation of average ranks is Spearman's rho and keeps the
        # audit runnable with the repository's numpy+pandas-only CI dependencies.
        summary[f"{field}_spearman"] = (float(paired.reference.rank().corr(paired.candidate.rank()))
                                         if len(paired) >= 3 and paired.reference.nunique() > 1
                                         and paired.candidate.nunique() > 1 else None)
        for ticker, values in paired.iterrows():
            details.append({"signal_date": date, "ticker": ticker, "field": field,
                            "reference": values.reference, "candidate": values.candidate,
                            "difference": values.candidate - values.reference})
    for field in PASS_FIELDS:
        a = old_scored.reindex(common)[field].fillna(False).astype(bool)
        b = new_scored.reindex(common)[field].fillna(False).astype(bool)
        summary[f"{field}_agreement"] = float(a.eq(b).mean()) if common else None
        summary[f"{field}_overlap_count"] = len(set(a.index[a]) & set(b.index[b]))
    old_top = set(build_portfolio_plan(old_scored.reset_index()).ticker.astype(str))
    new_top = set(build_portfolio_plan(new_scored.reset_index()).ticker.astype(str))
    summary["top5_overlap_count"] = len(old_top & new_top)
    summary["reference_top5"] = ";".join(sorted(old_top))
    summary["candidate_top5"] = ";".join(sorted(new_top))
    summary["semantic_equivalence_verified"] = False
    return summary, details


def audit_reference_nodes(reference_root: Path, candidate_root: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    rows, details = [], []
    for date in REFERENCE_DATES:
        reference = _read_snapshot(reference_root, date)
        if not (candidate_root / date / "raw_metrics.csv").is_file():
            rows.append({"signal_date": date, "status": "blocked_by_missing_data_credentials",
                         "reference_member_count": len(reference[0]), "candidate_member_count": None,
                         "member_overlap_count": None, "semantic_equivalence_verified": False})
            continue
        row, node_details = compare_node(date, reference, _read_snapshot(candidate_root, date))
        rows.append(row)
        details.extend(node_details)
    return pd.DataFrame(rows), pd.DataFrame(details,
                                            columns=["signal_date", "ticker", "field", "reference",
                                                     "candidate", "difference"])


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare nine frozen Baostock nodes with Tushare candidates")
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    summary, detail = audit_reference_nodes(args.reference, args.candidate)
    args.output.mkdir(parents=True, exist_ok=True)
    summary.to_csv(args.output / "provider_equivalence_summary.csv", index=False)
    detail.to_csv(args.output / "provider_equivalence_by_ticker.csv", index=False)
    print(json.dumps({"reference_nodes": len(summary),
                      "compared_nodes": int(summary.status.eq("numeric_comparison_only").sum()),
                      "blocked_nodes": int(summary.status.eq("blocked_by_missing_data_credentials").sum()),
                      "output": str(args.output.resolve())}, ensure_ascii=False))


if __name__ == "__main__":
    main()
