from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from data_history.providers.tushare import (MissingCredentials, ProviderResponseError,
                                             TushareProProvider)


class TushareProviderTests(unittest.TestCase):
    def test_response_is_cached_with_hash_and_no_token(self):
        calls = []
        def transport(body):
            calls.append(json.loads(body))
            return json.dumps({"code": 0, "msg": None, "data": {
                "fields": ["ts_code", "trade_date", "close"],
                "items": [["600000.SH", "20200316", 10.0]]}}).encode()
        with tempfile.TemporaryDirectory() as folder:
            provider = TushareProProvider(Path(folder), token="secret-token", transport=transport)
            first = provider.fetch("daily", {"trade_date": "20200316"},
                                   ("ts_code", "trade_date", "close"))
            second = TushareProProvider(Path(folder), token="").fetch(
                "daily", {"trade_date": "20200316"},
                ("ts_code", "trade_date", "close"), offline=True)
            self.assertEqual(first.rows, second.rows)
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0]["token"], "secret-token")
            self.assertNotIn("secret-token", "".join(
                path.read_text(encoding="utf-8") for path in Path(folder).rglob("*.json")))
            self.assertEqual(first.metadata["coverage_status"], "response_only_not_audited")

    def test_missing_credentials_and_cache_tamper_fail_closed(self):
        with tempfile.TemporaryDirectory() as folder:
            provider = TushareProProvider(Path(folder), token="")
            with self.assertRaises(MissingCredentials):
                provider.fetch("daily", {"trade_date": "20200316"})
            with self.assertRaises(FileNotFoundError):
                provider.fetch("daily", {"trade_date": "20200316"}, offline=True)
            good = TushareProProvider(Path(folder), token="x", transport=lambda body: json.dumps({
                "code": 0, "data": {"fields": ["a"], "items": [[1]]}}).encode())
            good.fetch("daily", {"trade_date": "20200316"})
            next(Path(folder).glob("raw/*.json")).write_bytes(b"{}")
            with self.assertRaises(ProviderResponseError):
                good.fetch("daily", {"trade_date": "20200316"}, offline=True)

    def test_provider_error_is_not_cached(self):
        with tempfile.TemporaryDirectory() as folder:
            provider = TushareProProvider(Path(folder), token="x", transport=lambda body: json.dumps({
                "code": 2002, "msg": "permission denied", "data": None}).encode())
            with self.assertRaises(ProviderResponseError):
                provider.fetch("index_weight", {"index_code": "000300.SH"})
            self.assertEqual(list(Path(folder).rglob("*.json")), [])
