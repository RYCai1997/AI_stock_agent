"""Point-in-time CSI 300 provider using Baostock public data."""

from __future__ import annotations

import math
import statistics
from datetime import datetime
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


INDUSTRY_SECTIONS = {
    "A": "A 农林牧渔业", "B": "B 采矿业", "C": "C 制造业", "D": "D 公用事业",
    "E": "E 建筑业", "F": "F 批发零售业", "G": "G 交通运输仓储业", "H": "H 住宿餐饮业",
    "I": "I 信息技术业", "J": "J 金融业", "K": "K 房地产业", "L": "L 商务服务业",
    "M": "M 科研技术服务业", "N": "N 环保公共设施业", "O": "O 居民服务业",
    "P": "P 教育", "Q": "Q 卫生社会工作", "R": "R 文化体育娱乐业", "S": "S 综合",
}
CACHE_VERSION = 3
K_FIELDS = (
    "date,code,open,high,low,close,volume,amount,turn,tradestatus,pctChg,"
    "peTTM,pbMRQ,psTTM,pcfNcfTTM,isST"
)


def _rows(result: Any) -> list[dict[str, str]]:
    if result.error_code != "0":
        raise RuntimeError(result.error_msg)
    values = []
    while result.next():
        values.append(dict(zip(result.fields, result.get_row_data())))
    return values


