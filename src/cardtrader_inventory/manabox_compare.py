"""Compare a ManaBox CSV export to a live CardTrader inventory snapshot.

Primary join key is Scryfall ID (ManaBox column + CT blueprints/export).
When CT omits scryfall_id, enrich via other UIDs only (TCGPlayer / Cardmarket)
through Scryfall's cross-reference endpoints — never name/set heuristics.
"""

from __future__ import annotations

import csv
import re
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from cardtrader_inventory.ct_client import CardTraderClient
from cardtrader_inventory.models import Listing

# ManaBox uses Cardmarket (EU) grades; CardTrader uses US-style grades.
# Canonical match values are CT labels (lowercase).
# Source: https://mtg.wiki/page/Grading_condition (Cardmarket ↔ US table)
_COND_TO_CT: dict[str, str] = {
    "mint": "near mint",
    "near_mint": "near mint",
    "nm": "near mint",
    "excellent": "slightly played",
    "ex": "slightly played",
    "good": "moderately played",
    "gd": "moderately played",
    "light_played": "played",
    "lightly_played": "played",
    "lp": "played",
    "played": "played",
    "pl": "played",
    "poor": "damaged",
    "near mint": "near mint",
    "slightly_played": "slightly played",
    "slightly played": "slightly played",
    "sp": "slightly played",
    "moderately_played": "moderately played",
    "moderately played": "moderately played",
    "mp": "moderately played",
    "heavily_played": "played",
    "heavily played": "played",
    "hp": "played",
    "damaged": "damaged",
    "dmg": "damaged",
    "poor_damaged": "damaged",
    "poor / damaged": "damaged",
}

_LANG_MAP = {
    "en": "en",
    "english": "en",
    "jp": "jp",
    "ja": "jp",
    "japanese": "jp",
    "de": "de",
    "german": "de",
    "fr": "fr",
    "french": "fr",
    "it": "it",
    "italian": "it",
    "es": "es",
    "spanish": "es",
    "pt": "pt",
    "portuguese": "pt",
    "zh-cn": "zh",
    "zh": "zh",
    "chinese": "zh",
    "ko": "ko",
    "korean": "ko",
    "ru": "ru",
    "russian": "ru",
}

# (scryfall_id, foil, language, condition)
MatchKey = tuple[str, bool, str, str]


def normalize_scryfall_id(value: str) -> str:
    return (value or "").strip().lower()


def normalize_collector_number(value: str) -> str:
    text = (value or "").strip().lower()
    match = re.match(r"^0*(\d+)([a-z]*)$", text)
    if match:
        return f"{int(match.group(1))}{match.group(2)}"
    return text.lstrip("0") or "0"


def normalize_set_code(value: str) -> str:
    return (value or "").strip().upper()


def normalize_language(value: str) -> str:
    raw = (value or "").strip().lower()
    return _LANG_MAP.get(raw, raw)


def normalize_condition(value: str) -> str:
    """Map ManaBox (EU) or CardTrader (US) condition strings to a CT-canonical grade."""
    text = (value or "").strip().lower()
    underscored = text.replace("-", "_").replace(" ", "_")
    spaced = text.replace("-", " ").replace("_", " ")
    for candidate in (underscored, spaced, text):
        if candidate in _COND_TO_CT:
            return _COND_TO_CT[candidate]
    return spaced


def foil_from_manabox(value: str) -> bool:
    return (value or "").strip().lower() in {"foil", "true", "1", "etched"}


def make_key(
    *,
    scryfall_id: str,
    foil: bool,
    language: str,
    condition: str,
    ignore_condition: bool = False,
) -> MatchKey:
    cond = "" if ignore_condition else normalize_condition(condition)
    return (
        normalize_scryfall_id(scryfall_id),
        bool(foil),
        normalize_language(language),
        cond,
    )


