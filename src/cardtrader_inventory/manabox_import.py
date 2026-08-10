"""Plan ManaBox CSV rows into CardTrader bulk_create payloads.

Resolution rules (strict):
  - Card identity: ManaBox Scryfall ID → CT blueprint (via blueprints/export UIDs,
    with TCGPlayer/Cardmarket enrich when CT omits scryfall_id)
  - Foil: ManaBox Foil column → properties.mtg_foil
  - Language: ManaBox Language → properties.mtg_language
  - Condition: ManaBox EU grade → CT US grade string (excellent→Slightly Played)
  - Price: sentinel maximum (default €9999.99 / SENTINEL_PRICE_CENTS)
"""

from __future__ import annotations

import csv
import logging
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

from cardtrader_inventory.config import SENTINEL_PRICE_CENTS
from cardtrader_inventory.ct_client import CardTraderClient
from cardtrader_inventory.manabox_compare import (
    foil_from_manabox,
    normalize_language,
    normalize_scryfall_id,
    normalize_set_code,
)
from cardtrader_inventory.models import Listing
from cardtrader_inventory.scryfall import (
    BlueprintUids,
    enrich_blueprint_scryfall,
)

logger = logging.getLogger(__name__)

# ManaBox / Cardmarket EU → CardTrader property string (US-style).
# Source: https://mtg.wiki/page/Grading_condition
_MANABOX_CONDITION_TO_CT: dict[str, str] = {
    "mint": "Near Mint",  # CT Mint exists but NM is the practical top grade
    "near_mint": "Near Mint",
    "nm": "Near Mint",
    "excellent": "Slightly Played",
    "ex": "Slightly Played",
    "good": "Moderately Played",
    "gd": "Moderately Played",
    "light_played": "Played",
    "lightly_played": "Played",
    "lp": "Played",
    "played": "Heavily Played",
    "pl": "Heavily Played",
    "poor": "Poor",
}


@dataclass
class CsvCardRow:
    name: str
    set_code: str
    collector_number: str
    foil: bool
    language: str
    condition_raw: str
    quantity: int
    scryfall_id: str
    altered: bool = False
    misprint: bool = False
    line_no: int = 0


@dataclass
class ImportPlanRow:
    action: str  # create | skip_existing | skip_error
    name: str
    set_code: str
    collector_number: str
    foil: bool
    language: str
    condition_ct: str
    quantity: int
    scryfall_id: str
    blueprint_id: int | None = None
    price_cents: int = SENTINEL_PRICE_CENTS
    reason: str = ""
    line_no: int = 0
    altered: bool = False


@dataclass
class ImportPlan:
    rows: list[ImportPlanRow]
    price_cents: int
    summary: dict[str, Any] = field(default_factory=dict)


def manabox_condition_to_ct(value: str) -> str | None:
    raw = (value or "").strip().lower().replace("-", "_").replace(" ", "_")
    return _MANABOX_CONDITION_TO_CT.get(raw)


def _as_bool(value: str) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "y"}


def load_csv_cards(path: Path) -> list[CsvCardRow]:
    rows: list[CsvCardRow] = []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for index, row in enumerate(reader, start=2):  # header is line 1
            scryfall = normalize_scryfall_id(row.get("Scryfall ID") or "")
            qty = int(float(row.get("Quantity") or 0))
            if qty <= 0:
                continue
            rows.append(
                CsvCardRow(
                    name=(row.get("Name") or "").strip(),
                    set_code=normalize_set_code(row.get("Set code") or ""),
                    collector_number=(row.get("Collector number") or "").strip(),
                    foil=foil_from_manabox(row.get("Foil") or ""),
                    language=normalize_language(row.get("Language") or "en"),
                    condition_raw=(row.get("Condition") or "").strip(),
                    quantity=qty,
                    scryfall_id=scryfall,
                    altered=_as_bool(row.get("Altered") or ""),
                    misprint=_as_bool(row.get("Misprint") or ""),
                    line_no=index,
                )
            )
    return rows


def expansion_ids_for_set_codes(
    set_codes: Iterable[str],
    expansions: dict[int, str],
) -> list[int]:
    """CT expansion ids that may contain cards for the given Scryfall set codes."""
    wanted = {normalize_set_code(c) for c in set_codes if c}
    if not wanted:
        return []
    hits: list[int] = []
    for exp_id, code in expansions.items():
        code_u = normalize_set_code(code)
        if code_u in wanted:
            hits.append(exp_id)
            continue
        # Collectors / promos: CTDM, PTDM, CECC, etc.
        for base in wanted:
            if code_u == f"C{base}" or code_u == f"P{base}":
                hits.append(exp_id)
                break
            if code_u.endswith(base) and 1 <= len(code_u) - len(base) <= 2:
                hits.append(exp_id)
                break
    return sorted(set(hits))


