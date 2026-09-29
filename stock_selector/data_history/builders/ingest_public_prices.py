"""Normalize cached public responses into ticker-oriented unadjusted price files."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from data_history.store import ingest_public_price_cache


def main() -> None:
    parser = argparse.ArgumentParser(description="Build local PIT price store from cached public responses")
    parser.add_argument("--public-bars-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = ingest_public_price_cache(args.public_bars_dir / "source_audit.json",
                                       args.public_bars_dir / "responses", args.output / "prices" / "unadjusted")
    print(json.dumps({key: value for key, value in report.items() if key != "missing_or_invalid"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
