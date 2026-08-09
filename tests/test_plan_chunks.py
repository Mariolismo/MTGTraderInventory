"""Tests for fetch-key chunking and plan merge."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from cardtrader_inventory.aws.plan_orchestrate import merge_chunk_plans
from cardtrader_inventory.config import PricingPolicy
from cardtrader_inventory.listing_serde import (
    listing_from_dict,
    listing_to_dict,
    listings_from_jsonl_text,
    listings_to_jsonl_bytes,
)
from cardtrader_inventory.models import (
    Listing,
    PlanAction,
    PlanRow,
    PlanSummary,
    PricingPlan,
    SkipReason,
)
from cardtrader_inventory.stages import (
    collect_fetch_keys,
    decode_fetch_key,
    encode_fetch_key,
    generate_pricing_plan_for_keys,
    merge_plan_summaries,
    slice_fetch_key_chunks,
)


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
        user_id=42,
        name_en="Test",
    )
    base.update(overrides)
    return Listing(**base)


class ChunkSliceTests(unittest.TestCase):
    def test_slice_fetch_key_chunks(self) -> None:
        keys = [(i, "en", False) for i in range(5)]
        chunks = slice_fetch_key_chunks(keys, 2)
        self.assertEqual(len(chunks), 3)
        self.assertEqual(len(chunks[0]), 2)
        self.assertEqual(len(chunks[2]), 1)

    def test_empty_keys_one_chunk(self) -> None:
        self.assertEqual(slice_fetch_key_chunks([], 2000), [[]])

    def test_encode_decode_roundtrip(self) -> None:
        key = (123, "en", True)
        self.assertEqual(decode_fetch_key(encode_fetch_key(key)), key)

    def test_collect_fetch_keys_unique(self) -> None:
        listings = [
            _listing(id=1, blueprint_id=1, language="EN", foil=False),
            _listing(id=2, blueprint_id=1, language="en", foil=False),
            _listing(id=3, blueprint_id=1, language="en", foil=True),
        ]
        keys = collect_fetch_keys(listings, PricingPolicy())
        self.assertEqual(len(keys), 2)


class ListingSerdeTests(unittest.TestCase):
    def test_jsonl_roundtrip(self) -> None:
        listings = [_listing(id=1), _listing(id=2, blueprint_id=99, foil=True)]
        text = listings_to_jsonl_bytes(listings).decode("utf-8")
        back = listings_from_jsonl_text(text)
        self.assertEqual(len(back), 2)
        self.assertEqual(back[1].blueprint_id, 99)
        self.assertTrue(back[1].foil)
        d = listing_to_dict(listings[0])
        self.assertEqual(listing_from_dict(d).id, 1)


class MergeSummaryTests(unittest.TestCase):
    def test_merge_plan_summaries(self) -> None:
        a = PlanSummary(cards_processed=10, price_updates_proposed=3, no_change=7)
        b = PlanSummary(cards_processed=5, price_updates_proposed=2, skipped_dead_band=3)
        m = merge_plan_summaries([a, b])
        self.assertEqual(m.cards_processed, 15)
        self.assertEqual(m.price_updates_proposed, 5)
        self.assertEqual(m.no_change, 7)
        self.assertEqual(m.skipped_dead_band, 3)

    def test_merge_chunk_plans_safety(self) -> None:
        policy = PricingPolicy()
        rows = [
            PlanRow(
                listing_id=1,
                blueprint_id=1,
                previous_price_cents=100,
                proposed_price_cents=100,
                action=PlanAction.KEEP,
                skip_reason=SkipReason.NO_CHANGE,
            )
        ]
        plan = PricingPlan(
            pricing_run_id="r1",
            mode="DRY_RUN",
            rows=rows,
            summary=PlanSummary(cards_processed=1, no_change=1),
        )
        result = merge_chunk_plans(
            pricing_run_id="r1",
            mode="DRY_RUN",
            listing_count=1,
            priceable_count=1,
            excluded_missing_attrs=[],
            chunk_plans=[plan],
            policy=policy,
        )
        self.assertTrue(result.safety.ok)
        self.assertEqual(len(result.plan.rows), 1)


class PlanForKeysTests(unittest.TestCase):
    def test_only_prices_matching_keys(self) -> None:
        policy = PricingPolicy()
        listings = [
            _listing(id=1, blueprint_id=10),
            _listing(id=2, blueprint_id=20),
        ]
        client = MagicMock()
        client.marketplace_products.return_value = []
        plan = generate_pricing_plan_for_keys(
            client,
            listings,
            fetch_keys=[(10, "en", False)],
            policy=policy,
            pricing_run_id="r1",
            mode="DRY_RUN",
        )
        self.assertEqual(len(plan.rows), 1)
        self.assertEqual(plan.rows[0].listing_id, 1)
        client.marketplace_products.assert_called()


if __name__ == "__main__":
    unittest.main()