def build_scryfall_blueprint_index(
    client: CardTraderClient,
    set_codes: Iterable[str],
) -> dict[str, list[BlueprintUids]]:
    """Map scryfall_id → CT blueprints (usually one)."""
    expansions = client.list_expansions()
    exp_ids = expansion_ids_for_set_codes(set_codes, expansions)
    logger.info(
        "Indexing blueprints for %s set codes across %s CT expansions",
        len(set(normalize_set_code(c) for c in set_codes if c)),
        len(exp_ids),
    )
    catalog = client.blueprint_uid_catalog(exp_ids)
    # Only hit Scryfall for CT blueprints that lack scryfall_id (can be slow).
    missing_scryfall = {
        bp_id for bp_id, info in catalog.items() if not info.scryfall_id
    }
    if missing_scryfall:
        enrich_blueprint_scryfall(catalog, needed_blueprint_ids=missing_scryfall)
    else:
        logger.info("All %s blueprints already have scryfall_id; skipping enrich", len(catalog))

    by_scryfall: dict[str, list[BlueprintUids]] = defaultdict(list)
    seen_bp: set[int] = set()
    for bp_id, info in catalog.items():
        if not info.scryfall_id or bp_id in seen_bp:
            continue
        seen_bp.add(bp_id)
        by_scryfall[info.scryfall_id].append(info)
    return dict(by_scryfall)


def inventory_product_keys(listings: Iterable[Listing]) -> set[tuple[int, bool, str, str]]:
    """Existing CT products keyed by blueprint + foil + language + CT condition."""
    keys: set[tuple[int, bool, str, str]] = set()
    for listing in listings:
        cond = (listing.condition or "").strip()
        lang = normalize_language(listing.language)
        keys.add((listing.blueprint_id, bool(listing.foil), lang, cond))
    return keys


def plan_import(
    csv_rows: list[CsvCardRow],
    scryfall_index: dict[str, list[BlueprintUids]],
    existing_keys: set[tuple[int, bool, str, str]],
    *,
    price_cents: int = SENTINEL_PRICE_CENTS,
) -> ImportPlan:
    planned: list[ImportPlanRow] = []
    for row in csv_rows:
        if not row.scryfall_id:
            planned.append(
                ImportPlanRow(
                    action="skip_error",
                    name=row.name,
                    set_code=row.set_code,
                    collector_number=row.collector_number,
                    foil=row.foil,
                    language=row.language,
                    condition_ct="",
                    quantity=row.quantity,
                    scryfall_id="",
                    reason="missing_scryfall_id",
                    line_no=row.line_no,
                    altered=row.altered or row.misprint,
                )
            )
            continue

        condition_ct = manabox_condition_to_ct(row.condition_raw)
        if condition_ct is None:
            planned.append(
                ImportPlanRow(
                    action="skip_error",
                    name=row.name,
                    set_code=row.set_code,
                    collector_number=row.collector_number,
                    foil=row.foil,
                    language=row.language,
                    condition_ct="",
                    quantity=row.quantity,
                    scryfall_id=row.scryfall_id,
                    reason=f"unknown_condition:{row.condition_raw!r}",
                    line_no=row.line_no,
                    altered=row.altered or row.misprint,
                )
            )
            continue

        matches = scryfall_index.get(row.scryfall_id) or []
        if not matches:
            planned.append(
                ImportPlanRow(
                    action="skip_error",
                    name=row.name,
                    set_code=row.set_code,
                    collector_number=row.collector_number,
                    foil=row.foil,
                    language=row.language,
                    condition_ct=condition_ct,
                    quantity=row.quantity,
                    scryfall_id=row.scryfall_id,
                    reason="blueprint_not_found_for_scryfall",
                    line_no=row.line_no,
                    altered=row.altered or row.misprint,
                )
            )
            continue
        if len(matches) > 1:
            ids = ",".join(str(m.blueprint_id) for m in matches)
            planned.append(
                ImportPlanRow(
                    action="skip_error",
                    name=row.name,
                    set_code=row.set_code,
                    collector_number=row.collector_number,
                    foil=row.foil,
                    language=row.language,
                    condition_ct=condition_ct,
                    quantity=row.quantity,
                    scryfall_id=row.scryfall_id,
                    reason=f"ambiguous_blueprints:{ids}",
                    line_no=row.line_no,
                    altered=row.altered or row.misprint,
                )
            )
            continue

        blueprint_id = matches[0].blueprint_id
        key = (blueprint_id, row.foil, row.language, condition_ct)
        if key in existing_keys:
            planned.append(
                ImportPlanRow(
                    action="skip_existing",
                    name=row.name,
                    set_code=row.set_code,
                    collector_number=row.collector_number,
                    foil=row.foil,
                    language=row.language,
                    condition_ct=condition_ct,
                    quantity=row.quantity,
                    scryfall_id=row.scryfall_id,
                    blueprint_id=blueprint_id,
                    price_cents=price_cents,
                    reason="already_in_ct_inventory",
                    line_no=row.line_no,
                    altered=row.altered or row.misprint,
                )
            )
            continue

        planned.append(
            ImportPlanRow(
                action="create",
                name=row.name,
                set_code=row.set_code,
                collector_number=row.collector_number,
                foil=row.foil,
                language=row.language,
                condition_ct=condition_ct,
                quantity=row.quantity,
                scryfall_id=row.scryfall_id,
                blueprint_id=blueprint_id,
                price_cents=price_cents,
                reason="ok",
                line_no=row.line_no,
                altered=row.altered or row.misprint,
            )
        )

    summary = {
        "csv_rows": len(csv_rows),
        "create": sum(1 for r in planned if r.action == "create"),
        "skip_existing": sum(1 for r in planned if r.action == "skip_existing"),
        "skip_error": sum(1 for r in planned if r.action == "skip_error"),
        "create_copies": sum(r.quantity for r in planned if r.action == "create"),
        "price_cents": price_cents,
        "price_eur": price_cents / 100.0,
    }
    return ImportPlan(rows=planned, price_cents=price_cents, summary=summary)


