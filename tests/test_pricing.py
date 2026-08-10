"""Unit tests for pricing clamps, comps filter, and safety gates (stdlib unittest)."""

from __future__ import annotations

import unittest

from cardtrader_inventory.buyer_fees import (
    buyer_fee_cents,
    list_from_market_buyer_total,
    list_price_for_buyer_total,
)
from cardtrader_inventory.config import PricingPolicy
from cardtrader_inventory.models import Listing, MarketOffer, PlanAction, SkipReason
from cardtrader_inventory.pricing import (
    apply_clamps,
    compute_market_price,
    filter_comparable_offers,
    price_listing,
)
from cardtrader_inventory.stages import safety_check_plan, validate_export
from cardtrader_inventory.models import PlanSummary, PricingPlan


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


class BuyerFeeTests(unittest.TestCase):
    def test_strip_fee_from_market_buyer_total(self) -> None:
        # Marketplace €50.00 buyer-facing → fee 64 → list 4936; undercut 1 → list 4935
        list_cents, fee, buyer = list_from_market_buyer_total(5000, undercut_cents=1)
        self.assertEqual(fee, 64)
        self.assertEqual(list_cents, 4935)
        self.assertEqual(buyer, 4999)
        self.assertEqual(list_price_for_buyer_total(35), 25)
        self.assertEqual(buyer_fee_cents(4935), 64)

    def test_plan_target_is_list_not_buyer(self) -> None:
        policy = PricingPolicy(
            min_comparable_offers=3,
            market_median_window=5,
            buyer_total_undercut_cents=1,
            min_change_cents=1,
            min_change_pct=0.0,
            max_decrease_pct=100.0,
        )
        listing = _listing(id=999, price_cents=5100)
        ladder = [4664, 4834, 5071, 5763, 5878]
        offers = [_offer(product_id=i, price_cents=p) for i, p in enumerate(ladder, start=1)]
        row = price_listing(listing, offers, policy, exclude_user_id=42)
        self.assertEqual(row.market_price_cents, 5071)  # buyer-facing market
        # 5071 − 1 → buyer 5070 → list 5006 (+64 fee)
        self.assertEqual(row.target_price_cents, 5006)
        self.assertEqual(row.proposed_price_cents, 5006)
        self.assertIn("list=5006+fee=64", row.reason)


