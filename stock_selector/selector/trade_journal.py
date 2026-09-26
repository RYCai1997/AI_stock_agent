"""Actual fills and resulting holdings, stored together in a local SQLite transaction."""
import json
import math
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from .holdings_store import validate_holdings, normalize_ticker, save_holdings


@contextmanager
def connection(path):
    db = sqlite3.connect(path)
    try:
        with db:
            yield db
    finally:
        db.close()


def record_fill(db_path, fill, holdings):
    rows = validate_holdings(holdings)
    code = normalize_ticker(fill["ticker"])
    when = datetime.strptime(fill["trade_date"], "%Y-%m-%d").date()
    if when > date.today():
        raise ValueError("成交日期不能晚于今天")
    qty, price, fees = (float(fill[k]) for k in ("quantity", "price", "fees"))
    if not all(math.isfinite(x) for x in (qty, price, fees)) or qty <= 0 or not qty.is_integer() or price <= 0 or fees < 0:
        raise ValueError("成交股数应为正整数，价格大于0，费用非负")
    side = fill["side"]
    if side not in ("BUY", "SELL"):
        raise ValueError("未知成交方向")
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with connection(db_path) as db:
        db.execute("CREATE TABLE IF NOT EXISTS fills (id TEXT PRIMARY KEY, recorded_at TEXT, trade_date TEXT, payload TEXT, holdings_after TEXT, realized_pnl REAL, cash_delta REAL)")
        prior = db.execute("SELECT holdings_after FROM fills WHERE id=?", (fill["id"],)).fetchone()
        if prior:
            return json.loads(prior[0])
        last = db.execute("SELECT MAX(trade_date) FROM fills").fetchone()[0]
        if last and fill["trade_date"] < last:
            raise ValueError("请按成交日期顺序登记，历史补录需先核对账本")
        positions = {r["ticker"]: r for r in rows}
        old = positions.get(code)
        if old and fill["trade_date"] < max(str(old["entry_date"]), str(old.get("last_review_date", ""))):
            raise ValueError("成交日期早于当前持仓状态，请使用当时的持仓记录")
        realized = 0.0
        if side == "BUY":
            previous_qty = old["quantity"] if old else 0
            previous_cost = previous_qty * old["entry_price"] if old else 0
            positions[code] = {
                **(old or {}), "ticker": code, "company": fill.get("company") or (old or {}).get("company", ""),
                "quantity": int(previous_qty + qty), "entry_price": (previous_cost + qty * price + fees) / (previous_qty + qty),
                "entry_date": old["entry_date"] if old else fill["trade_date"],
            }
            # Do not assert corporate actions reconciled merely because another purchase occurred.
            cash_delta = -qty * price - fees
        else:
            if old is None or qty > old["quantity"]:
                raise ValueError("卖出数量超过已记录持仓")
            realized = qty * (price - old["entry_price"]) - fees
            cash_delta = qty * price - fees
            old["quantity"] -= int(qty)
            if old["quantity"] == 0:
                del positions[code]
        after = validate_holdings(positions.values())
        db.execute("INSERT INTO fills VALUES (?,?,?,?,?,?,?)", (
            fill["id"], datetime.now().isoformat(), fill["trade_date"],
            json.dumps(fill, ensure_ascii=False), json.dumps(after, ensure_ascii=False), realized, cash_delta))
    return after


def export_latest_holdings(db_path, directory):
    with connection(db_path) as db:
        row = db.execute("SELECT holdings_after FROM fills ORDER BY rowid DESC LIMIT 1").fetchone()
    if not row:
        raise ValueError("暂无已登记成交")
    return save_holdings(json.loads(row[0]), directory)


def journal_rows(db_path):
    if not Path(db_path).exists():
        return []
    with connection(db_path) as db:
        return [dict(**json.loads(payload), realized_pnl=pnl, cash_delta=cash)
                for payload, pnl, cash in db.execute("SELECT payload,realized_pnl,cash_delta FROM fills ORDER BY rowid")]
