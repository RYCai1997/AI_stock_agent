from __future__ import annotations

import unittest

from backtest.concentration import closed_trade_contributions, winner_concentration


class ConcentrationTests(unittest.TestCase):
    def test_winner_shares_use_positive_profit_denominator(self):
        trades = []
        for ticker, window, pnl in (("A", "2025-01-02", 100),
                                    ("B", "2025-01-02", 50),
                                    ("C", "2025-02-03", -50)):
            trades += [
                {"ticker": ticker, "side": "buy", "signal_date": window,
                 "actual_execution_date": window, "quantity": 100,
                 "execution_price": 10, "fee": 0},
                {"ticker": ticker, "side": "sell", "signal_date": window,
                 "actual_execution_date": "2025-03-03", "quantity": 100,
                 "execution_price": 10 + pnl / 100, "fee": 0,
                 "realized_pnl": pnl},
            ]
        closed = closed_trade_contributions(trades)
        self.assertEqual(closed.realized_pnl.tolist(), [100, 50, -50])
        table = winner_concentration(trades).set_index("metric")
        self.assertEqual(table.loc["total_profits_from_winners", "value"], 150)
        self.assertAlmostEqual(table.loc["top_1_trade_share_of_winner_profits", "value"], 2 / 3)
        self.assertEqual(table.loc["top_1_window_share_of_winner_profits", "value"], 1)
