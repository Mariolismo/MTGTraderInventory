"""Plan KPIs and change tables for DRY_RUN inspection."""

from __future__ import annotations

import csv
import json
from dataclasses import asdict, dataclass
from pathlib import Path

from cardtrader_inventory.models import PlanAction, PricingPlan


@dataclass
class PlanKpis:
    changed_listings: int
    changed_copies: int
    unchanged_listings: int
    catalog_listings: int
    catalog_copies: int
    increases: int
    decreases: int
    # qty-weighted unit averages over changed copies
    avg_unit_price_before_cents: float | None
    avg_unit_price_after_cents: float | None
    avg_increase_cents: float | None
    avg_decrease_cents: float | None
    avg_increase_pct: float | None
    avg_decrease_pct: float | None
    # change-% distribution remains per listing (same % for all copies)
    median_change_pct: float | None
    max_increase_pct: float | None
    max_decrease_pct: float | None
    # qty-weighted: sum(unit_price × quantity)
    changed_value_before_cents: int
    changed_value_after_cents: int
    changed_value_delta_cents: int
    catalog_value_before_cents: int
    catalog_value_after_cents: int
    catalog_value_delta_cents: int
    # backward-compatible aliases (changed stock only)
    total_value_before_cents: int
    total_value_after_cents: int
    net_value_delta_cents: int
    clamp_decrease_hits: int
    clamp_increase_hits: int
    decreases_over_max_pct: int


def _weighted_mean(weighted_sums: list[tuple[float, int]]) -> float | None:
    """Mean of values weighted by copy count (qty)."""
    total_w = sum(w for _, w in weighted_sums)
    if total_w <= 0:
        return None
    return sum(v * w for v, w in weighted_sums) / total_w


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def compute_plan_kpis(plan: PricingPlan) -> PlanKpis:
    updates = [
        row
        for row in plan.rows
        if row.action == PlanAction.UPDATE and row.proposed_price_cents is not None
    ]
    non_updates = [row for row in plan.rows if row.action != PlanAction.UPDATE]

    increase_unit_weights: list[tuple[float, int]] = []
    decrease_unit_weights: list[tuple[float, int]] = []
    increase_pct_weights: list[tuple[float, int]] = []
    decrease_pct_weights: list[tuple[float, int]] = []
    all_change_pcts: list[float] = []
    decreases_over_max = 0
    clamp_dec = 0
    clamp_inc = 0
    changed_copies = 0
    increase_listings = 0
    decrease_listings = 0

    changed_before = 0
    changed_after = 0

    for row in updates:
        qty = max(1, row.quantity)
        prev = row.previous_price_cents
        new = row.proposed_price_cents or prev
        changed_copies += qty
        changed_before += prev * qty
        changed_after += new * qty
        delta = new - prev
        pct = (100.0 * delta / prev) if prev > 0 else 0.0
        all_change_pcts.append(pct)
        if row.clamp_decrease:
            clamp_dec += 1
        if row.clamp_increase:
            clamp_inc += 1
        if delta > 0:
            increase_listings += 1
            increase_unit_weights.append((float(delta), qty))
            increase_pct_weights.append((pct, qty))
        elif delta < 0:
            decrease_listings += 1
            decrease_unit_weights.append((float(-delta), qty))
            decrease_pct_weights.append((-pct, qty))
            if not row.sentinel_clear and prev > 0 and (-pct) > 5.0 + 1e-9:
                decreases_over_max += 1

    catalog_before = changed_before
    catalog_after = changed_after
    catalog_copies = changed_copies
    for row in non_updates:
        qty = max(1, row.quantity)
        prev = row.previous_price_cents
        catalog_copies += qty
        catalog_before += prev * qty
        catalog_after += prev * qty

    avg_before = (
        (changed_before / changed_copies) if changed_copies else None
    )
    avg_after = (changed_after / changed_copies) if changed_copies else None

    return PlanKpis(
        changed_listings=len(updates),
        changed_copies=changed_copies,
        unchanged_listings=len(non_updates),
        catalog_listings=len(plan.rows),
        catalog_copies=catalog_copies,
        increases=increase_listings,
        decreases=decrease_listings,
        avg_unit_price_before_cents=avg_before,
        avg_unit_price_after_cents=avg_after,
        avg_increase_cents=_weighted_mean(increase_unit_weights),
        avg_decrease_cents=_weighted_mean(decrease_unit_weights),
        avg_increase_pct=_weighted_mean(increase_pct_weights),
        avg_decrease_pct=_weighted_mean(decrease_pct_weights),
        median_change_pct=_median(all_change_pcts),
        max_increase_pct=(
            max(p for p, _ in increase_pct_weights) if increase_pct_weights else None
        ),
        max_decrease_pct=(
            max(p for p, _ in decrease_pct_weights) if decrease_pct_weights else None
        ),
        changed_value_before_cents=changed_before,
        changed_value_after_cents=changed_after,
        changed_value_delta_cents=changed_after - changed_before,
        catalog_value_before_cents=catalog_before,
        catalog_value_after_cents=catalog_after,
        catalog_value_delta_cents=catalog_after - catalog_before,
        total_value_before_cents=changed_before,
        total_value_after_cents=changed_after,
        net_value_delta_cents=changed_after - changed_before,
        clamp_decrease_hits=clamp_dec,
        clamp_increase_hits=clamp_inc,
        decreases_over_max_pct=decreases_over_max,
    )


