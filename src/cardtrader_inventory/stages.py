"""Pipeline stages: fetch, validate, generate plan, safety checks."""

from __future__ import annotations

import logging
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

from cardtrader_inventory.config import PricingPolicy
from cardtrader_inventory.ct_client import CardTraderClient
from cardtrader_inventory.models import (
    ExportValidationResult,
    Listing,
    MarketOffer,
    PlanAction,
    PlanSafetyResult,
    PlanSummary,
    PricingPlan,
    SkipReason,
)
from cardtrader_inventory.pricing import normalize_language, price_listing

logger = logging.getLogger(__name__)


class StageError(RuntimeError):
    """Fail-closed stage failure."""


def market_fetch_key(listing: Listing) -> tuple[int, str, bool]:
    """Unique marketplace GET key: blueprint + language + foil.

    Condition is not a CT query filter (still applied in pricing comps).
    """
    return (listing.blueprint_id, normalize_language(listing.language), listing.foil)


def fetch_inventory(client: CardTraderClient) -> list[Listing]:
    return client.export_products()


def _hard_identity_ok(listing: Listing) -> bool:
    """id, blueprint_id, and price must be present for any usable export row."""
    return bool(listing.id and listing.blueprint_id and listing.price_cents is not None)


def _priceable_attrs_ok(listing: Listing) -> bool:
    """Condition + language are required to build comparable offers (§4.1)."""
    return bool(listing.condition.strip() and listing.language.strip())


def validate_export(
    listings: list[Listing],
    policy: PricingPolicy,
) -> ExportValidationResult:
    """Sanity-check export; exclude non-priceable rows instead of failing the run.

    Architecture §6.1: listings *used for pricing* need condition/language.
    Incomplete singles (or sealed/non-card SKUs without those props) are logged
    and dropped from the priceable set. Hard identity corruption still fails closed.
    """
    errors: list[str] = []
    count = len(listings)
    if count <= 0:
        errors.append("Export is empty (listing count == 0)")
    if count < policy.min_expected_listings:
        errors.append(
            f"Listing count {count} < min_expected_listings {policy.min_expected_listings}"
        )
    if count > policy.max_expected_listings:
        errors.append(
            f"Listing count {count} > max_expected_listings {policy.max_expected_listings}"
        )

    hard_bad: list[str] = []
    excluded: list[int] = []
    priceable: list[Listing] = []

    for listing in listings:
        if not _hard_identity_ok(listing):
            hard_bad.append(
                f"id={listing.id} blueprint_id={listing.blueprint_id} "
                f"price_cents={listing.price_cents}"
            )
            continue
        if not _priceable_attrs_ok(listing):
            excluded.append(listing.id)
            logger.warning(
                "Excluding listing id=%s name=%r from pricing "
                "(missing condition=%r language=%r game_id=%s)",
                listing.id,
                listing.name_en,
                listing.condition,
                listing.language,
                listing.game_id,
            )
            continue
        priceable.append(listing)

    if hard_bad:
        sample = "; ".join(hard_bad[:5])
        errors.append(
            f"{len(hard_bad)} listings missing hard identity fields "
            f"(id/blueprint_id/price): {sample}"
        )

    if count > 0 and not priceable and not errors:
        errors.append(
            "No priceable listings after excluding rows missing condition/language"
        )

    ok = not errors
    result = ExportValidationResult(
        ok=ok,
        listing_count=count,
        priceable=priceable,
        excluded_missing_attrs=excluded,
        errors=errors,
    )
    if ok:
        logger.info(
            "Export validation OK: %s listings, %s priceable, %s excluded (missing attrs)",
            count,
            len(priceable),
            len(excluded),
        )
    else:
        logger.error("Export validation FAILED: %s", "; ".join(errors))
    return result


FetchKey = tuple[int, str, bool]


def owner_user_id(listings: list[Listing]) -> int | None:
    for listing in listings:
        if listing.user_id is not None:
            return listing.user_id
    return None


# Back-compat alias for older call sites / tests.
_owner_user_id = owner_user_id


def mtg_listings(listings: list[Listing], policy: PricingPolicy) -> list[Listing]:
    """Filter to game_id (fallback to all if none match)."""
    mtg = [lst for lst in listings if lst.game_id == policy.game_id]
    if not mtg and listings:
        logger.warning(
            "No listings matched game_id=%s; falling back to all listings",
            policy.game_id,
        )
        return list(listings)
    return mtg


def collect_fetch_keys(
    listings: list[Listing],
    policy: PricingPolicy,
) -> list[FetchKey]:
    """Sorted unique marketplace GET keys for priceable MTG listings."""
    return sorted({market_fetch_key(lst) for lst in mtg_listings(listings, policy)})


