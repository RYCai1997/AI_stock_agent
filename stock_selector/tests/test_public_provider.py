from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from data_public.archive import PublicSourceArchive, SourceResult, fetch_with_fallback
from data_public.policy import CORE_FIELDS, assess_quality
from selector.providers.a_baostock import _captured_rows


class PublicProviderTests(unittest.TestCase):
    def test_baostock_sdk_table_is_captured_before_normalization(self):
        class Table:
            error_code = "0"
            fields = ["code", "pubDate"]
            def __init__(self):
                self.remaining = [["sh.600000", "2026-10-01"]]
            def next(self):
                return bool(self.remaining)
            def get_row_data(self):
                return self.remaining.pop(0)
        captured = []
        rows = _captured_rows(Table(), "query_profit_data", {"code": "sh.600000"},
                              lambda *parts: captured.append(parts))
        self.assertEqual(rows[0]["pubDate"], "2026-10-01")
        self.assertEqual(captured[0][0], "query_profit_data")
        self.assertEqual(captured[0][3], rows)

    def test_fallback_and_both_hashes_are_auditable(self):
        with tempfile.TemporaryDirectory() as folder:
            archive = PublicSourceArchive(Path(folder))
            def unavailable():
                raise OSError("official endpoint unavailable")
            def fallback():
                return SourceResult("baostock", "query_hs300_stocks",
                                    {"date": "2026-10-15"}, b"raw", b"normalized", "1")
            meta = fetch_with_fallback("csi_official", [
                ("csi_official", unavailable), ("baostock", fallback)], archive)
            self.assertEqual(meta["actual_source"], "baostock")
            self.assertIn("official endpoint unavailable", meta["fallback_reason"])
            self.assertTrue(archive.verify(meta))
            (Path(folder) / meta["normalized_path"]).write_bytes(b"tampered")
            self.assertFalse(archive.verify(meta))

    def test_core_field_missing_blocks_primary_but_auxiliary_warning_does_not(self):
        coverage = dict.fromkeys(CORE_FIELDS, True)
        quality = assess_quality(coverage, 0, ["unused_relative_strength"])
        self.assertTrue(quality["primary_eligible"])
        coverage["financial_publication_dates"] = False
        self.assertFalse(assess_quality(coverage, 0)["primary_eligible"])
