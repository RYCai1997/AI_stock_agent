from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from audit_universe import compare_memberships


class UniverseAuditTests(unittest.TestCase):
    def test_unverified_date_is_pending_and_official_mismatch_is_visible(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            snapshot = root / "2025-07-15"
            snapshot.mkdir()
            (snapshot / "official_run_metadata.json").write_text(json.dumps({
                "provider": {"member_codes": ["A", "B"],
                             "membership_update_min": "2025-06-01",
                             "membership_update_max": "2025-07-01",
                             "membership_update_unique_count": 2}
            }), encoding="utf-8")
            pending = compare_memberships(root)
            self.assertEqual(pending.iloc[0]["status"], "pending_official_notice")
            official = pd.DataFrame([
                {"as_of": "2025-07-15", "ticker": "A", "notice_url": "https://example.org/notice"},
                {"as_of": "2025-07-15", "ticker": "C", "notice_url": "https://example.org/notice"},
            ])
            checked = compare_memberships(root, official)
            self.assertEqual(checked.iloc[0]["status"], "mismatch")
            self.assertEqual(checked.iloc[0]["missing_from_provider"], "C")
            self.assertEqual(checked.iloc[0]["unexpected_in_provider"], "B")
