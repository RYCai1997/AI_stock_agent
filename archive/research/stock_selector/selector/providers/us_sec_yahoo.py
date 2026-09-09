"""Build standardized US metrics from historical S&P, SEC and Yahoo caches.

The provider is deliberately read-only. It never substitutes current facts for
missing historical facts and never uses a filing whose filed date is after the
requested snapshot.
"""

from __future__ import annotations

import csv
import json
import math
import statistics
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd


ANNUAL_FORMS = {"10-K", "10-K/A", "20-F", "20-F/A", "40-F", "40-F/A"}
SIC_DIVISIONS = {
    "0": "0 Agriculture",
    "1": "1 Mining and Construction",
    "2": "2 Manufacturing",
    "3": "3 Manufacturing",
    "4": "4 Transportation and Utilities",
    "5": "5 Wholesale and Retail",
    "6": "6 Financials",
    "7": "7 Services",
    "8": "8 Services",
    "9": "9 Public Administration",
}


def _annual_records(rows: list[dict[str, Any]], as_of: str) -> dict[str, dict[str, Any]]:
    eligible = []
    for row in rows:
        if row.get("form") not in ANNUAL_FORMS:
            continue
        if not row.get("start") or not row.get("end") or not row.get("filed"):
            continue
        if row["filed"] > as_of or row["end"] > as_of:
            continue
        days = (pd.Timestamp(row["end"]) - pd.Timestamp(row["start"])).days
        if 250 <= days <= 450 and isinstance(row.get("val"), (int, float)):
            eligible.append(row)
    chosen: dict[str, dict[str, Any]] = {}
    for row in sorted(eligible, key=lambda item: (item["end"], item["filed"], -item["priority"])):
        current = chosen.get(row["end"])
        if current is None or (row["filed"], -row["priority"]) >= (current["filed"], -current["priority"]):
            chosen[row["end"]] = row
    return chosen


def _instant_record(
    rows: list[dict[str, Any]], as_of: str, target_end: str | None = None
) -> dict[str, Any] | None:
    eligible = []
    for row in rows:
        if not row.get("end") or not row.get("filed"):
            continue
        if row["filed"] > as_of or row["end"] > as_of:
            continue
        if target_end and abs((pd.Timestamp(row["end"]) - pd.Timestamp(target_end)).days) > 10:
            continue
        if isinstance(row.get("val"), (int, float)):
            eligible.append(row)
    return max(eligible, key=lambda item: (item["end"], item["filed"], -item["priority"])) if eligible else None


def _common_fiscal_end(series: dict[str, dict[str, dict[str, Any]]], required: Iterable[str]) -> str | None:
    names = tuple(required)
    key_sets = [set(series[name]) for name in names if series.get(name)]
    if len(key_sets) != len(names):
        return None
    common = set.intersection(*key_sets)
    return max(common) if common else None


def _at_or_before(series: pd.Series, when: pd.Timestamp) -> float | None:
    eligible = series.loc[series.index <= when].dropna()
    return float(eligible.iloc[-1]) if not eligible.empty else None


def _load_price(path: Path) -> pd.DataFrame:
    frame = pd.read_csv(path, parse_dates=["date"])
    required = {"date", "close", "adjusted_close", "split_ratio"}
    if not required.issubset(frame.columns):
        raise ValueError(f"price cache missing columns {sorted(required - set(frame.columns))}")
    frame = frame.drop_duplicates("date", keep="last").sort_values("date").set_index("date")
    return frame


