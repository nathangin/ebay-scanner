import pytest

import listings
from listings import (
    BUNDLE_RE,
    add_condition_details,
    build_filters,
    fetch_asking_price,
    fetch_ebay_listings,
    listing_from_item,
    parse_condition_descriptors,
    resolve_sort,
)
from conftest import FakeResponse, item_summary, token_response


def _search(*items):
    return FakeResponse(200, {"total": len(items), "itemSummaries": list(items)})


# ── sort / filters ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("old, api", [
    ("10", "newlyListed"),    # website: newly listed
    ("15", "price"),          # website: price + shipping, lowest first
    ("price", "price"),
    ("endingSoonest", "endingSoonest"),
    (None, None),             # Best Match
    ("", None),
])
def test_sort_codes_map_to_api_values(old, api):
    assert resolve_sort(old) == api


def test_unknown_sort_is_rejected():
    with pytest.raises(ValueError):
        resolve_sort("13")


def test_build_filters():
    assert build_filters() == ["buyingOptions:{FIXED_PRICE}"]
    assert build_filters(buy_it_now_only=False) == []
    assert build_filters(condition_ids=["2750", "4000"], delivery_country="US") == [
        "buyingOptions:{FIXED_PRICE}", "conditionIds:{2750|4000}", "deliveryCountry:US",
    ]


@pytest.mark.parametrize("title", [
    "Pokemon card lot 50 cards holo rares",
    "Pokemon Evolving Skies Booster Box Sealed",
    "Charizard collector tin",
    "Pokemon 9 pocket binder",
    "Mixed bundle of vintage cards",
])
def test_bundle_filter_skips_lots_and_sealed(title):
    assert BUNDLE_RE.search(title)


@pytest.mark.parametrize("title", [
    "Charizard 4/102 Base Set Holo Rare",
    "Tinkaton ex 219/193 Paldea Evolved",     # "tin" inside a word
    "Lottie 188/185 Full Art",                # "lot" inside a word
])
def test_bundle_filter_keeps_single_cards(title):
    assert not BUNDLE_RE.search(title)


# ── response mapping ──────────────────────────────────────────────────────────

def test_listing_maps_item_summary_fields():
    listing = listing_from_item(item_summary(price="100.00", shipping="5.00"))
    assert listing == {
        "item_id": "v1|110000000001|0",
        "title": "Charizard 4/102 Base Set Holo NM",
        "url": "https://www.ebay.com/itm/110000000001",
        "price": 100.0,
        "shipping": 5.0,
        "total": 105.0,
        "currency": "USD",
        "image_url": "https://i.ebayimg.com/images/g/110000000001/s-l225.jpg",
        "ebay_condition_raw": "Ungraded",
        "condition_id": "4000",
        "condition_descriptors": {},
        "seller_username": "demo_seller",
        "seller_feedback_pct": 99.5,
        "seller_feedback_score": 1234,
    }


def test_calculated_shipping_is_unknown():
    listing = listing_from_item(item_summary(price="20.00", shipping="7.50", shipping_type="CALCULATED"))
    assert listing["shipping"] is None
    assert listing["total"] == 20.0


def test_free_shipping_and_cheapest_option():
    free = listing_from_item(item_summary(price="20.00", shipping="0.00"))
    assert free["shipping"] == 0.0 and free["total"] == 20.0

    item = item_summary(price="20.00", shipping=None)
    item["shippingOptions"] = [
        {"shippingCostType": "FIXED", "shippingCost": {"value": "4.25", "currency": "USD"}},
        {"shippingCostType": "FIXED", "shippingCost": {"value": "1.10", "currency": "USD"}},
    ]
    assert listing_from_item(item)["total"] == 21.1


def test_no_shipping_info_and_thumbnail_fallback():
    item = item_summary(shipping=None)
    del item["image"]
    item["thumbnailImages"] = [{"imageUrl": "https://i.ebayimg.com/thumb.jpg"}]
    listing = listing_from_item(item)
    assert listing["shipping"] is None and listing["total"] == listing["price"]
    assert listing["image_url"] == "https://i.ebayimg.com/thumb.jpg"


@pytest.mark.parametrize("field", ["title", "itemWebUrl", "price"])
def test_incomplete_items_are_dropped(field):
    item = item_summary()
    del item[field]
    assert listing_from_item(item) is None


# ── fetch_ebay_listings ───────────────────────────────────────────────────────

def test_fetch_builds_request_and_filters_results(make_client):
    items = [
        item_summary("v1|1|0", "Charizard 4/102 Base Set", price="120.00"),
        item_summary("v1|2|0", "Pokemon card lot 100 cards", price="30.00"),        # bundle
        item_summary("v1|3|0", "Pikachu 58/102 Base Set", price="0.49"),           # < $0.50
        item_summary("v1|4|0", "Eevee pick your card", price="1.00",
                     itemGroupType="SELLER_DEFINED_VARIATIONS"),                   # price range
        item_summary("v1|5|0", "Blastoise 2/102 Base Set", price="80.00"),
    ]
    client, session = make_client(token_response(), _search(*items))
    results = fetch_ebay_listings("pokemon card", max_results=25, client=client)

    assert [r["item_id"] for r in results] == ["v1|1|0", "v1|5|0"]
    params = session.calls[1]["params"]
    assert params["category_ids"] == "183454"
    assert params["filter"] == "buyingOptions:{FIXED_PRICE},deliveryCountry:US"
    assert params["sort"] == "newlyListed"
    assert params["limit"] == 50


