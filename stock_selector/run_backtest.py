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
from backtest.official import OfficialSnapshot
from backtest.report import write_report, finalize_research_summary, missing_signal_months
from backtest.variants import VARIANTS, VariantSignalProvider, run_index_variant
from backtest.robustness import leave_one_window_out
from backtest.parameter_stability import parameter_surface
from backtest.monte_carlo import selection_monte_carlo


def validate_replay_price_inputs(bars_path: Path, benchmark_path: Path | None,
                                 provenance: dict, price_audit_path: Path | None = None) -> None:
    for path in (bars_path, benchmark_path):
        if path and path.name.endswith(".partial.csv"):
            raise ValueError(f"partial price input cannot enter replay: {path}")
    if provenance.get("price_collection_complete") is False:
        raise ValueError("partial price collection cannot enter replay")
    if price_audit_path:
        audit = json.loads(price_audit_path.read_text(encoding="utf-8"))
        if (not audit.get("collection_complete")
                or audit.get("complete_tickers") != audit.get("requested_tickers")):
            raise ValueError("partial public price audit cannot enter replay")
        if not any(row.get("ticker") == "sh.000300" and row.get("status") == "ok"
                   for row in audit.get("series", [])):
            raise ValueError("benchmark is missing from public price audit")


def load_inputs(args: argparse.Namespace):
    provenance = json.loads(args.input_manifest.read_text(encoding="utf-8"))
    validate_replay_price_inputs(args.bars, getattr(args, "benchmark_bars", None), provenance,
                                 getattr(args, "price_audit", None))
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
    parser.add_argument("--variant", choices=sorted(VARIANTS), default="H")
    parser.add_argument("--all-variants", action="store_true")
    parser.add_argument("--benchmark-bars", type=Path,
                        help="CSI300 index date,open,close,ema200 CSV for A/B variants")
    parser.add_argument("--price-audit", type=Path,
                        help="Public collection coverage audit; incomplete coverage blocks replay")
    parser.add_argument("--market-calendar", type=Path,
                        help="CSV with date column: complete market sessions, including no-stock-bar days")
    parser.add_argument("--adjusted-bars", type=Path,
                        help="Adjusted stock date,ticker,close history for EMA neighborhood")
    parser.add_argument("--adjusted-benchmark-bars", type=Path,
                        help="Adjusted CSI300 date,close history for EMA neighborhood")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    provenance, bars, actions, fees, snapshots = load_inputs(args)
    market_calendar = None
    if args.market_calendar:
        calendar_frame = pd.read_csv(args.market_calendar, dtype={"date": str})
        if "date" not in calendar_frame:
            parser.error("market calendar CSV requires date column")
        market_calendar = calendar_frame["date"].tolist()
    provenance = {**provenance,
                  "missing_signal_months": missing_signal_months(list(snapshots)),
                  "market_calendar": str(args.market_calendar) if args.market_calendar else "inferred_from_bars"}
    output = args.output or (Path("reports/continuous_backtest") if args.variant == "H"
                             else Path("reports/continuous_backtest/variants") / args.variant)
    codes = sorted(VARIANTS) if args.all_variants else [args.variant]
    if any(VARIANTS[code].kind == "index" for code in codes) and args.benchmark_bars is None:
        parser.error("A/B variants require --benchmark-bars")
    benchmark_frame = pd.read_csv(args.benchmark_bars) if args.benchmark_bars else None
    if market_calendar and benchmark_frame is not None:
        if set(benchmark_frame["date"].astype(str)) != set(market_calendar):
            parser.error("benchmark dates must cover every explicit market session")
    comparison = []
    for code in codes:
        spec = VARIANTS[code]
        variant_output = (output if not args.all_variants or code == "H"
                          else output / "variants" / code)
        if spec.kind == "index":
            engine = run_index_variant(benchmark_frame, spec, args.initial_cash)
        else:
            adapter = VariantSignalProvider(spec, snapshots, variant_output / "signal_audit")
            engine = BacktestEngine(args.initial_cash, fee_model=fees, stop_enabled=spec.stop_loss).run(
                bars, [], actions, signal_provider=adapter, market_calendar=market_calendar)
        measures = write_report(engine, variant_output,
                                start=min(day["date"] for day in engine.daily_nav),
                                end=max(day["date"] for day in engine.daily_nav),
                                input_provenance=provenance, snapshot_count=len(snapshots),
                                snapshot_dates=list(snapshots),
                                variant_code=code, benchmark_prices=benchmark_frame,
                                bars=bars if spec.kind == "stock" else [], actions=actions)
        comparison.append({"variant": code, "label": spec.label,
                           "kind": spec.kind, **measures})
        if code == "H":
            windows = [row["signal_date"] for row in adapter.official.signal_audit
                       if row["planned_buys"] > 0]
            influence = leave_one_window_out(
                windows=windows, bars=bars, actions=actions, fee_model=fees,
                initial_cash=args.initial_cash,
                provider_factory=lambda removed: VariantSignalProvider(
                    VARIANTS["H"], snapshots, output / "influence_signal_audit" / removed),
                benchmark_prices=benchmark_frame,
                market_calendar=market_calendar,
            )
            influence.to_csv(output / "window_influence.csv", index=False)
            adjusted_stocks = pd.read_csv(args.adjusted_bars) if args.adjusted_bars else None
            adjusted_index = pd.read_csv(args.adjusted_benchmark_bars) if args.adjusted_benchmark_bars else None
            surface = parameter_surface(
                snapshots=snapshots, bars=bars, actions=actions,
                fee_model=fees, initial_cash=args.initial_cash,
                audit_dir=output / "parameter_signal_audit",
                adjusted_stocks=adjusted_stocks, adjusted_index=adjusted_index,
                market_calendar=market_calendar,
            )
            surface.to_csv(output / "parameter_stability.csv", index=False)
            monte_carlo = selection_monte_carlo(
                windows=windows, snapshots=snapshots, bars=bars, actions=actions,
                fee_model=fees, initial_cash=args.initial_cash,
                actual_orders=engine.orders, iterations=1000, seed=20260928,
            )
            monte_carlo.to_csv(output / "monte_carlo_selection.csv", index=False)
    if args.all_variants:
        output.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(comparison).to_csv(output / "variant_comparison.csv", index=False)
    finalize_research_summary(output, comparison, provenance)
    serializable = [{key: value if not isinstance(value, float) or math.isfinite(value) else None
                     for key, value in row.items()} for row in comparison]
    print(json.dumps({"output": str(output.resolve()), "variants": serializable},
                     allow_nan=False, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
