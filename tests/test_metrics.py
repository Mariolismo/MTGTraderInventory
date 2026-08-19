"""Tests for plan observability metrics helpers."""

from __future__ import annotations

import unittest

from cardtrader_inventory.aws.metrics import (
    count_sentinel_remaining,
    plan_observability_metrics,
)
from cardtrader_inventory.models import (
    PlanAction,
    PlanRow,
    PlanSummary,
    PricingPlan,
    SkipReason,
)


class ObservabilityMetricsTests(unittest.TestCase):
    def test_sentinel_remaining_and_metric_names(self) -> None:
        rows = [
            PlanRow(
                listing_id=1,
                blueprint_id=1,
                previous_price_cents=999_999,
                proposed_price_cents=50,
                action=PlanAction.UPDATE,
                sentinel_clear=True,
            ),
            PlanRow(
                listing_id=2,
                blueprint_id=2,
                previous_price_cents=999_999,
                proposed_price_cents=None,
                action=PlanAction.SKIP,
                skip_reason=SkipReason.WIDE_SPREAD,
            ),
            PlanRow(
                listing_id=3,
                blueprint_id=3,
                previous_price_cents=100,
                proposed_price_cents=None,
                action=PlanAction.KEEP,
                skip_reason=SkipReason.NO_CHANGE,
            ),
        ]
        plan = PricingPlan(
            pricing_run_id="t",
            mode="DRY_RUN",
            rows=rows,
            summary=PlanSummary(
                price_updates_proposed=1,
                skipped_wide_spread=1,
                skipped_insufficient_comps=0,
                sentinel_initial_priced=1,
            ),
        )
        self.assertEqual(count_sentinel_remaining(plan), 1)
        metrics = plan_observability_metrics(
            plan,
            cards_in_inventory=3,
            inventory_eur=1.5,
            safety_ok=True,
        )
        self.assertEqual(metrics["SentinelRemaining"], (1, "Count"))
        self.assertEqual(metrics["SkipWideSpread"], (1, "Count"))
        self.assertNotIn("SentinelCleared", metrics)
        self.assertNotIn("PriceUpdatesProposed", metrics)
        # 9 merge-time metrics + WeeklyDiscountPct/WeeklySalesCents from prepare = 10 total custom names.
        self.assertLessEqual(len(metrics), 8)


if __name__ == "__main__":
    unittest.main()
