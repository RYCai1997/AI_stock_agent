from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd


RAW_METRICS = [
    "roic",
    "fcf_margin",
    "eps_growth_std",
    "earnings_yield",
    "fcf_yield",
    "book_to_price",
    "mom_6_1",
    "mom_12_1",
]


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare migrated US selector output with a legacy Q/V/M snapshot")
    parser.add_argument("--new", required=True, type=Path)
    parser.add_argument("--legacy", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    new = pd.read_csv(args.new).set_index("ticker")
    legacy = pd.read_csv(args.legacy).set_index("ticker")
    common = new.index.intersection(legacy.index)
    metric_error = {
        column: float((new.loc[common, column] - legacy.loc[common, column]).abs().max())
        for column in RAW_METRICS
    }
    new_selected = set(new.index[new["fundamental_candidate"] == True])  # noqa: E712
    old_selected = set(legacy.index[legacy["selected"] == True])  # noqa: E712
    result = {
        "status": "PASS" if new_selected == old_selected and max(metric_error.values()) < 1e-12 else "FAIL",
        "common_rows": len(common),
        "new_selected": len(new_selected),
        "legacy_selected": len(old_selected),
        "new_only": sorted(new_selected - old_selected),
        "legacy_only": sorted(old_selected - new_selected),
        "max_absolute_metric_error": metric_error,
    }
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