class PricingTests(unittest.TestCase):
    def test_filter_requires_language_condition_foil(self) -> None:
        listing = _listing(id=999)
        offers = [
            _offer(product_id=1, price_cents=500, ct_zero=True),
            _offer(product_id=2, price_cents=600, language="it", ct_zero=True),
            _offer(product_id=3, price_cents=700, condition="Played", ct_zero=True),
            _offer(product_id=4, price_cents=800, foil=True, ct_zero=True),
            _offer(product_id=5, price_cents=550, seller_user_id=42, ct_zero=True),
            _offer(product_id=6, price_cents=400, ct_zero=False),
        ]
        matched = filter_comparable_offers(offers, listing, exclude_user_id=42)
        self.assertEqual([o.product_id for o in matched], [1])

    def test_nm_and_sp_share_condition_bucket(self) -> None:
        listing = _listing(id=999, condition="Slightly Played")
        offers = [
            _offer(product_id=1, price_cents=4664, condition="Near Mint", ct_zero=True),
            _offer(product_id=2, price_cents=4834, condition="Near Mint", ct_zero=True),
            _offer(product_id=3, price_cents=5071, condition="Near Mint", ct_zero=True),
            _offer(product_id=4, price_cents=5878, condition="Slightly Played", ct_zero=True),
            _offer(product_id=5, price_cents=3860, condition="Near Mint", ct_zero=False),
        ]
        matched = filter_comparable_offers(offers, listing, exclude_user_id=42)
        self.assertEqual([o.product_id for o in matched], [1, 2, 3, 4])
        policy = PricingPolicy(
            min_comparable_offers=3,
            market_median_window=5,
            buyer_total_undercut_cents=0,
        )
        row = price_listing(listing, offers, policy, exclude_user_id=42)
        # 4 comps → 3rd-lowest = 5071
        self.assertEqual(row.market_price_cents, 5071)
        self.assertIn("third=", row.reason)

    def test_median_of_first_five(self) -> None:
        # Bloom-like ladder: spread ~26% < 50%; median of 5 = 5071
        ladder = [4664, 4834, 5071, 5763, 5878]
        policy = PricingPolicy(
            min_comparable_offers=3,
            market_median_window=5,
            buyer_total_undercut_cents=1,
            min_change_cents=1,
            min_change_pct=0.0,
        )
        selection = compute_market_price(ladder, policy)
        self.assertEqual(selection.method, "median5")
        self.assertEqual(selection.market_cents, 5071)

        listing = _listing(id=999, price_cents=4800)
        offers = [_offer(product_id=i, price_cents=p) for i, p in enumerate(ladder, start=1)]
        row = price_listing(listing, offers, policy, exclude_user_id=42)
        self.assertEqual(row.action, PlanAction.UPDATE)
        self.assertEqual(row.market_price_cents, 5071)
        self.assertEqual(row.proposed_price_cents, 5006)  # strip fee + undercut 1¢
        self.assertTrue(row.reason.startswith("update:"))
        self.assertIn("list=5006+fee=64", row.reason)

    def test_three_comps_uses_third(self) -> None:
        policy = PricingPolicy(
            min_comparable_offers=3,
            market_median_window=5,
            max_decrease_pct=5.0,
            minimum_floor_cents=5,
        )
        # Tight ladder (spread 11%); previous high so 5% down clamp engages
        listing = _listing(id=999, price_cents=1200)
        offers = [
            _offer(product_id=1, price_cents=900),
            _offer(product_id=2, price_cents=950),
            _offer(product_id=3, price_cents=1000),
        ]
        row = price_listing(listing, offers, policy, exclude_user_id=42)
        self.assertEqual(row.action, PlanAction.UPDATE)
        self.assertEqual(row.market_price_cents, 1000)
        self.assertEqual(row.proposed_price_cents, 1140)  # 5% down clamp from 1200
        self.assertTrue(row.clamp_decrease)
        self.assertIn("third=", row.reason)

    def test_wide_spread_skip_when_min_above_threshold(self) -> None:
        policy = PricingPolicy(
            min_comparable_offers=3,
            market_median_window=5,
            max_comp_spread_pct=100.0,
            comp_spread_min_price_cents=500,
            max_decrease_pct=100.0,
            buyer_total_undercut_cents=0,
        )
        listing = _listing(id=999, price_cents=1000)
        # Cheapest window min €6; (2000-600)/600 = 2.33 > 100% → skip
        offers = [
            _offer(product_id=1, price_cents=600),
            _offer(product_id=2, price_cents=700),
            _offer(product_id=3, price_cents=800),
            _offer(product_id=4, price_cents=900),
            _offer(product_id=5, price_cents=2000),
        ]
        row = price_listing(listing, offers, policy, exclude_user_id=42)
        self.assertEqual(row.action, PlanAction.SKIP)
        self.assertEqual(row.skip_reason, SkipReason.WIDE_SPREAD)
        self.assertTrue(row.reason.startswith("skip:wide_spread"))

    def test_wide_spread_ignored_when_window_min_at_or_below_5_eur(self) -> None:
        """Leo-like bulk ladder: high % spread but cheap — still price."""
        policy = PricingPolicy(
            min_comparable_offers=3,
            market_median_window=5,
            max_comp_spread_pct=50.0,  # would trip ratio if applied
            comp_spread_min_price_cents=500,
            max_decrease_pct=100.0,
            buyer_total_undercut_cents=0,
            minimum_floor_cents=5,
        )
        listing = _listing(id=999, price_cents=999_999)  # sentinel clear
        offers = [
            _offer(product_id=1, price_cents=18),
            _offer(product_id=2, price_cents=24),
            _offer(product_id=3, price_cents=26),
            _offer(product_id=4, price_cents=29),
            _offer(product_id=5, price_cents=29),
        ]
        row = price_listing(listing, offers, policy, exclude_user_id=42)
        self.assertEqual(row.action, PlanAction.UPDATE)
        self.assertEqual(row.market_price_cents, 26)  # median of 5
        self.assertTrue(row.sentinel_clear)

    def test_dead_band_keep_when_configured(self) -> None:
        """Dead band is opt-in; defaults are 0 (disabled)."""
        policy = PricingPolicy(
            min_comparable_offers=3,
            market_median_window=5,
            min_change_cents=5,
            min_change_pct=1.0,
            buyer_total_undercut_cents=0,
        )
        # previous list 1000; market buyer 1000 → strip fee → list 985 → large Δ
        # Use previous already at stripped list so strip-only is no_change.
        listing = _listing(id=999, price_cents=985)
        offers = [
            _offer(product_id=i, price_cents=p)
            for i, p in enumerate([996, 998, 1000, 1002, 1004], start=1)
        ]
        row = price_listing(listing, offers, policy, exclude_user_id=42)
        self.assertEqual(row.action, PlanAction.KEEP)
        self.assertEqual(row.skip_reason, SkipReason.NO_CHANGE)

        # Tiny buyer bump: median 1004 → list 989; |Δ| from 985 = 4 < band 10
        offers2 = [
            _offer(product_id=i, price_cents=p)
            for i, p in enumerate([1000, 1002, 1004, 1006, 1008], start=1)
        ]
        row2 = price_listing(listing, offers2, policy, exclude_user_id=42)
        self.assertEqual(row2.action, PlanAction.KEEP)
        self.assertEqual(row2.skip_reason, SkipReason.DEAD_BAND)
        self.assertIn("dead_band", row2.reason)

    def test_one_cent_update_passes_without_dead_band(self) -> None:
        policy = PricingPolicy(
            min_comparable_offers=3,
            market_median_window=5,
            buyer_total_undercut_cents=0,
            max_decrease_pct=100.0,
        )
        self.assertEqual(policy.min_change_cents, 0)
        self.assertEqual(policy.min_change_pct, 0.0)
        # previous list 985; market buyer 1004 → list 989 → Δ=+4 should UPDATE
        listing = _listing(id=999, price_cents=985)
        offers = [
            _offer(product_id=i, price_cents=p)
            for i, p in enumerate([1000, 1002, 1004, 1006, 1008], start=1)
        ]
        row = price_listing(listing, offers, policy, exclude_user_id=42)
        self.assertEqual(row.action, PlanAction.UPDATE)
        self.assertEqual(row.proposed_price_cents, 989)

    def test_clamp_decrease(self) -> None:
        policy = PricingPolicy(max_decrease_pct=5.0)
        proposed, dec, inc = apply_clamps(1000, 500, policy, is_sentinel=False)
        self.assertEqual(proposed, 950)
        self.assertTrue(dec)
        self.assertFalse(inc)

    def test_no_upside_clamp(self) -> None:
        policy = PricingPolicy(max_decrease_pct=5.0)
        proposed, dec, inc = apply_clamps(1000, 5000, policy, is_sentinel=False)
        self.assertEqual(proposed, 5000)
        self.assertFalse(dec)
        self.assertFalse(inc)

    def test_sentinel_skips_clamps(self) -> None:
        policy = PricingPolicy(minimum_floor_cents=5)
        proposed, dec, inc = apply_clamps(999_999, 500, policy, is_sentinel=True)
        self.assertEqual(proposed, 500)
        self.assertFalse(dec)
        self.assertFalse(inc)

    def test_rarity_foil_floors(self) -> None:
        from cardtrader_inventory.config import merge_rarity_floors

        policy = PricingPolicy(rarity_floor_cents=merge_rarity_floors())
        self.assertEqual(policy.floor_cents_for(rarity="Common", foil=False), (5, "common"))
        self.assertEqual(policy.floor_cents_for(rarity="Rare", foil=False), (20, "rare"))
        self.assertEqual(policy.floor_cents_for(rarity="Rare", foil=True), (20, "foil_rare"))
        self.assertEqual(
            policy.floor_cents_for(rarity="Mythic Rare", foil=True), (50, "foil_mythic")
        )
        self.assertEqual(
            policy.floor_cents_for(rarity="Basic Land", foil=True), (10, "foil_basic_land")
        )
        self.assertEqual(
            policy.floor_cents_for(rarity="Masterpiece", foil=True), (50, "masterpiece")
        )
        self.assertEqual(policy.floor_cents_for(rarity="", foil=False), (5, "other"))
        self.assertEqual(policy.floor_cents_for(rarity="Promo", foil=False), (5, "other"))

    def test_rarity_floor_raises_target(self) -> None:
        from cardtrader_inventory.config import merge_rarity_floors

        policy = PricingPolicy(
            min_comparable_offers=3,
            market_median_window=5,
            rarity_floor_cents=merge_rarity_floors({"mythic": 50}),
            max_decrease_pct=5.0,
        )
        listing = _listing(id=999, price_cents=30, rarity="Mythic", foil=False)
        # Tight ladder under mythic floor → target lifted to 50¢
        offers = [
            _offer(product_id=1, price_cents=10),
            _offer(product_id=2, price_cents=12),
            _offer(product_id=3, price_cents=14),
        ]
        row = price_listing(listing, offers, policy, exclude_user_id=42)
        self.assertEqual(row.market_price_cents, 14)
        self.assertEqual(row.target_price_cents, 50)
        self.assertEqual(row.proposed_price_cents, 50)
        self.assertIn("floor=mythic:50", row.reason)

    def test_insufficient_comps_skip(self) -> None:
        policy = PricingPolicy(min_comparable_offers=3, insufficient_comps_fallback="skip")
        listing = _listing(id=999)
        offers = [_offer(product_id=1, price_cents=100), _offer(product_id=2, price_cents=120)]
        row = price_listing(listing, offers, policy, exclude_user_id=42)
        self.assertEqual(row.action, PlanAction.SKIP)
        self.assertEqual(row.skip_reason, SkipReason.INSUFFICIENT_COMPS)
        self.assertIn("insufficient_comps", row.reason)


