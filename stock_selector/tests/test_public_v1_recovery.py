from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from selector.providers.a_baostock import (_eps_stability, _latest_quality,
                                            _session_error, build_a_metrics)


class PublicV1RecoveryTests(unittest.TestCase):
    def test_sdk_auth_and_network_error_codes_are_retryable(self):
        self.assertTrue(_session_error(RuntimeError("Baostock 10001001: 用户未登陆")))
        self.assertTrue(_session_error(RuntimeError("Baostock 10002007: 网络接收错误")))
        self.assertFalse(_session_error(RuntimeError("Baostock 10004011: invalid code")))

    def test_audited_code_succession_recovers_annual_eps_without_future_reports(self):
        calls = []
        def captured(_result, endpoint, params, recorder=None):
            calls.append((endpoint, dict(params)))
            code = params.get("code")
            if endpoint == "query_stock_basic":
                return [{"code": code, "ipoDate": "2010-08-27",
                         "outDate": "2025-02-17" if code == "sz.300114" else ""}]
            if endpoint == "query_profit_data" and params["quarter"] == 4:
                year = params["year"]
                if (code == "sz.302132" and year in (2024, 2025) or
                        code == "sz.300114" and 2020 <= year <= 2023):
                    return [{"code": code, "statDate": f"{year}-12-31",
                             "pubDate": f"{year + 1}-03-01", "epsTTM": str(year - 2019)}]
            if endpoint == "query_profit_data" and params["quarter"] == 2:
                return [{"code": code, "statDate": "2026-06-30",
                         "pubDate": "2026-08-01", "roeAvg": "0.1"}]
            if endpoint == "query_cash_flow_data" and params["quarter"] == 2:
                return [{"code": code, "statDate": "2026-06-30",
                         "pubDate": "2026-08-01", "CFOToOR": "0.1"}]
            return []
        bs = SimpleNamespace(query_profit_data=Mock(), query_cash_flow_data=Mock(),
                             query_stock_basic=Mock())
        with patch("selector.providers.a_baostock._captured_rows", side_effect=captured):
            _, annual = _latest_quality(bs, "sz.302132", "2026-09-30")
        self.assertEqual(len(annual), 6)
        self.assertEqual({row["code"] for row in annual}, {"sz.300114", "sz.302132"})
        self.assertIsNotNone(_eps_stability(annual))
        self.assertTrue(all(row["pubDate"] <= "2026-09-30" for row in annual))
        self.assertTrue(any(params.get("code") == "sz.300114" for _, params in calls))

    def test_auth_failure_restarts_ticker_and_discards_failed_source_tables(self):
        class Table(list):
            fields = ["code", "industry"]
        code = "sh.600000"
        universe = [{"code": code, "code_name": "Test", "updateDate": "2026-09-30"}]
        bs = SimpleNamespace(
            login=Mock(return_value=SimpleNamespace(error_code="0")), logout=Mock(),
            query_hs300_stocks=Mock(return_value=Table(universe)),
            query_stock_industry=Mock(return_value=Table([{"code": code, "industry": "C制造业"}])),
        )
        quality = {"profit": {"pubDate": "2026-08-01", "roeAvg": "0.1"},
                   "cash": {"pubDate": "2026-08-01", "CFOToOR": "0.1"},
                   "statDate": "2026-06-30"}
        annual = [{"pubDate": "2025-04-01", "statDate": f"{year}-12-31",
                   "epsTTM": "1"} for year in range(2021, 2026)]
        calls = []
        def quality_query(_bs, _code, _date, recorder):
            calls.append(1)
            recorder("query_profit_data", {"code": code}, ["code"], [{"code": code}])
            if len(calls) == 1:
                raise RuntimeError("用户未登录")
            return quality, annual
        price = {"price_as_of": "2026-09-30", "price": 10.0,
                 "mom_12_1": 0.1, "tradestatus": "1", "is_st": "0",
                 "earnings_yield": 0.1, "net_cashflow_yield": 0.1,
                 "book_to_price": 0.1}
        captured = []
        with tempfile.TemporaryDirectory() as folder, patch.dict(sys.modules, {"baostock": bs}), \
                patch("selector.providers.a_baostock._rows", side_effect=lambda rows: rows), \
                patch("selector.providers.a_baostock._market_trend", return_value=(
                    "up", {"price_as_of": "2026-09-30"})), \
                patch("selector.providers.a_baostock._latest_quality", side_effect=quality_query), \
                patch("selector.providers.a_baostock._price_metrics", return_value=price):
            frame, metadata = build_a_metrics("2026-09-30", cache_dir=Path(folder),
                                              raw_recorder=lambda *parts: captured.append(parts))
        self.assertEqual(len(frame), 1)
        self.assertEqual(len(calls), 2)
        self.assertEqual(len([part for part in captured if part[0] == "query_profit_data"]), 1)
        self.assertEqual(metadata["baostock_login_count"], 2)
        self.assertEqual(metadata["session_reconnect_count"], 1)
        self.assertEqual(metadata["retry_by_ticker"], {code: 1})


if __name__ == "__main__":
    unittest.main()
