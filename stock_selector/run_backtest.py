"""Offline historical replay from explicit, auditable input files."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import pandas as pd

from backtest.corporate_actions import CorporateAction
from backtest.engine import BacktestEngine, DailyBar
from backtest.fees import FeeModel, FeeSchedule
from backtest.official import OfficialSignalProvider, OfficialSnapshot
from backtest.report import write_report


def load_inputs(args: argparse.Namespace):
    provenance = json.loads(args.input_manifest.read_text(encoding="utf-8"))
    if provenance.get("bars_price_basis") != "unadjusted":
        raise ValueError("input manifest must confirm unadjusted execution bars")
    if provenance.get("corporate_actions_status") not in {"audited", "unverified"}:
        raise ValueError("corporate_actions_status must be audited or unverified")
    bar_frame = pd.read_csv(args.bars)
    required = {"date", "ticker", "open", "high", "low", "close", "tradable"}
    if not required <= set(bar_frame):
        raise ValueError(f"bars CSV is missing {sorted(required - set(bar_frame))}")
    bars = [DailyBar(
        date=str(row.date), ticker=str(row.ticker),
        open=float(row.open), high=float(row.high), low=float(row.low), close=float(row.close),
        tradable=str(row.tradable).lower() in {"true", "1", "yes"},
        limit_up=float(row.limit_up) if "limit_up" in bar_frame and pd.notna(row.limit_up) else None,
        limit_down=float(row.limit_down) if "limit_down" in bar_frame and pd.notna(row.limit_down) else None,
        is_st=str(row.is_st).lower() in {"true", "1", "yes"} if "is_st" in bar_frame else False,
    ) for row in bar_frame.itertuples()]
    action_frame = pd.read_csv(args.actions)
    if not {"date", "ticker", "kind"} <= set(action_frame):
        raise ValueError("actions CSV requires date,ticker,kind columns")
    actions = [CorporateAction(
        date=str(row.date), ticker=str(row.ticker), kind=str(row.kind),
        cash_per_share=float(row.cash_per_share) if "cash_per_share" in action_frame and pd.notna(row.cash_per_share) else 0,
        share_ratio=float(row.share_ratio) if "share_ratio" in action_frame and pd.notna(row.share_ratio) else 0,
        subscription_price=float(row.subscription_price) if "subscription_price" in action_frame and pd.notna(row.subscription_price) else None,
        exercise_rights=str(row.exercise_rights).lower() in {"true", "1", "yes"}
        if "exercise_rights" in action_frame else False,
    ) for row in action_frame.itertuples()]
    fee_config = json.loads(args.fee_config.read_text(encoding="utf-8"))
    fee_model = FeeModel(tuple(FeeSchedule(**item) for item in fee_config["schedules"]),
                         slippage=float(fee_config["slippage"]))
    snapshots = {}
    for path in sorted(args.snapshots_dir.glob("*/raw_metrics.csv")):
        date = path.parent.name
        run_metadata = json.loads((path.parent / "official_run_metadata.json").read_text(encoding="utf-8"))
        snapshots[date] = OfficialSnapshot(pd.read_csv(path), run_metadata["provider"])
    if not snapshots:
        raise ValueError("no dated official raw_metrics.csv snapshots found")
    return provenance, bars, actions, fee_model, snapshots


def main() -> None:
    parser = argparse.ArgumentParser(description="Replay frozen V1 on audited offline history")
    parser.add_argument("--bars", type=Path, required=True)
    parser.add_argument("--actions", type=Path, required=True)
    parser.add_argument("--snapshots-dir", type=Path, required=True)
    parser.add_argument("--fee-config", type=Path, required=True)
    parser.add_argument("--input-manifest", type=Path, required=True)
    parser.add_argument("--initial-cash", type=float, default=1000000)
    parser.add_argument("--output", type=Path, default=Path("reports/continuous_backtest"))
    args = parser.parse_args()
    provenance, bars, actions, fees, snapshots = load_inputs(args)
    adapter = OfficialSignalProvider(snapshots, args.output / "signal_audit")
    engine = BacktestEngine(args.initial_cash, fee_model=fees).run(
        bars, [], actions, signal_provider=adapter)
    measures = write_report(engine, args.output, start=min(bar.date for bar in bars),
                            end=max(bar.date for bar in bars),
                            input_provenance=provenance, snapshot_count=len(snapshots))
    serializable = {key: value if not isinstance(value, float) or math.isfinite(value) else None
                    for key, value in measures.items()}
    print(json.dumps({"output": str(args.output.resolve()), "performance": serializable},
                     allow_nan=False, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