def slice_fetch_key_chunks(
    fetch_keys: list[FetchKey],
    chunk_size: int,
) -> list[list[FetchKey]]:
    """Split fetch keys into chunks of ``chunk_size`` (at least one empty chunk if none)."""
    size = max(1, int(chunk_size))
    if not fetch_keys:
        return [[]]
    return [fetch_keys[i : i + size] for i in range(0, len(fetch_keys), size)]


def encode_fetch_key(key: FetchKey) -> list:
    bp, lang, foil = key
    return [bp, lang, foil]


def decode_fetch_key(raw: list | tuple) -> FetchKey:
    return (int(raw[0]), str(raw[1]), bool(raw[2]))


def summarize_plan_rows(rows: list, policy: PricingPolicy) -> PlanSummary:
    """Aggregate PlanSummary counters from plan rows."""
    summary = PlanSummary()
    for row in rows:
        summary.cards_processed += 1
        if row.action == PlanAction.UPDATE:
            summary.price_updates_proposed += 1
            if row.clamp_decrease:
                summary.clamp_hit_decrease += 1
            if row.clamp_increase:
                summary.clamp_hit_increase += 1
            if row.sentinel_clear:
                summary.sentinel_initial_priced += 1
            if (
                row.comparable_count < policy.min_comparable_offers
                and policy.insufficient_comps_fallback == "use_lowest"
            ):
                summary.fallback_used_lowest += 1
        elif row.skip_reason == SkipReason.INSUFFICIENT_COMPS:
            summary.skipped_insufficient_comps += 1
        elif row.skip_reason == SkipReason.KEEP_CURRENT_INSUFFICIENT:
            summary.kept_current_insufficient_comps += 1
        elif row.skip_reason == SkipReason.NO_CHANGE:
            summary.no_change += 1
        elif row.skip_reason == SkipReason.WIDE_SPREAD:
            summary.skipped_wide_spread += 1
        elif row.skip_reason == SkipReason.DEAD_BAND:
            summary.skipped_dead_band += 1
        else:
            summary.skipped_other += 1
    return summary


def merge_plan_summaries(parts: list[PlanSummary]) -> PlanSummary:
    """Sum chunk summaries into one PlanSummary."""
    out = PlanSummary()
    for part in parts:
        out.cards_processed += part.cards_processed
        out.price_updates_proposed += part.price_updates_proposed
        out.skipped_insufficient_comps += part.skipped_insufficient_comps
        out.kept_current_insufficient_comps += part.kept_current_insufficient_comps
        out.fallback_used_lowest += part.fallback_used_lowest
        out.clamp_hit_decrease += part.clamp_hit_decrease
        out.clamp_hit_increase += part.clamp_hit_increase
        out.sentinel_initial_priced += part.sentinel_initial_priced
        out.no_change += part.no_change
        out.skipped_wide_spread += part.skipped_wide_spread
        out.skipped_dead_band += part.skipped_dead_band
        out.skipped_other += part.skipped_other
    return out


