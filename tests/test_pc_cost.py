import pytest

import pc_cost
from pc_cost import (
    CARD_PRICE_FIELDS,
    PriceChartingClient,
    PriceChartingError,
    cards_for_pokemon,
    is_pokemon_card,
    name_matches,
    parse_card_prices,
    pennies_to_dollars,
)
from conftest import FakeResponse, FakeSession


@pytest.mark.parametrize("raw, dollars", [
    (1732, 17.32), ("1732", 17.32), (5, 0.05), (0, None), (None, None), ("n/a", None),
])
def test_pennies_to_dollars(raw, dollars):
    assert pennies_to_dollars(raw) == dollars


def test_card_price_field_mapping_follows_api_docs():
    product = {
        "loose-price": 1500, "cib-price": 2500, "new-price": 3000, "graded-price": 4500,
        "box-only-price": 6000, "manual-only-price": 12000, "bgs-10-price": 20000,
        "condition-17-price": 9000, "condition-18-price": 8000,
    }
    assert parse_card_prices(product) == {
        "ungraded": 15.0, "grade_7": 25.0, "grade_8": 30.0, "grade_9": 45.0,
        "grade_9_5": 60.0, "psa_10": 120.0, "bgs_10": 200.0, "cgc_10": 90.0, "sgc_10": 80.0,
    }
    assert CARD_PRICE_FIELDS["ungraded"] == "loose-price"
    assert CARD_PRICE_FIELDS["psa_10"] == "manual-only-price"


@pytest.mark.parametrize("product, expected", [
    ({"genre": "Pokemon Card", "console-name": "Pokemon Base Set"}, True),
    ({"genre": "RPG", "console-name": "GameBoy"}, False),
    ({"console-name": "Pokemon Southern Islands"}, True),       # search hits have no genre
    ({"console-name": "Nintendo DS"}, False),
])
def test_is_pokemon_card(product, expected):
    assert is_pokemon_card(product) is expected


def test_name_matches_whole_words():
    assert name_matches("Mew #1", "Mew")
    assert not name_matches("Mewtwo #10", "Mew")


def _client(*responses, clock=None):
    session = FakeSession(list(responses))
    kwargs = {"clock": clock, "sleep": clock.sleep} if clock else {"sleep": lambda s: None}
    return PriceChartingClient("t0ken", session=session, **kwargs), session


def test_client_sends_token_and_throttles(clock):
    client, session = _client(
        FakeResponse(200, {"status": "success", "products": []}),
        FakeResponse(200, {"status": "success", "id": "1", "loose-price": 100}),
        clock=clock,
    )
    client.search("mew")
    client.product("1")
    assert session.calls[0]["url"] == "https://www.pricecharting.com/api/products"
    assert session.calls[0]["params"] == {"t": "t0ken", "q": "mew"}
    assert session.calls[1]["url"] == "https://www.pricecharting.com/api/product"
    assert session.calls[1]["params"] == {"t": "t0ken", "id": "1"}
    assert clock.sleeps == [pytest.approx(pc_cost.MIN_INTERVAL)]    # at most 1 call per second


def test_client_raises_documented_error_message():
    client, _ = _client(FakeResponse(200, {"status": "error", "error-message": "Invalid token"}))
    with pytest.raises(PriceChartingError, match="Invalid token"):
        client.search("mew")


def test_client_rejects_non_json():
    client, _ = _client(FakeResponse(502, None, text="<html>Bad gateway</html>"))
    with pytest.raises(PriceChartingError, match="HTTP 502"):
        client.product("1")


def test_cards_for_pokemon_filters_and_prices(monkeypatch):
    search = FakeResponse(200, {"status": "success", "products": [
        {"id": "101", "product-name": "Mew #1", "console-name": "Pokemon Southern Islands"},
        {"id": "102", "product-name": "Mewtwo #10", "console-name": "Pokemon Base Set"},
        {"id": "103", "product-name": "Mew", "console-name": "Nintendo DS"},
        {"id": "104", "product-name": "Mew Collection Box", "console-name": "Pokemon Promo"},
        {"id": "105", "product-name": "Mew ex #151", "console-name": "Pokemon Scarlet & Violet 151",
         "loose-price": 450, "manual-only-price": 9000},     # prices already included
    ]})
    detail = FakeResponse(200, {"status": "success", "id": "101", "product-name": "Mew #1",
                                "console-name": "Pokemon Southern Islands", "genre": "Pokemon Card",
                                "loose-price": 1999, "manual-only-price": 25000})
    client, session = _client(search, detail)
    cache = {}
    cards = cards_for_pokemon(client, "Mew", cache)

    assert [(c["id"], c["title"], c["ungraded"], c["psa_10"]) for c in cards] == [
        ("101", "Mew #1", 19.99, 250.0),
        ("105", "Mew ex #151", 4.5, 90.0),
    ]
    assert len(session.calls) == 2           # one search + one detail lookup
    assert "101" in cache

    # A second run is served from the cache (only the search is repeated).
    client2, session2 = _client(FakeResponse(200, {"status": "success", "products": [
        {"id": "101", "product-name": "Mew #1", "console-name": "Pokemon Southern Islands"}]}))
    assert cards_for_pokemon(client2, "Mew", cache)[0]["ungraded"] == 19.99
    assert len(session2.calls) == 1


def test_missing_token_exits_with_setup_message(monkeypatch, capsys):
    monkeypatch.setattr(pc_cost, "load_dotenv", lambda: None)
    with pytest.raises(SystemExit) as exc:
        pc_cost.main()
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "PRICECHARTING_TOKEN" in out and "README.md" in out
