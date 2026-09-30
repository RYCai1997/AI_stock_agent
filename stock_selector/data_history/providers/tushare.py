"""Tushare Pro raw-response adapter; no token is written to disk or logs."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.request import Request, urlopen

from .base import CachedResponse


class MissingCredentials(RuntimeError):
    """The requested historical endpoint cannot be fetched without a token."""


class ProviderResponseError(RuntimeError):
    """The provider returned a failed or malformed response."""


Transport = Callable[[bytes], bytes]


def _network_transport(body: bytes) -> bytes:
    request = Request("https://api.tushare.pro", data=body,
                      headers={"Content-Type": "application/json", "User-Agent": "AI-stock-agent-PIT/1"},
                      method="POST")
    with urlopen(request, timeout=30) as response:
        return response.read()


def _canonical_request(endpoint: str, parameters: dict, fields: tuple[str, ...]) -> bytes:
    if not endpoint or not endpoint.replace("_", "").isalnum():
        raise ValueError("invalid Tushare endpoint")
    if not isinstance(parameters, dict) or any(str(key).lower() == "token" for key in parameters):
        raise ValueError("token must not appear in cached request parameters")
    return json.dumps({"endpoint": endpoint, "parameters": parameters,
                       "fields": list(fields)}, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8")


def _decode_rows(raw: bytes) -> tuple[dict, ...]:
    try:
        response = json.loads(raw)
        if response.get("code") != 0:
            raise ProviderResponseError(f"Tushare endpoint failed: code={response.get('code')}; "
                                        "check token, endpoint permission and quota")
        data = response["data"]
        fields, items = data["fields"], data["items"]
        if not isinstance(fields, list) or len(set(fields)) != len(fields) or not isinstance(items, list):
            raise ValueError("invalid Tushare table")
        if any(not isinstance(row, list) or len(row) != len(fields) for row in items):
            raise ValueError("Tushare row length does not match fields")
        return tuple(dict(zip(fields, row)) for row in items)
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise ProviderResponseError("malformed Tushare response") from exc


class TushareProProvider:
    def __init__(self, cache_dir: Path, *, token: str | None = None,
                 transport: Transport | None = None):
        self.cache_dir = Path(cache_dir)
        self._token = token if token is not None else (os.getenv("TUSHARE_TOKEN") or
                                                       os.getenv("TUSHARE_PRO_TOKEN"))
        self._transport = transport or _network_transport

    @property
    def has_credentials(self) -> bool:
        return bool(self._token)

    def fetch(self, endpoint: str, parameters: dict, fields: tuple[str, ...] = (),
              *, offline: bool = False) -> CachedResponse:
        request = _canonical_request(endpoint, parameters, fields)
        key = hashlib.sha256(request).hexdigest()
        raw_path = self.cache_dir / "raw" / f"{key}.json"
        meta_path = self.cache_dir / "metadata" / f"{key}.json"
        if raw_path.exists() or meta_path.exists():
            if not raw_path.is_file() or not meta_path.is_file():
                raise ProviderResponseError("incomplete cached Tushare response")
            raw = raw_path.read_bytes()
            metadata = json.loads(meta_path.read_text(encoding="utf-8"))
            if (metadata.get("request_sha256") != key or
                    metadata.get("raw_sha256") != hashlib.sha256(raw).hexdigest() or
                    metadata.get("endpoint") != endpoint or
                    metadata.get("parameters") != parameters or
                    metadata.get("fields") != list(fields)):
                raise ProviderResponseError("cached Tushare response integrity mismatch")
            rows = _decode_rows(raw)
            if metadata.get("row_count") != len(rows):
                raise ProviderResponseError("cached Tushare row count mismatch")
            return CachedResponse("tushare_pro", endpoint, dict(parameters), fields, rows, metadata)
        if offline:
            raise FileNotFoundError(f"uncached Tushare request: {endpoint} {key}")
        if not self._token:
            raise MissingCredentials("blocked_by_missing_data_credentials: set TUSHARE_TOKEN")
        body = json.dumps({"api_name": endpoint, "token": self._token,
                           "params": parameters, "fields": ",".join(fields)},
                          ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        raw = self._transport(body)
        rows = _decode_rows(raw)
        metadata = {"source": "tushare_pro", "endpoint": endpoint,
                    "parameters": parameters, "fields": list(fields),
                    "request_sha256": key, "raw_sha256": hashlib.sha256(raw).hexdigest(),
                    "retrieved_at_utc": datetime.now(timezone.utc).isoformat(),
                    "row_count": len(rows), "coverage_status": "response_only_not_audited"}
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        raw_path.write_bytes(raw)
        meta_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
                             encoding="utf-8")
        return CachedResponse("tushare_pro", endpoint, dict(parameters), fields, rows, metadata)

    def fetch_all(self, endpoint: str, parameters: dict, fields: tuple[str, ...] = (),
                  *, page_size: int = 5000, max_pages: int = 100, offline: bool = False) -> tuple[CachedResponse, ...]:
        if page_size < 1 or max_pages < 1 or "offset" in parameters or "limit" in parameters:
            raise ValueError("invalid pagination parameters")
        pages = []
        for index in range(max_pages):
            page = self.fetch(endpoint, {**parameters, "offset": index * page_size,
                                         "limit": page_size}, fields, offline=offline)
            pages.append(page)
            if len(page.rows) < page_size:
                return tuple(pages)
        raise ProviderResponseError("pagination reached max_pages; coverage cannot be certified")
