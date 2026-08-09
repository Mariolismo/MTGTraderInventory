"""Tests for marketplace fetch keying / gzip decode helpers."""

from __future__ import annotations

import gzip
import unittest
from email.message import Message

from cardtrader_inventory.ct_client import _decode_http_body
from cardtrader_inventory.models import Listing
from cardtrader_inventory.stages import market_fetch_key


def _listing(**overrides) -> Listing:
    base = dict(
        id=1,
        blueprint_id=10,
        quantity=1,
        price_cents=100,
        condition="Near Mint",
        language="en",
        foil=False,
        game_id=1,
        user_id=1,
    )
    base.update(overrides)
    return Listing(**base)


class MarketFetchKeyTests(unittest.TestCase):
    def test_splits_foil_and_language(self) -> None:
        a = market_fetch_key(_listing(blueprint_id=1, language="EN", foil=False))
        b = market_fetch_key(_listing(blueprint_id=1, language="en", foil=True))
        c = market_fetch_key(_listing(blueprint_id=1, language="it", foil=False))
        self.assertEqual(a, (1, "en", False))
        self.assertNotEqual(a, b)
        self.assertNotEqual(a, c)


class GzipDecodeTests(unittest.TestCase):
    def test_gzip_body(self) -> None:
        payload = b'{"ok":true}'
        headers = Message()
        headers["Content-Encoding"] = "gzip"
        raw = gzip.compress(payload)
        self.assertEqual(_decode_http_body(raw, headers), '{"ok":true}')

    def test_plain_body(self) -> None:
        self.assertEqual(_decode_http_body(b"hello", Message()), "hello")


if __name__ == "__main__":
    unittest.main()
