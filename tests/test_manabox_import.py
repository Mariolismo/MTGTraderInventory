"""Tests for ManaBox → CardTrader import planning."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from cardtrader_inventory.manabox_import import (
    CsvCardRow,
    build_bulk_create_payload,
    load_csv_cards,
    manabox_condition_to_ct,
    plan_import,
)
from cardtrader_inventory.scryfall import BlueprintUids


class ConditionMapTests(unittest.TestCase):
    def test_excellent_to_slightly_played(self) -> None:
        self.assertEqual(manabox_condition_to_ct("excellent"), "Slightly Played")
        self.assertEqual(manabox_condition_to_ct("near_mint"), "Near Mint")
        self.assertEqual(manabox_condition_to_ct("good"), "Moderately Played")
        self.assertIsNone(manabox_condition_to_ct("trash"))


class PlanImportTests(unittest.TestCase):
    def test_payload_sets_foil_condition_language_and_sentinel_price(self) -> None:
        scryfall = "019b51b0-e5c6-4208-922b-7736686dddcd"
        csv_rows = [
            CsvCardRow(
                name="Agatha's Soul Cauldron",
                set_code="WOE",
                collector_number="242",
                foil=True,
                language="en",
                condition_raw="excellent",
                quantity=1,
                scryfall_id=scryfall,
                line_no=2,
            )
        ]
        index = {
            scryfall: [
                BlueprintUids(
                    blueprint_id=256899,
                    scryfall_id=scryfall,
                    tcg_player_id=1,
                    card_market_ids=[],
                    scryfall_source="ct",
                )
            ]
        }
        plan = plan_import(csv_rows, index, existing_keys=set(), price_cents=999_999)
        self.assertEqual(plan.summary["create"], 1)
        payload = build_bulk_create_payload(plan.rows)
        self.assertEqual(len(payload), 1)
        product = payload[0]
        self.assertEqual(product["blueprint_id"], 256899)
        self.assertEqual(product["price"], 9999.99)
        self.assertEqual(product["quantity"], 1)
        self.assertEqual(product["error_mode"], "strict")
        self.assertEqual(product["properties"]["mtg_foil"], True)
        self.assertEqual(product["properties"]["condition"], "Slightly Played")
        self.assertEqual(product["properties"]["mtg_language"], "en")
        self.assertIn(scryfall, product["user_data_field"])

    def test_skips_existing_inventory_key(self) -> None:
        scryfall = "aaa"
        csv_rows = [
            CsvCardRow(
                name="Test",
                set_code="WOE",
                collector_number="1",
                foil=False,
                language="en",
                condition_raw="excellent",
                quantity=2,
                scryfall_id=scryfall,
                line_no=2,
            )
        ]
        index = {
            scryfall: [
                BlueprintUids(blueprint_id=10, scryfall_id=scryfall, scryfall_source="ct")
            ]
        }
        existing = {(10, False, "en", "Slightly Played")}
        plan = plan_import(csv_rows, index, existing)
        self.assertEqual(plan.rows[0].action, "skip_existing")
        self.assertEqual(build_bulk_create_payload(plan.rows), [])

    def test_ambiguous_blueprint_is_error(self) -> None:
        scryfall = "bbb"
        csv_rows = [
            CsvCardRow(
                name="X",
                set_code="TDM",
                collector_number="1",
                foil=False,
                language="en",
                condition_raw="near_mint",
                quantity=1,
                scryfall_id=scryfall,
                line_no=2,
            )
        ]
        index = {
            scryfall: [
                BlueprintUids(blueprint_id=1, scryfall_id=scryfall),
                BlueprintUids(blueprint_id=2, scryfall_id=scryfall),
            ]
        }
        plan = plan_import(csv_rows, index, set())
        self.assertEqual(plan.rows[0].action, "skip_error")
        self.assertIn("ambiguous", plan.rows[0].reason)

    def test_load_csv_foil_and_scryfall(self) -> None:
        text = (
            "Name,Set code,Collector number,Foil,Quantity,Condition,Language,"
            "Scryfall ID,Altered,Misprint\n"
            "Card,WOE,242,foil,1,excellent,en,"
            "019b51b0-e5c6-4208-922b-7736686dddcd,false,false\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x.csv"
            path.write_text(text, encoding="utf-8")
            rows = load_csv_cards(path)
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0].foil)
        self.assertEqual(rows[0].scryfall_id, "019b51b0-e5c6-4208-922b-7736686dddcd")


if __name__ == "__main__":
    unittest.main()
