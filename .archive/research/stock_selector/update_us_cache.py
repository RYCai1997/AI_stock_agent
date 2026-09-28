"""Safely identify or download missing SEC/Yahoo cache files for one US snapshot."""

from __future__ import annotations

import argparse
import json
import os
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import requests

from selector.providers.us_sec_yahoo import _load_universe


DURATION_TAGS = {
    "revenue": ("RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues", "SalesRevenueNet"),
    "operating_income": ("OperatingIncomeLoss",),
    "tax_expense": ("IncomeTaxExpenseBenefit",),
    "pretax_income": (
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments",
    ),
    "net_income": ("NetIncomeLoss", "ProfitLoss"),
    "cfo": ("NetCashProvidedByUsedInOperatingActivities",),
    "capex": ("PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsForAdditionsToPropertyPlantAndEquipment"),
    "eps": ("EarningsPerShareDiluted", "EarningsPerShareBasicAndDiluted"),
}
INSTANT_TAGS = {
    "equity": ("StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"),
    "cash": ("CashAndCashEquivalentsAtCarryingValue", "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"),
    "debt_noncurrent": ("LongTermDebtNoncurrent", "LongTermDebtAndFinanceLeaseObligationsNoncurrent"),
    "debt_current": ("LongTermDebtCurrent", "LongTermDebtAndFinanceLeaseObligationsCurrent", "ShortTermBorrowings"),
}


def _get_json(url: str, headers: dict[str, str] | None = None, attempts: int = 4) -> dict[str, Any]:
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            response = requests.get(url, headers=headers, timeout=60)
            if response.status_code in {429, 500, 502, 503, 504}:
                raise RuntimeError(f"temporary HTTP {response.status_code}")
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            last_error = exc
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"request failed after {attempts} attempts: {last_error}")


def _records(payload: dict[str, Any], namespace: str, tags: tuple[str, ...]) -> list[dict[str, Any]]:
    facts = payload.get("facts", {}).get(namespace, {})
    rows = []
    for priority, tag in enumerate(tags):
        for unit, values in facts.get(tag, {}).get("units", {}).items():
            if unit not in {"USD", "USD/shares", "shares"}:
                continue
            for value in values:
                row = dict(value)
                row.update({"tag": tag, "unit": unit, "priority": priority})
                rows.append(row)
    return rows


def _compact_facts(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "cik": payload.get("cik"),
        "entity_name": payload.get("entityName"),
        "duration": {name: _records(payload, "us-gaap", tags) for name, tags in DURATION_TAGS.items()},
        "instant": {
            **{name: _records(payload, "us-gaap", tags) for name, tags in INSTANT_TAGS.items()},
            "shares": _records(payload, "dei", ("EntityCommonStockSharesOutstanding",)),
        },
    }


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    temporary.replace(path)


