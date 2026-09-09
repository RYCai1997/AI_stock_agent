"""Single source of truth for the frozen official strategy."""

from __future__ import annotations

from dataclasses import asdict, dataclass

from .config import SelectorConfig


@dataclass(frozen=True)
class OfficialStrategy:
    strategy_id: str = "A_CSI300_QVM_TIMING_V1"
    strategy_version: str = "1.0.0"
    market: str = "A"
    universe: str = "CSI 300"
    instrument_mode: str = "spot_long_only"
    fixed_position_fraction: float = 0.10
    max_new_exposure_per_window: float = 0.30
    max_new_positions_per_window: int = 3
    overheat_return_sessions: int = 20
    overheat_return_threshold: float = 0.10
    overheat_delay_sessions: int = 5
    stop_loss_fraction: float = 0.10
    review_frequency: str = "monthly"
    confirmation_reviews: int = 2
    execution_timing: str = "next_tradable_open"
    human_approval_required: bool = True

    def selector_config(self) -> SelectorConfig:
        return SelectorConfig(
            factor_profile="a_share_v1",
            quality_quantile=0.50,
            value_min_score=20.0,
            momentum_top_fraction=0.20,
            minimum_industry_group=5,
            exclude_financials=True,
            use_relative_strength=False,
        )

    def to_dict(self) -> dict:
        result = asdict(self)
        result["selector"] = self.selector_config().to_dict()
        return result


OFFICIAL_STRATEGY = OfficialStrategy()
