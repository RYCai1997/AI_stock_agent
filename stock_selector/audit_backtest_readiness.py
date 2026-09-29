"""Read-only preflight for historical V1 replay inputs."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from backtest.report import missing_signal_months


def audit_snapshots(snapshots_dir: Path, expected_dates: list[str] | None = None) -> dict:
    issues: list[str] = []
    dates: list[str] = []
    for folder in sorted(path for path in snapshots_dir.iterdir() if path.is_dir()):
        metrics_path = folder / "raw_metrics.csv"
        metadata_path = folder / "official_run_metadata.json"
        if not metrics_path.exists() or not metadata_path.exists():
            issues.append(f"{folder.name}: missing raw_metrics.csv or official_run_metadata.json")
            continue
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            provider = metadata["provider"]
            metrics = pd.read_csv(metrics_path)
            rows = len(metrics)
            if (provider.get("as_of") != folder.name
                    or provider.get("built_rows") != rows
                    or provider.get("requested_members") != rows
                    or provider.get("errors")):
                issues.append(f"{folder.name}: provider date, row count, or error status is inconsistent")
            else:
                dates.append(folder.name)
            if "ticker" in metrics and metrics["ticker"].duplicated().any():
                issues.append(f"{folder.name}: duplicate ticker in snapshot")
            if provider.get("member_codes") and set(metrics["ticker"].astype(str)) != set(provider["member_codes"]):
                issues.append(f"{folder.name}: member codes differ from raw metrics")
            for column in ("fundamental_as_of", "price_as_of", "membership_update_date"):
                if column in metrics:
                    values = pd.to_datetime(metrics[column], errors="coerce")
                    if values.isna().any():
                        issues.append(f"{folder.name}: missing or invalid {column}")
                    if values.gt(pd.Timestamp(folder.name)).any():
                        issues.append(f"{folder.name}: future {column}")
        except (KeyError, ValueError, OSError) as exc:
            issues.append(f"{folder.name}: unreadable snapshot ({exc})")
    dates.sort()
    if expected_dates is None:
        missing = missing_signal_months(dates)
        expected_count = len({date[:7] for date in dates}) + len(missing)
        missing_dates = []
    else:
        if len(set(expected_dates)) != len(expected_dates):
            raise ValueError("signal calendar contains duplicate dates")
        expected = set(expected_dates)
        for date in dates:
            if date not in expected:
                issues.append(f"{date}: not on frozen signal calendar")
        missing_dates = sorted(expected - set(dates))
        missing = [date[:7] for date in missing_dates]
        expected_count = len(expected_dates)
    return {
        "snapshot_dates": dates,
        "complete_snapshots": len(dates),
        "expected_signal_months_between_first_and_last": expected_count,
        "missing_signal_months": missing,
        "missing_signal_dates": missing_dates,
        "snapshot_issues": issues,
        "monthly_signal_coverage_complete": bool(dates) and not missing and not issues,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit offline V1 research input readiness")
    parser.add_argument("--snapshots-dir", type=Path, required=True)
    parser.add_argument("--bars", type=Path)
    parser.add_argument("--actions", type=Path)
    parser.add_argument("--benchmark-bars", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--signal-calendar", type=Path,
                        default=Path(__file__).resolve().parent / "data_history" / "schema" /
                        "signal_calendar_2020-03_2025-07.csv")
    args = parser.parse_args()
    if not args.snapshots_dir.is_dir():
        parser.error("snapshots directory does not exist")
    calendar = pd.read_csv(args.signal_calendar, dtype={"signal_date": str})
    if "signal_date" not in calendar:
        parser.error("signal calendar requires signal_date column")
    report = audit_snapshots(args.snapshots_dir, expected_dates=calendar["signal_date"].tolist())
    report["signal_calendar"] = str(args.signal_calendar.resolve())
    for label, path in (("unadjusted_bars", args.bars),
                        ("corporate_actions", args.actions),
                        ("benchmark_bars", args.benchmark_bars)):
        report[label] = str(path.resolve()) if path and path.is_file() else None
    report["replay_inputs_present"] = (report["monthly_signal_coverage_complete"]
                                       and all(report[label] for label in
                                               ("unadjusted_bars", "corporate_actions", "benchmark_bars")))
    report["official_universe_and_corporate_action_audit"] = "not_assessed"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
