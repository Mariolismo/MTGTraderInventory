"""Lambda entrypoints for Step Functions prepare / plan-all / apply."""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from cardtrader_inventory.apply import (
    apply_plan_updates,
    load_plan_jsonl_text,
    update_rows_from_plan,
)
from cardtrader_inventory.aws.dynamodb_store import (
    DynamoBatchIdempotencyStore,
    DynamoPlanCheckpointStore,
    PlanCheckpoint,
)
from cardtrader_inventory.aws.metrics import plan_observability_metrics, put_metrics
from cardtrader_inventory.aws.plan_orchestrate import (
    build_manifest,
    listings_bytes,
    load_listings_jsonl_text,
    merge_chunk_plans,
    plan_rows_jsonl_bytes,
    prepare_run,
    run_plan_chunk,
)
from cardtrader_inventory.aws.s3_artifacts import (
    get_json,
    get_text,
    put_bytes,
    put_json,
    run_prefix,
)
from cardtrader_inventory.config import PricingPolicy, load_api_token
from cardtrader_inventory.ct_client import CardTraderClient
from cardtrader_inventory.models import PricingPlan
from cardtrader_inventory.pipeline import plan_jsonl_bytes
from cardtrader_inventory.rate_limiter import RateLimiter
from cardtrader_inventory.stages import StageError, fetch_inventory, summarize_plan_rows
from cardtrader_inventory.weekly_sales import fetch_weekly_sales

logger = logging.getLogger()
if not logger.handlers:
    logging.basicConfig(level=logging.INFO)
logger.setLevel(logging.INFO)

# Do not start another plan chunk unless at least this much Lambda time remains.
# Peak PlanChunk in prod was ~5.75 min; 8 min leaves headroom before 15 min timeout.
_DEFAULT_WAVE_MIN_REMAINING_MS = 480_000


def _mode_from_event(event: dict[str, Any]) -> str:
    mode = str(event.get("mode") or "DRY_RUN").strip().upper()
    if mode not in ("DRY_RUN", "LIVE"):
        raise ValueError(f"mode must be DRY_RUN or LIVE, got {mode!r}")
    return mode


def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable {name}")
    return value


def _wave_min_remaining_ms() -> int:
    raw = os.environ.get("PLAN_WAVE_MIN_REMAINING_MS", "").strip()
    if not raw:
        return _DEFAULT_WAVE_MIN_REMAINING_MS
    return max(60_000, int(raw))


def _remaining_ms(context: Any) -> int | None:
    getter = getattr(context, "get_remaining_time_in_millis", None)
    if not callable(getter):
        return None
    return int(getter())


def _run_context_payload(
    *,
    checkpoint: PlanCheckpoint,
    bucket: str,
    more: bool,
    safety_ok: bool | None = None,
    safety_errors: list[str] | None = None,
    summary_counts: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "pricing_run_id": checkpoint.pricing_run_id,
        "mode": checkpoint.mode,
        "more": more,
        "s3_bucket": bucket,
        "s3_keys": {
            "listings": checkpoint.listings_key,
            "manifest": checkpoint.manifest_key,
            "prefix": checkpoint.prefix,
        },
        "listing_count": checkpoint.listing_count,
        "priceable_count": checkpoint.priceable_count,
        "owner_user_id": checkpoint.owner_user_id,
        "discount_pct": checkpoint.discount_pct,
        "chunk_count": checkpoint.chunk_count,
        "next_chunk_index": checkpoint.next_index,
    }
    if checkpoint.plan_key:
        payload["s3_keys"]["plan"] = checkpoint.plan_key
    if safety_ok is not None:
        payload["safety_ok"] = safety_ok
    elif checkpoint.safety_ok is not None:
        payload["safety_ok"] = checkpoint.safety_ok
    if safety_errors is not None:
        payload["safety_errors"] = safety_errors
    if summary_counts is not None:
        payload["summary_counts"] = summary_counts
    return payload


