"""Persist inspectable account-level artifacts from a completed replay."""

from __future__ import annotations

import json
import math
from dataclasses import asdict
from pathlib import Path

import pandas as pd

from selector.research_manifest import build_research_manifest

from .corporate_actions import data_quality_report
from .engine import BacktestEngine
from .metrics import performance
from .benchmarks import benchmark_comparison
from .concentration import winner_concentration
from .diagnostics import MAE_MFE_COLUMNS, mae_mfe
from .corporate_actions import CorporateAction
from .engine import DailyBar


def missing_signal_months(snapshot_dates: list[str]) -> list[str]:
    """Find skipped calendar review months between the first and last signal."""
    if not snapshot_dates:
        return []
    observed = {pd.Period(date, freq="M") for date in snapshot_dates}
    expected = pd.period_range(min(observed), max(observed), freq="M")
    return [str(month) for month in expected if month not in observed]


def write_report(engine: BacktestEngine, output: Path, *, start: str, end: str,
                 input_provenance: dict, snapshot_count: int,
                 snapshot_dates: list[str] | None = None,
                 variant_code: str = "H",
                 benchmark_prices: pd.DataFrame | None = None,
                 bars: list[DailyBar] | None = None,
                 actions: list[CorporateAction] | None = None) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    measures = performance(engine.daily_nav, engine.trades)
    pd.DataFrame(engine.daily_nav).to_csv(output / "daily_nav.csv", index=False)
    pd.DataFrame(engine.trades).to_csv(output / "trades.csv", index=False)
    order_rows = [asdict(order) for order in engine.orders]
    for row in order_rows:
        row["block_history"] = json.dumps(row["block_history"], ensure_ascii=False)
    pd.DataFrame(order_rows).to_csv(output / "orders.csv", index=False)
    pd.DataFrame(engine.positions).to_csv(output / "positions.csv", index=False)
    pd.DataFrame([measures]).to_csv(output / "performance.csv", index=False)
    winner_concentration(engine.trades).to_csv(output / "winner_concentration.csv", index=False)
    diagnostics = (mae_mfe(engine.trades, bars, actions) if bars
                   else pd.DataFrame(columns=MAE_MFE_COLUMNS))
    diagnostics.to_csv(output / "mae_mfe.csv", index=False)
    benchmark_metrics = None
    if benchmark_prices is not None:
        comparison, benchmark_metrics = benchmark_comparison(engine.daily_nav, benchmark_prices)
        comparison.to_csv(output / "benchmark_comparison.csv", index=False)
        pd.DataFrame([benchmark_metrics]).to_csv(output / "benchmark_metrics.csv", index=False)
    quality = data_quality_report(engine.corporate_action_events)
    if input_provenance.get("corporate_actions_status") != "audited":
        quality += "\nInput corporate-action coverage is unverified; historical NAV confidence is degraded.\n"
    quality += "\nCSI300 historical constituent completeness requires a separate official-notice audit.\n"
    quality += "Opening limit checks are conservative proxies without order-book queue data.\n"
    stale_days = sum(int(row.get("stale_positions", 0) > 0) for row in engine.daily_nav)
    quality += (f"Trading sessions with at least one stale held-position valuation: {stale_days}. "
                "Last valid close is carried for NAV only, never used as an execution price.\n")
    quality += "Fee schedules are explicit input assumptions; historical broker rates require independent verification.\n"
    missing_months = missing_signal_months(snapshot_dates or [])
    if missing_months:
        quality += f"Missing monthly signal snapshots: {', '.join(missing_months)}.\n"
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
               f"Period: {start} to {end}; signal snapshots: {snapshot_count}.",
               f"Cumulative return after modelled costs: {measures['cumulative_return']:.2%}.",
               f"Maximum drawdown: {measures['maximum_drawdown']:.2%}.",
               f"Transaction costs: {measures['total_transaction_costs']:.2f}.",
               f"Manual corporate-action audit events: {sum(e.get('manual_audit_required', False) for e in engine.corporate_action_events)}.",
               f"Sessions with stale held-position valuation: {stale_days}.",
               ""]
    if missing_months:
        summary += [f"Missing signal months: {', '.join(missing_months)}.",
                    "This is not a complete monthly V1 replay; performance describes only the supplied sparse signal path."]
    if benchmark_metrics is not None:
        summary += [f"Full CSI300 cumulative return: {benchmark_metrics['full_csi300_cumulative_return']:.2%}.",
                    f"Exposure-matched CSI300 cumulative return: {benchmark_metrics['matched_csi300_cumulative_return']:.2%}.",
                    "Exposure matching uses prior-day strategy exposure and assumes zero cash yield.",
                    "The index close series excludes dividends unless the supplied benchmark source includes them."]
    summary.append("No alpha claim is made until benchmark and data audits are complete.")
    (output / "summary.md").write_text("\n".join(summary) + "\n", encoding="utf-8")
    return measures


