"""Pricing algorithm: comps → market → clamps → plan row."""

from __future__ import annotations

import math
from dataclasses import dataclass

from cardtrader_inventory.config import PricingPolicy
from cardtrader_inventory.models import (
    Listing,
    MarketOffer,
    PlanAction,
    PlanRow,
    SkipReason,
)


def normalize_language(value: str) -> str:
    return value.strip().lower()


# Sellers often list SP as NM; treat them as one comparable bucket.
_NM_SP_ALIASES = frozenset(
    {
        "near mint",
        "slightly played",
        "nm",
        "sp",
    }
)


def condition_bucket(value: str) -> str:
    """Map listing/offer condition to a comparable bucket.

    Near Mint and Slightly Played share a bucket; other conditions stay exact.
    """
    normalized = " ".join(value.strip().lower().split())
    if normalized in _NM_SP_ALIASES:
        return "nm_sp"
    return normalized


def filter_comparable_offers(
    offers: list[MarketOffer],
    listing: Listing,
    *,
    exclude_user_id: int | None,
    ct_zero_only: bool = True,
) -> list[MarketOffer]:
    """Language + condition-bucket + foil; optionally CT Zero only; exclude self."""
    lang = normalize_language(listing.language)
    bucket = condition_bucket(listing.condition)
    matched: list[MarketOffer] = []
    for offer in offers:
        if exclude_user_id is not None and offer.seller_user_id == exclude_user_id:
            continue
        if offer.product_id == listing.id:
            continue
        if ct_zero_only and not getattr(offer, "ct_zero", False):
            continue
        if normalize_language(offer.language) != lang:
            continue
        if condition_bucket(offer.condition) != bucket:
            continue
        if offer.foil != listing.foil:
            continue
        matched.append(offer)
    matched.sort(key=lambda o: o.price_cents)
    return matched


def nth_lowest_price(prices: list[int], n: int) -> int | None:
    if not prices or n < 1:
        return None
    if len(prices) < n:
        return None
    return prices[n - 1]


def median_cents(prices: list[int]) -> int:
    """Median of integer cents; for even length, average of middle two (rounded)."""
    ordered = sorted(prices)
    n = len(ordered)
    if n == 0:
        raise ValueError("median of empty list")
    mid = n // 2
    if n % 2:
        return ordered[mid]
    return int(round((ordered[mid - 1] + ordered[mid]) / 2.0))


def comp_spread_ratio(prices: list[int]) -> float | None:
    """(max - min) / min for a non-empty price window."""
    if not prices:
        return None
    lo = min(prices)
    if lo <= 0:
        return None
    return (max(prices) - lo) / float(lo)


@dataclass(frozen=True)
class MarketSelection:
    market_cents: int | None
    method: str  # e.g. median5, third, insufficient, wide_spread
    window_size: int
    spread_ratio: float | None
    detail: str


def compute_market_price(
    prices: list[int],
    policy: PricingPolicy,
) -> MarketSelection:
    """Select market price from sorted ascending comparable prices.

    - < min comps → insufficient
    - window = cheapest market_median_window (or all if fewer)
    - if spread too wide → skip
    - if window >= median window → median(window)
    - else (3–4 comps) → 3rd-lowest
    """
    min_n = policy.min_comparable_offers
    window_n = max(min_n, policy.market_median_window)

    if len(prices) < min_n:
        return MarketSelection(
            market_cents=None,
            method="insufficient",
            window_size=len(prices),
            spread_ratio=None,
            detail=f"comps={len(prices)}<min={min_n}",
        )

    window = prices[: min(window_n, len(prices))]
    spread = comp_spread_ratio(window)
    max_spread = policy.max_comp_spread_pct / 100.0
    if spread is not None and spread > max_spread:
        return MarketSelection(
            market_cents=None,
            method="wide_spread",
            window_size=len(window),
            spread_ratio=spread,
            detail=f"spread={spread:.2f}>max={max_spread:.2f}",
        )

    if len(window) >= policy.market_median_window:
        market = median_cents(window)
        return MarketSelection(
            market_cents=market,
            method=f"median{policy.market_median_window}",
            window_size=len(window),
            spread_ratio=spread,
            detail=f"median{policy.market_median_window}={market}",
        )

    # 3–4 comps: use 3rd-lowest
    third = nth_lowest_price(window, 3)
    return MarketSelection(
        market_cents=third,
        method="third",
        window_size=len(window),
        spread_ratio=spread,
        detail=f"third={third}",
    )


def dead_band_threshold_cents(previous_cents: int, policy: PricingPolicy) -> int:
    """Minimum |Δ| required to propose an update."""
    pct_part = int(math.ceil(previous_cents * (policy.min_change_pct / 100.0)))
    return max(policy.min_change_cents, pct_part)


