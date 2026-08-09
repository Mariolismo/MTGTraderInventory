"""DRY_RUN pricing pipeline orchestration (Phase 1)."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from cardtrader_inventory.config import PricingPolicy, load_api_token
from cardtrader_inventory.ct_client import CardTraderClient
from cardtrader_inventory.models import PlanSafetyResult, PricingPlan
from cardtrader_inventory.rate_limiter import RateLimiter
from cardtrader_inventory.report import PlanKpis, write_change_report

logger = logging.getLogger(__name__)


@dataclass
class DryRunResult:
    pricing_run_id: str
    mode: str
    listing_count: int
    priceable_count: int
    plan: PricingPlan
    sample_updates: list[dict]
    safety: PlanSafetyResult
    kpis: PlanKpis
    excluded_missing_attrs: list[int] = field(default_factory=list)


def new_pricing_run_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")
    return f"reprice-{stamp}"


def run_dry_run(
    *,
    policy: PricingPolicy | None = None,
    token: str | None = None,
    sample_size: int = 25,
    mode: str = "DRY_RUN",
    pricing_run_id: str | None = None,
) -> DryRunResult:
    """FETCH → VALIDATE → chunked PLAN → SAFETY CHECKS. Never mutates CT prices.

    Uses the same prepare → per-chunk plan → merge path as AWS Map orchestration,
    in-process (no S3).
    """
    if mode not in ("DRY_RUN", "LIVE"):
        raise ValueError(f"mode must be DRY_RUN or LIVE, got {mode!r}")

    # Local import avoids circular dependency with plan_orchestrate → pipeline.
    from cardtrader_inventory.aws.plan_orchestrate import (
        merge_chunk_plans,
        prepare_run,
        run_plan_chunk,
    )

    policy = policy or PricingPolicy.from_env()
    token = token or load_api_token()
    pricing_run_id = pricing_run_id or new_pricing_run_id()

    logger.info(
        "Starting %s run_id=%s marketplace_rps=%s chunk_size=%s",
        mode,
        pricing_run_id,
        policy.marketplace_rps,
        policy.generate_chunk_size,
    )

    client = CardTraderClient(
        token,
        policy,
        limiter=RateLimiter(policy.marketplace_rps),
    )

    prepared = prepare_run(
        client,
        policy,
        mode=mode,
        pricing_run_id=pricing_run_id,
    )
    chunk_plans = []
    for chunk in prepared.chunks:
        chunk_result = run_plan_chunk(
            client,
            prepared.listings,
            policy,
            pricing_run_id=prepared.pricing_run_id,
            mode=mode,
            chunk_id=str(chunk["chunk_id"]),
            fetch_keys_raw=list(chunk["fetch_keys"]),
            exclude_user_id=prepared.owner_user_id,
        )
        chunk_plans.append(chunk_result.plan)

    result = merge_chunk_plans(
        pricing_run_id=prepared.pricing_run_id,
        mode=mode,
        listing_count=prepared.listing_count,
        priceable_count=prepared.priceable_count,
        excluded_missing_attrs=prepared.excluded_missing_attrs,
        chunk_plans=chunk_plans,
        policy=policy,
        sample_size=sample_size,
    )
    logger.info(
        "%s complete run_id=%s export=%s priceable=%s excluded_attrs=%s "
        "proposed=%s safety_ok=%s chunks=%s",
        mode,
        result.pricing_run_id,
        result.listing_count,
        result.priceable_count,
        len(result.excluded_missing_attrs),
        result.plan.summary.price_updates_proposed,
        result.safety.ok,
        len(chunk_plans),
    )
    return result


def build_summary_dict(result: DryRunResult) -> dict:
    """JSON-serializable summary payload (local + S3)."""
    return {
        "pricing_run_id": result.pricing_run_id,
        "mode": result.mode,
        "listing_count": result.listing_count,
        "priceable_count": result.priceable_count,
        "excluded_missing_attrs": result.excluded_missing_attrs,
        "safety_ok": result.safety.ok,
        "safety_errors": result.safety.errors,
        "summary": asdict(result.plan.summary),
        "kpis": asdict(result.kpis),
        "sample_updates": result.sample_updates,
    }


def plan_jsonl_bytes(result: DryRunResult) -> bytes:
    from cardtrader_inventory.apply import plan_row_to_dict

    lines = [json.dumps(plan_row_to_dict(row)) for row in result.plan.rows]
    return ("\n".join(lines) + ("\n" if lines else "")).encode("utf-8")


def write_plan_artifacts(
    result: DryRunResult, out_dir: Path
) -> tuple[Path, Path, Path, Path]:
    """Write summary, plan JSONL, KPI JSON, and old→new CSV."""
    out_dir.mkdir(parents=True, exist_ok=True)
    summary_path = out_dir / f"{result.pricing_run_id}-summary.json"
    plan_path = out_dir / f"{result.pricing_run_id}-plan.jsonl"

    summary_path.write_text(
        json.dumps(build_summary_dict(result), indent=2), encoding="utf-8"
    )
    plan_path.write_bytes(plan_jsonl_bytes(result))

    kpi_path, csv_path, _ = write_change_report(
        result.plan, out_dir, result.pricing_run_id
    )
    return summary_path, plan_path, kpi_path, csv_path
