"""DynamoDB stores: LIVE apply idempotency + plan-wave checkpoints."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

_TTL_HOURS = 24
_PLAN_CHECKPOINT_SK = "plan#checkpoint"

from cardtrader_inventory.models import ApplyBatchResult

logger = logging.getLogger(__name__)


def _as_int(value: Any, default: int = 0) -> int:
    if value is None:
        return default
    if isinstance(value, Decimal):
        return int(value)
    return int(value)


def _ttl_epoch(now: datetime | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    return int((now + timedelta(hours=_TTL_HOURS)).timestamp())


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
                "ttl": _ttl_epoch(now),
            }
        )
        logger.info(
            "DynamoDB marked batch completed run=%s batch=%s",
            pricing_run_id,
            batch.batch_id,
        )


@dataclass
class PlanCheckpoint:
    """Progress for Prepare → PlanAll waves (same table, sk=plan#checkpoint)."""

    pricing_run_id: str
    mode: str
    next_index: int
    chunk_count: int
    status: str  # pending | in_progress | completed
    listings_key: str
    manifest_key: str
    prefix: str
    owner_user_id: int | None
    discount_pct: int
    listing_count: int
    priceable_count: int
    safety_ok: bool | None = None
    plan_key: str | None = None

    @property
    def more(self) -> bool:
        return self.status != "completed" and self.next_index < self.chunk_count


class DynamoPlanCheckpointStore:
    """Checkpoint plan waves so PlanAll can stop near timeout and resume."""

    def __init__(self, table_name: str, *, dynamodb_resource: Any | None = None) -> None:
        if dynamodb_resource is None:
            import boto3

            dynamodb_resource = boto3.resource("dynamodb")
        self._table = dynamodb_resource.Table(table_name)

    def put_new(
        self,
        *,
        pricing_run_id: str,
        mode: str,
        chunk_count: int,
        listings_key: str,
        manifest_key: str,
        prefix: str,
        owner_user_id: int | None,
        discount_pct: int,
        listing_count: int,
        priceable_count: int,
    ) -> PlanCheckpoint:
        now = datetime.now(timezone.utc)
        item: dict[str, Any] = {
            "pricing_run_id": pricing_run_id,
            "batch_id": _PLAN_CHECKPOINT_SK,
            "status": "pending",
            "mode": mode,
            "next_index": 0,
            "chunk_count": chunk_count,
            "listings_key": listings_key,
            "manifest_key": manifest_key,
            "prefix": prefix,
            "discount_pct": discount_pct,
            "listing_count": listing_count,
            "priceable_count": priceable_count,
            "updated_at": now.isoformat(),
            "ttl": _ttl_epoch(now),
        }
        if owner_user_id is not None:
            item["owner_user_id"] = owner_user_id
        self._table.put_item(Item=item)
        logger.info(
            "Plan checkpoint created run=%s chunks=%s",
            pricing_run_id,
            chunk_count,
        )
        return self._from_item(item)

    def get(self, pricing_run_id: str) -> PlanCheckpoint | None:
        resp = self._table.get_item(
            Key={"pricing_run_id": pricing_run_id, "batch_id": _PLAN_CHECKPOINT_SK}
        )
        item = resp.get("Item")
        if not item:
            return None
        return self._from_item(item)

    def save_progress(self, checkpoint: PlanCheckpoint) -> None:
        now = datetime.now(timezone.utc)
        item: dict[str, Any] = {
            "pricing_run_id": checkpoint.pricing_run_id,
            "batch_id": _PLAN_CHECKPOINT_SK,
            "status": checkpoint.status,
            "mode": checkpoint.mode,
            "next_index": checkpoint.next_index,
            "chunk_count": checkpoint.chunk_count,
            "listings_key": checkpoint.listings_key,
            "manifest_key": checkpoint.manifest_key,
            "prefix": checkpoint.prefix,
            "discount_pct": checkpoint.discount_pct,
            "listing_count": checkpoint.listing_count,
            "priceable_count": checkpoint.priceable_count,
            "updated_at": now.isoformat(),
            "ttl": _ttl_epoch(now),
        }
        if checkpoint.owner_user_id is not None:
            item["owner_user_id"] = checkpoint.owner_user_id
        if checkpoint.safety_ok is not None:
            item["safety_ok"] = checkpoint.safety_ok
        if checkpoint.plan_key:
            item["plan_key"] = checkpoint.plan_key
        self._table.put_item(Item=item)

    @staticmethod
    def _from_item(item: dict[str, Any]) -> PlanCheckpoint:
        owner_raw = item.get("owner_user_id")
        owner_id = _as_int(owner_raw) if owner_raw is not None else None
        safety_raw = item.get("safety_ok")
        safety_ok = bool(safety_raw) if safety_raw is not None else None
        plan_key = item.get("plan_key")
        return PlanCheckpoint(
            pricing_run_id=str(item["pricing_run_id"]),
            mode=str(item.get("mode") or "DRY_RUN"),
            next_index=_as_int(item.get("next_index"), 0),
            chunk_count=_as_int(item.get("chunk_count"), 0),
            status=str(item.get("status") or "pending"),
            listings_key=str(item.get("listings_key") or ""),
            manifest_key=str(item.get("manifest_key") or ""),
            prefix=str(item.get("prefix") or ""),
            owner_user_id=owner_id,
            discount_pct=_as_int(item.get("discount_pct"), 0),
            listing_count=_as_int(item.get("listing_count"), 0),
            priceable_count=_as_int(item.get("priceable_count"), 0),
            safety_ok=safety_ok,
            plan_key=str(plan_key) if plan_key else None,
        )