def build_bulk_create_payload(rows: list[ImportPlanRow]) -> list[dict[str, Any]]:
    products: list[dict[str, Any]] = []
    for row in rows:
        if row.action != "create" or row.blueprint_id is None:
            continue
        properties: dict[str, Any] = {
            "condition": row.condition_ct,
            "mtg_language": row.language,
            "mtg_foil": bool(row.foil),
            "signed": False,
            "altered": bool(row.altered),
        }
        products.append(
            {
                "blueprint_id": row.blueprint_id,
                "price": row.price_cents / 100.0,
                "quantity": row.quantity,
                "error_mode": "strict",
                "user_data_field": f"manabox:{row.scryfall_id}",
                "properties": properties,
                "graded": False,
            }
        )
    return products


def plan_from_csv(
    client: CardTraderClient,
    csv_path: Path,
    *,
    price_cents: int = SENTINEL_PRICE_CENTS,
) -> ImportPlan:
    csv_rows = load_csv_cards(csv_path)
    set_codes = {r.set_code for r in csv_rows if r.set_code}
    scryfall_index = build_scryfall_blueprint_index(client, set_codes)
    logger.info(
        "Scryfall index ready (%s ids); exporting CT inventory to skip duplicates",
        len(scryfall_index),
    )
    listings = client.export_products()
    logger.info("CT inventory snapshot: %s listings", len(listings))
    existing = inventory_product_keys(listings)
    plan = plan_import(
        csv_rows,
        scryfall_index,
        existing,
        price_cents=price_cents,
    )
    plan.summary["ct_listings_snapshot"] = len(listings)
    plan.summary["scryfall_index_size"] = len(scryfall_index)
    return plan


def apply_import_plan(
    client: CardTraderClient,
    plan: ImportPlan,
    *,
    batch_size: int = 100,
) -> dict[str, Any]:
    """LIVE bulk_create for plan rows with action=create. Waits for each job."""
    creates = [r for r in plan.rows if r.action == "create"]
    products = build_bulk_create_payload(creates)
    batches: list[dict[str, Any]] = []
    for start in range(0, len(products), batch_size):
        chunk = products[start : start + batch_size]
        job_uuid = client.bulk_create_products(chunk)
        job = client.wait_for_job(job_uuid)
        batches.append(
            {
                "job": job_uuid,
                "size": len(chunk),
                "state": job.get("state"),
                "stats": job.get("stats"),
            }
        )
        if str(job.get("state") or "") != "completed":
            raise RuntimeError(
                f"bulk_create job {job_uuid} finished state={job.get('state')} "
                f"stats={job.get('stats')}"
            )
    return {
        "created_rows": len(creates),
        "created_products_payload": len(products),
        "batches": batches,
    }


def plan_to_dict(plan: ImportPlan) -> dict[str, Any]:
    return {
        "summary": plan.summary,
        "price_cents": plan.price_cents,
        "rows": [asdict(r) for r in plan.rows],
    }
