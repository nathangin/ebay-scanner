"""One-shot scanner — Chinese Pokemon booster boxes vs PriceCharting prices."""
import io
import re
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from scraper import fetch_ebay_listings

# (display_name, pc_price_usd, ebay_search_term)
CHINESE_BOXES = [
    ("Gem Pack",           68.70,  "Pokemon Chinese Gem Pack booster box sealed"),
    ("Gem Pack 2",         71.30,  "Pokemon Chinese Gem Pack 2 booster box sealed"),
    ("Gem Pack 3",         61.00,  "Pokemon Chinese Gem Pack 3 booster box sealed"),
    ("Gem Pack 4",         34.98,  "Pokemon Chinese Gem Pack 4 booster box sealed"),
    ("Gem Pack 5",         47.38,  "Pokemon Chinese Gem Pack 5 booster box sealed"),
    ("151 Collect Box",    64.50,  "Pokemon Chinese 151 Collect booster box sealed"),
    ("151 Surprise Jumbo", 121.61, "Pokemon Chinese 151 Surprise Jumbo booster box sealed"),
    ("151 Surprise Slim",  53.00,  "Pokemon Chinese 151 Surprise Slim booster box sealed"),
    ("151 Volume 4",       76.33,  "Pokemon Chinese 151 Volume 4 booster box sealed"),
    ("CSV3C Box",          65.52,  "Pokemon Chinese CSV3C booster box sealed"),
    ("S8A Box",            92.86,  "Pokemon Chinese S8A booster box sealed"),
    ("CSM25C Box",         129.95, "Pokemon Chinese CSM25C booster box sealed"),
]

DEAL_THRESHOLD = 15  # % below PriceCharting to flag as a deal

# Skip listings that are clearly not sealed boxes
_SKIP_RE = re.compile(
    r"\bsingle\b|\bcard\b|\bopened\b|\bloose\b|\bsleeved?\b|"
    r"\bplaymat\b|\bbinder\b|\btoploader\b",
    re.I,
)

# Skip non-Chinese editions if explicitly labelled
_SKIP_LANG_RE = re.compile(r"\bjapanese\b|\benglish\b|\bkorean\b|\btaiwan\b", re.I)


def _skip(title):
    return bool(_SKIP_RE.search(title) or _SKIP_LANG_RE.search(title))


def main():
    results = []

    print(f"\n{'='*72}")
    print("  Chinese Pokemon Booster Boxes — eBay Deal Scan (vs PriceCharting)")
    print(f"{'='*72}\n")

    for name, pc_price, search_term in CHINESE_BOXES:
        label = f"{name:<22}"
        print(f"  {label}  PC: ${pc_price:>7.2f}  — searching eBay…", end="", flush=True)

        listings = fetch_ebay_listings(
            search_term,
            max_results=10,
            sort="15",           # cheapest first
            filter_bundles=False,
        )

        found = skipped = 0
        for listing in listings:
            if _skip(listing["title"]):
                skipped += 1
                continue
            ebay_price = listing["price"]
            if ebay_price <= 0:
                continue
            discount = round((pc_price - ebay_price) / pc_price * 100, 1)
            results.append({
                "name":       name,
                "pc_price":   pc_price,
                "ebay_price": ebay_price,
                "discount":   discount,
                "url":        listing["url"],
                "title":      listing["title"],
            })
            found += 1

        skip_note = f"  ({skipped} skipped)" if skipped else ""
        print(f"  {found} listings{skip_note}")

    # ── results table ──────────────────────────────────────────────────────────
    print(f"\n{'='*72}")
    print(f"  {'Box':<22} {'PC Price':>9} {'eBay':>9} {'Discount':>9}  Status")
    print(f"{'='*72}")

    results.sort(key=lambda r: r["discount"], reverse=True)
    deals = [r for r in results if r["discount"] >= DEAL_THRESHOLD]

    for r in results:
        flag = f"DEAL {r['discount']:.0f}% off" if r["discount"] >= DEAL_THRESHOLD else ""
        print(
            f"  {r['name']:<22}  "
            f"${r['pc_price']:>7.2f}   ${r['ebay_price']:>7.2f}   {r['discount']:>6.1f}%  {flag}"
        )

    print(f"\n  {len(results)} listings scanned | {len(deals)} deals (>={DEAL_THRESHOLD}% off PC)\n")

    if deals:
        print(f"{'─'*72}")
        print("  DEAL LINKS")
        print(f"{'─'*72}")
        for r in deals:
            print(f"\n  DEAL  {r['name']} — ${r['ebay_price']:.2f} vs PC ${r['pc_price']:.2f} ({r['discount']:.0f}% off)")
            print(f"     {r['url']}")
            print(f"     \"{r['title'][:80]}\"")


if __name__ == "__main__":
    main()
