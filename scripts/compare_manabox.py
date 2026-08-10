#!/usr/bin/env python3
"""Compare a ManaBox CSV to live CardTrader inventory.

Match key (default):
  scryfall_id + foil + language + condition
  (ManaBox EU grades mapped to CardTrader US grades)

Scryfall IDs for CT listings come from GET /blueprints/export?expansion_id=…

Examples:
  python scripts/compare_manabox.py "C:\\Users\\you\\Downloads\\CardTrader.csv"
  python scripts/compare_manabox.py .\\export.csv --out-dir artifacts -v
  python scripts/compare_manabox.py .\\export.csv --ignore-condition

Requires CARDTRADER_JWT (or CARDTRADER_API_TOKEN).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from cardtrader_inventory.config import ConfigError, PricingPolicy, load_api_token
from cardtrader_inventory.ct_client import CardTraderClient, CardTraderError
from cardtrader_inventory.manabox_compare import (
    compare_manabox_to_ct,
    result_to_dict,
    write_delta_csv,
)
from cardtrader_inventory.rate_limiter import RateLimiter


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Compare ManaBox CSV vs live CardTrader export "
            "(scryfall_id + foil + language + condition)"
        )
    )
    parser.add_argument(
        "csv",
        type=Path,
        help="Path to ManaBox CSV export",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=_ROOT / "artifacts",
        help="Directory for JSON/CSV reports (default: artifacts/)",
    )
    parser.add_argument(
        "--ignore-condition",
        action="store_true",
        help=(
            "Omit condition from the match key. By default ManaBox EU grades "
            "are converted to CardTrader US grades (excellent↔SP, etc.)."
        ),
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Debug logging",
    )
    return parser.parse_args(argv)


def _print_summary(result) -> None:
    s = result.summary
    print(f"match_key: {result.match_key}")
    print(f"csv: {result.csv_path}")
    print(
        f"CSV keys={s['csv_unique_keys']} copies={s['csv_copies']} | "
        f"CT keys={s['ct_unique_keys']} copies={s['ct_copies']}"
    )
    print(
        f"matched={s['matched_keys']} exact_qty={s['qty_exact_match']} "
        f"qty_mismatch={s['qty_mismatch']} "
        f"only_csv={s['only_in_csv']} only_ct={s['only_on_ct']}"
    )
    if s.get("ct_value_eur_excl_sentinel") is not None:
        print(
            f"CT value (excl sentinel): €{s['ct_value_eur_excl_sentinel']:.2f} "
            f"(sentinel copies excl={s.get('ct_sentinel_copies_excl_from_value', 0)})"
        )
    if s.get("csv_missing_scryfall_copies") or s.get("ct_missing_scryfall_copies"):
        print(
            f"warning: missing scryfall copies "
            f"csv={s.get('csv_missing_scryfall_copies', 0)} "
            f"ct={s.get('ct_missing_scryfall_copies', 0)}"
        )
    if s.get("scryfall_enrich_needed"):
        print(
            f"scryfall enrich: needed={s.get('scryfall_enrich_needed')} "
            f"tcgplayer={s.get('scryfall_enrich_via_tcgplayer')} "
            f"cardmarket={s.get('scryfall_enrich_via_cardmarket')} "
            f"still_missing={s.get('scryfall_enrich_still_missing')}"
        )

    def _line(row, *, show_delta: bool) -> str:
        foil = "foil" if row.foil else "-"
        base = (
            f"  {row.name[:36]:36} {row.set_code:<6} cn={row.collector_number:<6} "
            f"{foil:<4} csv={row.csv_qty} ct={row.ct_qty}"
        )
        if show_delta:
            return f"{base} delta={row.delta:+d}"
        return base

    if result.qty_mismatch:
        print("\nQty mismatches (CT - CSV):")
        for row in result.qty_mismatch[:25]:
            print(_line(row, show_delta=True))
        if len(result.qty_mismatch) > 25:
            print(f"  ... {len(result.qty_mismatch) - 25} more")

    if result.only_in_csv:
        print("\nOnly in CSV (top by qty):")
        for row in result.only_in_csv[:15]:
            print(_line(row, show_delta=False))
        if len(result.only_in_csv) > 15:
            print(f"  ... {len(result.only_in_csv) - 15} more")

    if result.only_on_ct:
        print("\nOnly on CardTrader (top by qty):")
        for row in result.only_on_ct[:15]:
            print(_line(row, show_delta=False))
        if len(result.only_on_ct) > 15:
            print(f"  ... {len(result.only_on_ct) - 15} more")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )

    csv_path = args.csv.expanduser().resolve()
    if not csv_path.is_file():
        print(f"CSV not found: {csv_path}", file=sys.stderr)
        return 2

    try:
        token = load_api_token()
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    policy = PricingPolicy.from_env()
    client = CardTraderClient(token, policy, limiter=RateLimiter(policy.marketplace_rps))

    try:
        result = compare_manabox_to_ct(
            client,
            csv_path,
            ignore_condition=args.ignore_condition,
        )
    except CardTraderError as exc:
        print(f"CardTrader API error: {exc}", file=sys.stderr)
        return 1

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = args.out_dir.expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / f"manabox-compare-{stamp}.json"
    delta_path = out_dir / f"manabox-compare-{stamp}-deltas.csv"

    payload = result_to_dict(result)
    json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    write_delta_csv(
        delta_path,
        result.qty_mismatch + result.only_in_csv + result.only_on_ct,
    )

    _print_summary(result)
    print(f"\nWrote {json_path}")
    print(f"Wrote {delta_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
