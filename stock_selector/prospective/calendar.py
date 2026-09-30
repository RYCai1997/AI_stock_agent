"""Prospective signal-day decision from a public monthly trading calendar."""

from __future__ import annotations

import json
import calendar
from datetime import datetime, time
from zoneinfo import ZoneInfo

from data_history.calendar import generate_signal_calendar
from data_public.archive import PublicSourceArchive, SourceResult


SHANGHAI = ZoneInfo("Asia/Shanghai")


def signal_day_decision(now: datetime, sessions: list[str],
                        research_date: str | None = None) -> dict:
    local = now.astimezone(SHANGHAI)
    today = local.date().isoformat()
    chosen = research_date or today
    if chosen[:7] != today[:7] or chosen != today:
        return {"signal_date": chosen, "prospective_primary": False,
                "evidence_label": "retrospective_reconstruction" if chosen < today else "research_only",
                "reason": "requested date differs from actual Shanghai generation date"}
    if local.time() < time(15, 0):
        return {"signal_date": chosen, "prospective_primary": False,
                "evidence_label": "research_only", "reason": "market close not reached"}
    calendar = generate_signal_calendar(sessions, today[:7], today[:7])
    scheduled = calendar[0]["signal_date"]
    eligible = chosen == scheduled
    return {"signal_date": chosen, "scheduled_signal_date": scheduled,
            "prospective_primary": eligible,
            "evidence_label": "prospective" if eligible else "research_only",
            "reason": None if eligible else "not the first market session on or after the 15th"}


def fetch_public_month_sessions(month: str, archive: PublicSourceArchive) -> tuple[list[str], dict]:
    """Capture the Baostock SDK's unmodified table before normalizing dates."""
    import baostock as bs

    year, number = map(int, month.split("-"))
    if not 1 <= number <= 12:
        raise ValueError("invalid month")
    end = f"{year:04d}-{number:02d}-{calendar.monthrange(year, number)[1]:02d}"
    start = month + "-01"
    login = bs.login()
    if login.error_code != "0":
        raise RuntimeError("Baostock calendar login failed")
    try:
        response = bs.query_trade_dates(start_date=start, end_date=end)
        if response.error_code != "0":
            raise RuntimeError("Baostock calendar query failed")
        fields, items = response.fields, []
        while response.next():
            items.append(response.get_row_data())
    finally:
        bs.logout()
    if not {"calendar_date", "is_trading_day"} <= set(fields):
        raise ValueError("public calendar lacks required fields")
    rows = [dict(zip(fields, row)) for row in items]
    if not rows or any(row["is_trading_day"] not in {"0", "1"} for row in rows):
        raise ValueError("invalid public trading calendar")
    sessions = [row["calendar_date"] for row in rows if row["is_trading_day"] == "1"]
    raw = json.dumps({"fields": fields, "items": items}, ensure_ascii=False).encode("utf-8")
    normalized = json.dumps(sessions, ensure_ascii=False).encode("utf-8")
    metadata = archive.capture(SourceResult("baostock", "query_trade_dates",
                                          {"start_date": start, "end_date": end},
                                          raw, normalized, "baostock-sdk-table-v1"),
                               requested_source="baostock", fallback_reason=None,
                               attempts=[{"source": "baostock", "status": "used"}])
    return sessions, metadata
