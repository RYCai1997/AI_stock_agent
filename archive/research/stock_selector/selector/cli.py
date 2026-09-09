from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from .pipeline import run_selection


def main() -> None:
    parser = argparse.ArgumentParser(description="Point-in-time cross-market stock selector")
    parser.add_argument("--market", required=True, choices=["A", "HK", "US"])
    parser.add_argument("--as-of", required=True, help="Selection date: YYYY-MM-DD")
    parser.add_argument("--input", required=True, type=Path, help="Prepared point-in-time metrics CSV")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--market-trend", choices=["up", "down", "unknown"], default="unknown")
    args = parser.parse_args()
    _, metadata = run_selection(
        pd.read_csv(args.input),
        market=args.market,
        as_of=args.as_of,
        output_dir=args.output,
        market_trend=args.market_trend,
    )
    print(json.dumps(metadata, ensure_ascii=False, indent=2))

