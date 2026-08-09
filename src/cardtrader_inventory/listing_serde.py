"""Serialize / deserialize Listing rows for S3 listings.jsonl (no raw payload)."""

from __future__ import annotations

import json
from typing import Any

from cardtrader_inventory.models import Listing


def listing_to_dict(listing: Listing) -> dict[str, Any]:
    return {
        "id": listing.id,
        "blueprint_id": listing.blueprint_id,
        "quantity": listing.quantity,
        "price_cents": listing.price_cents,
        "condition": listing.condition,
        "language": listing.language,
        "foil": listing.foil,
        "game_id": listing.game_id,
        "user_id": listing.user_id,
        "name_en": listing.name_en,
        "rarity": listing.rarity,
    }


def listing_from_dict(raw: dict[str, Any]) -> Listing:
    return Listing(
        id=int(raw["id"]),
        blueprint_id=int(raw["blueprint_id"]),
        quantity=int(raw.get("quantity") or 1),
        price_cents=int(raw["price_cents"]),
        condition=str(raw.get("condition") or ""),
        language=str(raw.get("language") or ""),
        foil=bool(raw.get("foil")),
        game_id=int(raw.get("game_id") or 0),
        user_id=(int(raw["user_id"]) if raw.get("user_id") is not None else None),
        name_en=str(raw.get("name_en") or ""),
        rarity=str(raw.get("rarity") or ""),
        raw={},
    )


def listings_to_jsonl_bytes(listings: list[Listing]) -> bytes:
    lines = [json.dumps(listing_to_dict(lst)) for lst in listings]
    return ("\n".join(lines) + ("\n" if lines else "")).encode("utf-8")


def listings_from_jsonl_text(text: str) -> list[Listing]:
    out: list[Listing] = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON on listings line {line_no}: {exc}") from exc
        out.append(listing_from_dict(raw))
    return out