def _finalize_merged_plan(
    *,
    bucket: str,
    checkpoint: PlanCheckpoint,
    policy: PricingPolicy,
) -> dict[str, Any]:
    """Load chunk plans from S3, merge, write plan.jsonl, emit metrics."""
    manifest = get_json(bucket, checkpoint.manifest_key)
    chunk_plans: list[PricingPlan] = []
    for entry in manifest.get("chunks") or []:
        plan_key = str(entry["plan_key"])
        rows = load_plan_jsonl_text(get_text(bucket, plan_key))
        chunk_plans.append(
            PricingPlan(
                pricing_run_id=checkpoint.pricing_run_id,
                mode=checkpoint.mode,
                rows=rows,
                summary=summarize_plan_rows(rows, policy),
            )
        )

    result = merge_chunk_plans(
        pricing_run_id=checkpoint.pricing_run_id,
        mode=checkpoint.mode,
        listing_count=int(
            checkpoint.listing_count or manifest.get("listing_count") or 0
        ),
        priceable_count=int(
            checkpoint.priceable_count or manifest.get("priceable_count") or 0
        ),
        excluded_missing_attrs=list(manifest.get("excluded_missing_attrs") or []),
        chunk_plans=chunk_plans,
        policy=policy,
        sample_size=25,
    )

    plan_key = f"{checkpoint.prefix}/plan.jsonl"
    put_bytes(
        bucket,
        plan_key,
        plan_jsonl_bytes(result),
        content_type="application/x-ndjson",
    )

    summary = result.plan.summary
    cards_in_inventory = sum(max(1, row.quantity) for row in result.plan.rows)
    inventory_eur = round(result.kpis.catalog_value_after_cents / 100.0, 2)
    put_metrics(
        plan_observability_metrics(
            result.plan,
            cards_in_inventory=cards_in_inventory,
            inventory_eur=inventory_eur,
            safety_ok=result.safety.ok,
        )
    )

    checkpoint.status = "completed"
    checkpoint.next_index = checkpoint.chunk_count
    checkpoint.safety_ok = result.safety.ok
    checkpoint.plan_key = plan_key

    return {
        "safety_ok": result.safety.ok,
        "safety_errors": result.safety.errors,
        "summary_counts": {
            "listing_count": result.listing_count,
            "priceable_count": result.priceable_count,
            "cards_processed": summary.cards_processed,
            "cards_in_inventory": cards_in_inventory,
            "price_updates_proposed": summary.price_updates_proposed,
            "skipped_wide_spread": summary.skipped_wide_spread,
            "skipped_insufficient_comps": summary.skipped_insufficient_comps,
            "skipped_dead_band": summary.skipped_dead_band,
            "sentinel_cleared": summary.sentinel_initial_priced,
            "no_change": summary.no_change,
            "inventory_value_eur": inventory_eur,
            "chunk_count": len(chunk_plans),
        },
    }


