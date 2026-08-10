"""CardTrader per-item buyer fees (Zero warehouse / Safeguard schedule).

Source: https://static.cardtrader.com/en/pages/payments-fees-and-refunds.html
"Purchase — CardTrader Safeguard Fees on each item" (EUR, VAT included).

Marketplace offer prices in this project are treated as buyer-facing totals
(list + fee). Sellers set / we propose the list price; CT adds the fee later
(shown as (+€X.XX) in the UI).
"""

from __future__ import annotations

# (min_list_cents inclusive, max_list_cents inclusive or None, fee_cents)
BUYER_FEE_TIERS: tuple[tuple[int, int | None, int], ...] = (
    (1, 25, 9),
    (26, 300, 10),
    (301, 500, 11),
    (501, 700, 14),
    (701, 1000, 15),
    (1001, 1500, 21),
    (1501, 2000, 27),
    (2001, 3000, 40),
    (3001, 4000, 52),
    (4001, None, 64),
)


def buyer_fee_cents(list_price_cents: int) -> int:
    """Fee CT adds on top of the seller list price for one item."""
    if list_price_cents < 1:
        return BUYER_FEE_TIERS[0][2]
    for lo, hi, fee in BUYER_FEE_TIERS:
        if list_price_cents < lo:
            continue
        if hi is None or list_price_cents <= hi:
            return fee
    return BUYER_FEE_TIERS[-1][2]


def buyer_total_cents(list_price_cents: int) -> int:
    return list_price_cents + buyer_fee_cents(list_price_cents)


def list_price_for_buyer_total(target_buyer_total_cents: int) -> int:
    """Largest list price P ≥ 1 with P + fee(P) ≤ target_buyer_total_cents."""
    if target_buyer_total_cents < 1:
        return 1

    best = 1
    for lo, hi, fee in BUYER_FEE_TIERS:
        max_p = target_buyer_total_cents - fee
        if max_p < lo:
            continue
        upper = max_p if hi is None else min(max_p, hi)
        if upper >= lo:
            best = max(best, upper)
    return best


def list_from_market_buyer_total(
    market_buyer_total_cents: int,
    *,
    undercut_cents: int = 1,
) -> tuple[int, int, int]:
    """Strip fee from a marketplace (buyer-facing) price and undercut.

    Returns (list_price, fee_on_that_list, implied_buyer_total).
    """
    undercut = max(0, int(undercut_cents))
    target_buyer = max(1, market_buyer_total_cents - undercut)
    list_cents = list_price_for_buyer_total(target_buyer)
    fee = buyer_fee_cents(list_cents)
    return list_cents, fee, list_cents + fee
