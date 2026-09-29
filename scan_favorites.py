"""Scan your favorites list for eBay deals.

Edit favorites.json to add/remove items.  Group types:
  tcg_cards      — specific cards by pokemontcg.io card_id, compared to the NM price
  sealed_boxes   — sealed products with a reference price you set (pc_price)
  artist_search  — keyword searches, matched to cards via the catalog

A group may set "category_ids" to override the eBay categories searched
(default: CCG Individual Cards for cards, all categories for sealed boxes).

Run: python scan_favorites.py
"""
from __future__ import annotations

import json
import os
import re
import sys
from typing import Any

from dotenv import load_dotenv

from deals import discount_pct
from ebay_api import EbayError, get_default_client
from listings import DEFAULT_CARD_CATEGORIES, fetch_ebay_listings
from matcher import CATALOG_FILE
from prices import PriceLookup

FAVORITES_FILE = "favorites.json"

_SKIP_CARD_RE = re.compile(
    r"\bjapanese|\bjapan\b|\bJP\b|"
    r"\bdmg\b|\bheavily\s+damaged|\bheavily\s+played|\bpoor\b|"
    r"\bdamaged\b|\bcreased?|\bbent\b|\bworn\b|\bwater\s+damage|"
    r"\bpostcard|\bpost\s+card",
    re.I,
)
_SKIP_BOX_RE = re.compile(
    r"\bsingle\b|\bcard\b|\bopened\b|\bloose\b|\bsleeved?\b|"
    r"\bplaymat\b|\bbinder\b|\btoploader\b|"
    r"\bjapanese\b|\benglish\b|\bkorean\b|\btaiwan\b",
    re.I,
)


# ── group scanners ─────────────────────────────────────────────────────────────

def _title_matches(title: str, card_name: str, must_contain: str | None = None) -> bool:
    """Return True only if the listing title actually mentions this card.

    card_name    — Pokemon name must appear as a word in the title
    must_contain — optional extra keyword (e.g. 'southern islands') that
                    must also appear, used to reject same-Pokemon cards from
                    wrong sets.
    """
    tl = title.lower()
    if not re.search(r"\b" + re.escape(card_name.lower()) + r"\b", tl):
        return False
    if must_contain and must_contain.lower() not in tl:
        return False
    return True


def _result(name: str, ref_price: float, ref_label: str, listing: dict,
            threshold: float) -> dict[str, Any]:
    """One result row; the discount uses price + shipping when shipping is known."""
    return {
        "name":           name,
        "ref_price":      ref_price,
        "ref_label":      ref_label,
        "ebay_total":     listing["total"],
        "shipping_known": listing["shipping"] is not None,
        "discount":       discount_pct(ref_price, listing["total"]),
        "url":            listing["url"],
        "title":          listing["title"],
        "threshold":      threshold,
    }


def scan_tcg_cards(group: dict, pricer: PriceLookup) -> list[dict]:
    """Each card has a card_id; price comes from TCGPlayer via pokemontcg.io."""
    threshold    = group.get("deal_threshold", 25)
    must_contain = group.get("title_must_contain")   # e.g. "southern islands"
    categories   = group.get("category_ids", DEFAULT_CARD_CATEGORIES)
    results      = []

    for card in group["cards"]:
        name     = card["name"]
        search   = card.get("search", f"{name} pokemon card")
        nm_price = pricer.get_nm_price(card_id=card["card_id"])
        label    = f"{name:<20}"

        if not nm_price:
            print(f"  {label}  — no TCGPlayer price, skipping")
            continue

        print(f"  {label}  NM: ${nm_price:.2f}  — searching eBay...", end="", flush=True)

        listings = fetch_ebay_listings(search, max_results=10, sort="15", category_ids=categories)
        found = skipped = 0

        for listing in listings:
            title = listing["title"]
            if _SKIP_CARD_RE.search(title) or not _title_matches(title, name, must_contain):
                skipped += 1
                continue
            results.append(_result(name, nm_price, "NM/TCG", listing, threshold))
            found += 1

        skip_note = f"  ({skipped} skipped)" if skipped else ""
        print(f"  {found} listings{skip_note}")

    return results


def scan_sealed_boxes(group: dict) -> list[dict]:
    """Each item has a reference price you maintain by hand (pc_price)."""
    threshold  = group.get("deal_threshold", 15)
    categories = group.get("category_ids")   # default: search all categories
    results    = []

    for item in group["items"]:
        name     = item["name"]
        pc_price = item["pc_price"]
        label    = f"{name:<24}"

        print(f"  {label}  PC: ${pc_price:.2f}  — searching eBay...", end="", flush=True)

        listings = fetch_ebay_listings(item["search"], max_results=10, sort="15",
                                       filter_bundles=False, category_ids=categories)
        found = skipped = 0

        for listing in listings:
            if _SKIP_BOX_RE.search(listing["title"]):
                skipped += 1
                continue
            results.append(_result(name, pc_price, "PC", listing, threshold))
            found += 1

        skip_note = f"  ({skipped} skipped)" if skipped else ""
        print(f"  {found} listings{skip_note}")

    return results


