"""Scryfall UID lookups used to fill missing CardTrader scryfall_id values."""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

_SCRYFALL_BASE = "https://api.scryfall.com"
# Scryfall asks for ~50–100 ms between requests; stay polite.
_MIN_INTERVAL_S = 0.12


@dataclass
class BlueprintUids:
    blueprint_id: int
    scryfall_id: str | None = None
    tcg_player_id: int | None = None
    card_market_ids: list[int] = field(default_factory=list)
    scryfall_source: str | None = None  # "ct" | "tcgplayer" | "cardmarket"


@dataclass
class ScryfallEnrichStats:
    needed: int = 0
    via_tcgplayer: int = 0
    via_cardmarket: int = 0
    still_missing: int = 0


class ScryfallClient:
    """Minimal Scryfall client for cross-UID resolution only."""

    def __init__(self, *, min_interval_s: float = _MIN_INTERVAL_S) -> None:
        self._min_interval_s = min_interval_s
        self._last_request_at = 0.0
        self._tcg_cache: dict[int, str | None] = {}
        self._cm_cache: dict[int, str | None] = {}

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self._min_interval_s:
            time.sleep(self._min_interval_s - elapsed)

    def _get_json(self, path: str) -> dict[str, Any] | None:
        self._throttle()
        url = f"{_SCRYFALL_BASE}{path}"
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "cardtrader-inventory/0.1",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                self._last_request_at = time.monotonic()
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            self._last_request_at = time.monotonic()
            if exc.code == 404:
                return None
            logger.warning("Scryfall HTTP %s for %s", exc.code, path)
            return None
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            logger.warning("Scryfall error for %s: %s", path, exc)
            return None
        return payload if isinstance(payload, dict) else None

    def scryfall_id_for_tcgplayer(self, tcg_player_id: int) -> str | None:
        if tcg_player_id in self._tcg_cache:
            return self._tcg_cache[tcg_player_id]
        payload = self._get_json(f"/cards/tcgplayer/{tcg_player_id}")
        scryfall = str(payload.get("id") or "").strip().lower() if payload else ""
        value = scryfall or None
        self._tcg_cache[tcg_player_id] = value
        return value

    def scryfall_id_for_cardmarket(self, cardmarket_id: int) -> str | None:
        if cardmarket_id in self._cm_cache:
            return self._cm_cache[cardmarket_id]
        payload = self._get_json(f"/cards/cardmarket/{cardmarket_id}")
        scryfall = str(payload.get("id") or "").strip().lower() if payload else ""
        value = scryfall or None
        self._cm_cache[cardmarket_id] = value
        return value


def enrich_blueprint_scryfall(
    blueprints: dict[int, BlueprintUids],
    *,
    needed_blueprint_ids: set[int] | None = None,
    scryfall: ScryfallClient | None = None,
) -> ScryfallEnrichStats:
    """Fill missing scryfall_id using TCGPlayer / Cardmarket UIDs only."""
    client = scryfall or ScryfallClient()
    stats = ScryfallEnrichStats()
    targets = needed_blueprint_ids if needed_blueprint_ids is not None else set(blueprints)

    missing = sorted(
        bp_id
        for bp_id in targets
        if (info := blueprints.get(bp_id)) is not None and not info.scryfall_id
    )
    logger.info(
        "Scryfall UID enrich starting: %s blueprints missing scryfall_id "
        "(~%.0fs if each needs one lookup)",
        len(missing),
        len(missing) * _MIN_INTERVAL_S,
    )

    for index, bp_id in enumerate(missing, start=1):
        info = blueprints.get(bp_id)
        if info is None:
            continue
        stats.needed += 1

        if info.tcg_player_id is not None:
            resolved = client.scryfall_id_for_tcgplayer(info.tcg_player_id)
            if resolved:
                info.scryfall_id = resolved
                info.scryfall_source = "tcgplayer"
                stats.via_tcgplayer += 1
                if index == 1 or index % 50 == 0 or index == len(missing):
                    logger.info(
                        "Scryfall enrich progress %s/%s (resolved=%s)",
                        index,
                        len(missing),
                        stats.via_tcgplayer + stats.via_cardmarket,
                    )
                continue

        for cm_id in info.card_market_ids:
            resolved = client.scryfall_id_for_cardmarket(cm_id)
            if resolved:
                info.scryfall_id = resolved
                info.scryfall_source = "cardmarket"
                stats.via_cardmarket += 1
                break
        else:
            if not info.scryfall_id:
                stats.still_missing += 1

        if index == 1 or index % 50 == 0 or index == len(missing):
            logger.info(
                "Scryfall enrich progress %s/%s (resolved=%s missing=%s)",
                index,
                len(missing),
                stats.via_tcgplayer + stats.via_cardmarket,
                stats.still_missing,
            )

    logger.info(
        "Scryfall UID enrich: needed=%s via_tcgplayer=%s via_cardmarket=%s still_missing=%s",
        stats.needed,
        stats.via_tcgplayer,
        stats.via_cardmarket,
        stats.still_missing,
    )
    return stats


def blueprint_scryfall_map(blueprints: dict[int, BlueprintUids]) -> dict[int, str]:
    return {
        bp_id: info.scryfall_id
        for bp_id, info in blueprints.items()
        if info.scryfall_id
    }
