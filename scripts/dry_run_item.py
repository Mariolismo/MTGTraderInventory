#!/usr/bin/env python3
"""DRY_RUN price proposal for a single inventory item (no CT mutations).

Examples:
  python scripts/dry_run_item.py --listing-id 396028383
  python scripts/dry_run_item.py --blueprint-id 365299
  python scripts/dry_run_item.py --name "Bloom Tender"
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from cardtrader_inventory.buyer_fees import buyer_total_cents
from cardtrader_inventory.config import ConfigError, PricingPolicy, load_api_token
from cardtrader_inventory.ct_client import CardTraderClient, CardTraderError
from cardtrader_inventory.models import Listing
from cardtrader_inventory.pricing import (
    compute_market_price,
    filter_comparable_offers,
    normalize_language,
    price_listing,
)
from cardtrader_inventory.rate_limiter import RateLimiter


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Single-item DRY_RUN pricing (no CardTrader mutations)"
    )
    parser.add_argument(
        "--listing-id",
        type=int,
        help="Your CardTrader product/listing id (e.g. 396028383)",
    )
    parser.add_argument(
        "--blueprint-id",
        type=int,
        help="Blueprint id (e.g. 365299 for Bloom Tender Collectors)",
    )
    parser.add_argument(
        "--name",
        type=str,
        help="Case-insensitive substring match on name_en (uses full export)",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Debug logging",
    )
    args = parser.parse_args(argv)
    if not args.listing_id and not args.blueprint_id and not args.name:
        parser.error("Provide --listing-id and/or --blueprint-id and/or --name")
    return args


def _eur(cents: int | None) -> str:
    if cents is None:
        return "n/a"
    return f"€{cents / 100.0:.2f}"


def _eur_buyer_facing(cents: int | None) -> str:
    """Marketplace comps are already what the customer pays."""
    if cents is None:
        return "n/a"
    return f"{_eur(cents)} (customer)"


def _eur_list_with_customer(list_cents: int | None) -> str:
    """Seller list price + implied CT checkout total for the buyer."""
    if list_cents is None:
        return "n/a"
    return f"{_eur(list_cents)} (customer {_eur(buyer_total_cents(list_cents))})"

def resolve_listing(
    client: CardTraderClient,
    *,
    listing_id: int | None,
    blueprint_id: int | None,
    name: str | None,
) -> Listing:
    """Load one of your products via export (scoped when possible)."""
    if blueprint_id is not None and listing_id is None and name is None:
        listings = client.export_products_for_blueprint(blueprint_id)
    elif blueprint_id is not None and listing_id is not None:
        listings = client.export_products_for_blueprint(blueprint_id)
    else:
        listings = client.export_products()

    matches = listings
    if listing_id is not None:
        matches = [lst for lst in matches if lst.id == listing_id]
    if blueprint_id is not None:
        matches = [lst for lst in matches if lst.blueprint_id == blueprint_id]
    if name:
        needle = name.strip().lower()
        matches = [lst for lst in matches if needle in lst.name_en.lower()]

    if not matches:
        raise SystemExit(
            "No matching listing in your inventory. "
            f"listing_id={listing_id} blueprint_id={blueprint_id} name={name!r}"
        )
    if len(matches) > 1:
        print(f"Matched {len(matches)} listings; using the first. Candidates:")
        for lst in matches[:20]:
            print(
                f"  id={lst.id} blueprint={lst.blueprint_id} "
                f"{lst.name_en!r} {lst.condition} "
                f"{'foil' if lst.foil else 'nonfoil'} {_eur(lst.price_cents)} x{lst.quantity}"
            )
        if len(matches) > 20:
            print(f"  ... and {len(matches) - 20} more")
    return matches[0]


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        policy = PricingPolicy.from_env()
        token = load_api_token()
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    client = CardTraderClient(
        token,
        policy,
        limiter=RateLimiter(policy.marketplace_rps),
    )

    try:
        listing = resolve_listing(
            client,
            listing_id=args.listing_id,
            blueprint_id=args.blueprint_id,
            name=args.name,
        )
        offers = client.marketplace_products(
            listing.blueprint_id,
            language=normalize_language(listing.language) or None,
            foil=listing.foil,
        )
    except CardTraderError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    comps = filter_comparable_offers(
        offers,
        listing,
        exclude_user_id=listing.user_id,
        ct_zero_only=policy.ct_zero_only,
    )
    prices = [o.price_cents for o in comps]
    selection = compute_market_price(prices, policy)
    row = price_listing(
        listing,
        offers,
        policy,
        exclude_user_id=listing.user_id,
    )

    print("=== Single-item DRY_RUN (no CT mutations) ===")
    print(f"listing_id:     {listing.id}")
    print(f"blueprint_id:   {listing.blueprint_id}")
    print(f"name:           {listing.name_en}")
    print(
        f"attrs:          {listing.condition} | {listing.language} | "
        f"{'foil' if listing.foil else 'nonfoil'} | "
        f"rarity={listing.rarity or 'n/a'} | qty={listing.quantity}"
    )
    floor_cents, floor_key = policy.floor_cents_for(
        rarity=listing.rarity, foil=listing.foil
    )
    print(f"your price:     {_eur_list_with_customer(listing.price_cents)}")
    print(
        f"policy:         median_window={policy.market_median_window} "
        f"ct_zero_only={policy.ct_zero_only} "
        f"nm_sp_merged=True "
        f"spread_max={policy.max_comp_spread_pct:g}% "
        f"(only if window_min>{policy.comp_spread_min_price_cents}¢) "
        f"dead_band=max({policy.min_change_cents}¢,{policy.min_change_pct:g}%) "
        f"clamp=decrease≤{policy.max_decrease_pct:g}% (no upside clamp) "
        f"floor={floor_key}:{floor_cents}¢"
    )
    print(f"raw offers:     {len(offers)}")
    print(f"comparable:     {len(comps)}")
    spread_txt = (
        f"{selection.spread_ratio:.2f}" if selection.spread_ratio is not None else "n/a"
    )
    print(
        f"market method:  {selection.method} window={selection.window_size} "
        f"spread={spread_txt} ({selection.detail})"
    )
    print()
    print("Comparable offers (after filters), cheapest first:")
    window_n = min(policy.market_median_window, len(comps))
    for i, offer in enumerate(comps[:15], start=1):
        marker = ""
        if selection.method.startswith("median") and i <= window_n:
            marker = " <-- window"
        elif selection.method == "third" and i == 3:
            marker = " <-- 3rd"
        print(
            f"  {i:2d}. {_eur(offer.price_cents):>8}  {offer.condition:<16}  "
            f"zero={offer.ct_zero}  id={offer.product_id}{marker}"
        )
    if len(comps) > 15:
        print(f"  ... {len(comps) - 15} more")
    print()
    print(f"action:         {row.action.value}")
    if row.skip_reason:
        print(f"skip_reason:    {row.skip_reason.value}")
    print(f"reason:         {row.reason}")
    print(f"market:         {_eur_buyer_facing(row.market_price_cents)}")
    print(f"target:         {_eur_list_with_customer(row.target_price_cents)}")
    print(f"proposed:       {_eur_list_with_customer(row.proposed_price_cents)}")
    print(
        f"clamps:         decrease={row.clamp_decrease} increase={row.clamp_increase} "
        f"sentinel={row.sentinel_clear}"
    )
    if (
        row.proposed_price_cents is not None
        and listing.price_cents > 0
        and row.action.value == "update"
    ):
        delta = row.proposed_price_cents - listing.price_cents
        pct = 100.0 * delta / listing.price_cents
        print(
            f"delta:          {_eur_list_with_customer(listing.price_cents)} → "
            f"{_eur_list_with_customer(row.proposed_price_cents)} "
            f"({delta:+d}¢ list / {pct:+.1f}%)"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
