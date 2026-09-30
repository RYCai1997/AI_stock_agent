from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from data_public.archive import sha256
from run_prospective_shadow import load_audited_inputs


class ShadowInputTests(unittest.TestCase):
    def test_hashes_and_future_session_gate(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            bars = root / "bars.json"
            actions = root / "actions.json"
            calendar = root / "calendar.json"
            manifest = root / "manifest.json"
            bars.write_text(json.dumps([{"date": "2026-10-16", "ticker": "T0",
                                         "open": 10, "high": 11, "low": 9, "close": 10,
                                         "tradable": True, "limit_up": 11,
                                         "limit_down": 9, "is_st": False}]))
            actions.write_text("[]")
            calendar.write_text('["2026-10-16"]')
            manifest.write_text(json.dumps({
                "source": "public_fixture", "retrieved_at": "2026-10-16T16:00:00+08:00",
                "price_basis": "unadjusted", "corporate_actions_complete": True,
                "bars_sha256": sha256(bars.read_bytes()),
                "actions_sha256": sha256(actions.read_bytes()),
                "calendar_sha256": sha256(calendar.read_bytes())}))
            now = datetime(2026, 10, 16, 16, tzinfo=ZoneInfo("Asia/Shanghai"))
            loaded = load_audited_inputs(bars, actions, calendar, manifest, now)
            self.assertEqual(loaded[0][0].ticker, "T0")
            with self.assertRaisesRegex(ValueError, "unfinished"):
                load_audited_inputs(bars, actions, calendar, manifest,
                                    datetime(2026, 10, 16, 14, tzinfo=ZoneInfo("Asia/Shanghai")))
            bars.write_text("[]")
            with self.assertRaisesRegex(ValueError, "hash mismatch"):
                load_audited_inputs(bars, actions, calendar, manifest, now)