def test_fetch_for_sealed_product(make_client):
    items = [item_summary("v1|1|0", "Pokemon Chinese Gem Pack booster box sealed", price="60.00")]
    client, session = make_client(token_response(), _search(*items))
    results = fetch_ebay_listings("gem pack box", sort="15", filter_bundles=False,
                                  category_ids=None, client=client)
    assert len(results) == 1
    params = session.calls[1]["params"]
    assert "category_ids" not in params
    assert params["sort"] == "price"


def test_fetch_respects_max_results(make_client):
    items = [item_summary(f"v1|{i}|0", f"Charizard {i}/102 Base Set") for i in range(1, 8)]
    client, _ = make_client(token_response(), _search(*items))
    assert len(fetch_ebay_listings("charizard", max_results=3, client=client)) == 3


def test_fetch_returns_empty_list_on_api_error(make_client, capsys):
    client, _ = make_client(token_response(), FakeResponse(400, {"errors": [{"message": "bad q"}]}))
    assert fetch_ebay_listings("charizard", client=client) == []
    assert "skipped" in capsys.readouterr().out


def test_fetch_condition_uses_get_item(make_client):
    detail = {
        "itemId": "v1|1|0", "condition": "Graded", "conditionId": "2750",
        "conditionDescriptors": [
            {"name": "Professional Grader", "values": [{"content": "Professional Sports Authenticator (PSA)"}]},
            {"name": "Grade", "values": [{"content": "10"}]},
        ],
    }
    client, session = make_client(token_response(), _search(item_summary("v1|1|0")),
                                  FakeResponse(200, detail))
    [listing] = fetch_ebay_listings("charizard", fetch_condition=True, client=client)
    assert session.calls[2]["url"].endswith("/item/v1%7C1%7C0")
    assert listing["condition_id"] == "2750"
    assert listing["condition_descriptors"] == {
        "Professional Grader": "Professional Sports Authenticator (PSA)", "Grade": "10",
    }


# ── condition descriptors ─────────────────────────────────────────────────────

def test_parse_condition_descriptors_ungraded_with_additional_info():
    raw = [{"name": "Card Condition",
            "values": [{"content": "Near mint or better", "additionalInfo": ["Minor corner wear"]}]}]
    assert parse_condition_descriptors(raw) == {"Card Condition": "Near mint or better"}


@pytest.mark.parametrize("raw", [None, "Graded", [None, {"values": []}, {"name": "Grade"}]])
def test_parse_condition_descriptors_ignores_malformed_input(raw):
    assert parse_condition_descriptors(raw) == {}


def test_add_condition_details_keeps_listing_on_failure(make_client):
    client, _ = make_client(token_response(), FakeResponse(404, {"errors": [{"message": "not found"}]}))
    listing = listing_from_item(item_summary())
    before = dict(listing)
    assert add_condition_details(listing, client) == before


# ── asking-price fallback ─────────────────────────────────────────────────────

def test_asking_price_is_median_of_cheapest_ungraded_listings(make_client):
    items = [
        item_summary("v1|1|0", "Mew 8/18 Southern Islands", price="4.00"),
        item_summary("v1|2|0", "Mew 8/18 Southern Islands PSA 9", price="1.00"),       # graded
        item_summary("v1|3|0", "Mew Southern Islands Japanese", price="1.50"),        # foreign
        item_summary("v1|4|0", "Mew Southern Islands proxy card", price="0.99"),      # proxy
        item_summary("v1|5|0", "Southern Islands lot 5 cards", price="2.00"),         # lot
        item_summary("v1|6|0", "Mew 8/18 Southern Islands", price="6.00"),
        item_summary("v1|7|0", "Mew 8/18 Southern Islands", price="5.00"),
        item_summary("v1|8|0", "Mew 8/18 Southern Islands", price="50.00"),
    ]
    client, session = make_client(token_response(), _search(*items))
    assert fetch_asking_price("Mew Southern Islands 8 pokemon card", max_results=3,
                              client=client) == 5.0          # median of 4, 5, 6

    params = session.calls[1]["params"]
    assert params["sort"] == "price"
    assert params["category_ids"] == "183454"
    assert "conditionIds:{4000}" in params["filter"]
    assert "buyingOptions:{FIXED_PRICE}" in params["filter"]


def test_asking_price_none_when_nothing_suitable(make_client):
    client, _ = make_client(token_response(),
                            _search(item_summary(title="Mew PSA 10 Southern Islands")))
    assert fetch_asking_price("mew", client=client) is None


def test_asking_price_none_on_api_error(make_client):
    client, _ = make_client(token_response(), FakeResponse(400, {"errors": [{"message": "bad"}]}))
    assert fetch_asking_price("mew", client=client) is None


def test_module_has_no_sold_price_function():
    # Sold prices would need eBay's restricted Marketplace Insights API.
    assert not hasattr(listings, "fetch_sold_price")
