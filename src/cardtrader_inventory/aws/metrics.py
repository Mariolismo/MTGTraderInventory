"""CloudWatch custom metrics for reprice runs (alarm-friendly).

Uses boto3 from the Lambda runtime. Failures are logged only — metrics must
never break the pricing pipeline.

Free-tier note: keep distinct metric *names* ≤ ~10 in this namespace.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Mapping

from cardtrader_inventory.config import SENTINEL_PRICE_CENTS
from cardtrader_inventory.models import PlanAction, PlanSummary, PricingPlan

logger = logging.getLogger(__name__)

DEFAULT_NAMESPACE = "CardTraderInventory/Reprice"


def _cloudwatch_client() -> Any:
    import boto3

    return boto3.client("cloudwatch")


def put_metrics(
    metrics: Mapping[str, tuple[float | int, str]],
    *,
    namespace: str | None = None,
) -> None:
    """Publish metrics as ``{name: (value, unit)}`` (e.g. unit ``Count`` or ``None``)."""
    if not metrics:
        return

    ns = namespace or os.environ.get("METRICS_NAMESPACE", DEFAULT_NAMESPACE)
    metric_data = [
        {
            "MetricName": name,
            "Value": float(value),
            "Unit": unit,
        }
        for name, (value, unit) in metrics.items()
    ]

    try:
        _cloudwatch_client().put_metric_data(Namespace=ns, MetricData=metric_data)
        logger.info(
            "Published %s CloudWatch metrics to %s names=%s",
            len(metric_data),
            ns,
            sorted(metrics),
        )
    except Exception:  # noqa: BLE001 — never fail the run for metrics
        logger.exception("Failed to publish CloudWatch metrics (continuing)")


def put_count_metrics(
    metrics: Mapping[str, float | int],
    *,
    mode: str = "",
    namespace: str | None = None,
) -> None:
    """Back-compat helper: all values as Count (``mode`` ignored)."""
    del mode
    put_metrics({k: (v, "Count") for k, v in metrics.items()}, namespace=namespace)


def count_sentinel_remaining(plan: PricingPlan) -> int:
    """Listings still at sentinel after this plan (not cleared by an UPDATE)."""
    n = 0
    for row in plan.rows:
        prev = row.previous_price_cents
        if prev < SENTINEL_PRICE_CENTS:
            continue
        if row.action == PlanAction.UPDATE and row.proposed_price_cents is not None:
            if row.proposed_price_cents < SENTINEL_PRICE_CENTS:
                continue  # clearing this run
        n += 1
    return n


def plan_observability_metrics(
    plan: PricingPlan,
    *,
    cards_in_inventory: int,
    inventory_eur: float,
    safety_ok: bool,
) -> dict[str, tuple[float | int, str]]:
    """Merge-time custom metrics (stay within ~10 names total in the namespace).

    Shared names with apply: RepriceError.
    Apply-only: PriceUpdatesApplied.
    """
    summary: PlanSummary = plan.summary
    return {
        "CardsInInventory": (cards_in_inventory, "Count"),
        "InventoryValue": (inventory_eur, "None"),
        "RepriceError": (0 if safety_ok else 1, "Count"),
        "PriceUpdatesProposed": (summary.price_updates_proposed, "Count"),
        "SkipWideSpread": (summary.skipped_wide_spread, "Count"),
        "SkipInsufficientComps": (summary.skipped_insufficient_comps, "Count"),
        "SentinelCleared": (summary.sentinel_initial_priced, "Count"),
        "SentinelRemaining": (count_sentinel_remaining(plan), "Count"),
    }
