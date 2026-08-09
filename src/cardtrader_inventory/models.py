"""Domain models for the DRY_RUN pricing pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class PlanAction(str, Enum):
    UPDATE = "update"
    SKIP = "skip"
    KEEP = "keep"


class SkipReason(str, Enum):
    INSUFFICIENT_COMPS = "insufficient_comps"
    KEEP_CURRENT_INSUFFICIENT = "keep_current_insufficient_comps"
    NO_CHANGE = "no_change"
    DEAD_BAND = "dead_band"
    WIDE_SPREAD = "wide_spread"
    MISSING_MARKET = "missing_market"
    NON_MTG = "non_mtg"
    MISSING_ATTRS = "missing_attrs"


@dataclass(frozen=True)
class Listing:
    """One CardTrader product from /products/export (snapshot only)."""

    id: int
    blueprint_id: int
    quantity: int
    price_cents: int
    condition: str
    language: str
    foil: bool
    game_id: int
    user_id: int | None
    name_en: str = ""
    rarity: str = ""
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @property
    def is_sentinel(self) -> bool:
        # Exact match or at/above threshold (architecture §4.4).
        return self.price_cents >= 999_999


@dataclass(frozen=True)
class MarketOffer:
    product_id: int
    blueprint_id: int
    price_cents: int
    condition: str
    language: str
    foil: bool
    seller_user_id: int | None
    quantity: int = 1
    ct_zero: bool = True


@dataclass
class PlanRow:
    listing_id: int
    blueprint_id: int
    previous_price_cents: int
    proposed_price_cents: int | None
    action: PlanAction
    quantity: int = 1
    skip_reason: SkipReason | None = None
    market_price_cents: int | None = None
    target_price_cents: int | None = None
    clamp_decrease: bool = False
    clamp_increase: bool = False
    sentinel_clear: bool = False
    initial_price: bool = False
    comparable_count: int = 0
    name_en: str = ""
    reason: str = ""


@dataclass
class PlanSummary:
    cards_processed: int = 0
    price_updates_proposed: int = 0
    skipped_insufficient_comps: int = 0
    kept_current_insufficient_comps: int = 0
    fallback_used_lowest: int = 0
    clamp_hit_decrease: int = 0
    clamp_hit_increase: int = 0
    sentinel_initial_priced: int = 0
    no_change: int = 0
    skipped_wide_spread: int = 0
    skipped_dead_band: int = 0
    skipped_other: int = 0


@dataclass
class PricingPlan:
    pricing_run_id: str
    mode: str
    rows: list[PlanRow]
    summary: PlanSummary


@dataclass
class ExportValidationResult:
    ok: bool
    listing_count: int
    priceable: list[Listing] = field(default_factory=list)
    excluded_missing_attrs: list[int] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass
class PlanSafetyResult:
    ok: bool
    proposed_count: int
    listing_count: int
    errors: list[str] = field(default_factory=list)


@dataclass
class ApplyBatchResult:
    batch_id: str
    job_uuid: str
    listing_ids: list[int]
    ok: int = 0
    warning: int = 0
    error: int = 0
    skipped_idempotent: bool = False


@dataclass
class ApplyResult:
    pricing_run_id: str
    mode: str
    proposed: int
    applied_ok: int = 0
    applied_warning: int = 0
    applied_error: int = 0
    aborted_stale: int = 0
    skipped_idempotent: int = 0
    batches: list[ApplyBatchResult] = field(default_factory=list)
    stale_listing_ids: list[int] = field(default_factory=list)
    error_details: list[dict[str, Any]] = field(default_factory=list)