@dataclass
class AggregateRow:
    qty: int = 0
    display_name: str = ""
    set_code: str = ""
    collector_number: str = ""
    foil: bool = False
    language: str = ""
    scryfall_id: str = ""
    conditions: set[str] = field(default_factory=set)
    listing_ids: list[int] = field(default_factory=list)
    value_cents: int = 0


@dataclass
class DiffRow:
    name: str
    set_code: str
    collector_number: str
    foil: bool
    language: str
    scryfall_id: str
    csv_qty: int
    ct_qty: int
    delta: int
    csv_conditions: list[str]
    ct_conditions: list[str]
    listing_ids: list[int] = field(default_factory=list)


@dataclass
class CompareResult:
    fetched_at: str
    csv_path: str
    match_key: str
    ignore_condition: bool
    summary: dict[str, Any]
    qty_mismatch: list[DiffRow]
    only_in_csv: list[DiffRow]
    only_on_ct: list[DiffRow]
    csv_missing_scryfall: int = 0
    ct_missing_scryfall: int = 0


def expansion_id_from_listing(listing: Listing) -> int | None:
    expansion = listing.raw.get("expansion")
    if isinstance(expansion, dict) and expansion.get("id") is not None:
        try:
            return int(expansion["id"])
        except (TypeError, ValueError):
            return None
    if listing.raw.get("expansion_id") is not None:
        try:
            return int(listing.raw["expansion_id"])
        except (TypeError, ValueError):
            return None
    return None


def load_manabox_csv(
    path: Path,
    *,
    ignore_condition: bool = False,
) -> tuple[dict[MatchKey, AggregateRow], int]:
    by_key: dict[MatchKey, AggregateRow] = defaultdict(AggregateRow)
    missing_scryfall = 0
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            scryfall = normalize_scryfall_id(row.get("Scryfall ID") or "")
            qty = int(float(row.get("Quantity") or 0))
            if not scryfall:
                missing_scryfall += qty
                continue
            name = row.get("Name") or ""
            set_code = row.get("Set code") or ""
            cn = row.get("Collector number") or ""
            foil = foil_from_manabox(row.get("Foil") or "")
            language = row.get("Language") or "en"
            condition = row.get("Condition") or ""
            key = make_key(
                scryfall_id=scryfall,
                foil=foil,
                language=language,
                condition=condition,
                ignore_condition=ignore_condition,
            )
            agg = by_key[key]
            agg.qty += qty
            agg.display_name = name or agg.display_name
            agg.set_code = normalize_set_code(set_code)
            agg.collector_number = normalize_collector_number(cn)
            agg.foil = foil
            agg.language = normalize_language(language)
            agg.scryfall_id = scryfall
            agg.conditions.add(normalize_condition(condition))
    return dict(by_key), missing_scryfall


def aggregate_ct_listings(
    listings: Iterable[Listing],
    blueprint_scryfall: dict[int, str],
    expansion_codes: dict[int, str],
    *,
    ignore_condition: bool = False,
) -> tuple[dict[MatchKey, AggregateRow], int]:
    by_key: dict[MatchKey, AggregateRow] = defaultdict(AggregateRow)
    missing_scryfall = 0
    for listing in listings:
        scryfall = normalize_scryfall_id(blueprint_scryfall.get(listing.blueprint_id, ""))
        if not scryfall:
            missing_scryfall += listing.quantity
            continue
        props = listing.raw.get("properties_hash") or listing.raw.get("properties") or {}
        if not isinstance(props, dict):
            props = {}
        cn = str(props.get("collector_number") or "")
        exp_id = expansion_id_from_listing(listing)
        set_code = expansion_codes.get(exp_id or -1, "")
        key = make_key(
            scryfall_id=scryfall,
            foil=listing.foil,
            language=listing.language,
            condition=listing.condition,
            ignore_condition=ignore_condition,
        )
        agg = by_key[key]
        agg.qty += listing.quantity
        agg.display_name = listing.name_en or agg.display_name
        agg.set_code = set_code
        agg.collector_number = normalize_collector_number(cn)
        agg.foil = listing.foil
        agg.language = normalize_language(listing.language)
        agg.scryfall_id = scryfall
        agg.conditions.add(normalize_condition(listing.condition))
        agg.listing_ids.append(listing.id)
        agg.value_cents += listing.price_cents * listing.quantity
    return dict(by_key), missing_scryfall


