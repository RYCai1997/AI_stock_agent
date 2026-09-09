"""Deterministic, long-only ETF portfolio research core."""

from .backtest import BacktestResult, run_rotation_backtest
from .config import RotationConfig

__all__ = ["BacktestResult", "RotationConfig", "run_rotation_backtest"]
