"""Reuse the frozen selector, portfolio plan, and exit policy on dated snapshots."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from selector.holding_review import review_holdings
from selector.pipeline import run_selection
from selector.portfolio_plan import build_portfolio_plan
from selector.strategy import OFFICIAL_STRATEGY, OfficialStrategy

from .account import Account
from .engine import DailyBar, Signal


@dataclass
class OfficialSnapshot:
    metrics: pd.DataFrame
    provider: dict


@dataclass
class OfficialSignalProvider:
    snapshots: dict[str, OfficialSnapshot]
    audit_dir: Path
    strategy: OfficialStrategy = OFFICIAL_STRATEGY
    review_state: dict[str, dict] = field(default_factory=dict)
    signal_audit: list[dict] = field(default_factory=list)

    def __call__(self, date: str, account: Account,
                 day: dict[str, DailyBar]) -> list[Signal]:
        snapshot = self.snapshots.get(date)
        if snapshot is None:
            return []
        metadata = snapshot.provider
        if metadata.get("errors") or metadata.get("built_rows") != len(snapshot.metrics):
            raise ValueError("incomplete point-in-time snapshot")
        market_trend = metadata["market_trend"]
        scored, _ = run_selection(
            snapshot.metrics, self.strategy.market, date,
            self.audit_dir / date, market_trend, self.strategy.selector_config(),
        )
        market_return = metadata.get("benchmark", {}).get("return_20d")
        overheated = market_return is not None and market_return > self.strategy.overheat_return_threshold
        plan = build_portfolio_plan(scored, strategy=self.strategy, overheated=overheated)
        signals: list[Signal] = []
        if account.positions:
            self.review_state = {
                ticker: state for ticker, state in self.review_state.items()
                if ticker in account.positions
                and state.get("_entry_date") == account.positions[ticker].entry_date
            }
            holdings = pd.DataFrame([{
                "ticker": ticker, "entry_date": position.entry_date,
                "entry_price": position.average_cost, "quantity": position.quantity,
                **{key: value for key, value in self.review_state.get(ticker, {}).items()
                   if not key.startswith("_")},
            } for ticker, position in account.positions.items()])
            quotes = {ticker: {"price": day[ticker].close, "price_as_of": date,
                               "price_basis": "unadjusted", "basis_changed": False,
                               "tradestatus": "1" if day[ticker].tradable else "0"}
                      for ticker in account.positions if ticker in day}
            review = review_holdings(
                holdings, scored, date, market_trend,
                strategy=self.strategy,
                member_codes=metadata.get("member_codes"), quotes=quotes,
                expected_price_date=date,
            )
            for row in review.to_dict("records"):
                ticker = row["ticker"]
                self.review_state[ticker] = {
                    "_entry_date": account.positions[ticker].entry_date,
                    "last_review_date": row["state_review_date"],
                    "nonselected_streak": row["nonselected_streak"],
                    "below_ema_streak": row["below_ema_streak"],
                    "last_action": row["action"], "last_reason": row["reason"],
                    "signal_date": row["signal_date"],
                }
                if row["action"] == "exit":
                    signals.append(Signal(date, ticker, "sell", reason=row["reason"]))
            review.to_csv(self.audit_dir / date / "holding_review.csv", index=False)
        for row in plan.to_dict("records"):
            ticker = row["ticker"]
            if ticker not in account.positions and ticker in day:
                signals.append(Signal(
                    date, ticker, "buy", target_fraction=float(row["target_fraction"]),
                    reason="official V1 Top 5", delay_sessions=int(row["entry_delay_sessions"]),
                ))
        self.signal_audit.append({"signal_date": date, "market_trend": market_trend,
                                  "overheated": overheated, "planned_buys": sum(s.side == "buy" for s in signals),
                                  "planned_exits": sum(s.side == "sell" for s in signals)})
        return signals
