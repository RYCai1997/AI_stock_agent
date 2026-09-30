"""Read future public price history only after a sealed horizon has matured."""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from data_public.archive import sha256
from evaluation.outcomes import HORIZONS, evaluate_outcome


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate a sealed prediction after a fixed horizon")
    parser.add_argument("prediction_dir", type=Path)
    parser.add_argument("--stock-prices", type=Path, required=True)
    parser.add_argument("--benchmark-prices", type=Path, required=True)
    parser.add_argument("--source-manifest", type=Path, required=True)
    parser.add_argument("--horizon", type=int, choices=HORIZONS, required=True)
    parser.add_argument("--output-root", type=Path,
                        default=Path(__file__).resolve().parent / "outputs" / "evaluation")
    args = parser.parse_args()
    source = json.loads(args.source_manifest.read_text(encoding="utf-8"))
    if (source.get("stock_sha256") != sha256(args.stock_prices.read_bytes()) or
            source.get("benchmark_sha256") != sha256(args.benchmark_prices.read_bytes())):
        parser.error("outcome source manifest hashes do not match price files")
    result = evaluate_outcome(
        prediction_dir=args.prediction_dir,
        stock_prices=pd.read_csv(args.stock_prices, dtype={"ticker": str, "date": str}),
        benchmark_prices=pd.read_csv(args.benchmark_prices, dtype={"date": str}),
        price_source_manifest=source, horizon_sessions=args.horizon,
        evaluated_at=datetime.now(ZoneInfo("Asia/Shanghai")), output_root=args.output_root)
    print(json.dumps({"status": "evaluated", "output": str(result.resolve())}))


if __name__ == "__main__":
    main()
