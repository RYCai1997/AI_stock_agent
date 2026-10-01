"""Immutable raw public-response capture with explicit source fallback history."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from uuid import uuid4

from . import DATA_PROVIDER_VERSION


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class SourceResult:
    source: str
    endpoint: str
    parameters: dict
    raw: bytes
    normalized: bytes
    parser_version: str


class PublicSourceArchive:
    def __init__(self, root: Path):
        self.root = Path(root)

    def capture(self, result: SourceResult, *, requested_source: str,
                fallback_reason: str | None, attempts: list[dict],
                source_selection_reason: str | None = None) -> dict:
        if not result.source or not result.endpoint or not result.parser_version:
            raise ValueError("source, endpoint and parser_version are required")
        if "token" in {str(key).lower() for key in result.parameters}:
            raise ValueError("public source parameters cannot contain credentials")
        stamp = datetime.now(timezone.utc).isoformat()
        entry_id = f"{stamp[:10]}_{uuid4().hex}"
        raw_path = self.root / "raw" / f"{entry_id}.bin"
        normalized_path = self.root / "normalized" / f"{entry_id}.bin"
        metadata_path = self.root / "metadata" / f"{entry_id}.json"
        for path in (raw_path, normalized_path, metadata_path):
            path.parent.mkdir(parents=True, exist_ok=True)
        metadata = {
            "archive_schema_version": 1, "data_provider_version": DATA_PROVIDER_VERSION,
            "entry_id": entry_id, "requested_source": requested_source,
            "actual_source": result.source, "fallback_reason": fallback_reason,
            "source_selection_reason": source_selection_reason,
            "attempts": attempts, "endpoint": result.endpoint,
            "query_parameters": result.parameters, "retrieved_at_utc": stamp,
            "parser_version": result.parser_version,
            "raw_sha256": sha256(result.raw),
            "normalized_sha256": sha256(result.normalized),
            "raw_path": str(raw_path.relative_to(self.root)),
            "normalized_path": str(normalized_path.relative_to(self.root)),
        }
        with raw_path.open("xb") as handle:
            handle.write(result.raw)
        with normalized_path.open("xb") as handle:
            handle.write(result.normalized)
        with metadata_path.open("x", encoding="utf-8") as handle:
            json.dump(metadata, handle, ensure_ascii=False, indent=2)
        return metadata

    def verify(self, metadata: dict) -> bool:
        for kind in ("raw", "normalized"):
            path = self.root / metadata[f"{kind}_path"]
            if not path.is_file() or sha256(path.read_bytes()) != metadata[f"{kind}_sha256"]:
                return False
        return True


def fetch_with_fallback(requested_source: str, sources: list[tuple[str, Callable[[], SourceResult]]],
                        archive: PublicSourceArchive) -> dict:
    """Only the ordered source list may supply a value; every failed attempt is recorded."""
    if not sources or sources[0][0] != requested_source:
        raise ValueError("first source must be the requested source")
    attempts = []
    for name, fetch in sources:
        try:
            result = fetch()
            if result.source != name:
                raise ValueError("source identity mismatch")
            attempts.append({"source": name, "status": "used"})
            reason = "; ".join(f"{a['source']}: {a['reason']}" for a in attempts[:-1]) or None
            return archive.capture(result, requested_source=requested_source,
                                   fallback_reason=reason, attempts=attempts)
        except (OSError, ValueError, RuntimeError) as exc:
            attempts.append({"source": name, "status": "failed", "reason": str(exc)})
    raise RuntimeError(f"all public sources failed: {attempts}")
