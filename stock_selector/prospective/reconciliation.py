"""Independent next-open observation; never edits the sealed signal package."""

from __future__ import annotations

import json
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from data_public.archive import sha256
from verify_prediction import verify_prediction


def reconcile_execution(*, prediction_dir: Path, observations: dict[str, dict],
                        source_manifest: dict, evaluated_at: datetime,
                        slippage: float, output_dir: Path) -> Path:
    if not verify_prediction(prediction_dir):
        raise ValueError("prediction seal invalid")
    if not 0 <= slippage < 1 or not source_manifest.get("raw_sha256") or not source_manifest.get("source"):
        raise ValueError("execution source or slippage assumption missing")
    if evaluated_at.tzinfo is None:
        raise ValueError("evaluation time must include timezone")
    record = json.loads((prediction_dir / "prediction_record.json").read_text(encoding="utf-8"))
    now = evaluated_at.astimezone(ZoneInfo("Asia/Shanghai"))
    rows = []
    for target in record["intended_execution_date"]:
        ticker, planned = target["ticker"], target["date"]
        obs = observations.get(ticker)
        base = {"ticker": ticker, "planned_execution_date": planned}
        if obs is None:
            rows.append({**base, "simulated_fill": False, "fill_block_reason": "observation_missing"})
            continue
        actual = date.fromisoformat(str(obs["date"])).isoformat()
        if actual < planned:
            raise ValueError("observation predates planned execution")
        if actual > now.date().isoformat() or (actual == now.date().isoformat()
                                             and now.time() < time(9, 30)):
            raise ValueError("future or unopened market observation")
        opening = float(obs["open"])
        if opening <= 0:
            raise ValueError("invalid market open")
        if not isinstance(obs.get("tradable"), bool):
            raise ValueError("tradable must be an explicit boolean")
        tradable = obs["tradable"]
        limit_up, limit_down = obs.get("limit_up"), obs.get("limit_down")
        if limit_up is not None and limit_down is not None and not (
                0 < float(limit_down) <= opening <= float(limit_up) and
                float(limit_down) < float(limit_up)):
            raise ValueError("inconsistent price limits or open")
        if actual != planned:
            reason = "not_planned_execution_date"
        elif not tradable:
            reason = "not_tradable"
        elif limit_up is None or limit_down is None:
            reason = "exact_price_limits_missing"
        elif opening >= float(limit_up) - 1e-8:
            reason = "limit_up_buy_block"
        else:
            reason = None
        rows.append({**base, "observed_date": actual,
                     "actual_market_open": opening, "tradable": tradable,
                     "limit_up": limit_up, "limit_down": limit_down,
                     "simulated_fill": reason is None, "fill_block_reason": reason,
                     "simulated_execution_price": opening * (1 + slippage) if reason is None else None,
                     "slippage": slippage})
    result = {"prediction_id": record["prediction_id"],
              "prediction_seal_sha256": sha256((prediction_dir / "prediction_seal.json").read_bytes()),
              "generated_at": evaluated_at.isoformat(),
              "source_manifest": source_manifest, "observations": rows,
              "orders_placed": 0}
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{record['prediction_id']}.execution_reconciliation.json"
    with path.open("x", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2)
    if not verify_prediction(prediction_dir):
        raise AssertionError("reconciliation changed sealed prediction")
    return path
