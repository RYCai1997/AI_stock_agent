"""PUBLIC_V1 signal-day runner; incomplete public inputs fail before any primary record."""

from __future__ import annotations

import argparse
import json
import tempfile
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from data_public import DATA_PROVIDER_VERSION
from data_public.archive import PublicSourceArchive, SourceResult
from data_public.policy import assess_quality
from prospective.calendar import fetch_public_month_sessions, signal_day_decision
from prospective.package import create_prediction_package, git_state
from selector.pipeline import run_selection
from selector.portfolio_plan import build_portfolio_plan
from selector.providers.a_baostock import build_a_metrics
from selector.strategy import OFFICIAL_STRATEGY


BASE = Path(__file__).resolve().parent
REPO = BASE.parent


def next_month(month: str) -> str:
    year, number = map(int, month.split("-"))
    if not 1 <= number <= 12:
        raise ValueError("invalid month")
    return f"{year + (number == 12):04d}-{number % 12 + 1:02d}"


def make_baostock_recorder(archive: PublicSourceArchive, entries: list[dict]):
    def record(endpoint: str, parameters: dict, fields: list[str], rows: list[dict]) -> None:
        raw = json.dumps({"fields": fields,
                          "items": [[row.get(field) for field in fields] for row in rows]},
                         ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        normalized = json.dumps(rows, ensure_ascii=False, sort_keys=True,
                                separators=(",", ":")).encode("utf-8")
        requested = {"query_hs300_stocks": "csi_official",
                     "query_profit_data": "cninfo",
                     "query_cash_flow_data": "cninfo"}.get(endpoint, "baostock")
        fallback_reason = ("no verified machine-readable official parser configured; "
                           "Baostock public SDK table used") if requested != "baostock" else None
        attempts = ([{"source": requested, "status": "unavailable",
                      "reason": "official parser not configured"}] if fallback_reason else [])
        attempts.append({"source": "baostock", "status": "used"})
        metadata = archive.capture(SourceResult("baostock", endpoint, parameters, raw,
                                               normalized, "baostock-sdk-table-v1"),
                                   requested_source=requested, fallback_reason=fallback_reason,
                                   attempts=attempts)
        entries.append(metadata)
    return record


def _csv_bytes(frame: pd.DataFrame) -> bytes:
    return frame.to_csv(index=False).encode("utf-8")


def assess_snapshot(metrics: pd.DataFrame, scored: pd.DataFrame, provider: dict,
                    signal_date: str, entries: list[dict]) -> dict:
    current = pd.Timestamp(signal_date)
    eligible = scored[scored["security_eligible"] & scored["model_supported"]]
    factors = ("roe", "cfo_to_revenue", "eps_growth_std", "earnings_yield",
               "net_cashflow_yield", "book_to_price", "mom_6_1", "mom_12_1")
    factor_ok = bool(len(eligible)) and eligible[list(factors)].notna().all().all()
    def dated(column: str) -> pd.Series:
        return pd.to_datetime(metrics[column], errors="coerce") if column in metrics else pd.Series(
            pd.NaT, index=metrics.index)
    member_dates = dated("membership_update_date")
    publication_dates = dated("fundamental_as_of")
    price_dates = dated("price_as_of")
    memberships_ok = (len(metrics) == 300 and provider.get("original_members") == 300
                      and provider.get("requested_members") == 300 and not provider.get("errors")
                      and member_dates.notna().all() and member_dates.le(current).all())
    publications_ok = publication_dates.notna().all() and publication_dates.le(current).all()
    prices_ok = price_dates.notna().all() and price_dates.le(current).all()
    active = scored["security_eligible"]
    active_prices_today = bool(active.any()) and price_dates.loc[active.index[active]].eq(current).all()
    industries_ok = (metrics[["industry_l1", "industry_l2"]].notna().all().all()
                     and not metrics["industry_l2"].astype(str).str.contains("\ufffd", regex=False).any()
                     and not metrics["industry_l2"].astype(str).str.lower().eq("unknown").any())
    coverage = {"historical_csi300_membership": bool(memberships_ok),
                "qvm_factors": bool(factor_ok),
                "financial_publication_dates": bool(publications_ok),
                "momentum_history": bool(len(eligible)) and eligible[["mom_6_1", "mom_12_1"]].notna().all().all(),
                "ema200": bool(len(eligible)) and eligible["ema200"].notna().all(),
                "latest_price": bool(prices_ok and active_prices_today),
                "security_eligibility": ("tradestatus" in metrics and "is_st" in metrics and
                                         metrics["tradestatus"].notna().all() and
                                         metrics["is_st"].notna().all()),
                "historical_industry": bool(industries_ok),
                "universe_complete": bool(memberships_ok),
                "fundamental_complete": bool(factor_ok and publications_ok),
                "price_complete": bool(prices_ok and active_prices_today),
                "security_state_complete": "tradestatus" in metrics and "is_st" in metrics,
                "corporate_action_complete": False,
                "industry_complete": bool(industries_ok),
                "cross_source_checks": False}
    fallback_count = sum(item.get("fallback_reason") is not None for item in entries)
    warnings = ["official CSI/CNINFO parser unavailable; fallback explicitly recorded",
                "corporate-action and cross-source checks incomplete"]
    return assess_quality(coverage, fallback_count, warnings)


def build_from_public_snapshot(*, signal_date: str, generated_at: datetime,
                               decision: dict, sessions: list[str],
                               metrics: pd.DataFrame, provider: dict,
                               archive: PublicSourceArchive, entries: list[dict],
                               output_root: Path, provenance: dict,
                               allow_dirty: bool = False) -> Path:
    if signal_date not in sessions:
        raise ValueError("signal date absent from public market calendar")
    if provider.get("as_of") != signal_date or provider.get("built_rows") != len(metrics):
        raise ValueError("public provider date or row count mismatch")
    if metrics.ticker.duplicated().any():
        raise ValueError("duplicate public universe ticker")
    for entry in entries:
        if not archive.verify(entry):
            raise ValueError("raw or normalized public source hash mismatch")
    with tempfile.TemporaryDirectory(prefix="v1_selector_") as folder:
        scored, counts = run_selection(metrics, OFFICIAL_STRATEGY.market, signal_date,
                                       Path(folder), provider["market_trend"],
                                       OFFICIAL_STRATEGY.selector_config())
        return20 = provider.get("benchmark", {}).get("return_20d")
        overheated = return20 is not None and return20 > OFFICIAL_STRATEGY.overheat_return_threshold
        plan = build_portfolio_plan(scored, overheated=overheated)
        quality = assess_snapshot(metrics, scored, provider, signal_date, entries)
        if not quality["primary_eligible"]:
            raise ValueError(f"PUBLIC_V1 fail closed: {quality['missing_core_fields']}")
        dates = sorted(set(sessions))
        intended = []
        for row in plan.itertuples():
            execution_index = dates.index(signal_date) + 1 + int(row.entry_delay_sessions)
            if execution_index >= len(dates):
                raise ValueError("public calendar lacks intended execution session")
            intended.append({"ticker": row.ticker, "date": dates[execution_index]})
        files = {"universe.csv": _csv_bytes(metrics[["ticker", "company", "universe_as_of",
                                                     "membership_update_date"]]),
                 "normalized/raw_metrics.csv": _csv_bytes(metrics),
                 "normalized/provider_snapshot.json": json.dumps(provider, ensure_ascii=False,
                                                             sort_keys=True, default=str).encode("utf-8"),
                 "fundamentals.csv": _csv_bytes(metrics[["ticker", "financial_period",
                                                        "fundamental_as_of", "roe", "cfo_to_revenue",
                                                        "eps_growth_std", "earnings_yield",
                                                        "net_cashflow_yield", "book_to_price"]]),
                 "market_snapshot.csv": _csv_bytes(pd.DataFrame([{
                     "signal_date": signal_date, "market_trend": provider["market_trend"],
                     **provider.get("benchmark", {})}])),
                 "candidates.csv": (Path(folder) / "candidates.csv").read_bytes(),
                 "selected.csv": (Path(folder) / "selected.csv").read_bytes(),
                 "actionable.csv": (Path(folder) / "actionable.csv").read_bytes(),
                 "portfolio_plan.csv": _csv_bytes(plan)}
        for entry in entries:
            files[f"raw/{entry['entry_id']}.bin"] = (archive.root / entry["raw_path"]).read_bytes()
            files[f"normalized/{entry['entry_id']}.bin"] = (archive.root / entry["normalized_path"]).read_bytes()
        manifest = {"data_provider_version": DATA_PROVIDER_VERSION,
                    "evidence_label": decision["evidence_label"], "signal_date": signal_date,
                    "sources": entries, "fallback_count": quality["fallback_count"],
                    "coverage": quality}
        signal = {"scheduled_signal_date": decision.get("scheduled_signal_date"),
                  "market_close_verified": decision["prospective_primary"],
                  "market_state": provider["market_trend"],
                  "market_ema_state": provider["market_trend"],
                  "market_20d_return": return20, "overheat_status": overheated,
                  "universe": "CSI300", "eligible_count": int(scored.security_eligible.sum()),
                  "quality_pass_count": counts["counts"]["quality_pass"],
                  "value_pass_count": counts["counts"]["value_pass"],
                  "momentum_pass_count": counts["counts"]["fundamental_candidates"],
                  "actionable_count": counts["counts"]["actionable_candidates"],
                  "selected_tickers": plan.ticker.astype(str).tolist(),
                  "ranking": plan[["ticker", "priority"]].to_dict("records"),
                  "target_weights": plan[["ticker", "target_fraction"]].to_dict("records"),
                  "intended_execution_date": intended,
                  "entry_delay_sessions": OFFICIAL_STRATEGY.overheat_delay_sessions if overheated else 0}
        return create_prediction_package(
            prospective_root=output_root / "prospective",
            retrospective_root=output_root / "retrospective",
            signal_date=signal_date, generated_at=generated_at,
            evidence_label=decision["evidence_label"], files=files,
            source_manifest=manifest, signal=signal, quality=quality,
            provenance=provenance, allow_dirty=allow_dirty)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run PUBLIC_V1 only on a live scheduled market close")
    parser.add_argument("--research-date", help="debug or retrospective date; never auto-promoted to prospective")
    parser.add_argument("--allow-dirty", action="store_true")
    parser.add_argument("--output-root", type=Path, default=BASE / "outputs")
    args = parser.parse_args()
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    today = now.date().isoformat()
    chosen = args.research_date or today
    if date.fromisoformat(chosen).isoformat() != chosen:
        parser.error("research-date must be YYYY-MM-DD")
    if not args.research_date and (now.day < 15 or now.hour < 15):
        print(json.dumps({"status": "skipped", "reason": "before scheduled close"}))
        return
    provenance = git_state(REPO)
    if chosen == today and provenance["dirty"] and not args.allow_dirty:
        parser.error("primary prospective run requires clean git tree")
    archive = PublicSourceArchive(args.output_root / "public_raw")
    entries = []
    month = chosen[:7]
    sessions, metadata = fetch_public_month_sessions(month, archive)
    entries.append(metadata)
    decision = signal_day_decision(now, sessions, args.research_date)
    if chosen == today and not decision["prospective_primary"] and not args.research_date:
        print(json.dumps({"status": "skipped", "reason": decision["reason"]}))
        return
    next_sessions, metadata = fetch_public_month_sessions(next_month(month), archive)
    entries.append(metadata)
    all_sessions = sorted(set(sessions + next_sessions))
    recorder = make_baostock_recorder(archive, entries)
    metrics, provider = build_a_metrics(chosen, raw_recorder=recorder)
    package = build_from_public_snapshot(signal_date=chosen,
                                         generated_at=datetime.now(ZoneInfo("Asia/Shanghai")),
                                         decision=decision, sessions=all_sessions,
                                         metrics=metrics, provider=provider,
                                         archive=archive, entries=entries,
                                         output_root=args.output_root,
                                         provenance=provenance, allow_dirty=args.allow_dirty)
    print(json.dumps({"status": "SEALED", "package": str(package),
                      "evidence_label": decision["evidence_label"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
