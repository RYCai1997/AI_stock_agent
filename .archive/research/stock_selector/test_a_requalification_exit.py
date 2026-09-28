"""Backtest monthly requalification exits with frozen entry-date fundamentals."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from selector.config import SelectorConfig
from selector.exit_policy import evaluate_monthly_exit
from selector.pipeline import run_selection
from selector.providers.a_baostock import K_FIELDS, _price_metrics_from_frame, _rows
from test_a_risk_rules import HORIZONS, market_is_overheated, prepare_prices, simulate_rule


def fetch_long_path(bs: Any, code: str, as_of: str) -> pd.DataFrame:
    start = str((pd.Timestamp(as_of) - pd.Timedelta(days=550)).date())
    end = str((pd.Timestamp(as_of) + pd.DateOffset(months=6, days=14)).date())
    rows = _rows(bs.query_history_k_data_plus(
        code, K_FIELDS, start_date=start, end_date=end, frequency="d", adjustflag="2"
    ))
    if not rows:
        raise ValueError("no long price path")
    return pd.DataFrame(rows)


def load_or_fetch_long_path(bs: Any, code: str, as_of: str, cache_dir: Path) -> pd.DataFrame:
    path = cache_dir / f"{as_of}_{code}.csv"
    if path.exists():
        return pd.read_csv(path)
    frame = fetch_long_path(bs, code, as_of)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".csv.tmp")
    frame.to_csv(temporary, index=False)
    temporary.replace(path)
    return frame


def membership_at(bs: Any, review_date: str, cache_dir: Path) -> set[str]:
    path = cache_dir / f"{review_date}.json"
    if path.exists():
        return set(json.loads(path.read_text(encoding="utf-8"))["codes"])
    rows = _rows(bs.query_hs300_stocks(review_date))
    codes = sorted(row["code"] for row in rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"review_date": review_date, "codes": codes}), encoding="utf-8")
    return set(codes)


def build_frozen_fundamental_review(
    entry_metrics: pd.DataFrame,
    paths: dict[str, pd.DataFrame],
    review_date: str,
) -> pd.DataFrame:
    """Update price/valuation fields while preserving entry-date fundamentals."""
    rows = []
    for _, original in entry_metrics.iterrows():
        updated = original.to_dict()
        price = _price_metrics_from_frame(paths[str(original["ticker"])], review_date)
        updated.update(price)
        updated["security_eligible"] = price["tradestatus"] == "1" and price["is_st"] != "1"
        rows.append(updated)
    frame = pd.DataFrame(rows)
    groups = frame["industry_l2"].where(
        frame.groupby("industry_l2")["ticker"].transform("size") >= 5, frame["industry_l1"]
    )
    frame["relative_strength"] = (
        frame["mom_12_1"] - frame.groupby(groups)["mom_12_1"].transform("median")
    )
    return frame


def simulate_policy_exit(
    prices: pd.DataFrame,
    as_of: str,
    overheated: bool,
    review_decisions: list[dict[str, Any]],
    stop_loss: float = 0.10,
    delay_sessions: int = 5,
) -> dict[str, Any]:
    prepared = prepare_prices(prices)
    future = prepared[prepared["date"].gt(pd.Timestamp(as_of))].reset_index(drop=True)
    entry_position = delay_sessions if overheated else 0
    if len(future) <= entry_position:
        return {"outcome_error": "insufficient future sessions for entry"}
    entry = future.iloc[entry_position]
    entry_price = float(entry["open"])

    policy_exit = None
    for decision in review_decisions:
        if decision["action"] != "exit":
            continue
        eligible = future[future["date"].gt(pd.Timestamp(decision["review_date"]))]
        if not eligible.empty:
            row = eligible.iloc[0]
            policy_exit = {
                "date": row["date"], "fill": float(row["open"]),
                "reason": decision["reason"], "review_date": decision["review_date"],
            }
        break

    stop_price = entry_price * (1.0 - stop_loss)
    stop_search = future[future.index >= entry_position]
    if policy_exit is not None:
        stop_search = stop_search[stop_search["date"].lt(policy_exit["date"])]
    triggered = stop_search[stop_search["low"].le(stop_price)]
    stop_exit = None
    if not triggered.empty:
        row = triggered.iloc[0]
        stop_exit = {
            "date": row["date"], "fill": min(float(row["open"]), stop_price),
            "reason": "fixed -10% stop", "review_date": None,
        }

    actual_exit = stop_exit or policy_exit
    values = pd.Series(1.0, index=future.index, dtype=float)
    active = future.index >= entry_position
    values.loc[active] = future.loc[active, "close"] / entry_price
    if actual_exit is not None:
        exit_index = future.index[future["date"].eq(actual_exit["date"])][0]
        values.loc[values.index >= exit_index] = actual_exit["fill"] / entry_price

    result: dict[str, Any] = {
        "entry_date": str(entry["date"].date()),
        "entry_price": entry_price,
        "exit_date": str(actual_exit["date"].date()) if actual_exit else None,
        "exit_price": actual_exit["fill"] if actual_exit else None,
        "exit_reason": actual_exit["reason"] if actual_exit else "held to horizon",
        "exit_type": (
            "stop" if stop_exit is not None else
            "requalification" if policy_exit is not None else "horizon"
        ),
        "trigger_review_date": actual_exit["review_date"] if actual_exit else None,
    }
    for months in HORIZONS:
        target = pd.Timestamp(as_of) + pd.DateOffset(months=months)
        eligible = future[future["date"].ge(target)]
        result[f"return_{months}m"] = (
            float(values.loc[eligible.index[0]] - 1.0) if not eligible.empty else None
        )
    six_month_target = pd.Timestamp(as_of) + pd.DateOffset(months=6)
    path_values = values.loc[future["date"].le(six_month_target)]
    with_initial = pd.concat([pd.Series([1.0]), path_values.reset_index(drop=True)], ignore_index=True)
    result["max_drawdown_6m"] = float((with_initial / with_initial.cummax() - 1.0).min())
    result["worst_return_from_entry_6m"] = float(with_initial.min() - 1.0)
    return result


def summarize(outcomes: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for (as_of, rule), sample in outcomes.groupby(["as_of", "rule"], sort=True):
        for months in HORIZONS:
            returns = pd.to_numeric(sample[f"return_{months}m"], errors="coerce")
            rows.append({
                "as_of": as_of, "rule": rule, "selected_n": len(sample),
                "horizon_months": months, "mean_return": float(returns.mean()),
                "median_return": float(returns.median()),
                "win_rate": float(returns.gt(0).mean()),
                "exit_rate": float(sample["exit_date"].notna().mean()),
                "stop_rate": float(sample["exit_type"].eq("stop").mean()),
                "requalification_exit_rate": float(
                    sample["exit_type"].eq("requalification").mean()
                ),
                "mean_stock_max_drawdown_6m": float(sample["max_drawdown_6m"].mean()),
                "mean_worst_return_from_entry_6m": float(
                    sample["worst_return_from_entry_6m"].mean()
                ),
            })
    return pd.DataFrame(rows)


def run_experiment(snapshot_root: Path, nodes_file: Path, output_dir: Path) -> None:
    nodes = pd.read_csv(nodes_file)
    active_nodes = nodes[nodes["n_qvm_current"].gt(0)]["as_of"].astype(str).tolist()
    output_dir.mkdir(parents=True, exist_ok=True)
    path_cache = output_dir / "long_path_cache"
    membership_cache = output_dir / "membership_cache"
    review_root = output_dir / "reviews"

    try:
        import baostock as bs
    except ImportError as exc:
        raise RuntimeError("baostock is required") from exc
    login = bs.login()
    if login.error_code != "0":
        raise RuntimeError(f"Baostock login failed: {login.error_msg}")

    outcome_rows = []
    decision_rows = []
    review_rows = []
    try:
        for as_of in active_nodes:
            node_dir = snapshot_root / as_of
            entry_metrics = pd.read_csv(node_dir / "raw_metrics.csv")
            entry_candidates = pd.read_csv(node_dir / "candidates.csv")
            selected = entry_candidates[entry_candidates["actionable_candidate"].astype(bool)]
            codes = entry_metrics["ticker"].astype(str).tolist()
            paths = {}
            for position, code in enumerate(codes, 1):
                paths[code] = load_or_fetch_long_path(bs, code, as_of, path_cache)
                if position % 25 == 0 or position == len(codes):
                    print(f"long paths {as_of}: {position}/{len(codes)}", flush=True)
            benchmark_path = load_or_fetch_long_path(bs, "sh.000300", as_of, path_cache)
            overheated, market_return_20d = market_is_overheated(benchmark_path, as_of)

            review_maps = []
            for month in range(1, 6):
                review_date = str((pd.Timestamp(as_of) + pd.DateOffset(months=month)).date())
                benchmark_metrics = _price_metrics_from_frame(benchmark_path, review_date)
                market_trend = (
                    "up" if benchmark_metrics["ema200"] is not None
                    and benchmark_metrics["price"] > benchmark_metrics["ema200"] else "down"
                )
                current_members = membership_at(bs, review_date, membership_cache)
                review_metrics = build_frozen_fundamental_review(entry_metrics, paths, review_date)
                review_dir = review_root / as_of / review_date
                scored, metadata = run_selection(
                    review_metrics, "A", review_date, review_dir, market_trend,
                    SelectorConfig(factor_profile="a_share_v1"),
                )
                review_maps.append((review_date, market_trend, current_members, scored.set_index("ticker")))
                review_rows.append({
                    "entry_as_of": as_of, "review_date": review_date,
                    "market_trend": market_trend, "current_member_n": len(current_members),
                    **metadata["counts"],
                })

            for _, stock in selected.iterrows():
                ticker = str(stock["ticker"])
                streak = 0
                below_ema_streak = 0
                decisions = []
                for review_date, market_trend, members, review in review_maps:
                    row = review.loc[ticker] if ticker in review.index else None
                    decision = evaluate_monthly_exit(
                        row, market_trend, ticker in members, streak, below_ema_streak
                    )
                    streak = decision.nonselected_streak
                    below_ema_streak = decision.below_ema_streak
                    event = {
                        "entry_as_of": as_of, "ticker": ticker, "company": stock["company"],
                        "review_date": review_date, "action": decision.action,
                        "reason": decision.reason, "nonselected_streak": streak,
                        "below_ema_streak": below_ema_streak,
                        "market_trend": market_trend,
                        "momentum_percentile": row.get("momentum_percentile") if row is not None else None,
                    }
                    decisions.append(event)
                    decision_rows.append(event)
                    if decision.action == "exit":
                        break

                baseline = simulate_rule(
                    paths[ticker], as_of, "overheat_delay5_stop10", overheated
                )
                components = json.loads(baseline["components"])
                stop_dates = [item["stop_date"] for item in components if item["stop_date"]]
                baseline["exit_date"] = min(stop_dates) if stop_dates else None
                baseline["exit_price"] = next(
                    (item["stop_fill"] for item in components if item["stop_date"]), None
                )
                baseline["exit_reason"] = "fixed -10% stop" if stop_dates else "held to horizon"
                baseline["exit_type"] = "stop" if stop_dates else "horizon"
                baseline["trigger_review_date"] = None
                outcome_rows.append({
                    "as_of": as_of, "ticker": ticker, "company": stock["company"],
                    "rule": "delay_if_overheated_plus_stop10", **baseline,
                })
                policy = simulate_policy_exit(paths[ticker], as_of, overheated, decisions)
                outcome_rows.append({
                    "as_of": as_of, "ticker": ticker, "company": stock["company"],
                    "rule": "delay_stop10_monthly_requalification", **policy,
                })
            print(
                f"requalification complete {as_of}: selected={len(selected)} "
                f"overheated={overheated} market20d={market_return_20d:.4f}", flush=True
            )
    finally:
        bs.logout()

    outcomes = pd.DataFrame(outcome_rows)
    decisions = pd.DataFrame(decision_rows)
    reviews = pd.DataFrame(review_rows)
    outcomes.to_csv(output_dir / "requalification_outcomes.csv", index=False)
    decisions.to_csv(output_dir / "requalification_decisions.csv", index=False)
    reviews.to_csv(output_dir / "review_funnels.csv", index=False)
    summarize(outcomes).to_csv(output_dir / "requalification_summary.csv", index=False)
    metadata = {
        "entry_rule": "Q/V/M + EMA; delay 5 sessions when CSI300 20-session return >10%",
        "intramonth_exit": "fixed -10% stop with adverse gap-open fill",
        "review_frequency": "monthly calendar anniversary, execute next tradable open",
        "hard_review_exits": [
            "left CSI300", "security ineligible", "model unsupported",
        ],
        "confirmed_review_exit": (
            "exit when both market and stock are below EMA200, or when a stock below "
            "EMA200 also has two consecutive failed reviews / two consecutive EMA breaks"
        ),
        "warning_only": (
            "quality, value or momentum rank loss alone does not auto-exit; it remains a warning"
        ),
        "fundamental_boundary": (
            "ROE, CFO/revenue and EPS stability are frozen at entry for this demo; "
            "daily valuation, momentum and EMA fields are updated at every review"
        ),
        "universe_boundary": (
            "membership departures are checked, but new members are not added to the frozen "
            "entry cross-section"
        ),
    }
    (output_dir / "requalification_metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Test monthly requalification exits")
    parser.add_argument("--snapshot-root", required=True, type=Path)
    parser.add_argument("--nodes-file", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    run_experiment(args.snapshot_root, args.nodes_file, args.output)


if __name__ == "__main__":
    main()
