"""Minimal historical provider contract shared by source adapters."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class CachedResponse:
    source: str
    endpoint: str
    parameters: dict
    fields: tuple[str, ...]
    rows: tuple[dict, ...]
    metadata: dict


class HistoricalProvider(Protocol):
    def fetch(self, endpoint: str, parameters: dict, fields: tuple[str, ...] = (),
              *, offline: bool = False) -> CachedResponse: ...
