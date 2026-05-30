"""For every Pokemon species, sum the cheapest ungraded price of all its cards.

Groups "Dark Vileplume", "Erika's Vileplume ex", "Vileplume VMAX" etc. all under "Vileplume".
Excludes trainer/energy/stadium cards using the supertype field.

First run (or --refresh): fetches all ~20k cards from pokemontcg.io (~2-3 min).
Subsequent runs load instantly from price_cache.json.

Run: py cost_to_collect.py                 full table, all Pokemon
     py cost_to_collect.py pikachu          detail view for one Pokemon
     py cost_to_collect.py --refresh        re-fetch prices from API
     py cost_to_collect.py --fill-missing   search eBay sold listings for cards
                                            that have no TCGPlayer/CardMarket price
                                            (slow — a few seconds per card)
"""
import csv
import io
import json
import os
import re
import sys
import time
from collections import defaultdict

import requests

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

CACHE_FILE      = "price_cache.json"
FILL_CACHE_FILE = "price_fill_cache.json"   # eBay-sold prices for API-missing cards
OUTPUT_CSV      = "cost_to_collect.csv"
BASE_URL        = "https://api.pokemontcg.io/v2/cards"
PAGE_SIZE       = 250

_fill_cache: dict = {}   # card_id -> price (None = searched, not found)

# All ungraded TCGPlayer variant keys — graded prices are not in this API
_VARIANTS = (
    "normal", "holofoil", "reverseHolofoil",
    "1stEditionNormal", "1stEditionHolofoil",
    "unlimitedHolofoil", "promo",
)


# ── price helper ───────────────────────────────────────────────────────────────

def _load_fill_cache():
    global _fill_cache
    if os.path.exists(FILL_CACHE_FILE):
        with open(FILL_CACHE_FILE, "r", encoding="utf-8") as fh:
            _fill_cache = json.load(fh)


def _save_fill_cache():
    with open(FILL_CACHE_FILE, "w", encoding="utf-8") as fh:
        json.dump(_fill_cache, fh)


def _cheapest_price(card):
    """Lowest ungraded price for this card, trying every available source."""
    # 1. TCGPlayer market or mid price (any variant)
    tcg = card.get("tcgplayer", {}).get("prices", {})
    prices = []
    for variant in _VARIANTS:
        entry = tcg.get(variant)
        if entry:
            p = entry.get("market") or entry.get("mid") or entry.get("low")
            if p and p > 0:
                prices.append(float(p))
    if prices:
        return min(prices)

    # 2. CardMarket — try every available price field (already in cache, no extra request)
    cm = card.get("cardmarket", {}).get("prices", {})
    for field in ("averageSellPrice", "trendPrice", "avg30", "avg7", "avg1",
                  "lowPrice", "suggestedPrice"):
        v = cm.get(field)
        if v and v > 0:
            return float(v)

    # 3. eBay sold cache (populated by --fill-missing)
    card_id = card.get("id")
    if card_id and card_id in _fill_cache:
        p = _fill_cache[card_id]
        return float(p) if p is not None else None

    return None
    # Still None = no sales data anywhere: very old promos, cards never listed
    # on TCGPlayer/CardMarket, or so rare eBay searches also come up empty.


# ── name extraction ────────────────────────────────────────────────────────────

# Possessives: "Erika's ", "N's ", "Team Rocket's ", "Larry's " …
_POSSESSIVE_RE = re.compile(r"^.+?'s\s+")

# Prefixes that describe a TCG mechanic, not the species
_PREFIX_RE = re.compile(
    r"^(?:Dark|Light|Shadow|Shining|Rocket's|Team\s+Rocket's)\s+"
    r"|^(?:Mega)\s+"
    r"|^M\s+(?=[A-Z])",   # "M Charizard-EX" — M before a capital
)

# Suffixes to strip (applied repeatedly until stable)
_SUFFIX_RE = re.compile(
    r"\s+(?:VMAX|VSTAR|BREAK|Prime|LEGEND|Prism\s+Star)\s*$"
    r"|\s+(?:ex|EX|GX|V)\s*$"
    r"|-(?:EX|GX|VMAX|VSTAR|V)\s*$"
    r"|\s+LV\.[0-9X]+\s*$"
    r"|\s+(?:SP|GL|FB|4|G|C)\s*$"
    r"|\s+[★☆◇♦◆].*$"     # star/prism symbols and anything after
    r"|\s+δ.*$"              # delta species
    r"|\s+[A-Z]\s*$",        # single-letter form: Charizard X, Charizard Y, Unown A
    re.UNICODE,
)


