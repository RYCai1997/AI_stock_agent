from __future__ import annotations

import argparse
import json
from pathlib import Path

from selector.pipeline import run_selection
from selector.providers import build_us_metrics


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a point-in-time US selector from local SEC/Yahoo caches")
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--history", required=True, type=Path)
    parser.add_argument("--ticker-map", required=True, type=Path)
    parser.add_argument("--fact-cache", required=True, type=Path)
    parser.add_argument("--submission-cache", required=True, type=Path)
    parser.add_argument("--price-cache", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    metrics, provider_metadata = build_us_metrics(
        args.as_of,
        args.history,
        args.ticker_map,
        args.fact_cache,
        args.submission_cache,
        args.price_cache,
    )
    args.output.mkdir(parents=True, exist_ok=True)
    metrics.to_csv(args.output / "raw_metrics.csv", index=False)
    _, selector_metadata = run_selection(
        metrics,
        market="US",
        as_of=args.as_of,
        output_dir=args.output,
        market_trend=provider_metadata["market_trend"],
    )
    combined = {"provider": provider_metadata, "selector": selector_metadata}
    (args.output / "run_metadata.json").write_text(
        json.dumps(combined, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(combined, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

