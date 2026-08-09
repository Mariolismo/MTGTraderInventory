"""Prepare → plan-chunk → merge helpers for AWS (and in-process local orchestration)."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from cardtrader_inventory.apply import plan_row_to_dict
from cardtrader_inventory.config import PricingPolicy
from cardtrader_inventory.ct_client import CardTraderClient
from cardtrader_inventory.listing_serde import (
    listings_from_jsonl_text,
    listings_to_jsonl_bytes,
)
from cardtrader_inventory.models import (
    Listing,
    PlanAction,
    PlanRow,
    PlanSummary,
    PricingPlan,
)
from cardtrader_inventory.pipeline import DryRunResult, new_pricing_run_id
from cardtrader_inventory.report import compute_plan_kpis
from cardtrader_inventory.stages import (
    StageError,
    collect_fetch_keys,
    decode_fetch_key,
    encode_fetch_key,
    fetch_inventory,
    generate_pricing_plan_for_keys,
    merge_plan_summaries,
    mtg_listings,
    owner_user_id,
    safety_check_plan,
    slice_fetch_key_chunks,
    validate_export,
)

logger = logging.getLogger(__name__)


@dataclass
class PrepareResult:
    pricing_run_id: str
    mode: str
    listing_count: int
    priceable_count: int
    excluded_missing_attrs: list[int]
    listings: list[Listing]
    owner_user_id: int | None
    chunks: list[dict[str, Any]]
    fetch_key_count: int


@dataclass
class ChunkPlanResult:
    chunk_id: str
    pricing_run_id: str
    mode: str
    plan: PricingPlan
    plan_key: str = ""


def prepare_run(
    client: CardTraderClient,
    policy: PricingPolicy,
    *,
    mode: str,
    pricing_run_id: str | None = None,
) -> PrepareResult:
    """FETCH → VALIDATE → build fetch-key chunks (no marketplace GETs yet)."""
    if mode not in ("DRY_RUN", "LIVE"):
        raise ValueError(f"mode must be DRY_RUN or LIVE, got {mode!r}")

    pricing_run_id = pricing_run_id or new_pricing_run_id()
    listings = fetch_inventory(client)
    validation = validate_export(listings, policy)
    if not validation.ok:
        raise StageError("ExportValidationFailed: " + "; ".join(validation.errors))

    priceable = validation.priceable
    keys = collect_fetch_keys(priceable, policy)
    key_chunks = slice_fetch_key_chunks(keys, policy.generate_chunk_size)
    owner_id = owner_user_id(mtg_listings(priceable, policy))

    chunks: list[dict[str, Any]] = []
    for index, key_chunk in enumerate(key_chunks):
        chunk_id = f"{index:03d}"
        chunks.append(
            {
                "chunk_id": chunk_id,
                "fetch_keys": [encode_fetch_key(k) for k in key_chunk],
                "fetch_key_count": len(key_chunk),
            }
        )

    logger.info(
        "Prepare run_id=%s listings=%s priceable=%s fetch_keys=%s chunks=%s "
        "chunk_size=%s",
        pricing_run_id,
        validation.listing_count,
        len(priceable),
        len(keys),
        len(chunks),
        policy.generate_chunk_size,
    )
    return PrepareResult(
        pricing_run_id=pricing_run_id,
        mode=mode,
        listing_count=validation.listing_count,
        priceable_count=len(priceable),
        excluded_missing_attrs=list(validation.excluded_missing_attrs),
        listings=priceable,
        owner_user_id=owner_id,
        chunks=chunks,
        fetch_key_count=len(keys),
    )


def build_manifest(
    prepare: PrepareResult,
    *,
    listings_key: str,
    prefix: str,
) -> dict[str, Any]:
    chunk_entries = []
    for chunk in prepare.chunks:
        chunk_id = chunk["chunk_id"]
        chunk_entries.append(
            {
                **chunk,
                "plan_key": f"{prefix}/chunks/{chunk_id}.plan.jsonl",
            }
        )
    return {
        "pricing_run_id": prepare.pricing_run_id,
        "mode": prepare.mode,
        "listing_count": prepare.listing_count,
        "priceable_count": prepare.priceable_count,
        "excluded_missing_attrs": prepare.excluded_missing_attrs,
        "owner_user_id": prepare.owner_user_id,
        "fetch_key_count": prepare.fetch_key_count,
        "generate_chunk_size": None,
        "listings_key": listings_key,
        "prefix": prefix,
        "chunks": chunk_entries,
    }


def run_plan_chunk(
    client: CardTraderClient,
    listings: list[Listing],
    policy: PricingPolicy,
    *,
    pricing_run_id: str,
    mode: str,
    chunk_id: str,
    fetch_keys_raw: list,
    exclude_user_id: int | None = None,
) -> ChunkPlanResult:
    """Price listings for one fetch-key chunk."""
    keys = [decode_fetch_key(k) for k in fetch_keys_raw]
    plan = generate_pricing_plan_for_keys(
        client,
        listings,
        keys,
        policy,
        pricing_run_id=pricing_run_id,
        mode=mode,
        exclude_user_id=exclude_user_id,
    )
    return ChunkPlanResult(
        chunk_id=chunk_id,
        pricing_run_id=pricing_run_id,
        mode=mode,
        plan=plan,
    )


def plan_rows_jsonl_bytes(rows: list[PlanRow]) -> bytes:
    lines = [json.dumps(plan_row_to_dict(row)) for row in rows]
    return ("\n".join(lines) + ("\n" if lines else "")).encode("utf-8")


def merge_chunk_plans(
    *,
    pricing_run_id: str,
    mode: str,
    listing_count: int,
    priceable_count: int,
    excluded_missing_attrs: list[int],
    chunk_plans: list[PricingPlan],
    policy: PricingPolicy,
    sample_size: int = 25,
) -> DryRunResult:
    """Concatenate chunk plans, safety-check, KPIs."""
    rows: list[PlanRow] = []
    for plan in chunk_plans:
        rows.extend(plan.rows)
    summary = merge_plan_summaries([p.summary for p in chunk_plans])
    plan = PricingPlan(
        pricing_run_id=pricing_run_id,
        mode=mode,
        rows=rows,
        summary=summary,
    )
    safety = safety_check_plan(plan, listing_count=priceable_count, policy=policy)
    if not safety.ok:
        logger.error(
            "Plan safety gate failed (plan retained for inspection): %s",
            "; ".join(safety.errors),
        )

    sample_updates = []
    for row in plan.rows:
        if row.action != PlanAction.UPDATE:
            continue
        sample_updates.append(
            {
                "listing_id": row.listing_id,
                "blueprint_id": row.blueprint_id,
                "name_en": row.name_en,
                "quantity": row.quantity,
                "previous_cents": row.previous_price_cents,
                "proposed_cents": row.proposed_price_cents,
                "market_cents": row.market_price_cents,
                "clamp_decrease": row.clamp_decrease,
                "clamp_increase": row.clamp_increase,
                "sentinel_clear": row.sentinel_clear,
                "reason": row.reason,
            }
        )
        if len(sample_updates) >= sample_size:
            break

    kpis = compute_plan_kpis(plan)
    logger.info(
        "Merge complete run_id=%s rows=%s proposed=%s safety_ok=%s chunks=%s",
        pricing_run_id,
        len(rows),
        summary.price_updates_proposed,
        safety.ok,
        len(chunk_plans),
    )
    return DryRunResult(
        pricing_run_id=pricing_run_id,
        mode=mode,
        listing_count=listing_count,
        priceable_count=priceable_count,
        plan=plan,
        sample_updates=sample_updates,
        safety=safety,
        kpis=kpis,
        excluded_missing_attrs=excluded_missing_attrs,
    )


def load_listings_jsonl_text(text: str) -> list[Listing]:
    return listings_from_jsonl_text(text)


def listings_bytes(listings: list[Listing]) -> bytes:
    return listings_to_jsonl_bytes(listings)
