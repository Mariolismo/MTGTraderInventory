"""Shared configuration for CardTrader inventory automation."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Literal, Mapping

API_BASE_URL = "https://api.cardtrader.com/api/v2"

# Frozen from Phase 0 probe (docs/rate-probe-results.md).
MARKETPLACE_SAFE_RPS = 6.0

SENTINEL_PRICE_CENTS = 999_999

InsufficientCompsFallback = Literal["use_lowest", "keep_current", "skip"]

_TOKEN_ENV_NAMES = ("CARDTRADER_JWT", "CARDTRADER_API_TOKEN")

# Defaults match CardTrader "Default Magic Minimum Prices" (euros → cents).
# Keys are normalized rarity (+ foil_ prefix when the UI has a foil row).
DEFAULT_RARITY_FLOOR_CENTS: dict[str, int] = {
    "other": 5,
    "basic_land": 5,
    "foil_basic_land": 10,
    "token": 5,
    "foil_token": 10,
    "common": 5,
    "foil_common": 5,
    "uncommon": 10,
    "foil_uncommon": 10,
    "rare": 20,
    "foil_rare": 20,
    "mythic": 50,
    "foil_mythic": 50,
    "masterpiece": 50,
    "special": 100,
}

# Rarities that have a distinct foil floor in the CT UI.
_FOIL_FLOOR_RARITIES = frozenset(
    {"basic_land", "token", "common", "uncommon", "rare", "mythic"}
)


class ConfigError(ValueError):
    """Raised when required configuration is missing or invalid."""


def load_api_token() -> str:
    """Load the CardTrader API Bearer token from the environment."""
    for name in _TOKEN_ENV_NAMES:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    joined = " or ".join(_TOKEN_ENV_NAMES)
    raise ConfigError(
        f"Missing CardTrader API token. Set {joined} in the environment."
    )


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return float(raw)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


def _env_str(name: str, default: str) -> str:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip()


def normalize_rarity(value: str) -> str:
    """Map CT mtg_rarity strings to floor table keys (non-foil)."""
    normalized = " ".join(value.strip().lower().split())
    if not normalized:
        return "other"
    aliases = {
        "other": "other",
        "basic land": "basic_land",
        "basic_land": "basic_land",
        "land": "basic_land",
        "token": "token",
        "common": "common",
        "uncommon": "uncommon",
        "rare": "rare",
        "mythic": "mythic",
        "mythic rare": "mythic",
        "mythic_rare": "mythic",
        "masterpiece": "masterpiece",
        "special": "special",
    }
    return aliases.get(normalized, "other")


def rarity_floor_key(rarity: str, *, foil: bool) -> str:
    """Policy key for rarity (+ foil) minimum price lookup."""
    base = normalize_rarity(rarity)
    if foil and base in _FOIL_FLOOR_RARITIES:
        return f"foil_{base}"
    return base


def merge_rarity_floors(
    overrides: Mapping[str, int] | None = None,
    *,
    other_cents: int | None = None,
) -> dict[str, int]:
    """Defaults + optional overrides; `other` drives unknown/missing rarity."""
    floors = dict(DEFAULT_RARITY_FLOOR_CENTS)
    if overrides:
        for raw_key, cents in overrides.items():
            key = str(raw_key).strip().lower().replace(" ", "_").replace("-", "_")
            floors[key] = int(cents)
    if other_cents is not None:
        floors["other"] = int(other_cents)
    return floors


def _env_rarity_floors(other_cents: int) -> dict[str, int]:
    """Parse optional RARITY_FLOOR_CENTS_JSON object and apply MINIMUM_FLOOR_CENTS as other."""
    raw = os.environ.get("RARITY_FLOOR_CENTS_JSON", "").strip()
    overrides: dict[str, int] = {}
    if raw:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ConfigError(
                f"RARITY_FLOOR_CENTS_JSON must be valid JSON object: {exc}"
            ) from exc
        if not isinstance(parsed, dict):
            raise ConfigError("RARITY_FLOOR_CENTS_JSON must be a JSON object")
        for key, value in parsed.items():
            try:
                overrides[str(key)] = int(value)
            except (TypeError, ValueError) as exc:
                raise ConfigError(
                    f"RARITY_FLOOR_CENTS_JSON[{key!r}] must be an integer cents value"
                ) from exc
    return merge_rarity_floors(overrides, other_cents=other_cents)


@dataclass(frozen=True)
class PricingPolicy:
    """Read-only pricing / safety policy (not inventory state)."""

    marketplace_rps: float = MARKETPLACE_SAFE_RPS
    nth_lowest: int = 3
    min_comparable_offers: int = 3
    insufficient_comps_fallback: InsufficientCompsFallback = "skip"
    max_decrease_pct: float = 1.5
    max_increase_pct: float = 30.0  # unused: upside is not clamped
    # Fallback / "Other" floor when rarity is missing or unknown.
    minimum_floor_cents: int = 5
    rarity_floor_cents: Mapping[str, int] = field(
        default_factory=lambda: dict(DEFAULT_RARITY_FLOOR_CENTS)
    )
    sentinel_price_cents: int = SENTINEL_PRICE_CENTS
    # Export validation bounds — widen for first dry-runs; tighten after baseline.
    min_expected_listings: int = 1
    max_expected_listings: int = 50_000
    # Plan safety: large update volume is OK; fail on steep non-sentinel drops.
    max_proposed_pct: float = 100.0
    max_proposed_absolute: int = 50_000
    max_allowed_decrease_pct: float = 5.0
    # MTG singles game id on CardTrader.
    game_id: int = 1
    generate_chunk_size: int = 2_000
    export_timeout_s: float = 180.0
    request_timeout_s: float = 30.0
    marketplace_max_workers: int = 16
    # Only use CardTrader Zero (hub) offers for market comps.
    ct_zero_only: bool = True
    # Market = median of cheapest N; if only 3–4 comps use 3rd-lowest.
    market_median_window: int = 5
    # Skip if (max-min)/min among market window exceeds this % — but only when
    # the window's cheapest offer is above comp_spread_min_price_cents (bulk
    # variance as a ratio is noise below that).
    max_comp_spread_pct: float = 100.0
    comp_spread_min_price_cents: int = 500  # €5; ratio gate only if min > this
    # Dead band: |Δ| must be >= max(min_change_cents, min_change_pct of previous).
    # Default off — 1¢ undercuts matter; set via env if API churn becomes noisy.
    min_change_cents: int = 0
    min_change_pct: float = 0.0
    # Marketplace comps are buyer-facing (fee included). Target is the seller
    # list price after stripping fee and undercutting buyer total by this many
    # cents. 0 = strip fee only (match market buyer total).
    buyer_total_undercut_cents: int = 1
    # LIVE apply: products per bulk_update call; job wait timeout.
    bulk_update_batch_size: int = 100
    bulk_job_timeout_s: float = 300.0
    # Transient CT HTTP: retries after the first try (GET/HEAD: 429/502/503/504 +
    # network; POST mutations: 429 only). Full jitter, capped max delay.
    ct_http_max_retries: int = 3
    ct_http_retry_base_s: float = 1.0
    ct_http_retry_max_s: float = 30.0

    def floor_cents_for(self, *, rarity: str, foil: bool) -> tuple[int, str]:
        """Return (floor_cents, policy_key) for a listing's rarity + foil."""
        key = rarity_floor_key(rarity, foil=foil)
        if key in self.rarity_floor_cents:
            return int(self.rarity_floor_cents[key]), key
        # Foil key missing → non-foil rarity → other / global minimum.
        base = normalize_rarity(rarity)
        if base in self.rarity_floor_cents:
            return int(self.rarity_floor_cents[base]), base
        return int(self.minimum_floor_cents), "other"

    @classmethod
    def from_env(cls) -> PricingPolicy:
        fallback = _env_str("INSUFFICIENT_COMPS_FALLBACK", "skip")
        if fallback not in ("use_lowest", "keep_current", "skip"):
            raise ConfigError(
                "INSUFFICIENT_COMPS_FALLBACK must be one of: "
                "use_lowest, keep_current, skip"
            )
        minimum_floor_cents = _env_int("MINIMUM_FLOOR_CENTS", 5)
        return cls(
            marketplace_rps=_env_float("MARKETPLACE_SAFE_RPS", MARKETPLACE_SAFE_RPS),
            nth_lowest=_env_int("NTH_LOWEST", 3),
            min_comparable_offers=_env_int("MIN_COMPARABLE_OFFERS", 3),
            insufficient_comps_fallback=fallback,  # type: ignore[arg-type]
            max_decrease_pct=_env_float("MAX_DECREASE_PCT", 1.5),
            max_increase_pct=_env_float("MAX_INCREASE_PCT", 30.0),
            minimum_floor_cents=minimum_floor_cents,
            rarity_floor_cents=_env_rarity_floors(minimum_floor_cents),
            sentinel_price_cents=_env_int("SENTINEL_PRICE_CENTS", SENTINEL_PRICE_CENTS),
            min_expected_listings=_env_int("MIN_EXPECTED_LISTINGS", 1),
            max_expected_listings=_env_int("MAX_EXPECTED_LISTINGS", 50_000),
            max_proposed_pct=_env_float("MAX_PROPOSED_PCT", 100.0),
            max_proposed_absolute=_env_int("MAX_PROPOSED_ABSOLUTE", 50_000),
            max_allowed_decrease_pct=_env_float("MAX_ALLOWED_DECREASE_PCT", 5.0),
            game_id=_env_int("CT_GAME_ID", 1),
            generate_chunk_size=_env_int("GENERATE_CHUNK_SIZE", 2_000),
            export_timeout_s=_env_float("EXPORT_TIMEOUT_S", 180.0),
            request_timeout_s=_env_float("REQUEST_TIMEOUT_S", 30.0),
            marketplace_max_workers=_env_int("MARKETPLACE_MAX_WORKERS", 16),
            ct_zero_only=_env_str("CT_ZERO_ONLY", "true").lower()
            in {"1", "true", "yes", "y"},
            market_median_window=_env_int("MARKET_MEDIAN_WINDOW", 5),
            max_comp_spread_pct=_env_float("MAX_COMP_SPREAD_PCT", 100.0),
            comp_spread_min_price_cents=_env_int("COMP_SPREAD_MIN_PRICE_CENTS", 500),
            min_change_cents=_env_int("MIN_CHANGE_CENTS", 0),
            min_change_pct=_env_float("MIN_CHANGE_PCT", 0.0),
            buyer_total_undercut_cents=_env_int("BUYER_TOTAL_UNDERCUT_CENTS", 1),
            bulk_update_batch_size=_env_int("BULK_UPDATE_BATCH_SIZE", 100),
            bulk_job_timeout_s=_env_float("BULK_JOB_TIMEOUT_S", 300.0),
            ct_http_max_retries=_env_int("CT_HTTP_MAX_RETRIES", 3),
            ct_http_retry_base_s=_env_float("CT_HTTP_RETRY_BASE_S", 1.0),
            ct_http_retry_max_s=_env_float("CT_HTTP_RETRY_MAX_S", 30.0),
        )
