from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import pandas as pd

from backtest_a_nodes import calculate_forward_outcomes, select_variants
from test_a_risk_rules import market_is_overheated, simulate_rule
from test_a_requalification_exit import simulate_policy_exit
from selector.config import SelectorConfig
from selector.exit_policy import evaluate_monthly_exit
from selector.holding_review import review_holdings
from selector.pipeline import run_selection, validate_input
from selector.portfolio_plan import build_portfolio_plan
from selector.providers.a_baostock import _price_metrics_from_frame, _upgrade_cached_row
from selector.providers.us_sec_yahoo import _annual_records
from selector.strategy import OFFICIAL_STRATEGY


def sample_frame(rows: int = 20) -> pd.DataFrame:
    records = []
    for index in range(rows):
        strength = index + 1
        records.append({
            "market": "US",
            "ticker": f"T{index:02d}",
            "company": f"Company {index}",
            "industry_l1": "Technology",
            "industry_l2": "Software" if index < 10 else "Hardware",
            "universe_as_of": "2025-07-01",
            "fundamental_as_of": "2025-06-30",
            "price_as_of": "2025-07-15",
            "roic": strength / 100,
            "fcf_margin": strength / 110,
            "eps_growth_std": (rows - index) / 100,
            "earnings_yield": strength / 200,
            "fcf_yield": strength / 220,
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


class SelectorTests(unittest.TestCase):
    def test_official_strategy_parameters_are_frozen(self) -> None:
        strategy = OFFICIAL_STRATEGY
        config = strategy.selector_config()
        self.assertEqual(strategy.strategy_id, "A_CSI300_QVM_TIMING_V1")
        self.assertEqual(strategy.instrument_mode, "spot_long_only")
        self.assertEqual(strategy.fixed_position_fraction, 0.10)
        self.assertEqual(strategy.max_new_exposure_per_window, 0.30)
        self.assertEqual(strategy.stop_loss_fraction, 0.10)
        self.assertEqual(config.quality_quantile, 0.50)
        self.assertEqual(config.value_min_score, 20.0)
        self.assertEqual(config.momentum_top_fraction, 0.20)

    def test_portfolio_plan_limits_window_to_three_fixed_positions(self) -> None:
        frame = sample_frame(50).rename(columns={
            "roic": "roe", "fcf_margin": "cfo_to_revenue",
            "fcf_yield": "net_cashflow_yield",
        })
        frame["market"] = "A"
        with tempfile.TemporaryDirectory() as folder:
            scored, _ = run_selection(
                frame, "A", "2025-07-15", Path(folder), "up",
                OFFICIAL_STRATEGY.selector_config(),
            )
        plan = build_portfolio_plan(scored)
        self.assertLessEqual(len(plan), 3)
        self.assertTrue(plan["target_fraction"].eq(0.10).all())
        self.assertLessEqual(plan["target_fraction"].sum(), 0.30 + 1e-12)
        self.assertTrue(plan["approval_status"].eq("REQUIRES_HUMAN_APPROVAL").all())

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
        result = review_holdings(holdings, scored, "2025-03-01", "up")
        self.assertEqual(result.loc[0, "action"], "exit")
        self.assertAlmostEqual(result.loc[0, "stop_loss_price"], 90.0)
        self.assertEqual(result.loc[0, "approval_status"], "REQUIRES_HUMAN_APPROVAL")

    def test_holding_review_blocks_future_entry_date(self) -> None:
        holdings = pd.DataFrame([{
            "ticker": "sh.600000", "entry_date": "2025-03-02", "entry_price": 100.0,
        }])
        with self.assertRaisesRegex(ValueError, "entry date after"):
            review_holdings(holdings, pd.DataFrame(columns=["ticker"]), "2025-03-01", "up")

    def test_monthly_policy_exit_uses_next_session_open(self) -> None:
        dates = pd.date_range("2025-07-15", periods=140, freq="B")
        prices = pd.DataFrame({
            "date": dates, "open": 100.0, "high": 102.0,
            "low": 99.0, "close": 101.0, "tradestatus": "1",
        })
        review_date = str(dates[22].date())
        next_date = str(dates[23].date())
        prices.loc[23, "open"] = 105.0
        outcome = simulate_policy_exit(prices, "2025-07-15", False, [{
            "review_date": review_date, "action": "exit", "reason": "stock below EMA200",
        }])
        self.assertEqual(outcome["exit_date"], next_date)
        self.assertEqual(outcome["exit_reason"], "stock below EMA200")
        self.assertAlmostEqual(outcome["return_3m"], 0.05)

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
                frame, "US", "2025-07-15", Path(folder), market_trend="up"
            )
        plan = build_portfolio_plan(scored, overheated=True)
        self.assertTrue(plan["entry_status"].eq("WAIT_5_SESSIONS").all())
        self.assertTrue(plan["entry_delay_sessions"].eq(5).all())

    def test_portfolio_plan_is_ready_when_market_is_not_overheated(self) -> None:
        frame = sample_frame()
        with tempfile.TemporaryDirectory() as folder:
            scored, _ = run_selection(
                frame, "US", "2025-07-15", Path(folder), market_trend="up"
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

    def test_intraday_stop_fills_at_stop_and_gap_uses_worse_open(self) -> None:
        prices = pd.DataFrame([
            {"date": "2025-07-15", "open": 100, "high": 101, "low": 99, "close": 100, "tradestatus": "1"},
        ])
        future_dates = pd.date_range("2025-07-16", periods=130, freq="B")
        future = pd.DataFrame({
            "date": future_dates, "open": 100.0, "high": 101.0,
            "low": 99.0, "close": 100.0, "tradestatus": "1",
        })
        future.loc[5, ["open", "high", "low", "close"]] = [95.0, 96.0, 89.0, 92.0]
        prices = pd.concat([prices.iloc[:1], future], ignore_index=True)
        stopped = simulate_rule(prices, "2025-07-15", "stop10_immediate", False)
        self.assertTrue(stopped["stop_triggered"])
        self.assertAlmostEqual(stopped["return_1m"], -0.10)
        self.assertAlmostEqual(stopped["worst_return_from_entry_6m"], -0.10)

        future.loc[5, ["open", "high", "low", "close"]] = [85.0, 88.0, 82.0, 86.0]
        prices = pd.concat([prices.iloc[:1], future], ignore_index=True)
        gap = simulate_rule(prices, "2025-07-15", "stop10_immediate", False)
        self.assertAlmostEqual(gap["return_1m"], -0.15)
        self.assertAlmostEqual(gap["worst_return_from_entry_6m"], -0.15)

    def test_overheat_uses_only_signal_date_and_prior_closes(self) -> None:
        dates = pd.date_range("2025-06-16", periods=22, freq="B")
        prices = pd.DataFrame({
            "date": dates, "open": range(100, 122), "high": range(101, 123),
            "low": range(99, 121), "close": range(100, 122), "tradestatus": "1",
        })
        signal = str(dates[20].date())
        overheated, observed = market_is_overheated(prices, signal, threshold=0.10)
        self.assertTrue(overheated)
        self.assertAlmostEqual(observed, 0.20)

    def test_forward_outcome_enters_after_signal_and_uses_calendar_horizons(self) -> None:
        prices = pd.DataFrame([
            {"date": "2025-07-15", "open": 99, "high": 101, "low": 98, "close": 100, "tradestatus": "1"},
            {"date": "2025-07-16", "open": 100, "high": 103, "low": 99, "close": 102, "tradestatus": "1"},
            {"date": "2025-08-15", "open": 108, "high": 111, "low": 107, "close": 110, "tradestatus": "1"},
            {"date": "2025-10-15", "open": 118, "high": 121, "low": 117, "close": 120, "tradestatus": "1"},
            {"date": "2026-01-15", "open": 128, "high": 131, "low": 127, "close": 130, "tradestatus": "1"},
        ])
        outcome = calculate_forward_outcomes(prices, "2025-07-15")
        self.assertEqual(outcome["entry_date"], "2025-07-16")
        self.assertAlmostEqual(outcome["return_1m"], 0.10)
        self.assertAlmostEqual(outcome["return_3m"], 0.20)
        self.assertAlmostEqual(outcome["return_6m"], 0.30)

    def test_comparison_variants_do_not_use_future_outcomes(self) -> None:
        frame = sample_frame()
        with tempfile.TemporaryDirectory() as folder:
            scored, _ = run_selection(
                frame, "US", "2025-07-15", Path(folder), market_trend="up"
            )
        scored["net_cashflow_yield"] = scored["fcf_yield"]
        scored["future_return"] = range(len(scored))
        variants = select_variants(scored, "up")
        scored["future_return"] = list(reversed(range(len(scored))))
        changed_labels = select_variants(scored, "up")
        self.assertEqual(len(variants["qvm_current"]), len(variants["momentum_only_matched_n"]))
        for strategy in variants:
            self.assertEqual(set(variants[strategy]["ticker"]), set(changed_labels[strategy]["ticker"]))

    def test_empty_intermediate_pool_returns_zero_candidates(self) -> None:
        frame = sample_frame()
        frame["industry_l1"] = "J 金融业"
        with tempfile.TemporaryDirectory() as folder:
            result, metadata = run_selection(
                frame, "US", "2025-07-15", Path(folder), market_trend="up"
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
        frame = sample_frame().rename(columns={
            "roic": "roe",
            "fcf_margin": "cfo_to_revenue",
            "fcf_yield": "net_cashflow_yield",
        })
        frame["market"] = "A"
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

    def test_sec_record_filed_after_snapshot_is_excluded(self) -> None:
        rows = [
            {"start": "2024-01-01", "end": "2024-12-31", "filed": "2025-02-01", "form": "10-K", "val": 10, "priority": 0},
            {"start": "2023-01-01", "end": "2023-12-31", "filed": "2024-02-01", "form": "10-K", "val": 8, "priority": 0},
        ]
        selected = _annual_records(rows, "2025-01-15")
        self.assertEqual(set(selected), {"2023-12-31"})

    def test_future_data_is_blocked(self) -> None:
        frame = sample_frame()
        frame.loc[0, "fundamental_as_of"] = "2025-07-16"
        with self.assertRaisesRegex(ValueError, "look-ahead blocked"):
            validate_input(frame, "US", "2025-07-15")

    def test_duplicate_ticker_is_blocked(self) -> None:
        frame = sample_frame()
        frame.loc[1, "ticker"] = frame.loc[0, "ticker"]
        with self.assertRaisesRegex(ValueError, "duplicate tickers"):
            validate_input(frame, "US", "2025-07-15")

    def test_pipeline_outputs_auditable_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            result, metadata = run_selection(
                sample_frame(), "US", "2025-07-15", Path(folder), market_trend="up"
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
                sample_frame(), "US", "2025-07-15", Path(folder), market_trend="unknown"
            )
            selected = pd.read_csv(Path(folder) / "selected.csv")
            actionable = pd.read_csv(Path(folder) / "actionable.csv")
            self.assertEqual(len(selected), metadata["counts"]["fundamental_candidates"])
            self.assertTrue(actionable.empty)


if __name__ == "__main__":
    unittest.main()