def _diff_from_sides(
    key: MatchKey,
    csv_row: AggregateRow | None,
    ct_row: AggregateRow | None,
) -> DiffRow:
    name = (csv_row.display_name if csv_row else "") or (
        ct_row.display_name if ct_row else ""
    )
    set_code = (csv_row.set_code if csv_row else "") or (
        ct_row.set_code if ct_row else ""
    )
    cn = (csv_row.collector_number if csv_row else "") or (
        ct_row.collector_number if ct_row else ""
    )
    foil = csv_row.foil if csv_row else (ct_row.foil if ct_row else key[1])
    language = (csv_row.language if csv_row else "") or (
        ct_row.language if ct_row else key[2]
    )
    scryfall = key[0]
    csv_qty = csv_row.qty if csv_row else 0
    ct_qty = ct_row.qty if ct_row else 0
    return DiffRow(
        name=name,
        set_code=set_code,
        collector_number=cn,
        foil=foil,
        language=language,
        scryfall_id=scryfall,
        csv_qty=csv_qty,
        ct_qty=ct_qty,
        delta=ct_qty - csv_qty,
        csv_conditions=sorted(csv_row.conditions) if csv_row else [],
        ct_conditions=sorted(ct_row.conditions) if ct_row else [],
        listing_ids=list(ct_row.listing_ids) if ct_row else [],
    )


def compare_aggregates(
    csv_by_key: dict[MatchKey, AggregateRow],
    ct_by_key: dict[MatchKey, AggregateRow],
    *,
    csv_path: str,
    ignore_condition: bool,
    csv_missing_scryfall: int = 0,
    ct_missing_scryfall: int = 0,
) -> CompareResult:
    csv_keys = set(csv_by_key)
    ct_keys = set(ct_by_key)
    matched = csv_keys & ct_keys
    only_csv = csv_keys - ct_keys
    only_ct = ct_keys - csv_keys

    qty_match = 0
    qty_mismatch: list[DiffRow] = []
    for key in matched:
        row = _diff_from_sides(key, csv_by_key[key], ct_by_key[key])
        if row.delta == 0:
            qty_match += 1
        else:
            qty_mismatch.append(row)
    qty_mismatch.sort(
        key=lambda r: (-abs(r.delta), r.name, r.set_code, r.collector_number)
    )

    only_in_csv = [
        _diff_from_sides(key, csv_by_key[key], None)
        for key in sorted(only_csv, key=lambda k: (-csv_by_key[k].qty, k))
    ]
    only_on_ct = [
        _diff_from_sides(key, None, ct_by_key[key])
        for key in sorted(only_ct, key=lambda k: (-ct_by_key[k].qty, k))
    ]

    match_parts = ["scryfall_id", "foil", "language"]
    if ignore_condition:
        match_key = " + ".join(match_parts) + " (condition ignored)"
    else:
        match_key = " + ".join(match_parts + ["condition"]) + " [EU<->CT grade map]"

    summary = {
        "csv_unique_keys": len(csv_by_key),
        "csv_copies": sum(r.qty for r in csv_by_key.values()),
        "ct_unique_keys": len(ct_by_key),
        "ct_copies": sum(r.qty for r in ct_by_key.values()),
        "matched_keys": len(matched),
        "qty_exact_match": qty_match,
        "qty_mismatch": len(qty_mismatch),
        "only_in_csv": len(only_in_csv),
        "only_on_ct": len(only_on_ct),
        "copies_only_csv": sum(r.csv_qty for r in only_in_csv),
        "copies_only_ct": sum(r.ct_qty for r in only_on_ct),
        "copies_qty_mismatch_abs": sum(abs(r.delta) for r in qty_mismatch),
        "csv_missing_scryfall_copies": csv_missing_scryfall,
        "ct_missing_scryfall_copies": ct_missing_scryfall,
    }
    return CompareResult(
        fetched_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        csv_path=csv_path,
        match_key=match_key,
        ignore_condition=ignore_condition,
        summary=summary,
        qty_mismatch=qty_mismatch,
        only_in_csv=only_in_csv,
        only_on_ct=only_on_ct,
        csv_missing_scryfall=csv_missing_scryfall,
        ct_missing_scryfall=ct_missing_scryfall,
    )