def scan_artist_search(group: dict, pricer: PriceLookup) -> list[dict]:
    """Search eBay by keyword, match cards via the catalog, price via TCGPlayer."""
    if not os.path.exists(CATALOG_FILE):
        print("  [skip] card_catalog.json not found — run build_card_list.py first")
        return []

    from matcher import CardMatcher

    matcher    = CardMatcher()
    threshold  = group.get("deal_threshold", 25)
    categories = group.get("category_ids", DEFAULT_CARD_CATEGORIES)
    results    = []
    seen_urls: set[str] = set()

    for search_term in group.get("search_terms", []):
        print(f"  Searching: {search_term}...", end="", flush=True)
        listings = fetch_ebay_listings(search_term, max_results=25, sort="15",
                                       category_ids=categories)
        found = 0

        for listing in listings:
            url = listing["url"]
            if url in seen_urls:
                continue
            seen_urls.add(url)

            if _SKIP_CARD_RE.search(listing["title"]):
                continue

            card_id, card_name, set_name, card_number, _ = matcher.match(listing["title"])
            if not card_name:
                continue

            nm_price = pricer.get_nm_price(
                card_id=card_id, card_name=card_name,
                set_name=set_name, card_number=card_number,
            )
            if not nm_price:
                continue

            label = f"{card_name}" + (f" ({set_name})" if set_name else "")
            results.append(_result(label[:40], nm_price, "NM/TCG", listing, threshold))
            found += 1

        print(f"  {found} matched")

    return results


# ── output ─────────────────────────────────────────────────────────────────────

def _print_results(results: list[dict]) -> None:
    if not results:
        print("  (no listings found)")
        return

    threshold = results[0]["threshold"]
    sorted_r  = sorted(results, key=lambda r: r["discount"], reverse=True)
    deals     = [r for r in sorted_r if r["discount"] >= threshold]

    print(f"\n  {'Item':<40} {'Ref':>9} {'eBay+ship':>10} {'Disc':>7}  Status")
    print(f"  {'-'*74}")
    for r in sorted_r:
        flag = f"DEAL {r['discount']:.0f}% off" if r["discount"] >= threshold else ""
        mark = " " if r["shipping_known"] else "*"
        print(
            f"  {r['name']:<40}  "
            f"${r['ref_price']:>7.2f}   ${r['ebay_total']:>7.2f}{mark}  {r['discount']:>5.1f}%  {flag}"
        )

    print(f"\n  {len(sorted_r)} listings | {len(deals)} deals (>={threshold}% off)")
    if any(not r["shipping_known"] for r in sorted_r):
        print("  * shipping calculated at checkout — not included")
    print()

    if deals:
        print(f"  {'─'*72}")
        print("  DEAL LINKS")
        print(f"  {'─'*72}")
        for r in deals:
            print(f"\n  DEAL  {r['name']} — ${r['ebay_total']:.2f} vs {r['ref_label']} "
                  f"${r['ref_price']:.2f} ({r['discount']:.0f}% off)")
            print(f"  {r['url']}")
            print(f"  \"{r['title'][:80]}\"")


# ── main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    load_dotenv()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    if not os.path.exists(FAVORITES_FILE):
        print(f"No {FAVORITES_FILE} found. Add one to get started.")
        sys.exit(1)
    try:
        get_default_client()
    except EbayError as exc:
        print(exc)
        sys.exit(1)

    with open(FAVORITES_FILE, "r", encoding="utf-8") as fh:
        favorites = json.load(fh)

    pricer = PriceLookup()
    total_deals = 0

    for group in favorites:
        print(f"\n{'='*72}")
        print(f"  {group['name']}")
        print(f"{'='*72}\n")

        gtype = group.get("type", "")
        if gtype == "tcg_cards":
            results = scan_tcg_cards(group, pricer)
        elif gtype == "sealed_boxes":
            results = scan_sealed_boxes(group)
        elif gtype == "artist_search":
            results = scan_artist_search(group, pricer)
        else:
            print(f"  Unknown type: {gtype!r}")
            continue

        _print_results(results)
        total_deals += sum(1 for r in results if r["discount"] >= r["threshold"])

    print(f"\n{'='*72}")
    print(f"  Scan complete — {total_deals} total deal(s) found across all favorites")
    print(f"{'='*72}\n")


if __name__ == "__main__":
    main()