def _ticker_metrics(
    ticker: str,
    cik: int,
    as_of: str,
    membership_date: str,
    fact_cache: Path,
    submission_cache: Path,
    price_cache: Path,
) -> dict[str, Any]:
    fact = json.loads((fact_cache / f"{cik:010d}.json").read_text(encoding="utf-8"))
    submission = json.loads((submission_cache / f"{cik:010d}.json").read_text(encoding="utf-8"))
    duration = {name: _annual_records(rows, as_of) for name, rows in fact["duration"].items()}
    fiscal_end = _common_fiscal_end(duration, ("revenue", "net_income", "cfo", "capex"))
    if not fiscal_end:
        return {"ticker": ticker, "error": "no common annual fiscal period"}

    chosen = {name: records.get(fiscal_end) for name, records in duration.items()}
    revenue = chosen["revenue"]["val"]
    net_income = chosen["net_income"]["val"]
    cfo = chosen["cfo"]["val"]
    capex = chosen["capex"]["val"]
    op_income = chosen.get("operating_income")
    pretax = chosen.get("pretax_income")
    tax = chosen.get("tax_expense")

    instant = fact["instant"]
    equity_row = _instant_record(instant["equity"], as_of, fiscal_end)
    cash_row = _instant_record(instant["cash"], as_of, fiscal_end)
    debt_long_row = _instant_record(instant["debt_noncurrent"], as_of, fiscal_end)
    debt_current_row = _instant_record(instant["debt_current"], as_of, fiscal_end)
    shares_row = _instant_record(instant["shares"], as_of)
    equity = float(equity_row["val"]) if equity_row else None
    cash = float(cash_row["val"]) if cash_row else 0.0
    debt_long = float(debt_long_row["val"]) if debt_long_row else 0.0
    debt_current = float(debt_current_row["val"]) if debt_current_row else 0.0

    full_prices = _load_price(price_cache / f"{ticker}.csv")
    prices = full_prices.loc[full_prices.index <= pd.Timestamp(as_of)].copy()
    if prices.empty:
        return {"ticker": ticker, "error": "no price on or before snapshot"}
    node = pd.Timestamp(as_of)
    close = _at_or_before(prices["close"], node)
    adjusted = prices["adjusted_close"].dropna()
    adjusted_price = _at_or_before(adjusted, node)
    p1 = _at_or_before(adjusted, node - pd.DateOffset(months=1))
    p7 = _at_or_before(adjusted, node - pd.DateOffset(months=7))
    p13 = _at_or_before(adjusted, node - pd.DateOffset(months=13))

    shares_reported = float(shares_row["val"]) if shares_row else None
    split_factor = 1.0
    if shares_row:
        # Yahoo back-adjusts historical Close for every split present in the downloaded
        # series. Apply the same complete-cache split basis to SEC historical shares.
        # Later split events are unit normalization only; they are not an investment signal.
        later_splits = full_prices.loc[
            full_prices.index > pd.Timestamp(shares_row["end"]), "split_ratio"
        ].dropna()
        split_factor = float(later_splits.prod()) if not later_splits.empty else 1.0
    shares = shares_reported * split_factor if shares_reported is not None else None
    market_cap = close * shares if close is not None and shares not in {None, 0} else None

    fcf = float(cfo) - float(capex)
    tax_rate = 0.21
    if pretax and pretax["val"] > 0 and tax:
        tax_rate = min(0.35, max(0.0, float(tax["val"]) / float(pretax["val"])))
    invested_capital = equity + debt_long + debt_current - cash if equity is not None else None
    roic = None
    if op_income and invested_capital and invested_capital > 0:
        roic = float(op_income["val"]) * (1.0 - tax_rate) / invested_capital

    eps_records = [row for end, row in sorted(duration["eps"].items()) if end <= fiscal_end][-6:]
    eps_growth = []
    for previous, current in zip(eps_records, eps_records[1:]):
        denominator = max(abs(float(previous["val"])), 0.01)
        eps_growth.append(max(-5.0, min(5.0, (float(current["val"]) - float(previous["val"])) / denominator)))
    eps_growth_std = statistics.stdev(eps_growth) if len(eps_growth) >= 4 else None

    daily_returns = adjusted.pct_change(fill_method=None).dropna()
    volatility = float(daily_returns.tail(252).std(ddof=1) * math.sqrt(252)) if len(daily_returns) >= 60 else None
    recent = adjusted.tail(126)
    drawdown = recent / recent.cummax() - 1.0
    max_drawdown = float(drawdown.min()) if not drawdown.empty else None
    ema200 = float(adjusted.ewm(span=200, adjust=False, min_periods=200).mean().iloc[-1]) if len(adjusted) >= 200 else None

    filing_dates = [row["filed"] for row in chosen.values() if row]
    filing_dates += [row["filed"] for row in (equity_row, cash_row, debt_long_row, debt_current_row, shares_row) if row]
    sic = str(submission.get("sic") or "").zfill(4)
    return {
        "market": "US",
        "ticker": ticker,
        "company": fact.get("entity_name") or submission.get("name") or ticker,
        "industry_l1": SIC_DIVISIONS.get(sic[:1], "Unknown"),
        "industry_l2": f"SIC-{sic[:2]}",
        "industry_description": submission.get("sic_description"),
        "sic": sic,
        "universe_as_of": membership_date,
        "fundamental_as_of": max(filing_dates),
        "fundamental_age_days": (pd.Timestamp(as_of) - pd.Timestamp(max(filing_dates))).days,
        "price_as_of": str(adjusted.index[-1].date()),
        "fiscal_end": fiscal_end,
        "roic": roic,
        "fcf_margin": fcf / float(revenue) if revenue else None,
        "eps_growth_std": eps_growth_std,
        "earnings_yield": float(net_income) / market_cap if market_cap else None,
        "fcf_yield": fcf / market_cap if market_cap else None,
        "book_to_price": equity / market_cap if equity is not None and market_cap else None,
        "mom_6_1": p1 / p7 - 1.0 if p1 and p7 else None,
        "mom_12_1": p1 / p13 - 1.0 if p1 and p13 else None,
        "relative_strength": np.nan,
        "price": adjusted_price,
        "ema200": ema200,
        "volatility_1y": volatility,
        "max_drawdown_6m": max_drawdown,
        "avg_daily_turnover": np.nan,
        "shares_basis_date": shares_row["end"] if shares_row else None,
        "split_factor_to_snapshot": split_factor,
        "market_cap": market_cap,
    }


