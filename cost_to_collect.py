"""For every Pokemon, sum the cheapest price of all its cards.

First run fetches all ~20k cards from pokemontcg.io (~2 min, cached to price_cache.json).
Subsequent runs use the cache (instant). Pass --refresh to force a re-fetch.

Run: py cost_to_collect.py
     py cost_to_collect.py --refresh
     py cost_to_collect.py pikachu          (filter to one Pokemon)
"""
import csv
import io
import json
import os
import sys
import time
from collections import defaultdict

import requests

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

CACHE_FILE  = "price_cache.json"
OUTPUT_CSV  = "cost_to_collect.csv"
BASE_URL    = "https://api.pokemontcg.io/v2/cards"
PAGE_SIZE   = 250

_VARIANTS = (
    "normal", "holofoil", "reverseHolofoil",
    "1stEditionNormal", "1stEditionHolofoil",
    "unlimitedHolofoil", "promo",
)


def _cheapest_price(card):
    """Return the lowest priced variant available for this card."""
    tcg = card.get("tcgplayer", {}).get("prices", {})
    prices = []
    for variant in _VARIANTS:
        entry = tcg.get(variant)
        if entry:
            p = entry.get("market") or entry.get("mid")
            if p and p > 0:
                prices.append(float(p))
    if prices:
        return min(prices)
    cm  = card.get("cardmarket", {}).get("prices", {})
    avg = cm.get("averageSellPrice") or cm.get("trendPrice")
    if avg and avg > 0:
        return float(avg)
    return None


def fetch_all_cards(force_refresh=False):
    if not force_refresh and os.path.exists(CACHE_FILE):
        print(f"Loading cached data from {CACHE_FILE}  (pass --refresh to re-fetch)...")
        with open(CACHE_FILE, "r", encoding="utf-8") as fh:
            return json.load(fh)

    print("Fetching all card prices from pokemontcg.io (~2 min)...")
    all_cards = []
    page      = 1

    while True:
        try:
            resp = requests.get(
                BASE_URL,
                params={
                    "select":   "id,name,number,set,tcgplayer,cardmarket",
                    "pageSize": PAGE_SIZE,
                    "page":     page,
                },
                headers={"Accept": "application/json"},
                timeout=30,
            )
            resp.raise_for_status()
            data  = resp.json()
            cards = data.get("data", [])
            if not cards:
                break
            all_cards.extend(cards)
            total = data.get("totalCount", "?")
            print(f"  Page {page}: {len(all_cards)}/{total} cards", end="\r", flush=True)
            if isinstance(total, int) and len(all_cards) >= total:
                break
            page += 1
            time.sleep(0.25)
        except Exception as exc:
            print(f"\n  Error on page {page}: {exc}")
            break

    print(f"\n  Done — {len(all_cards)} cards fetched.")
    with open(CACHE_FILE, "w", encoding="utf-8") as fh:
        json.dump(all_cards, fh)
    print(f"  Saved to {CACHE_FILE}")
    return all_cards


def build_summary(all_cards):
    by_name = defaultdict(list)
    for card in all_cards:
        name = card.get("name", "").strip()
        if not name:
            continue
        by_name[name].append({
            "id":     card.get("id", ""),
            "number": card.get("number", ""),
            "set":    card.get("set", {}).get("name", ""),
            "price":  _cheapest_price(card),
        })

    rows = []
    for name, cards in by_name.items():
        priced   = [c for c in cards if c["price"] is not None]
        unpriced = len(cards) - len(priced)
        total    = sum(c["price"] for c in priced)
        rows.append({
            "name":         name,
            "total_cards":  len(cards),
            "priced_cards": len(priced),
            "unpriced":     unpriced,
            "total_cost":   round(total, 2),
        })

    rows.sort(key=lambda r: r["total_cost"])
    return rows


def print_table(rows, top_n=30):
    w = 65
    print(f"\n{'='*w}")
    print(f"  {'Pokemon':<28} {'Cards':>5} {'Priced':>6} {'Total Cost':>10}")
    print(f"{'='*w}")

    print("  Cheapest complete collection")
    print(f"  {'-'*62}")
    for r in rows[:top_n]:
        note = f"  ({r['unpriced']} no price)" if r["unpriced"] else ""
        print(f"  {r['name']:<28} {r['total_cards']:>5} {r['priced_cards']:>6}  ${r['total_cost']:>8.2f}{note}")

    print(f"\n  Most expensive complete collection")
    print(f"  {'-'*62}")
    for r in reversed(rows[-top_n:]):
        note = f"  ({r['unpriced']} no price)" if r["unpriced"] else ""
        print(f"  {r['name']:<28} {r['total_cards']:>5} {r['priced_cards']:>6}  ${r['total_cost']:>8.2f}{note}")

    print(f"\n  {len(rows)} unique Pokemon names across {sum(r['total_cards'] for r in rows)} cards")


def print_pokemon_detail(rows, all_cards, name_filter):
    norm = name_filter.lower()
    matches = [r for r in rows if norm in r["name"].lower()]
    if not matches:
        print(f"  No Pokemon found matching '{name_filter}'")
        return

    for row in matches:
        print(f"\n  {row['name']} — ${row['total_cost']:.2f} total ({row['total_cards']} cards, {row['priced_cards']} priced)")
        print(f"  {'Set':<35} {'Number':>6} {'Price':>8}")
        print(f"  {'-'*52}")
        cards = [c for c in all_cards if c.get("name", "").strip() == row["name"]]
        cards.sort(key=lambda c: c.get("set", {}).get("releaseDate", ""))
        for c in cards:
            p     = _cheapest_price(c)
            price = f"${p:.2f}" if p else "—"
            sname = c.get("set", {}).get("name", "")[:35]
            num   = c.get("number", "")
            print(f"  {sname:<35} {num:>6} {price:>8}")


def main():
    args          = sys.argv[1:]
    force_refresh = "--refresh" in args
    name_filter   = next((a for a in args if not a.startswith("--")), None)

    all_cards = fetch_all_cards(force_refresh=force_refresh)
    rows      = build_summary(all_cards)

    if name_filter:
        print_pokemon_detail(rows, all_cards, name_filter)
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
