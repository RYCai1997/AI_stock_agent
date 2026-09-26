from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path

import pandas as pd

from selector.config import SelectorConfig
from selector.exit_policy import evaluate_monthly_exit
from selector.holding_review import review_holdings
from selector.holdings_store import (
    latest_holdings_file, load_holdings, normalize_ticker, reviewed_snapshot,
    save_holdings,
)
from selector.pipeline import run_selection, validate_input
from selector.portfolio_plan import build_portfolio_plan
from selector.providers.a_baostock import _price_metrics_from_frame, _upgrade_cached_row
from selector.providers.a_baostock import quote_from_frames
from selector.strategy import OFFICIAL_STRATEGY


def sample_frame(rows: int = 20) -> pd.DataFrame:
    records = []
    for index in range(rows):
        strength = index + 1
        records.append({
            "market": "A",
            "ticker": f"T{index:02d}",
            "company": f"Company {index}",
            "industry_l1": "Technology",
            "industry_l2": "Software" if index < 10 else "Hardware",
            "universe_as_of": "2025-07-01",
            "fundamental_as_of": "2025-06-30",
            "price_as_of": "2025-07-15",
            "roe": strength / 100,
            "cfo_to_revenue": strength / 110,
            "eps_growth_std": (rows - index) / 100,
            "earnings_yield": strength / 200,
            "net_cashflow_yield": strength / 220,
            "book_to_price": strength / 50,
            "mom_6_1": strength / 100,
            "mom_12_1": strength / 80,
            "relative_strength": strength / 120,
            "price": 100 + index,
            "ema200": 90 + index,
            "volatility_1y": 0.20,
            "max_drawdown_6m": -0.10,
            "avg_daily_turnover": 10_000_000,
        })
    return pd.DataFrame(records)


def raw_quotes(price, when="2025-03-01"):
    return {"sh.600000": {"price": price, "price_basis": "unadjusted", "price_as_of": when,
                           "basis_changed": False, "tradestatus": "1"}}


