"""One-shot set scanner — searches eBay for every card in a set and prints deal analysis."""
import io
import re
import sys
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from prices import PriceLookup
from scraper import fetch_ebay_listings

SOUTHERN_ISLANDS = [
    ("si1-1",  "Mew",        "1"),
    ("si1-2",  "Pidgeot",    "2"),
    ("si1-3",  "Onix",       "3"),
    ("si1-4",  "Togepi",     "4"),
    ("si1-5",  "Ivysaur",    "5"),
    ("si1-6",  "Raticate",   "6"),
    ("si1-7",  "Ledyba",     "7"),
    ("si1-8",  "Jigglypuff", "8"),
    ("si1-9",  "Butterfree", "9"),
    ("si1-10", "Tentacruel", "10"),
    ("si1-11", "Marill",     "11"),
    ("si1-12", "Lapras",     "12"),
    ("si1-13", "Exeggutor",  "13"),
    ("si1-14", "Slowking",   "14"),
    ("si1-15", "Wartortle",  "15"),
    ("si1-16", "Lickitung",  "16"),
    ("si1-17", "Vileplume",  "17"),
    ("si1-18", "Primeape",   "18"),
]

DEAL_THRESHOLD = 25  # % below NM price to flag as a deal

_SKIP_TITLE = re.compile(
    r"\bjapanese|\bjapan\b|\bJP\b|"
    r"\bdmg\b|\bheavily\s+damaged|\bheavily\s+played|\bpoor\b|"
    r"\bdamaged\b|\bcreased?|\bbent\b|\bworn\b|\bwater\s+damage|"
    r"\bpostcard|\bpost\s+card",
    re.I,
)


def _skip(title):
    return bool(_SKIP_TITLE.search(title))


def main():
    pricer  = PriceLookup()
    results = []

    print(f"\n{'='*70}")
    print("  Southern Islands — eBay Deal Scan")
    print(f"{'='*70}\n")

    for card_id, name, number in SOUTHERN_ISLANDS:
        nm_price = pricer.get_nm_price(card_id=card_id)
        label    = f"#{number} {name:<12}"

        if not nm_price:
            print(f"  {label}  — no TCGPlayer price found, skipping")
            continue

        print(f"  {label}  NM: ${nm_price:.2f}  — searching eBay…", end="", flush=True)

        search_term = f"Southern Islands {name} pokemon card"
        listings = fetch_ebay_listings(search_term, max_results=10, sort="15")

        found = skipped = 0
        for listing in listings:
            if _skip(listing["title"]):
                skipped += 1
                continue
            ebay_price = listing["price"]
            if ebay_price <= 0:
                continue
            discount = round((nm_price - ebay_price) / nm_price * 100, 1)
            results.append({
                "name":       name,
                "number":     number,
                "nm_price":   nm_price,
                "ebay_price": ebay_price,
                "discount":   discount,
                "url":        listing["url"],
                "title":      listing["title"],
            })
            found += 1

        skip_note = f"  ({skipped} JP/damaged skipped)" if skipped else ""
        print(f"  {found} listings{skip_note}")

    # ── results table ──────────────────────────────────────────────────────────
    print(f"\n{'='*70}")
    print(f"  {'Card':<18} {'NM Price':>9} {'eBay':>9} {'Discount':>9}  Status")
    print(f"{'='*70}")

    results.sort(key=lambda r: r["discount"], reverse=True)

    deals = [r for r in results if r["discount"] >= DEAL_THRESHOLD]

    for r in results:
        flag = f"🔥 {r['discount']:.0f}% off" if r["discount"] >= DEAL_THRESHOLD else ""
        print(
            f"  #{r['number']:<3} {r['name']:<14}  "
            f"${r['nm_price']:>7.2f}   ${r['ebay_price']:>7.2f}   {r['discount']:>6.1f}%  {flag}"
        )

    print(f"\n  {len(results)} listings scanned | {len(deals)} deals (≥{DEAL_THRESHOLD}% off NM)\n")

    if deals:
        print(f"{'─'*70}")
        print("  DEAL LINKS")
        print(f"{'─'*70}")
        for r in deals:
            print(f"\n  🔥 #{r['number']} {r['name']} — ${r['ebay_price']:.2f} vs NM ${r['nm_price']:.2f} ({r['discount']:.0f}% off)")
            print(f"     {r['url']}")
            print(f"     \"{r['title'][:80]}\"")


if __name__ == "__main__":
    main()
