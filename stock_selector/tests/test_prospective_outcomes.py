from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from data_public.archive import sha256
from evaluation.outcomes import evaluate_outcome
from prospective.package import REQUIRED_INPUT_FILES, create_prediction_package
from verify_prediction import verify_prediction


class ProspectiveOutcomesTests(unittest.TestCase):
    def _prediction(self, root):
        files = {name: b"x\n" for name in REQUIRED_INPUT_FILES}
        files["portfolio_plan.csv"] = b"ticker,target_fraction\nT0,0.06\n"
        return create_prediction_package(
            prospective_root=root / "prospective", retrospective_root=root / "retrospective",
            signal_date="2026-10-15",
            generated_at=datetime(2026, 10, 15, 15, 1, tzinfo=ZoneInfo("Asia/Shanghai")),
            evidence_label="prospective", files=files,
            source_manifest={"data_provider_version": "PUBLIC_V1",
                             "sources": [{"raw_sha256": sha256(b"a"),
                                          "normalized_sha256": sha256(b"b")}]},
            signal={"scheduled_signal_date": "2026-10-15", "market_close_verified": True},
            quality={"primary_eligible": True},
            provenance={"git_commit": "a" * 40, "dirty": False,
                        "working_tree_status": ""})

    def test_all_fixed_horizons_and_sealed_prediction_unchanged(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            prediction = self._prediction(root)
            dates = pd.bdate_range("2026-10-15", periods=130).strftime("%Y-%m-%d")
            stocks = pd.DataFrame([{"date": day, "ticker": "T0",
                                    "adjusted_close": 100 + index}
                                   for index, day in enumerate(dates)])
            benchmark = pd.DataFrame([{"date": day, "adjusted_close": 100 + .5 * index}
                                      for index, day in enumerate(dates)])
            source = {"source": "public_fixture", "adjustment_method": "fixture",
                      "stock_sha256": sha256(stocks.to_csv(index=False).encode()),
                      "benchmark_sha256": sha256(benchmark.to_csv(index=False).encode())}
            before = (prediction / "prediction_record.json").read_bytes()
            for horizon in (20, 63, 126):
                path = evaluate_outcome(
                    prediction_dir=prediction, stock_prices=stocks,
                    benchmark_prices=benchmark, price_source_manifest=source,
                    horizon_sessions=horizon,
                    evaluated_at=datetime(2027, 6, 1, 16, tzinfo=ZoneInfo("Asia/Shanghai")),
                    output_root=root / "evaluation")
                result = json.loads(path.read_text())
                self.assertEqual(result["horizon_sessions"], horizon)
                self.assertAlmostEqual(result["individual_returns"][0]["adjusted_close_return"],
                                       horizon / 100)
                self.assertAlmostEqual(result["exposure_matched_csi300_return"],
                                       .06 * .5 * horizon / 100)
            self.assertEqual((prediction / "prediction_record.json").read_bytes(), before)
            self.assertTrue(verify_prediction(prediction))

    def test_unmatured_horizon_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            prediction = self._prediction(root)
            dates = pd.bdate_range("2026-10-15", periods=22).strftime("%Y-%m-%d")
            stock = pd.DataFrame([{"date": day, "ticker": "T0", "adjusted_close": 100}
                                  for day in dates])
            benchmark = pd.DataFrame([{"date": day, "adjusted_close": 100} for day in dates])
            with self.assertRaisesRegex(ValueError, "not matured"):
                evaluate_outcome(prediction_dir=prediction, stock_prices=stock,
                                 benchmark_prices=benchmark,
                                 price_source_manifest={"source": "fixture", "adjustment_method": "fixture",
                                                        "stock_sha256": "x", "benchmark_sha256": "y"},
                                 horizon_sessions=20,
                                 evaluated_at=datetime(2026, 10, 20, 16,
                                                       tzinfo=ZoneInfo("Asia/Shanghai")),
                                 output_root=root / "evaluation")
