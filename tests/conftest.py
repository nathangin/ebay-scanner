"""Shared test helpers.  All HTTP is faked -- the suite never touches the network."""
from __future__ import annotations

import json
import os
import sys
from typing import Any

import pytest
import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


class FakeResponse:
    """Just enough of requests.Response for the code under test."""

    def __init__(self, status_code: int = 200, payload: Any = None,
                 headers: dict | None = None, text: str | None = None):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}
        self.text = text if text is not None else (json.dumps(payload) if payload is not None else "")

    def json(self) -> Any:
        if self._payload is None:
            raise ValueError("no JSON body")
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


class FakeSession:
    """Records every request and replies from a queue of responses / exceptions."""

    def __init__(self, responses: list | None = None):
        self.responses = list(responses or [])
        self.calls: list[dict] = []

    def queue(self, *responses: Any) -> "FakeSession":
        self.responses.extend(responses)
        return self

    def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append({"method": method, "url": url, **kwargs})
        if not self.responses:
            raise AssertionError(f"unexpected request: {method} {url}")
        nxt = self.responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        return self.request("GET", url, **kwargs)


class FakeClock:
    """Manually advanced monotonic clock; sleep() advances it and records waits."""

    def __init__(self, start: float = 1000.0):
        self.now = start
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def token_response(token: str = "tok-1", expires_in: int = 7200) -> FakeResponse:
    return FakeResponse(200, {"access_token": token, "expires_in": expires_in,
                              "token_type": "Application Access Token"})


def item_summary(item_id: str = "v1|110000000001|0", title: str = "Charizard 4/102 Base Set Holo NM",
                 price: str = "100.00", shipping: str | None = "5.00",
                 shipping_type: str = "FIXED", **extra: Any) -> dict:
    """An ItemSummary shaped like the Browse API search response."""
    legacy = item_id.split("|")[1]
    item = {
        "itemId": item_id,
        "legacyItemId": legacy,
        "title": title,
        "price": {"value": price, "currency": "USD"},
        "itemWebUrl": f"https://www.ebay.com/itm/{legacy}?hash=item1a2b3c:g:AbC&amdata=enc%3Axyz",
        "image": {"imageUrl": f"https://i.ebayimg.com/images/g/{legacy}/s-l225.jpg"},
        "seller": {"username": "demo_seller", "feedbackPercentage": "99.5", "feedbackScore": 1234},
        "condition": "Ungraded",
        "conditionId": "4000",
        "buyingOptions": ["FIXED_PRICE"],
    }
    if shipping is not None:
        item["shippingOptions"] = [{"shippingCostType": shipping_type,
                                    "shippingCost": {"value": shipping, "currency": "USD"}}]
    item.update(extra)
    return item


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def make_client(clock):
    """Factory for an EbayClient wired to a FakeSession and FakeClock."""
    from ebay_api import EbayClient

    def _make(*responses: Any, **kwargs: Any):
        session = FakeSession(list(responses))
        client = EbayClient("my-app-id", "my-cert-id", session=session,
                            clock=clock, sleep=clock.sleep, **kwargs)
        return client, session

    return _make


@pytest.fixture(autouse=True)
def _no_default_client(monkeypatch):
    """Never let a test build the real default client from the environment."""
    import ebay_api
    monkeypatch.setattr(ebay_api, "_default_client", None)
    for var in ("EBAY_CLIENT_ID", "EBAY_CLIENT_SECRET", "EBAY_MARKETPLACE_ID", "EBAY_ENV",
                "DISCORD_WEBHOOK_URL", "PRICECHARTING_TOKEN"):
        monkeypatch.delenv(var, raising=False)
