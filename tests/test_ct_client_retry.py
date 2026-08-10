"""Tests for CardTrader HTTP retry behaviour."""

from __future__ import annotations

import io
import unittest
import urllib.error
from unittest import mock

from cardtrader_inventory.config import PricingPolicy
from cardtrader_inventory.ct_client import (
    CardTraderClient,
    CardTraderError,
    _backoff_sleep_s,
    _parse_retry_after_s,
    _retry_allowed,
)
from cardtrader_inventory.rate_limiter import RateLimiter


class RetryPolicyHelpersTests(unittest.TestCase):
    def test_get_retries_503_and_network(self) -> None:
        self.assertTrue(_retry_allowed(method="GET", status=503, is_network=False))
        self.assertTrue(_retry_allowed(method="GET", status=None, is_network=True))

    def test_post_retries_only_429(self) -> None:
        self.assertTrue(_retry_allowed(method="POST", status=429, is_network=False))
        self.assertFalse(_retry_allowed(method="POST", status=503, is_network=False))
        self.assertFalse(_retry_allowed(method="POST", status=None, is_network=True))

    def test_backoff_respects_cap_and_retry_after(self) -> None:
        with mock.patch("cardtrader_inventory.ct_client.random.uniform", return_value=0.5):
            sleep_s = _backoff_sleep_s(0, base_s=1.0, max_s=30.0, retry_after_s=5.0)
        self.assertEqual(sleep_s, 5.0)

    def test_parse_retry_after_seconds(self) -> None:
        self.assertEqual(_parse_retry_after_s({"Retry-After": "3"}), 3.0)


class CtHttpRetryTests(unittest.TestCase):
    def test_retries_503_then_succeeds(self) -> None:
        policy = PricingPolicy(
            ct_http_max_retries=3,
            ct_http_retry_base_s=0.01,
            ct_http_retry_max_s=1.0,
        )
        client = CardTraderClient("token", policy, limiter=RateLimiter(1000.0))

        ok = mock.MagicMock()
        ok.read.return_value = b'{"ok":true}'
        ok.status = 200
        ok.headers = {}
        ok.__enter__.return_value = ok
        ok.__exit__.return_value = False

        err = urllib.error.HTTPError(
            url="https://api.cardtrader.com/api/v2/x",
            code=503,
            msg="Unavailable",
            hdrs={},  # type: ignore[arg-type]
            fp=io.BytesIO(b"queue full"),
        )

        with mock.patch(
            "urllib.request.urlopen",
            side_effect=[err, err, ok],
        ) as urlopen:
            with mock.patch("time.sleep") as sleep:
                payload = client._request("GET", "/x", rate_limit=False)

        self.assertEqual(payload, {"ok": True})
        self.assertEqual(urlopen.call_count, 3)
        self.assertEqual(sleep.call_count, 2)

    def test_post_does_not_retry_503(self) -> None:
        policy = PricingPolicy(ct_http_max_retries=3, ct_http_retry_base_s=0.01)
        client = CardTraderClient("token", policy, limiter=RateLimiter(1000.0))
        err = urllib.error.HTTPError(
            url="https://api.cardtrader.com/api/v2/products/bulk_update",
            code=503,
            msg="Unavailable",
            hdrs={},  # type: ignore[arg-type]
            fp=io.BytesIO(b"queue full"),
        )
        with mock.patch("urllib.request.urlopen", side_effect=[err]) as urlopen:
            with mock.patch("time.sleep") as sleep:
                with self.assertRaises(CardTraderError):
                    client._request(
                        "POST",
                        "/products/bulk_update",
                        body={"products": []},
                        rate_limit=False,
                    )
        self.assertEqual(urlopen.call_count, 1)
        sleep.assert_not_called()

    def test_exhausts_retries_on_503(self) -> None:
        policy = PricingPolicy(ct_http_max_retries=2, ct_http_retry_base_s=0.01)
        client = CardTraderClient("token", policy, limiter=RateLimiter(1000.0))
        err = urllib.error.HTTPError(
            url="https://api.cardtrader.com/api/v2/x",
            code=503,
            msg="Unavailable",
            hdrs={},  # type: ignore[arg-type]
            fp=io.BytesIO(b"queue full"),
        )
        with mock.patch("urllib.request.urlopen", side_effect=[err, err, err]):
            with mock.patch("time.sleep"):
                with self.assertRaises(CardTraderError) as ctx:
                    client._request("GET", "/x", rate_limit=False)
        self.assertIn("503", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