def compare_manabox_to_ct(
    client: CardTraderClient,
    csv_path: Path,
    *,
    ignore_condition: bool = False,
) -> CompareResult:
    """Fetch live CT export, resolve scryfall via CT + Scryfall UIDs, compare CSV."""
    from cardtrader_inventory.scryfall import (
        blueprint_scryfall_map,
        enrich_blueprint_scryfall,
    )

    expansion_codes = client.list_expansions()
    listings = client.export_products()
    expansion_ids = [
        exp_id
        for exp_id in {expansion_id_from_listing(lst) for lst in listings}
        if exp_id is not None
    ]
    catalog = client.blueprint_uid_catalog(expansion_ids)
    needed = {lst.blueprint_id for lst in listings}
    enrich_stats = enrich_blueprint_scryfall(catalog, needed_blueprint_ids=needed)
    blueprint_scryfall = blueprint_scryfall_map(catalog)

    csv_by_key, csv_missing = load_manabox_csv(
        csv_path, ignore_condition=ignore_condition
    )
    ct_by_key, ct_missing = aggregate_ct_listings(
        listings,
        blueprint_scryfall,
        expansion_codes,
        ignore_condition=ignore_condition,
    )
    result = compare_aggregates(
        csv_by_key,
        ct_by_key,
        csv_path=str(csv_path),
        ignore_condition=ignore_condition,
        csv_missing_scryfall=csv_missing,
        ct_missing_scryfall=ct_missing,
    )
    result.summary["scryfall_enrich_needed"] = enrich_stats.needed
    result.summary["scryfall_enrich_via_tcgplayer"] = enrich_stats.via_tcgplayer
    result.summary["scryfall_enrich_via_cardmarket"] = enrich_stats.via_cardmarket
    result.summary["scryfall_enrich_still_missing"] = enrich_stats.still_missing
    return result


def result_to_dict(result: CompareResult) -> dict[str, Any]:
    return {
        "fetched_at": result.fetched_at,
        "csv_path": result.csv_path,
        "match_key": result.match_key,
        "ignore_condition": result.ignore_condition,
        "summary": result.summary,
        "qty_mismatch": [asdict(r) for r in result.qty_mismatch],
        "only_in_csv": [asdict(r) for r in result.only_in_csv],
        "only_on_ct": [asdict(r) for r in result.only_on_ct],
    }


def write_delta_csv(path: Path, rows: list[DiffRow]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "name",
        "set_code",
        "collector_number",
        "foil",
        "language",
        "scryfall_id",
        "csv_qty",
        "ct_qty",
        "delta",
        "csv_conditions",
        "ct_conditions",
        "listing_ids",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "name": row.name,
                    "set_code": row.set_code,
                    "collector_number": row.collector_number,
                    "foil": row.foil,
                    "language": row.language,
                    "scryfall_id": row.scryfall_id,
                    "csv_qty": row.csv_qty,
                    "ct_qty": row.ct_qty,
                    "delta": row.delta,
                    "csv_conditions": "|".join(row.csv_conditions),
                    "ct_conditions": "|".join(row.ct_conditions),
                    "listing_ids": "|".join(str(i) for i in row.listing_ids),
                }
            )
