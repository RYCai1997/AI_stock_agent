from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from data_public.archive import PublicSourceArchive, SourceResult, fetch_with_fallback
from data_public.policy import CORE_FIELDS, assess_quality


class PublicProviderTests(unittest.TestCase):
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
