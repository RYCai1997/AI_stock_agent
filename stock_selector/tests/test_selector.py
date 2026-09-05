from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import pandas as pd

from selector.pipeline import run_selection, validate_input


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
