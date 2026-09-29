"""Frozen, mechanical monthly signal calendar from dated market sessions."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from datetime import date
from pathlib import Path

RULE = "first_market_session_on_or_after_15th"
SOURCE = "Baostock query_trade_dates"


def generate_signal_calendar(trading_dates: list[str], first_month: str,
                             last_month: str) -> list[dict[str, str]]:
    """Select one close-of-session signal date per month, without price inputs."""
    if first_month > last_month:
        raise ValueError("first_month must be no later than last_month")
    sessions = sorted(set(trading_dates))
    if len(sessions) != len(trading_dates):
        raise ValueError("duplicate market trading dates")
    for session in sessions:
        if date.fromisoformat(session).isoformat() != session:
            raise ValueError(f"invalid trading date: {session}")
    result = []
    year, month = map(int, first_month.split("-"))
    end_year, end_month = map(int, last_month.split("-"))
    while (year, month) <= (end_year, end_month):
        label = f"{year:04d}-{month:02d}"
        eligible = [day for day in sessions if day.startswith(label) and day[8:] >= "15"]
        if not eligible:
            raise ValueError(f"no eligible market session for {label}")
        result.append({"month": label, "signal_date": eligible[0]})
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return result


def load_baostock_trade_dates(raw_csv: Path) -> list[str]:
    with raw_csv.open(newline="", encoding="utf-8") as stream:
        reader = csv.DictReader(stream)
        if not {"calendar_date", "is_trading_day"} <= set(reader.fieldnames or []):
            raise ValueError("Baostock calendar requires calendar_date,is_trading_day")
        rows = list(reader)
    if not rows:
        raise ValueError("empty market calendar")
    if any(row["is_trading_day"] not in {"0", "1"} for row in rows):
        raise ValueError("unexpected trading-day flag")
    return [row["calendar_date"] for row in rows if row["is_trading_day"] == "1"]


def main() -> None:
    parser = argparse.ArgumentParser(description="Build frozen historical signal calendar offline")
    parser.add_argument("--source-csv", type=Path, required=True)
    parser.add_argument("--first-month", default="2020-03")
    parser.add_argument("--last-month", default="2025-07")
    parser.add_argument("--output-csv", type=Path, required=True)
    parser.add_argument("--market-sessions-csv", type=Path)
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()
    raw = args.source_csv.read_bytes()
    sessions = load_baostock_trade_dates(args.source_csv)
    rows = generate_signal_calendar(sessions,
                                    args.first_month, args.last_month)
    args.output_csv.parent.mkdir(parents=True, exist_ok=True)
    with args.output_csv.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=["month", "signal_date"])
        writer.writeheader()
        writer.writerows(rows)
    manifest = {"rule": RULE, "source": SOURCE, "source_sha256": hashlib.sha256(raw).hexdigest(),
                "first_month": args.first_month, "last_month": args.last_month,
                "signal_months": len(rows), "calendar_sha256": hashlib.sha256(args.output_csv.read_bytes()).hexdigest()}
    if args.market_sessions_csv:
        args.market_sessions_csv.parent.mkdir(parents=True, exist_ok=True)
        with args.market_sessions_csv.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.writer(stream)
            writer.writerow(["date"])
            writer.writerows([[day] for day in sessions if args.first_month <= day[:7] <= args.last_month])
        manifest["market_sessions_sha256"] = hashlib.sha256(args.market_sessions_csv.read_bytes()).hexdigest()
        manifest["market_sessions"] = sum(args.first_month <= day[:7] <= args.last_month for day in sessions)
    args.manifest.parent.mkdir(parents=True, exist_ok=True)
    args.manifest.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False))


if __name__ == "__main__":
    main()
