"""DynamoDB batch idempotency store for LIVE apply."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

_TTL_HOURS = 24

from cardtrader_inventory.models import ApplyBatchResult

logger = logging.getLogger(__name__)


class DynamoBatchIdempotencyStore:
    """pk=pricing_run_id, sk=batch_id — execution metadata only."""

    def __init__(self, table_name: str, *, dynamodb_resource: Any | None = None) -> None:
        if dynamodb_resource is None:
            import boto3

            dynamodb_resource = boto3.resource("dynamodb")
        self._table = dynamodb_resource.Table(table_name)

    def is_completed(self, batch_id: str) -> bool:
        # batch_id format: {pricing_run_id}#chunk-NNN
        pricing_run_id, _, _ = batch_id.partition("#")
        if not pricing_run_id:
            return False
        resp = self._table.get_item(
            Key={"pricing_run_id": pricing_run_id, "batch_id": batch_id}
        )
        item = resp.get("Item") or {}
        return item.get("status") == "completed"

    def mark_completed(self, batch: ApplyBatchResult, *, pricing_run_id: str) -> None:
        now = datetime.now(timezone.utc)
        expires_at = int((now + timedelta(hours=_TTL_HOURS)).timestamp())
        self._table.put_item(
            Item={
                "pricing_run_id": pricing_run_id,
                "batch_id": batch.batch_id,
                "status": "completed",
                "job_uuid": batch.job_uuid,
                "listing_ids": batch.listing_ids,
                "ok": batch.ok,
                "warning": batch.warning,
                "error": batch.error,
                "completed_at": now.isoformat(),
                "ttl": expires_at,
            }
        )
        logger.info(
            "DynamoDB marked batch completed run=%s batch=%s",
            pricing_run_id,
            batch.batch_id,
        )
