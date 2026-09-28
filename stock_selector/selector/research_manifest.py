"""Provenance for a selector or historical research run."""

from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .scoring import factor_profile
from .strategy import OFFICIAL_STRATEGY
from .version import APPLICATION_VERSION


def git_provenance(repo_root: Path) -> dict[str, Any]:
    def call(*args: str) -> str | None:
        try:
            return subprocess.check_output(
                ["git", *args], cwd=repo_root, stderr=subprocess.DEVNULL,
                text=True, timeout=5,
            ).strip()
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            return None

    commit = call("rev-parse", "HEAD")
    status = call("status", "--porcelain", "--untracked-files=no")
    return {"commit": commit, "tracked_worktree_dirty": None if status is None else bool(status)}


def build_research_manifest(
    *, as_of: str, provider: dict[str, Any],
    benchmark: dict[str, Any] | None = None,
    backtest_configuration: dict[str, Any] | None = None,
    execution_assumptions: dict[str, Any] | None = None,
    repo_root: Path | None = None,
    generated_at: str | None = None,
    provenance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    root = repo_root or Path(__file__).resolve().parents[2]
    strategy = OFFICIAL_STRATEGY
    return {
        "strategy_id": strategy.strategy_id,
        "strategy_version": strategy.strategy_version,
        "application_version": APPLICATION_VERSION,
        "git": provenance if provenance is not None else git_provenance(root),
        "data_provider": {
            "name": "Baostock",
            "requested_as_of": as_of,
            "membership_snapshot": provider.get("membership_snapshot"),
            "benchmark_price_as_of": provider.get("benchmark", {}).get("price_as_of"),
            "requested_members": provider.get("requested_members"),
            "built_rows": provider.get("built_rows"),
            "errors": provider.get("errors", {}),
        },
        "factor_definition": factor_profile(strategy.selector_config().factor_profile),
        "selector_configuration": strategy.selector_config().to_dict(),
        "execution_assumptions": execution_assumptions or {
            "timing": strategy.execution_timing,
            "orders_placed": False,
            "transaction_costs_modelled": False,
        },
        "benchmark_definition": benchmark or {
            "name": "CSI 300", "code": "sh.000300", "price_mode": "provider snapshot"
        },
        "backtest_configuration": backtest_configuration,
        "generated_at": generated_at or datetime.now(timezone.utc).isoformat(),
    }
