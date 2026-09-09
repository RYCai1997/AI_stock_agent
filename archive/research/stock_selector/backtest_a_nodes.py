"""Retrospective multi-node evaluation for the point-in-time A-share selector.

Selection snapshots and forward outcomes are deliberately stored separately so
future prices can never become scoring inputs.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from selector.config import SelectorConfig
from selector.pipeline import run_selection
from selector.providers import build_a_metrics
from selector.providers.a_baostock import _rows
from selector.scoring import percentile


HORIZONS = (1, 3, 6)
FORWARD_FIELDS = "date,code,open,high,low,close,tradestatus"


def select_variants(scored: pd.DataFrame, market_trend: str) -> dict[str, pd.DataFrame]:
    """Return strategy members without reading any forward outcome columns."""
    actionable = scored[scored["actionable_candidate"]].copy()
    positive_cash = actionable[actionable["net_cashflow_yield"].gt(0)].copy()

    eligible = (
        scored["security_eligible"].fillna(False).astype(bool)
        & scored[["mom_6_1", "mom_12_1"]].notna().all(axis=1)
    )
    momentum = scored.loc[eligible].copy()
    momentum["comparison_momentum_score"] = (
        0.5 * percentile(momentum["mom_6_1"])
        + 0.5 * percentile(momentum["mom_12_1"])
    )
    if market_trend != "up":
        momentum = momentum.iloc[0:0].copy()
    else:
        momentum = momentum[momentum["above_ema200"]]
        momentum = momentum.nlargest(len(actionable), "comparison_momentum_score")

    return {
        "qvm_current": actionable,
        "qvm_positive_net_cashflow": positive_cash,
        "momentum_only_matched_n": momentum,
    }


def calculate_forward_outcomes(prices: pd.DataFrame, as_of: str) -> dict[str, Any]:
    """Use next tradable open as entry and later closes as outcome labels."""
    frame = prices.copy()
    frame["date"] = pd.to_datetime(frame["date"])
    for column in ["open", "high", "low", "close"]:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame[frame["tradestatus"].astype(str).eq("1")]
    frame = frame[frame["date"].gt(pd.Timestamp(as_of))].sort_values("date")
    frame = frame.dropna(subset=["open", "close"])
    if frame.empty:
        return {"outcome_error": "no tradable session after signal date"}

    entry = frame.iloc[0]
    result: dict[str, Any] = {
        "entry_date": str(entry["date"].date()),
        "entry_price": float(entry["open"]),
    }
    for months in HORIZONS:
        target = pd.Timestamp(as_of) + pd.DateOffset(months=months)
        eligible = frame[frame["date"].ge(target)]
        key = f"{months}m"
        if eligible.empty:
            result[f"exit_date_{key}"] = None
            result[f"return_{key}"] = None
        else:
            exit_row = eligible.iloc[0]
            result[f"exit_date_{key}"] = str(exit_row["date"].date())
            result[f"return_{key}"] = float(exit_row["close"] / entry["open"] - 1.0)

    six_month_target = pd.Timestamp(as_of) + pd.DateOffset(months=6)
    path = frame[frame["date"].le(six_month_target)]["close"]
    values = pd.concat([pd.Series([float(entry["open"])]), path.reset_index(drop=True)], ignore_index=True)
    result["max_drawdown_6m_forward"] = float((values / values.cummax() - 1.0).min())
    return result


def fetch_outcome(bs: Any, code: str, as_of: str) -> dict[str, Any]:
    start = str((pd.Timestamp(as_of) + pd.Timedelta(days=1)).date())
    end = str((pd.Timestamp(as_of) + pd.DateOffset(months=6, days=14)).date())
    rows = _rows(bs.query_history_k_data_plus(
        code, FORWARD_FIELDS, start_date=start, end_date=end, frequency="d", adjustflag="2"
    ))
    if not rows:
        return {"outcome_error": "no forward price rows"}
    return calculate_forward_outcomes(pd.DataFrame(rows), as_of)


def load_or_fetch_outcome(
    bs: Any, code: str, as_of: str, cache_dir: Path
) -> dict[str, Any]:
    cache_path = cache_dir / f"{as_of}_{code}.json"
    if cache_path.exists():
        return json.loads(cache_path.read_text(encoding="utf-8"))
    outcome = fetch_outcome(bs, code, as_of)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = cache_path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(outcome, ensure_ascii=False), encoding="utf-8")
    temporary.replace(cache_path)
    return outcome


def summarize_portfolios(holdings: pd.DataFrame, nodes: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for _, node in nodes.iterrows():
        as_of = node["as_of"]
        benchmark = holdings[(holdings["as_of"] == as_of) & (holdings["strategy"] == "benchmark")]
        for strategy in ["qvm_current", "qvm_positive_net_cashflow", "momentum_only_matched_n"]:
            sample = holdings[(holdings["as_of"] == as_of) & (holdings["strategy"] == strategy)]
            selected_n = int(node[f"n_{strategy}"])
            for months in HORIZONS:
                column = f"return_{months}m"
                returns = pd.to_numeric(sample[column], errors="coerce").dropna()
                benchmark_return = pd.to_numeric(benchmark[column], errors="coerce").dropna()
                data_usable = node["data_status"] == "usable"
                mean_return = float(returns.mean()) if not returns.empty else (
                    0.0 if selected_n == 0 and data_usable else np.nan
                )
                bench = float(benchmark_return.iloc[0]) if not benchmark_return.empty else np.nan
                rows.append({
                    "as_of": as_of,
                    "market_trend": node["market_trend"],
                    "strategy": strategy,
                    "selected_n": selected_n,
                    "evaluated_n": int(len(returns)),
                    "horizon_months": months,
                    "mean_return": mean_return,
                    "median_return": float(returns.median()) if not returns.empty else (
                        0.0 if selected_n == 0 and data_usable else np.nan
                    ),
                    "win_rate": float(returns.gt(0).mean()) if not returns.empty else np.nan,
                    "benchmark_return": bench,
                    "excess_return": mean_return - bench if pd.notna(mean_return) and pd.notna(bench) else np.nan,
                    "mean_stock_max_drawdown_6m": float(pd.to_numeric(
                        sample["max_drawdown_6m_forward"], errors="coerce"
                    ).mean()) if not sample.empty else (0.0 if selected_n == 0 else np.nan),
                })
    return pd.DataFrame(rows)


def run_nodes(nodes: list[str], output_dir: Path, snapshot_cache: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    snapshot_root = output_dir / "snapshots"
    outcome_cache = output_dir / "outcome_cache"
    node_rows = []
    members_by_node: dict[str, dict[str, pd.DataFrame]] = {}

    for as_of in nodes:
        node_dir = snapshot_root / as_of
        raw_path = node_dir / "raw_metrics.csv"
        provider_path = node_dir / "provider_metadata.json"
        if raw_path.exists() and provider_path.exists():
            metrics = pd.read_csv(raw_path)
            provider = json.loads(provider_path.read_text(encoding="utf-8"))
        else:
            metrics, provider = build_a_metrics(as_of, cache_dir=snapshot_cache)
            node_dir.mkdir(parents=True, exist_ok=True)
            metrics.to_csv(raw_path, index=False)
            provider_path.write_text(json.dumps(provider, ensure_ascii=False, indent=2), encoding="utf-8")
        scored, selector_metadata = run_selection(
            metrics, "A", as_of, node_dir, provider["market_trend"],
            SelectorConfig(factor_profile="a_share_v1"),
        )
        variants = select_variants(scored, provider["market_trend"])
        members_by_node[as_of] = variants
        quality_complete = selector_metadata["counts"]["quality_complete"]
        data_status = "usable" if quality_complete > 0 else "insufficient_quality_history"
        node_rows.append({
            "as_of": as_of,
            "market_trend": provider["market_trend"],
            "data_status": data_status,
            "universe_n": len(scored),
            "quality_complete_n": quality_complete,
            "fundamental_candidate_n": selector_metadata["counts"]["fundamental_candidates"],
            "n_qvm_current": len(variants["qvm_current"]),
            "n_qvm_positive_net_cashflow": len(variants["qvm_positive_net_cashflow"]),
            "n_momentum_only_matched_n": len(variants["momentum_only_matched_n"]),
        })
        print(f"snapshot ready {as_of}: {node_rows[-1]}", flush=True)

    try:
        import baostock as bs
    except ImportError as exc:
        raise RuntimeError("baostock is required") from exc
    login = bs.login()
    if login.error_code != "0":
        raise RuntimeError(f"Baostock login failed: {login.error_msg}")
    holding_rows = []
    try:
        for as_of, variants in members_by_node.items():
            codes = {"sh.000300"}
            for sample in variants.values():
                codes.update(sample["ticker"].astype(str))
            outcomes = {}
            for position, code in enumerate(sorted(codes), 1):
                outcomes[code] = load_or_fetch_outcome(bs, code, as_of, outcome_cache)
                if position % 10 == 0 or position == len(codes):
                    print(f"outcomes {as_of}: {position}/{len(codes)}", flush=True)
            holding_rows.append({
                "as_of": as_of, "strategy": "benchmark", "ticker": "sh.000300",
                "company": "沪深300", **outcomes["sh.000300"],
            })
            for strategy, sample in variants.items():
                for _, row in sample.iterrows():
                    holding_rows.append({
                        "as_of": as_of, "strategy": strategy, "ticker": row["ticker"],
                        "company": row["company"], "industry_l1": row["industry_l1"],
                        "industry_l2": row["industry_l2"], **outcomes[row["ticker"]],
                    })
    finally:
        bs.logout()

    nodes_frame = pd.DataFrame(node_rows)
    holdings = pd.DataFrame(holding_rows)
    summary = summarize_portfolios(holdings, nodes_frame)
    nodes_frame.to_csv(output_dir / "nodes.csv", index=False)
    holdings.to_csv(output_dir / "forward_outcomes.csv", index=False)
    summary.to_csv(output_dir / "portfolio_summary.csv", index=False)
    metadata = {
        "nodes": nodes,
        "entry_rule": "next tradable session open after signal date",
        "exit_rule": "first tradable close on or after each calendar-month horizon",
        "portfolio_rule": "equal-weight arithmetic mean; no fees, tax or slippage",
        "market_timing": "hold cash when CSI 300 is not above EMA200",
        "download_mode": "Baostock snapshot downloads must run serially; concurrent logins interfere",
        "outcome_separation": "forward prices are saved separately and never passed to scoring",
    }
    (output_dir / "metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest several point-in-time CSI 300 snapshots")
    parser.add_argument("--nodes", required=True, help="Comma-separated YYYY-MM-DD dates")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--snapshot-cache", required=True, type=Path)
    args = parser.parse_args()
    nodes = [value.strip() for value in args.nodes.split(",") if value.strip()]
    if not nodes:
        raise ValueError("at least one node is required")
    run_nodes(nodes, args.output, args.snapshot_cache)


if __name__ == "__main__":
    main()
