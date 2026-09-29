"""Collect public, unadjusted daily prices as auditable offline research inputs.

Source: Eastmoney push2his kline endpoint. This collector does not infer missing
sessions, corporate actions, ST flags, or exchange limit prices.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import time
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pandas as pd

ENDPOINT = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
FIELDS = "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"


def secid_for(ticker: str) -> str:
    if len(ticker) != 9 or ticker[2] != "." or not ticker[3:].isdigit():
        raise ValueError(f"unsupported ticker: {ticker}")
    market = {"sh": "1", "sz": "0"}.get(ticker[:2])
    if market is None:
        raise ValueError(f"unsupported ticker: {ticker}")
    return f"{market}.{ticker[3:]}"


def parse_klines(payload: dict, ticker: str, start: str, end: str) -> list[dict]:
    data = payload.get("data")
    if not isinstance(data, dict) or data.get("code") != ticker[3:]:
        raise ValueError(f"missing or mismatched public price response for {ticker}")
    lines = data.get("klines")
    if not isinstance(lines, list) or not lines:
        raise ValueError(f"no daily prices for {ticker}")
    rows = []
    prior = ""
    for line in lines:
        fields = line.split(",")
        if len(fields) < 5:
            raise ValueError(f"short daily price row for {ticker}")
        date = fields[0]
        if not start <= date <= end or date <= prior:
            raise ValueError(f"out-of-range or unordered daily price for {ticker}: {date}")
        stamp = pd.Timestamp(date)
        if str(stamp.date()) != date:
            raise ValueError(f"invalid date for {ticker}: {date}")
        op, close, high, low = (float(value) for value in fields[1:5])
        if low <= 0 or not low <= min(op, close) <= max(op, close) <= high:
            raise ValueError(f"invalid OHLC for {ticker}: {date}")
        rows.append({"date": date, "ticker": ticker, "open": op,
                     "high": high, "low": low, "close": close, "tradable": True})
        prior = date
    return rows


def fetch_one(ticker: str, start: str, end: str) -> bytes:
    query = urlencode({"secid": secid_for(ticker), "fields1": "f1,f2,f3,f4,f5,f6",
                       "fields2": FIELDS, "klt": "101", "fqt": "0",
                       "beg": start.replace("-", ""), "end": end.replace("-", "")})
    request = Request(f"{ENDPOINT}?{query}", headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(request, timeout=20) as response:
        return response.read()


def output_name(stem: str, complete: int, requested: int) -> str:
    return f"{stem}.csv" if complete == requested else f"{stem}.partial.csv"


def main() -> None:
    parser = argparse.ArgumentParser(description="Collect public unadjusted daily research prices")
    parser.add_argument("--snapshots-dir", type=Path, required=True)
    parser.add_argument("--start", default="2020-03-16")
    parser.add_argument("--end", default="2025-08-15")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--offline", action="store_true", help="Rebuild coverage from saved responses without new requests")
    args = parser.parse_args()
    if args.start > args.end:
        parser.error("start must be no later than end")
    files = sorted(args.snapshots_dir.glob("*/raw_metrics.csv"))
    if not files:
        parser.error("no official snapshot metrics found")
    tickers = sorted(set().union(*(set(pd.read_csv(path, usecols=["ticker"])["ticker"])
                                   for path in files)))
    tickers.append("sh.000300")
    args.output.mkdir(parents=True, exist_ok=True)
    cache = args.output / "responses"
    cache.mkdir(exist_ok=True)
    rows = []
    audit = []
    for index, ticker in enumerate(tickers, 1):
        target = cache / f"{ticker}_{args.start}_{args.end}.json"
        try:
            if target.exists():
                raw = target.read_bytes()
                source = "cache"
            else:
                if args.offline:
                    raise FileNotFoundError("public response not cached")
                raw = fetch_one(ticker, args.start, args.end)
                # Validate before placing an external response in the resumable cache.
                parse_klines(json.loads(raw), ticker, args.start, args.end)
                target.write_bytes(raw)
                source = "network"
            parsed = parse_klines(json.loads(raw), ticker, args.start, args.end)
            rows.extend(parsed)
            audit.append({"ticker": ticker, "status": "ok", "rows": len(parsed),
                          "first_date": parsed[0]["date"], "last_date": parsed[-1]["date"],
                          "sha256": hashlib.sha256(raw).hexdigest(), "source": source})
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            audit.append({"ticker": ticker, "status": "error", "error": str(exc)})
        if index % 25 == 0 or index == len(tickers):
            print(f"{index}/{len(tickers)} public price series checked", flush=True)
        if not target.exists() and not args.offline:
            time.sleep(0.2)
    complete = sum(a["status"] == "ok" for a in audit)
    fields = ["date", "ticker", "open", "high", "low", "close", "tradable"]
    for stem, selected in (("bars", [r for r in rows if r["ticker"] != "sh.000300"]),
                           ("benchmark_raw", [r for r in rows if r["ticker"] == "sh.000300"])):
        stale = args.output / (f"{stem}.partial.csv" if complete == len(tickers) else f"{stem}.csv")
        if stale.exists():
            stale.unlink()
        with (args.output / output_name(stem, complete, len(tickers))).open(
                "w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            writer.writerows(sorted(selected, key=lambda row: (row["date"], row["ticker"])))
    report = {"source": ENDPOINT, "fqt": 0, "price_basis": "unadjusted",
              "requested_start": args.start, "requested_end": args.end,
              "requested_tickers": len(tickers), "complete_tickers": complete,
              "collection_complete": complete == len(tickers),
              "series": audit, "limitations": ["No missing-session interpolation", "No corporate-action audit",
                                               "No verified ST or price-limit flags"]}
    (args.output / "source_audit.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "series"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
