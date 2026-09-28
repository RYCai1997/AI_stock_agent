"""Semi-manual CSI300 historical constituent cross-check against official lists."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd


def compare_memberships(snapshots_dir: Path,
                        official_membership: pd.DataFrame | None = None) -> pd.DataFrame:
    official = official_membership
    if official is not None and not {"as_of", "ticker", "notice_url"} <= set(official):
        raise ValueError("official CSV requires as_of,ticker,notice_url")
    rows = []
    for metadata_file in sorted(snapshots_dir.glob("*/official_run_metadata.json")):
        as_of = metadata_file.parent.name
        run = json.loads(metadata_file.read_text(encoding="utf-8"))
        provider = run["provider"]
        members = set(provider.get("member_codes", []))
        references = (official.loc[official["as_of"].astype(str).eq(as_of)]
                      if official is not None else pd.DataFrame())
        source_urls = sorted(set(references["notice_url"].dropna().astype(str))) if len(references) else []
        if not len(references) or not source_urls:
            status = "pending_official_notice"
            expected = set()
        else:
            expected = set(references["ticker"].astype(str))
            status = "match" if expected == members else "mismatch"
        rows.append({"as_of": as_of, "status": status,
                     "provider_count": len(members),
                     "official_count": len(expected) if status != "pending_official_notice" else None,
                     "missing_from_provider": ";".join(sorted(expected - members)),
                     "unexpected_in_provider": ";".join(sorted(members - expected))
                     if status != "pending_official_notice" else "",
                     "membership_update_min": provider.get("membership_update_min"),
                     "membership_update_max": provider.get("membership_update_max"),
                     "membership_update_unique_count": provider.get("membership_update_unique_count"),
                     "official_notice_urls": ";".join(source_urls)})
    return pd.DataFrame(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare stored CSI300 snapshots with official dated membership lists")
    parser.add_argument("--snapshots-dir", type=Path, required=True)
    parser.add_argument("--official-membership", type=Path)
    parser.add_argument("--output", type=Path, default=Path("reports/continuous_backtest/universe_audit.csv"))
    args = parser.parse_args()
    official = pd.read_csv(args.official_membership) if args.official_membership else None
    result = compare_memberships(args.snapshots_dir, official)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    result.to_csv(args.output, index=False)
    print(result.to_string(index=False))


if __name__ == "__main__":
    main()
