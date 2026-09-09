from __future__ import annotations

import argparse
import json
from pathlib import Path

from selector.pipeline import run_selection
from selector.providers import build_a_metrics
from selector.strategy import OFFICIAL_STRATEGY


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a point-in-time CSI 300 selector")
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--limit", type=int, default=0, help="Controlled interface test; 0 means full CSI 300")
    parser.add_argument("--cache-dir", type=Path, help="Resume-safe per-stock provider cache")
    args = parser.parse_args()
    cache_dir = args.cache_dir or args.output / "provider_cache"
    metrics, provider = build_a_metrics(args.as_of, limit=args.limit, cache_dir=cache_dir)
    args.output.mkdir(parents=True, exist_ok=True)
    metrics.to_csv(args.output / "raw_metrics.csv", index=False)
    _, selector = run_selection(
        metrics, "A", args.as_of, args.output, provider["market_trend"],
        OFFICIAL_STRATEGY.selector_config(),
    )
    combined = {"provider": provider, "selector": selector}
    (args.output / "run_metadata.json").write_text(
        json.dumps(combined, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(combined, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
