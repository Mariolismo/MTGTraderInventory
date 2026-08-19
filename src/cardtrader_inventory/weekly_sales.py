"""Graduated weekly discount based on pending CT Zero sales.

Fetches hub_pending seller orders from the CT API each run, sums
seller_subtotal, and evaluates the discount tier.  Purely stateless —
no DynamoDB persistence needed since the CT API is the source of truth
and the hub_pending queue resets naturally on shipment.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# Thresholds (cents) and day-of-week gates.
# isoweekday(): Mon=1 … Sun=7.
_TIERS: list[tuple[int, int, int]] = [
    # (min_isoweekday, sales_below_cents, discount_pct)
    (5, 10_000, 7),   # Friday, sales < €100 → 7 %
    (5, 18_000, 5),   # Friday, sales < €180 → 5 %
    (3, 10_000, 3),   # Wednesday, sales < €100 → 3 %
]

TARGET_CENTS = 30_000  # €300 — auto-off threshold


@dataclass(frozen=True)
class WeeklySalesResult:
    sales_cents: int
    discount_pct: int


def evaluate_discount(sales_cents: int, now: Any) -> int:
    """Pure function: return the discount % (0/3/5/7) for current state."""
    if sales_cents >= TARGET_CENTS:
        return 0
    dow = now.isoweekday()  # Mon=1 … Sun=7
    for min_dow, threshold, pct in _TIERS:
        if dow >= min_dow and sales_cents < threshold:
            return pct
    return 0


def sum_pending_sales_cents(orders: list[dict[str, Any]]) -> int:
    """Sum seller_subtotal (cents) for hub_pending CT Zero orders.

    These are items sold but not yet shipped to the hub.  The queue resets
    naturally when you ship (CT moves them out of hub_pending), so no date
    filtering is needed — the total *is* the current week's pending revenue.
    """
    total = 0
    for order in orders:
        ss = order.get("seller_subtotal")
        if isinstance(ss, dict):
            total += int(ss.get("cents", 0))
        elif isinstance(ss, (int, float)):
            total += int(ss)
    return total


def fetch_weekly_sales(client: Any, now: Any) -> WeeklySalesResult:
    """Fetch hub_pending orders, compute total, evaluate discount tier.

    Stateless — calls the CT API every time.  Designed to run in the
    prepare handler at the start of each repricing run (~hourly).
    """
    orders = client.list_orders(state="hub_pending")
    sales = sum_pending_sales_cents(orders)
    pct = evaluate_discount(sales, now)
    logger.info("Weekly sales: %d cents, discount=%d%%", sales, pct)
    return WeeklySalesResult(sales_cents=sales, discount_pct=pct)
