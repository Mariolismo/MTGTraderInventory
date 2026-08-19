"""Weekly sales tracking and graduated discount evaluation.

DynamoDB row per week (reuses IdempotencyTable):
  PK = "weekly-sales"
  SK = "week-YYYY-MM-DD"   (Sunday that opens the sales week)

The pricing pipeline reads the active discount at plan time and applies it
as a market-price multiplier.  The apply handler increments sales_cents
after successful bulk updates.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)

PK = "weekly-sales"


# Thresholds (cents) and day-of-week gates.
# isoweekday(): Mon=1 … Sun=7.
_TIERS: list[tuple[int, int, int]] = [
    # (min_isoweekday, sales_below_cents, discount_pct)
    (5, 10_000, 7),   # Friday, sales < €100 → 7 %
    (5, 18_000, 5),   # Friday, sales < €180 → 5 %
    (3, 10_000, 3),   # Wednesday, sales < €100 → 3 %
]

TARGET_CENTS = 30_000  # €300 — auto-off threshold


def week_key(now: datetime) -> str:
    """Return ``week-YYYY-MM-DD`` for the Sunday that opens *now*'s sales week.

    The sales week runs Sunday 00:00 UTC → Saturday 23:59 UTC.
    ``isoweekday()`` maps Sun=7, so we subtract ``isoweekday() % 7`` days.
    """
    sunday = (now - timedelta(days=now.isoweekday() % 7)).date()
    return f"week-{sunday.isoformat()}"


def evaluate_discount(sales_cents: int, now: datetime) -> int:
    """Pure function: return the discount % (0/3/5/7) for current state."""
    if sales_cents >= TARGET_CENTS:
        return 0
    dow = now.isoweekday()  # Mon=1 … Sun=7
    for min_dow, threshold, pct in _TIERS:
        if dow >= min_dow and sales_cents < threshold:
            return pct
    return 0


# ---------------------------------------------------------------------------
# DynamoDB persistence
# ---------------------------------------------------------------------------

@dataclass
class WeeklySalesRow:
    week: str
    sales_cents: int
    discount_pct: int


class WeeklySalesStore:
    """Read/write weekly sales state in the shared IdempotencyTable."""

    def __init__(self, table_name: str, *, dynamodb_resource: Any | None = None) -> None:
        if dynamodb_resource is None:
            import boto3
            dynamodb_resource = boto3.resource("dynamodb")
        self._table = dynamodb_resource.Table(table_name)

    def get(self, now: datetime) -> WeeklySalesRow:
        """Return current week's row, defaulting to zeros if absent."""
        wk = week_key(now)
        resp = self._table.get_item(Key={"pricing_run_id": PK, "batch_id": wk})
        item = resp.get("Item")
        if not item:
            return WeeklySalesRow(week=wk, sales_cents=0, discount_pct=0)
        return WeeklySalesRow(
            week=wk,
            sales_cents=int(item.get("sales_cents", 0)),
            discount_pct=int(item.get("discount_pct", 0)),
        )

    def get_active_discount(self, now: datetime) -> int:
        """Convenience: return just the discount pct for plan-time reads."""
        return self.get(now).discount_pct

    def refresh_discount(self, now: datetime) -> int:
        """Re-evaluate discount from current sales total and persist if changed.

        Returns the (possibly updated) discount_pct.
        """
        row = self.get(now)
        new_pct = evaluate_discount(row.sales_cents, now)
        if new_pct != row.discount_pct:
            self._table.update_item(
                Key={"pricing_run_id": PK, "batch_id": row.week},
                UpdateExpression="SET discount_pct = :p, updated_at = :t",
                ExpressionAttributeValues={
                    ":p": new_pct,
                    ":t": now.isoformat(),
                },
            )
            logger.info(
                "Weekly discount changed %d%% -> %d%% (sales=%d week=%s)",
                row.discount_pct, new_pct, row.sales_cents, row.week,
            )
        return new_pct

    def increment_sales(self, amount_cents: int, now: datetime) -> WeeklySalesRow:
        """Atomically add to sales_cents, re-evaluate discount, return new state."""
        wk = week_key(now)
        resp = self._table.update_item(
            Key={"pricing_run_id": PK, "batch_id": wk},
            UpdateExpression=(
                "SET updated_at = :t"
                " ADD sales_cents :a"
            ),
            ExpressionAttributeValues={
                ":a": amount_cents,
                ":t": now.isoformat(),
            },
            ReturnValues="ALL_NEW",
        )
        attrs = resp.get("Attributes", {})
        new_sales = int(attrs.get("sales_cents", amount_cents))
        new_pct = evaluate_discount(new_sales, now)

        old_pct = int(attrs.get("discount_pct", 0))
        if new_pct != old_pct:
            self._table.update_item(
                Key={"pricing_run_id": PK, "batch_id": wk},
                UpdateExpression="SET discount_pct = :p",
                ExpressionAttributeValues={":p": new_pct},
            )
            logger.info(
                "Discount updated %d%% -> %d%% after +%d cents (total=%d week=%s)",
                old_pct, new_pct, amount_cents, new_sales, wk,
            )

        return WeeklySalesRow(week=wk, sales_cents=new_sales, discount_pct=new_pct)


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


def sync_weekly_sales(
    client: Any,
    store: "WeeklySalesStore",
    now: datetime,
) -> WeeklySalesRow:
    """Fetch orders from CT API, compute weekly total, overwrite store, return row.

    Uses an overwrite (not increment) so re-runs are idempotent.
    """
    orders = client.list_orders(state="hub_pending")
    sales = sum_pending_sales_cents(orders)
    wk = week_key(now)
    new_pct = evaluate_discount(sales, now)

    store._table.put_item(
        Item={
            "pricing_run_id": PK,
            "batch_id": wk,
            "sales_cents": sales,
            "discount_pct": new_pct,
            "updated_at": now.isoformat(),
        }
    )
    logger.info(
        "Weekly sales synced: %d cents, discount=%d%% (week=%s)",
        sales, new_pct, wk,
    )
    return WeeklySalesRow(week=wk, sales_cents=sales, discount_pct=new_pct)
