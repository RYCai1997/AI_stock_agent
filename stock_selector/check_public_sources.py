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


def check_sources(fetch: Callable[[str], tuple[int, bytes]] = _fetch_small) -> dict:
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
    return {"checked_at_utc": datetime.now(timezone.utc).isoformat(),
            "data_provider_version": "PUBLIC_V1", "sources": status}


def main() -> None:
    print(json.dumps(check_sources(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
