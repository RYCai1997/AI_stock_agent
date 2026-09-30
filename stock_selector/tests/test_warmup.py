from __future__ import annotations

import unittest

import pandas as pd

from data_history.warmup import audit_warmup


class WarmupTests(unittest.TestCase):
    def test_sufficient_prices_and_published_eps(self):
        dates = pd.bdate_range("2018-01-01", "2020-03-16")
        prices = pd.DataFrame({"ticker": "sh.600000", "date": dates.strftime("%Y-%m-%d"),
                               "close": 10.0})
        filings = pd.DataFrame([{"ticker": "sh.600000", "statDate": f"{year}-12-31",
                                 "pubDate": f"{year+1}-03-01", "epsTTM": 1.0 + year / 1000}
                                for year in range(2014, 2019)])
        result = audit_warmup(prices, filings, ["sh.600000"])
        self.assertEqual(result.iloc[0].status, "ready")
        self.assertGreaterEqual(result.iloc[0].adjusted_sessions, 250)

    def test_future_filing_and_short_price_history_fail_closed(self):
        prices = pd.DataFrame([{"ticker": "sh.600000", "date": "2020-03-16", "close": 10}])
        filings = pd.DataFrame([{"ticker": "sh.600000", "statDate": "2019-12-31",
                                 "pubDate": "2020-04-01", "epsTTM": 1}])
        result = audit_warmup(prices, filings, ["sh.600000"])
        self.assertEqual(result.iloc[0].status, "fail_closed")
        self.assertIn("fewer_than_five_published_annual_eps", result.iloc[0].reasons)
        self.assertEqual(result.iloc[0].annual_eps_count, 0)
