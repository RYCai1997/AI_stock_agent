"""Regenerable, machine-readable prospective status without premature alpha claims."""

from __future__ import annotations

import csv
import json
import os
from pathlib import Path

from .registry import read_registry


FIELDS = ("status", "number_of_signals", "number_of_actual_entry_windows",
          "evaluated_20d", "evaluated_63d", "evaluated_126d",
          "cumulative_shadow_nav", "csi300_benchmark", "exposure_matched_benchmark",
          "hit_rate", "turnover", "drawdown", "realized_costs")


def build_performance(prediction_registry: Path, evaluation_registry: Path,
                      shadow_dir: Path, output_path: Path) -> dict:
    if prediction_registry.exists():
        with prediction_registry.open(newline="", encoding="utf-8") as handle:
            predictions = list(csv.DictReader(handle))
    else:
        predictions = []
    evaluations = read_registry(evaluation_registry)
    nav_path = shadow_dir / "shadow_nav.jsonl"
    trade_path = shadow_dir / "shadow_trades.jsonl"
    nav = [json.loads(line) for line in nav_path.read_text(encoding="utf-8").splitlines()] if nav_path.exists() else []
    trades = [json.loads(line) for line in trade_path.read_text(encoding="utf-8").splitlines()] if trade_path.exists() else []
    signal_ids = {row["prediction_id"] for row in predictions}
    if any(row["prediction_id"] not in signal_ids for row in evaluations):
        raise ValueError("outcome registry references an unknown prediction")
    entry_dates = {trade["actual_execution_date"] for trade in trades if trade.get("side") == "buy"}
    # Fewer than 12 monthly signals is protocol monitoring, not performance evidence.
    row = {"status": "insufficient_sample" if len(predictions) < 12 else "descriptive_only",
           "number_of_signals": len(predictions),
           "number_of_actual_entry_windows": len(entry_dates),
           **{f"evaluated_{h}d": sum(item["horizon_sessions"] == str(h) for item in evaluations)
              for h in (20, 63, 126)},
           "cumulative_shadow_nav": nav[-1]["daily_nav"] if nav else "",
           "csi300_benchmark": "", "exposure_matched_benchmark": "",
           "hit_rate": "", "turnover": "", "drawdown": "", "realized_costs": ""}
    if nav:
        values = [float(item["daily_nav"]) for item in nav]
        high = values[0]
        drawdowns = []
        for value in values:
            high = max(high, value)
            drawdowns.append(value / high - 1)
        row["drawdown"] = min(drawdowns)
        row["realized_costs"] = nav[-1].get("fees", "")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_suffix(output_path.suffix + ".tmp")
    with temporary.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerow(row)
    os.replace(temporary, output_path)
    return row
