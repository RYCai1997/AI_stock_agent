"""Cash-and-shares ledger. Realized P&L is never added twice to equity."""

from __future__ import annotations

from dataclasses import dataclass, field
from math import isfinite


@dataclass
class Position:
    quantity: int = 0
    average_cost: float = 0.0
    stop_reference_price: float = 0.0
    entry_date: str | None = None


@dataclass
class Account:
    initial_cash: float
    cash: float = field(init=False)
    positions: dict[str, Position] = field(default_factory=dict)
    realized_pnl: float = 0.0
    fees: float = 0.0
    dividends: float = 0.0

    def __post_init__(self) -> None:
        if not isfinite(self.initial_cash) or self.initial_cash <= 0:
            raise ValueError("initial_cash must be positive and finite")
        self.cash = float(self.initial_cash)

    def buy(self, ticker: str, quantity: int, price: float, fee: float = 0.0,
            date: str | None = None) -> None:
        self._validate_trade(quantity, price, fee)
        cost = quantity * price + fee
        if cost > self.cash + 1e-8:
            raise ValueError("insufficient cash")
        position = self.positions.setdefault(ticker, Position())
        old_basis = position.quantity * position.average_cost
        old_stop_basis = position.quantity * position.stop_reference_price
        position.quantity += quantity
        position.average_cost = (old_basis + cost) / position.quantity
        position.stop_reference_price = (old_stop_basis + quantity * price) / position.quantity
        if position.entry_date is None:
            position.entry_date = date
        self.cash -= cost
        self.fees += fee

    def sell(self, ticker: str, quantity: int, price: float, fee: float = 0.0) -> None:
        self._validate_trade(quantity, price, fee)
        position = self.positions.get(ticker)
        if position is None or position.quantity < quantity:
            raise ValueError("insufficient shares")
        proceeds = quantity * price - fee
        self.cash += proceeds
        self.realized_pnl += proceeds - quantity * position.average_cost
        self.fees += fee
        position.quantity -= quantity
        if not position.quantity:
            del self.positions[ticker]

    @staticmethod
    def _validate_trade(quantity: int, price: float, fee: float) -> None:
        if type(quantity) is not int or quantity <= 0 or not isfinite(price) or price <= 0:
            raise ValueError("quantity and price must be positive")
        if not isfinite(fee) or fee < 0:
            raise ValueError("fee must be nonnegative and finite")

    def mark(self, prices: dict[str, float]) -> dict[str, float]:
        missing = set(self.positions) - set(prices)
        if missing:
            raise ValueError(f"missing valuation prices: {sorted(missing)}")
        market_value = 0.0
        unrealized = 0.0
        for ticker, position in self.positions.items():
            price = prices[ticker]
            if not isfinite(price) or price <= 0:
                raise ValueError(f"invalid valuation price: {ticker}")
            market_value += position.quantity * price
            unrealized += position.quantity * (price - position.average_cost)
        equity = self.cash + market_value
        if abs(equity - (self.cash + market_value)) > 1e-8:
            raise AssertionError("accounting identity failed")
        return {"cash": self.cash, "market_value": market_value, "total_equity": equity,
                "realized_pnl": self.realized_pnl, "unrealized_pnl": unrealized,
                "fees": self.fees, "dividends": self.dividends,
                "actual_exposure": market_value / equity if equity else 0.0,
                "daily_nav": equity / self.initial_cash}
