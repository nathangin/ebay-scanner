import base64

import pytest
import requests

import ebay_api
from ebay_api import (
    BROWSE_SCOPE,
    EbayApiError,
    EbayClient,
    EbayCredentialsError,
    get_default_client,
    marketplace_country,
)
from conftest import FakeResponse, token_response


def _search_ok():
    return FakeResponse(200, {"total": 0, "itemSummaries": []})


# ── OAuth token ───────────────────────────────────────────────────────────────

def test_token_request_uses_client_credentials_grant(make_client):
    client, session = make_client(token_response(), _search_ok())
    client.search("pikachu")

    token_call = session.calls[0]
    assert token_call["method"] == "POST"
    assert token_call["url"] == "https://api.ebay.com/identity/v1/oauth2/token"
    expected = base64.b64encode(b"my-app-id:my-cert-id").decode()
    assert token_call["headers"]["Authorization"] == f"Basic {expected}"
    assert token_call["headers"]["Content-Type"] == "application/x-www-form-urlencoded"
    assert token_call["data"] == {"grant_type": "client_credentials", "scope": BROWSE_SCOPE}


def test_token_is_cached_until_shortly_before_expiry(make_client, clock):
    client, session = make_client(
        token_response("tok-1", expires_in=7200), _search_ok(), _search_ok(),
        token_response("tok-2", expires_in=7200), _search_ok(),
    )
    client.search("a")
    clock.now += 7000            # still inside the 7200 - 120 s window
    client.search("b")
    assert [c["method"] for c in session.calls] == ["POST", "GET", "GET"]
    assert session.calls[2]["headers"]["Authorization"] == "Bearer tok-1"

    clock.now += 100             # 7100 s after minting: past the refresh margin
    client.search("c")
    assert [c["method"] for c in session.calls] == ["POST", "GET", "GET", "POST", "GET"]
    assert session.calls[-1]["headers"]["Authorization"] == "Bearer tok-2"


def test_rejected_token_is_replaced_once(make_client):
    client, session = make_client(
        token_response("stale"), FakeResponse(401, {"errors": [{"message": "Invalid access token"}]}),
        token_response("fresh"), _search_ok(),
    )
    assert client.search("pikachu") == {"total": 0, "itemSummaries": []}
    assert session.calls[1]["headers"]["Authorization"] == "Bearer stale"
    assert session.calls[3]["headers"]["Authorization"] == "Bearer fresh"


def test_bad_credentials_error_points_to_setup(make_client):
    client, _ = make_client(FakeResponse(401, {"error": "invalid_client",
                                               "error_description": "client authentication failed"}))
    with pytest.raises(EbayApiError) as exc:
        client.search("pikachu")
    message = str(exc.value)
    assert "invalid_client" in message
    assert "EBAY_CLIENT_ID" in message and "README.md" in message


# ── search / getItem requests ─────────────────────────────────────────────────

def test_search_builds_documented_request(make_client):
    client, session = make_client(token_response("tok"), _search_ok())
    client.search(
        "charizard base set",
        category_ids=["183454"],
        filters=["buyingOptions:{FIXED_PRICE}", "deliveryCountry:US"],
        sort="newlyListed",
        limit=50,
    )
    call = session.calls[1]
    assert call["method"] == "GET"
    assert call["url"] == "https://api.ebay.com/buy/browse/v1/item_summary/search"
    assert call["params"] == {
        "q": "charizard base set",
        "limit": 50,
        "category_ids": "183454",
        "filter": "buyingOptions:{FIXED_PRICE},deliveryCountry:US",
        "sort": "newlyListed",
    }
    assert call["headers"]["Authorization"] == "Bearer tok"
    assert call["headers"]["X-EBAY-C-MARKETPLACE-ID"] == "EBAY_US"
    assert call["timeout"] == client.timeout


def test_search_omits_empty_options_and_truncates_query(make_client):
    client, session = make_client(token_response(), _search_ok())
    client.search("x" * 150, category_ids=None, filters=[], sort=None, limit=10, offset=20)
    params = session.calls[1]["params"]
    assert params == {"q": "x" * 100, "limit": 10, "offset": 20}


@pytest.mark.parametrize("kwargs", [{"limit": 0}, {"limit": 201}, {"offset": 10_000}])
def test_search_rejects_out_of_range_paging(make_client, kwargs):
    client, _ = make_client()
    with pytest.raises(ValueError):
        client.search("pikachu", **kwargs)


