"""Independent 20/63/126-session forward selection assessment."""

from __future__ import annotations

import json
from datetime import datetime, time, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from data_public.archive import sha256
from verify_prediction import verify_prediction


HORIZONS = (20, 63, 126)


def evaluate_outcome(*, prediction_dir: Path, stock_prices: pd.DataFrame,
                     benchmark_prices: pd.DataFrame, price_source_manifest: dict,
                     horizon_sessions: int, evaluated_at: datetime,
                     output_root: Path) -> Path:
    if horizon_sessions not in HORIZONS:
        raise ValueError("outcome horizon must be predeclared: 20, 63 or 126 sessions")
    if not verify_prediction(prediction_dir):
        raise ValueError("prediction seal invalid")
    if not {"source", "adjustment_method", "stock_sha256", "benchmark_sha256"} <= set(price_source_manifest):
        raise ValueError("outcome price source provenance incomplete")
    required_stock = {"date", "ticker", "adjusted_close"}
    required_benchmark = {"date", "adjusted_close"}
    if not required_stock <= set(stock_prices) or not required_benchmark <= set(benchmark_prices):
        raise ValueError("adjusted stock and benchmark close histories are required")
    if stock_prices.duplicated(["date", "ticker"]).any() or benchmark_prices.date.duplicated().any():
        raise ValueError("duplicate outcome price dates")
    record = json.loads((prediction_dir / "prediction_record.json").read_text(encoding="utf-8"))
    signal_date = record["signal_date"]
    market = benchmark_prices.sort_values("date").reset_index(drop=True)
    dates = market.date.astype(str).tolist()
    if signal_date not in dates:
        raise ValueError("prediction signal is absent from benchmark market sessions")
    end_index = dates.index(signal_date) + horizon_sessions
    if end_index >= len(dates):
        raise ValueError("horizon has not matured in source history")
    end_date = dates[end_index]
    local = evaluated_at.astimezone(ZoneInfo("Asia/Shanghai"))
    if end_date > local.date().isoformat() or (end_date == local.date().isoformat()
                                               and local.time() < time(15, 0)):
        raise ValueError("horizon has not matured at evaluation time")
    plan = pd.read_csv(prediction_dir / "portfolio_plan.csv", dtype={"ticker": str})
    if not {"ticker", "target_fraction"} <= set(plan):
        raise ValueError("sealed portfolio plan is incomplete")
    def pair_return(frame: pd.DataFrame, ticker: str | None = None) -> float:
        selected = frame if ticker is None else frame[frame.ticker.eq(ticker)]
        values = selected.set_index("date")["adjusted_close"]
        if signal_date not in values.index or end_date not in values.index:
            raise ValueError(f"missing outcome endpoint price for {ticker or 'CSI300'}")
        start = float(values.loc[signal_date])
        end = float(values.loc[end_date])
        if start <= 0 or end <= 0:
            raise ValueError("invalid adjusted outcome close")
        return end / start - 1
    benchmark_return = pair_return(market)
    individual = [{"ticker": ticker, "adjusted_close_return": pair_return(stock_prices, ticker)}
                  for ticker in plan.ticker.astype(str)]
    weight_by_ticker = {str(row.ticker): float(row.target_fraction) for row in plan.itertuples()}
    exposure = sum(weight_by_ticker.values())
    weighted_contribution = sum(weight_by_ticker[row["ticker"]] * row["adjusted_close_return"]
                                for row in individual)
    evaluation = {"evaluation_schema_version": 1, "evidence_label": "prospective_outcome"
                  if record["prospective_primary"] else "retrospective_outcome",
                  "prediction_id": record["prediction_id"],
                  "prediction_seal_sha256": sha256((prediction_dir / "prediction_seal.json").read_bytes()),
                  "signal_date": signal_date, "horizon_end_date": end_date,
                  "horizon_sessions": horizon_sessions,
                  "evaluated_at_utc": evaluated_at.astimezone(timezone.utc).isoformat(),
                  "selection_price_basis": "adjusted_close_to_close_from_signal",
                  "execution_return_included": False,
                  "source_manifest": price_source_manifest,
                  "individual_returns": individual,
                  "selected_count": len(individual),
                  "equal_weight_selection_return":
                  sum(row["adjusted_close_return"] for row in individual) / len(individual) if individual else None,
                  "portfolio_weighted_selection_return": weighted_contribution / exposure if exposure else None,
                  "strategy_target_weighted_contribution": weighted_contribution,
                  "target_exposure": exposure,
                  "csi300_return": benchmark_return,
                  "exposure_matched_csi300_return": exposure * benchmark_return,
                  "cash_return_assumption": 0.0,
                  "limitations": ["Close-to-close selection diagnostic; not next-open shadow execution PnL",
                                  "Adjusted-series corporate-action convention requires source audit"]}
    path = output_root / f"{record['prediction_id']}_{horizon_sessions}d.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        json.dump(evaluation, handle, ensure_ascii=False, indent=2)
    if not verify_prediction(prediction_dir):
        raise AssertionError("evaluator changed sealed prediction")
    return path