def fetch_offers_for_keys(
    client: CardTraderClient,
    fetch_keys: list[FetchKey],
    policy: PricingPolicy,
) -> dict[FetchKey, list[MarketOffer]]:
    """Parallel marketplace GETs for the given keys (sliding window)."""
    workers = max(1, policy.marketplace_max_workers)
    offers_by_key: dict[FetchKey, list[MarketOffer]] = {}
    total_fetches = len(fetch_keys)
    if total_fetches == 0:
        return offers_by_key

    logger.info(
        "Marketplace fetch %s keys (workers=%s, rps=%s)",
        total_fetches,
        workers,
        policy.marketplace_rps,
    )

    def _fetch(key: FetchKey) -> tuple[FetchKey, list[MarketOffer]]:
        bp, lang, foil = key
        return key, client.marketplace_products(
            bp,
            language=lang or None,
            foil=foil,
        )

    completed = 0
    progress_every = max(25, total_fetches // 20)
    fetch_started = time.monotonic()
    in_flight: set = set()
    next_index = 0
    pool = ThreadPoolExecutor(max_workers=workers)
    try:
        while next_index < total_fetches or in_flight:
            while next_index < total_fetches and len(in_flight) < workers:
                key = fetch_keys[next_index]
                next_index += 1
                in_flight.add(pool.submit(_fetch, key))

            if not in_flight:
                break

            done, in_flight = wait(in_flight, return_when=FIRST_COMPLETED, timeout=0.5)
            if not done:
                continue

            for fut in done:
                key, offers = fut.result()
                offers_by_key[key] = offers
                completed += 1
                if completed == total_fetches or completed % progress_every == 0:
                    elapsed = max(time.monotonic() - fetch_started, 1e-9)
                    rate = completed / elapsed
                    remaining = total_fetches - completed
                    eta_s = remaining / rate if rate > 0 else 0.0
                    logger.info(
                        "Marketplace progress %s/%s (%.1f%%) rate=%.2f fetch/s ETA=%.0fs",
                        completed,
                        total_fetches,
                        100.0 * completed / total_fetches,
                        rate,
                        eta_s,
                    )
    except KeyboardInterrupt:
        logger.warning(
            "Interrupted during marketplace fetch (%s/%s done); shutting down workers",
            completed,
            total_fetches,
        )
        raise
    finally:
        pool.shutdown(wait=False, cancel_futures=True)

    return offers_by_key


def generate_pricing_plan_for_keys(
    client: CardTraderClient,
    listings: list[Listing],
    fetch_keys: list[FetchKey],
    policy: PricingPolicy,
    *,
    pricing_run_id: str,
    mode: str = "DRY_RUN",
    exclude_user_id: int | None = None,
) -> PricingPlan:
    """Market fetch + plan rows for listings whose fetch key is in ``fetch_keys``."""
    mtg = mtg_listings(listings, policy)
    key_set = set(fetch_keys)
    subset = [lst for lst in mtg if market_fetch_key(lst) in key_set]
    owner_id = (
        exclude_user_id if exclude_user_id is not None else owner_user_id(mtg)
    )

    logger.info(
        "Generating plan chunk run_id=%s listings=%s/%s fetches=%s",
        pricing_run_id,
        len(subset),
        len(mtg),
        len(fetch_keys),
    )

    offers_by_key = fetch_offers_for_keys(client, fetch_keys, policy)
    rows = []
    for listing in subset:
        offers = offers_by_key.get(market_fetch_key(listing), [])
        rows.append(
            price_listing(
                listing,
                offers,
                policy,
                exclude_user_id=owner_id,
            )
        )

    summary = summarize_plan_rows(rows, policy)
    logger.info(
        "Plan chunk summary: processed=%s proposed=%s skip_comps=%s wide_spread=%s "
        "dead_band=%s clamps_dec=%s sentinel=%s no_change=%s",
        summary.cards_processed,
        summary.price_updates_proposed,
        summary.skipped_insufficient_comps,
        summary.skipped_wide_spread,
        summary.skipped_dead_band,
        summary.clamp_hit_decrease,
        summary.sentinel_initial_priced,
        summary.no_change,
    )
    return PricingPlan(
        pricing_run_id=pricing_run_id,
        mode=mode,
        rows=rows,
        summary=summary,
    )


def generate_pricing_plan(
    client: CardTraderClient,
    listings: list[Listing],
    policy: PricingPolicy,
    *,
    pricing_run_id: str,
    mode: str = "DRY_RUN",
) -> PricingPlan:
    """Full-catalog market fetch + plan rows (single chunk wrapper)."""
    keys = collect_fetch_keys(listings, policy)
    return generate_pricing_plan_for_keys(
        client,
        listings,
        keys,
        policy,
        pricing_run_id=pricing_run_id,
        mode=mode,
    )


def safety_check_plan(
    plan: PricingPlan,
    listing_count: int,
    policy: PricingPolicy,
) -> PlanSafetyResult:
    """Fail closed on steep drops; large update volume is allowed by default."""
    proposed = plan.summary.price_updates_proposed
    errors: list[str] = []

    if proposed > policy.max_proposed_absolute:
        errors.append(
            f"Proposed updates {proposed} > max_proposed_absolute "
            f"{policy.max_proposed_absolute}"
        )

    if listing_count > 0 and policy.max_proposed_pct < 100.0:
        pct = 100.0 * proposed / listing_count
        if pct > policy.max_proposed_pct:
            errors.append(
                f"Proposed updates {proposed} ({pct:.1f}% of catalog) > "
                f"max_proposed_pct {policy.max_proposed_pct}"
            )

    steep_drops = 0
    worst: tuple[int, float] | None = None
    for row in plan.rows:
        if row.action != PlanAction.UPDATE or row.proposed_price_cents is None:
            continue
        if row.sentinel_clear:
            continue
        prev = row.previous_price_cents
        if prev <= 0:
            continue
        drop_pct = 100.0 * (prev - row.proposed_price_cents) / prev
        if drop_pct > policy.max_allowed_decrease_pct:
            steep_drops += 1
            if worst is None or drop_pct > worst[1]:
                worst = (row.listing_id, drop_pct)

    if steep_drops:
        worst_txt = (
            f" (worst listing_id={worst[0]} drop={worst[1]:.1f}%)" if worst else ""
        )
        errors.append(
            f"{steep_drops} proposed updates drop more than "
            f"{policy.max_allowed_decrease_pct:g}%{worst_txt}"
        )

    ok = not errors
    result = PlanSafetyResult(
        ok=ok,
        proposed_count=proposed,
        listing_count=listing_count,
        errors=errors,
    )
    if ok:
        logger.info(
            "Plan safety OK: proposed=%s / listings=%s",
            proposed,
            listing_count,
        )
    else:
        logger.error("Plan safety FAILED: %s", "; ".join(errors))
    return result
