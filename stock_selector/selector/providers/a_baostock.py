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

from ..run_feedback import ProgressCallback


INDUSTRY_SECTIONS = {
    "A": "A 农林牧渔业", "B": "B 采矿业", "C": "C 制造业", "D": "D 公用事业",
    "E": "E 建筑业", "F": "F 批发零售业", "G": "G 交通运输仓储业", "H": "H 住宿餐饮业",
    "I": "I 信息技术业", "J": "J 金融业", "K": "K 房地产业", "L": "L 商务服务业",
    "M": "M 科研技术服务业", "N": "N 环保公共设施业", "O": "O 居民服务业",
    "P": "P 教育", "Q": "Q 卫生社会工作", "R": "R 文化体育娱乐业", "S": "S 综合",
}
CACHE_VERSION = 4
MAX_TICKER_ATTEMPTS = 3
# Audited code succession: Baostock closes sz.300114 on 2025-02-17 and
# publishes the continuing issuer as sz.302132. Public annual-report metadata
# under 302132 independently lists the predecessor's 2020-2023 reports.
HISTORICAL_CODE_CONTINUITY = {"sz.302132": ("sz.300114", "2025-02-17")}
K_FIELDS = (
    "date,code,open,high,low,close,volume,amount,turn,tradestatus,pctChg,"
    "peTTM,pbMRQ,psTTM,pcfNcfTTM,isST"
)


def _rows(result: Any) -> list[dict[str, str]]:
    if result.error_code != "0":
        raise RuntimeError(f"Baostock {result.error_code}: {result.error_msg}")
    values = []
    while result.next():
        values.append(dict(zip(result.fields, result.get_row_data())))
    return values


def _session_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return any(term in message for term in (
        "10001001", "10002001", "10002002", "10002003", "10002004",
        "10002005", "10002006", "10002007", "10002008",
        "用户未登录", "用户未登陆", "not logged in", "session invalid",
        "session expired", "connection reset", "timeout", "network error"))


def _reconnect(bs: Any) -> None:
    try:
        bs.logout()
    except Exception:
        pass
    response = bs.login()
    if response.error_code != "0":
        raise RuntimeError(f"Baostock reconnect failed: {response.error_msg}")


def _captured_rows(result: Any, endpoint: str, parameters: dict,
                   recorder=None) -> list[dict[str, str]]:
    """Preserve the Baostock SDK table before downstream factor normalization."""
    rows = _rows(result)
    if recorder is not None:
        recorder(endpoint, parameters, list(result.fields), rows)
    return rows


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


def _cache_sources_match(entries: list[dict], code: str, as_of: str) -> bool:
    if not isinstance(entries, list) or not all(isinstance(entry, dict) for entry in entries):
        return False
    required = {"query_profit_data", "query_cash_flow_data", "query_history_k_data_plus"}
    seen = set()
    for entry in entries:
        params = entry.get("query_parameters", {})
        permitted = {code}
        if code in HISTORICAL_CODE_CONTINUITY:
            permitted.add(HISTORICAL_CODE_CONTINUITY[code][0])
        if params.get("code") not in permitted:
            return False
        if entry.get("endpoint") == "query_history_k_data_plus" and params.get("end_date") != as_of:
            return False
        seen.add(entry.get("endpoint"))
    return required <= seen


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


def _latest_quality(bs: Any, code: str, as_of: str, recorder=None) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    latest = None
    for year, quarter in _quarters(as_of):
        params = {"code": code, "year": year, "quarter": quarter}
        profits = _captured_rows(bs.query_profit_data(**params), "query_profit_data", params, recorder)
        cashflows = _captured_rows(bs.query_cash_flow_data(**params), "query_cash_flow_data", params, recorder)
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
        params = {"code": code, "year": year, "quarter": 4}
        for row in _captured_rows(bs.query_profit_data(**params), "query_profit_data", params, recorder):
            if row.get("pubDate") and row["pubDate"] <= as_of and _number(row.get("epsTTM")) is not None:
                annual.append(row)
    if len(annual) < 5 and code in HISTORICAL_CODE_CONTINUITY:
        prior_code, last_date = HISTORICAL_CODE_CONTINUITY[code]
        if as_of > last_date:
            old_params, new_params = {"code": prior_code}, {"code": code}
            old_basic = _captured_rows(bs.query_stock_basic(**old_params),
                                       "query_stock_basic", old_params, recorder)
            new_basic = _captured_rows(bs.query_stock_basic(**new_params),
                                       "query_stock_basic", new_params, recorder)
            if (len(old_basic) != 1 or len(new_basic) != 1 or
                    old_basic[0].get("outDate") != last_date or
                    old_basic[0].get("ipoDate") != new_basic[0].get("ipoDate")):
                raise ValueError(f"historical code continuity unverified: {prior_code} -> {code}")
            existing = {row["statDate"] for row in annual}
            for year in range(pd.Timestamp(as_of).year - 10, int(last_date[:4])):
                params = {"code": prior_code, "year": year, "quarter": 4}
                for row in _captured_rows(bs.query_profit_data(**params),
                                          "query_profit_data", params, recorder):
                    if (row.get("statDate") not in existing and row.get("pubDate") and
                            row["pubDate"] <= as_of and _number(row.get("epsTTM")) is not None):
                        annual.append(row)
                        existing.add(row["statDate"])
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


