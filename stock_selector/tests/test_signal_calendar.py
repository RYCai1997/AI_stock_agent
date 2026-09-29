from __future__ import annotations

import unittest
import csv
from pathlib import Path

from data_history.calendar import generate_signal_calendar


class SignalCalendarTests(unittest.TestCase):
    def test_frozen_calendar_has_all_65_months(self):
        path = Path(__file__).resolve().parents[1] / "data_history" / "schema" / "signal_calendar_2020-03_2025-07.csv"
        with path.open(newline="", encoding="utf-8") as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual(len(rows), 65)
        self.assertEqual(rows[0], {"month": "2020-03", "signal_date": "2020-03-16"})
        self.assertEqual(rows[-1], {"month": "2025-07", "signal_date": "2025-07-15"})
        self.assertEqual(len({row["month"] for row in rows}), 65)

    def test_first_session_on_or_after_fifteenth_is_deterministic(self):
        sessions = ["2025-02-17", "2025-01-16", "2025-01-15", "2025-02-14",
                    "2025-02-18", "2025-01-14"]
        expected = [{"month": "2025-01", "signal_date": "2025-01-15"},
                    {"month": "2025-02", "signal_date": "2025-02-17"}]
        self.assertEqual(generate_signal_calendar(sessions, "2025-01", "2025-02"), expected)
        self.assertEqual(generate_signal_calendar(list(reversed(sessions)), "2025-01", "2025-02"), expected)

    def test_missing_month_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "2025-02"):
            generate_signal_calendar(["2025-01-15", "2025-03-17"], "2025-01", "2025-03")

    def test_duplicate_source_date_fails(self):
        with self.assertRaisesRegex(ValueError, "duplicate"):
            generate_signal_calendar(["2025-01-15", "2025-01-15"], "2025-01", "2025-01")