class SelectorTests(unittest.TestCase):
    def test_official_strategy_parameters_are_frozen(self) -> None:
        strategy = OFFICIAL_STRATEGY
        config = strategy.selector_config()
        self.assertEqual(strategy.strategy_id, "A_CSI300_QVM_TIMING_V1")
        self.assertEqual(strategy.instrument_mode, "spot_long_only")
        self.assertEqual(strategy.fixed_position_fraction, 0.06)
        self.assertEqual(strategy.max_new_exposure_per_window, 0.30)
        self.assertEqual(strategy.max_new_positions_per_window, 5)
        self.assertEqual(strategy.stop_loss_fraction, 0.10)
        self.assertEqual(config.quality_quantile, 0.50)
        self.assertEqual(config.value_min_score, 20.0)
        self.assertEqual(config.momentum_top_fraction, 0.20)

    def test_portfolio_plan_limits_window_to_five_fixed_positions(self) -> None:
        frame = sample_frame(100)
        with tempfile.TemporaryDirectory() as folder:
            scored, _ = run_selection(
                frame, "A", "2025-07-15", Path(folder), "up",
                OFFICIAL_STRATEGY.selector_config(),
            )
        plan = build_portfolio_plan(scored)
        self.assertEqual(len(plan), 5)
        self.assertTrue(plan["target_fraction"].eq(0.06).all())
        self.assertAlmostEqual(plan["target_fraction"].sum(), 0.30)
        self.assertTrue(plan["approval_status"].eq("REQUIRES_HUMAN_APPROVAL").all())

    def test_official_release_rejects_non_a_markets(self) -> None:
        frame = sample_frame()
        frame["market"] = "US"
        with self.assertRaisesRegex(ValueError, "supports A only"):
            validate_input(frame, "US", "2025-07-15")

    def test_holding_review_outputs_stop_and_stateful_exit(self) -> None:
        scored = pd.DataFrame([{
            "ticker": "sh.600000", "company": "Example", "price": 90.0,
            "above_ema200": False, "fundamental_candidate": False,
            "security_eligible": True, "model_supported": True,
        }])
        holdings = pd.DataFrame([{
            "ticker": "sh.600000", "entry_date": "2025-01-02", "entry_price": 100.0,
            "nonselected_streak": 1, "below_ema_streak": 1,
        }])
        result = review_holdings(holdings, scored, "2025-03-01", "up", quotes=raw_quotes(90))
        self.assertEqual(result.loc[0, "action"], "exit")
        self.assertAlmostEqual(result.loc[0, "stop_loss_price"], 90.0)
        self.assertEqual(result.loc[0, "approval_status"], "REQUIRES_HUMAN_APPROVAL")

    def test_holding_review_preserves_quantity_and_flags_stop_exit(self) -> None:
        scored = pd.DataFrame([{
            "ticker": "sh.600000", "company": "Example", "price": 89.0,
            "above_ema200": True, "fundamental_candidate": True,
            "security_eligible": True, "model_supported": True,
        }])
        holdings = pd.DataFrame([{
            "ticker": "sh.600000", "entry_date": "2025-01-02",
            "entry_price": 100.0, "quantity": 300,
        }])
        result = review_holdings(holdings, scored, "2025-03-01", "up", quotes=raw_quotes(89))
        self.assertEqual(result.loc[0, "quantity"], 300)
        self.assertEqual(result.loc[0, "position_cost"], 30_000)
        self.assertEqual(result.loc[0, "action"], "exit")
        self.assertEqual(result.loc[0, "reason"], "latest close at or below stop loss threshold")
        self.assertEqual(result.loc[0, "signal_date"], "2025-03-01")
        self.assertEqual(result.loc[0, "suggested_execution"], "NEXT_TRADABLE_OPEN")

    def test_repeated_run_in_same_month_does_not_add_confirmation(self) -> None:
        scored = pd.DataFrame([{
            "ticker": "sh.600000", "company": "Example", "price": 95.0,
            "above_ema200": False, "fundamental_candidate": False,
            "security_eligible": True, "model_supported": True,
        }])
        holdings = pd.DataFrame([{
            "ticker": "sh.600000", "entry_date": "2025-01-02",
            "entry_price": 100.0, "quantity": 100,
            "nonselected_streak": 1, "below_ema_streak": 1,
            "last_review_date": "2025-03-01",
        }])
        result = review_holdings(holdings, scored, "2025-03-20", "up", quotes=raw_quotes(95, "2025-03-20"))
        self.assertEqual(result.loc[0, "action"], "hold")
        self.assertEqual(result.loc[0, "nonselected_streak"], 1)
        self.assertEqual(result.loc[0, "below_ema_streak"], 1)

    def test_holdings_snapshots_are_validated_and_latest_is_found(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            first = save_holdings([{
                "ticker": "600000", "company": "Example",
                "entry_date": "2025-01-02", "entry_price": 10,
                "quantity": 100,
            }], directory)
            loaded = load_holdings(first)
            self.assertEqual(loaded[0]["ticker"], "sh.600000")
            self.assertEqual(loaded[0]["quantity"], 100)
            self.assertEqual(latest_holdings_file(directory), first)
        self.assertEqual(normalize_ticker("000001"), "sz.000001")

    def test_reviewed_snapshot_carries_confirmation_state_forward(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            source = save_holdings([{
                "ticker": "600000", "company": "Example",
                "entry_date": "2025-01-02", "entry_price": 10,
                "quantity": 100,
            }], directory)
            review = directory / "review.csv"
            pd.DataFrame([{
                "ticker": "sh.600000", "company": "Example",
                "review_date": str(date.today()), "action": "hold",
                "reason": "qualification warning; trend break not confirmed",
                "nonselected_streak": 1, "below_ema_streak": 1,
            }]).to_csv(review, index=False)
            self.assertIsNone(reviewed_snapshot(source, review, directory))
            updated = reviewed_snapshot(source, review, directory, current_run=True)
            self.assertIsNotNone(updated)
            row = load_holdings(updated)[0]
            self.assertEqual(row["last_review_date"], str(date.today()))
            self.assertEqual(row["nonselected_streak"], 1)

    def test_holding_review_blocks_future_entry_date(self) -> None:
        holdings = pd.DataFrame([{
            "ticker": "sh.600000", "entry_date": "2025-03-02", "entry_price": 100.0,
        }])
        with self.assertRaisesRegex(ValueError, "entry date after"):
            review_holdings(holdings, pd.DataFrame(columns=["ticker"]), "2025-03-01", "up")

    def test_price_metrics_ignore_future_rows(self) -> None:
        dates = pd.date_range("2024-01-01", periods=400, freq="B")
        frame = pd.DataFrame({
            "date": dates, "close": range(100, 500), "volume": 1, "amount": 1,
            "turn": 1, "peTTM": 20, "pbMRQ": 2, "pcfNcfTTM": 10,
            "tradestatus": "1", "isST": "0",
        })
        as_of = str(dates[300].date())
        before = _price_metrics_from_frame(frame.iloc[:301], as_of)
        changed_future = frame.copy()
        changed_future.loc[301:, "close"] = 99999
        after = _price_metrics_from_frame(changed_future, as_of)
        self.assertEqual(before["price"], after["price"])
        self.assertEqual(before["mom_12_1"], after["mom_12_1"])
        self.assertEqual(before["return_20d"], after["return_20d"])

    def test_portfolio_plan_marks_five_session_overheat_delay(self) -> None:
        frame = sample_frame()
        with tempfile.TemporaryDirectory() as folder:
            scored, _ = run_selection(
                frame, "A", "2025-07-15", Path(folder), market_trend="up"
            )
        plan = build_portfolio_plan(scored, overheated=True)
        self.assertTrue(plan["entry_status"].eq("WAIT_5_SESSIONS").all())
        self.assertTrue(plan["entry_delay_sessions"].eq(5).all())

    def test_portfolio_plan_is_ready_when_market_is_not_overheated(self) -> None:
        frame = sample_frame()
        with tempfile.TemporaryDirectory() as folder:
            scored, _ = run_selection(
                frame, "A", "2025-07-15", Path(folder), market_trend="up"
            )
        plan = build_portfolio_plan(scored, overheated=False)
        self.assertTrue(plan["entry_status"].eq("READY_AFTER_HUMAN_APPROVAL").all())
        self.assertTrue(plan["entry_delay_sessions"].eq(0).all())

    def test_exit_policy_rank_loss_alone_is_warning_not_exit(self) -> None:
        row = pd.Series({
            "security_eligible": True, "model_supported": True, "quality_pass": True,
            "value_pass": True, "above_ema200": True, "momentum_percentile": 75,
            "fundamental_candidate": False,
        })
        first = evaluate_monthly_exit(row, "up", True, 0)
        second = evaluate_monthly_exit(
            row, "up", True, first.nonselected_streak, first.below_ema_streak
        )
        self.assertEqual(first.action, "hold")
        self.assertEqual(second.action, "hold")

    def test_exit_policy_requires_confirmation_for_stock_ema_break(self) -> None:
        row = pd.Series({
            "security_eligible": True, "model_supported": True, "quality_pass": True,
            "value_pass": True, "above_ema200": False, "momentum_percentile": 99,
            "fundamental_candidate": True,
        })
        first = evaluate_monthly_exit(row, "up", True, 0, 0)
        second = evaluate_monthly_exit(
            row, "up", True, first.nonselected_streak, first.below_ema_streak
        )
        self.assertEqual(first.action, "hold")
        self.assertEqual(second.action, "exit")
        self.assertEqual(second.reason, "selection and EMA200 break confirmed")

    def test_exit_policy_market_and_stock_break_exits_immediately(self) -> None:
        row = pd.Series({
            "security_eligible": True, "model_supported": True, "quality_pass": True,
            "value_pass": True, "above_ema200": False, "momentum_percentile": 99,
            "fundamental_candidate": True,
        })
        decision = evaluate_monthly_exit(row, "down", True, 0, 0)
        self.assertEqual(decision.action, "exit")
        self.assertEqual(decision.reason, "market and stock below EMA200")

    def test_exit_policy_does_not_treat_unknown_market_as_down(self) -> None:
        row = pd.Series({
            "security_eligible": True, "model_supported": True,
            "above_ema200": False, "fundamental_candidate": True,
        })
        decision = evaluate_monthly_exit(row, "unknown", True, 0, 0)
        self.assertEqual(decision.action, "hold")
        self.assertEqual(decision.below_ema_streak, 1)

    def test_empty_intermediate_pool_returns_zero_candidates(self) -> None:
        frame = sample_frame()
        frame["industry_l1"] = "J 金融业"
        with tempfile.TemporaryDirectory() as folder:
            result, metadata = run_selection(
                frame, "A", "2025-07-15", Path(folder), market_trend="up"
            )
        self.assertEqual(metadata["counts"]["quality_pass"], 0)
        self.assertEqual(metadata["counts"]["value_pass"], 0)
        self.assertEqual(metadata["counts"]["fundamental_candidates"], 0)
        self.assertFalse(result["actionable_candidate"].any())

    def test_a_share_v1_cache_label_is_migrated_without_value_change(self) -> None:
        payload = {
            "cache_version": 1,
            "row": {"ticker": "sh.600000", "operating_cashflow_yield": -0.125},
        }
        row = _upgrade_cached_row(payload)
        self.assertNotIn("operating_cashflow_yield", row)
        self.assertEqual(row["net_cashflow_yield"], -0.125)

    def test_a_share_factor_profile_uses_market_specific_fields(self) -> None:
        frame = sample_frame()
        with tempfile.TemporaryDirectory() as folder:
            result, metadata = run_selection(
                frame,
                "A",
                "2025-07-15",
                Path(folder),
                market_trend="up",
                config=SelectorConfig(factor_profile="a_share_v1"),
            )
            self.assertEqual(metadata["config"]["factor_profile"], "a_share_v1")
            self.assertTrue(result["quality_formula"].str.contains("ROE").all())
            self.assertGreater(metadata["counts"]["fundamental_candidates"], 0)

    def test_future_data_is_blocked(self) -> None:
        frame = sample_frame()
        frame.loc[0, "fundamental_as_of"] = "2025-07-16"
        with self.assertRaisesRegex(ValueError, "look-ahead blocked"):
            validate_input(frame, "A", "2025-07-15")

    def test_duplicate_ticker_is_blocked(self) -> None:
        frame = sample_frame()
        frame.loc[1, "ticker"] = frame.loc[0, "ticker"]
        with self.assertRaisesRegex(ValueError, "duplicate tickers"):
            validate_input(frame, "A", "2025-07-15")

    def test_pipeline_outputs_auditable_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            result, metadata = run_selection(
                sample_frame(), "A", "2025-07-15", Path(folder), market_trend="up"
            )
            self.assertGreater(metadata["counts"]["fundamental_candidates"], 0)
            self.assertEqual(
                metadata["counts"]["fundamental_candidates"],
                metadata["counts"]["actionable_candidates"],
            )
            self.assertTrue((Path(folder) / "candidates.csv").exists())
            self.assertTrue((Path(folder) / "selected.csv").exists())
            self.assertTrue((Path(folder) / "actionable.csv").exists())
            self.assertTrue((Path(folder) / "metadata.json").exists())
            self.assertIn("selection_reason", result.columns)
            self.assertTrue(result.loc[result["actionable_candidate"], "above_ema200"].all())

    def test_unknown_market_trend_keeps_selection_pool(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            _, metadata = run_selection(
                sample_frame(), "A", "2025-07-15", Path(folder), market_trend="unknown"
            )
            selected = pd.read_csv(Path(folder) / "selected.csv")
            actionable = pd.read_csv(Path(folder) / "actionable.csv")
            self.assertEqual(len(selected), metadata["counts"]["fundamental_candidates"])
            self.assertTrue(actionable.empty)


if __name__ == "__main__":
    unittest.main()
