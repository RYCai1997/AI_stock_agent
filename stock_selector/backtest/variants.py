"""Predeclared ablation layers. H delegates to the unmodified official adapter."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from math import isfinite

import pandas as pd

from selector.pipeline import validate_input, run_selection
from selector.portfolio_plan import build_portfolio_plan
from selector.scoring import percentile, score_frame
from selector.strategy import OFFICIAL_STRATEGY

from .account import Account
from .engine import DailyBar, Signal
from .official import OfficialSignalProvider, OfficialSnapshot


@dataclass(frozen=True)
class VariantSpec:
    code: str
    label: str
    quality: bool
    value: bool
    market_ema: bool
    stock_ema: bool
    stop_loss: bool
    overheat_delay: bool
    monthly_exit: bool
    kind: str = "stock"


VARIANTS = {
    "A": VariantSpec("A", "CSI300 buy and hold", False, False, False, False, False, False, False, "index"),
    "B": VariantSpec("B", "CSI300 + market EMA200", False, False, True, False, False, False, False, "index"),
    "C": VariantSpec("C", "Momentum only", False, False, False, False, False, False, False),
    "D": VariantSpec("D", "Momentum + market EMA200", False, False, True, False, False, False, False),
    "E": VariantSpec("E", "Quality + Momentum + EMA200", True, False, True, True, False, False, False),
    "F": VariantSpec("F", "Quality + Value + Momentum + EMA200", True, True, True, True, False, False, False),
    "G": VariantSpec("G", "Q/V/M + EMA200 + stop loss", True, True, True, True, True, False, False),
    "H": VariantSpec("H", "Full frozen V1", True, True, True, True, True, True, True),
}


def variant_candidates(metrics: pd.DataFrame, date: str, market_trend: str,
                       spec: VariantSpec) -> pd.DataFrame:
    clean = validate_input(metrics, OFFICIAL_STRATEGY.market, date,
                           OFFICIAL_STRATEGY.selector_config())
    scored = score_frame(clean, OFFICIAL_STRATEGY.selector_config())
    eligible = scored["security_eligible"] & scored["model_supported"]
    if spec.quality:
        eligible &= scored["quality_pass"]
    if spec.value:
        eligible &= scored["value_pass"]
    eligible &= scored[["mom_6_1", "mom_12_1"]].notna().all(axis=1)
    subset = scored.loc[eligible].copy()
    if subset.empty:
        return subset
    subset["ablation_momentum_score"] = (
        .5 * percentile(subset["mom_6_1"]) +
        .5 * percentile(subset["mom_12_1"])
    )
    cutoff = subset["ablation_momentum_score"].quantile(
        1 - OFFICIAL_STRATEGY.selector_config().momentum_top_fraction)
    subset = subset[subset["ablation_momentum_score"].ge(cutoff)]
    if spec.market_ema and market_trend != "up":
        subset = subset.iloc[0:0]
    if spec.stock_ema:
        subset = subset[subset["price"].gt(subset["ema200"]) & subset["ema200"].notna()]
    return subset.sort_values(["ablation_momentum_score", "ticker"],
                              ascending=[False, True]).head(OFFICIAL_STRATEGY.max_new_positions_per_window)


@dataclass
class VariantSignalProvider:
    spec: VariantSpec
    snapshots: dict[str, OfficialSnapshot]
    audit_dir: Path
    official: OfficialSignalProvider = field(init=False)

    def __post_init__(self) -> None:
        self.official = OfficialSignalProvider(self.snapshots, self.audit_dir)

    def __call__(self, date: str, account: Account,
                 day: dict[str, DailyBar]) -> list[Signal]:
        if self.spec.kind == "index":
            raise ValueError("index variants require benchmark OHLC input")
        if self.spec.code == "H":
            return self.official(date, account, day)
        snapshot = self.snapshots.get(date)
        if snapshot is None:
            return []
        if snapshot.provider.get("errors") or snapshot.provider.get("built_rows") != len(snapshot.metrics):
            raise ValueError("incomplete point-in-time snapshot")
        self.audit_dir.mkdir(parents=True, exist_ok=True)
        if self.spec.code in {"F", "G"}:
            scored, _ = run_selection(
                snapshot.metrics, OFFICIAL_STRATEGY.market, date,
                self.audit_dir / date, snapshot.provider["market_trend"],
                OFFICIAL_STRATEGY.selector_config())
            selected = build_portfolio_plan(scored, overheated=False)
        else:
            selected = variant_candidates(snapshot.metrics, date,
                                          snapshot.provider["market_trend"], self.spec)
        selected.to_csv(self.audit_dir / f"{date}_{self.spec.code}.csv", index=False)
        return [Signal(date, row.ticker, "buy",
                       target_fraction=OFFICIAL_STRATEGY.fixed_position_fraction,
                       reason=f"ablation {self.spec.code}")
                for row in selected.itertuples()
                if row.ticker not in account.positions and row.ticker in day]


def run_index_variant(frame: pd.DataFrame, spec: VariantSpec,
                      initial_cash: float):
    """Synthetic index exposure, entered/exited at next open; index is not tradable."""
    from .engine import BacktestEngine

    if spec.kind != "index":
        raise ValueError("stock variant supplied to index runner")
    required = {"date", "open", "close", "ema200"}
    if not required <= set(frame):
        raise ValueError(f"benchmark CSV missing {sorted(required - set(frame))}")
    data = frame.sort_values("date").reset_index(drop=True)
    if data["date"].duplicated().any() or len(data) < 2:
        raise ValueError("benchmark dates must be unique with at least two sessions")
    result = BacktestEngine(initial_cash, stop_enabled=False)
    cash, units, basis, realized = float(initial_cash), 0.0, 0.0, 0.0
    for index, row in data.iterrows():
        opening, close = float(row["open"]), float(row["close"])
        if not isfinite(opening) or not isfinite(close) or min(opening, close) <= 0:
            raise ValueError("invalid index OHLC")
        if index:
            prior = data.iloc[index - 1]
            target = not spec.market_ema or (
                pd.notna(prior["ema200"]) and float(prior["close"]) > float(prior["ema200"]))
            if target and units == 0:
                units = cash / opening
                basis = opening
                cash = 0.0
                result.trades.append({"ticker": "sh.000300", "side": "buy",
                                      "signal_date": str(prior["date"]),
                                      "intended_execution_date": str(row["date"]),
                                      "actual_execution_date": str(row["date"]),
                                      "signal_price": float(prior["close"]),
                                      "execution_price": opening, "quantity": units, "fee": 0.0})
            elif not target and units:
                cash = units * opening
                realized += units * (opening - basis)
                result.trades.append({"ticker": "sh.000300", "side": "sell",
                                      "signal_date": str(prior["date"]),
                                      "intended_execution_date": str(row["date"]),
                                      "actual_execution_date": str(row["date"]),
                                      "signal_price": float(prior["close"]),
                                      "execution_price": opening, "quantity": units, "fee": 0.0})
                units = 0.0
        value = units * close
        equity = cash + value
        result.daily_nav.append({"date": str(row["date"]), "cash": cash,
                                 "market_value": value, "total_equity": equity,
                                 "realized_pnl": realized,
                                 "unrealized_pnl": units * (close - basis),
                                 "fees": 0.0, "dividends": 0.0,
                                 "actual_exposure": value / equity,
                                 "daily_nav": equity / initial_cash,
                                 "pending_orders": 0})
    return result
