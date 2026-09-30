"""Independently record observed opens after a sealed prospective signal."""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from data_public.archive import sha256
from prospective.reconciliation import reconcile_execution


def main() -> None:
    parser = argparse.ArgumentParser(description="Reconcile a sealed prediction against observed opens")
    parser.add_argument("prediction_dir", type=Path)
    parser.add_argument("--observations", type=Path, required=True,
                        help="JSON object keyed by ticker with date/open/tradable/limit_up/limit_down")
    parser.add_argument("--source-manifest", type=Path, required=True)
    parser.add_argument("--slippage", type=float, default=.001)
    parser.add_argument("--output-dir", type=Path,
                        default=Path(__file__).resolve().parent / "outputs" / "evaluation" / "reconciliation")
    args = parser.parse_args()
    source = json.loads(args.source_manifest.read_text(encoding="utf-8"))
    if source.get("raw_sha256") != sha256(args.observations.read_bytes()):
        parser.error("observation source hash mismatch")
    observations = json.loads(args.observations.read_text(encoding="utf-8"))
    if not isinstance(observations, dict):
        parser.error("observations must be a ticker-keyed JSON object")
    path = reconcile_execution(prediction_dir=args.prediction_dir,
                               observations=observations, source_manifest=source,
                               evaluated_at=datetime.now(ZoneInfo("Asia/Shanghai")),
                               slippage=args.slippage, output_dir=args.output_dir)
    print(json.dumps({"status": "reconciled", "output": str(path.resolve())}))


if __name__ == "__main__":
    main()
