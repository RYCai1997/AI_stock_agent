"""Compare Top-N portfolio breadth with the same total capital allocation."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd


HORIZONS = (1, 3, 6)
VARIANTS: dict[str, int | None] = {
    "top3": 3,
    "top5": 5,
    "top10": 10,
    "all_candidates": None,
}
OFFICIAL_OUTCOME_RULE = "delay_stop10_monthly_requalification"


def select_ranked(frame: pd.DataFrame, limit: int | None) -> pd.DataFrame:
    ranked = frame.sort_values(
        ["candidate_rank", "momentum_score", "ticker"],
        ascending=[True, False, True],
        na_position="last",
    )
    return ranked if limit is None else ranked.head(limit)


def compare_node(
    ranked_outcomes: pd.DataFrame,
    as_of: str,
    total_exposure: float,
) -> list[dict]:
    rows = []
    for variant, limit in VARIANTS.items():
        selected = select_ranked(ranked_outcomes, limit)
        for months in HORIZONS:
            returns = pd.to_numeric(selected[f"return_{months}m"], errors="coerce").dropna()
            sleeve_return = float(returns.mean()) if len(returns) else None
            rows.append({
                "as_of": as_of,
                "variant": variant,
                "horizon_months": months,
                "available_candidates": len(ranked_outcomes),
                "positions": len(selected),
                "total_exposure": total_exposure if len(selected) else 0.0,
                "weight_per_position": total_exposure / len(selected) if len(selected) else 0.0,
                "sleeve_return": sleeve_return,
                "account_return_contribution": (
                    total_exposure * sleeve_return if sleeve_return is not None else 0.0
                ),
                "winning_positions": int(returns.gt(0).sum()),
                "position_win_rate": float(returns.gt(0).mean()) if len(returns) else None,
            })
    return rows


def run_comparison(
    snapshot_roots: list[Path],
    outcomes_files: list[Path],
    nodes_files: list[Path],
    output: Path,
    total_exposure: float = 0.30,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    if not (len(snapshot_roots) == len(outcomes_files) == len(nodes_files)):
        raise ValueError("snapshot roots, outcome files and node files must have equal counts")
    if not 0 < total_exposure <= 1:
        raise ValueError("total_exposure must be in (0, 1]")

    detail_rows = []
    seen_nodes: set[str] = set()
    for snapshot_root, outcomes_file, nodes_file in zip(
        snapshot_roots, outcomes_files, nodes_files
    ):
        nodes = pd.read_csv(nodes_file)
        outcomes = pd.read_csv(outcomes_file)
        outcomes = outcomes[outcomes["rule"].eq(OFFICIAL_OUTCOME_RULE)].copy()
        for node in nodes.itertuples(index=False):
            as_of = str(node.as_of)
            if as_of in seen_nodes:
                continue
            seen_nodes.add(as_of)
            candidate_path = snapshot_root / as_of / "candidates.csv"
            candidates = pd.read_csv(candidate_path)
            ranked = candidates[candidates["actionable_candidate"].astype(bool)][[
                "ticker", "company", "industry_l1", "candidate_rank", "momentum_score"
            ]]
            node_outcomes = outcomes[outcomes["as_of"].astype(str).eq(as_of)]
            merged = ranked.merge(node_outcomes, on=["ticker", "company"], how="left")
            if len(ranked) and merged["return_6m"].isna().all():
                raise ValueError(f"no official outcomes matched active node {as_of}")
            detail_rows.extend(compare_node(merged, as_of, total_exposure))

    detail = pd.DataFrame(detail_rows)
    summary_rows = []
    for (variant, months), sample in detail.groupby(
        ["variant", "horizon_months"], sort=False
    ):
        active = sample[sample["positions"].gt(0)]
        summary_rows.append({
            "variant": variant,
            "horizon_months": months,
            "nodes": len(sample),
            "active_nodes": len(active),
            "mean_account_return_all_nodes": float(sample["account_return_contribution"].mean()),
            "mean_sleeve_return_active_nodes": float(active["sleeve_return"].mean()),
            "median_sleeve_return_active_nodes": float(active["sleeve_return"].median()),
            "stdev_sleeve_return_active_nodes": float(active["sleeve_return"].std(ddof=1)),
            "profitable_active_node_rate": float(active["sleeve_return"].gt(0).mean()),
            "worst_active_node_sleeve_return": float(active["sleeve_return"].min()),
            "best_active_node_sleeve_return": float(active["sleeve_return"].max()),
            "mean_positions_active_nodes": float(active["positions"].mean()),
        })
    summary = pd.DataFrame(summary_rows)
    output.mkdir(parents=True, exist_ok=True)
    detail.to_csv(output / "portfolio_breadth_detail.csv", index=False)
    summary.to_csv(output / "portfolio_breadth_summary.csv", index=False)
    metadata = {
        "selection_inputs": "point-in-time candidate_rank and momentum_score from each snapshot",
        "outcome_rule": OFFICIAL_OUTCOME_RULE,
        "variants": VARIANTS,
        "total_exposure": total_exposure,
        "cash_fraction": 1.0 - total_exposure,
        "inactive_node_policy": "100% cash, zero return contribution",
        "weighting": "equal weight within each variant; same total exposure for every active node",
        "boundaries": [
            "returns are historical outcome labels and never enter ranking",
            "portfolio path drawdown cannot be inferred from cross-sectional endpoint returns",
            "fees, tax, slippage and limit-lock execution are not modeled",
        ],
    }
    (output / "portfolio_breadth_metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return detail, summary


def main() -> None:
    base = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description="Compare Top-N breadth at fixed exposure")
    parser.add_argument("--total-exposure", type=float, default=0.30)
    parser.add_argument("--output", type=Path, default=base / "outputs" / "portfolio_breadth_demo")
    args = parser.parse_args()
    output_groups = [
        base / "outputs" / "a_multinode_demo_v2",
        base / "outputs" / "a_user_nodes_20210908",
        base / "outputs" / "a_regime_nodes_20210908",
    ]
    outcome_groups = [
        base / "outputs" / "a_requalification_exit_demo" / "requalification_outcomes.csv",
        base / "outputs" / "a_user_nodes_requalification_20210908" / "requalification_outcomes.csv",
        base / "outputs" / "a_regime_requalification_20210909" / "requalification_outcomes.csv",
    ]
    _, summary = run_comparison(
        [group / "snapshots" for group in output_groups],
        outcome_groups,
        [group / "nodes.csv" for group in output_groups],
        args.output,
        args.total_exposure,
    )
    print(summary.to_string(index=False))


if __name__ == "__main__":
    main()
