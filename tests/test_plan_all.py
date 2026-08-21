"""Unit tests for plan_all_handler wave + checkpoint behavior (mocked)."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from cardtrader_inventory.aws.dynamodb_store import PlanCheckpoint
from cardtrader_inventory.aws.handlers import plan_all_handler
from cardtrader_inventory.models import PlanSummary, PricingPlan
from cardtrader_inventory.aws.plan_orchestrate import ChunkPlanResult


class _Ctx:
    def __init__(self, remaining_ms: int) -> None:
        self._remaining_ms = remaining_ms

    def get_remaining_time_in_millis(self) -> int:
        return self._remaining_ms


def _checkpoint(**overrides: object) -> PlanCheckpoint:
    base = dict(
        pricing_run_id="run-1",
        mode="LIVE",
        next_index=0,
        chunk_count=2,
        status="pending",
        listings_key="runs/run-1/listings.jsonl",
        manifest_key="runs/run-1/chunks/manifest.json",
        prefix="runs/run-1",
        owner_user_id=1,
        discount_pct=0,
        listing_count=10,
        priceable_count=10,
    )
    base.update(overrides)
    return PlanCheckpoint(**base)  # type: ignore[arg-type]


class PlanAllHandlerTests(unittest.TestCase):
    @patch.dict(
        "os.environ",
        {
            "ARTIFACTS_BUCKET": "bucket",
            "IDEMPOTENCY_TABLE": "tbl",
            "PLAN_WAVE_MIN_REMAINING_MS": "480000",
            "CARDTRADER_JWT": "token",
        },
        clear=False,
    )
    @patch("cardtrader_inventory.aws.handlers.put_bytes")
    @patch("cardtrader_inventory.aws.handlers.run_plan_chunk")
    @patch("cardtrader_inventory.aws.handlers.load_listings_jsonl_text")
    @patch("cardtrader_inventory.aws.handlers.get_text")
    @patch("cardtrader_inventory.aws.handlers.get_json")
    @patch("cardtrader_inventory.aws.handlers.CardTraderClient")
    @patch("cardtrader_inventory.aws.handlers.DynamoPlanCheckpointStore")
    def test_pauses_when_remaining_below_budget(
        self,
        store_cls: MagicMock,
        _client_cls: MagicMock,
        get_json: MagicMock,
        get_text: MagicMock,
        load_listings: MagicMock,
        run_chunk: MagicMock,
        put_bytes: MagicMock,
    ) -> None:
        store = store_cls.return_value
        cp = _checkpoint()
        store.get.return_value = cp
        get_json.return_value = {
            "chunks": [
                {
                    "chunk_id": "000",
                    "fetch_keys": [[1, "en", False]],
                    "plan_key": "runs/run-1/chunks/000.plan.jsonl",
                },
                {
                    "chunk_id": "001",
                    "fetch_keys": [[2, "en", False]],
                    "plan_key": "runs/run-1/chunks/001.plan.jsonl",
                },
            ]
        }
        get_text.return_value = ""
        load_listings.return_value = []
        run_chunk.return_value = ChunkPlanResult(
            chunk_id="000",
            pricing_run_id="run-1",
            mode="LIVE",
            plan=PricingPlan(
                pricing_run_id="run-1",
                mode="LIVE",
                rows=[],
                summary=PlanSummary(),
            ),
        )

        # After first chunk, remaining drops below 8 min → pause with more=true.
        remaining = {"ms": 900_000}

        def _remaining() -> int:
            return remaining["ms"]

        ctx = MagicMock()
        ctx.get_remaining_time_in_millis.side_effect = _remaining

        def _after_chunk(*_a, **_k):
            remaining["ms"] = 100_000
            return run_chunk.return_value

        run_chunk.side_effect = _after_chunk

        out = plan_all_handler(
            {"pricing_run_id": "run-1", "mode": "LIVE", "s3_bucket": "bucket"},
            ctx,
        )
        self.assertTrue(out["more"])
        self.assertEqual(out["next_chunk_index"], 1)
        self.assertEqual(run_chunk.call_count, 1)
        store.save_progress.assert_called()

    @patch.dict(
        "os.environ",
        {
            "ARTIFACTS_BUCKET": "bucket",
            "IDEMPOTENCY_TABLE": "tbl",
            "PLAN_WAVE_MIN_REMAINING_MS": "480000",
            "CARDTRADER_JWT": "token",
        },
        clear=False,
    )
    @patch("cardtrader_inventory.aws.handlers._finalize_merged_plan")
    @patch("cardtrader_inventory.aws.handlers.put_bytes")
    @patch("cardtrader_inventory.aws.handlers.run_plan_chunk")
    @patch("cardtrader_inventory.aws.handlers.load_listings_jsonl_text")
    @patch("cardtrader_inventory.aws.handlers.get_text")
    @patch("cardtrader_inventory.aws.handlers.get_json")
    @patch("cardtrader_inventory.aws.handlers.CardTraderClient")
    @patch("cardtrader_inventory.aws.handlers.DynamoPlanCheckpointStore")
    def test_merges_when_all_chunks_done(
        self,
        store_cls: MagicMock,
        _client_cls: MagicMock,
        get_json: MagicMock,
        get_text: MagicMock,
        load_listings: MagicMock,
        run_chunk: MagicMock,
        put_bytes: MagicMock,
        finalize: MagicMock,
    ) -> None:
        store = store_cls.return_value
        cp = _checkpoint(chunk_count=1)
        store.get.return_value = cp
        get_json.return_value = {
            "chunks": [
                {
                    "chunk_id": "000",
                    "fetch_keys": [],
                    "plan_key": "runs/run-1/chunks/000.plan.jsonl",
                }
            ]
        }
        get_text.return_value = ""
        load_listings.return_value = []
        run_chunk.return_value = ChunkPlanResult(
            chunk_id="000",
            pricing_run_id="run-1",
            mode="LIVE",
            plan=PricingPlan(
                pricing_run_id="run-1",
                mode="LIVE",
                rows=[],
                summary=PlanSummary(),
            ),
        )
        finalize.return_value = {
            "safety_ok": True,
            "safety_errors": [],
            "summary_counts": {"chunk_count": 1},
        }

        out = plan_all_handler(
            {"pricing_run_id": "run-1", "mode": "LIVE", "s3_bucket": "bucket"},
            _Ctx(900_000),
        )
        self.assertFalse(out["more"])
        self.assertTrue(out["safety_ok"])
        finalize.assert_called_once()


if __name__ == "__main__":
    unittest.main()
