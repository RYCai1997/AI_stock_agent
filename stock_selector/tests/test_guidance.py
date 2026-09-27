import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch
import json
import pandas as pd
from selector.holding_review import review_holdings
from selector.holdings_store import validate_holdings
from selector.providers.a_baostock import quote_from_frames
from selector.account_guidance import build_account_guidance
from selector.trade_journal import record_fill, journal_rows


class GuidanceTests(unittest.TestCase):
    def setUp(self):
        self.h = pd.DataFrame([dict(ticker="sh.600000", entry_date="2025-01-02",
                                  entry_price=100, quantity=100)])
        self.s = pd.DataFrame([dict(ticker="sh.600000", price=30, above_ema200=False,
                                  fundamental_candidate=False, security_eligible=True, model_supported=True)])
        self.q = {"sh.600000": dict(price=95, price_as_of="2025-03-03", price_basis="unadjusted",
                                    basis_changed=False, tradestatus="1")}

    def review(self, **kw):
        return review_holdings(self.h, self.s, "2025-03-03", "up", **kw).iloc[0]

    def test_missing_row_does_not_mean_index_removal(self):
        self.s = self.s.iloc[:0]
        self.assertEqual(self.review(member_codes=["sh.600000"], quotes=self.q).action, "review")
        self.assertEqual(self.review(member_codes=[], quotes=self.q).reason, "left CSI 300 universe")

    def test_adjusted_price_is_never_compared_to_actual_cost(self):
        self.assertEqual(self.review().action, "review")
        self.assertEqual(self.review(quotes=self.q).action, "hold")
        self.q["sh.600000"]["basis_changed"] = True
        self.assertEqual(self.review(quotes=self.q).reason, "corporate action: reconcile cost and quantity")

    def test_stale_quote_blocks_decision(self):
        self.assertEqual(self.review(quotes=self.q, expected_price_date="2025-03-04").action, "review")

    def test_pending_exit_is_not_cancelled_by_refresh(self):
        self.h["last_action"] = "exit"
        self.h["last_reason"] = "selection and EMA200 break confirmed"
        self.h["last_review_date"] = "2025-02-03"
        self.h["signal_date"] = "2025-02-03"
        self.assertEqual(self.review(quotes=self.q).signal_date, "2025-02-03")
        self.assertEqual(self.review(quotes=self.q).action, "exit")

    def test_future_state_rejected(self):
        self.h["last_review_date"] = "2025-04-01"
        with self.assertRaises(ValueError):
            self.review(quotes=self.q)

    def test_gap_is_not_consecutive_confirmation(self):
        self.h["last_review_date"] = "2025-01-02"
        self.h["nonselected_streak"] = 1
        self.h["below_ema_streak"] = 1
        self.assertEqual(self.review(quotes=self.q).action, "hold")

    def test_corporate_action_detection(self):
        raw = pd.DataFrame(dict(date=["2025-01-02", "2025-03-03"], close=[100, 50], tradestatus=["1", "1"]))
        adjusted = raw.copy()
        adjusted["close"] = [50, 50]
        self.assertTrue(quote_from_frames(raw, adjusted, "2025-01-02", "2025-03-03")["basis_changed"])
        self.assertFalse(quote_from_frames(raw, raw, "2025-01-02", "2025-03-03")["basis_changed"])

    def test_nonfinite_cost_rejected(self):
        for cost in [float("nan"), float("inf"), -1]:
            with self.assertRaises(ValueError):
                validate_holdings([dict(ticker="600000", entry_date="2025-01-02", entry_price=cost, quantity=100)])

    def test_share_budget_respects_cash_industry_and_existing_names(self):
        plan = pd.DataFrame([dict(ticker=c, industry_l1="行业A", entry_delay_sessions=0,
                                  execution_price=10, execution_price_as_of="2025-03-03")
                             for c in ("sh.600001", "sh.600002", "sh.600003")])
        scored = pd.DataFrame([dict(ticker="sh.600000", industry_l1="行业A")])
        account = dict(as_of="2025-03-03", equity=100000, cash=90500,
                       max_exposure=.3, max_industry=.2, cost_buffer=0)
        result = build_account_guidance(plan, self.h, scored, self.q, account, "2025-03-03", "2025-03-03")
        self.assertEqual(list(result.suggested_quantity), [600, 400, 0])
        self.assertLessEqual(result.estimated_amount.sum() + 9500, 20000)
        plan.loc[0, "ticker"] = "sh.600000"
        result = build_account_guidance(plan, self.h, scored, self.q, account, "2025-03-03", "2025-03-03")
        self.assertEqual(result.iloc[0].suggested_quantity, 0)
        account["cash"] = 10
        self.assertEqual(build_account_guidance(plan, self.h, scored, self.q, account, "2025-03-03", "2025-03-03").suggested_quantity.sum(), 0)

    def test_journal_partial_full_sale_fees_and_retry(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Path(folder) / "trades.db"
            fill = dict(id="buy1", ticker="600000", side="BUY", trade_date="2025-01-02", price=10, quantity=200, fees=5)
            positions = record_fill(db, fill, [])
            self.assertAlmostEqual(positions[0]["entry_price"], 10.025)
            self.assertEqual(record_fill(db, fill, positions), positions)
            sell = dict(id="sell1", ticker="600000", side="SELL", trade_date="2025-01-03", price=11, quantity=100, fees=5)
            positions = record_fill(db, sell, positions)
            self.assertEqual(positions[0]["quantity"], 100)
            self.assertAlmostEqual(journal_rows(db)[-1]["realized_pnl"], 92.5)
            with self.assertRaises(ValueError):
                record_fill(db, sell | {"id": "oversell", "quantity": 200}, positions)
            self.assertEqual(record_fill(db, sell | {"id": "sell2"}, positions), [])

    def test_official_entry_exports_guidance_and_preserves_research_mode(self):
        import run_official_strategy as runner
        from test_selector import sample_frame
        metrics = sample_frame(100)
        provider = dict(market_trend="up", errors={}, member_codes=list(metrics.ticker),
                        requested_members=len(metrics), built_rows=len(metrics), cache_hits=0,
                        benchmark=dict(price_as_of="2025-07-15", return_20d=.03))
        with tempfile.TemporaryDirectory() as folder:
            with patch("sys.argv", ["runner", "--as-of", "2025-07-15", "--output", folder]), \
                 patch.object(runner, "build_a_metrics", return_value=(metrics, provider)), \
                 patch.object(runner, "fetch_holding_quotes", return_value={}):
                runner.main()
            result = json.loads((Path(folder)/"official_run_metadata.json").read_text(encoding="utf-8"))
            self.assertEqual(result["mode"], "research")
            self.assertEqual(result["plan"]["ideas"], 5)
            self.assertEqual(pd.read_csv(Path(folder)/"account_guidance.csv").suggested_quantity.sum(), 0)
