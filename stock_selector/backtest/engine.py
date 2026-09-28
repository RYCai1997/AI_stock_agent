"""Daily event loop: prior-close intent, subsequent-open fill, then mark-to-market."""

from __future__ import annotations

from dataclasses import dataclass, field
from math import floor, isfinite

from .account import Account
from .execution import LimitContext, fill_block_reason, stop_execution_price
from selector.strategy import OFFICIAL_STRATEGY


@dataclass(frozen=True)
class DailyBar:
    date: str
    ticker: str
    open: float
    high: float
    low: float
    close: float
    tradable: bool = True
    limit_up: float | None = None
    limit_down: float | None = None
    is_st: bool = False


@dataclass(frozen=True)
class Signal:
    signal_date: str
    ticker: str
    side: str
    target_fraction: float = 0.0
    reason: str = "monthly selection"


@dataclass
class Order:
    signal_date: str
    intended_execution_date: str
    ticker: str
    side: str
    quantity: int
    signal_price: float
    reason: str
    status: str = "pending"
    actual_execution_date: str | None = None
    execution_price: float | None = None
    execution_block_reason: str | None = None
    planned_stop: float | None = None
    gap_loss: float = 0.0


@dataclass
class BacktestEngine:
    initial_cash: float
    account: Account = field(init=False)
    orders: list[Order] = field(default_factory=list)
    trades: list[dict] = field(default_factory=list)
    daily_nav: list[dict] = field(default_factory=list)
    positions: list[dict] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.account = Account(self.initial_cash)

    def run(self, bars: list[DailyBar], signals: list[Signal]) -> "BacktestEngine":
        by_date: dict[str, dict[str, DailyBar]] = {}
        for bar in bars:
            if any(not isfinite(x) or x <= 0 for x in (bar.open, bar.high, bar.low, bar.close)):
                raise ValueError("unadjusted OHLC must be positive and finite")
            if bar.ticker in by_date.setdefault(bar.date, {}):
                raise ValueError("duplicate daily bar")
            by_date[bar.date][bar.ticker] = bar
        dates = sorted(by_date)
        if not dates:
            raise ValueError("no trading dates")
        signal_map: dict[str, list[Signal]] = {}
        for signal in signals:
            if signal.signal_date not in by_date:
                raise ValueError("signal_date has no trading session")
            if signal.side not in {"buy", "sell"} or not 0 <= signal.target_fraction <= 1:
                raise ValueError("invalid signal")
            signal_map.setdefault(signal.signal_date, []).append(signal)
        for index, date in enumerate(dates):
            day = by_date[date]
            previous = by_date[dates[index - 1]] if index else {}
            for order in self.orders:
                if order.status != "pending" or order.intended_execution_date > date:
                    continue
                bar = day.get(order.ticker)
                if bar is None:
                    order.execution_block_reason = "no_tradable_bar"
                    continue
                block = fill_block_reason(
                    order.side, date=date, ticker=order.ticker, open_price=bar.open,
                    tradable=bar.tradable,
                    context=LimitContext(
                        previous_close=previous[order.ticker].close if order.ticker in previous else None,
                        explicit_limit_up=bar.limit_up, explicit_limit_down=bar.limit_down,
                        is_st=bar.is_st,
                    ),
                )
                if block:
                    order.execution_block_reason = block
                    continue
                if order.side == "buy":
                    if order.quantity * bar.open > self.account.cash + 1e-8:
                        order.status = "cancelled"
                        order.execution_block_reason = "insufficient_cash"
                        continue
                    self.account.buy(order.ticker, order.quantity, bar.open)
                else:
                    self.account.sell(order.ticker, order.quantity, bar.open)
                order.status = "filled"
                order.actual_execution_date = date
                order.execution_price = bar.open
                order.execution_block_reason = None
                self.trades.append({"ticker": order.ticker, "side": order.side,
                                    "signal_date": order.signal_date,
                                    "intended_execution_date": order.intended_execution_date,
                                    "actual_execution_date": date,
                                    "signal_price": order.signal_price,
                                    "execution_price": bar.open, "quantity": order.quantity,
                                    "planned_stop": order.planned_stop,
                                    "gap_loss": order.gap_loss})
            for ticker, position in list(self.account.positions.items()):
                if any(o.status == "pending" and o.side == "sell" and o.ticker == ticker for o in self.orders):
                    continue
                bar = day.get(ticker)
                if bar is None:
                    continue
                planned_stop = position.average_cost * (1 - OFFICIAL_STRATEGY.stop_loss_fraction)
                trigger = stop_execution_price(bar.open, bar.low, planned_stop)
                if trigger is None:
                    continue
                execution_price, gap_loss = trigger
                order = Order(date, date, ticker, "sell", position.quantity,
                              planned_stop, "stop loss", planned_stop=planned_stop,
                              gap_loss=gap_loss * position.quantity)
                self.orders.append(order)
                block = fill_block_reason(
                    "sell", date=date, ticker=ticker, open_price=bar.open,
                    tradable=bar.tradable,
                    context=LimitContext(
                        previous_close=previous[ticker].close if ticker in previous else None,
                        explicit_limit_up=bar.limit_up, explicit_limit_down=bar.limit_down,
                        is_st=bar.is_st,
                    ),
                )
                if block:
                    order.execution_block_reason = block
                    continue
                filled_quantity = position.quantity
                self.account.sell(ticker, filled_quantity, execution_price)
                order.status = "filled"
                order.actual_execution_date = date
                order.execution_price = execution_price
                self.trades.append({"ticker": ticker, "side": "sell",
                                    "signal_date": date, "intended_execution_date": date,
                                    "actual_execution_date": date,
                                    "signal_price": planned_stop,
                                    "execution_price": execution_price,
                                    "quantity": filled_quantity,
                                    "planned_stop": planned_stop, "gap_loss": order.gap_loss})
            prices = {ticker: bar.close for ticker, bar in day.items()}
            snapshot = self.account.mark(prices)
            self.daily_nav.append({"date": date, **snapshot,
                                   "pending_orders": sum(o.status == "pending" for o in self.orders)})
            for ticker, position in self.account.positions.items():
                self.positions.append({"date": date, "ticker": ticker,
                                       "quantity": position.quantity,
                                       "average_cost": position.average_cost,
                                       "valuation_price": prices[ticker],
                                       "market_value": position.quantity * prices[ticker]})
            if index + 1 == len(dates):
                continue
            next_date = dates[index + 1]
            for signal in signal_map.get(date, []):
                if signal.ticker not in day:
                    raise ValueError("signal has no point-in-time price")
                signal_price = day[signal.ticker].close
                if signal.side == "buy":
                    budget = snapshot["total_equity"] * signal.target_fraction
                    quantity = floor(budget / signal_price / 100) * 100
                    if signal.ticker in self.account.positions or quantity == 0:
                        continue
                else:
                    position = self.account.positions.get(signal.ticker)
                    if position is None:
                        continue
                    quantity = position.quantity
                self.orders.append(Order(date, next_date, signal.ticker, signal.side,
                                         quantity, signal_price, signal.reason))
        return self