def extract_base_names(card_name):
    """Return list of base Pokemon species names from a card name.

    "Erika's Vileplume ex"        -> ["Vileplume"]
    "M Charizard-EX"              -> ["Charizard"]
    "Latias & Latios-GX"          -> ["Latias", "Latios"]
    "Pikachu VMAX"                -> ["Pikachu"]
    "Mewtwo *"                    -> ["Mewtwo"]
    "Dark Charizard"              -> ["Charizard"]
    "Championship Arena"          -> ["Championship Arena"]  (filtered by supertype)
    """
    parts = [p.strip() for p in card_name.split(" & ")]
    result = []
    for part in parts:
        name = part
        # Strip possessive prefix
        name = _POSSESSIVE_RE.sub("", name)
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

def _load_cache():
    if not os.path.exists(CACHE_FILE):
        return [], False, False
    with open(CACHE_FILE, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if isinstance(data, list):
        return data, False, False   # old bare-list format
    cards      = data.get("cards", [])
    complete   = data.get("complete", False)
    has_super  = data.get("has_supertype", False)
    return cards, complete, has_super


def _save_cache(cards, complete=False, has_supertype=False):
    with open(CACHE_FILE, "w", encoding="utf-8") as fh:
        json.dump({
            "complete":      complete,
            "has_supertype": has_supertype,
            "cards":         cards,
        }, fh)


def _fetch_page(page):
    last_exc = None
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
        except Exception as exc:
            last_exc = exc
            wait = 5 * (attempt + 1)
            print(f"\n  Page {page} error (attempt {attempt+1}/3, retry in {wait}s): {exc}", flush=True)
            time.sleep(wait)
    raise last_exc


def fetch_all_cards(force_refresh=False):
    cached, complete, has_supertype = _load_cache()

    if complete and has_supertype and not force_refresh:
        print(f"Loading cached data ({len(cached):,} cards)  (pass --refresh to update)...")
        return cached

    if complete and not has_supertype and not force_refresh:
        print(
            f"Cache has {len(cached):,} cards but is missing the supertype field needed to\n"
            f"filter out trainer/energy cards.  Re-fetching now to get clean data...\n"
        )
        # Fall through to re-fetch
        cached = []

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
        except Exception as exc:
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


# ── eBay sold fill ─────────────────────────────────────────────────────────────

def fill_missing_prices(all_cards):
    """For cards with no API price, look up recent eBay sold listings."""
    from scraper import fetch_sold_price

    candidates = [
        c for c in all_cards
        if c.get("supertype") == "Pokémon"
        and _cheapest_price(c) is None
        and c.get("id") not in _fill_cache
    ]

    if not candidates:
        print("  No missing prices to fill — all Pokemon cards already have data.")
        return

    print(f"  Searching eBay sold listings for {len(candidates)} unpriced cards...")
    print("  (This takes a few seconds per card. Press Ctrl+C to stop and save progress.)\n")
    found = 0

    try:
        for i, card in enumerate(candidates):
            name    = card.get("name", "")
            set_nm  = card.get("set", {}).get("name", "")
            number  = card.get("number", "")
            card_id = card.get("id", "")

            search = f"{name} {set_nm} {number} pokemon card"
            print(f"  [{i+1}/{len(candidates)}] {name} ({set_nm} #{number}) ...", end="", flush=True)

            price = fetch_sold_price(search)
            _fill_cache[card_id] = price   # None = searched, nothing found
            if price is not None:
                found += 1
                print(f" ${price:.2f}")
            else:
                print(" no sold data")

            if (i + 1) % 20 == 0:
                _save_fill_cache()
                print(f"  -- progress saved ({found} found so far) --")

            time.sleep(1)

    except KeyboardInterrupt:
        print("\n  Interrupted.")

    _save_fill_cache()
    print(f"\n  Done — filled {found}/{len(candidates)} prices.  Saved to {FILL_CACHE_FILE}")


# ── analysis ───────────────────────────────────────────────────────────────────

def build_summary(all_cards):
    """Group cards by base Pokemon species name, filter to Pokemon only."""
    by_base = defaultdict(list)

    for card in all_cards:
        supertype = card.get("supertype")
        # If supertype available, filter strictly; otherwise best-effort
        if supertype is not None and supertype != "Pokémon":
            continue

        base_names = extract_base_names(card.get("name", "").strip())
        for base in base_names:
            by_base[base].append(card)

    rows = []
    for name, cards in by_base.items():
        prices   = [_cheapest_price(c) for c in cards]
        priced   = [p for p in prices if p is not None]
        unpriced = len(cards) - len(priced)
        total    = sum(priced)
        rows.append({
            "name":         name,
            "total_cards":  len(cards),
            "priced_cards": len(priced),
            "unpriced":     unpriced,
            "total_cost":   round(total, 2),
        })

    rows.sort(key=lambda r: r["total_cost"])
    return rows, by_base


# ── output ─────────────────────────────────────────────────────────────────────

def print_table(rows, top_n=30):
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
        print(f"  {r['name']:<30} {r['total_cards']:>5} {r['priced_cards']:>6}  ${r['total_cost']:>8.2f}")

    print(f"\n  -- Cheapest to collect (includes partial pricing) --")
    print(f"  {'-'*69}")
    for r in any_priced[:top_n]:
        note = f"  ({r['unpriced']} no TCG data)" if r["unpriced"] else ""
        print(f"  {r['name']:<30} {r['total_cards']:>5} {r['priced_cards']:>6}  ${r['total_cost']:>8.2f}{note}")

    print(f"\n  -- Most expensive to collect --")
    print(f"  {'-'*69}")
    for r in reversed(any_priced[-top_n:]):
        note = f"  ({r['unpriced']} no TCG data)" if r["unpriced"] else ""
        print(f"  {r['name']:<30} {r['total_cards']:>5} {r['priced_cards']:>6}  ${r['total_cost']:>8.2f}{note}")

    total_cards = sum(r["total_cards"] for r in rows)
    print(f"\n  {len(rows)} Pokemon species | {total_cards:,} cards")
    if no_price_ct:
        print(f"  {no_price_ct} Pokemon excluded (zero pricing data — old promos / no TCGPlayer sales)")
    print(f"  All prices are ungraded TCGPlayer market price (cheapest variant per card)")


def print_pokemon_detail(by_base, name_filter):
    norm    = name_filter.lower()
    matches = {n: cards for n, cards in by_base.items() if norm in n.lower()}

    if not matches:
        print(f"\n  No Pokemon found matching '{name_filter}'")
        return

    for base_name, cards in sorted(matches.items()):
        prices   = [_cheapest_price(c) for c in cards]
        priced   = [p for p in prices if p is not None]
        total    = sum(priced)
        print(f"\n  {base_name} — ${total:.2f} total ({len(cards)} cards, {len(priced)} priced)")
        print(f"  {'Original Card Name':<38} {'Set':<30} {'#':>5} {'Price':>8}")
        print(f"  {'-'*84}")
        paired = list(zip(cards, prices))
        paired.sort(key=lambda x: x[0].get("set", {}).get("releaseDate", ""))
        for card, price in paired:
            orig  = card.get("name", "")[:38]
            sname = card.get("set", {}).get("name", "")[:30]
            num   = card.get("number", "")
            pstr  = f"${price:.2f}" if price else "— (no TCG data)"
            print(f"  {orig:<38} {sname:<30} {num:>5} {pstr:>8}")


# ── main ───────────────────────────────────────────────────────────────────────

def main():
    args          = sys.argv[1:]
    force_refresh = "--refresh" in args
    do_fill       = "--fill-missing" in args
    name_filter   = next((a for a in args if not a.startswith("--")), None)

    _load_fill_cache()
    all_cards        = fetch_all_cards(force_refresh=force_refresh)

    if do_fill:
        fill_missing_prices(all_cards)

    rows, by_base    = build_summary(all_cards)

    if name_filter:
        print_pokemon_detail(by_base, name_filter)
    else:
        print_table(rows, top_n=30)
        with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(
                fh,
                fieldnames=["name", "total_cards", "priced_cards", "unpriced", "total_cost"],
            )
            writer.writeheader()
            writer.writerows(rows)
        print(f"\n  Full list saved to {OUTPUT_CSV}")


if __name__ == "__main__":
    main()
