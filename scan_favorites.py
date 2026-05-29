"""Scan your favorites list for eBay deals.

Edit favorites.json to add/remove items.
Run: py scan_favorites.py
"""
import io
import json
import os
import re
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from prices import PriceLookup
from scraper import fetch_ebay_listings

FAVORITES_FILE = "favorites.json"
CARD_CATALOG   = "card_catalog.json"

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

def scan_tcg_cards(group, pricer):
    """Each card has a card_id; price comes from TCGPlayer via pokemontcg.io."""
    threshold = group.get("deal_threshold", 25)
    results   = []

    for card in group["cards"]:
        card_id  = card["card_id"]
        name     = card["name"]
        search   = card.get("search", f"{name} pokemon card")
        nm_price = pricer.get_nm_price(card_id=card_id)
        label    = f"{name:<20}"

        if not nm_price:
            print(f"  {label}  — no TCGPlayer price, skipping")
            continue

        print(f"  {label}  NM: ${nm_price:.2f}  — searching eBay...", end="", flush=True)

        listings = fetch_ebay_listings(search, max_results=10, sort="15")
        found = skipped = 0

        for listing in listings:
            if _SKIP_CARD_RE.search(listing["title"]):
                skipped += 1
                continue
            ebay_price = listing["price"]
            if ebay_price <= 0:
                continue
            discount = round((nm_price - ebay_price) / nm_price * 100, 1)
            results.append({
                "name":       name,
                "ref_price":  nm_price,
                "ref_label":  "NM/TCG",
                "ebay_price": ebay_price,
                "discount":   discount,
                "url":        listing["url"],
                "title":      listing["title"],
                "threshold":  threshold,
            })
            found += 1

        skip_note = f"  ({skipped} skipped)" if skipped else ""
        print(f"  {found} listings{skip_note}")

    return results


def scan_sealed_boxes(group):
    """Each item has a hardcoded PriceCharting price."""
    threshold = group.get("deal_threshold", 15)
    results   = []

    for item in group["items"]:
        name      = item["name"]
        pc_price  = item["pc_price"]
        search    = item["search"]
        label     = f"{name:<24}"

        print(f"  {label}  PC: ${pc_price:.2f}  — searching eBay...", end="", flush=True)

        listings = fetch_ebay_listings(search, max_results=10, sort="15", filter_bundles=False)
        found = skipped = 0

        for listing in listings:
            if _SKIP_BOX_RE.search(listing["title"]):
                skipped += 1
                continue
            ebay_price = listing["price"]
            if ebay_price <= 0:
                continue
            discount = round((pc_price - ebay_price) / pc_price * 100, 1)
            results.append({
                "name":       name,
                "ref_price":  pc_price,
                "ref_label":  "PC",
                "ebay_price": ebay_price,
                "discount":   discount,
                "url":        listing["url"],
                "title":      listing["title"],
                "threshold":  threshold,
            })
            found += 1

        skip_note = f"  ({skipped} skipped)" if skipped else ""
        print(f"  {found} listings{skip_note}")

    return results


def scan_artist_search(group, pricer):
    """Search eBay by keyword, match cards via the catalog, price via TCGPlayer."""
    if not os.path.exists(CARD_CATALOG):
        print(f"  [skip] {CARD_CATALOG} not found — run build_card_list.py first")
        return []

    from matcher import CardMatcher

    matcher   = CardMatcher()
    threshold = group.get("deal_threshold", 25)
    results   = []
    seen_urls = set()

    for search_term in group.get("search_terms", []):
        print(f"  Searching: {search_term}...", end="", flush=True)
        listings = fetch_ebay_listings(search_term, max_results=25, sort="15")
        found = 0

        for listing in listings:
            url = listing["url"]
            if url in seen_urls:
                continue
            seen_urls.add(url)

            if _SKIP_CARD_RE.search(listing["title"]):
                continue

            card_id, card_name, set_name, card_number, confidence = matcher.match(listing["title"])
            if not card_name:
                continue

            nm_price = pricer.get_nm_price(
                card_id=card_id, card_name=card_name,
                set_name=set_name, card_number=card_number,
            )
            if not nm_price:
                continue

            ebay_price = listing["price"]
            discount   = round((nm_price - ebay_price) / nm_price * 100, 1)
            label      = f"{card_name}" + (f" ({set_name})" if set_name else "")
            results.append({
                "name":       label[:40],
                "ref_price":  nm_price,
                "ref_label":  "NM/TCG",
                "ebay_price": ebay_price,
                "discount":   discount,
                "url":        url,
                "title":      listing["title"],
                "threshold":  threshold,
            })
            found += 1

        print(f"  {found} matched")

    return results


# ── output ─────────────────────────────────────────────────────────────────────

def _print_results(group_name, results):
    if not results:
        print("  (no listings found)")
        return

    threshold = results[0]["threshold"]
    sorted_r  = sorted(results, key=lambda r: r["discount"], reverse=True)
    deals     = [r for r in sorted_r if r["discount"] >= threshold]

    print(f"\n  {'Item':<40} {'Ref':>9} {'eBay':>9} {'Disc':>7}  Status")
    print(f"  {'-'*72}")
    for r in sorted_r:
        flag = f"DEAL {r['discount']:.0f}% off" if r["discount"] >= threshold else ""
        print(
            f"  {r['name']:<40}  "
            f"${r['ref_price']:>7.2f}   ${r['ebay_price']:>7.2f}   {r['discount']:>5.1f}%  {flag}"
        )

    print(f"\n  {len(sorted_r)} listings | {len(deals)} deals (>={threshold}% off)\n")

    if deals:
        print(f"  {'─'*72}")
        print("  DEAL LINKS")
        print(f"  {'─'*72}")
        for r in deals:
            print(f"\n  DEAL  {r['name']} — ${r['ebay_price']:.2f} vs {r['ref_label']} ${r['ref_price']:.2f} ({r['discount']:.0f}% off)")
            print(f"  {r['url']}")
            print(f"  \"{r['title'][:80]}\"")


# ── main ───────────────────────────────────────────────────────────────────────

def main():
    if not os.path.exists(FAVORITES_FILE):
        print(f"No {FAVORITES_FILE} found. Add one to get started.")
        sys.exit(1)

    with open(FAVORITES_FILE, "r", encoding="utf-8") as fh:
        favorites = json.load(fh)

    pricer = PriceLookup()

    total_deals = 0

    for group in favorites:
        print(f"\n{'='*72}")
        print(f"  {group['name']}")
        print(f"{'='*72}\n")

        gtype   = group.get("type", "")
        results = []

        if gtype == "tcg_cards":
            results = scan_tcg_cards(group, pricer)
        elif gtype == "sealed_boxes":
            results = scan_sealed_boxes(group)
        elif gtype == "artist_search":
            results = scan_artist_search(group, pricer)
        else:
            print(f"  Unknown type: {gtype!r}")
            continue

        _print_results(group["name"], results)
        total_deals += sum(1 for r in results if r["discount"] >= r["threshold"])

    print(f"\n{'='*72}")
    print(f"  Scan complete — {total_deals} total deal(s) found across all favorites")
    print(f"{'='*72}\n")


if __name__ == "__main__":
    main()
