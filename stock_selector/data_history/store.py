"""Fail-closed contracts for locally cached point-in-time history."""

from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

from collect_public_bars import parse_klines


def disclosures_available(records: list[dict], signal_date: str) -> list[dict]:
    """Never expose a report merely because its accounting period has ended."""
    available = []
    for record in records:
        if not record.get("statDate") or not record.get("pubDate"):
            raise ValueError("financial record requires statDate and pubDate")
        if record["pubDate"] <= signal_date:
            available.append(record)
    return available


def normalize_eastmoney_response(raw: bytes, ticker: str, start: str, end: str) -> list[dict]:
    payload = json.loads(raw)
    checked = parse_klines(payload, ticker, start, end)
    lines = payload["data"]["klines"]
    rows = []
    for checked_row, line in zip(checked, lines):
        fields = line.split(",")
        if len(fields) < 7:
            raise ValueError(f"missing volume or amount for {ticker}")
        volume, amount = float(fields[5]), float(fields[6])
        if volume < 0 or amount < 0:
            raise ValueError(f"negative volume or amount for {ticker}")
        rows.append({**checked_row, "volume": volume, "amount": amount,
                     "volume_unit": "Eastmoney hands", "price_basis": "unadjusted",
                     "security_state_source": "returned_bar_only"})
    return rows


def ingest_public_price_cache(source_audit: Path, responses_dir: Path, output_dir: Path) -> dict:
    """Normalize saved original responses once per ticker; expose all coverage gaps."""
    audit = json.loads(source_audit.read_text(encoding="utf-8"))
    if audit.get("fqt") != 0 or audit.get("price_basis") != "unadjusted":
        raise ValueError("source audit does not identify unadjusted prices")
    start, end = audit["requested_start"], audit["requested_end"]
    series = audit["series"]
    if len(series) != audit["requested_tickers"] or len({x["ticker"] for x in series}) != len(series):
        raise ValueError("source audit ticker coverage is inconsistent")
    output_dir.mkdir(parents=True, exist_ok=True)
    included = []
    errors = {}
    for item in series:
        ticker = item["ticker"]
        if item["status"] != "ok":
            errors[ticker] = item.get("error", "source did not return prices")
            continue
        path = responses_dir / f"{ticker}_{start}_{end}.json"
        try:
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != item["sha256"]:
                raise ValueError("raw response hash mismatch")
            rows = normalize_eastmoney_response(raw, ticker, start, end)
            if len(rows) != item["rows"] or rows[0]["date"] != item["first_date"] or rows[-1]["date"] != item["last_date"]:
                raise ValueError("source audit row coverage mismatch")
            target = output_dir / f"{ticker}.csv"
            with target.open("w", newline="", encoding="utf-8") as stream:
                writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)
            included.append(ticker)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            errors[ticker] = str(exc)
    manifest = {"source": audit["source"], "start": start, "end": end,
                "requested_tickers": len(series), "unadjusted_tickers": len(included),
                "unadjusted_complete": len(included) == len(series),
                "adjusted_indicator_prices_complete": False,
                "fundamentals_complete": False, "universe_complete": False,
                "industry_history_complete": False, "security_state_complete": False,
                "missing_or_invalid": errors, "usable_for_full_v1": False}
    (output_dir.parent / "pit_store_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest
