"""Point-in-time, cross-market stock selector."""

from .config import SelectorConfig
from .pipeline import run_selection
from .strategy import OFFICIAL_STRATEGY, OfficialStrategy

__all__ = ["SelectorConfig", "run_selection", "OfficialStrategy", "OFFICIAL_STRATEGY"]
