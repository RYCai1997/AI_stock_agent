"""Official point-in-time CSI 300 stock selector."""

from .config import SelectorConfig
from .pipeline import run_selection
from .strategy import OFFICIAL_STRATEGY, OfficialStrategy

__all__ = ["SelectorConfig", "run_selection", "OfficialStrategy", "OFFICIAL_STRATEGY"]
