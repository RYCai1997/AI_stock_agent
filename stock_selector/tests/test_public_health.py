from __future__ import annotations

import unittest

from check_public_sources import check_sources


class PublicHealthTests(unittest.TestCase):
    def test_site_reachability_is_only_degraded_and_api_bar_available(self):
        def fetch(url):
            if "push2his" in url:
                return 200, b'{"data":{"klines":["2025-07-15,1,1,1,1"]}}'
            return 200, b"<html>ok</html>"
        report = check_sources(fetch)
        self.assertEqual(report["sources"]["eastmoney"]["status"], "available")
        self.assertEqual(report["sources"]["csi"]["status"], "degraded")
        self.assertEqual(report["sources"]["baostock"]["status"], "degraded")

    def test_network_error_is_unavailable_not_credential_blocker(self):
        report = check_sources(lambda url: (_ for _ in ()).throw(OSError("offline")))
        self.assertTrue(all(item["status"] == "unavailable"
                            for item in report["sources"].values()))

    def test_baostock_available_requires_dated_sdk_parse(self):
        report = check_sources(lambda _: (200, b"<html>ok</html>"),
                               probe_baostock=lambda: True)
        self.assertEqual(report["sources"]["baostock"]["status"], "available")
        self.assertIn("dated SDK", report["sources"]["baostock"]["reason"])
