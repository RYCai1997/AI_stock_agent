"""Fail-closed first-signal lookback audit for adjusted prices and filed EPS."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd


FIRST_SIGNAL_DATE = "2020-03-16"
PRICE_COLLECTION_START = "2018-01-01"
FUNDAMENTAL_COLLECTION_START = "2012-01-01"
MIN_EMA_SESSIONS = 250  # Covers the predeclared EMA150-250 neighborhood.
MIN_ANNUAL_EPS = 5      # Four growth observations for sample standard deviation.


def audit_warmup(prices: pd.DataFrame, filings: pd.DataFrame, tickers: list[str],
                 signal_date: str = FIRST_SIGNAL_DATE) -> pd.DataFrame:
    price_fields = {"ticker", "date", "close"}
    filing_fields = {"ticker", "statDate", "pubDate", "epsTTM"}
    if not price_fields <= set(prices) or not filing_fields <= set(filings):
        raise ValueError("warmup requires adjusted ticker,date,close and filed ticker,statDate,pubDate,epsTTM")
    if prices.duplicated(["ticker", "date"]).any() or filings.duplicated(["ticker", "statDate", "pubDate"]).any():
        raise ValueError("duplicate warmup source rows")
    node = pd.Timestamp(signal_date)
    prior_13m = node - pd.DateOffset(months=13)
    result = []
    for ticker in sorted(set(tickers)):
        p = prices[prices.ticker.eq(ticker)].copy()
        p["date"] = pd.to_datetime(p["date"], errors="coerce")
        p["close"] = pd.to_numeric(p["close"], errors="coerce")
        if p["date"].isna().any():
            raise ValueError(f"invalid price date for {ticker}")
        p = p[p.date.le(node)].sort_values("date")
        valid_prices = p[p.close.gt(0) & p.close.notna()]
        f = filings[filings.ticker.eq(ticker)].copy()
        f["statDate"] = pd.to_datetime(f["statDate"], errors="coerce")
        f["pubDate"] = pd.to_datetime(f["pubDate"], errors="coerce")
        f["epsTTM"] = pd.to_numeric(f["epsTTM"], errors="coerce")
        if f[["statDate", "pubDate"]].isna().any().any():
            raise ValueError(f"invalid filing date for {ticker}")
        f = f[f.pubDate.le(node) & f.statDate.le(node) & f.statDate.dt.month.eq(12)
              & f.statDate.dt.day.eq(31) & f.epsTTM.notna()]
        f = f.sort_values(["statDate", "pubDate"]).drop_duplicates("statDate", keep="last")
        reasons = []
        if len(valid_prices) < MIN_EMA_SESSIONS:
            reasons.append("fewer_than_250_adjusted_sessions")
        if not valid_prices.date.le(prior_13m).any():
            reasons.append("missing_13_month_momentum_anchor")
        if p.close.isna().any() or p.close.le(0).any():
            reasons.append("invalid_adjusted_close")
        if len(f) < MIN_ANNUAL_EPS:
            reasons.append("fewer_than_five_published_annual_eps")
        result.append({"ticker": ticker, "signal_date": signal_date,
                       "adjusted_sessions": len(valid_prices),
                       "first_adjusted_date": str(valid_prices.date.min().date()) if len(valid_prices) else None,
                       "annual_eps_count": len(f),
                       "oldest_annual_statDate": str(f.statDate.min().date()) if len(f) else None,
                       "status": "ready" if not reasons else "fail_closed",
                       "reasons": ";".join(reasons)})
    return pd.DataFrame(result)


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit first V1 signal's technical and EPS warmup")
    parser.add_argument("--adjusted-prices", type=Path, required=True)
    parser.add_argument("--filings", type=Path, required=True)
    parser.add_argument("--members", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    members = pd.read_csv(args.members, dtype={"ticker": str})
    if "ticker" not in members:
        parser.error("members CSV requires ticker")
    result = audit_warmup(pd.read_csv(args.adjusted_prices, dtype={"ticker": str}),
                          pd.read_csv(args.filings, dtype={"ticker": str}),
                          members.ticker.tolist())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    result.to_csv(args.output, index=False)
    print(json.dumps({"signal_date": FIRST_SIGNAL_DATE, "members": len(result),
                      "ready": int(result.status.eq("ready").sum()),
                      "fail_closed": int(result.status.eq("fail_closed").sum())}))


if __name__ == "__main__":
    main()
