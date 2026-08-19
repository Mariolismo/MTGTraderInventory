"""Tests for CardTrader → ManaBox stock export."""

from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path

from cardtrader_inventory.manabox_export import (
    MANABOX_CSV_FIELDS,
    ct_condition_to_manabox,
    foil_to_manabox,
    rows_from_ct_aggregate,
    write_manabox_stock_csv,
)
from cardtrader_inventory.manabox_compare import AggregateRow, make_key


class ConditionExportTests(unittest.TestCase):
    def test_ct_to_manabox_roundtrip_common_grades(self) -> None:
        self.assertEqual(ct_condition_to_manabox("Slightly Played"), "excellent")
        self.assertEqual(ct_condition_to_manabox("Near Mint"), "near_mint")
        self.assertEqual(ct_condition_to_manabox("Moderately Played"), "good")
        self.assertEqual(ct_condition_to_manabox("Heavily Played"), "played")

    def test_foil_labels(self) -> None:
        self.assertEqual(foil_to_manabox(True), "foil")
        self.assertEqual(foil_to_manabox(False), "normal")


class RowsFromAggregateTests(unittest.TestCase):
    def test_builds_import_rows(self) -> None:
        scryfall = "019b51b0-e5c6-4208-922b-7736686dddcd"
        key = make_key(
            scryfall_id=scryfall,
            foil=True,
            language="en",
            condition="Slightly Played",
        )
        by_key = {
            key: AggregateRow(
                qty=3,
                display_name="Agatha's Soul Cauldron",
                set_code="WOE",
                collector_number="242",
                foil=True,
                language="en",
                scryfall_id=scryfall,
                conditions={"slightly played"},
            )
        }
        rows = rows_from_ct_aggregate(by_key, {}, ignore_condition=False)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row.quantity, 3)
        self.assertEqual(row.condition_manabox, "excellent")
        self.assertEqual(row.scryfall_id, scryfall)

    def test_write_csv_matches_manabox_headers(self) -> None:
        scryfall = "aaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        key = make_key(
            scryfall_id=scryfall,
            foil=False,
            language="en",
            condition="Near Mint",
        )
        rows = rows_from_ct_aggregate(
            {
                key: AggregateRow(
                    qty=1,
                    display_name="Test Card",
                    set_code="WOE",
                    collector_number="1",
                    foil=False,
                    language="en",
                    scryfall_id=scryfall,
                    conditions={"near mint"},
                )
            },
            {},
            ignore_condition=False,
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "stock.csv"
            write_manabox_stock_csv(path, rows)
            with path.open(encoding="utf-8", newline="") as handle:
                reader = csv.DictReader(handle)
                self.assertEqual(reader.fieldnames, MANABOX_CSV_FIELDS)
                data = list(reader)
            self.assertEqual(len(data), 1)
            self.assertEqual(data[0]["Name"], "Test Card")
            self.assertEqual(data[0]["Set code"], "WOE")
            self.assertEqual(data[0]["Foil"], "normal")
            self.assertEqual(data[0]["Condition"], "near_mint")
            self.assertEqual(data[0]["Scryfall ID"], scryfall)


if __name__ == "__main__":
    unittest.main()