def prepare_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Export + validate; write listings/manifest; seed DynamoDB plan checkpoint."""
    del context
    mode = _mode_from_event(event)
    bucket = _require_env("ARTIFACTS_BUCKET")
    table_name = _require_env("IDEMPOTENCY_TABLE")
    policy = PricingPolicy.from_env()

    try:
        client = CardTraderClient(
            load_api_token(),
            policy,
            limiter=RateLimiter(policy.marketplace_rps),
        )
        prepared = prepare_run(client, policy, mode=mode)
    except StageError:
        logger.exception("Prepare stage failed")
        put_metrics(
            {
                "CardsInInventory": (0, "Count"),
                "RepriceError": (1, "Count"),
            }
        )
        raise

    # Weekly discount: fetch hub_pending orders, compute sales total, evaluate tier.
    # Failures here degrade gracefully — discount defaults to 0, run continues.
    discount_pct = 0
    try:
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        result = fetch_weekly_sales(client, now)
        discount_pct = result.discount_pct
        put_metrics(
            {
                "WeeklyDiscountPct": (discount_pct, "None"),
                "WeeklySalesEur": (round(result.sales_cents / 100.0, 2), "None"),
            }
        )
    except Exception:
        logger.exception("Weekly sales check failed — proceeding with discount_pct=0")

    prefix = run_prefix(prepared.pricing_run_id)
    listings_key = f"{prefix}/listings.jsonl"
    manifest_key = f"{prefix}/chunks/manifest.json"

    put_bytes(
        bucket,
        listings_key,
        listings_bytes(prepared.listings),
        content_type="application/x-ndjson",
    )
    manifest = build_manifest(
        prepared,
        listings_key=listings_key,
        prefix=prefix,
    )
    manifest["generate_chunk_size"] = policy.generate_chunk_size
    put_json(bucket, manifest_key, manifest)

    checkpoint = DynamoPlanCheckpointStore(table_name).put_new(
        pricing_run_id=prepared.pricing_run_id,
        mode=mode,
        chunk_count=len(manifest["chunks"]),
        listings_key=listings_key,
        manifest_key=manifest_key,
        prefix=prefix,
        owner_user_id=prepared.owner_user_id,
        discount_pct=discount_pct,
        listing_count=prepared.listing_count,
        priceable_count=prepared.priceable_count,
    )

    payload = _run_context_payload(
        checkpoint=checkpoint,
        bucket=bucket,
        more=checkpoint.more,
    )
    logger.info(
        "Prepare handler result run_id=%s chunks=%s fetch_keys=%s more=%s",
        prepared.pricing_run_id,
        checkpoint.chunk_count,
        prepared.fetch_key_count,
        payload["more"],
    )
    return payload


def plan_all_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Plan one or more chunks per invoke; checkpoint in DynamoDB; merge when done.

    Stops starting new chunks when remaining Lambda time is below
    PLAN_WAVE_MIN_REMAINING_MS so SFN can loop without hitting the 15 min ceiling.
    """
    mode = _mode_from_event(event)
    bucket = str(event.get("s3_bucket") or _require_env("ARTIFACTS_BUCKET"))
    table_name = _require_env("IDEMPOTENCY_TABLE")
    pricing_run_id = str(event.get("pricing_run_id") or "").strip()
    if not pricing_run_id:
        raise ValueError("pricing_run_id is required")

    store = DynamoPlanCheckpointStore(table_name)
    checkpoint = store.get(pricing_run_id)
    if checkpoint is None:
        raise RuntimeError(f"Missing plan checkpoint for run {pricing_run_id}")

    if checkpoint.status == "completed":
        logger.info("Plan checkpoint already completed run_id=%s", pricing_run_id)
        return _run_context_payload(
            checkpoint=checkpoint,
            bucket=bucket,
            more=False,
            safety_ok=checkpoint.safety_ok,
        )

    policy = PricingPolicy.from_env()
    manifest = get_json(bucket, checkpoint.manifest_key)
    chunks = list(manifest.get("chunks") or [])
    if len(chunks) != checkpoint.chunk_count:
        # Manifest is source of truth if counts drift.
        checkpoint.chunk_count = len(chunks)

    listings = load_listings_jsonl_text(get_text(bucket, checkpoint.listings_key))
    owner_id = checkpoint.owner_user_id
    discount_pct = checkpoint.discount_pct
    min_remaining = _wave_min_remaining_ms()

    client = CardTraderClient(
        load_api_token(),
        policy,
        limiter=RateLimiter(policy.marketplace_rps),
    )

    checkpoint.status = "in_progress"
    chunks_this_wave = 0

    while checkpoint.next_index < checkpoint.chunk_count:
        remaining = _remaining_ms(context)
        if (
            chunks_this_wave > 0
            and remaining is not None
            and remaining < min_remaining
        ):
            logger.info(
                "Plan wave pausing run_id=%s next_index=%s remaining_ms=%s "
                "min_remaining_ms=%s",
                pricing_run_id,
                checkpoint.next_index,
                remaining,
                min_remaining,
            )
            break

        entry = chunks[checkpoint.next_index]
        chunk_id = str(entry["chunk_id"])
        plan_key = str(entry["plan_key"])
        logger.info(
            "Plan wave chunk run_id=%s chunk_id=%s index=%s/%s remaining_ms=%s",
            pricing_run_id,
            chunk_id,
            checkpoint.next_index,
            checkpoint.chunk_count,
            remaining,
        )
        chunk_result = run_plan_chunk(
            client,
            listings,
            policy,
            pricing_run_id=pricing_run_id,
            mode=mode,
            chunk_id=chunk_id,
            fetch_keys_raw=list(entry.get("fetch_keys") or []),
            exclude_user_id=owner_id,
            discount_pct=discount_pct,
        )
        put_bytes(
            bucket,
            plan_key,
            plan_rows_jsonl_bytes(chunk_result.plan.rows),
            content_type="application/x-ndjson",
        )
        checkpoint.next_index += 1
        chunks_this_wave += 1
        store.save_progress(checkpoint)

    if checkpoint.next_index < checkpoint.chunk_count:
        store.save_progress(checkpoint)
        payload = _run_context_payload(
            checkpoint=checkpoint,
            bucket=bucket,
            more=True,
        )
        logger.info(
            "Plan wave incomplete run_id=%s next_index=%s/%s chunks_this_wave=%s",
            pricing_run_id,
            checkpoint.next_index,
            checkpoint.chunk_count,
            chunks_this_wave,
        )
        return payload

    finalized = _finalize_merged_plan(
        bucket=bucket,
        checkpoint=checkpoint,
        policy=policy,
    )
    store.save_progress(checkpoint)
    payload = _run_context_payload(
        checkpoint=checkpoint,
        bucket=bucket,
        more=False,
        safety_ok=finalized["safety_ok"],
        safety_errors=finalized["safety_errors"],
        summary_counts=finalized["summary_counts"],
    )
    logger.info("Plan all complete: %s", json.dumps(payload))
    return payload


