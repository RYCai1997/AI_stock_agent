"""Price inputs for ETF research. Network data are cached with source metadata."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests


YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"


def _timestamps_to_exchange_dates(timestamps: list[int], exchange_timezone: str) -> pd.DatetimeIndex:
    """Convert Yahoo UTC timestamps to the calendar dates of their own exchange."""
    return (
        pd.to_datetime(timestamps, unit="s", utc=True)
        .tz_convert(exchange_timezone)
        .tz_localize(None)
        .normalize()
    )


def _unix_seconds(value: str, end_of_day: bool = False) -> int:
    stamp = pd.Timestamp(value, tz="UTC")
    if end_of_day:
        stamp += pd.Timedelta(days=1)
    return int(stamp.timestamp())


def fetch_yahoo_adjusted_close(symbol: str, start: str, end: str, timeout: int = 30) -> tuple[pd.Series, dict]:
    """Fetch daily adjusted closes from Yahoo's chart endpoint."""
    url = YAHOO_CHART_URL.format(symbol=symbol)
    params = {
        "period1": _unix_seconds(start),
        "period2": _unix_seconds(end, end_of_day=True),
        "interval": "1d",
        "events": "div,splits",
        "includeAdjustedClose": "true",
    }
    response = requests.get(url, params=params, headers={"User-Agent": "Mozilla/5.0"}, timeout=timeout)
    response.raise_for_status()
    payload = response.json()
    result = payload.get("chart", {}).get("result")
    if not result:
        error = payload.get("chart", {}).get("error")
        raise RuntimeError(f"Yahoo returned no data for {symbol}: {error}")
    block = result[0]
    timestamps = block.get("timestamp") or []
    indicators = block.get("indicators", {})
    adjusted_blocks = indicators.get("adjclose") or []
    if adjusted_blocks:
        values = adjusted_blocks[0].get("adjclose") or []
        field = "adjclose"
    else:
        quotes = indicators.get("quote") or []
        values = quotes[0].get("close") if quotes else []
        field = "close_fallback"
    if len(timestamps) != len(values) or not timestamps:
        raise RuntimeError(f"Yahoo returned malformed data for {symbol}")
    exchange_timezone = block.get("meta", {}).get("exchangeTimezoneName") or "UTC"
    index = _timestamps_to_exchange_dates(timestamps, exchange_timezone)
    series = pd.Series(values, index=index, name=symbol, dtype=float).dropna()
    metadata = {
        "symbol": symbol,
        "source": "Yahoo Finance chart API",
        "source_url": response.url,
        "price_field": field,
        "exchange_timezone": exchange_timezone,
        "fetched_at_utc": datetime.now(timezone.utc).isoformat(),
        "first_date": str(series.index.min().date()),
        "last_date": str(series.index.max().date()),
        "rows": int(len(series)),
    }
    return series, metadata


def fetch_price_panel(symbols: list[str], start: str, end: str) -> tuple[pd.DataFrame, list[dict]]:
    series_list: list[pd.Series] = []
    metadata: list[dict] = []
    for symbol in symbols:
        series, item = fetch_yahoo_adjusted_close(symbol, start, end)
        series_list.append(series)
        metadata.append(item)
    return pd.concat(series_list, axis=1).sort_index(), metadata


def load_price_directory(path: str | Path, symbols: list[str]) -> tuple[pd.DataFrame, list[dict]]:
    """Load one CSV per symbol; each file requires date and adjusted_close columns."""
    root = Path(path)
    series_list: list[pd.Series] = []
    metadata: list[dict] = []
    for symbol in symbols:
        source = root / f"{symbol}.csv"
        frame = pd.read_csv(source)
        required = {"date", "adjusted_close"}
        if not required.issubset(frame.columns):
            raise ValueError(f"{source} must contain columns: date, adjusted_close")
        series = pd.Series(
            pd.to_numeric(frame["adjusted_close"], errors="coerce").to_numpy(),
            index=pd.to_datetime(frame["date"], errors="raise"),
            name=symbol,
        ).dropna()
        series_list.append(series)
        metadata.append({
            "symbol": symbol,
            "source": "local_csv",
            "source_path": str(source.resolve()),
            "rows": int(len(series)),
            "first_date": str(series.index.min().date()),
            "last_date": str(series.index.max().date()),
        })
    return pd.concat(series_list, axis=1).sort_index(), metadata


def save_source_metadata(path: str | Path, metadata: list[dict]) -> None:
    Path(path).write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