def _number(value: Any) -> float | None:
    if value in {None, "", "None", "nan"}:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _upgrade_cached_row(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Apply lossless cache schema migrations without changing source values."""
    row = payload.get("row")
    if not row:
        return None
    if payload.get("cache_version") == 1 and "operating_cashflow_yield" in row:
        row = dict(row)
        row["net_cashflow_yield"] = row.pop("operating_cashflow_yield")
    return row


def _quarters(as_of: str, count: int = 8) -> list[tuple[int, int]]:
    stamp = pd.Timestamp(as_of)
    quarter = (stamp.month - 1) // 3 + 1
    year = stamp.year
    result = []
    for _ in range(count):
        result.append((year, quarter))
        quarter -= 1
        if quarter == 0:
            year -= 1
            quarter = 4
    return result


def _latest_quality(bs: Any, code: str, as_of: str) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    latest = None
    for year, quarter in _quarters(as_of):
        profits = _rows(bs.query_profit_data(code=code, year=year, quarter=quarter))
        cashflows = _rows(bs.query_cash_flow_data(code=code, year=year, quarter=quarter))
        for profit in profits:
            if not profit.get("pubDate") or profit["pubDate"] > as_of:
                continue
            matches = [
                row for row in cashflows
                if row.get("statDate") == profit.get("statDate") and row.get("pubDate") <= as_of
            ]
            if matches:
                cash = max(matches, key=lambda row: row["pubDate"])
                latest = {"profit": profit, "cash": cash, "statDate": profit["statDate"]}
        if latest is not None:
            break

    annual = []
    # Early-year snapshots may not yet have the preceding fiscal year's annual
    # filing. Search a wider calendar window without accepting later filings.
    for year in range(pd.Timestamp(as_of).year, pd.Timestamp(as_of).year - 10, -1):
        for row in _rows(bs.query_profit_data(code=code, year=year, quarter=4)):
            if row.get("pubDate") and row["pubDate"] <= as_of and _number(row.get("epsTTM")) is not None:
                annual.append(row)
    annual.sort(key=lambda row: row["statDate"])
    return latest, annual[-6:]


def _at_or_before(series: pd.Series, when: pd.Timestamp) -> float | None:
    eligible = series.loc[series.index <= when].dropna()
    return float(eligible.iloc[-1]) if not eligible.empty else None


def _price_metrics_from_frame(frame: pd.DataFrame, as_of: str) -> dict[str, Any]:
    """Compute point-in-time price fields while ignoring rows after ``as_of``."""
    node = pd.Timestamp(as_of)
    frame = frame.copy()
    if frame.empty:
        raise ValueError("no price history")
    frame["date"] = pd.to_datetime(frame["date"])
    frame = frame[frame["date"].le(node)]
    if frame.empty:
        raise ValueError("no price history at or before snapshot")
    numeric = ["close", "volume", "amount", "turn", "peTTM", "pbMRQ", "pcfNcfTTM"]
    frame[numeric] = frame[numeric].apply(pd.to_numeric, errors="coerce")
    frame = frame.drop_duplicates("date", keep="last").sort_values("date").set_index("date")
    close = frame["close"].dropna()
    latest = frame.loc[close.index[-1]]
    p1 = _at_or_before(close, node - pd.DateOffset(months=1))
    p7 = _at_or_before(close, node - pd.DateOffset(months=7))
    p13 = _at_or_before(close, node - pd.DateOffset(months=13))
    returns = close.pct_change(fill_method=None).dropna()
    recent = close.tail(126)
    pe, pb, pcf = (_number(latest.get(name)) for name in ("peTTM", "pbMRQ", "pcfNcfTTM"))

    def inverse(value: float | None) -> float | None:
        return 1.0 / value if value not in {None, 0} else None

    return {
        "price_as_of": str(close.index[-1].date()),
        "price": float(close.iloc[-1]),
        "return_20d": float(close.iloc[-1] / close.iloc[-21] - 1.0) if len(close) >= 21 else None,
        "ema200": float(close.ewm(span=200, adjust=False, min_periods=200).mean().iloc[-1]) if len(close) >= 200 else None,
        "volatility_1y": float(returns.tail(252).std(ddof=1) * math.sqrt(252)) if len(returns) >= 60 else None,
        "max_drawdown_6m": float((recent / recent.cummax() - 1.0).min()),
        "avg_daily_turnover": float(frame["amount"].tail(60).mean()),
        "mom_6_1": p1 / p7 - 1.0 if p1 and p7 else None,
        "mom_12_1": p1 / p13 - 1.0 if p1 and p13 else None,
        "earnings_yield": inverse(pe),
        # Baostock pcfNcfTTM is price / net cash flow, not price / operating cash flow.
        "net_cashflow_yield": inverse(pcf),
        "book_to_price": inverse(pb),
        "tradestatus": str(latest.get("tradestatus", "")),
        "is_st": str(latest.get("isST", "")),
    }


def _price_metrics(bs: Any, code: str, as_of: str) -> dict[str, Any]:
    node = pd.Timestamp(as_of)
    start = str((node - pd.Timedelta(days=550)).date())
    rows = _rows(bs.query_history_k_data_plus(
        code, K_FIELDS, start_date=start, end_date=as_of, frequency="d", adjustflag="2"
    ))
    return _price_metrics_from_frame(pd.DataFrame(rows), as_of)


def _eps_stability(annual: list[dict[str, Any]]) -> float | None:
    eps = [_number(row.get("epsTTM")) for row in annual]
    eps = [value for value in eps if value is not None]
    growth = []
    for previous, current in zip(eps, eps[1:]):
        denominator = max(abs(previous), 0.01)
        growth.append(max(-5.0, min(5.0, (current - previous) / denominator)))
    return statistics.stdev(growth) if len(growth) >= 4 else None


def _market_trend(bs: Any, as_of: str) -> tuple[str, dict[str, Any]]:
    data = _price_metrics(bs, "sh.000300", as_of)
    if data["ema200"] is None:
        return "unknown", {"benchmark": "沪深300", "reason": "insufficient history"}
    status = "up" if data["price"] > data["ema200"] else "down"
    return status, {
        "benchmark": "沪深300", "code": "sh.000300", "price_as_of": data["price_as_of"],
        "close": data["price"], "ema200": data["ema200"],
        "return_20d": data["return_20d"],
    }


def build_a_metrics(
    as_of: str, limit: int = 0, cache_dir: Path | None = None
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Download one historical CSI 300 snapshot with disclosure-date controls."""
    try:
        import baostock as bs
    except ImportError as exc:
        raise RuntimeError("baostock is required; install stock_selector/requirements.txt") from exc
    login = bs.login()
    if login.error_code != "0":
        raise RuntimeError(f"Baostock login failed: {login.error_msg}")
    try:
        universe = _rows(bs.query_hs300_stocks(as_of))
        original_members = len(universe)
        membership_date = max(row["updateDate"] for row in universe)
        industry_rows = _rows(bs.query_stock_industry(date=as_of))
        industries = {row["code"]: row for row in industry_rows}
        if limit:
            universe = universe[:limit]
        output = []
        errors = {}
        cache_hits = 0
        for position, item in enumerate(universe, 1):
            code = item["code"]
            cache_path = cache_dir / f"{as_of}_{code}.json" if cache_dir else None
            if cache_path and cache_path.exists():
                cached = json.loads(cache_path.read_text(encoding="utf-8"))
                if cached.get("cache_version") in {1, 2, CACHE_VERSION} and cached.get("as_of") == as_of:
                    cached_row = _upgrade_cached_row(cached)
                    legacy_missing_stability = (
                        cached.get("cache_version") < CACHE_VERSION
                        and cached_row is not None
                        and cached_row.get("eps_growth_std") is None
                    )
                    if cached_row and not legacy_missing_stability:
                        cache_hits += 1
                        output.append(cached_row)
                        continue
                    # Provider/network errors are intentionally retried. A
                    # transient login failure must not become permanent data
                    # missingness merely because it was cached during a run.
            try:
                industry = industries.get(code, {})
                industry_name = industry.get("industry") or "Unknown"
                section = industry_name[:1]
                quality, annual = _latest_quality(bs, code, as_of)
                price = _price_metrics(bs, code, as_of)
                if quality is None:
                    raise ValueError("no profit and cash-flow report jointly filed by snapshot")
                profit, cash = quality["profit"], quality["cash"]
                filing_date = max(
                    [profit["pubDate"], cash["pubDate"]] + [row["pubDate"] for row in annual]
                )
                row = {
                    "market": "A", "ticker": code, "company": item["code_name"],
                    "industry_l1": INDUSTRY_SECTIONS.get(section, f"{section} Unknown"),
                    "industry_l2": industry_name,
                    "industry_source": industry.get("industryClassification", "Baostock"),
                    "universe_as_of": membership_date, "fundamental_as_of": filing_date,
                    "financial_period": quality["statDate"],
                    "roe": _number(profit.get("roeAvg")),
                    "cfo_to_revenue": _number(cash.get("CFOToOR")),
                    "eps_growth_std": _eps_stability(annual),
                    "relative_strength": np.nan,
                    "security_eligible": price["tradestatus"] == "1" and price["is_st"] != "1",
                    **price,
                }
                output.append(row)
                cache_payload = {"cache_version": CACHE_VERSION, "as_of": as_of, "row": row}
            except Exception as exc:
                errors[code] = str(exc)
                cache_payload = {"cache_version": CACHE_VERSION, "as_of": as_of, "error": str(exc)}
            if cache_path:
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                temporary = cache_path.with_suffix(".json.tmp")
                temporary.write_text(json.dumps(cache_payload, ensure_ascii=False), encoding="utf-8")
                temporary.replace(cache_path)
            if position % 10 == 0 or position == len(universe):
                print(f"A-share provider {position}/{len(universe)} built={len(output)} errors={len(errors)}", flush=True)
        frame = pd.DataFrame(output)
        if frame.empty:
            raise ValueError("no CSI 300 rows could be built")
        groups = frame["industry_l2"].where(
            frame.groupby("industry_l2")["ticker"].transform("size") >= 5, frame["industry_l1"]
        )
        frame["relative_strength"] = frame["mom_12_1"] - frame.groupby(groups)["mom_12_1"].transform("median")
        trend, benchmark = _market_trend(bs, as_of)
        metadata = {
            "market": "A", "universe": "CSI 300", "as_of": as_of,
            "membership_snapshot": membership_date, "original_members": original_members,
            "requested_members": len(universe), "built_rows": len(frame), "errors": errors,
            "cache_hits": cache_hits, "market_trend": trend,
            "benchmark": benchmark,
            "factor_policy": (
                "ROE + CFO/revenue + EPS stability; PE/price-to-net-cash-flow/PB inverted; "
                "filed dates <= as_of"
            ),
            "generated_at": datetime.now().astimezone().isoformat(),
        }
        return frame, metadata
    finally:
        bs.logout()
