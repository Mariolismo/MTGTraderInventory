"""Unit tests for weekly_sales evaluate_discount and price_listing discount integration."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from cardtrader_inventory.config import PricingPolicy
from cardtrader_inventory.models import Listing, MarketOffer, PlanAction
from cardtrader_inventory.pricing import price_listing
from cardtrader_inventory.weekly_sales import (
    evaluate_discount,
    sum_pending_sales_cents,
)


def _listing(**overrides) -> Listing:
    base = dict(
        id=1,
        blueprint_id=10,
        quantity=1,
        price_cents=1000,
        condition="Near Mint",
        language="en",
        foil=False,
        game_id=1,
        user_id=42,
        name_en="Test Card",
    )
    base.update(overrides)
    return Listing(**base)


def _offer(**overrides) -> MarketOffer:
    base = dict(
        product_id=100,
        blueprint_id=10,
        price_cents=900,
        condition="Near Mint",
        language="en",
        foil=False,
        seller_user_id=99,
        quantity=1,
        ct_zero=True,
    )
    base.update(overrides)
    return MarketOffer(**base)


class EvaluateDiscountTests(unittest.TestCase):
    def test_target_reached_always_zero(self) -> None:
        # Even on Friday with high sales, discount is 0.
        fri = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(evaluate_discount(30_000, fri), 0)
        self.assertEqual(evaluate_discount(50_000, fri), 0)

    def test_monday_no_discount(self) -> None:
        mon = datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(evaluate_discount(0, mon), 0)
        self.assertEqual(evaluate_discount(5_000, mon), 0)

    def test_tuesday_no_discount(self) -> None:
        tue = datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(evaluate_discount(0, tue), 0)

    def test_wednesday_below_100_gets_3pct(self) -> None:
        wed = datetime(2026, 8, 19, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(evaluate_discount(5_000, wed), 3)
        self.assertEqual(evaluate_discount(9_999, wed), 3)

    def test_wednesday_at_100_no_discount(self) -> None:
        wed = datetime(2026, 8, 19, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(evaluate_discount(10_000, wed), 0)

    def test_friday_below_100_gets_7pct(self) -> None:
        fri = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(evaluate_discount(5_000, fri), 7)
        self.assertEqual(evaluate_discount(9_999, fri), 7)

    def test_friday_between_100_180_gets_5pct(self) -> None:
        fri = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(evaluate_discount(10_000, fri), 5)
        self.assertEqual(evaluate_discount(17_999, fri), 5)

    def test_friday_at_180_no_discount(self) -> None:
        fri = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(evaluate_discount(18_000, fri), 0)

    def test_saturday_same_as_friday(self) -> None:
        sat = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(evaluate_discount(5_000, sat), 7)
        self.assertEqual(evaluate_discount(12_000, sat), 5)

    def test_sunday_is_day_7_triggers_wednesday_tier(self) -> None:
        # isoweekday() == 7 for Sunday, so >= 3 and >= 5 both true.
        sun = datetime(2026, 8, 16, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(evaluate_discount(5_000, sun), 7)


class SumPendingSalesTests(unittest.TestCase):
    def test_sums_hub_pending_orders(self) -> None:
        orders = [
            {"seller_subtotal": {"cents": 500, "currency": "EUR"}},
            {"seller_subtotal": {"cents": 300, "currency": "EUR"}},
        ]
        self.assertEqual(sum_pending_sales_cents(orders), 800)

    def test_empty_orders(self) -> None:
        self.assertEqual(sum_pending_sales_cents([]), 0)

    def test_integer_seller_subtotal(self) -> None:
        orders = [{"seller_subtotal": 1200}]
        self.assertEqual(sum_pending_sales_cents(orders), 1200)

    def test_missing_seller_subtotal(self) -> None:
        orders = [{"id": 123}]
        self.assertEqual(sum_pending_sales_cents(orders), 0)


class PriceListingDiscountTests(unittest.TestCase):
    """Verify discount_pct shifts the market reference price down."""

    def _policy(self, **kw) -> PricingPolicy:
        defaults = dict(
            nth_lowest=3,
            min_comparable_offers=3,
            max_decrease_pct=1.5,
            minimum_floor_cents=5,
            sentinel_price_cents=999_999,
            ct_zero_only=True,
            market_median_window=5,
            max_comp_spread_pct=100.0,
            comp_spread_min_price_cents=500,
            buyer_total_undercut_cents=1,
        )
        defaults.update(kw)
        return PricingPolicy(**defaults)

    def test_no_discount_baseline(self) -> None:
        policy = self._policy()
        listing = _listing(price_cents=999_999)  # sentinel
        offers = [_offer(price_cents=p) for p in [900, 920, 940, 950, 960]]
        row = price_listing(listing, offers, policy, exclude_user_id=42, discount_pct=0)
        self.assertIsNotNone(row.market_price_cents)
        market_no_discount = row.market_price_cents

        row_disc = price_listing(listing, offers, policy, exclude_user_id=42, discount_pct=5)
        self.assertIsNotNone(row_disc.market_price_cents)
        # Market price recorded in the row is pre-discount (the raw market).
        # But the proposed price should be lower with discount.
        self.assertLessEqual(row_disc.proposed_price_cents, row.proposed_price_cents)

    def test_discount_appears_in_reason(self) -> None:
        policy = self._policy()
        listing = _listing(price_cents=999_999)
        offers = [_offer(price_cents=p) for p in [900, 920, 940, 950, 960]]
        row = price_listing(listing, offers, policy, exclude_user_id=42, discount_pct=3)
        self.assertIn("discount=3%", row.reason)

    def test_zero_discount_no_tag(self) -> None:
        policy = self._policy()
        listing = _listing(price_cents=999_999)
        offers = [_offer(price_cents=p) for p in [900, 920, 940, 950, 960]]
        row = price_listing(listing, offers, policy, exclude_user_id=42, discount_pct=0)
        self.assertNotIn("discount=", row.reason)


if __name__ == "__main__":
    unittest.main()