def _price_metrics(bs: Any, code: str, as_of: str, recorder=None) -> dict[str, Any]:
    node = pd.Timestamp(as_of)
    start = str((node - pd.Timedelta(days=550)).date())
    params = {"code": code, "fields": K_FIELDS, "start_date": start,
              "end_date": as_of, "frequency": "d", "adjustflag": "2"}
    rows = _captured_rows(bs.query_history_k_data_plus(
        code, K_FIELDS, start_date=start, end_date=as_of, frequency="d", adjustflag="2"
    ), "query_history_k_data_plus", params, recorder)
    return _price_metrics_from_frame(pd.DataFrame(rows), as_of)


def _eps_stability(annual: list[dict[str, Any]]) -> float | None:
    eps = [_number(row.get("epsTTM")) for row in annual]
    eps = [value for value in eps if value is not None]
    growth = []
    for previous, current in zip(eps, eps[1:]):
        denominator = max(abs(previous), 0.01)
        growth.append(max(-5.0, min(5.0, (current - previous) / denominator)))
    return statistics.stdev(growth) if len(growth) >= 4 else None


def _market_trend(bs: Any, as_of: str, recorder=None) -> tuple[str, dict[str, Any]]:
    data = _price_metrics(bs, "sh.000300", as_of, recorder)
    if data["ema200"] is None:
        return "unknown", {"benchmark": "沪深300", "reason": "insufficient history"}
    status = "up" if data["price"] > data["ema200"] else "down"
    return status, {
        "benchmark": "沪深300", "code": "sh.000300", "price_as_of": data["price_as_of"],
        "close": data["price"], "ema200": data["ema200"],
        "return_20d": data["return_20d"],
    }


