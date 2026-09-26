"""Official entry point for the frozen A-share selector strategy."""

from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

import pandas as pd

from selector.pipeline import run_selection
from selector.holding_review import REVIEW_COLUMNS, review_holdings
from selector.portfolio_plan import build_portfolio_plan
from selector.providers import build_a_metrics
from selector.providers.a_baostock import fetch_holding_quotes
from selector.account_guidance import build_account_guidance
from selector.strategy import OFFICIAL_STRATEGY

BASE_DIR = Path(__file__).resolve().parent


def main() -> None:
    parser = argparse.ArgumentParser(description="Run frozen CSI300 Q/V/M timing strategy")
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {OFFICIAL_STRATEGY.strategy_version}",
    )
    parser.add_argument("--as-of", default=str(date.today()))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--cache-dir", type=Path)
    parser.add_argument("--holdings", type=Path, help="Optional holdings CSV for monthly review")
    parser.add_argument("--mode", choices=["research", "current"], default="research")
    parser.add_argument("--account", type=Path)
    args = parser.parse_args()
    if args.mode == "current" and args.as_of != str(date.today()):
        parser.error("current mode requires today's date")
    output = args.output or BASE_DIR / "outputs" / "official" / args.as_of
    cache = args.cache_dir or BASE_DIR / "outputs" / "official_provider_cache"

    metrics, provider = build_a_metrics(args.as_of, cache_dir=cache)
    output.mkdir(parents=True, exist_ok=True)
    metrics.to_csv(output / "raw_metrics.csv", index=False)
    scored, selector = run_selection(
        metrics,
        OFFICIAL_STRATEGY.market,
        args.as_of,
        output,
        provider["market_trend"],
        OFFICIAL_STRATEGY.selector_config(),
    )
    market_return_20d = provider.get("benchmark", {}).get("return_20d")
    overheated = (
        market_return_20d is not None
        and market_return_20d > OFFICIAL_STRATEGY.overheat_return_threshold
    )
    entry_delay_sessions = (
        OFFICIAL_STRATEGY.overheat_delay_sessions if overheated else 0
    )
    plan = build_portfolio_plan(scored, overheated=overheated)
    holdings = pd.read_csv(args.holdings) if args.holdings else pd.DataFrame(columns=["ticker", "entry_date", "entry_price", "quantity"])
    price_date = provider.get("benchmark", {}).get("price_as_of")
    quote_inputs = holdings.copy()
    candidate_inputs = pd.DataFrame([{"ticker": code, "entry_date": price_date}
                                    for code in plan["ticker"] if code not in set(holdings["ticker"])])
    quote_inputs = pd.concat([quote_inputs, candidate_inputs], ignore_index=True)
    quotes = fetch_holding_quotes(quote_inputs, args.as_of)
    plan["execution_price"] = plan["ticker"].map(lambda c: quotes.get(c, {}).get("price"))
    plan["execution_price_as_of"] = plan["ticker"].map(lambda c: quotes.get(c, {}).get("price_as_of"))
    plan.to_csv(output / "portfolio_plan.csv", index=False)
    if args.holdings:
        holding_review = review_holdings(
            holdings, scored, args.as_of, provider["market_trend"],
            member_codes=provider["member_codes"], quotes=quotes,
            expected_price_date=provider.get("benchmark", {}).get("price_as_of"),
        )
    else:
        holding_review = pd.DataFrame(columns=REVIEW_COLUMNS)
    holding_review.to_csv(output / "holding_review.csv", index=False)
    account = json.loads(args.account.read_text(encoding="utf-8")) if args.account else None
    guidance = build_account_guidance(plan, holdings, scored, quotes, account, args.as_of, price_date)
    if provider["errors"]:
        guidance["suggested_quantity"] = 0
        guidance["estimated_amount"] = 0.0
        guidance["estimated_stop_risk"] = 0.0
        guidance["guidance"] = "股票池数据不完整，请修复数据后重新生成建仓数量"
    guidance.to_csv(output / "account_guidance.csv", index=False)
    metadata = {
        "mode": args.mode,
        "account": account,
        "quote_errors": {c: q["error"] for c, q in quotes.items() if "error" in q},
        "strategy": OFFICIAL_STRATEGY.to_dict(),
        "provider": provider,
        "selector": selector,
        "plan": {
            "ideas": len(plan),
            "planned_new_exposure": float(plan["target_fraction"].sum()) if len(plan) else 0.0,
            "market_return_20d": market_return_20d,
            "overheated": overheated,
            "entry_delay_sessions": entry_delay_sessions,
            "orders_placed": 0,
        },
        "holding_review": {
            "input": str(args.holdings.resolve()) if args.holdings else None,
            "holdings_reviewed": len(holding_review),
            "exit_signals": int(holding_review["action"].eq("exit").sum()) if len(holding_review) else 0,
            "orders_placed": 0,
        },
    }
    (output / "official_run_metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps({
        "strategy_id": OFFICIAL_STRATEGY.strategy_id,
        "as_of": args.as_of,
        "market_trend": provider["market_trend"],
        "actionable_candidates": selector["counts"]["actionable_candidates"],
        "portfolio_plan_ideas": len(plan),
        "planned_new_exposure": metadata["plan"]["planned_new_exposure"],
        "market_return_20d": market_return_20d,
        "overheated": overheated,
        "entry_delay_sessions": entry_delay_sessions,
        "holdings_reviewed": len(holding_review),
        "output": str(output.resolve()),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