class SafetyTests(unittest.TestCase):
    def test_validate_export_empty(self) -> None:
        result = validate_export([], PricingPolicy())
        self.assertFalse(result.ok)

    def test_validate_export_excludes_missing_attrs(self) -> None:
        good = _listing(id=1, condition="Near Mint", language="en")
        bad = _listing(id=2, condition="", language="")
        result = validate_export([good, bad], PricingPolicy())
        self.assertTrue(result.ok)
        self.assertEqual([lst.id for lst in result.priceable], [1])
        self.assertEqual(result.excluded_missing_attrs, [2])

    def test_plan_safety_allows_high_volume(self) -> None:
        policy = PricingPolicy(max_proposed_pct=100.0, max_proposed_absolute=50_000)
        plan = PricingPlan(
            pricing_run_id="t",
            mode="DRY_RUN",
            rows=[],
            summary=PlanSummary(price_updates_proposed=90, cards_processed=100),
        )
        result = safety_check_plan(plan, listing_count=100, policy=policy)
        self.assertTrue(result.ok)

    def test_plan_safety_steep_drop(self) -> None:
        from cardtrader_inventory.models import PlanRow

        policy = PricingPolicy(max_allowed_decrease_pct=5.0, max_decrease_pct=5.0)
        row = PlanRow(
            listing_id=1,
            blueprint_id=10,
            previous_price_cents=1000,
            proposed_price_cents=800,  # -20%
            action=PlanAction.UPDATE,
        )
        plan = PricingPlan(
            pricing_run_id="t",
            mode="DRY_RUN",
            rows=[row],
            summary=PlanSummary(price_updates_proposed=1, cards_processed=1),
        )
        result = safety_check_plan(plan, listing_count=1, policy=policy)
        self.assertFalse(result.ok)


if __name__ == "__main__":
    unittest.main()