def _load_universe(history_path: Path, ticker_map_path: Path, as_of: str) -> tuple[str, list[tuple[str, int]], list[str]]:
    with history_path.open(encoding="utf-8", newline="") as handle:
        rows = sorted(csv.DictReader(handle), key=lambda row: row["date"])
    eligible = [row for row in rows if row["date"] <= as_of]
    if not eligible:
        raise ValueError(f"no historical S&P membership on or before {as_of}")
    source = eligible[-1]
    raw_map = json.loads(ticker_map_path.read_text(encoding="utf-8"))
    ticker_map = {item["ticker"]: int(item["cik_str"]) for item in raw_map.values()}
    ticker_map.update({ticker.replace("-", "."): cik for ticker, cik in list(ticker_map.items()) if "-" in ticker})
    tickers = source["tickers"].split(",")
    mapped = [(ticker, ticker_map[ticker]) for ticker in tickers if ticker in ticker_map]
    ignored = [ticker for ticker in tickers if ticker not in ticker_map]
    return source["date"], mapped, ignored


def _benchmark_trend(price_cache: Path, as_of: str) -> tuple[str, dict[str, Any]]:
    spy_frame = _load_price(price_cache / "SPY.csv")
    spy = spy_frame.loc[spy_frame.index <= pd.Timestamp(as_of), "adjusted_close"].dropna()
    ema = spy.ewm(span=200, adjust=False, min_periods=200).mean()
    if spy.empty or ema.empty or pd.isna(ema.iloc[-1]):
        return "unknown", {"benchmark": "SPY", "reason": "insufficient price history"}
    status = "up" if spy.iloc[-1] > ema.iloc[-1] else "down"
    return status, {
        "benchmark": "SPY",
        "price_as_of": str(spy.index[-1].date()),
        "adjusted_close": float(spy.iloc[-1]),
        "ema200": float(ema.iloc[-1]),
    }


def build_us_metrics(
    as_of: str,
    history_path: Path,
    ticker_map_path: Path,
    fact_cache: Path,
    submission_cache: Path,
    price_cache: Path,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Return standardized rows and an audit summary for one historical US snapshot."""
    membership_date, members, ignored = _load_universe(history_path, ticker_map_path, as_of)
    rows = []
    errors: dict[str, str] = {}
    for ticker, cik in members:
        required = [
            fact_cache / f"{cik:010d}.json",
            submission_cache / f"{cik:010d}.json",
            price_cache / f"{ticker}.csv",
        ]
        if not all(path.exists() for path in required):
            errors[ticker] = "missing one or more local SEC/Yahoo cache files"
            continue
        try:
            item = _ticker_metrics(
                ticker, cik, as_of, membership_date, fact_cache, submission_cache, price_cache
            )
            if item.get("error"):
                errors[ticker] = item["error"]
            else:
                rows.append(item)
        except Exception as exc:  # retain per-company failure without substituting data
            errors[ticker] = str(exc)
    frame = pd.DataFrame(rows)
    if frame.empty:
        raise ValueError("no US metrics could be built from the supplied caches")
    group = frame["industry_l2"].where(
        frame.groupby("industry_l2")["ticker"].transform("size") >= 5,
        frame["industry_l1"],
    )
    frame["relative_strength"] = frame["mom_12_1"] - frame.groupby(group)["mom_12_1"].transform("median")
    market_trend, benchmark = _benchmark_trend(price_cache, as_of)
    metadata = {
        "market": "US",
        "as_of": as_of,
        "membership_snapshot": membership_date,
        "original_members": len(members) + len(ignored),
        "mapped_members": len(members),
        "built_rows": len(frame),
        "ignored_unmapped_symbols": ignored,
        "errors": errors,
        "market_trend": market_trend,
        "benchmark": benchmark,
        "source_policy": "local historical S&P membership; SEC facts filtered by filed date; Yahoo cache truncated at as_of",
    }
    return frame, metadata
