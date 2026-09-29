from __future__ import annotations

import unittest

from backtest.account import Account
from backtest.corporate_actions import CorporateAction, apply_corporate_action, data_quality_report
from backtest.engine import BacktestEngine, DailyBar, Signal


class CorporateActionTests(unittest.TestCase):
    def test_dividend_and_bonus_preserve_account_basis(self):
        account = Account(10000)
        account.buy("A", 100, 10)
        apply_corporate_action(account, CorporateAction("2025-01-03", "A", "dividend", cash_per_share=.5))
        self.assertEqual(account.dividends, 50)
        self.assertEqual(account.cash, 9050)
        apply_corporate_action(account, CorporateAction("2025-01-03", "A", "bonus", share_ratio=1))
        self.assertEqual(account.positions["A"].quantity, 200)
        self.assertEqual(account.positions["A"].average_cost, 5)
        self.assertEqual(account.mark({"A": 5})["total_equity"], 10050)

    def test_gross_dividend_explicitly_discloses_unmodelled_tax(self):
        account = Account(10000)
        account.buy("A", 100, 10)
        event = apply_corporate_action(account, CorporateAction(
            "2025-01-03", "A", "dividend", cash_per_share=.5))
        self.assertEqual(event["dividend_tax_model"], "gross_no_withholding")
        self.assertTrue(event["data_confidence_degraded"])
        self.assertIn("tax", data_quality_report([event]).lower())

    def test_rights_require_explicit_decision_and_reprice_basis(self):
        account = Account(10000)
        account.buy("A", 100, 10)
        action = CorporateAction("2025-01-03", "A", "rights", share_ratio=.2,
                                 subscription_price=5)
        skipped = apply_corporate_action(account, action)
        self.assertEqual(skipped["status"], "unsupported")
        self.assertTrue(skipped["manual_audit_required"])
        exercised = apply_corporate_action(account, CorporateAction(
            "2025-01-03", "A", "rights", share_ratio=.2, subscription_price=5,
            exercise_rights=True))
        self.assertEqual(exercised["status"], "applied")
        self.assertEqual(account.positions["A"].quantity, 120)
        self.assertAlmostEqual(account.positions["A"].average_cost, 1100 / 120)

    def test_delisting_is_explicitly_degraded(self):
        account = Account(10000)
        account.buy("A", 100, 10)
        event = apply_corporate_action(account, CorporateAction("2025-01-03", "A", "delisting"))
        self.assertEqual(event["status"], "unsupported")
        self.assertTrue(event["data_confidence_degraded"])
        self.assertIn("Manual audit required: 1", data_quality_report([event]))

    def test_engine_applies_dividend_before_daily_nav(self):
        bars = [DailyBar("2025-01-02", "A", 10, 10, 10, 10),
                DailyBar("2025-01-03", "A", 10, 10, 10, 10),
                DailyBar("2025-01-06", "A", 9.5, 9.6, 9.4, 9.5)]
        result = BacktestEngine(100000).run(
            bars, [Signal("2025-01-02", "A", "buy", .06)],
            [CorporateAction("2025-01-06", "A", "dividend", cash_per_share=.5)])
        self.assertGreater(result.daily_nav[-1]["dividends"], 0)
        self.assertAlmostEqual(result.daily_nav[-1]["total_equity"], 100000)
