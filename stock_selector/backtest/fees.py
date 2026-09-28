"""Explicit, date-bounded transaction cost assumptions."""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite


@dataclass(frozen=True)
class FeeSchedule:
    effective_from: str
    effective_to: str | None
    commission_rate: float
    minimum_commission: float
    stamp_duty: float
    transfer_fee: float

    def __post_init__(self) -> None:
        if self.effective_to is not None and self.effective_to < self.effective_from:
            raise ValueError("effective_to precedes effective_from")
        for value in (self.commission_rate, self.minimum_commission,
                      self.stamp_duty, self.transfer_fee):
            if not isfinite(value) or value < 0:
                raise ValueError("fee components must be nonnegative and finite")

    def includes(self, date: str) -> bool:
        return self.effective_from <= date and (self.effective_to is None or date <= self.effective_to)


@dataclass(frozen=True)
class FeeBreakdown:
    commission: float
    stamp_duty: float
    transfer_fee: float

    @property
    def total(self) -> float:
        return self.commission + self.stamp_duty + self.transfer_fee


@dataclass(frozen=True)
class FeeModel:
    schedules: tuple[FeeSchedule, ...]
    slippage: float = 0.0

    def __post_init__(self) -> None:
        if not self.schedules or not isfinite(self.slippage) or not 0 <= self.slippage < 1:
            raise ValueError("a fee schedule and valid slippage are required")
        ordered = sorted(self.schedules, key=lambda item: item.effective_from)
        for left, right in zip(ordered, ordered[1:]):
            if left.effective_to is None or left.effective_to >= right.effective_from:
                raise ValueError("fee schedule date ranges overlap")

    def schedule_for(self, date: str) -> FeeSchedule:
        matches = [schedule for schedule in self.schedules if schedule.includes(date)]
        if len(matches) != 1:
            raise ValueError(f"no unique fee schedule for {date}")
        return matches[0]

    def execution_price(self, raw_price: float, side: str) -> float:
        if side not in {"buy", "sell"} or not isfinite(raw_price) or raw_price <= 0:
            raise ValueError("invalid execution price or side")
        return raw_price * (1 + self.slippage if side == "buy" else 1 - self.slippage)

    def fee(self, date: str, side: str, quantity: int, price: float) -> FeeBreakdown:
        if side not in {"buy", "sell"} or quantity <= 0 or price <= 0:
            raise ValueError("invalid trade")
        schedule = self.schedule_for(date)
        notional = quantity * price
        return FeeBreakdown(
            commission=max(schedule.minimum_commission, notional * schedule.commission_rate),
            stamp_duty=notional * schedule.stamp_duty if side == "sell" else 0.0,
            transfer_fee=notional * schedule.transfer_fee,
        )


ZERO_COST_MODEL = FeeModel((FeeSchedule("1900-01-01", None, 0, 0, 0, 0),))
