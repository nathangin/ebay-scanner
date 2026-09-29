"""PriceCharting cost to collect the cards of each Pokemon in favorites.json (tcg_cards groups).

Uses PriceCharting's official API, which needs a paid subscription:
put your 40-character API token in PRICECHARTING_TOKEN (environment or .env).
API docs: https://www.pricecharting.com/api-documentation

For each Pokemon the script searches /api/products (the API returns the first
20 matches), keeps Pokemon cards whose name contains the Pokemon, and reads
each card's prices from /api/product.  PriceCharting covers English, Japanese
and other regional printings.  The API allows one call per second, so product
details are cached for 24 hours in pricecharting_cache.json.

Run: python pc_cost.py
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from typing import Any, Callable

import requests
from dotenv import load_dotenv

from listings import BUNDLE_RE

API_BASE      = "https://www.pricecharting.com/api"
MIN_INTERVAL  = 1.1             # seconds between calls (documented limit: 1 per second)
CACHE_FILE    = "pricecharting_cache.json"
CACHE_TTL     = 24 * 3600
FAVORITES_FILE = "favorites.json"

# What PriceCharting's price fields mean for trading cards (per the API docs).
# Values are integers in pennies (USD).
CARD_PRICE_FIELDS = {
    "ungraded":  "loose-price",
    "grade_7":   "cib-price",           # Grade 7 / 7.5
    "grade_8":   "new-price",           # Grade 8 / 8.5
    "grade_9":   "graded-price",
    "grade_9_5": "box-only-price",
    "psa_10":    "manual-only-price",
    "bgs_10":    "bgs-10-price",
    "cgc_10":    "condition-17-price",
    "sgc_10":    "condition-18-price",
}

SETUP_MESSAGE = (
    "PRICECHARTING_TOKEN is not set.\n"
    "pc_cost.py uses PriceCharting's official API, which requires a paid subscription.\n"
    "Copy the API token from your PriceCharting subscription page ('API/Download')\n"
    "into .env as PRICECHARTING_TOKEN=...  See the 'PriceCharting' section of README.md."
)


class PriceChartingError(RuntimeError):
    """The API answered with status "error" or an unreadable response."""


def pennies_to_dollars(value: Any) -> float | None:
    """PriceCharting prices are integer pennies; 0 / missing means no price."""
    try:
        cents = float(value)
    except (TypeError, ValueError):
        return None
    return round(cents / 100, 2) if cents > 0 else None


def parse_card_prices(product: dict[str, Any]) -> dict[str, float | None]:
    """Dollar prices for every trading-card price field of a product."""
    return {key: pennies_to_dollars(product.get(field)) for key, field in CARD_PRICE_FIELDS.items()}


def has_prices(product: dict[str, Any]) -> bool:
    return any(field in product for field in CARD_PRICE_FIELDS.values())


def is_pokemon_card(product: dict[str, Any]) -> bool:
    """True for Pokemon TCG products (genre "Pokemon Card"; else a "Pokemon ..." set name)."""
    genre = (product.get("genre") or "").lower()
    if genre:
        return "pokemon" in genre and "card" in genre
    return (product.get("console-name") or "").lower().startswith("pokemon")


def name_matches(product_name: str, pokemon_name: str) -> bool:
    """Pokemon name appears as a whole word ("Mew" does not match "Mewtwo")."""
    return re.search(r"\b" + re.escape(pokemon_name.lower()) + r"\b", product_name.lower()) is not None


class PriceChartingClient:
    """Minimal PriceCharting API client that keeps to the 1 call/second limit."""

    def __init__(
        self,
        token: str,
        *,
        session: requests.Session | None = None,
        timeout: float = 20.0,
        min_interval: float = MIN_INTERVAL,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.token        = token
        self.session      = session or requests.Session()
        self.timeout      = timeout
        self.min_interval = min_interval
        self._clock       = clock
        self._sleep       = sleep
        self._last_call: float | None = None

    def search(self, query: str) -> list[dict]:
        """GET /api/products -- up to 20 products (id, product-name, console-name)."""
        return self._get("products", q=query).get("products") or []

    def product(self, product_id: str) -> dict:
        """GET /api/product -- one product with its price fields."""
        return self._get("product", id=product_id)

    def _get(self, endpoint: str, **params: Any) -> dict:
        if self._last_call is not None:
            wait = self.min_interval - (self._clock() - self._last_call)
            if wait > 0:
                self._sleep(wait)
        self._last_call = self._clock()

        resp = self.session.get(f"{API_BASE}/{endpoint}", params={"t": self.token, **params},
                                timeout=self.timeout)
        try:
            data = resp.json()
        except ValueError as exc:
            raise PriceChartingError(f"/api/{endpoint}: HTTP {resp.status_code}, response is not JSON") from exc
        if not isinstance(data, dict) or data.get("status") != "success":
            message = data.get("error-message") if isinstance(data, dict) else None
            raise PriceChartingError(f"/api/{endpoint}: {message or f'HTTP {resp.status_code}'}")
        return data


# ── cache ──────────────────────────────────────────────────────────────────────

def load_cache(path: str = CACHE_FILE) -> dict[str, dict]:
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def save_cache(cache: dict[str, dict], path: str = CACHE_FILE) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(cache, fh)


def _product_details(client: PriceChartingClient, product_id: str, cache: dict[str, dict]) -> dict:
    entry = cache.get(product_id)
    if entry and time.time() - entry.get("fetched_at", 0) < CACHE_TTL:
        return entry["product"]
    product = client.product(product_id)
    cache[product_id] = {"fetched_at": time.time(), "product": product}
    return product


# ── lookup ─────────────────────────────────────────────────────────────────────

def cards_for_pokemon(client: PriceChartingClient, pokemon_name: str,
                      cache: dict[str, dict]) -> list[dict]:
    """Priced Pokemon cards on PriceCharting whose name contains pokemon_name."""
    cards = []
    for hit in client.search(pokemon_name):
        name = hit.get("product-name") or ""
        if not hit.get("id") or not name_matches(name, pokemon_name) or BUNDLE_RE.search(name):
            continue
        if not is_pokemon_card(hit):
            continue
        product = hit if has_prices(hit) else _product_details(client, str(hit["id"]), cache)
        if not is_pokemon_card(product):
            continue
        prices = parse_card_prices(product)
        cards.append({
            "id":    str(product.get("id") or hit["id"]),
            "title": product.get("product-name") or name,
            "set":   product.get("console-name") or hit.get("console-name") or "",
            **prices,
        })
    return cards


def get_pokemon_from_favorites(path: str = FAVORITES_FILE) -> list[tuple[str, str]]:
    """(group_name, pokemon_name) for every card in the tcg_cards groups."""
    with open(path, encoding="utf-8") as fh:
        favorites = json.load(fh)

    entries = []
    seen    = set()
    for group in favorites:
        if group.get("type") != "tcg_cards":
            continue
        for card in group["cards"]:
            name = card["name"]
            if name not in seen:
                entries.append((group["name"], name))
                seen.add(name)
    return entries


def _money(value: float | None) -> str:
    return f"${value:.2f}" if value else "—"


# ── main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    load_dotenv()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    token = os.getenv("PRICECHARTING_TOKEN", "").strip()
    if not token:
        print(SETUP_MESSAGE)
        sys.exit(1)

    client  = PriceChartingClient(token)
    cache   = load_cache()
    entries = get_pokemon_from_favorites()

    print(f"\n{'='*72}")
    print("  PriceCharting — Cost to Collect (favorites, ungraded prices)")
    print(f"{'='*72}\n")
    print("  Fetching from the PriceCharting API (1 call per second)...\n")

    all_results = []
    try:
        for group_name, pokemon_name in entries:
            print(f"  {pokemon_name:<20} [{group_name}]...", end="", flush=True)
            try:
                cards = cards_for_pokemon(client, pokemon_name, cache)
            except (PriceChartingError, requests.RequestException) as exc:
                print(f"  ERROR: {exc}")
                all_results.append((group_name, pokemon_name, [], str(exc)))
                continue
            priced = [c for c in cards if c["ungraded"] is not None]
            total  = sum(c["ungraded"] for c in priced)
            miss   = len(cards) - len(priced)
            note   = f"  ({miss} no price)" if miss else ""
            print(f"  {len(cards):>3} cards | {len(priced):>3} priced | ${total:>7.2f}{note}")
            all_results.append((group_name, pokemon_name, cards, None))
    finally:
        save_cache(cache)

    # ── summary table ──────────────────────────────────────────────────────────
    print(f"\n{'='*72}")
    print(f"  {'Pokemon':<22} {'Group':<20} {'Cards':>5} {'Priced':>6} {'Total Cost':>11}")
    print(f"{'='*72}")

    grand_total = 0.0
    for group_name, pokemon_name, cards, err in all_results:
        if err:
            print(f"  {pokemon_name:<22} {group_name:<20}  -- error --")
            continue
        priced = [c for c in cards if c["ungraded"] is not None]
        total  = sum(c["ungraded"] for c in priced)
        miss   = len(cards) - len(priced)
        grand_total += total
        note   = f"  ({miss} no price)" if miss else ""
        print(f"  {pokemon_name:<22} {group_name:<20} {len(cards):>5} {len(priced):>6}  ${total:>8.2f}{note}")

    print(f"\n  Grand total (ungraded price per card): ${grand_total:,.2f}")
    print("  Coverage: the first 20 PriceCharting search results per Pokemon.")
    print(f"{'='*72}\n")

    # ── detailed breakdown ─────────────────────────────────────────────────────
    print("  Detailed breakdown (sorted by ungraded price, highest first):\n")
    for group_name, pokemon_name, cards, err in all_results:
        if err or not cards:
            continue
        total = sum(c["ungraded"] for c in cards if c["ungraded"] is not None)
        print(f"  {pokemon_name} [{group_name}] — ${total:.2f} total")
        print(f"  {'Title':<45} {'Set':<28} {'Ungraded':>9} {'PSA 10':>9}")
        print(f"  {'-'*94}")
        for c in sorted(cards, key=lambda x: x["ungraded"] or 0, reverse=True):
            print(f"  {c['title'][:45]:<45} {c['set'][:28]:<28} "
                  f"{_money(c['ungraded']):>9} {_money(c['psa_10']):>9}")
        print()


if __name__ == "__main__":
    main()
