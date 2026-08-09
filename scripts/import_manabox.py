#!/usr/bin/env python3
"""Import a ManaBox CSV into CardTrader inventory (create listings).

Safety:
  - DRY-RUN by default (writes a plan under artifacts/)
  - LIVE requires --confirm-live
  - Resolves CT blueprint via Scryfall ID only (TCGPlayer/Cardmarket UID enrich)
  - Maps ManaBox EU condition → CT US condition (excellent→Slightly Played)
  - Sets mtg_foil / mtg_language explicitly with error_mode=strict
  - Price defaults to sentinel maximum (€9999.99)
  - Skips rows already present in CT (same blueprint+foil+language+condition)

Examples:
  python scripts/import_manabox.py "C:\\Users\\you\\Downloads\\to_add.csv"
  python scripts/import_manabox.py .\\to_add.csv --confirm-live
  python scripts/import_manabox.py .\\to_add.csv --limit 5 --confirm-live

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

from cardtrader_inventory.config import (
    SENTINEL_PRICE_CENTS,
    ConfigError,
    PricingPolicy,
    load_api_token,
)
from cardtrader_inventory.ct_client import CardTraderClient, CardTraderError
from cardtrader_inventory.manabox_import import (
    apply_import_plan,
    plan_from_csv,
    plan_to_dict,
)
from cardtrader_inventory.rate_limiter import RateLimiter


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Import ManaBox CSV into CardTrader inventory "
            "(scryfall→blueprint, EU→CT condition, foil, sentinel max price)"
        )
    )
    parser.add_argument("csv", type=Path, help="ManaBox CSV to import")
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=_ROOT / "artifacts",
        help="Directory for plan/result JSON (default: artifacts/)",
    )
    parser.add_argument(
        "--confirm-live",
        action="store_true",
        help="Actually call bulk_create (otherwise DRY-RUN plan only)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Only create the first N plan rows (after planning; 0=all)",
    )
    parser.add_argument(
        "--price-cents",
        type=int,
        default=SENTINEL_PRICE_CENTS,
        help=f"List price in cents (default sentinel {SENTINEL_PRICE_CENTS})",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser.parse_args(argv)


def _print_plan(plan) -> None:
    s = plan.summary
    print(f"price: €{s['price_eur']:.2f} ({s['price_cents']} cents)")
    print(
        f"csv_rows={s['csv_rows']} create={s['create']} "
        f"skip_existing={s['skip_existing']} skip_error={s['skip_error']} "
        f"create_copies={s['create_copies']}"
    )
    creates = [r for r in plan.rows if r.action == "create"]
    errors = [r for r in plan.rows if r.action == "skip_error"]
    if creates:
        print("\nWill create (sample):")
        for row in creates[:20]:
            foil = "foil" if row.foil else "-"
            print(
                f"  L{row.line_no} {row.name[:36]:36} {row.set_code:<6} "
                f"cn={row.collector_number:<6} {foil:<4} {row.condition_ct:<18} "
                f"x{row.quantity} bp={row.blueprint_id}"
            )
        if len(creates) > 20:
            print(f"  ... {len(creates) - 20} more")
    if errors:
        print("\nErrors / unresolved (sample):")
        for row in errors[:20]:
            print(
                f"  L{row.line_no} {row.name[:36]:36} {row.set_code:<6} "
                f"{row.reason}"
            )
        if len(errors) > 20:
            print(f"  ... {len(errors) - 20} more")


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
    if args.price_cents <= 0:
        print("--price-cents must be positive", file=sys.stderr)
        return 2

    try:
        token = load_api_token()
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    policy = PricingPolicy.from_env()
    client = CardTraderClient(token, policy, limiter=RateLimiter(policy.marketplace_rps))

    try:
        plan = plan_from_csv(client, csv_path, price_cents=args.price_cents)
    except CardTraderError as exc:
        print(f"CardTrader API error while planning: {exc}", file=sys.stderr)
        return 1

    if args.limit and args.limit > 0:
        kept = 0
        for row in plan.rows:
            if row.action != "create":
                continue
            kept += 1
            if kept > args.limit:
                row.action = "skip_error"
                row.reason = "limited_out"
        plan.summary["create"] = sum(1 for r in plan.rows if r.action == "create")
        plan.summary["create_copies"] = sum(
            r.quantity for r in plan.rows if r.action == "create"
        )
        plan.summary["limit"] = args.limit

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = args.out_dir.expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    plan_path = out_dir / f"manabox-import-{stamp}-plan.json"
    plan_path.write_text(json.dumps(plan_to_dict(plan), indent=2), encoding="utf-8")

    mode = "LIVE" if args.confirm_live else "DRY_RUN"
    print(f"mode: {mode}")
    print(f"csv:  {csv_path}")
    _print_plan(plan)
    print(f"\nWrote plan {plan_path}")

    if not args.confirm_live:
        print("DRY-RUN only. Re-run with --confirm-live to create listings.")
        return 0

    if plan.summary["create"] == 0:
        print("Nothing to create.")
        return 0

    if plan.summary["skip_error"]:
        print(
            f"Refusing LIVE create while {plan.summary['skip_error']} rows have "
            f"resolution errors. Fix the CSV/plan or remove bad rows.",
            file=sys.stderr,
        )
        return 2

    try:
        result = apply_import_plan(
            client,
            plan,
            batch_size=policy.bulk_update_batch_size,
        )
    except (CardTraderError, RuntimeError) as exc:
        print(f"LIVE import failed: {exc}", file=sys.stderr)
        return 1

    result_path = out_dir / f"manabox-import-{stamp}-result.json"
    result_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(f"LIVE create done: {result}")
    print(f"Wrote {result_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
