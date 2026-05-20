"""TCGPlayer market price lookup via pokemontcg.io — no API key required.

Lookup priority:
  1. Direct card ID  → GET /v2/cards/{id}      (exact, used when matcher returns 'exact' or 'set+name')
  2. name + set + number → query               (fallback)
  3. name + set          → query
  4. name only           → median across all printings
"""
import statistics
import time

import requests

_BASE_URL = "https://api.pokemontcg.io/v2/cards"
_VARIANTS = ("holofoil", "normal", "reverseHolofoil", "1stEditionHolofoil", "promo")


def _nm_price_from_card(card):
    tcg = card.get("tcgplayer", {}).get("prices", {})
    for variant in _VARIANTS:
        entry = tcg.get(variant)
        if entry:
            market = entry.get("market") or entry.get("mid")
            if market and market > 0:
                return float(market)
    cm  = card.get("cardmarket", {}).get("prices", {})
    avg = cm.get("averageSellPrice") or cm.get("trendPrice")
    if avg and avg > 0:
        return float(avg)
    return None


class PriceLookup:
    def __init__(self, cache_ttl=900):
        self._cache     = {}
        self._cache_ttl = cache_ttl

    def get_nm_price(self, card_id=None, card_name=None, set_name=None, card_number=None):
        """Return the NM market price for a card.

        If card_id is provided (from an 'exact' or 'set+name' match), fetches that
        specific printing directly — no ambiguity.  Falls back to name-based query
        for 'name'-confidence matches.
        """
        if card_id:
            cached = self._cache.get(card_id)
            if cached and time.time() - cached["ts"] < self._cache_ttl:
                return cached["price"]
            price = self._fetch_by_id(card_id)
            self._cache[card_id] = {"ts": time.time(), "price": price}
            return price

        if not card_name:
            return None

        key    = f"{card_name.lower()}|{(set_name or '').lower()}|{card_number or ''}"
        cached = self._cache.get(key)
        if cached and time.time() - cached["ts"] < self._cache_ttl:
            return cached["price"]
        price = self._fetch_by_name(card_name, set_name, card_number)
        self._cache[key] = {"ts": time.time(), "price": price}
        return price

    def _fetch_by_id(self, card_id):
        try:
            resp = requests.get(
                f"{_BASE_URL}/{card_id}",
                headers={"Accept": "application/json"},
                timeout=20,
            )
            resp.raise_for_status()
            card = resp.json().get("data")
            return _nm_price_from_card(card) if card else None
        except Exception:
            return None

    def _fetch_by_name(self, card_name, set_name=None, card_number=None):
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

        prices = [p for p in (_nm_price_from_card(c) for c in cards) if p]
        return statistics.median(prices) if prices else None

    def _query(self, q):
        try:
            resp = requests.get(
                _BASE_URL,
                params={"q": q, "pageSize": 10},
                headers={"Accept": "application/json"},
                timeout=20,
            )
            resp.raise_for_status()
            return resp.json().get("data", [])
        except Exception:
            return []
