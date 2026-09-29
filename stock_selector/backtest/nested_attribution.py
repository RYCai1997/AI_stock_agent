"""Research-only nested ablation with one mechanism changed per adjacent step."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from selector.pipeline import validate_input, run_selection
from selector.portfolio_plan import build_portfolio_plan
from selector.scoring import percentile, score_frame
from selector.strategy import OFFICIAL_STRATEGY

from .account import Account
from .engine import DailyBar, Signal
from .official import OfficialSignalProvider, OfficialSnapshot, validate_snapshot_provider_dates


@dataclass(frozen=True)
class NestedLayer:
    code: str
    label: str
    changed_layer: str
    market_ema: bool = False
    stock_ema: bool = False
    quality: bool = False
    value: bool = False
    stop_loss: bool = False
    overheat_delay: bool = False
    monthly_exit: bool = False


NESTED_LADDER = (
    NestedLayer("R0", "Momentum", "Momentum"),
    NestedLayer("R1", "Momentum + market EMA", "market EMA", market_ema=True),
    NestedLayer("R2", "Momentum + both EMA", "stock EMA", market_ema=True, stock_ema=True),
    NestedLayer("R3", "Quality + Momentum + both EMA", "Quality", market_ema=True, stock_ema=True, quality=True),
    NestedLayer("R4", "Q/V/M + both EMA", "Value", market_ema=True, stock_ema=True, quality=True, value=True),
    NestedLayer("R5", "Q/V/M + both EMA + stop", "stop loss", market_ema=True, stock_ema=True,
                quality=True, value=True, stop_loss=True),
    NestedLayer("R6", "Q/V/M + both EMA + stop + overheat", "overheat delay", market_ema=True,
                stock_ema=True, quality=True, value=True, stop_loss=True, overheat_delay=True),
    NestedLayer("R7", "Full frozen V1", "monthly confirmation exit", market_ema=True, stock_ema=True,
                quality=True, value=True, stop_loss=True, overheat_delay=True, monthly_exit=True),
)


def changed_flags(previous: NestedLayer, current: NestedLayer) -> list[str]:
    flags = ("market_ema", "stock_ema", "quality", "value", "stop_loss",
             "overheat_delay", "monthly_exit")
    return [name for name in flags if getattr(previous, name) != getattr(current, name)]


def nested_candidates(metrics: pd.DataFrame, date: str, market_trend: str,
                      layer: NestedLayer) -> pd.DataFrame:
    clean = validate_input(metrics, OFFICIAL_STRATEGY.market, date,
                           OFFICIAL_STRATEGY.selector_config())
    scored = score_frame(clean, OFFICIAL_STRATEGY.selector_config())
    eligible = scored["security_eligible"] & scored["model_supported"] & scored[["mom_6_1", "mom_12_1"]].notna().all(axis=1)
    if layer.quality:
        eligible &= scored["quality_pass"]
    if layer.value:
        eligible &= scored["value_pass"]
    subset = scored.loc[eligible].copy()
    if subset.empty:
        return subset
    six_rank = percentile(subset["mom_6_1"])
    twelve_rank = percentile(subset["mom_12_1"])
    if (OFFICIAL_STRATEGY.selector_config().use_relative_strength
            and "relative_strength" in subset
            and subset["relative_strength"].notna().all()):
        relative_rank = percentile(subset["relative_strength"])
        subset["research_momentum_score"] = .30 * six_rank + .40 * twelve_rank + .30 * relative_rank
    else:
        subset["research_momentum_score"] = .50 * six_rank + .50 * twelve_rank
    cutoff = subset["research_momentum_score"].quantile(
        1 - OFFICIAL_STRATEGY.selector_config().momentum_top_fraction)
    subset = subset[subset["research_momentum_score"].ge(cutoff)]
    if layer.market_ema and market_trend != "up":
        subset = subset.iloc[0:0]
    if layer.stock_ema:
        subset = subset[subset["ema200"].notna() & subset["price"].gt(subset["ema200"])]
    return subset.sort_values(["research_momentum_score", "ticker"],
                              ascending=[False, True]).head(OFFICIAL_STRATEGY.max_new_positions_per_window)


@dataclass
class NestedSignalProvider:
    layer: NestedLayer
    snapshots: dict[str, OfficialSnapshot]
    audit_dir: Path
    official: OfficialSignalProvider = field(init=False)

    def __post_init__(self) -> None:
        self.official = OfficialSignalProvider(self.snapshots, self.audit_dir)

    def __call__(self, date: str, account: Account,
                 day: dict[str, DailyBar]) -> list[Signal]:
        if self.layer.monthly_exit:
            return self.official(date, account, day)
        snapshot = self.snapshots.get(date)
        if snapshot is None:
            return []
        validate_snapshot_provider_dates(snapshot.provider, date)
        if snapshot.provider.get("errors") or snapshot.provider.get("built_rows") != len(snapshot.metrics):
            raise ValueError("incomplete point-in-time snapshot")
        self.audit_dir.mkdir(parents=True, exist_ok=True)
        market_return = snapshot.provider.get("benchmark", {}).get("return_20d")
        overheated = (self.layer.overheat_delay and market_return is not None
                      and market_return > OFFICIAL_STRATEGY.overheat_return_threshold)
        if self.layer.quality and self.layer.value and self.layer.stock_ema and self.layer.market_ema:
            scored, _ = run_selection(
                snapshot.metrics, OFFICIAL_STRATEGY.market, date,
                self.audit_dir / date, snapshot.provider["market_trend"],
                OFFICIAL_STRATEGY.selector_config())
            selected = build_portfolio_plan(scored, strategy=OFFICIAL_STRATEGY,
                                            overheated=overheated)
        else:
            selected = nested_candidates(snapshot.metrics, date,
                                         snapshot.provider["market_trend"], self.layer)
        selected.to_csv(self.audit_dir / f"{date}_{self.layer.code}.csv", index=False)
        return [Signal(date, row.ticker, "buy",
                       target_fraction=float(row.target_fraction) if "target_fraction" in selected else
                       OFFICIAL_STRATEGY.fixed_position_fraction,
                       reason=f"nested attribution {self.layer.code}",
                       delay_sessions=int(row.entry_delay_sessions) if "entry_delay_sessions" in selected else 0)
                for row in selected.itertuples()
                if row.ticker not in account.positions and row.ticker in day]


def incremental_attribution(rows: list[dict]) -> pd.DataFrame:
    by_code = {row["variant"]: row for row in rows}
    if set(by_code) != {layer.code for layer in NESTED_LADDER}:
        raise ValueError("complete nested ladder results are required")
    output = []
    previous = None
    for layer in NESTED_LADDER:
        current = by_code[layer.code]
        output.append({"variant": layer.code, "label": layer.label,
                       "changed_layer": layer.changed_layer,
                       "cumulative_return": current["cumulative_return"],
                       "incremental_cumulative_return": None if previous is None else
                       current["cumulative_return"] - previous["cumulative_return"],
                       "maximum_drawdown": current["maximum_drawdown"],
                       "incremental_drawdown": None if previous is None else
                       current["maximum_drawdown"] - previous["maximum_drawdown"]})
        previous = current
    return pd.DataFrame(output)
