#!/usr/bin/env python3
"""Inspect pending CT Zero sales and the resulting discount tier.

Shows all hub_pending seller orders (items sold, awaiting your shipment),
the total pending revenue, and what discount tier that total triggers today.

Usage:
    python scripts/check_weekly_sales.py
    python scripts/check_weekly_sales.py --raw    # dump full JSON for first order
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from cardtrader_inventory.config import PricingPolicy, load_api_token
from cardtrader_inventory.ct_client import CardTraderClient
from cardtrader_inventory.rate_limiter import RateLimiter
from cardtrader_inventory.weekly_sales import (
    TARGET_CENTS,
    _TIERS,
    evaluate_discount,
    sum_pending_sales_cents,
)


def _subtotal_cents(order: dict) -> int:
    ss = order.get("seller_subtotal")
    if isinstance(ss, dict):
        return int(ss.get("cents", 0))
    if isinstance(ss, (int, float)):
        return int(ss)
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw", action="store_true", help="Dump full JSON for first order")
    args = parser.parse_args()

    policy = PricingPolicy()
    client = CardTraderClient(load_api_token(), policy, limiter=RateLimiter(policy.marketplace_rps))

    now = datetime.now(timezone.utc)
    orders = client.list_orders(state="hub_pending")
    total = sum_pending_sales_cents(orders)
    discount = evaluate_discount(total, now)

    print(f"Hub-pending orders : {len(orders)}")
    print(f"Pending revenue    : {total} cents  (EUR {total/100:.2f})")
    print(f"Discount tier      : {discount}%  (target EUR {TARGET_CENTS/100:.0f})")
    print(f"Day-of-week        : {now.strftime('%A')} (isoweekday={now.isoweekday()})")
    print()

    if orders:
        print(f"  {'Order ID':<12} {'Items':>5}  {'seller_subtotal':>16}  {'via_zero'}")
        for o in orders:
            oid = str(o.get("id") or "?")
            size = o.get("size", "?")
            cents = _subtotal_cents(o)
            zero = o.get("via_cardtrader_zero", "?")
            print(f"  {oid:<12} {size:>5}  {cents:>14}c  {zero}")
        print()

    if args.raw and orders:
        print("--- Raw JSON (first order) ---")
        print(json.dumps(orders[0], indent=2, default=str))
        print()

    print("Tier thresholds:")
    for min_dow, threshold, pct in _TIERS:
        day_name = ["", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][min_dow]
        print(f"  {day_name}+  sales < EUR {threshold/100:.0f}  ->  {pct}%")
    print(f"  Auto-off at EUR {TARGET_CENTS/100:.0f}")


if __name__ == "__main__":
    main()
