"""Append an observed next-open review to an immutable shadow run."""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from selector.shadow import append_next_open_observation


def main() -> None:
    parser = argparse.ArgumentParser(description="Reconcile one prospective shadow run")
    parser.add_argument("--record", type=Path, required=True)
    parser.add_argument("--observations", type=Path, required=True)
    parser.add_argument("--source", required=True)
    parser.add_argument("--slippage", type=float, default=.001)
    args = parser.parse_args()
    frame = pd.read_csv(args.observations)
    required = {"ticker", "date", "open", "limit_up", "tradable"}
    if not required <= set(frame):
        raise ValueError(f"observation CSV missing {sorted(required - set(frame))}")
    observations = {}
    for row in frame.itertuples():
        ticker = str(row.ticker)
        if ticker in observations:
            raise ValueError(f"duplicate observation: {ticker}")
        observations[ticker] = {
            "date": str(row.date), "open": float(row.open),
            "limit_up": None if pd.isna(row.limit_up) else float(row.limit_up),
            "tradable": str(row.tradable).lower() in {"true", "1", "yes"},
        }
    path = append_next_open_observation(
        args.record, observations, source=args.source, slippage=args.slippage)
    print(path)


if __name__ == "__main__":
    main()