def plan_chunk_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Deprecated single-chunk entry — prefer plan_all_handler. Kept for break-glass."""
    logger.warning("plan_chunk_handler is deprecated; use plan_all_handler")
    del context
    mode = _mode_from_event(event)
    bucket = str(event.get("s3_bucket") or _require_env("ARTIFACTS_BUCKET"))
    pricing_run_id = str(event.get("pricing_run_id") or "").strip()
    chunk_id = str(event.get("chunk_id") or "").strip()
    listings_key = str(event.get("listings_key") or "")
    plan_key = str(
        event.get("plan_key")
        or f"{run_prefix(pricing_run_id)}/chunks/{chunk_id}.plan.jsonl"
    )
    if not pricing_run_id or not chunk_id or not listings_key:
        raise ValueError("pricing_run_id, chunk_id, and listings_key are required")

    policy = PricingPolicy.from_env()
    listings = load_listings_jsonl_text(get_text(bucket, listings_key))
    owner_raw = event.get("owner_user_id")
    owner_id = int(owner_raw) if owner_raw is not None else None

    discount_pct = int(event.get("discount_pct") or 0)

    client = CardTraderClient(
        load_api_token(),
        policy,
        limiter=RateLimiter(policy.marketplace_rps),
    )
    chunk_result = run_plan_chunk(
        client,
        listings,
        policy,
        pricing_run_id=pricing_run_id,
        mode=mode,
        chunk_id=chunk_id,
        fetch_keys_raw=list(event.get("fetch_keys") or []),
        exclude_user_id=owner_id,
        discount_pct=discount_pct,
    )

    put_bytes(
        bucket,
        plan_key,
        plan_rows_jsonl_bytes(chunk_result.plan.rows),
        content_type="application/x-ndjson",
    )

    payload = {
        "chunk_id": chunk_id,
        "pricing_run_id": pricing_run_id,
        "mode": mode,
        "plan_key": plan_key,
        "rows": len(chunk_result.plan.rows),
        "proposed": chunk_result.plan.summary.price_updates_proposed,
        "discount_pct": discount_pct,
    }
    logger.info("Plan chunk handler result: %s", json.dumps(payload))
    return payload


def merge_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Deprecated — merge now runs inside plan_all_handler when the wave finishes."""
    logger.warning("merge_handler is deprecated; merge runs inside plan_all_handler")
    del context
    mode = _mode_from_event(event)
    bucket = str(event.get("s3_bucket") or _require_env("ARTIFACTS_BUCKET"))
    pricing_run_id = str(event.get("pricing_run_id") or "").strip()
    if not pricing_run_id:
        raise ValueError("pricing_run_id is required")

    s3_keys = dict(event.get("s3_keys") or {})
    prefix = str(s3_keys.get("prefix") or run_prefix(pricing_run_id))
    manifest_key = str(s3_keys.get("manifest") or f"{prefix}/chunks/manifest.json")
    policy = PricingPolicy.from_env()

    checkpoint = PlanCheckpoint(
        pricing_run_id=pricing_run_id,
        mode=mode,
        next_index=0,
        chunk_count=0,
        status="in_progress",
        listings_key=str(s3_keys.get("listings") or ""),
        manifest_key=manifest_key,
        prefix=prefix,
        owner_user_id=None,
        discount_pct=int(event.get("discount_pct") or 0),
        listing_count=int(event.get("listing_count") or 0),
        priceable_count=int(event.get("priceable_count") or 0),
    )
    finalized = _finalize_merged_plan(
        bucket=bucket,
        checkpoint=checkpoint,
        policy=policy,
    )
    payload = {
        "pricing_run_id": pricing_run_id,
        "mode": mode,
        "more": False,
        "safety_ok": finalized["safety_ok"],
        "safety_errors": finalized["safety_errors"],
        "s3_bucket": bucket,
        "s3_keys": {
            **s3_keys,
            "plan": checkpoint.plan_key,
            "prefix": prefix,
            "manifest": manifest_key,
        },
        "summary_counts": finalized["summary_counts"],
    }
    logger.info("Merge handler result: %s", json.dumps(payload))
    return payload


