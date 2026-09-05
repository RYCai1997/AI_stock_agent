from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class SelectorConfig:
    quality_quantile: float = 0.50
    value_min_score: float = 20.0
    momentum_top_fraction: float = 0.20
    minimum_industry_group: int = 5
    exclude_financials: bool = True

    def __post_init__(self) -> None:
        if not 0 < self.quality_quantile < 1:
            raise ValueError("quality_quantile must be in (0, 1)")
        if not 0 <= self.value_min_score < 100:
            raise ValueError("value_min_score must be in [0, 100)")
        if not 0 < self.momentum_top_fraction <= 1:
            raise ValueError("momentum_top_fraction must be in (0, 1]")
        if self.minimum_industry_group < 2:
            raise ValueError("minimum_industry_group must be at least 2")

    def to_dict(self) -> dict:
        return asdict(self)

