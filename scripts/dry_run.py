#!/usr/bin/env python3
"""Phase 1: run the DRY_RUN pricing pipeline (no CardTrader price mutations).

Usage:
  $env:CARDTRADER_JWT = "..."
  python scripts/dry_run.py
  python scripts/dry_run.py --out-dir artifacts
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from cardtrader_inventory.config import ConfigError, PricingPolicy
from cardtrader_inventory.pipeline import run_dry_run, write_plan_artifacts
from cardtrader_inventory.report import format_kpi_table
from cardtrader_inventory.stages import StageError


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="CardTrader DRY_RUN pricing pipeline")
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=_ROOT / "artifacts",
        help="Directory for summary JSON + plan JSONL (default: ./artifacts)",
    )
    parser.add_argument(
        "--sample-size",
        type=int,
        default=25,
        help="How many proposed updates to print/embed in summary",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Debug logging",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        policy = PricingPolicy.from_env()
        result = run_dry_run(policy=policy, sample_size=args.sample_size)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        # Workers are non-daemon; a normal exit can still hang on in-flight urllib.
        os._exit(130)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except StageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001
        logging.exception("DRY_RUN failed")
        print(f"error: {exc}", file=sys.stderr)
        return 1

    summary_path, plan_path, kpi_path, csv_path = write_plan_artifacts(
        result, args.out_dir
    )
    s = result.plan.summary
    print("=== DRY_RUN complete (no CT mutations) ===")
    print(f"pricing_run_id:     {result.pricing_run_id}")
    print(f"listings:           {result.listing_count}")
    print(f"priceable:          {result.priceable_count}")
    print(f"cards_processed:    {s.cards_processed}")
    print(f"proposed_updates:   {s.price_updates_proposed}")
    print(f"skip_insuff_comps:  {s.skipped_insufficient_comps}")
    print(f"skip_wide_spread:   {s.skipped_wide_spread}")
    print(f"skip_dead_band:     {s.skipped_dead_band}")
    print(f"no_change:          {s.no_change}")
    print(f"safety_ok:          {result.safety.ok}")
    if result.safety.errors:
        for err in result.safety.errors:
            print(f"safety_error:       {err}")
    print()
    print(format_kpi_table(result.kpis))
    print()
    print(f"summary:            {summary_path}")
    print(f"plan:               {plan_path}")
    print(f"kpis:               {kpi_path}")
    print(f"changes_csv:        {csv_path}")
    if result.sample_updates:
        print("\nSample proposed updates (unit price EUR, CardTrader cents÷100):")
        for row in result.sample_updates[: args.sample_size]:
            prev = row["previous_cents"] / 100.0
            new = (row["proposed_cents"] or 0) / 100.0
            market = (row["market_cents"] or 0) / 100.0
            qty = row.get("quantity", 1)
            reason = row.get("reason", "")
            print(
                f"  #{row['listing_id']} {row['name_en']!r} x{qty}: "
                f"€{prev:.2f} → €{new:.2f} "
                f"(market €{market:.2f}) {reason}"
            )

    return 0 if result.safety.ok else 3


if __name__ == "__main__":
    raise SystemExit(main())