def plan_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Deprecated monolith — prefer prepare/plan_all. Kept for emergency invoke."""
    logger.warning("plan_handler is deprecated; use prepare → plan_all")
    from cardtrader_inventory.pipeline import run_dry_run

    del context
    mode = _mode_from_event(event)
    bucket = _require_env("ARTIFACTS_BUCKET")
    policy = PricingPolicy.from_env()
    try:
        result = run_dry_run(policy=policy, mode=mode, sample_size=25)
    except StageError as exc:
        logger.exception("Plan stage failed")
        put_metrics(
            {
                "CardsInInventory": (0, "Count"),
                "RepriceError": (1, "Count"),
            }
        )
        return {
            "pricing_run_id": event.get("pricing_run_id") or "",
            "mode": mode,
            "safety_ok": False,
            "error": str(exc),
            "s3_keys": {},
            "summary_counts": {},
        }

    prefix = run_prefix(result.pricing_run_id)
    plan_key = f"{prefix}/plan.jsonl"
    put_bytes(
        bucket,
        plan_key,
        plan_jsonl_bytes(result),
        content_type="application/x-ndjson",
    )
    summary = result.plan.summary
    cards_in_inventory = sum(max(1, row.quantity) for row in result.plan.rows)
    inventory_eur = round(result.kpis.catalog_value_after_cents / 100.0, 2)
    put_metrics(
        plan_observability_metrics(
            result.plan,
            cards_in_inventory=cards_in_inventory,
            inventory_eur=inventory_eur,
            safety_ok=result.safety.ok,
        )
    )
    return {
        "pricing_run_id": result.pricing_run_id,
        "mode": mode,
        "safety_ok": result.safety.ok,
        "safety_errors": result.safety.errors,
        "s3_bucket": bucket,
        "s3_keys": {
            "plan": plan_key,
            "prefix": prefix,
        },
        "summary_counts": {
            "listing_count": result.listing_count,
            "priceable_count": result.priceable_count,
            "cards_processed": summary.cards_processed,
            "cards_in_inventory": cards_in_inventory,
            "price_updates_proposed": summary.price_updates_proposed,
            "skipped_wide_spread": summary.skipped_wide_spread,
            "skipped_insufficient_comps": summary.skipped_insufficient_comps,
            "sentinel_cleared": summary.sentinel_initial_priced,
            "inventory_value_eur": inventory_eur,
        },
    }


def apply_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Load plan from S3, re-export, stale-filter, bulk_update with DynamoDB idempotency."""
    del context
    mode = _mode_from_event(event)
    if mode != "LIVE":
        raise ValueError("apply_handler requires mode=LIVE")

    pricing_run_id = str(event.get("pricing_run_id") or "").strip()
    if not pricing_run_id:
        raise ValueError("pricing_run_id is required")

    bucket = str(event.get("s3_bucket") or _require_env("ARTIFACTS_BUCKET"))
    s3_keys = event.get("s3_keys") or {}
    plan_key = str(s3_keys.get("plan") or f"{run_prefix(pricing_run_id)}/plan.jsonl")
    table_name = _require_env("IDEMPOTENCY_TABLE")

    if event.get("safety_ok") is False:
        raise RuntimeError("Refusing apply: safety_ok is false")

    policy = PricingPolicy.from_env()
    token = load_api_token()
    plan_text = get_text(bucket, plan_key)
    rows = load_plan_jsonl_text(plan_text)
    updates = update_rows_from_plan(rows)

    client = CardTraderClient(
        token,
        policy,
        limiter=RateLimiter(policy.marketplace_rps),
    )
    listings = fetch_inventory(client)
    store = DynamoBatchIdempotencyStore(table_name)
    result = apply_plan_updates(
        client,
        pricing_run_id=pricing_run_id,
        updates=updates,
        current_listings=listings,
        policy=policy,
        store=store,
    )

    put_metrics(
        {
            "PriceUpdatesApplied": (result.applied_ok, "Count"),
            "RepriceError": (1 if result.applied_error else 0, "Count"),
        }
    )

    payload = {
        "pricing_run_id": pricing_run_id,
        "mode": mode,
        "proposed": result.proposed,
        "applied_ok": result.applied_ok,
        "applied_warning": result.applied_warning,
        "applied_error": result.applied_error,
        "aborted_stale": result.aborted_stale,
        "skipped_idempotent": result.skipped_idempotent,
        "s3_keys": dict(s3_keys),
    }
    if result.stale_listing_ids:
        payload["stale_listing_ids"] = result.stale_listing_ids
    if result.error_details:
        payload["error_details"] = result.error_details
    logger.info("Apply handler result: %s", json.dumps(payload))
    return payload
