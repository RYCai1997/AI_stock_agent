"""Create one sealed, append-only prediction package from already audited inputs."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

from data_public import DATA_PROVIDER_VERSION
from data_public.archive import sha256
from selector.strategy import OFFICIAL_STRATEGY


PREDICTION_SCHEMA_VERSION = 1
EXECUTION_MODEL_VERSION = "1.0"
REQUIRED_INPUT_FILES = ("universe.csv", "fundamentals.csv", "market_snapshot.csv",
                        "candidates.csv", "selected.csv", "actionable.csv", "portfolio_plan.csv")
REGISTRY_FIELDS = ("prediction_id", "signal_date", "generated_at", "strategy_version",
                   "provider_version", "git_commit", "prediction_hash", "status",
                   "previous_row_hash", "20d_evaluation", "63d_evaluation", "126d_evaluation")


def git_state(root: Path) -> dict:
    try:
        commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root,
                                         text=True, stderr=subprocess.DEVNULL).strip()
        status = subprocess.check_output(["git", "status", "--porcelain"], cwd=root,
                                         text=True, stderr=subprocess.DEVNULL).strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError("git commit and working tree are required for prediction") from exc
    return {"git_commit": commit, "working_tree_status": status,
            "dirty": bool(status)}


def _canonical(data: dict) -> bytes:
    return (json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _aggregate_hash(files: dict[str, bytes], selected: tuple[str, ...]) -> str:
    return sha256(_canonical({name: sha256(files[name]) for name in selected}))


def _registry_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    if any(set(row) != set(REGISTRY_FIELDS) for row in rows):
        raise ValueError("prospective registry schema mismatch")
    previous = "GENESIS"
    for row in rows:
        if row["previous_row_hash"] != previous:
            raise ValueError("prospective registry hash chain invalid")
        previous = sha256(_canonical(row))
    return rows


def _append_registry(path: Path, record: dict, seal_hash: str) -> None:
    rows = _registry_rows(path)
    if any(row["prediction_id"] == record["prediction_id"] for row in rows):
        raise ValueError("duplicate prediction ID")
    previous = sha256(_canonical(rows[-1])) if rows else "GENESIS"
    entry = {"prediction_id": record["prediction_id"], "signal_date": record["signal_date"],
             "generated_at": record["generated_at"], "strategy_version": record["strategy_version"],
             "provider_version": record["data_provider_version"], "git_commit": record["git_commit"],
             "prediction_hash": seal_hash, "status": "SEALED", "previous_row_hash": previous,
             "20d_evaluation": "pending", "63d_evaluation": "pending", "126d_evaluation": "pending"}
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=REGISTRY_FIELDS)
        if not rows:
            writer.writeheader()
        writer.writerow(entry)


def create_prediction_package(*, prospective_root: Path, retrospective_root: Path,
                              signal_date: str, generated_at: datetime,
                              evidence_label: str, files: dict[str, bytes],
                              source_manifest: dict, signal: dict, quality: dict,
                              provenance: dict, allow_dirty: bool = False) -> Path:
    """Seal only truly live, complete V1 runs as primary; never replace an existing date."""
    if evidence_label not in {"prospective", "retrospective_reconstruction", "research_only"}:
        raise ValueError("invalid evidence label")
    local_date = generated_at.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
    if evidence_label == "prospective" and local_date != signal_date:
        raise ValueError("past or future date cannot be prospective")
    if evidence_label == "prospective" and (
            signal.get("scheduled_signal_date") != signal_date or
            signal.get("market_close_verified") is not True):
        raise ValueError("primary prediction requires verified scheduled close")
    missing = set(REQUIRED_INPUT_FILES) - set(files)
    if missing:
        raise ValueError(f"missing prediction package files: {sorted(missing)}")
    if not quality.get("primary_eligible"):
        raise ValueError("core public input coverage is incomplete")
    if provenance.get("dirty") and evidence_label == "prospective" and not allow_dirty:
        raise ValueError("clean git tree required; use --allow-dirty for degraded research")
    if not provenance.get("git_commit"):
        raise ValueError("git commit required")
    if source_manifest.get("data_provider_version") != DATA_PROVIDER_VERSION:
        raise ValueError("source manifest must identify PUBLIC_V1")
    protected = {"prediction_id", "signal_date", "generated_at", "strategy_id",
                 "strategy_version", "data_provider_version", "git_commit",
                 "evidence_label", "prospective_primary", "non_primary_rerun",
                 "source_manifest_hash", "input_hash", "output_hash", "data_quality"}
    if set(signal) & protected:
        raise ValueError("signal payload cannot override sealed prediction identity")
    if not source_manifest.get("sources") or any(not item.get("raw_sha256") or
                                                 not item.get("normalized_sha256")
                                                 for item in source_manifest["sources"]):
        raise ValueError("every public source requires raw and normalized hashes")
    primary = evidence_label == "prospective"
    root = prospective_root if primary else retrospective_root
    dated = root / signal_date
    if primary and dated.exists():
        primary = False
        root = retrospective_root
        dated = root / signal_date
    if not primary:
        dated.mkdir(parents=True, exist_ok=True)
        index = 1
        while (dated / f"rerun_{index:03d}").exists():
            index += 1
        target = dated / f"rerun_{index:03d}"
    else:
        target = dated
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".prediction_staging_", dir=root) as folder:
        staging = Path(folder)
        for name, payload in files.items():
            if Path(name).is_absolute() or ".." in Path(name).parts or name in {
                "prediction_record.json", "prediction_seal.json"}:
                raise ValueError("unsafe or reserved package path")
            path = staging / name
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("xb") as handle:
                handle.write(payload)
        strategy_manifest = {"strategy_id": OFFICIAL_STRATEGY.strategy_id,
                             "strategy_version": OFFICIAL_STRATEGY.strategy_version,
                             "strategy": OFFICIAL_STRATEGY.to_dict(),
                             "data_provider_version": DATA_PROVIDER_VERSION,
                             "execution_model_version": EXECUTION_MODEL_VERSION,
                             "prediction_schema_version": PREDICTION_SCHEMA_VERSION}
        (staging / "source_manifest.json").write_bytes(_canonical(source_manifest))
        (staging / "strategy_manifest.json").write_bytes(_canonical(strategy_manifest))
        entry_id = f"{signal_date}_{uuid4().hex}"
        record = {"prediction_id": entry_id, "signal_date": signal_date,
                  "generated_at": generated_at.astimezone(timezone.utc).isoformat(),
                  "strategy_id": OFFICIAL_STRATEGY.strategy_id,
                  "strategy_version": OFFICIAL_STRATEGY.strategy_version,
                  "data_provider_version": DATA_PROVIDER_VERSION,
                  "execution_model_version": EXECUTION_MODEL_VERSION,
                  "prediction_schema_version": PREDICTION_SCHEMA_VERSION,
                  "git_commit": provenance["git_commit"],
                  "working_tree_status": provenance.get("working_tree_status", ""),
                  "research_quality": "degraded" if provenance.get("dirty") else "normal",
                  "evidence_label": evidence_label if primary else "retrospective_reconstruction",
                  "prospective_primary": primary, "non_primary_rerun": not primary,
                  "source_manifest_hash": sha256((staging / "source_manifest.json").read_bytes()),
                  "input_hash": _aggregate_hash(files, ("universe.csv", "fundamentals.csv",
                                                        "market_snapshot.csv")),
                  "output_hash": _aggregate_hash(files, ("candidates.csv", "selected.csv",
                                                         "actionable.csv", "portfolio_plan.csv")),
                  **signal, "data_quality": quality}
        (staging / "prediction_record.json").write_bytes(_canonical(record))
        (staging / "run_summary.md").write_text(
            f"# {signal_date} V1 prediction\n\nEvidence: {record['evidence_label']}\n"
            f"Primary: {primary}\nProvider: {DATA_PROVIDER_VERSION}\n", encoding="utf-8")
        sealed_files = sorted(str(path.relative_to(staging)).replace("\\", "/") for path in
                              staging.rglob("*") if path.is_file())
        seal = {"seal_version": 1, "prediction_id": entry_id,
                "files": {name: sha256((staging / name).read_bytes()) for name in sealed_files}}
        (staging / "prediction_seal.json").write_bytes(_canonical(seal))
        target.parent.mkdir(parents=True, exist_ok=True)
        os.rename(staging, target)
    if primary:
        _append_registry(prospective_root / "prospective_registry.csv", record,
                         sha256((target / "prediction_seal.json").read_bytes()))
    return target
