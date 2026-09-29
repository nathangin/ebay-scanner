import requests

import prices
from prices import PriceLookup, nm_price_from_card
from conftest import FakeResponse


def _card(tcg=None, cardmarket=None, card_id="base1-4"):
    card = {"id": card_id, "name": "Charizard"}
    if tcg is not None:
        card["tcgplayer"] = {"prices": tcg}
    if cardmarket is not None:
        card["cardmarket"] = {"prices": cardmarket}
    return card


# ── parsing ───────────────────────────────────────────────────────────────────

def test_prefers_holofoil_market_price():
    card = _card({"normal": {"market": 1.5}, "holofoil": {"market": 12.0, "mid": 15.0}})
    assert nm_price_from_card(card) == 12.0


def test_falls_back_to_mid_when_no_market():
    assert nm_price_from_card(_card({"normal": {"market": None, "mid": 3.25}})) == 3.25


def test_unlimited_is_preferred_over_first_edition():
    # WOTC holos have no plain "holofoil" key; the 1st Edition price must not win.
    card = _card({"1stEditionHolofoil": {"market": 900.0}, "unlimitedHolofoil": {"market": 120.0}})
    assert nm_price_from_card(card) == 120.0


def test_first_edition_used_when_it_is_the_only_price():
    assert nm_price_from_card(_card({"1stEditionNormal": {"market": 40.0}})) == 40.0


def test_cardmarket_fallback_and_no_price():
    assert nm_price_from_card(_card(cardmarket={"averageSellPrice": 0, "trendPrice": 4.2})) == 4.2
    assert nm_price_from_card(_card({"holofoil": {"market": 0}})) is None
    assert nm_price_from_card({"id": "x", "tcgplayer": None}) is None


# ── lookups (requests.get mocked) ─────────────────────────────────────────────

class FakeGet:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        nxt = self.responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


def test_lookup_by_id_is_cached(monkeypatch):
    fake = FakeGet(FakeResponse(200, {"data": _card({"holofoil": {"market": 350.0}})}))
    monkeypatch.setattr(prices.requests, "get", fake)
    lookup = PriceLookup()
    assert lookup.get_nm_price(card_id="base1-4") == 350.0
    assert lookup.get_nm_price(card_id="base1-4") == 350.0
    assert len(fake.calls) == 1
    assert fake.calls[0]["url"] == "https://api.pokemontcg.io/v2/cards/base1-4"


def test_lookup_by_name_takes_median_of_printings(monkeypatch):
    cards = [_card({"holofoil": {"market": p}}, card_id=f"c{i}") for i, p in enumerate((10.0, 30.0, 20.0))]
    fake = FakeGet(FakeResponse(200, {"data": cards}))
    monkeypatch.setattr(prices.requests, "get", fake)
    assert PriceLookup().get_nm_price(card_name="Lugia") == 20.0
    assert fake.calls[0]["params"] == {"q": 'name:"Lugia"', "pageSize": 10}


def test_lookup_by_name_broadens_query_when_empty(monkeypatch):
    fake = FakeGet(
        FakeResponse(200, {"data": []}),                                        # name + set + number
        FakeResponse(200, {"data": []}),                                        # name + set
        FakeResponse(200, {"data": [_card({"normal": {"market": 2.0}})]}),      # name only
    )
    monkeypatch.setattr(prices.requests, "get", fake)
    assert PriceLookup().get_nm_price(card_name="Mew", set_name="Southern Islands", card_number="1") == 2.0
    assert [c["params"]["q"] for c in fake.calls] == [
        'name:"Mew" set.name:"Southern Islands" number:"1"',
        'name:"Mew" set.name:"Southern Islands"',
        'name:"Mew"',
    ]


def test_network_errors_return_none_and_are_not_cached(monkeypatch):
    fake = FakeGet(requests.ConnectionError("down"),
                   FakeResponse(200, {"data": _card({"normal": {"market": 5.0}})}))
    monkeypatch.setattr(prices.requests, "get", fake)
    lookup = PriceLookup()
    assert lookup.get_nm_price(card_id="base1-58") is None
    assert lookup.get_nm_price(card_id="base1-58") == 5.0     # retried, not a cached None


def test_unknown_card_id_is_none(monkeypatch):
    monkeypatch.setattr(prices.requests, "get", FakeGet(FakeResponse(404, {"error": "not found"})))
    assert PriceLookup().get_nm_price(card_id="nope-1") is None


def test_nothing_to_look_up():
    assert PriceLookup().get_nm_price() is None
