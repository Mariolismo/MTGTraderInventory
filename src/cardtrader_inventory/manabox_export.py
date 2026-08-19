"""Export live CardTrader inventory as a ManaBox-importable CSV."""

from __future__ import annotations

import csv
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from cardtrader_inventory.ct_client import CardTraderClient
from cardtrader_inventory.manabox_compare import (
    MatchKey,
    AggregateRow,
    aggregate_ct_listings,
    expansion_id_from_listing,
    make_key,
    normalize_condition,
)
from cardtrader_inventory.models import Listing
from cardtrader_inventory.scryfall import (
    blueprint_scryfall_map,
    enrich_blueprint_scryfall,
)

logger = logging.getLogger(__name__)

MANABOX_CSV_FIELDS = [
    "Name",
    "Set code",
    "Collector number",
    "Foil",
    "Quantity",
    "Condition",
    "Language",
    "Scryfall ID",
    "Altered",
    "Misprint",
]

# CardTrader US grades (normalized) → ManaBox EU export labels.
_CT_TO_MANABOX_CONDITION: dict[str, str] = {
    "near mint": "near_mint",
    "slightly played": "excellent",
    "moderately played": "good",
    "played": "played",
    "heavily played": "played",
    "poor": "poor",
    "damaged": "poor",
    "mint": "near_mint",
}


@dataclass(frozen=True)
class ManaboxStockRow:
    name: str
    set_code: str
    collector_number: str
    foil: bool
    quantity: int
    condition_manabox: str
    language: str
    scryfall_id: str
    altered: bool = False
    misprint: bool = False


@dataclass
class ManaboxStockExport:
    fetched_at: str
    rows: list[ManaboxStockRow]
    summary: dict[str, Any]


def ct_condition_to_manabox(condition: str) -> str:
    canonical = normalize_condition(condition)
    mapped = _CT_TO_MANABOX_CONDITION.get(canonical)
    if mapped:
        return mapped
    return canonical.replace(" ", "_") or "near_mint"


def foil_to_manabox(foil: bool) -> str:
    return "foil" if foil else "normal"


def _listing_flags(listing: Listing) -> tuple[bool, bool]:
    props = listing.raw.get("properties_hash") or listing.raw.get("properties") or {}
    if not isinstance(props, dict):
        return False, False
    altered = str(props.get("altered", False)).strip().lower() in {"1", "true", "yes", "y"}
    misprint = str(props.get("misprint", False)).strip().lower() in {"1", "true", "yes", "y"}
    return altered, misprint


def aggregate_ct_for_manabox_export(
    listings: Iterable[Listing],
    blueprint_scryfall: dict[int, str],
    expansion_codes: dict[int, str],
    *,
    ignore_condition: bool = False,
) -> tuple[dict[MatchKey, AggregateRow], dict[MatchKey, tuple[bool, bool]], int, int]:
    """Aggregate CT listings; also track altered/misprint flags per match key."""
    filtered = [lst for lst in listings if lst.quantity > 0]
    by_key, missing_scryfall, sentinel_copies = aggregate_ct_listings(
        filtered,
        blueprint_scryfall,
        expansion_codes,
        ignore_condition=ignore_condition,
    )
    flags: dict[MatchKey, tuple[bool, bool]] = {}
    for listing in filtered:
        scryfall = (blueprint_scryfall.get(listing.blueprint_id) or "").strip().lower()
        if not scryfall:
            continue
        key = make_key(
            scryfall_id=scryfall,
            foil=listing.foil,
            language=listing.language,
            condition=listing.condition,
            ignore_condition=ignore_condition,
        )
        if key not in by_key:
            continue
        altered, misprint = _listing_flags(listing)
        prev_alt, prev_mis = flags.get(key, (False, False))
        flags[key] = (prev_alt or altered, prev_mis or misprint)
    return by_key, flags, missing_scryfall, sentinel_copies


def rows_from_ct_aggregate(
    by_key: dict[MatchKey, AggregateRow],
    flags: dict[MatchKey, tuple[bool, bool]],
    *,
    ignore_condition: bool,
) -> list[ManaboxStockRow]:
    rows: list[ManaboxStockRow] = []
    for key, agg in sorted(
        by_key.items(),
        key=lambda item: (item[1].display_name.lower(), item[0]),
    ):
        scryfall_id, foil, language, cond_norm = key
        if cond_norm:
            condition_mb = ct_condition_to_manabox(cond_norm)
        elif agg.conditions:
            condition_mb = ct_condition_to_manabox(next(iter(sorted(agg.conditions))))
        else:
            condition_mb = "near_mint"
        if ignore_condition and not cond_norm:
            condition_mb = "near_mint"
        altered, misprint = flags.get(key, (False, False))
        rows.append(
            ManaboxStockRow(
                name=agg.display_name,
                set_code=agg.set_code,
                collector_number=agg.collector_number,
                foil=foil,
                quantity=agg.qty,
                condition_manabox=condition_mb,
                language=language or "en",
                scryfall_id=scryfall_id,
                altered=altered,
                misprint=misprint,
            )
        )
    return rows


def export_ct_to_manabox_stock(
    client: CardTraderClient,
    *,
    ignore_condition: bool = False,
) -> ManaboxStockExport:
    """Fetch CT inventory and build ManaBox-import rows."""
    expansion_codes = client.list_expansions()
    listings = client.export_products()
    expansion_ids = [
        exp_id
        for exp_id in {expansion_id_from_listing(lst) for lst in listings}
        if exp_id is not None
    ]
    catalog = client.blueprint_uid_catalog(expansion_ids)
    needed = {lst.blueprint_id for lst in listings if lst.quantity > 0}
    enrich_stats = enrich_blueprint_scryfall(catalog, needed_blueprint_ids=needed)
    blueprint_scryfall = blueprint_scryfall_map(catalog)

    by_key, flags, missing_scryfall, sentinel_copies = aggregate_ct_for_manabox_export(
        listings,
        blueprint_scryfall,
        expansion_codes,
        ignore_condition=ignore_condition,
    )
    rows = rows_from_ct_aggregate(by_key, flags, ignore_condition=ignore_condition)
    summary = {
        "ct_listings": len(listings),
        "ct_copies": sum(lst.quantity for lst in listings),
        "export_unique_keys": len(rows),
        "export_copies": sum(r.quantity for r in rows),
        "ct_missing_scryfall_copies": missing_scryfall,
        "ct_sentinel_copies_excl_from_value": sentinel_copies,
        "scryfall_enrich_needed": enrich_stats.needed,
        "scryfall_enrich_via_tcgplayer": enrich_stats.via_tcgplayer,
        "scryfall_enrich_via_cardmarket": enrich_stats.via_cardmarket,
        "scryfall_enrich_still_missing": enrich_stats.still_missing,
    }
    return ManaboxStockExport(
        fetched_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        rows=rows,
        summary=summary,
    )


def write_manabox_stock_csv(path: Path, rows: list[ManaboxStockRow]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=MANABOX_CSV_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "Name": row.name,
                    "Set code": row.set_code,
                    "Collector number": row.collector_number,
                    "Foil": foil_to_manabox(row.foil),
                    "Quantity": row.quantity,
                    "Condition": row.condition_manabox,
                    "Language": row.language,
                    "Scryfall ID": row.scryfall_id,
                    "Altered": "true" if row.altered else "false",
                    "Misprint": "true" if row.misprint else "false",
                }
            )


def export_to_dict(export: ManaboxStockExport) -> dict[str, Any]:
    return {
        "fetched_at": export.fetched_at,
        "summary": export.summary,
        "rows": [asdict(r) for r in export.rows],
    }
