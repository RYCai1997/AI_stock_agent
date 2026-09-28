"""Describe how much closed-trade profit comes from a few winners or windows."""

from __future__ import annotations

import pandas as pd


def closed_trade_contributions(trades: list[dict]) -> pd.DataFrame:
    entries: dict[str, dict] = {}
    rows = []
    for trade in trades:
        ticker = trade["ticker"]
        if trade["side"] == "buy":
            entries[ticker] = trade
        elif trade["side"] == "sell" and ticker in entries:
            entry = entries.pop(ticker)
            pnl = trade.get("realized_pnl")
            if pnl is None:
                pnl = (trade["quantity"] * trade["execution_price"] - trade.get("fee", 0)
                       - entry["quantity"] * entry["execution_price"] - entry.get("fee", 0))
            rows.append({"ticker": ticker, "window": entry["signal_date"],
                         "entry_date": entry["actual_execution_date"],
                         "exit_date": trade["actual_execution_date"], "realized_pnl": float(pnl)})
    return pd.DataFrame(rows, columns=["ticker", "window", "entry_date", "exit_date", "realized_pnl"])


def winner_concentration(trades: list[dict]) -> pd.DataFrame:
    closed = closed_trade_contributions(trades)
    winners = closed.loc[closed["realized_pnl"].gt(0)]
    positive_total = float(winners["realized_pnl"].sum())
    net_total = float(closed["realized_pnl"].sum())
    rows = [{"metric": "total_profits_from_winners", "value": positive_total,
             "denominator": "currency", "closed_trades": len(closed)}]
    trade_profits = winners["realized_pnl"].sort_values(ascending=False)
    window_profits = winners.groupby("window")["realized_pnl"].sum().sort_values(ascending=False)
    for count in (1, 3, 5):
        contribution = float(trade_profits.head(count).sum())
        rows.append({"metric": f"top_{count}_trade_share_of_winner_profits",
                     "value": contribution / positive_total if positive_total else float("nan"),
                     "denominator": "winner profits", "closed_trades": len(closed)})
    for count in (1, 3):
        contribution = float(window_profits.head(count).sum())
        rows.append({"metric": f"top_{count}_window_share_of_winner_profits",
                     "value": contribution / positive_total if positive_total else float("nan"),
                     "denominator": "winner profits", "closed_trades": len(closed)})
    rows.append({"metric": "top_1_trade_share_of_net_realized",
                 "value": float(trade_profits.head(1).sum()) / net_total if net_total else float("nan"),
                 "denominator": "net realized pnl", "closed_trades": len(closed)})
    return pd.DataFrame(rows)
