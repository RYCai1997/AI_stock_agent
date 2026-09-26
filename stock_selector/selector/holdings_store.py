"""Local, append-only holdings snapshots used by the desktop interface."""

from __future__ import annotations

import csv
import math
from datetime import datetime
from pathlib import Path
import re
from typing import Iterable, Mapping


HOLDING_COLUMNS = [
    "ticker", "company", "entry_date", "entry_price", "quantity",
    "nonselected_streak", "below_ema_streak", "last_review_date",
    "last_action", "last_reason",
    "cost_basis_date", "signal_date",
]


def normalize_ticker(value: object) -> str:
    ticker = str(value or "").strip().lower()
    if re.fullmatch(r"\d{6}", ticker):
        if ticker.startswith(("5", "6", "9")):
            return f"sh.{ticker}"
        if ticker.startswith(("0", "1", "2", "3")):
            return f"sz.{ticker}"
    if re.fullmatch(r"(?:sh|sz)\.\d{6}", ticker):
        return ticker
    raise ValueError(f"无效A股代码：{value}")


def validate_holdings(rows: Iterable[Mapping[str, object]]) -> list[dict[str, object]]:
    clean: list[dict[str, object]] = []
    seen: set[str] = set()
    for position, source in enumerate(rows, 1):
        ticker = normalize_ticker(source.get("ticker"))
        if ticker in seen:
            raise ValueError(f"持仓代码重复：{ticker}")
        seen.add(ticker)
        try:
            entry_date = datetime.strptime(str(source.get("entry_date", "")).strip(), "%Y-%m-%d").date()
        except ValueError as exc:
            raise ValueError(f"第{position}行买入日期应为YYYY-MM-DD") from exc
        if entry_date > datetime.now().date():
            raise ValueError("实际持仓买入日期不能晚于今天")
        try:
            entry_price = float(source.get("entry_price", ""))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"第{position}行买入价格无效") from exc
        try:
            quantity_value = float(source.get("quantity", ""))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"第{position}行购买数量无效") from exc
        if not math.isfinite(entry_price) or entry_price <= 0:
            raise ValueError(f"第{position}行买入价格必须大于0")
        if not math.isfinite(quantity_value) or quantity_value <= 0 or not quantity_value.is_integer():
            raise ValueError(f"第{position}行购买数量必须是正整数股数")
        row: dict[str, object] = {
            "ticker": ticker,
            "company": str(source.get("company", "") or "").strip(),
            "entry_date": str(entry_date),
            "entry_price": entry_price,
            "quantity": int(quantity_value),
        }
        for column in ("nonselected_streak", "below_ema_streak"):
            try:
                value = float(source.get(column, 0) or 0)
                if not math.isfinite(value) or not value.is_integer():
                    raise ValueError("invalid streak")
                streak = int(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"第{position}行连续确认次数无效") from exc
            if streak < 0:
                raise ValueError(f"第{position}行连续确认次数不能为负数")
            row[column] = streak
        for column in ("last_review_date", "last_action", "last_reason", "cost_basis_date", "signal_date"):
            row[column] = str(source.get(column, "") or "").strip()
        for column in ("last_review_date", "cost_basis_date", "signal_date"):
            if row[column]:
                stamp = datetime.strptime(str(row[column]), "%Y-%m-%d").date()
                if not entry_date <= stamp <= datetime.now().date():
                    raise ValueError(f"{column}必须介于买入日和今天之间")
        clean.append(row)
    return clean


def load_holdings(path: Path) -> list[dict[str, object]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return validate_holdings(csv.DictReader(handle))


def save_holdings(
    rows: Iterable[Mapping[str, object]],
    directory: Path,
    timestamp: datetime | None = None,
) -> Path:
    clean = validate_holdings(rows)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = timestamp or datetime.now()
    path = directory / f"holdings_{stamp:%Y%m%d_%H%M%S_%f}.csv"
    temporary = path.with_suffix(".csv.tmp")
    with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=HOLDING_COLUMNS)
        writer.writeheader()
        writer.writerows(clean)
    temporary.replace(path)
    return path


def latest_holdings_file(directory: Path) -> Path | None:
    files = [path for path in directory.glob("holdings_*.csv") if path.is_file()]
    return max(files, key=lambda path: (path.stat().st_mtime_ns, path.name)) if files else None


def reviewed_snapshot(
    holdings_path: Path,
    review_path: Path,
    directory: Path,
    *, current_run: bool = False,
) -> Path | None:
    if not current_run:
        return None
    holdings = load_holdings(holdings_path)
    if not holdings:
        return None
    with review_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reviews = {row["ticker"]: row for row in csv.DictReader(handle)}
    for holding in holdings:
        review = reviews.get(str(holding["ticker"]))
        if not review:
            continue
        holding["company"] = review.get("company") or holding["company"]
        holding["nonselected_streak"] = review.get("nonselected_streak", 0)
        holding["below_ema_streak"] = review.get("below_ema_streak", 0)
        if review.get("review_date") != str(datetime.now().date()):
            raise ValueError("historical reviews cannot update current holdings")
        if review.get("action") != "review":
            holding["last_review_date"] = review.get("state_review_date") or review.get("review_date", "")
            holding["last_action"] = review.get("action", "")
            holding["last_reason"] = review.get("reason", "")
            holding["signal_date"] = review.get("signal_date", "")
    return save_holdings(holdings, directory)
