"""NM market price lookup via pokemontcg.io (TCGPlayer, CardMarket fallback) — no API key required.

Lookup priority:
  1. Direct card ID  -> GET /v2/cards/{id}      (exact, used for 'exact' / 'set+name' matches)
  2. name + set + number -> query               (fallback)
  3. name + set          -> query
  4. name only           -> median across all printings

TCGPlayer prices are USD for English cards.  The CardMarket fallback is in EUR
and is used as-is (see "Limitations" in README.md).
"""
from __future__ import annotations

import statistics
import time
from typing import Any

import requests

_BASE_URL = "https://api.pokemontcg.io/v2/cards"

# TCGPlayer variant keys in order of preference.  Unlimited prints come before
# 1st Edition ones: a WOTC holo has no plain "holofoil" price, and falling
# through to "1stEditionHolofoil" would value an ordinary unlimited copy at
# the (much higher) 1st Edition price.
_VARIANTS = (
    "holofoil", "normal", "reverseHolofoil", "unlimitedHolofoil",
    "1stEditionHolofoil", "1stEditionNormal", "promo",
)


def nm_price_from_card(card: dict[str, Any]) -> float | None:
    """NM market price from a pokemontcg.io card object, or None if it has no prices."""
    tcg = (card.get("tcgplayer") or {}).get("prices") or {}
    for variant in _VARIANTS:
        entry = tcg.get(variant)
        if entry:
            market = entry.get("market") or entry.get("mid")
            if market and market > 0:
                return float(market)
    cm  = (card.get("cardmarket") or {}).get("prices") or {}
    avg = cm.get("averageSellPrice") or cm.get("trendPrice")
    if avg and avg > 0:
        return float(avg)
    return None


class PriceLookup:
    """Cached NM price lookups (default cache lifetime 15 minutes)."""

    def __init__(self, cache_ttl: float = 900):
        self._cache: dict[str, tuple[float, float | None]] = {}
        self._cache_ttl = cache_ttl

    def get_nm_price(
        self,
        card_id: str | None = None,
        card_name: str | None = None,
        set_name: str | None = None,
        card_number: str | None = None,
    ) -> float | None:
        """Return the NM market price for a card.

        If card_id is provided (from an 'exact' or 'set+name' match), that exact
        printing is fetched.  Otherwise a name-based query is used for
        'name'-confidence matches.  Network failures return None and are not
        cached, so the next listing retries.
        """
        if card_id:
            key = card_id
        elif card_name:
            key = f"{card_name.lower()}|{(set_name or '').lower()}|{card_number or ''}"
        else:
            return None

        cached = self._cache.get(key)
        if cached and time.time() - cached[0] < self._cache_ttl:
            return cached[1]

        try:
            if card_id:
                price = self._fetch_by_id(card_id)
            else:
                price = self._fetch_by_name(card_name, set_name, card_number)
        except (requests.RequestException, ValueError):
            return None

        self._cache[key] = (time.time(), price)
        return price

    def _fetch_by_id(self, card_id: str) -> float | None:
        resp = requests.get(
            f"{_BASE_URL}/{card_id}",
            headers={"Accept": "application/json"},
            timeout=20,
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        card = resp.json().get("data")
        return nm_price_from_card(card) if card else None

    def _fetch_by_name(self, card_name: str, set_name: str | None = None,
                       card_number: str | None = None) -> float | None:
        q_parts = [f'name:"{card_name}"']
        if set_name:
            q_parts.append(f'set.name:"{set_name}"')
        if card_number:
            q_parts.append(f'number:"{card_number}"')

        cards = self._query(" ".join(q_parts))

        if not cards and card_number and set_name:
            cards = self._query(f'name:"{card_name}" set.name:"{set_name}"')
        if not cards and set_name:
            cards = self._query(f'name:"{card_name}"')

        prices = [p for p in (nm_price_from_card(c) for c in cards) if p]
        return statistics.median(prices) if prices else None

    @staticmethod
    def _query(q: str) -> list[dict]:
        resp = requests.get(
            _BASE_URL,
            params={"q": q, "pageSize": 10},
            headers={"Accept": "application/json"},
            timeout=20,
        )
        resp.raise_for_status()
        return resp.json().get("data", [])
