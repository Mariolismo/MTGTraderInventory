"""CloudWatch custom metrics for reprice runs (alarm-friendly).

Uses boto3 from the Lambda runtime. Failures are logged only — metrics must
never break the pricing pipeline.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Mapping

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
