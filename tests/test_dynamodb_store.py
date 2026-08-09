"""Unit tests for DynamoDB idempotency store (mocked, no AWS)."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from cardtrader_inventory.aws.dynamodb_store import DynamoBatchIdempotencyStore
from cardtrader_inventory.models import ApplyBatchResult


class DynamoIdempotencyTests(unittest.TestCase):
    def test_is_completed_and_mark(self) -> None:
        table = MagicMock()
        table.get_item.return_value = {"Item": {"status": "completed"}}
        resource = MagicMock()
        resource.Table.return_value = table
        store = DynamoBatchIdempotencyStore("tbl", dynamodb_resource=resource)

        self.assertTrue(store.is_completed("reprice-1#chunk-000"))
        table.get_item.assert_called_with(
            Key={"pricing_run_id": "reprice-1", "batch_id": "reprice-1#chunk-000"}
        )

        table.get_item.return_value = {}
        self.assertFalse(store.is_completed("reprice-1#chunk-001"))

        store.mark_completed(
            ApplyBatchResult(
                batch_id="reprice-1#chunk-001",
                job_uuid="job-1",
                listing_ids=[1, 2],
                ok=2,
            ),
            pricing_run_id="reprice-1",
        )
        table.put_item.assert_called_once()
        item = table.put_item.call_args.kwargs["Item"]
        self.assertEqual(item["pricing_run_id"], "reprice-1")
        self.assertEqual(item["batch_id"], "reprice-1#chunk-001")
        self.assertEqual(item["status"], "completed")


if __name__ == "__main__":
    unittest.main()
