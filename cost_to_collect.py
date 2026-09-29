"""For every Pokemon species, sum the cheapest ungraded price of all its cards.

Groups "Dark Vileplume", "Erika's Vileplume ex", "Vileplume VMAX" etc. all under "Vileplume".
Excludes trainer/energy/stadium cards using the supertype field.

First run (or --refresh): fetches all ~20k cards from pokemontcg.io (~2-3 min).
Subsequent runs load instantly from price_cache.json.

Run: python cost_to_collect.py                 full table, all Pokemon
     python cost_to_collect.py pikachu         detail view for one Pokemon
     python cost_to_collect.py --refresh       re-fetch prices from the API
     python cost_to_collect.py --fill-missing  for cards with no TCGPlayer/CardMarket
                                               price, use current eBay ASKING prices
                                               (median of the cheapest Buy It Now
                                               listings — not sold prices)
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

CACHE_FILE        = "price_cache.json"
ASKING_CACHE_FILE = "asking_price_cache.json"   # eBay asking prices for API-missing cards
OUTPUT_CSV        = "cost_to_collect.csv"
BASE_URL          = "https://api.pokemontcg.io/v2/cards"
PAGE_SIZE         = 250

ASKING_PRICE_NOTE = (
    "eBay asking prices: median of the cheapest current Buy It Now listings for "
    "ungraded single cards (Browse API). These are NOT sold prices."
)

_asking_cache: dict[str, float | None] = {}   # card_id -> price (None = searched, not found)

# All ungraded TCGPlayer variant keys — graded prices are not in this API
_VARIANTS = (
    "normal", "holofoil", "reverseHolofoil",
    "1stEditionNormal", "1stEditionHolofoil",
    "unlimitedHolofoil", "promo",
)


# ── price helpers ──────────────────────────────────────────────────────────────

def _load_asking_cache() -> None:
    global _asking_cache
    _asking_cache = {}
    if os.path.exists(ASKING_CACHE_FILE):
        with open(ASKING_CACHE_FILE, "r", encoding="utf-8") as fh:
            _asking_cache = json.load(fh).get("asking_prices", {})


def _save_asking_cache() -> None:
    with open(ASKING_CACHE_FILE, "w", encoding="utf-8") as fh:
        json.dump({
            "note":          ASKING_PRICE_NOTE,
            "updated_at":    datetime.now(timezone.utc).isoformat(),
            "asking_prices": _asking_cache,
        }, fh, indent=1)


def price_with_source(card: dict) -> tuple[float | None, str | None]:
    """Lowest ungraded price for this card and where it came from.

    Source is "tcgplayer", "cardmarket", "ebay_asking" (filled in by
    --fill-missing) or None when there is no price anywhere.
    """
    # 1. TCGPlayer market or mid price (any variant)
    tcg = (card.get("tcgplayer") or {}).get("prices") or {}
    prices = []
    for variant in _VARIANTS:
        entry = tcg.get(variant)
        if entry:
            p = entry.get("market") or entry.get("mid") or entry.get("low")
            if p and p > 0:
                prices.append(float(p))
    if prices:
        return min(prices), "tcgplayer"

    # 2. CardMarket — try every available price field (already in cache, no extra request)
    cm = (card.get("cardmarket") or {}).get("prices") or {}
    for field in ("averageSellPrice", "trendPrice", "avg30", "avg7", "avg1",
                  "lowPrice", "suggestedPrice"):
        v = cm.get(field)
        if v and v > 0:
            return float(v), "cardmarket"

    # 3. eBay asking price (populated by --fill-missing)
    p = _asking_cache.get(card.get("id") or "")
    if p is not None:
        return float(p), "ebay_asking"

    # No price anywhere: very old promos, cards never listed on TCGPlayer /
    # CardMarket, or so rare that nothing is for sale on eBay either.
    return None, None


def _cheapest_price(card: dict) -> float | None:
    return price_with_source(card)[0]


# ── name extraction ────────────────────────────────────────────────────────────

# Possessives: "Erika's ", "N's ", "Team Rocket's ", "Larry's " …
_POSSESSIVE_RE = re.compile(r"^.+?'s\s+")

# Prefixes that describe a TCG mechanic, not the species
_PREFIX_RE = re.compile(
    r"^(?:Dark|Light|Shadow|Shining|Radiant|Rocket's|Team\s+Rocket's)\s+"
    r"|^(?:Mega)\s+"
    r"|^M\s+(?=[A-Z])",   # "M Charizard-EX" — M before a capital
)

# Suffixes to strip (applied repeatedly until stable)
_SUFFIX_RE = re.compile(
    r"\s+(?:VMAX|VSTAR|BREAK|Prime|LEGEND|Prism\s+Star)\s*$"
    r"|\s+(?:ex|EX|GX|V)\s*$"
    r"|-(?:EX|GX|VMAX|VSTAR|V)\s*$"
    r"|\s+LV\.[0-9X]+\s*$"
    r"|\s+(?:SP|GL|FB|E4|4|G|C)\s*$"
    r"|\s+[★☆◇♦◆].*$"     # star/prism symbols and anything after
    r"|\s+δ.*$"              # delta species
    r"|\s+[A-Z]\s*$",        # single-letter form: Charizard X, Charizard Y, Unown A
    re.UNICODE,
)


def extract_base_names(card_name: str) -> list[str]:
    """Return list of base Pokemon species names from a card name.

    "Erika's Vileplume ex"        -> ["Vileplume"]
    "M Charizard-EX"              -> ["Charizard"]
    "Latias & Latios-GX"          -> ["Latias", "Latios"]
    "Pikachu VMAX"                -> ["Pikachu"]
    "Mewtwo ★"                    -> ["Mewtwo"]
    "Dark Charizard"              -> ["Charizard"]
    "Championship Arena"          -> ["Championship Arena"]  (filtered by supertype)
    """
    parts = [p.strip() for p in card_name.split(" & ")]
    result = []
    for part in parts:
        name = _POSSESSIVE_RE.sub("", part)
        # Strip mechanic prefixes (may need multiple passes)
        for _ in range(3):
            prev = name
            name = _PREFIX_RE.sub("", name).strip()
            if name == prev:
                break
        # Strip mechanic suffixes (may need multiple passes)
        for _ in range(6):
            prev = name
            name = _SUFFIX_RE.sub("", name).strip()
            if name == prev:
                break
        if name:
            result.append(name)
    return result


# ── cache / fetch ──────────────────────────────────────────────────────────────

def _load_cache() -> tuple[list[dict], bool, bool]:
    if not os.path.exists(CACHE_FILE):
        return [], False, False
    with open(CACHE_FILE, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if isinstance(data, list):
        return data, False, False   # old bare-list format
    return data.get("cards", []), data.get("complete", False), data.get("has_supertype", False)


def _save_cache(cards: list[dict], complete: bool = False, has_supertype: bool = False) -> None:
    with open(CACHE_FILE, "w", encoding="utf-8") as fh:
        json.dump({
            "complete":      complete,
            "has_supertype": has_supertype,
            "cards":         cards,
        }, fh)


def _fetch_page(page: int) -> tuple[list[dict], int]:
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            resp = requests.get(
                BASE_URL,
                params={
                    "select":   "id,name,number,set,supertype,tcgplayer,cardmarket",
                    "pageSize": PAGE_SIZE,
                    "page":     page,
                },
                headers={"Accept": "application/json"},
                timeout=60,
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("data", []), data.get("totalCount", 0)
        except (requests.RequestException, ValueError) as exc:
            last_exc = exc
            wait = 5 * (attempt + 1)
            print(f"\n  Page {page} error (attempt {attempt+1}/3, retry in {wait}s): {exc}", flush=True)
            time.sleep(wait)
    raise last_exc


def fetch_all_cards(force_refresh: bool = False) -> list[dict]:
    cached, complete, has_supertype = _load_cache()

    if complete and has_supertype and not force_refresh:
        print(f"Loading cached data ({len(cached):,} cards)  (pass --refresh to update)...")
        return cached

    if complete and not has_supertype and not force_refresh:
        print(
            f"Cache has {len(cached):,} cards but is missing the supertype field needed to\n"
            f"filter out trainer/energy cards.  Re-fetching now to get clean data...\n"
        )
        cached = []   # fall through to a full re-fetch

    if cached and not force_refresh:
        resume_page = len(cached) // PAGE_SIZE + 1
        print(f"Resuming from page {resume_page} ({len(cached):,} cards cached)...")
        all_cards = list(cached)
    else:
        print("Fetching all card prices from pokemontcg.io (takes a few minutes)...")
        all_cards = []
        resume_page = 1

    page  = resume_page
    total = 0

    while True:
        try:
            cards, total = _fetch_page(page)
        except (requests.RequestException, ValueError) as exc:
            print(f"\n  Page {page} failed after 3 attempts: {exc}")
            _save_cache(all_cards, complete=False, has_supertype=False)
            print(f"  Progress saved ({len(all_cards):,} cards). Re-run to resume.")
            return all_cards

        if not cards:
            break

        all_cards.extend(cards)
        print(f"  Page {page}: {len(all_cards):,}/{total:,} cards", end="\r", flush=True)

        if total and len(all_cards) >= total:
            break

        page += 1
        time.sleep(0.5)

    done = (total == 0) or (len(all_cards) >= total)
    _save_cache(all_cards, complete=done, has_supertype=True)
    print(f"\n  Done — {len(all_cards):,} cards fetched.  Saved to {CACHE_FILE}")
    return all_cards


# ── eBay asking-price fill ─────────────────────────────────────────────────────

def fill_missing_prices(all_cards: list[dict]) -> None:
    """For Pokemon cards with no API price, record the current eBay asking price.

    Uses listings.fetch_asking_price: the median of the cheapest current Buy It
    Now listings for the ungraded card.  eBay's Browse API has no sold data, so
    these are asking prices and are labelled that way everywhere.
    """
    from ebay_api import EbayError, get_default_client
    from listings import fetch_asking_price

    try:
        get_default_client()
    except EbayError as exc:
        print(f"  Skipping --fill-missing: {exc}")
        return

    candidates = [
        c for c in all_cards
        if c.get("supertype") == "Pokémon"
        and _cheapest_price(c) is None
        and c.get("id") not in _asking_cache
    ]

    if not candidates:
        print("  No missing prices to fill — all Pokemon cards already have data.")
        return

    print(f"  Looking up eBay asking prices for {len(candidates)} unpriced cards...")
    print("  (One eBay API call per card. Press Ctrl+C to stop and save progress.)\n")
    found = 0

    try:
        for i, card in enumerate(candidates):
            name    = card.get("name", "")
            set_nm  = (card.get("set") or {}).get("name", "")
            number  = card.get("number", "")
            card_id = card.get("id", "")

            print(f"  [{i+1}/{len(candidates)}] {name} ({set_nm} #{number}) ...", end="", flush=True)

            price = fetch_asking_price(f"{name} {set_nm} {number} pokemon card")
            _asking_cache[card_id] = price   # None = searched, nothing listed
            if price is not None:
                found += 1
                print(f" ${price:.2f} asking")
            else:
                print(" nothing listed")

            if (i + 1) % 20 == 0:
                _save_asking_cache()
                print(f"  -- progress saved ({found} found so far) --")

            time.sleep(0.5)

    except KeyboardInterrupt:
        print("\n  Interrupted.")

    _save_asking_cache()
    print(f"\n  Done — filled {found}/{len(candidates)} asking prices.  Saved to {ASKING_CACHE_FILE}")


# ── analysis ───────────────────────────────────────────────────────────────────

def build_summary(all_cards: list[dict]) -> tuple[list[dict], dict[str, list[dict]]]:
    """Group cards by base Pokemon species name, filter to Pokemon only."""
    by_base: dict[str, list[dict]] = defaultdict(list)

    for card in all_cards:
        supertype = card.get("supertype")
        # If supertype available, filter strictly; otherwise best-effort
        if supertype is not None and supertype != "Pokémon":
            continue
        for base in extract_base_names(card.get("name", "").strip()):
            by_base[base].append(card)

    rows = []
    for name, cards in by_base.items():
        priced = [price_with_source(c) for c in cards]
        priced = [(p, src) for p, src in priced if p is not None]
        rows.append({
            "name":          name,
            "total_cards":   len(cards),
            "priced_cards":  len(priced),
            "unpriced":      len(cards) - len(priced),
            "asking_priced": sum(1 for _, src in priced if src == "ebay_asking"),
            "total_cost":    round(sum(p for p, _ in priced), 2),
        })

    rows.sort(key=lambda r: r["total_cost"])
    return rows, by_base


# ── output ─────────────────────────────────────────────────────────────────────

def _notes(r: dict) -> str:
    parts = []
    if r["unpriced"]:
        parts.append(f"{r['unpriced']} no price data")
    if r["asking_priced"]:
        parts.append(f"{r['asking_priced']} eBay asking*")
    return f"  ({', '.join(parts)})" if parts else ""


def print_table(rows: list[dict], top_n: int = 30) -> None:
    fully_priced = [r for r in rows if r["priced_cards"] == r["total_cards"] and r["total_cost"] > 0]
    any_priced   = [r for r in rows if r["total_cost"] > 0]
    no_price_ct  = len(rows) - len(any_priced)

    w = 72
    hdr = f"  {'Pokemon':<30} {'Cards':>5} {'Priced':>6} {'Total Cost':>11}"
    print(f"\n{'='*w}")
    print(hdr)
    print(f"{'='*w}")

    print("  -- Cheapest to collect (all cards priced — most reliable) --")
    print(f"  {'-'*69}")
    for r in fully_priced[:top_n]:
        print(f"  {r['name']:<30} {r['total_cards']:>5} {r['priced_cards']:>6}  ${r['total_cost']:>8.2f}{_notes(r)}")

    print("\n  -- Cheapest to collect (includes partial pricing) --")
    print(f"  {'-'*69}")
    for r in any_priced[:top_n]:
        print(f"  {r['name']:<30} {r['total_cards']:>5} {r['priced_cards']:>6}  ${r['total_cost']:>8.2f}{_notes(r)}")

    print("\n  -- Most expensive to collect --")
    print(f"  {'-'*69}")
    for r in reversed(any_priced[-top_n:]):
        print(f"  {r['name']:<30} {r['total_cards']:>5} {r['priced_cards']:>6}  ${r['total_cost']:>8.2f}{_notes(r)}")

    total_cards = sum(r["total_cards"] for r in rows)
    print(f"\n  {len(rows)} Pokemon species | {total_cards:,} cards")
    if no_price_ct:
        print(f"  {no_price_ct} Pokemon excluded (zero pricing data — old promos / no TCGPlayer sales)")
    print("  Prices are the cheapest ungraded variant per card: TCGPlayer market price,")
    print("  else CardMarket (EUR, used as-is).")
    if any(r["asking_priced"] for r in rows):
        print(f"  * {ASKING_PRICE_NOTE}")


def print_pokemon_detail(by_base: dict[str, list[dict]], name_filter: str) -> None:
    norm    = name_filter.lower()
    matches = {n: cards for n, cards in by_base.items() if norm in n.lower()}

    if not matches:
        print(f"\n  No Pokemon found matching '{name_filter}'")
        return

    any_asking = False
    for base_name, cards in sorted(matches.items()):
        priced = [price_with_source(c) for c in cards]
        total  = sum(p for p, _ in priced if p is not None)
        n_priced = sum(1 for p, _ in priced if p is not None)
        print(f"\n  {base_name} — ${total:.2f} total ({len(cards)} cards, {n_priced} priced)")
        print(f"  {'Original Card Name':<38} {'Set':<30} {'#':>5} {'Price':>10}")
        print(f"  {'-'*86}")
        paired = sorted(zip(cards, priced), key=lambda x: (x[0].get("set") or {}).get("releaseDate", ""))
        for card, (price, source) in paired:
            orig  = card.get("name", "")[:38]
            sname = (card.get("set") or {}).get("name", "")[:30]
            num   = card.get("number", "")
            if price is None:
                pstr = "— (no data)"
            elif source == "ebay_asking":
                pstr = f"${price:.2f}*"
                any_asking = True
            else:
                pstr = f"${price:.2f}"
            print(f"  {orig:<38} {sname:<30} {num:>5} {pstr:>10}")

    if any_asking:
        print(f"\n  * {ASKING_PRICE_NOTE}")


# ── main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    load_dotenv()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(
        description="Total cost to collect every card of each Pokemon (ungraded prices)."
    )
    parser.add_argument("pokemon", nargs="?", help="show the card-by-card detail for one Pokemon")
    parser.add_argument("--refresh", action="store_true", help="re-fetch all prices from pokemontcg.io")
    parser.add_argument("--fill-missing", action="store_true",
                        help="use eBay asking prices (not sold prices) for cards with no API price")
    args = parser.parse_args()

    _load_asking_cache()
    all_cards = fetch_all_cards(force_refresh=args.refresh)

    if args.fill_missing:
        fill_missing_prices(all_cards)

    rows, by_base = build_summary(all_cards)

    if args.pokemon:
        print_pokemon_detail(by_base, args.pokemon)
    else:
        print_table(rows, top_n=30)
        with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(
                fh,
                fieldnames=["name", "total_cards", "priced_cards", "unpriced",
                            "asking_priced", "total_cost"],
            )
            writer.writeheader()
            writer.writerows(rows)
        print(f"\n  Full list saved to {OUTPUT_CSV}  (asking_priced = cards priced from eBay asking prices)")


if __name__ == "__main__":
    main()
