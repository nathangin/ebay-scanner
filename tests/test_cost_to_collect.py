import json

import pytest

import cost_to_collect as ctc
import ebay_api
import listings


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(ctc, "ASKING_CACHE_FILE", str(tmp_path / "asking_price_cache.json"))
    monkeypatch.setattr(ctc, "_asking_cache", {})


def _card(card_id, name="Mew", tcg=None, cardmarket=None, supertype="Pokémon"):
    card = {"id": card_id, "name": name, "number": card_id.split("-")[-1], "supertype": supertype,
            "set": {"name": "Southern Islands", "releaseDate": "2001/07/31"}}
    if tcg:
        card["tcgplayer"] = {"prices": tcg}
    if cardmarket:
        card["cardmarket"] = {"prices": cardmarket}
    return card


def test_price_sources():
    assert ctc.price_with_source(_card("a-1", tcg={"normal": {"market": 3.0},
                                                    "reverseHolofoil": {"market": 2.0}})) == (2.0, "tcgplayer")
    assert ctc.price_with_source(_card("a-2", cardmarket={"avg30": 1.5})) == (1.5, "cardmarket")
    assert ctc.price_with_source(_card("a-3")) == (None, None)

    ctc._asking_cache["a-3"] = 4.0
    assert ctc.price_with_source(_card("a-3")) == (4.0, "ebay_asking")


def test_fill_missing_uses_labelled_asking_prices(monkeypatch, capsys):
    monkeypatch.setattr(ebay_api, "get_default_client", lambda: object())
    searched = []

    def fake_asking_price(term, max_results=12, client=None):
        searched.append(term)
        return 7.5 if term.startswith("Mew ") else None

    monkeypatch.setattr(listings, "fetch_asking_price", fake_asking_price)
    monkeypatch.setattr(ctc.time, "sleep", lambda s: None)

    cards = [
        _card("si1-1", "Mew"),                                          # needs a price
        _card("si1-2", "Pidgeot", tcg={"holofoil": {"market": 9.0}}),   # already priced
        _card("si1-3", "Onix"),                                         # nothing listed
        _card("si1-x", "Rare Candy", supertype="Trainer"),              # not a Pokemon
    ]
    ctc.fill_missing_prices(cards)

    assert searched == ["Mew Southern Islands 1 pokemon card", "Onix Southern Islands 3 pokemon card"]
    with open(ctc.ASKING_CACHE_FILE, encoding="utf-8") as fh:
        saved = json.load(fh)
    assert saved["asking_prices"] == {"si1-1": 7.5, "si1-3": None}
    assert "NOT sold prices" in saved["note"]
    assert "asking" in capsys.readouterr().out

    rows, by_base = ctc.build_summary(cards)
    mew = next(r for r in rows if r["name"] == "Mew")
    assert (mew["priced_cards"], mew["asking_priced"], mew["total_cost"]) == (1, 1, 7.5)


def test_fill_missing_skips_without_credentials(capsys):
    ctc.fill_missing_prices([_card("si1-1")])
    assert "Skipping --fill-missing" in capsys.readouterr().out


def test_detail_view_marks_asking_prices(capsys):
    ctc._asking_cache["si1-1"] = 7.5
    _, by_base = ctc.build_summary([_card("si1-1", "Mew")])
    ctc.print_pokemon_detail(by_base, "mew")
    out = capsys.readouterr().out
    assert "$7.50*" in out
    assert "asking prices" in out and "NOT sold prices" in out


@pytest.mark.parametrize("name, bases", [
    ("Erika's Vileplume ex", ["Vileplume"]),
    ("M Charizard-EX", ["Charizard"]),
    ("Latias & Latios-GX", ["Latias", "Latios"]),
    ("Dark Charizard", ["Charizard"]),
    ("Mewtwo ★", ["Mewtwo"]),
])
def test_extract_base_names(name, bases):
    assert ctc.extract_base_names(name) == bases
