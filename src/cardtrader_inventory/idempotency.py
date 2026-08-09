"""Batch apply idempotency stores (execution metadata only)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from cardtrader_inventory.models import ApplyBatchResult


class BatchIdempotencyStore(Protocol):
    def is_completed(self, batch_id: str) -> bool: ...

    def mark_completed(
        self, batch: ApplyBatchResult, *, pricing_run_id: str
    ) -> None: ...


class FileBatchIdempotencyStore:
    """File-backed completed-batch log for local CLI apply."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._completed: dict[str, dict[str, Any]] = {}
        if path.exists():
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                batches = raw.get("batches") or {}
                if isinstance(batches, dict):
                    self._completed = {
                        str(k): v for k, v in batches.items() if isinstance(v, dict)
                    }

    def is_completed(self, batch_id: str) -> bool:
        entry = self._completed.get(batch_id)
        return bool(entry and entry.get("status") == "completed")

    def mark_completed(self, batch: ApplyBatchResult, *, pricing_run_id: str) -> None:
        self._completed[batch.batch_id] = {
            "status": "completed",
            "job_uuid": batch.job_uuid,
            "listing_ids": batch.listing_ids,
            "ok": batch.ok,
            "warning": batch.warning,
            "error": batch.error,
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "pricing_run_id": pricing_run_id,
            "batches": self._completed,
        }
        self.path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
