"""Lambda entrypoints for Step Functions prepare / plan-chunk / merge / apply."""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from cardtrader_inventory.apply import (
    apply_plan_updates,
    apply_result_to_dict,
    load_plan_jsonl_text,
    update_rows_from_plan,
)
from cardtrader_inventory.aws.dynamodb_store import DynamoBatchIdempotencyStore
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
from cardtrader_inventory.pipeline import build_summary_dict, plan_jsonl_bytes
from cardtrader_inventory.rate_limiter import RateLimiter
from cardtrader_inventory.stages import StageError, fetch_inventory

logger = logging.getLogger()
if not logger.handlers:
    logging.basicConfig(level=logging.INFO)
logger.setLevel(logging.INFO)


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


def prepare_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Export + validate; write listings + manifest; return Map chunk items."""
    del context
    mode = _mode_from_event(event)
    bucket = _require_env("ARTIFACTS_BUCKET")
    policy = PricingPolicy.from_env()

    try:
        client = CardTraderClient(
            load_api_token(),
            policy,
            limiter=RateLimiter(policy.marketplace_rps),
        )
        prepared = prepare_run(client, policy, mode=mode)
    except StageError as exc:
        logger.exception("Prepare stage failed")
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
            "s3_bucket": bucket,
            "s3_keys": {},
            "chunks": [],
            "summary_counts": {},
        }

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

    # Self-contained Map items (shared context duplicated per chunk).
    chunks = []
    for entry in manifest["chunks"]:
        chunks.append(
            {
                "chunk_id": entry["chunk_id"],
                "fetch_keys": entry["fetch_keys"],
                "plan_key": entry["plan_key"],
                "pricing_run_id": prepared.pricing_run_id,
                "mode": mode,
                "s3_bucket": bucket,
                "listings_key": listings_key,
                "owner_user_id": prepared.owner_user_id,
            }
        )

    payload = {
        "pricing_run_id": prepared.pricing_run_id,
        "mode": mode,
        "s3_bucket": bucket,
        "s3_keys": {
            "listings": listings_key,
            "manifest": manifest_key,
            "prefix": prefix,
        },
        "listing_count": prepared.listing_count,
        "priceable_count": prepared.priceable_count,
        "excluded_missing_attrs": prepared.excluded_missing_attrs,
        "owner_user_id": prepared.owner_user_id,
        "fetch_key_count": prepared.fetch_key_count,
        "chunk_count": len(chunks),
        "chunks": chunks,
    }
    logger.info(
        "Prepare handler result run_id=%s chunks=%s fetch_keys=%s",
        prepared.pricing_run_id,
        len(chunks),
        prepared.fetch_key_count,
    )
    return payload


def plan_chunk_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Marketplace fetch + plan rows for one fetch-key chunk."""
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
    }
    logger.info("Plan chunk handler result: %s", json.dumps(payload))
    return payload


def merge_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Merge chunk plans, safety-check, write summary + plan.jsonl, emit metrics."""
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

    manifest = get_json(bucket, manifest_key)
    chunk_plans = []
    for entry in manifest.get("chunks") or []:
        plan_key = str(entry["plan_key"])
        rows = load_plan_jsonl_text(get_text(bucket, plan_key))
        from cardtrader_inventory.models import PricingPlan
        from cardtrader_inventory.stages import summarize_plan_rows

        chunk_plans.append(
            PricingPlan(
                pricing_run_id=pricing_run_id,
                mode=mode,
                rows=rows,
                summary=summarize_plan_rows(rows, policy),
            )
        )

    result = merge_chunk_plans(
        pricing_run_id=pricing_run_id,
        mode=mode,
        listing_count=int(event.get("listing_count") or manifest.get("listing_count") or 0),
        priceable_count=int(
            event.get("priceable_count") or manifest.get("priceable_count") or 0
        ),
        excluded_missing_attrs=list(
            event.get("excluded_missing_attrs")
            or manifest.get("excluded_missing_attrs")
            or []
        ),
        chunk_plans=chunk_plans,
        policy=policy,
        sample_size=25,
    )

    summary_key = f"{prefix}/summary.json"
    plan_key = f"{prefix}/plan.jsonl"
    put_json(bucket, summary_key, build_summary_dict(result))
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

    payload = {
        "pricing_run_id": result.pricing_run_id,
        "mode": mode,
        "safety_ok": result.safety.ok,
        "safety_errors": result.safety.errors,
        "s3_bucket": bucket,
        "s3_keys": {
            **s3_keys,
            "summary": summary_key,
            "plan": plan_key,
            "prefix": prefix,
            "manifest": manifest_key,
        },
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
    logger.info("Merge handler result: %s", json.dumps(payload))
    return payload


def plan_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Deprecated monolith — prefer prepare/plan_chunk/merge. Kept for emergency invoke."""
    logger.warning("plan_handler is deprecated; use prepare → map → merge")
    # In-process full path via local dry_run for break-glass single invoke.
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
    summary_key = f"{prefix}/summary.json"
    plan_key = f"{prefix}/plan.jsonl"
    put_json(bucket, summary_key, build_summary_dict(result))
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
            "summary": summary_key,
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

    apply_key = f"{run_prefix(pricing_run_id)}/apply.json"
    put_json(bucket, apply_key, apply_result_to_dict(result))

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
        "s3_keys": {
            **s3_keys,
            "apply": apply_key,
        },
    }
    logger.info("Apply handler result: %s", json.dumps(payload))
    return payload
