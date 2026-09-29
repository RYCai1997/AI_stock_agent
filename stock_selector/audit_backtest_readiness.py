"""Read-only preflight for historical V1 replay inputs."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from backtest.report import missing_signal_months


def audit_snapshots(snapshots_dir: Path) -> dict:
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
            rows = len(pd.read_csv(metrics_path))
            if (provider.get("as_of") != folder.name
                    or provider.get("built_rows") != rows
                    or provider.get("requested_members") != rows
                    or provider.get("errors")):
                issues.append(f"{folder.name}: provider date, row count, or error status is inconsistent")
            else:
                dates.append(folder.name)
        except (KeyError, ValueError, OSError) as exc:
            issues.append(f"{folder.name}: unreadable snapshot ({exc})")
    dates.sort()
    missing = missing_signal_months(dates)
    expected_count = len({date[:7] for date in dates}) + len(missing)
    return {
        "snapshot_dates": dates,
        "complete_snapshots": len(dates),
        "expected_signal_months_between_first_and_last": expected_count,
        "missing_signal_months": missing,
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
    args = parser.parse_args()
    if not args.snapshots_dir.is_dir():
        parser.error("snapshots directory does not exist")
    report = audit_snapshots(args.snapshots_dir)
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
