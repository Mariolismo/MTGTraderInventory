"""Unit tests for LIVE apply helpers (no network)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from cardtrader_inventory.idempotency import FileBatchIdempotencyStore
from cardtrader_inventory.apply import (
    build_bulk_payload,
    cents_to_ct_price,
    chunk_rows,
    filter_stale_updates,
    update_rows_from_plan,
)
from cardtrader_inventory.models import (
    ApplyBatchResult,
    Listing,
    PlanAction,
    PlanRow,
)


def _row(**overrides) -> PlanRow:
    base = dict(
        listing_id=1,
        blueprint_id=10,
        previous_price_cents=1000,
        proposed_price_cents=1100,
        action=PlanAction.UPDATE,
        quantity=1,
    )
    base.update(overrides)
    return PlanRow(**base)


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
    )
    base.update(overrides)
    return Listing(**base)


class ApplyHelperTests(unittest.TestCase):
    def test_update_rows_filters_non_updates(self) -> None:
        rows = [
            _row(listing_id=1),
            _row(listing_id=2, action=PlanAction.KEEP, proposed_price_cents=None),
            _row(listing_id=3, proposed_price_cents=None),
        ]
        self.assertEqual([r.listing_id for r in update_rows_from_plan(rows)], [1])

    def test_stale_on_price_or_qty_or_missing(self) -> None:
        updates = [
            _row(listing_id=1),
            _row(listing_id=2, previous_price_cents=2000),
            _row(listing_id=3, quantity=2),
            _row(listing_id=4),
        ]
        current = {
            1: _listing(id=1, price_cents=1000),
            2: _listing(id=2, price_cents=1999),  # price drifted
            3: _listing(id=3, price_cents=1000, quantity=1),  # qty drifted
            # 4 missing
        }
        fresh, stale = filter_stale_updates(updates, current)
        self.assertEqual([r.listing_id for r in fresh], [1])
        self.assertEqual(stale, [2, 3, 4])

    def test_bulk_payload_uses_euro_float(self) -> None:
        self.assertEqual(cents_to_ct_price(5071), 50.71)
        payload = build_bulk_payload([_row(listing_id=9, proposed_price_cents=5071)])
        self.assertEqual(payload, [{"id": 9, "price": 50.71}])

    def test_chunk_and_idempotency(self) -> None:
        rows = [_row(listing_id=i) for i in range(5)]
        chunks = chunk_rows(rows, 2)
        self.assertEqual(len(chunks), 3)
        self.assertEqual([r.listing_id for r in chunks[0]], [0, 1])

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "batches.json"
            store = FileBatchIdempotencyStore(path)
            self.assertFalse(store.is_completed("run#chunk-000"))
            store.mark_completed(
                ApplyBatchResult(
                    batch_id="run#chunk-000",
                    job_uuid="abc",
                    listing_ids=[0, 1],
                    ok=2,
                ),
                pricing_run_id="run",
            )
            store2 = FileBatchIdempotencyStore(path)
            self.assertTrue(store2.is_completed("run#chunk-000"))


if __name__ == "__main__":
    unittest.main()