def build_a_metrics(
    as_of: str, limit: int = 0, cache_dir: Path | None = None,
    progress: ProgressCallback | None = None,
    raw_recorder=None,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Download one historical CSI 300 snapshot with disclosure-date controls."""
    try:
        import baostock as bs
    except ImportError as exc:
        raise RuntimeError("baostock is required; install stock_selector/requirements.txt") from exc
    def report(stage: int, completed: int, total: int, detail: str) -> None:
        if progress:
            progress(stage, completed, total, detail)

    report(1, 0, 4, "正在连接数据源")
    login = bs.login()
    if login.error_code != "0":
        raise RuntimeError(f"Baostock login failed: {login.error_msg}")
    login_count = 1
    reconnect_count = 0
    retry_by_ticker: dict[str, int] = {}
    try:
        report(1, 1, 4, "已连接；正在获取沪深300成分")
        universe = _captured_rows(bs.query_hs300_stocks(as_of), "query_hs300_stocks",
                                  {"date": as_of}, raw_recorder)
        if not universe:
            raise ValueError("no verified CSI 300 membership snapshot")
        member_codes = [row["code"] for row in universe]
        original_members = len(universe)
        update_dates = [row["updateDate"] for row in universe]
        membership_updates_by_ticker = {row["code"]: row["updateDate"] for row in universe}
        membership_date = max(update_dates)
        report(1, 2, 4, "已获取股票池；正在获取行业")
        industry_rows = _captured_rows(bs.query_stock_industry(date=as_of), "query_stock_industry",
                                       {"date": as_of}, raw_recorder)
        industries = {row["code"]: row for row in industry_rows}
        report(1, 3, 4, "已获取行业；正在获取沪深300行情")
        trend, benchmark = _market_trend(bs, as_of, raw_recorder) if raw_recorder else _market_trend(bs, as_of)
        report(1, 4, 4, "股票池、行业及基准已就绪")
        if limit:
            universe = universe[:limit]
        output = []
        errors = {}
        cache_hits = 0
        for position, item in enumerate(universe, 1):
            code = item["code"]
            report(2, position - 1, len(universe), f"正在处理 {code} {item['code_name']}")
            cache_path = cache_dir / f"{as_of}_{code}.json" if cache_dir else None
            if cache_path and cache_path.exists():
                try:
                    cached = json.loads(cache_path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    cached = {}
                if cached.get("cache_version") in {1, 2, 3, CACHE_VERSION} and cached.get("as_of") == as_of:
                    cached_row = _upgrade_cached_row(cached)
                    legacy_missing_stability = (
                        cached.get("cache_version") < CACHE_VERSION
                        and cached_row is not None
                        and cached_row.get("eps_growth_std") is None
                    )
                    row_ok = (cached_row and not legacy_missing_stability and
                              cached_row.get("ticker") == code and
                              cached_row.get("factor_data_status") != "suspicious_missing" and
                              cached_row.get("price_as_of") == benchmark.get("price_as_of"))
                    provenance_ok = (raw_recorder is None or
                                     (row_ok and cached.get("cache_version") == CACHE_VERSION and
                                      _cache_sources_match(cached.get("source_entries", []), code, as_of) and
                                      callable(getattr(raw_recorder, "restore", None)) and
                                      raw_recorder.restore(cached.get("source_entries", []))))
                    if row_ok and provenance_ok:
                        cached_row["membership_update_date"] = item["updateDate"]
                        cache_hits += 1
                        output.append(cached_row)
                        report(2, position, len(universe),
                               f"{code} 缓存已读取；成功{len(output)}，错误{len(errors)}，缓存{cache_hits}")
                        continue
                    # Provider/network errors are intentionally retried. A
                    # transient login failure must not become permanent data
                    # missingness merely because it was cached during a run.
            try:
                industry = industries.get(code, {})
                industry_name = industry.get("industry") or "Unknown"
                section = industry_name[:1]
                for attempt in range(MAX_TICKER_ATTEMPTS):
                    # Capture SDK tables only after the whole ticker succeeds. A failed
                    # attempt must never contribute half a ticker to the final manifest.
                    pending: list[tuple] = []
                    recorder = (lambda *parts: pending.append(parts)) if raw_recorder else None
                    try:
                        quality, annual = _latest_quality(bs, code, as_of, recorder)
                        report(2, position - 1, len(universe), f"{code} 财务查询完成，正在获取行情")
                        price = _price_metrics(bs, code, as_of, recorder)
                        if quality is None:
                            raise ValueError("no profit and cash-flow report jointly filed by snapshot")
                        history_status = "complete"
                        listing_date = None
                        if len(annual) < 5:
                            params = {"code": code}
                            basics = _captured_rows(bs.query_stock_basic(**params),
                                                    "query_stock_basic", params, recorder)
                            listing_date = basics[0].get("ipoDate") if len(basics) == 1 else None
                            latest_expected = (pd.Timestamp(as_of).year -
                                               (1 if pd.Timestamp(as_of).month >= 5 else 2))
                            expected = set(range(pd.Timestamp(listing_date).year - 1,
                                                 latest_expected + 1)) if listing_date else set()
                            observed = {pd.Timestamp(item["statDate"]).year for item in annual}
                            history_status = ("insufficient_history" if listing_date and
                                              listing_date <= as_of and expected <= observed
                                              else "suspicious_missing")
                        break
                    except Exception as exc:
                        if not _session_error(exc) or attempt + 1 == MAX_TICKER_ATTEMPTS:
                            raise
                        retry_by_ticker[code] = retry_by_ticker.get(code, 0) + 1
                        _reconnect(bs)
                        login_count += 1
                        reconnect_count += 1
                source_entries = []
                if raw_recorder:
                    for parts in pending:
                        source_entries.append(raw_recorder(*parts))
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
                    "membership_update_date": item["updateDate"],
                    "financial_period": quality["statDate"],
                    "roe": _number(profit.get("roeAvg")),
                    "cfo_to_revenue": _number(cash.get("CFOToOR")),
                    "eps_growth_std": _eps_stability(annual),
                    "annual_eps_observations": len(annual),
                    "annual_eps_growth_observations": max(0, len(annual) - 1),
                    "eps_history_source_codes": sorted({item.get("code", code) for item in annual}),
                    "eps_stability_status": history_status,
                    "listing_date": listing_date,
                    "provider_status": "complete",
                    "quality_history_status": history_status,
                    "price_status": "current" if price["price_as_of"] == as_of else "stale",
                    "security_state_status": "complete" if price["tradestatus"] in {"0", "1"} and price["is_st"] in {"0", "1"} else "unknown",
                    "relative_strength": np.nan,
                    "security_eligible": price["tradestatus"] == "1" and price["is_st"] != "1",
                    **price,
                }
                other_factors = ("roe", "cfo_to_revenue", "earnings_yield",
                                 "net_cashflow_yield", "book_to_price")
                if not row["security_eligible"] or section == "J":
                    row["factor_data_status"] = "ineligible"
                elif any(row[name] is None for name in other_factors):
                    row["factor_data_status"] = "suspicious_missing"
                elif row["eps_growth_std"] is not None:
                    row["factor_data_status"] = "complete"
                elif history_status == "insufficient_history":
                    row["factor_data_status"] = "structurally_incomplete"
                else:
                    row["factor_data_status"] = "suspicious_missing"
                output.append(row)
                cache_payload = {"cache_version": CACHE_VERSION, "as_of": as_of,
                                 "row": row, "source_entries": source_entries}
            except Exception as exc:
                errors[code] = str(exc)
                cache_payload = None
            if cache_path and cache_payload is not None:
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                temporary = cache_path.with_suffix(".json.tmp")
                temporary.write_text(json.dumps(cache_payload, ensure_ascii=False), encoding="utf-8")
                temporary.replace(cache_path)
            report(2, position, len(universe),
                   f"{code} {'取数失败' if code in errors else '取数完成'}；成功{len(output)}，错误{len(errors)}，缓存{cache_hits}")
            if not progress and (position % 10 == 0 or position == len(universe)):
                print(f"A-share provider {position}/{len(universe)} built={len(output)} errors={len(errors)}", flush=True)
        frame = pd.DataFrame(output)
        if frame.empty:
            raise ValueError("no CSI 300 rows could be built")
        groups = frame["industry_l2"].where(
            frame.groupby("industry_l2")["ticker"].transform("size") >= 5, frame["industry_l1"]
        )
        frame["relative_strength"] = frame["mom_12_1"] - frame.groupby(groups)["mom_12_1"].transform("median")
        metadata = {
            "market": "A", "universe": "CSI 300", "as_of": as_of,
            "membership_snapshot": membership_date, "original_members": original_members,
            "membership_update_min": min(update_dates),
            "membership_update_max": max(update_dates),
            "membership_update_unique_count": len(set(update_dates)),
            "membership_updates_by_ticker": membership_updates_by_ticker,
            "member_codes": member_codes,
            "requested_members": len(universe), "built_rows": len(frame), "errors": errors,
            "cache_hits": cache_hits, "market_trend": trend,
            "baostock_login_count": login_count,
            "session_reconnect_count": reconnect_count,
            "ticker_retry_count": sum(retry_by_ticker.values()),
            "retry_by_ticker": retry_by_ticker,
            "provider_error_count": len(errors),
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


def quote_from_frames(raw: pd.DataFrame, adjusted: pd.DataFrame, start: str, end: str) -> dict:
    """Unadjusted execution price; flag basis changes instead of inventing cost adjustments."""
    def clean(frame):
        frame = frame.copy()
        frame = frame[frame["date"].between(start, end)]
        frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
        return frame.sort_values("date").drop_duplicates("date").set_index("date")
    r, a = clean(raw), clean(adjusted)
    joined = r[["close"]].join(a[["close"]], lsuffix="_raw", rsuffix="_adj").dropna()
    if joined.empty or start not in joined.index:
        raise ValueError("cost basis date has no comparable trading price")
    ratios = joined["close_raw"] / joined["close_adj"]
    if not np.isfinite(ratios).all() or (joined <= 0).any().any():
        raise ValueError("invalid execution price")
    latest = str(joined.index[-1])
    return {"price": float(joined.iloc[-1]["close_raw"]), "price_as_of": latest,
            "tradestatus": str(r.loc[latest, "tradestatus"]),
            "basis_changed": bool((abs(ratios / ratios.iloc[0] - 1) > 1e-5).any()),
            "price_basis": "unadjusted"}


def fetch_holding_quotes(
    holdings: pd.DataFrame, as_of: str, progress: ProgressCallback | None = None,
) -> dict:
    """Fetch held names even when outside the selection universe. Errors remain explicit."""
    if holdings.empty:
        return {}
    def report(completed: int, detail: str) -> None:
        if progress:
            progress(4, completed, len(holdings) + 2, detail)

    import baostock as bs
    report(0, "正在连接持仓／计划报价数据源")
    login = bs.login()
    if login.error_code != "0":
        report(len(holdings), "报价连接失败；已记录错误，继续复核数据风险")
        return {str(h.ticker): {"error": login.error_msg} for h in holdings.itertuples()}
    quotes = {}
    try:
        for position, h in enumerate(holdings.to_dict("records"), 1):
            code = str(h["ticker"])
            report(position - 1, f"正在获取 {code} 未复权价格与成本口径")
            start = h.get("cost_basis_date")
            start = str(start) if pd.notna(start) and start else str(h["entry_date"])
            try:
                frames = [pd.DataFrame(_rows(bs.query_history_k_data_plus(
                    code, "date,close,tradestatus", start_date=start, end_date=as_of,
                    frequency="d", adjustflag=flag))) for flag in ("3", "2")]
                quotes[code] = quote_from_frames(*frames, start, as_of)
            except Exception as exc:
                quotes[code] = {"error": str(exc)}
            report(position, f"{code} 报价{'失败' if 'error' in quotes[code] else '完成'}")
    finally:
        bs.logout()
    return quotes
