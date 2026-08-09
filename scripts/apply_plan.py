#!/usr/bin/env python3
"""Apply a DRY_RUN plan.jsonl to CardTrader (LIVE mutations).

Safety:
  - Requires --confirm-live
  - Refuses if companion *-summary.json has safety_ok=false (unless --force-unsafe)
  - Re-exports inventory and aborts rows whose price/qty changed since the plan
  - Chunked bulk_update with local batch idempotency under artifacts/

Usage:
  $env:CARDTRADER_JWT = "..."
  python scripts/apply_plan.py --plan artifacts/reprice-...-plan.jsonl --confirm-live
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from cardtrader_inventory.apply import (
    apply_plan_updates,
    apply_result_to_dict,
    load_plan_jsonl,
    update_rows_from_plan,
)
from cardtrader_inventory.config import ConfigError, PricingPolicy, load_api_token
from cardtrader_inventory.ct_client import CardTraderClient, CardTraderError
from cardtrader_inventory.rate_limiter import RateLimiter
from cardtrader_inventory.stages import StageError, fetch_inventory


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Apply a pricing plan to CardTrader (LIVE price mutations)"
    )
    parser.add_argument(
        "--plan",
        type=Path,
        required=True,
        help="Path to *-plan.jsonl from a DRY_RUN",
    )
    parser.add_argument(
        "--confirm-live",
        action="store_true",
        help="Required. Acknowledge that this mutates CardTrader prices.",
    )
    parser.add_argument(
        "--force-unsafe",
        action="store_true",
        help="Apply even if companion summary has safety_ok=false",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Optional cap on number of updates to apply (0 = all)",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        help="Directory for apply report + idempotency log (default: plan parent)",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser.parse_args(argv)


def _pricing_run_id_from_plan(path: Path) -> str:
    name = path.name
    if name.endswith("-plan.jsonl"):
        return name[: -len("-plan.jsonl")]
    return path.stem


def _require_safety(plan_path: Path, *, force_unsafe: bool) -> None:
    summary_path = plan_path.with_name(
        plan_path.name.replace("-plan.jsonl", "-summary.json")
    )
    if not summary_path.exists():
        if force_unsafe:
            logging.warning("No summary at %s; continuing due to --force-unsafe", summary_path)
            return
        raise StageError(
            f"Missing companion summary {summary_path}; "
            "refusing LIVE apply without safety_ok (use --force-unsafe to override)"
        )
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    if summary.get("safety_ok") is True:
        return
    if force_unsafe:
        logging.warning(
            "safety_ok is not true in %s; continuing due to --force-unsafe",
            summary_path,
        )
        return
    raise StageError(
        f"Plan safety gate failed in {summary_path}: "
        f"{summary.get('safety_errors')}; refusing LIVE apply"
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if not args.confirm_live:
        print(
            "error: refusing to mutate prices without --confirm-live",
            file=sys.stderr,
        )
        return 2

    plan_path = args.plan.resolve()
    if not plan_path.is_file():
        print(f"error: plan not found: {plan_path}", file=sys.stderr)
        return 2

    out_dir = (args.out_dir or plan_path.parent).resolve()
    pricing_run_id = _pricing_run_id_from_plan(plan_path)

    try:
        _require_safety(plan_path, force_unsafe=args.force_unsafe)
        policy = PricingPolicy.from_env()
        token = load_api_token()
        rows = load_plan_jsonl(plan_path)
        updates = update_rows_from_plan(rows)
        if args.limit and args.limit > 0:
            updates = updates[: args.limit]

        print("=== LIVE apply (CardTrader price mutations) ===")
        print(f"pricing_run_id:  {pricing_run_id}")
        print(f"plan:            {plan_path}")
        print(f"updates:         {len(updates)}")
        print(f"batch_size:      {policy.bulk_update_batch_size}")

        client = CardTraderClient(
            token,
            policy,
            limiter=RateLimiter(policy.marketplace_rps),
        )
        listings = fetch_inventory(client)
        idem_path = out_dir / f"{pricing_run_id}-apply-batches.json"
        result = apply_plan_updates(
            client,
            pricing_run_id=pricing_run_id,
            updates=updates,
            current_listings=listings,
            policy=policy,
            idempotency_path=idem_path,
        )

        report_path = out_dir / f"{pricing_run_id}-apply.json"
        report_path.write_text(
            json.dumps(apply_result_to_dict(result), indent=2),
            encoding="utf-8",
        )

        print()
        print(f"proposed:        {result.proposed}")
        print(f"applied_ok:      {result.applied_ok}")
        print(f"applied_warning: {result.applied_warning}")
        print(f"applied_error:   {result.applied_error}")
        print(f"aborted_stale:   {result.aborted_stale}")
        print(f"idempotent_skip: {result.skipped_idempotent}")
        print(f"batches:         {len(result.batches)}")
        print(f"report:          {report_path}")
        print(f"idempotency:     {idem_path}")
        if result.error_details:
            print("\nPer-item errors (first 10):")
            for detail in result.error_details[:10]:
                print(f"  {detail}")

        if result.applied_error:
            return 1
        return 0
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        os._exit(130)
    except (ConfigError, StageError, CardTraderError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001
        logging.exception("LIVE apply failed")
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