def _fetch_sec(cik: int, fact_path: Path, submission_path: Path, user_agent: str) -> None:
    headers = {"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"}
    if not fact_path.exists():
        payload = _get_json(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json", headers)
        _atomic_json(fact_path, _compact_facts(payload))
        time.sleep(0.12)
    if not submission_path.exists():
        payload = _get_json(f"https://data.sec.gov/submissions/CIK{cik:010d}.json", headers)
        _atomic_json(submission_path, {
            "cik": payload.get("cik"),
            "name": payload.get("name"),
            "sic": payload.get("sic"),
            "sic_description": payload.get("sicDescription"),
        })
        time.sleep(0.12)


def _fetch_yahoo(ticker: str, start: str, end: str, path: Path) -> None:
    symbol = ticker.replace(".", "-")
    p1 = int(pd.Timestamp(start, tz="UTC").timestamp())
    p2 = int((pd.Timestamp(end, tz="UTC") + pd.Timedelta(days=1)).timestamp())
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        f"?period1={p1}&period2={p2}&interval=1d&events=div%2Csplits&includeAdjustedClose=true"
    )
    payload = _get_json(url)
    results = payload.get("chart", {}).get("result")
    if not results:
        raise RuntimeError(str(payload.get("chart", {}).get("error")))
    block = results[0]
    timestamps = block.get("timestamp") or []
    quote = (block.get("indicators", {}).get("quote") or [{}])[0]
    adjusted = (block.get("indicators", {}).get("adjclose") or [{}])[0].get("adjclose", [])
    closes = quote.get("close", [])
    volumes = quote.get("volume", [])
    if not (len(timestamps) == len(adjusted) == len(closes) == len(volumes)):
        raise RuntimeError("Yahoo timestamp/value length mismatch")
    frame = pd.DataFrame({
        "date": pd.to_datetime(timestamps, unit="s", utc=True).date,
        "close": closes,
        "adjusted_close": adjusted,
        "volume": volumes,
    })
    frame["split_ratio"] = 1.0
    for event in block.get("events", {}).get("splits", {}).values():
        split_date = pd.to_datetime(event["date"], unit="s", utc=True).date()
        ratio = float(event["numerator"]) / float(event["denominator"])
        frame.loc[frame["date"] == split_date, "split_ratio"] = ratio
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".csv.tmp")
    frame.dropna(subset=["date", "close", "adjusted_close"]).to_csv(temporary, index=False)
    temporary.replace(path)


def main() -> None:
    parser = argparse.ArgumentParser(description="Find or fetch missing US selector cache files")
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--history", required=True, type=Path)
    parser.add_argument("--ticker-map", required=True, type=Path)
    parser.add_argument("--fact-cache", required=True, type=Path)
    parser.add_argument("--submission-cache", required=True, type=Path)
    parser.add_argument("--price-cache", required=True, type=Path)
    parser.add_argument("--execute", action="store_true", help="Actually download; default is dry-run")
    parser.add_argument("--limit", type=int, default=0, help="Limit missing symbols for a controlled update")
    parser.add_argument("--sec-user-agent", default=os.environ.get("SEC_USER_AGENT", ""))
    args = parser.parse_args()

    _, members, ignored = _load_universe(args.history, args.ticker_map, args.as_of)
    missing = []
    for ticker, cik in members:
        needs = []
        if not (args.fact_cache / f"{cik:010d}.json").exists():
            needs.append("sec_facts")
        if not (args.submission_cache / f"{cik:010d}.json").exists():
            needs.append("sec_submission")
        if not (args.price_cache / f"{ticker}.csv").exists():
            needs.append("yahoo_price")
        if needs:
            missing.append({"ticker": ticker, "cik": cik, "needs": needs})
    if args.limit:
        missing = missing[: args.limit]
    result: dict[str, Any] = {
        "mode": "execute" if args.execute else "dry_run",
        "as_of": args.as_of,
        "mapped_members": len(members),
        "unmapped_symbols": ignored,
        "missing_symbols": missing,
        "updated": [],
        "errors": {},
    }
    if args.execute and any(any(item.startswith("sec_") for item in row["needs"]) for row in missing):
        if not args.sec_user_agent or "@" not in args.sec_user_agent:
            raise ValueError("SEC downloads require --sec-user-agent with a contact email")
    start = str((pd.Timestamp(args.as_of) - pd.Timedelta(days=800)).date())
    end = str(max(date.today(), pd.Timestamp(args.as_of).date()) + timedelta(days=2))
    if args.execute:
        for item in missing:
            ticker, cik = item["ticker"], item["cik"]
            try:
                if "sec_facts" in item["needs"] or "sec_submission" in item["needs"]:
                    _fetch_sec(
                        cik,
                        args.fact_cache / f"{cik:010d}.json",
                        args.submission_cache / f"{cik:010d}.json",
                        args.sec_user_agent,
                    )
                if "yahoo_price" in item["needs"]:
                    _fetch_yahoo(ticker, start, end, args.price_cache / f"{ticker}.csv")
                result["updated"].append(ticker)
            except Exception as exc:
                result["errors"][ticker] = str(exc)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

