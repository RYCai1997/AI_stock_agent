"""Save the original Baostock A-share trading-date response for offline review."""

from __future__ import annotations

import argparse
import csv
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch dated Baostock A-share market sessions")
    parser.add_argument("--start", default="2020-03-01")
    parser.add_argument("--end", default="2025-07-31")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    import baostock as bs
    login = bs.login()
    if login.error_code != "0":
        raise RuntimeError(f"Baostock login failed: {login.error_msg}")
    try:
        result = bs.query_trade_dates(start_date=args.start, end_date=args.end)
        if result.error_code != "0":
            raise RuntimeError(result.error_msg)
        fields = result.fields
        rows = []
        while result.next():
            rows.append(result.get_row_data())
    finally:
        bs.logout()
    if not rows or not {"calendar_date", "is_trading_day"} <= set(fields):
        raise ValueError("missing Baostock trading-date fields or rows")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(fields)
        writer.writerows(rows)
    print(f"saved {len(rows)} dated market-calendar rows to {args.output}")


if __name__ == "__main__":
    main()
