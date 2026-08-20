"""LIVE apply: stale checks, chunked bulk_update, batch idempotency."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from pathlib import Path
from typing import Any, Iterable

from cardtrader_inventory.config import PricingPolicy
from cardtrader_inventory.ct_client import CardTraderClient, CardTraderError
from cardtrader_inventory.idempotency import FileBatchIdempotencyStore
from cardtrader_inventory.models import (
    ApplyBatchResult,
    ApplyResult,
    Listing,
    PlanAction,
    PlanRow,
    PricingPlan,
    SkipReason,
)

logger = logging.getLogger(__name__)

# Historical name used by tests/scripts.
BatchIdempotencyStore = FileBatchIdempotencyStore


def parse_plan_row(raw: dict[str, Any]) -> PlanRow:
    action = PlanAction(raw.get("action", "skip"))
    skip_raw = raw.get("skip_reason")
    skip = SkipReason(skip_raw) if skip_raw else None
    return PlanRow(
        listing_id=int(raw["listing_id"]),
        blueprint_id=int(raw["blueprint_id"]),
        previous_price_cents=int(raw["previous_price_cents"]),
        proposed_price_cents=(
            int(raw["proposed_price_cents"])
            if raw.get("proposed_price_cents") is not None
            else None
        ),
        action=action,
        quantity=int(raw.get("quantity") or 1),
        skip_reason=skip,
        market_price_cents=raw.get("market_price_cents"),
        target_price_cents=raw.get("target_price_cents"),
        clamp_decrease=bool(raw.get("clamp_decrease")),
        clamp_increase=bool(raw.get("clamp_increase")),
        sentinel_clear=bool(raw.get("sentinel_clear")),
        initial_price=bool(raw.get("initial_price")),
        comparable_count=int(raw.get("comparable_count") or 0),
        name_en=str(raw.get("name_en") or ""),
        reason=str(raw.get("reason") or ""),
    )


def load_plan_jsonl_text(text: str) -> list[PlanRow]:
    """Parse plan rows from a JSONL string (e.g. S3 object body)."""
    rows: list[PlanRow] = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON on plan line {line_no}: {exc}") from exc
        rows.append(parse_plan_row(raw))
    return rows


def load_plan_jsonl(path: Path) -> list[PlanRow]:
    """Load plan rows from a DRY_RUN plan.jsonl artifact."""
    return load_plan_jsonl_text(path.read_text(encoding="utf-8"))


def plan_row_to_dict(row: PlanRow) -> dict[str, Any]:
    return {
        "listing_id": row.listing_id,
        "blueprint_id": row.blueprint_id,
        "name_en": row.name_en,
        "previous_price_cents": row.previous_price_cents,
        "proposed_price_cents": row.proposed_price_cents,
        "quantity": row.quantity,
        "action": row.action.value,
        "skip_reason": row.skip_reason.value if row.skip_reason else None,
        "market_price_cents": row.market_price_cents,
        "target_price_cents": row.target_price_cents,
        "clamp_decrease": row.clamp_decrease,
        "clamp_increase": row.clamp_increase,
        "sentinel_clear": row.sentinel_clear,
        "initial_price": row.initial_price,
        "comparable_count": row.comparable_count,
        "reason": row.reason,
    }


def update_rows_from_plan(rows: Iterable[PlanRow]) -> list[PlanRow]:
    """Only UPDATE rows with a concrete proposed price."""
    out: list[PlanRow] = []
    for row in rows:
        if row.action != PlanAction.UPDATE:
            continue
        if row.proposed_price_cents is None:
            continue
        out.append(row)
    return out


def filter_stale_updates(
    updates: list[PlanRow],
    current_by_id: dict[int, Listing],
) -> tuple[list[PlanRow], list[int]]:
    """Abort rows whose CT price/qty no longer matches the plan snapshot."""
    fresh: list[PlanRow] = []
    stale_ids: list[int] = []
    for row in updates:
        listing = current_by_id.get(row.listing_id)
        if listing is None:
            stale_ids.append(row.listing_id)
            continue
        if listing.price_cents != row.previous_price_cents:
            stale_ids.append(row.listing_id)
            continue
        if max(1, listing.quantity) != max(1, row.quantity):
            stale_ids.append(row.listing_id)
            continue
        fresh.append(row)
    return fresh, stale_ids


def chunk_rows(rows: list[PlanRow], batch_size: int) -> list[list[PlanRow]]:
    if batch_size < 1:
        raise ValueError("batch_size must be >= 1")
    return [rows[i : i + batch_size] for i in range(0, len(rows), batch_size)]


def cents_to_ct_price(cents: int) -> float:
    """CardTrader bulk_update expects price as a float in account currency."""
    return round(cents / 100.0, 2)


def build_bulk_payload(rows: list[PlanRow]) -> list[dict[str, Any]]:
    products: list[dict[str, Any]] = []
    for row in rows:
        assert row.proposed_price_cents is not None
        products.append(
            {
                "id": row.listing_id,
                "price": cents_to_ct_price(row.proposed_price_cents),
            }
        )
    return products


def _stats_counts(job: dict[str, Any]) -> tuple[int, int, int]:
    stats = job.get("stats") or {}
    if not isinstance(stats, dict):
        return 0, 0, 0
    return (
        int(stats.get("ok") or 0),
        int(stats.get("warning") or 0),
        int(stats.get("error") or 0),
    )


def _error_details_from_job(
    job: dict[str, Any], rows: list[PlanRow]
) -> list[dict[str, Any]]:
    results = job.get("results") or []
    if not isinstance(results, list):
        return []
    details: list[dict[str, Any]] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        if item.get("result") != "error":
            continue
        idx = item.get("job_index")
        listing_id = None
        if isinstance(idx, int) and 0 <= idx < len(rows):
            listing_id = rows[idx].listing_id
        details.append(
            {
                "listing_id": listing_id,
                "job_index": idx,
                "errors": item.get("errors"),
            }
        )
    return details


def apply_plan_updates(
    client: CardTraderClient,
    *,
    pricing_run_id: str,
    updates: list[PlanRow],
    current_listings: list[Listing],
    policy: PricingPolicy,
    store: Any | None = None,
    idempotency_path: Path | None = None,
) -> ApplyResult:
    """Apply proposed price updates with stale abort + batch idempotency."""
    if store is None:
        if idempotency_path is None:
            raise ValueError("Provide store= or idempotency_path=")
        store = FileBatchIdempotencyStore(idempotency_path)

    result = ApplyResult(
        pricing_run_id=pricing_run_id,
        mode="LIVE",
        proposed=len(updates),
    )
    if not updates:
        logger.info("No updates to apply for run_id=%s", pricing_run_id)
        return result

    current_by_id = {lst.id: lst for lst in current_listings}
    fresh, stale_ids = filter_stale_updates(updates, current_by_id)
    result.aborted_stale = len(stale_ids)
    result.stale_listing_ids = stale_ids
    if stale_ids:
        logger.warning(
            "Aborting %s stale updates (price/qty mismatch or missing listing): %s",
            len(stale_ids),
            json.dumps(stale_ids),
        )

    batches = chunk_rows(fresh, policy.bulk_update_batch_size)

    for index, batch_rows in enumerate(batches):
        batch_id = f"{pricing_run_id}#chunk-{index:03d}"
        listing_ids = [r.listing_id for r in batch_rows]

        if store.is_completed(batch_id):
            logger.info(
                "Skipping already-completed batch %s (%s items)",
                batch_id,
                len(listing_ids),
            )
            result.skipped_idempotent += len(listing_ids)
            result.batches.append(
                ApplyBatchResult(
                    batch_id=batch_id,
                    job_uuid="",
                    listing_ids=listing_ids,
                    skipped_idempotent=True,
                )
            )
            continue

        products = build_bulk_payload(batch_rows)
        try:
            job_uuid = client.bulk_update_products(products)
            job = client.wait_for_job(job_uuid)
        except CardTraderError:
            logger.exception("Apply failed for batch %s", batch_id)
            raise

        state = str(job.get("state") or "")
        if state == "unprocessable":
            raise CardTraderError(
                f"bulk_update job {job_uuid} unprocessable for batch {batch_id}",
                body=json.dumps(job)[:800],
            )

        ok, warning, error = _stats_counts(job)
        batch_result = ApplyBatchResult(
            batch_id=batch_id,
            job_uuid=job_uuid,
            listing_ids=listing_ids,
            ok=ok,
            warning=warning,
            error=error,
        )
        result.batches.append(batch_result)
        result.applied_ok += ok
        result.applied_warning += warning
        result.applied_error += error
        new_errs = _error_details_from_job(job, batch_rows)
        result.error_details.extend(new_errs)
        if new_errs:
            logger.warning(
                "Batch %s errors (%s): %s",
                batch_id,
                len(new_errs),
                json.dumps(new_errs),
            )

        store.mark_completed(batch_result, pricing_run_id=pricing_run_id)
        logger.info(
            "Batch %s done job=%s ok=%s warning=%s error=%s",
            batch_id,
            job_uuid,
            ok,
            warning,
            error,
        )

    logger.info(
        "APPLY complete run_id=%s proposed=%s applied_ok=%s warning=%s error=%s "
        "stale=%s idempotent_skip=%s",
        pricing_run_id,
        result.proposed,
        result.applied_ok,
        result.applied_warning,
        result.applied_error,
        result.aborted_stale,
        result.skipped_idempotent,
    )
    return result


def apply_result_to_dict(result: ApplyResult) -> dict[str, Any]:
    return asdict(result)


def plan_from_rows(pricing_run_id: str, rows: list[PlanRow]) -> PricingPlan:
    """Minimal PricingPlan wrapper (for callers that already have rows)."""
    from cardtrader_inventory.models import PlanSummary

    summary = PlanSummary(
        cards_processed=len(rows),
        price_updates_proposed=sum(1 for r in rows if r.action == PlanAction.UPDATE),
    )
    return PricingPlan(
        pricing_run_id=pricing_run_id,
        mode="LIVE",
        rows=rows,
        summary=summary,
    )
