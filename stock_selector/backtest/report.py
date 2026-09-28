"""Persist inspectable account-level artifacts from a completed replay."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import pandas as pd

from selector.research_manifest import build_research_manifest

from .corporate_actions import data_quality_report
from .engine import BacktestEngine
from .metrics import performance
from .benchmarks import benchmark_comparison


def write_report(engine: BacktestEngine, output: Path, *, start: str, end: str,
                 input_provenance: dict, snapshot_count: int,
                 variant_code: str = "H",
                 benchmark_prices: pd.DataFrame | None = None) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    measures = performance(engine.daily_nav, engine.trades)
    pd.DataFrame(engine.daily_nav).to_csv(output / "daily_nav.csv", index=False)
    pd.DataFrame(engine.trades).to_csv(output / "trades.csv", index=False)
    pd.DataFrame([asdict(order) for order in engine.orders]).to_csv(output / "orders.csv", index=False)
    pd.DataFrame(engine.positions).to_csv(output / "positions.csv", index=False)
    pd.DataFrame([measures]).to_csv(output / "performance.csv", index=False)
    benchmark_metrics = None
    if benchmark_prices is not None:
        comparison, benchmark_metrics = benchmark_comparison(engine.daily_nav, benchmark_prices)
        comparison.to_csv(output / "benchmark_comparison.csv", index=False)
        pd.DataFrame([benchmark_metrics]).to_csv(output / "benchmark_metrics.csv", index=False)
    quality = data_quality_report(engine.corporate_action_events)
    if input_provenance.get("corporate_actions_status") != "audited":
        quality += "\nInput corporate-action coverage is unverified; historical NAV confidence is degraded.\n"
    (output / "data_quality_report.md").write_text(quality, encoding="utf-8")
    manifest = build_research_manifest(
        as_of=end,
        provider={"membership_snapshot": None, "requested_members": None,
                  "built_rows": None, "errors": {}},
        benchmark={"name": "CSI 300", "code": "sh.000300",
                   "comparison": "full and prior-exposure matched close-to-close"
                   if benchmark_metrics is not None else "pending benchmark price input"},
        execution_assumptions={
            "price_basis": "unadjusted", "timing": "next_tradable_open",
            "limit_rule": "opening at limit blocks adverse-side order",
            "fee_schedules": [asdict(schedule) for schedule in engine.fee_model.schedules],
            "slippage": engine.fee_model.slippage,
        },
        backtest_configuration={"start": start, "end": end,
                                "initial_cash": engine.initial_cash,
                                "research_variant": variant_code,
                                "signal_snapshots": snapshot_count,
                                "input_provenance": input_provenance},
    )
    (output / "research_manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    summary = ["# Continuous account backtest", "",
               f"Research variant: {variant_code}.",
               "Retrospective historical validation; these dates are not strict out-of-sample data.", "",
               f"Period: {start} to {end}; monthly snapshots: {snapshot_count}.",
               f"Cumulative return after modelled costs: {measures['cumulative_return']:.2%}.",
               f"Maximum drawdown: {measures['maximum_drawdown']:.2%}.",
               f"Transaction costs: {measures['total_transaction_costs']:.2f}.",
               f"Manual corporate-action audit events: {sum(e.get('manual_audit_required', False) for e in engine.corporate_action_events)}.",
               ""]
    if benchmark_metrics is not None:
        summary += [f"Full CSI300 cumulative return: {benchmark_metrics['full_csi300_cumulative_return']:.2%}.",
                    f"Exposure-matched CSI300 cumulative return: {benchmark_metrics['matched_csi300_cumulative_return']:.2%}.",
                    "Exposure matching uses prior-day strategy exposure and assumes zero cash yield.",
                    "The index close series excludes dividends unless the supplied benchmark source includes them."]
    summary.append("No alpha claim is made until benchmark and data audits are complete.")
    (output / "summary.md").write_text("\n".join(summary) + "\n", encoding="utf-8")
    return measures
