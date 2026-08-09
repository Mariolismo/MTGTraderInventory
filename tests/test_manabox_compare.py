"""Unit tests for ManaBox ↔ CardTrader scryfall-based matching."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from cardtrader_inventory.manabox_compare import (
    aggregate_ct_listings,
    compare_aggregates,
    load_manabox_csv,
    make_key,
    normalize_condition,
)
from cardtrader_inventory.models import Listing
from cardtrader_inventory.scryfall import (
    BlueprintUids,
    ScryfallClient,
    enrich_blueprint_scryfall,
)


def _listing(
    *,
    listing_id: int,
    blueprint_id: int,
    name: str,
    cn: str,
    foil: bool,
    condition: str,
    expansion_id: int,
    qty: int = 1,
    language: str = "en",
) -> Listing:
    return Listing(
        id=listing_id,
        blueprint_id=blueprint_id,
        quantity=qty,
        price_cents=100,
        condition=condition,
        language=language,
        foil=foil,
        game_id=1,
        user_id=1,
        name_en=name,
        rarity="common",
        raw={
            "expansion": {"id": expansion_id},
            "properties_hash": {"collector_number": cn, "condition": condition},
        },
    )


class MatchKeyTests(unittest.TestCase):
    def test_scryfall_distinguishes_printings(self) -> None:
        a = make_key(
            scryfall_id="aaa",
            foil=True,
            language="en",
            condition="excellent",
        )
        b = make_key(
            scryfall_id="bbb",
            foil=True,
            language="en",
            condition="excellent",
        )
        self.assertNotEqual(a, b)
        self.assertEqual(a[0], "aaa")

    def test_excellent_maps_to_slightly_played(self) -> None:
        self.assertEqual(normalize_condition("excellent"), "slightly played")
        self.assertEqual(
            make_key(
                scryfall_id="0ed38a8d-9c77-4f21-90f5-f6a4c947d6d9",
                foil=False,
                language="en",
                condition="excellent",
            ),
            make_key(
                scryfall_id="0ed38a8d-9c77-4f21-90f5-f6a4c947d6d9",
                foil=False,
                language="en",
                condition="Slightly Played",
            ),
        )


class CompareScryfallTests(unittest.TestCase):
    def test_puca_matches_despite_ct_cecc_vs_manabox_ecc(self) -> None:
        """Same scryfall_id joins even when CT expansion code != ManaBox set code."""
        scryfall = "0ed38a8d-9c77-4f21-90f5-f6a4c947d6d9"
        csv_text = (
            "Name,Set code,Collector number,Foil,Quantity,Condition,Language,Scryfall ID\n"
            f"Puca's Covenant,ECC,38,normal,2,excellent,en,{scryfall}\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mb.csv"
            path.write_text(csv_text, encoding="utf-8")
            csv_by, missing = load_manabox_csv(path)
        self.assertEqual(missing, 0)

        listings = [
            _listing(
                listing_id=417210384,
                blueprint_id=365345,
                name="Puca's Covenant",
                cn="038",
                foil=False,
                condition="Slightly Played",
                expansion_id=4329,  # CECC on CT
                qty=2,
            )
        ]
        # CT expansion code deliberately wrong/different from ManaBox ECC
        ct_by, unresolved = aggregate_ct_listings(
            listings,
            blueprint_scryfall={365345: scryfall},
            expansion_codes={4329: "CECC"},
        )
        self.assertEqual(unresolved, 0)

        result = compare_aggregates(
            csv_by,
            ct_by,
            csv_path=str(path),
            ignore_condition=False,
        )
        self.assertEqual(result.summary["matched_keys"], 1)
        self.assertEqual(result.summary["qty_exact_match"], 1)
        self.assertEqual(result.summary["only_in_csv"], 0)
        self.assertEqual(result.summary["only_on_ct"], 0)

    def test_island_printings_split_by_scryfall(self) -> None:
        tla = "d894c61a-4062-442e-8ead-5197c3bffd00"
        tdm = "b300be80-6618-4284-b5c3-95c1ab373e6f"
        csv_text = (
            "Name,Set code,Collector number,Foil,Quantity,Condition,Language,Scryfall ID\n"
            f"Island,TLA,288,foil,1,excellent,en,{tla}\n"
            f"Island,TDM,288,foil,4,excellent,en,{tdm}\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mb.csv"
            path.write_text(csv_text, encoding="utf-8")
            csv_by, _ = load_manabox_csv(path)

        listings = [
            _listing(
                listing_id=1,
                blueprint_id=10,
                name="Island",
                cn="288",
                foil=True,
                condition="Slightly Played",
                expansion_id=1,
                qty=1,
            ),
            _listing(
                listing_id=2,
                blueprint_id=20,
                name="Island",
                cn="288",
                foil=True,
                condition="Slightly Played",
                expansion_id=2,
                qty=3,
            ),
        ]
        ct_by, _ = aggregate_ct_listings(
            listings,
            blueprint_scryfall={10: tla, 20: tdm},
            expansion_codes={1: "TLA", 2: "TDM"},
        )
        result = compare_aggregates(
            csv_by, ct_by, csv_path=str(path), ignore_condition=False
        )
        self.assertEqual(result.summary["matched_keys"], 2)
        self.assertEqual(result.summary["qty_exact_match"], 1)
        self.assertEqual(result.summary["qty_mismatch"], 1)
        mismatch = result.qty_mismatch[0]
        self.assertEqual(mismatch.scryfall_id, tdm)
        self.assertEqual(mismatch.csv_qty, 4)
        self.assertEqual(mismatch.ct_qty, 3)


class UidEnrichTests(unittest.TestCase):
    def test_tcgplayer_uid_fills_missing_scryfall(self) -> None:
        catalog = {
            249165: BlueprintUids(
                blueprint_id=249165,
                scryfall_id=None,
                tcg_player_id=498450,
                card_market_ids=[716069],
            )
        }
        test_case = self

        class FakeScryfall(ScryfallClient):
            def scryfall_id_for_tcgplayer(self, tcg_player_id: int) -> str | None:
                test_case.assertEqual(tcg_player_id, 498450)
                return "24521350-ffa6-46d9-95ed-6573c681e095"

            def scryfall_id_for_cardmarket(self, cardmarket_id: int) -> str | None:
                test_case.fail("cardmarket should not be used when tcgplayer succeeds")

        stats = enrich_blueprint_scryfall(
            catalog,
            needed_blueprint_ids={249165},
            scryfall=FakeScryfall(),
        )
        self.assertEqual(stats.via_tcgplayer, 1)
        self.assertEqual(stats.still_missing, 0)
        self.assertEqual(
            catalog[249165].scryfall_id,
            "24521350-ffa6-46d9-95ed-6573c681e095",
        )
        self.assertEqual(catalog[249165].scryfall_source, "tcgplayer")

    def test_cardmarket_uid_used_when_tcgplayer_missing(self) -> None:
        catalog = {
            1: BlueprintUids(
                blueprint_id=1,
                scryfall_id=None,
                tcg_player_id=None,
                card_market_ids=[716069],
            )
        }
        test_case = self

        class FakeScryfall(ScryfallClient):
            def scryfall_id_for_tcgplayer(self, tcg_player_id: int) -> str | None:
                test_case.fail("tcgplayer should not be called")

            def scryfall_id_for_cardmarket(self, cardmarket_id: int) -> str | None:
                test_case.assertEqual(cardmarket_id, 716069)
                return "24521350-ffa6-46d9-95ed-6573c681e095"

        stats = enrich_blueprint_scryfall(
            catalog, needed_blueprint_ids={1}, scryfall=FakeScryfall()
        )
        self.assertEqual(stats.via_cardmarket, 1)
        self.assertEqual(catalog[1].scryfall_source, "cardmarket")


if __name__ == "__main__":
    unittest.main()