def apply_clamps(
    previous_cents: int,
    target_cents: int,
    policy: PricingPolicy,
    *,
    is_sentinel: bool,
    floor_cents: int | None = None,
) -> tuple[int, bool, bool]:
    """Return (proposed, clamp_decrease, clamp_increase).

    Only decreases are %-clamped. Upside follows market/target fully (still
    respects absolute floor). Sentinel skips %-clamps entirely.
    """
    floor = policy.minimum_floor_cents if floor_cents is None else floor_cents
    if is_sentinel:
        proposed = max(target_cents, floor)
        return proposed, False, False

    proposed = target_cents
    clamp_dec = False

    if previous_cents > 0 and proposed < previous_cents:
        min_allowed = math.ceil(
            previous_cents * (1.0 - policy.max_decrease_pct / 100.0)
        )
        if proposed < min_allowed:
            proposed = min_allowed
            clamp_dec = True

    proposed = max(proposed, floor)
    return proposed, clamp_dec, False


def price_listing(
    listing: Listing,
    offers: list[MarketOffer],
    policy: PricingPolicy,
    *,
    exclude_user_id: int | None,
) -> PlanRow:
    base = PlanRow(
        listing_id=listing.id,
        blueprint_id=listing.blueprint_id,
        previous_price_cents=listing.price_cents,
        proposed_price_cents=None,
        action=PlanAction.SKIP,
        quantity=max(1, listing.quantity),
        name_en=listing.name_en,
        reason="",
    )

    if not listing.condition or not listing.language:
        base.skip_reason = SkipReason.MISSING_ATTRS
        base.reason = "skip:missing_attrs"
        return base

    floor_cents, floor_key = policy.floor_cents_for(
        rarity=listing.rarity, foil=listing.foil
    )

    comps = filter_comparable_offers(
        offers,
        listing,
        exclude_user_id=exclude_user_id,
        ct_zero_only=policy.ct_zero_only,
    )
    base.comparable_count = len(comps)
    prices = [o.price_cents for o in comps]

    selection = compute_market_price(prices, policy)

    if selection.method == "insufficient":
        if policy.insufficient_comps_fallback == "use_lowest" and prices:
            market_price = prices[0]
            method_tag = f"fallback_lowest={market_price}"
        elif policy.insufficient_comps_fallback == "keep_current":
            base.action = PlanAction.KEEP
            base.skip_reason = SkipReason.KEEP_CURRENT_INSUFFICIENT
            base.reason = f"keep:insufficient_comps={len(prices)}"
            return base
        else:
            base.skip_reason = SkipReason.INSUFFICIENT_COMPS
            base.reason = f"skip:insufficient_comps={len(prices)}"
            return base
    elif selection.method == "wide_spread":
        base.skip_reason = SkipReason.WIDE_SPREAD
        base.reason = f"skip:wide_spread={selection.detail}"
        return base
    elif selection.market_cents is None:
        base.skip_reason = SkipReason.MISSING_MARKET
        base.reason = "skip:missing_market"
        return base
    else:
        market_price = selection.market_cents
        method_tag = selection.detail

    is_sentinel = listing.price_cents >= policy.sentinel_price_cents
    floor_applied = market_price < floor_cents
    target = max(market_price, floor_cents)
    proposed, clamp_dec, clamp_inc = apply_clamps(
        listing.price_cents,
        target,
        policy,
        is_sentinel=is_sentinel,
        floor_cents=floor_cents,
    )

    base.market_price_cents = market_price
    base.target_price_cents = target
    base.proposed_price_cents = proposed
    base.clamp_decrease = clamp_dec
    base.clamp_increase = clamp_inc
    base.sentinel_clear = is_sentinel
    base.initial_price = is_sentinel

    parts = [method_tag]
    if floor_applied:
        parts.append(f"floor={floor_key}:{floor_cents}")
    if clamp_dec:
        parts.append(f"clamp_dec≤{policy.max_decrease_pct:g}%")
    if is_sentinel:
        parts.append("sentinel")

    if proposed == listing.price_cents and not is_sentinel:
        base.action = PlanAction.KEEP
        base.skip_reason = SkipReason.NO_CHANGE
        base.proposed_price_cents = None
        base.reason = "keep:no_change;" + ";".join(parts)
        return base

    delta = proposed - listing.price_cents
    band = dead_band_threshold_cents(listing.price_cents, policy)
    if not is_sentinel and abs(delta) < band:
        base.action = PlanAction.KEEP
        base.skip_reason = SkipReason.DEAD_BAND
        base.proposed_price_cents = None
        base.reason = f"keep:dead_band={abs(delta)}¢<{band}¢;" + ";".join(parts)
        return base

    base.action = PlanAction.UPDATE
    sign = "+" if delta >= 0 else ""
    base.reason = f"update:{';'.join(parts)};delta={sign}{delta}"
    return base
