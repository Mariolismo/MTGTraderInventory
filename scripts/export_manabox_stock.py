#!/usr/bin/env python3
"""Export live CardTrader inventory as a ManaBox CSV for binder replace.

Weekly ship-day flow (single for-sale binder):
  1. Run this script → import-ready CSV from live CardTrader stock.
  2. Delete the old for-sale binder in ManaBox.
  3. Import the new CSV.

Optional diff before replace:
  python scripts/compare_manabox.py path\\to\\old-binder-export.csv

Examples:
  python scripts/export_manabox_stock.py
  python scripts/export_manabox_stock.py --out-dir artifacts -v

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
from cardtrader_inventory.manabox_export import (
    export_ct_to_manabox_stock,
    export_to_dict,
    write_manabox_stock_csv,
)
from cardtrader_inventory.rate_limiter import RateLimiter


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build a ManaBox-import CSV from live CardTrader inventory "
            "(delete old for-sale binder, then import the output file)."
        )
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=_ROOT / "artifacts",
        help="Directory for CSV/JSON reports (default: artifacts/)",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Debug logging",
    )
    return parser.parse_args(argv)


def _print_export_summary(export) -> None:
    s = export.summary
    print("ManaBox stock export (from CardTrader)")
    print(f"CT snapshot: {s['ct_listings']} listings, {s['ct_copies']} copies")
    print(f"Export CSV:  {s['export_unique_keys']} rows, {s['export_copies']} copies")
    if s.get("ct_missing_scryfall_copies"):
        print(
            f"warning: {s['ct_missing_scryfall_copies']} CT copies skipped "
            "(no Scryfall ID — not included in CSV)"
        )
    if s.get("scryfall_enrich_still_missing"):
        print(
            f"warning: scryfall enrich still_missing={s['scryfall_enrich_still_missing']}"
        )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )

    try:
        token = load_api_token()
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    policy = PricingPolicy.from_env()
    client = CardTraderClient(token, policy, limiter=RateLimiter(policy.marketplace_rps))

    try:
        export = export_ct_to_manabox_stock(client)
    except CardTraderError as exc:
        print(f"CardTrader API error: {exc}", file=sys.stderr)
        return 1

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = args.out_dir.expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / f"manabox-stock-{stamp}.csv"
    json_path = out_dir / f"manabox-stock-{stamp}.json"

    write_manabox_stock_csv(csv_path, export.rows)
    json_path.write_text(json.dumps(export_to_dict(export), indent=2), encoding="utf-8")

    _print_export_summary(export)
    print(f"\nWrote {csv_path}")
    print(f"Wrote {json_path}")
    print("\nNext: delete your for-sale binder in ManaBox, then import the stock CSV.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
