"""Conservative A-share fill checks without order-book data."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LimitContext:
    previous_close: float | None = None
    explicit_limit_up: float | None = None
    explicit_limit_down: float | None = None
    is_st: bool = False


def limit_fraction(ticker: str, date: str, is_st: bool = False) -> float:
    if is_st:
        return 0.05
    if ticker.startswith("sh.688"):
        return 0.20
    if ticker.startswith("sz.30") and date >= "2020-08-24":
        return 0.20
    if ticker.startswith("bj."):
        return 0.30
    return 0.10


def fill_block_reason(side: str, *, date: str, ticker: str, open_price: float,
                      tradable: bool, context: LimitContext) -> str | None:
    if not tradable:
        return "suspension"
    fraction = limit_fraction(ticker, date, context.is_st)
    upper = context.explicit_limit_up
    lower = context.explicit_limit_down
    if context.previous_close is not None:
        upper = upper if upper is not None else round(context.previous_close * (1 + fraction) + 1e-8, 2)
        lower = lower if lower is not None else round(context.previous_close * (1 - fraction) + 1e-8, 2)
    if side == "buy" and upper is not None and open_price >= upper - 1e-8:
        return "limit_up_buy_block"
    if side == "sell" and lower is not None and open_price <= lower + 1e-8:
        return "limit_down_sell_block"
    return None


def stop_execution_price(open_price: float, low_price: float, planned_stop: float) -> tuple[float, float] | None:
    """Return raw fill price and per-share gap loss before slippage."""
    if open_price < planned_stop:
        return open_price, planned_stop - open_price
    if low_price <= planned_stop <= open_price:
        return planned_stop, 0.0
    return None
