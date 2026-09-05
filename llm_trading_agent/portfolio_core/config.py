"""Configuration for the ETF rotation research model."""

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class RotationConfig:
    """Parameters that are fixed before a backtest starts."""

    target_exposure: float = 0.70
    max_asset_weight: float = 0.30
    top_n: int = 4
    rebalance_frequency: str = "monthly"
    momentum_6m_days: int = 126
    momentum_12m_days: int = 252
    skip_recent_days: int = 21
    trend_days: int = 200
    momentum_6m_weight: float = 0.50
    momentum_12_1_weight: float = 0.50
    require_positive_momentum: bool = True
    transaction_cost_bps: float = 10.0
    annualization_days: int = 252

    def __post_init__(self) -> None:
        if not 0 < self.target_exposure <= 1:
            raise ValueError("target_exposure must be in (0, 1]")
        if not 0 < self.max_asset_weight <= 1:
            raise ValueError("max_asset_weight must be in (0, 1]")
        if self.top_n < 1:
            raise ValueError("top_n must be at least 1")
        if self.rebalance_frequency != "monthly":
            raise ValueError("ETF Rotation V1 currently supports monthly rebalancing only")
        if self.momentum_12m_days <= self.skip_recent_days:
            raise ValueError("momentum_12m_days must exceed skip_recent_days")
        if self.trend_days < 2:
            raise ValueError("trend_days must be at least 2")
        factor_weight = self.momentum_6m_weight + self.momentum_12_1_weight
        if abs(factor_weight - 1.0) > 1e-9:
            raise ValueError("momentum factor weights must sum to 1")
        if self.transaction_cost_bps < 0:
            raise ValueError("transaction_cost_bps cannot be negative")

    @property
    def cost_rate(self) -> float:
        return self.transaction_cost_bps / 10_000.0

    def to_dict(self) -> dict:
        return asdict(self)
