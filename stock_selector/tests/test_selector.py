from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import pandas as pd

from backtest_a_nodes import calculate_forward_outcomes, select_variants
from test_a_risk_rules import market_is_overheated, simulate_rule
from selector.config import SelectorConfig
from selector.pipeline import run_selection, validate_input
from selector.providers.a_baostock import _upgrade_cached_row
from selector.providers.us_sec_yahoo import _annual_records


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