def test_get_item_encodes_restful_item_id(make_client):
    client, session = make_client(token_response(), FakeResponse(200, {"itemId": "v1|123|0"}))
    assert client.get_item("v1|123|0") == {"itemId": "v1|123|0"}
    assert session.calls[1]["url"] == "https://api.ebay.com/buy/browse/v1/item/v1%7C123%7C0"


# ── retries and errors ────────────────────────────────────────────────────────

def test_retries_429_and_5xx_with_backoff(make_client, clock):
    client, session = make_client(
        token_response(),
        FakeResponse(429, {"errors": [{"errorId": 2001, "message": "Too many requests."}]},
                     headers={"Retry-After": "3"}),
        FakeResponse(503, None),
        _search_ok(),
    )
    assert client.search("pikachu")["total"] == 0
    assert clock.sleeps == [3.0, 2.0]   # Retry-After first, then 1s * 2**1
    assert len(session.calls) == 4


def test_gives_up_after_max_retries(make_client, clock):
    client, session = make_client(token_response(), *[FakeResponse(500, None) for _ in range(4)])
    with pytest.raises(EbayApiError) as exc:
        client.search("pikachu")
    assert exc.value.status_code == 500
    assert len(session.calls) == 1 + 4          # token + first try + 3 retries
    assert clock.sleeps == [1.0, 2.0, 4.0]


def test_network_errors_are_retried_then_reported(make_client):
    client, _ = make_client(token_response(), *[requests.ConnectionError("down") for _ in range(4)])
    with pytest.raises(EbayApiError, match="could not reach eBay"):
        client.search("pikachu")


def test_client_errors_are_not_retried_and_message_is_parsed(make_client, clock):
    body = {"errors": [{"errorId": 12001, "domain": "API_BROWSE", "category": "REQUEST",
                        "message": "Invalid filter", "longMessage": "The filter value is invalid."}]}
    client, session = make_client(token_response(), FakeResponse(400, body))
    with pytest.raises(EbayApiError) as exc:
        client.search("pikachu")
    assert exc.value.status_code == 400
    assert "The filter value is invalid." in str(exc.value)
    assert exc.value.errors[0]["errorId"] == 12001
    assert len(session.calls) == 2 and clock.sleeps == []


# ── configuration ─────────────────────────────────────────────────────────────

def test_missing_credentials_error_points_to_readme():
    with pytest.raises(EbayCredentialsError) as exc:
        EbayClient.from_env()
    message = str(exc.value)
    assert "EBAY_CLIENT_ID" in message and "EBAY_CLIENT_SECRET" in message
    assert "README.md" in message


def test_from_env_sandbox_and_marketplace(monkeypatch):
    monkeypatch.setenv("EBAY_CLIENT_ID", "id")
    monkeypatch.setenv("EBAY_CLIENT_SECRET", "secret")
    monkeypatch.setenv("EBAY_ENV", "sandbox")
    monkeypatch.setenv("EBAY_MARKETPLACE_ID", "EBAY_GB")
    client = EbayClient.from_env()
    assert client.host == "https://api.sandbox.ebay.com"
    assert client.marketplace_id == "EBAY_GB"


def test_from_env_rejects_unknown_environment(monkeypatch):
    monkeypatch.setenv("EBAY_CLIENT_ID", "id")
    monkeypatch.setenv("EBAY_CLIENT_SECRET", "secret")
    monkeypatch.setenv("EBAY_ENV", "staging")
    with pytest.raises(EbayCredentialsError, match="EBAY_ENV"):
        EbayClient.from_env()


def test_default_client_is_built_once(monkeypatch):
    monkeypatch.setenv("EBAY_CLIENT_ID", "id")
    monkeypatch.setenv("EBAY_CLIENT_SECRET", "secret")
    first = get_default_client()
    assert get_default_client() is first
    assert first.host == ebay_api.PRODUCTION_HOST


@pytest.mark.parametrize("marketplace, country", [
    ("EBAY_US", "US"), ("EBAY_GB", "GB"), ("EBAY_MOTORS_US", "US"), ("bogus", None), ("", None),
])
def test_marketplace_country(marketplace, country):
    assert marketplace_country(marketplace) == country
