"""Unit tests for DynamoDB plan checkpoint store (mocked, no AWS)."""

from __future__ import annotations

import unittest
from decimal import Decimal
from unittest.mock import MagicMock

from cardtrader_inventory.aws.dynamodb_store import DynamoPlanCheckpointStore


class DynamoPlanCheckpointTests(unittest.TestCase):
    def test_put_get_and_progress(self) -> None:
        table = MagicMock()
        resource = MagicMock()
        resource.Table.return_value = table
        store = DynamoPlanCheckpointStore("tbl", dynamodb_resource=resource)

        created = store.put_new(
            pricing_run_id="run-1",
            mode="LIVE",
            chunk_count=2,
            listings_key="runs/run-1/listings.jsonl",
            manifest_key="runs/run-1/chunks/manifest.json",
            prefix="runs/run-1",
            owner_user_id=42,
            discount_pct=3,
            listing_count=100,
            priceable_count=90,
        )
        self.assertEqual(created.next_index, 0)
        self.assertTrue(created.more)
        table.put_item.assert_called()
        item = table.put_item.call_args.kwargs["Item"]
        self.assertEqual(item["batch_id"], "plan#checkpoint")
        self.assertEqual(item["chunk_count"], 2)

        table.get_item.return_value = {
            "Item": {
                "pricing_run_id": "run-1",
                "batch_id": "plan#checkpoint",
                "status": "in_progress",
                "mode": "LIVE",
                "next_index": Decimal("1"),
                "chunk_count": Decimal("2"),
                "listings_key": "runs/run-1/listings.jsonl",
                "manifest_key": "runs/run-1/chunks/manifest.json",
                "prefix": "runs/run-1",
                "owner_user_id": Decimal("42"),
                "discount_pct": Decimal("3"),
                "listing_count": Decimal("100"),
                "priceable_count": Decimal("90"),
            }
        }
        loaded = store.get("run-1")
        assert loaded is not None
        self.assertEqual(loaded.next_index, 1)
        self.assertEqual(loaded.owner_user_id, 42)
        self.assertTrue(loaded.more)

        loaded.next_index = 2
        loaded.status = "completed"
        loaded.safety_ok = True
        loaded.plan_key = "runs/run-1/plan.jsonl"
        store.save_progress(loaded)
        saved = table.put_item.call_args.kwargs["Item"]
        self.assertEqual(saved["status"], "completed")
        self.assertEqual(saved["next_index"], 2)
        self.assertTrue(saved["safety_ok"])


if __name__ == "__main__":
    unittest.main()
