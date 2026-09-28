"""Performance calculations from daily account equity, never node averages."""

from __future__ import annotations

import math
from datetime import date

import numpy as np
import pandas as pd


def performance(daily_nav: list[dict], trades: list[dict]) -> dict:
    if not daily_nav:
        raise ValueError("daily NAV is empty")
    frame = pd.DataFrame(daily_nav).sort_values("date")
    equity = frame["total_equity"].astype(float)
    returns = equity.pct_change().dropna()
    years = max((date.fromisoformat(frame.iloc[-1]["date"]) -
                 date.fromisoformat(frame.iloc[0]["date"])).days / 365.25, 1 / 252)
    cumulative = equity.iloc[-1] / equity.iloc[0] - 1
    cagr = (equity.iloc[-1] / equity.iloc[0]) ** (1 / years) - 1
    vol = returns.std(ddof=1) * math.sqrt(252) if len(returns) > 1 else float("nan")
    drawdown = equity / equity.cummax() - 1
    downside = returns.clip(upper=0)
    downside_dev = math.sqrt(float((downside ** 2).mean())) * math.sqrt(252) if len(returns) else float("nan")
    annual_mean = float(returns.mean()) * 252 if len(returns) else float("nan")
    sharpe = annual_mean / vol if vol and math.isfinite(vol) else float("nan")
    sortino = annual_mean / downside_dev if downside_dev and math.isfinite(downside_dev) else float("nan")
    max_drawdown = float(drawdown.min())
    calmar = cagr / abs(max_drawdown) if max_drawdown < 0 else float("nan")

    # One full-position sale closes one lot; partial exits are not used by V1.
    open_lots: dict[str, dict] = {}
    closed: list[dict] = []
    for trade in trades:
        ticker = trade["ticker"]
        if trade["side"] == "buy":
            open_lots[ticker] = trade
        elif ticker in open_lots:
            buy = open_lots.pop(ticker)
            buy_cost = buy["quantity"] * buy["execution_price"] + buy.get("fee", 0)
            proceeds = trade["quantity"] * trade["execution_price"] - trade.get("fee", 0)
            closed.append({"pnl": trade.get("realized_pnl")
                           if trade.get("realized_pnl") is not None else proceeds - buy_cost,
                           "holding_days": (date.fromisoformat(trade["actual_execution_date"]) -
                                            date.fromisoformat(buy["actual_execution_date"])).days})
    winners = [item["pnl"] for item in closed if item["pnl"] > 0]
    losers = [item["pnl"] for item in closed if item["pnl"] < 0]
    bought_notional = sum(t["quantity"] * t["execution_price"] for t in trades if t["side"] == "buy")
    sold_notional = sum(t["quantity"] * t["execution_price"] for t in trades if t["side"] == "sell")
    return {
        "cumulative_return": float(cumulative), "cagr": float(cagr),
        "annualized_volatility": float(vol), "maximum_drawdown": max_drawdown,
        "sharpe": float(sharpe), "sortino": float(sortino), "calmar": float(calmar),
        "turnover": float((bought_notional + sold_notional) / (2 * equity.mean())),
        "win_rate": len(winners) / len(closed) if closed else float("nan"),
        "profit_loss_ratio": (sum(winners) / len(winners)) / abs(sum(losers) / len(losers))
        if winners and losers else float("nan"),
        "average_holding_days": float(np.mean([item["holding_days"] for item in closed])) if closed else float("nan"),
        "average_exposure": float(frame["actual_exposure"].mean()),
        "realized_pnl": float(frame.iloc[-1]["realized_pnl"]),
        "unrealized_pnl": float(frame.iloc[-1]["unrealized_pnl"]),
        "total_transaction_costs": float(frame.iloc[-1]["fees"]),
        "closed_trades": len(closed),
    }
