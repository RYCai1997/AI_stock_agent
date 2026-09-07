"""Test fixed stop and overheat-entry rules on already-selected A-share names."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from selector.providers.a_baostock import _rows


HORIZONS = (1, 3, 6)
PATH_FIELDS = "date,code,open,high,low,close,tradestatus"
RULES = (
    "hold_immediate",
    "stop10_immediate",
    "overheat_delay5",
    "overheat_stagger50",
    "overheat_delay5_stop10",
)


def prepare_prices(frame: pd.DataFrame) -> pd.DataFrame:
    result = frame.copy()
    result["date"] = pd.to_datetime(result["date"])
    for column in ["open", "high", "low", "close"]:
        result[column] = pd.to_numeric(result[column], errors="coerce")
    result = result[result["tradestatus"].astype(str).eq("1")]
    return result.dropna(subset=["open", "high", "low", "close"]).sort_values("date")


def market_is_overheated(prices: pd.DataFrame, as_of: str, threshold: float = 0.10) -> tuple[bool, float | None]:
    history = prepare_prices(prices)
    history = history[history["date"].le(pd.Timestamp(as_of))]
    if len(history) < 21:
        return False, None
    return_20d = float(history.iloc[-1]["close"] / history.iloc[-21]["close"] - 1.0)
    return return_20d > threshold, return_20d


def _component_path(
    future: pd.DataFrame, entry_position: int, stop_loss: float | None
) -> tuple[pd.Series, dict[str, Any]]:
    if len(future) <= entry_position:
        raise ValueError("insufficient future sessions for entry")
    entry = future.iloc[entry_position]
    entry_price = float(entry["open"])
    values = pd.Series(1.0, index=future.index, dtype=float)
    active = future.index >= entry.name
    values.loc[active] = future.loc[active, "close"] / entry_price
    stop_date = None
    stop_fill = None
    if stop_loss is not None:
        stop_price = entry_price * (1.0 - stop_loss)
        after_entry = future.loc[active]
        triggered = after_entry[after_entry["low"].le(stop_price)]
        if not triggered.empty:
            stop_row = triggered.iloc[0]
            stop_date = stop_row["date"]
            stop_fill = min(float(stop_row["open"]), stop_price)
            values.loc[values.index >= stop_row.name] = stop_fill / entry_price
    return values, {
        "entry_date": str(entry["date"].date()),
        "entry_price": entry_price,
        "stop_date": str(stop_date.date()) if stop_date is not None else None,
        "stop_fill": stop_fill,
    }


def simulate_rule(
    prices: pd.DataFrame,
    as_of: str,
    rule: str,
    overheated: bool,
    delay_sessions: int = 5,
    stop_loss: float = 0.10,
) -> dict[str, Any]:
    if rule not in RULES:
        raise ValueError(f"unsupported rule: {rule}")
    prepared = prepare_prices(prices)
    future = prepared[prepared["date"].gt(pd.Timestamp(as_of))].reset_index(drop=True)
    if future.empty:
        return {"outcome_error": "no future prices"}

    use_delay = overheated and rule in {
        "overheat_delay5", "overheat_stagger50", "overheat_delay5_stop10"
    }
    use_stop = rule in {"stop10_immediate", "overheat_delay5_stop10"}
    if use_delay and rule == "overheat_stagger50":
        specifications = [(0.5, 0), (0.5, delay_sessions)]
    else:
        specifications = [(1.0, delay_sessions if use_delay else 0)]

    total_values = pd.Series(0.0, index=future.index)
    component_metadata = []
    for weight, entry_position in specifications:
        values, metadata = _component_path(
            future, entry_position, stop_loss if use_stop else None
        )
        total_values = total_values + weight * values
        component_metadata.append({"weight": weight, **metadata})

    result: dict[str, Any] = {
        "overheated": overheated,
        "components": json.dumps(component_metadata, ensure_ascii=False),
        "stop_triggered": any(item["stop_date"] is not None for item in component_metadata),
    }
    for months in HORIZONS:
        target = pd.Timestamp(as_of) + pd.DateOffset(months=months)
        eligible = future[future["date"].ge(target)]
        result[f"return_{months}m"] = (
            float(total_values.loc[eligible.index[0]] - 1.0) if not eligible.empty else None
        )
    six_month_target = pd.Timestamp(as_of) + pd.DateOffset(months=6)
    path_values = total_values.loc[future["date"].le(six_month_target)]
    with_initial = pd.concat([pd.Series([1.0]), path_values.reset_index(drop=True)], ignore_index=True)
    result["max_drawdown_6m"] = float((with_initial / with_initial.cummax() - 1.0).min())
    result["worst_return_from_entry_6m"] = float(with_initial.min() - 1.0)
    return result


def fetch_path(bs: Any, code: str, as_of: str) -> pd.DataFrame:
    start = str((pd.Timestamp(as_of) - pd.Timedelta(days=45)).date())
    end = str((pd.Timestamp(as_of) + pd.DateOffset(months=6, days=14)).date())
    rows = _rows(bs.query_history_k_data_plus(
        code, PATH_FIELDS, start_date=start, end_date=end, frequency="d", adjustflag="2"
    ))
    if not rows:
        raise ValueError("no price path")
    return pd.DataFrame(rows)


def load_or_fetch_path(bs: Any, code: str, as_of: str, cache_dir: Path) -> pd.DataFrame:
    path = cache_dir / f"{as_of}_{code}.csv"
    if path.exists():
        return pd.read_csv(path)
    frame = fetch_path(bs, code, as_of)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".csv.tmp")
    frame.to_csv(temporary, index=False)
    temporary.replace(path)
    return frame


def summarize(outcomes: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for (as_of, rule), sample in outcomes.groupby(["as_of", "rule"], sort=True):
        for months in HORIZONS:
            returns = pd.to_numeric(sample[f"return_{months}m"], errors="coerce").dropna()
            rows.append({
                "as_of": as_of,
                "rule": rule,
                "overheated": bool(sample["overheated"].iloc[0]),
                "selected_n": len(sample),
                "evaluated_n": len(returns),
                "horizon_months": months,
                "mean_return": float(returns.mean()),
                "median_return": float(returns.median()),
                "win_rate": float(returns.gt(0).mean()),
                "stop_rate": float(sample["stop_triggered"].mean()),
                "mean_stock_max_drawdown_6m": float(sample["max_drawdown_6m"].mean()),
                "mean_worst_return_from_entry_6m": float(
                    sample["worst_return_from_entry_6m"].mean()
                ),
            })
    return pd.DataFrame(rows)


def run_experiment(snapshot_root: Path, nodes_file: Path, output_dir: Path) -> None:
    nodes = pd.read_csv(nodes_file)
    active_nodes = nodes[nodes["n_qvm_current"].gt(0)]["as_of"].astype(str).tolist()
    if not active_nodes:
        raise ValueError("no active Q/V/M nodes")
    output_dir.mkdir(parents=True, exist_ok=True)
    path_cache = output_dir / "path_cache"

    try:
        import baostock as bs
    except ImportError as exc:
        raise RuntimeError("baostock is required") from exc
    login = bs.login()
    if login.error_code != "0":
        raise RuntimeError(f"Baostock login failed: {login.error_msg}")
    rows = []
    node_metadata = []
    try:
        for as_of in active_nodes:
            candidates = pd.read_csv(snapshot_root / as_of / "candidates.csv")
            selected = candidates[candidates["actionable_candidate"].astype(bool)]
            benchmark_path = load_or_fetch_path(bs, "sh.000300", as_of, path_cache)
            overheated, market_return_20d = market_is_overheated(benchmark_path, as_of)
            node_metadata.append({
                "as_of": as_of, "selected_n": len(selected), "overheated": overheated,
                "market_return_20d": market_return_20d,
            })
            for position, (_, stock) in enumerate(selected.iterrows(), 1):
                prices = load_or_fetch_path(bs, stock["ticker"], as_of, path_cache)
                for rule in RULES:
                    rows.append({
                        "as_of": as_of, "ticker": stock["ticker"], "company": stock["company"],
                        "industry_l1": stock["industry_l1"], "rule": rule,
                        **simulate_rule(prices, as_of, rule, overheated),
                    })
                if position % 10 == 0 or position == len(selected):
                    print(f"risk paths {as_of}: {position}/{len(selected)}", flush=True)
    finally:
        bs.logout()

    outcomes = pd.DataFrame(rows)
    summary = summarize(outcomes)
    outcomes.to_csv(output_dir / "risk_rule_outcomes.csv", index=False)
    summary.to_csv(output_dir / "risk_rule_summary.csv", index=False)
    pd.DataFrame(node_metadata).to_csv(output_dir / "risk_rule_nodes.csv", index=False)
    metadata = {
        "selection": "unchanged qvm_current actionable candidates",
        "overheat_rule": "CSI 300 trailing 20-trading-session return > 10%, measured at signal close",
        "delay_rule": "when overheated, enter at the open of the sixth tradable session after signal",
        "stagger_rule": "when overheated, 50% next-session open and 50% sixth-session open",
        "stop_rule": "10% intraday fixed stop; gap below stop fills at the lower opening price",
        "costs": "fees, tax, slippage and limit-lock execution not modeled",
    }
    (output_dir / "risk_rule_metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Test A-share stop and overheat entry rules")
    parser.add_argument("--snapshot-root", required=True, type=Path)
    parser.add_argument("--nodes-file", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    run_experiment(args.snapshot_root, args.nodes_file, args.output)


if __name__ == "__main__":
    main()