def iter_change_rows(plan: PricingPlan) -> list[dict]:
    rows: list[dict] = []
    for row in plan.rows:
        if row.action != PlanAction.UPDATE or row.proposed_price_cents is None:
            continue
        qty = max(1, row.quantity)
        prev = row.previous_price_cents
        new = row.proposed_price_cents
        delta = new - prev
        pct = (100.0 * delta / prev) if prev > 0 else 0.0
        rows.append(
            {
                "listing_id": row.listing_id,
                "blueprint_id": row.blueprint_id,
                "name_en": row.name_en,
                "quantity": qty,
                "old_price_cents": prev,
                "new_price_cents": new,
                "old_price_eur": round(prev / 100.0, 2),
                "new_price_eur": round(new / 100.0, 2),
                "market_eur": round((row.market_price_cents or 0) / 100.0, 2),
                "delta_cents": delta,
                "delta_pct": round(pct, 2),
                "line_value_before_eur": round(prev * qty / 100.0, 2),
                "line_value_after_eur": round(new * qty / 100.0, 2),
                "line_value_delta_eur": round((new - prev) * qty / 100.0, 2),
                "market_cents": row.market_price_cents,
                "comparable_count": row.comparable_count,
                "clamp_decrease": row.clamp_decrease,
                "clamp_increase": row.clamp_increase,
                "sentinel_clear": row.sentinel_clear,
                "reason": row.reason,
            }
        )
    rows.sort(key=lambda r: r["delta_pct"])
    return rows


def write_change_report(
    plan: PricingPlan,
    out_dir: Path,
    pricing_run_id: str,
) -> tuple[Path, Path, PlanKpis]:
    """Write KPI JSON + CSV of all proposed old→new prices."""
    out_dir.mkdir(parents=True, exist_ok=True)
    kpis = compute_plan_kpis(plan)
    changes = iter_change_rows(plan)

    kpi_path = out_dir / f"{pricing_run_id}-kpis.json"
    csv_path = out_dir / f"{pricing_run_id}-changes.csv"

    kpi_path.write_text(json.dumps(asdict(kpis), indent=2), encoding="utf-8")

    fieldnames = [
        "listing_id",
        "blueprint_id",
        "name_en",
        "quantity",
        "old_price_eur",
        "new_price_eur",
        "market_eur",
        "delta_pct",
        "line_value_before_eur",
        "line_value_after_eur",
        "line_value_delta_eur",
        "old_price_cents",
        "new_price_cents",
        "delta_cents",
        "market_cents",
        "comparable_count",
        "clamp_decrease",
        "clamp_increase",
        "sentinel_clear",
        "reason",
    ]
    with csv_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(changes)

    return kpi_path, csv_path, kpis


def format_kpi_table(kpis: PlanKpis) -> str:
    def eur(cents: float | None) -> str:
        if cents is None:
            return "n/a"
        return f"€{cents / 100.0:.2f}"

    def pct(value: float | None) -> str:
        if value is None:
            return "n/a"
        return f"{value:.2f}%"

    lines = [
        "--- Plan KPIs (money = unit_price * quantity) ---",
        f"changed_listings:       {kpis.changed_listings}",
        f"changed_copies:         {kpis.changed_copies}",
        f"unchanged_listings:     {kpis.unchanged_listings}",
        f"catalog_listings/copies:{kpis.catalog_listings} / {kpis.catalog_copies}",
        f"increases / decreases:  {kpis.increases} / {kpis.decreases} (listings)",
        f"avg unit price before:  {eur(kpis.avg_unit_price_before_cents)} (qty-weighted)",
        f"avg unit price after:   {eur(kpis.avg_unit_price_after_cents)} (qty-weighted)",
        f"avg increase (unit):    {eur(kpis.avg_increase_cents)} ({pct(kpis.avg_increase_pct)}) qty-weighted",
        f"avg decrease (unit):    {eur(kpis.avg_decrease_cents)} ({pct(kpis.avg_decrease_pct)}) qty-weighted",
        f"median change %:        {pct(kpis.median_change_pct)} (per listing)",
        f"max increase %:         {pct(kpis.max_increase_pct)}",
        f"max decrease %:         {pct(kpis.max_decrease_pct)}",
        f"changed stock before:   {eur(float(kpis.changed_value_before_cents))}",
        f"changed stock after:    {eur(float(kpis.changed_value_after_cents))}",
        f"changed stock delta:    {eur(float(kpis.changed_value_delta_cents))}",
        f"full catalog before:    {eur(float(kpis.catalog_value_before_cents))}",
        f"full catalog after:     {eur(float(kpis.catalog_value_after_cents))}",
        f"full catalog delta:     {eur(float(kpis.catalog_value_delta_cents))}",
        f"clamp decrease hits:    {kpis.clamp_decrease_hits}",
        f"clamp increase hits:    {kpis.clamp_increase_hits}",
        f"decreases > 5%:         {kpis.decreases_over_max_pct}",
    ]
    return "\n".join(lines)


def format_skip_counts(summary) -> str:
    return (
        f"wide_spread={summary.skipped_wide_spread} "
        f"dead_band={summary.skipped_dead_band} "
        f"insuff_comps={summary.skipped_insufficient_comps}"
    )
