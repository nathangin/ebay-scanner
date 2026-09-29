"""One-shot scanner — Chinese Pokemon booster boxes vs PriceCharting prices.

The reference prices below are maintained by hand (update them from
PriceCharting as the market moves).  Searches all eBay categories, since
sealed product is not listed under single cards.

Run: python scan_chinese_boxes.py
"""
from __future__ import annotations

import re
import sys

from dotenv import load_dotenv

from deals import discount_pct
from ebay_api import EbayError, get_default_client
from listings import fetch_ebay_listings

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

DEAL_THRESHOLD = 15  # % below PriceCharting (compared with price + shipping) to flag as a deal

# Skip listings that are clearly not sealed boxes
_SKIP_RE = re.compile(
    r"\bsingle\b|\bcard\b|\bopened\b|\bloose\b|\bsleeved?\b|"
    r"\bplaymat\b|\bbinder\b|\btoploader\b",
    re.I,
)

# Skip non-Chinese editions if explicitly labelled
_SKIP_LANG_RE = re.compile(r"\bjapanese\b|\benglish\b|\bkorean\b|\btaiwan\b", re.I)


def _skip(title: str) -> bool:
    return bool(_SKIP_RE.search(title) or _SKIP_LANG_RE.search(title))


def main() -> None:
    load_dotenv()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    try:
        get_default_client()
    except EbayError as exc:
        print(exc)
        sys.exit(1)

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
            sort="15",             # lowest price + shipping first
            filter_bundles=False,
            category_ids=None,     # sealed product: search all categories
        )

        found = skipped = 0
        for listing in listings:
            if _skip(listing["title"]):
                skipped += 1
                continue
            results.append({
                "name":           name,
                "pc_price":       pc_price,
                "ebay_total":     listing["total"],
                "shipping_known": listing["shipping"] is not None,
                "discount":       discount_pct(pc_price, listing["total"]),
                "url":            listing["url"],
                "title":          listing["title"],
            })
            found += 1

        skip_note = f"  ({skipped} skipped)" if skipped else ""
        print(f"  {found} listings{skip_note}")

    # ── results table ──────────────────────────────────────────────────────────
    print(f"\n{'='*72}")
    print(f"  {'Box':<22} {'PC Price':>9} {'eBay+ship':>10} {'Discount':>9}  Status")
    print(f"{'='*72}")

    results.sort(key=lambda r: r["discount"], reverse=True)
    deals = [r for r in results if r["discount"] >= DEAL_THRESHOLD]

    for r in results:
        flag = f"DEAL {r['discount']:.0f}% off" if r["discount"] >= DEAL_THRESHOLD else ""
        mark = " " if r["shipping_known"] else "*"
        print(
            f"  {r['name']:<22}  "
            f"${r['pc_price']:>7.2f}   ${r['ebay_total']:>7.2f}{mark}  {r['discount']:>6.1f}%  {flag}"
        )

    print(f"\n  {len(results)} listings scanned | {len(deals)} deals (>={DEAL_THRESHOLD}% off PC)")
    if any(not r["shipping_known"] for r in results):
        print("  * shipping calculated at checkout — not included")
    print()

    if deals:
        print(f"{'─'*72}")
        print("  DEAL LINKS")
        print(f"{'─'*72}")
        for r in deals:
            print(f"\n  DEAL  {r['name']} — ${r['ebay_total']:.2f} vs PC ${r['pc_price']:.2f} "
                  f"({r['discount']:.0f}% off)")
            print(f"     {r['url']}")
            print(f"     \"{r['title'][:80]}\"")


if __name__ == "__main__":
    main()
