"""Point-in-time, cross-market stock selector."""

from .config import SelectorConfig
from .pipeline import run_selection

__all__ = ["SelectorConfig", "run_selection"]

