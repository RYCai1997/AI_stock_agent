"""Continuous shadow ledger reconstructed from sealed predictions, appended by day."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory

import pandas as pd

from backtest.corporate_actions import CorporateAction
from backtest.engine import BacktestEngine, DailyBar
from backtest.fees import FeeModel, FeeSchedule
from backtest.official import OfficialSignalProvider, OfficialSnapshot
from verify_prediction import verify_prediction


def prospective_fee_model(config_path: Path) -> FeeModel:
    config = json.loads(config_path.read_text(encoding="utf-8"))
    schedule = FeeSchedule(config["effective_from"], config["effective_to"],
                           config["commission_rate"], config["minimum_commission"],
                           config["seller_stamp_duty"], config["transfer_fee_both_sides"])
    return FeeModel((schedule,), slippage=config["slippage"])


def _checked_offset(path: Path, rows: list[dict]) -> int:
    existing = []
    if path.exists():
        existing = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    if len(existing) > len(rows) or rows[:len(existing)] != existing:
        raise ValueError(f"shadow history drift; old rows cannot be rewritten: {path.name}")
    return len(existing)


def _append_verified(path: Path, rows: list[dict], offset: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for row in rows[offset:]:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def replay_continuous_shadow(*, prediction_dirs: list[Path], bars: list[DailyBar],
                             actions: list[CorporateAction], market_calendar: list[str],
                             shadow_dir: Path, fee_config: Path,
                             initial_cash: float = 1000000) -> BacktestEngine:
    if not prediction_dirs or not market_calendar:
        raise ValueError("sealed predictions and dated market calendar required")
    predictions = []
    for directory in prediction_dirs:
        if not verify_prediction(directory):
            raise ValueError(f"unsealed prediction cannot enter shadow account: {directory}")
        record = json.loads((directory / "prediction_record.json").read_text(encoding="utf-8"))
        if not record["prospective_primary"] or record["evidence_label"] != "prospective":
            raise ValueError("only primary prospective signals enter official shadow account")
        predictions.append((record, directory))
    predictions.sort(key=lambda pair: pair[0]["signal_date"])
    if len({record["signal_date"] for record, _ in predictions}) != len(predictions):
        raise ValueError("duplicate primary prediction date")
    if predictions[0][0]["signal_date"] != market_calendar[0]:
        raise ValueError("shadow calendar must begin at first sealed signal")
    snapshots = {}
    for record, directory in predictions:
        metrics_path = directory / "normalized" / "raw_metrics.csv"
        provider_path = directory / "normalized" / "provider_snapshot.json"
        if not metrics_path.is_file() or not provider_path.is_file():
            raise ValueError("sealed prediction lacks dated selector input")
        snapshots[record["signal_date"]] = OfficialSnapshot(
            pd.read_csv(metrics_path, dtype={"ticker": str}),
            json.loads(provider_path.read_text(encoding="utf-8")))
    config = json.loads(fee_config.read_text(encoding="utf-8"))
    manifest = {"initial_cash": initial_cash,
                "execution_model_version": config["execution_model_version"],
                "first_signal_date": predictions[0][0]["signal_date"]}
    manifest_path = shadow_dir / "shadow_manifest.json"
    if manifest_path.exists():
        if json.loads(manifest_path.read_text(encoding="utf-8")) != manifest:
            raise ValueError("shadow capital or execution model changed")
    with TemporaryDirectory(prefix="shadow_signal_audit_") as folder:
        adapter = OfficialSignalProvider(snapshots, Path(folder))
        engine = BacktestEngine(initial_cash, fee_model=prospective_fee_model(fee_config)).run(
            bars, [], actions, signal_provider=adapter, market_calendar=market_calendar)
    order_intents = [{"signal_date": order.signal_date,
                      "intended_execution_date": order.intended_execution_date,
                      "ticker": order.ticker, "side": order.side,
                      "signal_price": order.signal_price, "reason": order.reason}
                     for order in engine.orders]
    order_events = []
    for order in engine.orders:
        for block in order.block_history:
            order_events.append({"date": block["date"], "ticker": order.ticker,
                                 "side": order.side, "event": "blocked", "reason": block["reason"]})
        if order.actual_execution_date:
            order_events.append({"date": order.actual_execution_date, "ticker": order.ticker,
                                 "side": order.side, "event": "filled",
                                 "execution_price": order.execution_price})
    order_events.sort(key=lambda row: (row["date"], row["ticker"], row["side"], row["event"]))
    date = market_calendar[-1]
    checkpoint = {"date": date, "cash": engine.account.cash,
                  "positions": {ticker: asdict(position) for ticker, position in engine.account.positions.items()},
                  "pending_orders": [asdict(order) for order in engine.orders if order.status == "pending"],
                  "fees": engine.account.fees, "dividends": engine.account.dividends,
                  "realized_pnl": engine.account.realized_pnl,
                  "nav": engine.daily_nav[-1]["daily_nav"]}
    path = shadow_dir / "checkpoints" / f"{date}.json"
    if path.exists():
        if json.loads(path.read_text(encoding="utf-8")) != checkpoint:
            raise ValueError("existing shadow checkpoint changed")
    outputs = {"shadow_nav.jsonl": engine.daily_nav,
               "shadow_positions.jsonl": engine.positions,
               "shadow_orders.jsonl": order_intents,
               "shadow_order_events.jsonl": order_events,
               "shadow_trades.jsonl": engine.trades}
    offsets = {name: _checked_offset(shadow_dir / name, rows)
               for name, rows in outputs.items()}
    shadow_dir.mkdir(parents=True, exist_ok=True)
    if not manifest_path.exists():
        with manifest_path.open("x", encoding="utf-8") as handle:
            json.dump(manifest, handle, ensure_ascii=False, indent=2)
    for name, rows in outputs.items():
        _append_verified(shadow_dir / name, rows, offsets[name])
    path.parent.mkdir(exist_ok=True)
    if not path.exists():
        with path.open("x", encoding="utf-8") as handle:
            json.dump(checkpoint, handle, ensure_ascii=False, indent=2)
    return engine
