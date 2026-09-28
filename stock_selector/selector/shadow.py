"""Append-only prospective observation logs; never submits an order."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .strategy import OFFICIAL_STRATEGY
from .version import APPLICATION_VERSION


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_shadow_record(*, as_of: str, output: Path, metadata: dict,
                        plan, actionable, account: dict | None,
                        shadow_dir: Path) -> Path:
    now = datetime.now(timezone.utc).isoformat()
    planned = plan.to_dict("records")
    record = {
        "schema_version": 1,
        "run_id": f"{as_of}_{uuid4().hex[:12]}",
        "generated_at": now,
        "strategy_id": OFFICIAL_STRATEGY.strategy_id,
        "strategy_version": OFFICIAL_STRATEGY.strategy_version,
        "application_version": APPLICATION_VERSION,
        "git_commit": metadata.get("git", {}).get("commit"),
        "git_worktree_dirty": metadata.get("git", {}).get("tracked_worktree_dirty"),
        "data_snapshot": metadata.get("data_provider"),
        "data_hash": {
            "raw_metrics_sha256": _sha256(output / "raw_metrics.csv"),
            "candidates_sha256": _sha256(output / "candidates.csv"),
        },
        "signal": {"as_of": as_of, "actionable_tickers": actionable["ticker"].astype(str).tolist()},
        "planned_order": planned,
        "simulated_order": None,
        "observable_next_open": None,
        "execution_feasibility": "pending_observation",
        "execution_difference": None,
        "account_state": account,
        "orders_placed": 0,
    }
    shadow_dir.mkdir(parents=True, exist_ok=True)
    path = shadow_dir / f"{record['run_id']}.json"
    with path.open("x", encoding="utf-8") as handle:
        json.dump(record, handle, ensure_ascii=False, indent=2, default=str)
    return path


def append_next_open_observation(record_path: Path, observations: dict,
                                 *, source: str, slippage: float = .001) -> Path:
    """Write a separate reconciliation file; original signal record remains immutable."""
    if not 0 <= slippage < 1:
        raise ValueError("invalid slippage")
    original = json.loads(record_path.read_text(encoding="utf-8"))
    signal_date = original["signal"]["as_of"]
    rows = []
    for plan in original["planned_order"]:
        ticker = str(plan["ticker"])
        quote = observations.get(ticker)
        if quote is None:
            rows.append({"ticker": ticker, "execution_feasibility": "missing_observation",
                         "simulated_order": None, "execution_difference": None})
            continue
        observed_date = str(quote["date"])
        if observed_date <= signal_date:
            raise ValueError("next-open observation must be after signal date")
        opening = float(quote["open"])
        if opening <= 0:
            raise ValueError("next-open price must be positive")
        if not quote.get("tradable", False):
            feasibility = "suspension"
        elif quote.get("limit_up") is None:
            feasibility = "undetermined_limit_data_missing"
        elif opening >= float(quote["limit_up"]) - 1e-8:
            feasibility = "limit_up_buy_block"
        else:
            feasibility = "feasible_under_opening_rule"
        reference = plan.get("execution_price")
        difference = opening / float(reference) - 1 if reference is not None else None
        rows.append({"ticker": ticker, "date": observed_date,
                     "observable_next_open": opening,
                     "execution_feasibility": feasibility,
                     "simulated_order": {"side": "buy", "simulated_price": opening * (1 + slippage)}
                     if feasibility == "feasible_under_opening_rule" else None,
                     "execution_difference": difference})
    reconciliation = {
        "original_record_sha256": _sha256(record_path),
        "source": source, "observed_at": datetime.now(timezone.utc).isoformat(),
        "slippage_assumption": slippage, "observations": rows,
        "orders_placed": 0,
    }
    path = record_path.with_name(record_path.stem + f".reconciliation.{uuid4().hex[:8]}.json")
    with path.open("x", encoding="utf-8") as handle:
        json.dump(reconciliation, handle, ensure_ascii=False, indent=2)
    return path
