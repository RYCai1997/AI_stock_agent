"""Account treatment for dated corporate actions on unadjusted shares."""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite

from .account import Account


@dataclass(frozen=True)
class CorporateAction:
    date: str
    ticker: str
    kind: str
    cash_per_share: float = 0.0
    share_ratio: float = 0.0
    subscription_price: float | None = None
    exercise_rights: bool = False


def apply_corporate_action(account: Account, action: CorporateAction) -> dict:
    position = account.positions.get(action.ticker)
    event = {"date": action.date, "ticker": action.ticker, "kind": action.kind,
             "status": "applied", "manual_audit_required": False,
             "data_confidence_degraded": False, "detail": ""}
    if position is None:
        event["status"] = "not_held"
        return event
    if action.kind == "dividend":
        if not isfinite(action.cash_per_share) or action.cash_per_share < 0:
            raise ValueError("invalid cash dividend")
        amount = position.quantity * action.cash_per_share
        account.cash += amount
        account.dividends += amount
        event["cash_amount"] = amount
    elif action.kind in {"bonus", "conversion", "split"}:
        if not isfinite(action.share_ratio) or action.share_ratio <= -1:
            raise ValueError("invalid share ratio")
        new_quantity = position.quantity * (1 + action.share_ratio)
        if abs(new_quantity - round(new_quantity)) > 1e-8 or new_quantity <= 0:
            return {**event, "status": "unsupported", "manual_audit_required": True,
                    "data_confidence_degraded": True,
                    "detail": "fractional share or invalid post-action quantity"}
        old_basis = position.quantity * position.average_cost
        position.quantity = round(new_quantity)
        position.average_cost = old_basis / position.quantity
        event["new_quantity"] = position.quantity
    elif action.kind == "rights":
        if not action.exercise_rights:
            return {**event, "status": "unsupported", "manual_audit_required": True,
                    "data_confidence_degraded": True,
                    "detail": "rights subscription requires an explicit exercise decision"}
        if (not isfinite(action.share_ratio) or action.share_ratio <= 0
                or action.subscription_price is None
                or not isfinite(action.subscription_price) or action.subscription_price <= 0):
            raise ValueError("invalid rights terms")
        new_shares = position.quantity * action.share_ratio
        if abs(new_shares - round(new_shares)) > 1e-8:
            return {**event, "status": "unsupported", "manual_audit_required": True,
                    "data_confidence_degraded": True, "detail": "fractional rights shares"}
        cost = round(new_shares) * action.subscription_price
        if cost > account.cash:
            return {**event, "status": "unsupported", "manual_audit_required": True,
                    "data_confidence_degraded": True, "detail": "insufficient cash for rights"}
        old_basis = position.quantity * position.average_cost
        account.cash -= cost
        position.quantity += round(new_shares)
        position.average_cost = (old_basis + cost) / position.quantity
        event["new_quantity"] = position.quantity
        event["cash_amount"] = -cost
    elif action.kind == "delisting":
        return {**event, "status": "unsupported", "manual_audit_required": True,
                "data_confidence_degraded": True,
                "detail": "delisting settlement and recovery value require source verification"}
    else:
        return {**event, "status": "unsupported", "manual_audit_required": True,
                "data_confidence_degraded": True, "detail": "unknown corporate action"}
    return event


def data_quality_report(events: list[dict]) -> str:
    degraded = [event for event in events if event.get("data_confidence_degraded")]
    lines = ["# Data quality report", "", f"Corporate actions reviewed: {len(events)}",
             f"Manual audit required: {len(degraded)}", ""]
    if degraded:
        lines += ["Simulation confidence is degraded until these events are reconciled:", ""]
        for event in degraded:
            lines.append(f"- {event['date']} {event['ticker']} {event['kind']}: {event['detail']}")
    else:
        lines.append("No unsupported corporate actions were recorded in the supplied event stream.")
    lines += ["", "This report only covers supplied events. Missing source events cannot be detected here."]
    return "\n".join(lines) + "\n"
