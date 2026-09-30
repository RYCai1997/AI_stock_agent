"""Verify sealed prospective or reconstructed prediction files without modifying them."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from data_public.archive import sha256


def verify_prediction(directory: Path, registry: Path | None = None) -> bool:
    try:
        seal_path = directory / "prediction_seal.json"
        seal = json.loads(seal_path.read_text(encoding="utf-8"))
        expected = seal["files"]
        actual = {str(path.relative_to(directory)).replace("\\", "/") for path in
                  directory.rglob("*") if path.is_file() and path != seal_path}
        if actual != set(expected):
            return False
        if any(sha256((directory / name).read_bytes()) != digest for name, digest in expected.items()):
            return False
        record = json.loads((directory / "prediction_record.json").read_text(encoding="utf-8"))
        if seal["prediction_id"] != record["prediction_id"]:
            return False
        if sha256((directory / "source_manifest.json").read_bytes()) != record["source_manifest_hash"]:
            return False
        from prospective.package import _aggregate_hash
        source_files = {name: (directory / name).read_bytes() for name in (
            "universe.csv", "fundamentals.csv", "market_snapshot.csv",
            "candidates.csv", "selected.csv", "actionable.csv", "portfolio_plan.csv")}
        if _aggregate_hash(source_files, ("universe.csv", "fundamentals.csv",
                                          "market_snapshot.csv")) != record["input_hash"]:
            return False
        if _aggregate_hash(source_files, ("candidates.csv", "selected.csv",
                                          "actionable.csv", "portfolio_plan.csv")) != record["output_hash"]:
            return False
        if registry and record["prospective_primary"]:
            import csv
            from prospective.package import _registry_rows
            _registry_rows(registry)
            with registry.open(newline="", encoding="utf-8") as handle:
                matches = [row for row in csv.DictReader(handle)
                           if row["prediction_id"] == record["prediction_id"]]
            if len(matches) != 1 or matches[0]["prediction_hash"] != sha256(seal_path.read_bytes()):
                return False
        return True
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return False


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate a sealed prediction package")
    parser.add_argument("prediction_dir", type=Path)
    parser.add_argument("--registry", type=Path)
    args = parser.parse_args()
    valid = verify_prediction(args.prediction_dir, args.registry)
    print("SEALED" if valid else "INVALID")
    if not valid:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
