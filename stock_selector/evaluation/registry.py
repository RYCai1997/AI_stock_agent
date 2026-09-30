"""Append-only outcome index, kept separate from sealed prediction history."""

from __future__ import annotations

import csv
import json
from pathlib import Path

from data_public.archive import sha256
from verify_prediction import verify_prediction


FIELDS = ("prediction_id", "signal_date", "horizon_sessions", "horizon_end_date",
          "evaluated_at_utc", "evaluation_sha256", "prediction_seal_sha256",
          "previous_row_hash")


def _hash_row(row: dict) -> str:
    return sha256(json.dumps(row, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def read_registry(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if tuple(reader.fieldnames or ()) != FIELDS:
            raise ValueError("evaluation registry schema mismatch")
        rows = list(reader)
    prior = "GENESIS"
    for row in rows:
        if row["previous_row_hash"] != prior:
            raise ValueError("evaluation registry hash chain invalid")
        prior = _hash_row(row)
    return rows


def register_evaluation(prediction_dir: Path, evaluation_path: Path, registry_path: Path) -> dict:
    if not verify_prediction(prediction_dir):
        raise ValueError("prediction seal invalid")
    record = json.loads((prediction_dir / "prediction_record.json").read_text(encoding="utf-8"))
    evaluation = json.loads(evaluation_path.read_text(encoding="utf-8"))
    seal_hash = sha256((prediction_dir / "prediction_seal.json").read_bytes())
    if (not record["prospective_primary"] or
            evaluation.get("evidence_label") != "prospective_outcome" or
            evaluation.get("prediction_id") != record["prediction_id"] or
            evaluation.get("prediction_seal_sha256") != seal_hash or
            evaluation.get("signal_date") != record["signal_date"] or
            evaluation.get("horizon_sessions") not in (20, 63, 126)):
        raise ValueError("evaluation does not match primary sealed prediction")
    rows = read_registry(registry_path)
    key = (record["prediction_id"], str(evaluation["horizon_sessions"]))
    if any((row["prediction_id"], row["horizon_sessions"]) == key for row in rows):
        raise ValueError("evaluation already registered; original row cannot be replaced")
    row = {"prediction_id": record["prediction_id"], "signal_date": record["signal_date"],
           "horizon_sessions": str(evaluation["horizon_sessions"]),
           "horizon_end_date": evaluation["horizon_end_date"],
           "evaluated_at_utc": evaluation["evaluated_at_utc"],
           "evaluation_sha256": sha256(evaluation_path.read_bytes()),
           "prediction_seal_sha256": seal_hash,
           "previous_row_hash": _hash_row(rows[-1]) if rows else "GENESIS"}
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    with registry_path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        if not rows:
            writer.writeheader()
        writer.writerow(row)
    return row
