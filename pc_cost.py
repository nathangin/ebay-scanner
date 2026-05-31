"""Scrape PriceCharting for the total cost to collect all cards of each
Pokemon in your favorites.json (tcg_cards groups only).

Covers English + Japanese + Topps + all regional variants on PC.
Uses mid price where available, falls back to low price.

Run: py pc_cost.py
"""
import io
import json
import re
import sys
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from bs4 import BeautifulSoup
from scraper import _ensure_browser, _DEFAULT_UA

PC_SEARCH = "https://www.pricecharting.com/search-products?q={}&type=pokemon"
_PRICE_RE = re.compile(r"\d[\d,.]*")


def _parse_price(text):
    if not text:
        return None
    m = _PRICE_RE.search(text.replace(",", ""))
    try:
        return float(m.group()) if m else None
    except ValueError:
        return None


def scrape_pc(pokemon_name):
    """Return list of card dicts from PriceCharting search for this Pokemon."""
    url  = PC_SEARCH.format(pokemon_name.replace(" ", "+"))
    ctx  = _ensure_browser(_DEFAULT_UA)
    page = ctx.new_page()
    try:
        page.goto(url, timeout=30_000, wait_until="domcontentloaded")
        try:
            page.wait_for_selector("table#games_table", timeout=8_000)
        except Exception:
            pass
        html = page.content()
    except Exception as exc:
        return [], str(exc)
    finally:
        page.close()

    soup  = BeautifulSoup(html, "html.parser")
    table = soup.select_one("table#games_table")
    if not table:
        return [], "no results table found on page"

    cards = []
    for row in table.select("tbody tr"):
        title_el   = row.select_one("td.title a") or row.select_one("td.title")
        console_el = row.select_one("td.console")
        if not title_el:
            continue

        title    = title_el.get_text(strip=True)
        set_name = console_el.get_text(strip=True) if console_el else ""

        # Skip non-card items (PC games, accessories, etc.)
        if not set_name:
            continue
        if re.search(r"\bPC Game\b|\bAccessory\b", set_name, re.I):
            continue

        # Mid price (cib_price) preferred; fall back to low (used_price)
        mid_el = row.select_one("td.cib_price .js-price")
        low_el = row.select_one("td.used_price .js-price")
        mid    = _parse_price(mid_el.get_text(strip=True)) if mid_el else None
        low    = _parse_price(low_el.get_text(strip=True)) if low_el else None
        price  = mid if mid else low

        cards.append({
            "title": title,
            "set":   set_name,
            "mid":   mid,
            "low":   low,
            "price": price,
        })

    return cards, None


def get_pokemon_from_favorites():
    with open("favorites.json", encoding="utf-8") as f:
        favorites = json.load(f)

    entries = []   # (group_name, pokemon_name)
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


def main():
    entries = get_pokemon_from_favorites()

    print(f"\n{'='*72}")
    print("  PriceCharting — Cost to Collect All Cards (favorites)")
    print(f"{'='*72}\n")
    print("  Fetching from PriceCharting (a few seconds per Pokemon)...\n")

    all_results = []
    for group_name, pokemon_name in entries:
        print(f"  {pokemon_name:<20} [{group_name}]...", end="", flush=True)
        cards, err = scrape_pc(pokemon_name)
        if err:
            print(f"  ERROR: {err}")
            all_results.append((group_name, pokemon_name, [], err))
        else:
            priced = [c for c in cards if c["price"] is not None]
            total  = sum(c["price"] for c in priced)
            miss   = len(cards) - len(priced)
            note   = f"  ({miss} no price)" if miss else ""
            print(f"  {len(cards):>3} cards | {len(priced):>3} priced | ${total:>7.2f}{note}")
            all_results.append((group_name, pokemon_name, cards, None))
        time.sleep(2)

    # ── summary table ──────────────────────────────────────────────────────────
    print(f"\n{'='*72}")
    print(f"  {'Pokemon':<22} {'Group':<20} {'Cards':>5} {'Priced':>6} {'Total Cost':>11}")
    print(f"{'='*72}")

    grand_total = 0.0
    for group_name, pokemon_name, cards, err in all_results:
        if err:
            print(f"  {pokemon_name:<22} {group_name:<20}  -- error --")
            continue
        priced = [c for c in cards if c["price"] is not None]
        total  = sum(c["price"] for c in priced)
        miss   = len(cards) - len(priced)
        grand_total += total
        note   = f"  ({miss} no price)" if miss else ""
        print(f"  {pokemon_name:<22} {group_name:<20} {len(cards):>5} {len(priced):>6}  ${total:>8.2f}{note}")

    print(f"\n  Grand total (mid/low price per card): ${grand_total:,.2f}")
    print(f"{'='*72}\n")

    # ── detailed breakdown ─────────────────────────────────────────────────────
    print("  Detailed breakdown (sorted by price, highest first):\n")
    for group_name, pokemon_name, cards, err in all_results:
        if err or not cards:
            continue
        priced = [c for c in cards if c["price"] is not None]
        total  = sum(c["price"] for c in priced)
        print(f"  {pokemon_name} [{group_name}] — ${total:.2f} total")
        print(f"  {'Title':<45} {'Set':<28} {'Mid':>7} {'Low':>7}")
        print(f"  {'-'*90}")
        for c in sorted(cards, key=lambda x: x["price"] or 0, reverse=True):
            mid_s = f"${c['mid']:.2f}" if c["mid"] else "—"
            low_s = f"${c['low']:.2f}" if c["low"] else "—"
            print(f"  {c['title'][:45]:<45} {c['set'][:28]:<28} {mid_s:>7} {low_s:>7}")
        print()


if __name__ == "__main__":
    main()
