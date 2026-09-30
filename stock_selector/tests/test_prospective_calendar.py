from __future__ import annotations

import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from prospective.calendar import signal_day_decision


class ProspectiveCalendarTests(unittest.TestCase):
    def test_only_actual_first_session_after_close_is_primary(self):
        tz = ZoneInfo("Asia/Shanghai")
        sessions = ["2026-10-14", "2026-10-15", "2026-10-16"]
        self.assertTrue(signal_day_decision(datetime(2026, 10, 15, 15, 1, tzinfo=tz), sessions)
                        ["prospective_primary"])
        self.assertFalse(signal_day_decision(datetime(2026, 10, 15, 14, 59, tzinfo=tz), sessions)
                         ["prospective_primary"])
        self.assertFalse(signal_day_decision(datetime(2026, 10, 16, 15, 1, tzinfo=tz), sessions)
                         ["prospective_primary"])

    def test_past_research_date_cannot_be_prospective(self):
        result = signal_day_decision(datetime(2027, 1, 20, 16, tzinfo=ZoneInfo("Asia/Shanghai")),
                                     [], research_date="2026-10-15")
        self.assertEqual(result["evidence_label"], "retrospective_reconstruction")
        self.assertFalse(result["prospective_primary"])