def finalize_research_summary(output: Path, comparison: list[dict],
                              input_provenance: dict) -> None:
    """Answer the requested research questions without inventing absent evidence."""
    by_variant = {row["variant"]: row for row in comparison}
    full = by_variant.get("H")
    if full is None:
        return

    def pct(value) -> str:
        try:
            number = float(value)
            return f"{number:.2%}" if math.isfinite(number) else "unavailable"
        except (TypeError, ValueError):
            return "unavailable"

    def read_csv(name: str) -> pd.DataFrame:
        path = output / name
        return pd.read_csv(path) if path.exists() and path.stat().st_size else pd.DataFrame()

    benchmark = read_csv("benchmark_metrics.csv")
    windows = read_csv("window_influence.csv")
    concentration = read_csv("winner_concentration.csv")
    stops = read_csv("mae_mfe.csv")
    parameters = read_csv("parameter_stability.csv")
    monte = read_csv("monte_carlo_selection.csv")
    b = benchmark.iloc[0] if len(benchmark) else None
    winner_values = concentration.set_index("metric")["value"] if len(concentration) else pd.Series(dtype=float)
    top_one = winner_values.get("top_1_trade_share_of_winner_profits", float("nan"))
    top_three = winner_values.get("top_3_trade_share_of_winner_profits", float("nan"))
    completed_monte = monte.loc[monte["status"].eq("completed")] if len(monte) else pd.DataFrame()
    completed_params = parameters.loc[parameters["status"].eq("completed")] if len(parameters) else pd.DataFrame()
    missing_months = input_provenance.get("missing_signal_months", [])
    result_status = "UNRESOLVED" if missing_months else "TENTATIVE"

    lines = ["# Continuous portfolio historical research", "",
             "This is retrospective historical validation. Dates used to design V1 are not strict out-of-sample data.",
             "Returns below are conditional on the supplied snapshots, raw prices, corporate actions and fee schedules.",
             "SUPPORTED — The ledger enforces cash plus marked position value equals total equity under supplied inputs.",
             "", "## Requested questions", "",
             f"1. **{result_status}** — {'Sparse-input' if missing_months else 'V1'} ending NAV: {1 + full['cumulative_return']:.4f}; cumulative return {pct(full['cumulative_return'])}.",
             f"2. **{result_status}** — Maximum drawdown: {pct(full['maximum_drawdown'])}.",
             f"3. **{result_status}** — Cost-after return: {pct(full['cumulative_return'])}; modelled transaction costs {full['total_transaction_costs']:.2f}."]
    if b is not None:
        lines += [
            f"4. **{result_status}** — Full CSI300 return {pct(b['full_csi300_cumulative_return'])}; V1 excess {pct(b['excess_vs_full_csi300'])}.",
            f"5. **{result_status}** — Exposure-matched CSI300 return {pct(b['matched_csi300_cumulative_return'])}; V1 excess {pct(b['excess_vs_matched_csi300'])}.",
        ]
    else:
        lines += ["4. **UNRESOLVED** — Full CSI300 comparison needs aligned benchmark prices.",
                  "5. **UNRESOLVED** — Exposure-matched CSI300 needs aligned benchmark prices."]
    if missing_months:
        lines.append(f"Missing signal months: {', '.join(missing_months)}. This is not a complete monthly V1 replay.")
    if "A" in by_variant and "B" in by_variant:
        timing_delta = by_variant["B"]["cumulative_return"] - by_variant["A"]["cumulative_return"]
        lines.append(f"6. **TENTATIVE** — Index-proxy EMA timing difference B−A: {pct(timing_delta)}; this is not pure stock-strategy attribution.")
    else:
        lines.append("6. **UNRESOLVED** — Run A and B on aligned CSI300 input to assess EMA timing.")
    lines.append("7. **UNRESOLVED** — D−B mixes stock selection with the index proxy, so it cannot isolate Momentum alone.")
    if "D" in by_variant and "E" in by_variant:
        lines.append(f"8. **TENTATIVE** — E−D return difference {pct(by_variant['E']['cumulative_return'] - by_variant['D']['cumulative_return'])}; stock EMA also changes, so this is not pure Quality attribution.")
    else:
        lines.append("8. **UNRESOLVED** — Quality increment requires D/E runs on the same history.")
    if "E" in by_variant and "F" in by_variant:
        lines.append(f"9. **TENTATIVE** — F−E Value-layer return difference {pct(by_variant['F']['cumulative_return'] - by_variant['E']['cumulative_return'])}.")
    else:
        lines.append("9. **UNRESOLVED** — Value increment requires E/F runs.")
    if "F" in by_variant and "G" in by_variant:
        lines.append(f"10. **TENTATIVE** — G−F stop-layer return difference {pct(by_variant['G']['cumulative_return'] - by_variant['F']['cumulative_return'])}; review tail risk and whipsaw in mae_mfe.csv.")
    else:
        lines.append("10. **UNRESOLVED** — Stop contribution requires F/G runs.")
    lines.append("11. **UNRESOLVED** — G−H changes overheat delay and monthly exit together; monthly exit cannot be isolated from this contrast.")
    if len(windows):
        most = windows.iloc[(windows["cumulative_return"] - full["cumulative_return"]).abs().argmax()]
        lines.append(f"12. **TENTATIVE** — Most influential removed opening window: {most['removed_window']}; rerun return {pct(most['cumulative_return'])}. This is an influence diagnostic, not achievable counterfactual performance.")
    else:
        lines.append("12. **UNRESOLVED** — No opening window was available for leave-one-window-out.")
    lines.append(f"13. **TENTATIVE** — Top 1 and Top 3 closed winners account for {pct(top_one)} and {pct(top_three)} of positive closed-trade profit; open gains and dividends are separate.")
    ema_done = len(completed_params.loc[completed_params["parameter"].eq("ema")]) if len(completed_params) else 0
    lines.append(f"14. **{'TENTATIVE' if ema_done == 5 else 'UNRESOLVED'}** — EMA neighborhood completed {ema_done}/5; review parameter_stability.csv for a plateau rather than choosing the maximum.")
    if len(completed_monte):
        median_percentile = completed_monte["actual_percentile_at_or_below"].median()
        lines.append(f"15. **TENTATIVE** — Median fixed-horizon random-selection percentile across completed windows: {median_percentile:.1f}; this is not a significance test.")
    else:
        lines.append("15. **UNRESOLVED** — Random-selection percentile needs complete eligible paths and a 20-session horizon.")
    lines += [
        "16. **UNRESOLVED** — Historical CSI300 membership has not been certified against official notices for every date; see DATA_UNIVERSE_AUDIT.md.",
        "17. **UNRESOLVED** — Order-book fill probability, complete corporate-action coverage, survivorship, small sample size and prospective out-of-sample alpha remain open.",
        "", "## Evidence limits", "",
        f"Corporate-action input status: {input_provenance.get('corporate_actions_status', 'missing')}.",
        f"Closed stop trades with path diagnostics: {len(stops.loc[stops['exit_reason'].eq('stop loss')]) if len(stops) else 0}.",
        "The report does not tune or replace frozen V1 parameters. Human review remains required; no broker orders are placed.",
    ]
    (output / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
