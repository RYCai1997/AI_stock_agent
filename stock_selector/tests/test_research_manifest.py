from __future__ import annotations

import unittest

from selector.research_manifest import build_research_manifest
from selector.strategy import OFFICIAL_STRATEGY


class ResearchManifestTests(unittest.TestCase):
    def test_manifest_records_frozen_strategy_and_reproducibility_fields(self):
        kwargs = dict(
            as_of="2025-07-15",
            provider={"membership_snapshot": "2025-06-30", "requested_members": 300,
                      "built_rows": 299, "errors": {"sh.600001": "missing"},
                      "benchmark": {"price_as_of": "2025-07-15"}},
            backtest_configuration={"initial_cash": 100000},
            execution_assumptions={"timing": "next_tradable_open", "slippage": 0.001},
            generated_at="2026-09-28T00:00:00+00:00",
            provenance={"commit": "abc123", "tracked_worktree_dirty": False},
        )
        manifest = build_research_manifest(**kwargs)
        self.assertEqual(manifest, build_research_manifest(**kwargs))
        self.assertEqual(manifest["strategy_id"], OFFICIAL_STRATEGY.strategy_id)
        self.assertEqual(manifest["strategy_version"], "1.0.0")
        self.assertEqual(manifest["git"]["commit"], "abc123")
        self.assertEqual(manifest["data_provider"]["built_rows"], 299)
        self.assertEqual(manifest["backtest_configuration"]["initial_cash"], 100000)
        self.assertIn("quality", manifest["factor_definition"])
