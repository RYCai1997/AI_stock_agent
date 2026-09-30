"""Replay sealed monthly signals against independently audited execution inputs."""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from backtest.corporate_actions import CorporateAction
from backtest.engine import DailyBar
from data_public.archive import sha256
from evaluation.performance import build_performance
from prospective.shadow_account import replay_continuous_shadow


def load_audited_inputs(bars_path: Path, actions_path: Path, calendar_path: Path,
                        manifest_path: Path, now: datetime):
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    required = {"source", "retrieved_at", "price_basis", "bars_sha256",
                "actions_sha256", "calendar_sha256", "corporate_actions_complete"}
    if not required <= set(manifest) or manifest["price_basis"] != "unadjusted":
        raise ValueError("shadow execution source provenance incomplete")
    if manifest["corporate_actions_complete"] is not True:
        raise ValueError("audited corporate-action coverage required")
    for path, key in ((bars_path, "bars_sha256"), (actions_path, "actions_sha256"),
                      (calendar_path, "calendar_sha256")):
        if sha256(path.read_bytes()) != manifest[key]:
            raise ValueError(f"shadow source hash mismatch: {key}")
    sessions = json.loads(calendar_path.read_text(encoding="utf-8"))
    if not isinstance(sessions, list) or not sessions or sessions != sorted(set(sessions)):
        raise ValueError("sorted unique market calendar required")
    local = now.astimezone(ZoneInfo("Asia/Shanghai"))
    if sessions[-1] > local.date().isoformat() or (sessions[-1] == local.date().isoformat()
                                                  and local.hour < 15):
        raise ValueError("future or unfinished execution session")
    bars = [DailyBar(**item) for item in json.loads(bars_path.read_text(encoding="utf-8"))]
    actions = [CorporateAction(**item) for item in json.loads(actions_path.read_text(encoding="utf-8"))]
    if not bars or any(bar.date not in sessions for bar in bars):
        raise ValueError("execution bars missing or outside calendar")
    raw_bars = json.loads(bars_path.read_text(encoding="utf-8"))
    if any(not {"date", "ticker", "open", "high", "low", "close", "tradable",
                "limit_up", "limit_down", "is_st"} <= set(item) for item in raw_bars):
        raise ValueError("explicit execution OHLC and security state required")
    if any(action.date not in sessions for action in actions):
        raise ValueError("corporate action outside market calendar")
    return bars, actions, sessions, manifest


def main() -> None:
    parser = argparse.ArgumentParser(description="Replay audited continuous prospective shadow account")
    parser.add_argument("--prospective-root", type=Path,
                        default=Path(__file__).resolve().parent / "outputs" / "prospective")
    parser.add_argument("--bars", type=Path, required=True)
    parser.add_argument("--actions", type=Path, required=True)
    parser.add_argument("--calendar", type=Path, required=True)
    parser.add_argument("--source-manifest", type=Path, required=True)
    parser.add_argument("--fee-config", type=Path,
                        default=Path(__file__).resolve().parent.parent / "PROSPECTIVE_EXECUTION_MODEL.json")
    args = parser.parse_args()
    predictions = sorted(path for path in args.prospective_root.iterdir()
                         if path.is_dir() and (path / "prediction_seal.json").is_file())
    bars, actions, sessions, source = load_audited_inputs(
        args.bars, args.actions, args.calendar, args.source_manifest,
        datetime.now(ZoneInfo("Asia/Shanghai")))
    if not predictions:
        raise ValueError("no sealed primary prospective predictions")
    audit_path = args.prospective_root / "shadow" / "input_manifests" / f"{sessions[-1]}.json"
    if audit_path.exists() and json.loads(audit_path.read_text(encoding="utf-8")) != source:
        raise ValueError("shadow execution inputs for this date changed")
    engine = replay_continuous_shadow(
        prediction_dirs=predictions, bars=bars, actions=actions,
        market_calendar=sessions, shadow_dir=args.prospective_root / "shadow",
        fee_config=args.fee_config)
    # Keep the execution-input identity next to the append-only account output.
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    if not audit_path.exists():
        with audit_path.open("x", encoding="utf-8") as handle:
            json.dump(source, handle, ensure_ascii=False, indent=2)
    build_performance(args.prospective_root / "prospective_registry.csv",
                      args.prospective_root / "evaluation_registry.csv",
                      args.prospective_root / "shadow",
                      args.prospective_root / "prospective_performance.csv")
    print(json.dumps({"status": "replayed", "date": sessions[-1],
                      "nav": engine.daily_nav[-1]["daily_nav"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
