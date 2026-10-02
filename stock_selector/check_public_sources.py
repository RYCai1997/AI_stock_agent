"""Small, bounded public-source health probes; no bulk historical download."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Callable
from urllib.request import Request, urlopen


SOURCES = {
    "csi": ("https://www.csindex.com.cn/", "site"),
    "sse": ("https://www.sse.com.cn/", "site"),
    "szse": ("https://www.szse.cn/", "site"),
    "cninfo": ("https://www.cninfo.com.cn/", "site"),
    "eastmoney": ("https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=1.000300&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55&klt=101&fqt=0&beg=20250715&end=20250715", "api"),
    "baostock": ("https://www.baostock.com/", "site"),
}


def _fetch_small(url: str) -> tuple[int, bytes]:
    request = Request(url, headers={"User-Agent": "AI-stock-agent-PUBLIC_V1-health/1"})
    with urlopen(request, timeout=4) as response:
        return response.status, response.read(4096)


def _probe_baostock_dated() -> bool:
    import baostock as bs
    login = bs.login()
    if login.error_code != "0":
        raise RuntimeError(f"Baostock SDK login failed: {login.error_msg}")
    try:
        result = bs.query_trade_dates(start_date="2025-07-15", end_date="2025-07-15")
        if result.error_code != "0" or not result.next():
            raise RuntimeError("Baostock dated calendar query failed")
        row = dict(zip(result.fields, result.get_row_data()))
        return row.get("calendar_date") == "2025-07-15" and row.get("is_trading_day") == "1"
    finally:
        bs.logout()


def check_sources(fetch: Callable[[str], tuple[int, bytes]] = _fetch_small,
                  probe_baostock: Callable[[], bool] | None = None) -> dict:
    status = {}
    for name, (url, kind) in SOURCES.items():
        try:
            code, body = fetch(url)
            if code != 200 or not body:
                state, reason = "degraded", f"HTTP {code} or empty response"
            elif kind == "api":
                payload = json.loads(body)
                data = payload.get("data") if isinstance(payload, dict) else None
                state = "available" if isinstance(data, dict) and data.get("klines") else "degraded"
                reason = "small API response parsed" if state == "available" else "API did not return a bar"
            else:
                state, reason = "degraded", "site reachable; dated endpoint/parser unverified"
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            state, reason = "unavailable", type(exc).__name__
        status[name] = {"status": state, "reason": reason, "url_identifier": url}
    if probe_baostock is not None:
        try:
            valid = probe_baostock()
            status["baostock"] = {
                "status": "available" if valid else "degraded",
                "reason": ("dated SDK calendar endpoint parsed" if valid else
                           "dated SDK calendar endpoint returned invalid data"),
                "url_identifier": "Baostock SDK query_trade_dates(2025-07-15)",
            }
        except Exception as exc:
            status["baostock"] = {"status": "unavailable", "reason": type(exc).__name__,
                                   "url_identifier": "Baostock SDK query_trade_dates(2025-07-15)"}
    return {"checked_at_utc": datetime.now(timezone.utc).isoformat(),
            "data_provider_version": "PUBLIC_V1", "sources": status}


def main() -> None:
    print(json.dumps(check_sources(probe_baostock=_probe_baostock_dated),
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
