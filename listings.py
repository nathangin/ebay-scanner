"""eBay listing search on top of the official Browse API (see ebay_api.py).

``fetch_ebay_listings`` is what the scanners call.  Every listing is returned
as a plain dict:

    item_id, title, url, price, shipping, total, currency, image_url,
    ebay_condition_raw, condition_id, condition_descriptors,
    seller_username, seller_feedback_pct, seller_feedback_score

``shipping`` is None when it is not known up front (calculated shipping); in
that case ``total`` equals the item price.
"""
from __future__ import annotations

import re
import statistics
from typing import Any, Iterable, Sequence
from urllib.parse import urlsplit

from ebay_api import (
    CCG_INDIVIDUAL_CARDS_CATEGORY,
    SEARCH_MAX_LIMIT,
    UNGRADED_CONDITION_ID,
    EbayApiError,
    EbayClient,
    get_default_client,
    marketplace_country,
)

# Default search scope for single cards.  Pass category_ids=None to search
# all categories (e.g. for sealed product).
DEFAULT_CARD_CATEGORIES: tuple[str, ...] = (CCG_INDIVIDUAL_CARDS_CATEGORY,)

# Callers may pass eBay *website* sort codes (the _sop URL values) or API sort names.
SORT_CODES = {
    "10": "newlyListed",   # newly listed first
    "15": "price",         # lowest price + shipping first
}
API_SORTS = frozenset({"price", "newlyListed", "endingSoonest", "distance"})

MIN_PRICE = 0.50   # ignore $0.01 placeholder / "read the description" listings

# Lots, sealed product and accessories -- skipped when filter_bundles=True.
BUNDLE_RE = re.compile(
    r"\b(lot|lots|bundle|collection|booster|box|"
    r"sleeve|sleeves|binder|playmat|toploader|album|"
    r"tin|case|display|promo pack|blister)\b",
    re.I,
)
GRADED_RE = re.compile(r"\bpsa\b|\bbgs\b|\bcgc\b|\bsgc\b|\bgraded\b", re.I)
# Reference prices come from English TCGPlayer data, so other languages would
# be compared against the wrong price.
FOREIGN_LANGUAGE_RE = re.compile(
    r"\b(japanese|japan|jpn|jp|chinese|korean|german|french|italian|"
    r"spanish|portuguese|thai|indonesian)\b",
    re.I,
)
# Proxies, fan-made cards and digital code cards look like huge "deals".
REPRODUCTION_RE = re.compile(
    r"\b(proxy|proxies|custom|replica|orica|unofficial|fan[\s-]?(?:art|made)|"
    r"code\s*cards?|digital)\b",
    re.I,
)


# ── request helpers ───────────────────────────────────────────────────────────

def resolve_sort(sort: str | None) -> str | None:
    """Map an old website sort code ("10", "15") or an API sort name to the API value.

    None or "" means eBay's default Best Match order.
    """
    if not sort:
        return None
    if sort in SORT_CODES:
        return SORT_CODES[sort]
    if sort in API_SORTS:
        return sort
    raise ValueError(f"unknown sort {sort!r}; use one of {sorted(SORT_CODES)} or {sorted(API_SORTS)}")


def build_filters(
    *,
    buy_it_now_only: bool = True,
    condition_ids: Iterable[str] | None = None,
    delivery_country: str | None = None,
) -> list[str]:
    """Browse API filter expressions for a search."""
    filters = []
    if buy_it_now_only:
        filters.append("buyingOptions:{FIXED_PRICE}")
    ids = [str(c) for c in (condition_ids or [])]
    if ids:
        filters.append("conditionIds:{" + "|".join(ids) + "}")
    if delivery_country:
        filters.append(f"deliveryCountry:{delivery_country}")
    return filters


# ── response mapping ──────────────────────────────────────────────────────────

def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def clean_url(url: str) -> str:
    """Drop eBay tracking parameters: keep only scheme + host + path."""
    p = urlsplit(url)
    return f"{p.scheme}://{p.netloc}{p.path}"


def _known_shipping(item: dict) -> float | None:
    """Cheapest shipping cost that is fixed up front, or None if unknown.

    CALCULATED shipping depends on the buyer's address, so it is treated as
    unknown rather than trusting an estimate.
    """
    costs = []
    for option in item.get("shippingOptions") or []:
        if option.get("shippingCostType") == "CALCULATED":
            continue
        cost = _to_float((option.get("shippingCost") or {}).get("value"))
        if cost is not None:
            costs.append(cost)
    return min(costs) if costs else None


def listing_from_item(item: dict) -> dict | None:
    """Convert one Browse API ItemSummary (or Item) into a listing dict."""
    title = (item.get("title") or "").strip()
    url   = item.get("itemWebUrl")
    price_info = item.get("price") or {}
    price = _to_float(price_info.get("value"))
    if not title or not url or price is None:
        return None

    shipping = _known_shipping(item)
    seller   = item.get("seller") or {}
    image    = (item.get("image") or {}).get("imageUrl")
    if not image:
        thumbs = item.get("thumbnailImages") or []
        image = (thumbs[0] or {}).get("imageUrl") if thumbs else None

    return {
        "item_id":               item.get("itemId"),
        "title":                 title,
        "url":                   clean_url(url),
        "price":                 price,
        "shipping":              shipping,
        "total":                 round(price + shipping, 2) if shipping is not None else price,
        "currency":              price_info.get("currency"),
        "image_url":             image,
        "ebay_condition_raw":    item.get("condition"),
        "condition_id":          item.get("conditionId"),
        "condition_descriptors": parse_condition_descriptors(item.get("conditionDescriptors")),
        "seller_username":       seller.get("username"),
        "seller_feedback_pct":   _to_float(seller.get("feedbackPercentage")),
        "seller_feedback_score": seller.get("feedbackScore"),
    }


def parse_condition_descriptors(raw: Any) -> dict[str, str]:
    """Flatten getItem conditionDescriptors into {name: first value}.

    eBay returns e.g. [{"name": "Professional Grader",
                        "values": [{"content": "Professional Sports Authenticator (PSA)"}]},
                       {"name": "Grade", "values": [{"content": "10"}]}]
    and, for ungraded cards, [{"name": "Card Condition",
                               "values": [{"content": "Near mint or better"}]}].
    """
    result: dict[str, str] = {}
    if not isinstance(raw, list):
        return result
    for descriptor in raw:
        if not isinstance(descriptor, dict) or not descriptor.get("name"):
            continue
        for value in descriptor.get("values") or []:
            content = (value or {}).get("content") if isinstance(value, dict) else None
            if content:
                result[str(descriptor["name"]).strip()] = str(content).strip()
                break
    return result


# ── public functions ──────────────────────────────────────────────────────────

def fetch_ebay_listings(
    search_term: str,
    max_results: int = 25,
    fetch_condition: bool = False,
    sort: str | None = "10",
    filter_bundles: bool = True,
    *,
    category_ids: Sequence[str] | None = DEFAULT_CARD_CATEGORIES,
    buy_it_now_only: bool = True,
    client: EbayClient | None = None,
) -> list[dict]:
    """Search active eBay listings and return listing dicts (see module docstring).

    sort:            "10" newly listed, "15" lowest price + shipping, an API sort
                     value, or None for Best Match
    fetch_condition: also call getItem per listing for grader / grade / card
                     condition (one extra API call each -- mind the daily limit)
    filter_bundles:  skip lots, sealed product and accessories; set False when
                     searching for boxes / sealed product
    category_ids:    eBay categories to search; None searches all categories

    Multi-variation listings are skipped because their price is a range.
    API errors are printed and an empty list is returned so a scan loop keeps
    running; missing credentials raise EbayCredentialsError instead.
    """
    client = client or get_default_client()
    filters = build_filters(
        buy_it_now_only=buy_it_now_only,
        delivery_country=marketplace_country(client.marketplace_id),
    )
    try:
        data = client.search(
            search_term,
            category_ids=category_ids,
            filters=filters,
            sort=resolve_sort(sort),
            limit=min(SEARCH_MAX_LIMIT, max(50, max_results)),
        )
    except EbayApiError as exc:
        print(f"\n  [ebay] {search_term!r} -- skipped ({exc})")
        return []

    results = []
    for item in data.get("itemSummaries") or []:
        if item.get("itemGroupType"):
            continue
        listing = listing_from_item(item)
        if listing is None or listing["price"] < MIN_PRICE:
            continue
        if filter_bundles and BUNDLE_RE.search(listing["title"]):
            continue
        if fetch_condition:
            add_condition_details(listing, client)
        results.append(listing)
        if len(results) >= max_results:
            break
    return results


def add_condition_details(listing: dict, client: EbayClient | None = None) -> dict:
    """Fill condition fields from getItem (grader, grade, card condition).

    Search results carry the item condition ("Graded"/"Ungraded") but not the
    descriptors.  On failure the listing is returned unchanged.
    """
    if not listing.get("item_id"):
        return listing
    client = client or get_default_client()
    try:
        item = client.get_item(listing["item_id"])
    except EbayApiError as exc:
        print(f"\n  [ebay] condition lookup failed for {listing['item_id']} ({exc})")
        return listing
    listing["ebay_condition_raw"]    = item.get("condition") or listing.get("ebay_condition_raw")
    listing["condition_id"]          = item.get("conditionId") or listing.get("condition_id")
    listing["condition_descriptors"] = parse_condition_descriptors(item.get("conditionDescriptors"))
    return listing


def fetch_asking_price(
    search_term: str,
    max_results: int = 12,
    client: EbayClient | None = None,
) -> float | None:
    """Median of the lowest current Buy It Now *asking* prices for an ungraded card.

    This is NOT a sold price.  The Browse API only exposes active listings;
    sold data needs eBay's Marketplace Insights API, which is a Limited
    Release API.  Searches single cards in the ungraded condition, sorted by
    price + shipping, drops graded / foreign-language / lot / proxy titles and
    takes the median item price (shipping excluded, to stay comparable with
    TCGPlayer market prices) of the ``max_results`` cheapest.  Returns None when
    nothing suitable is listed or the request fails.
    """
    client = client or get_default_client()
    filters = build_filters(
        buy_it_now_only=True,
        condition_ids=[UNGRADED_CONDITION_ID],
        delivery_country=marketplace_country(client.marketplace_id),
    )
    try:
        data = client.search(search_term, category_ids=DEFAULT_CARD_CATEGORIES,
                             filters=filters, sort="price", limit=50)
    except EbayApiError as exc:
        print(f" [ebay] asking-price search failed ({exc})", end="")
        return None

    prices = []
    for item in data.get("itemSummaries") or []:
        if item.get("itemGroupType"):
            continue
        listing = listing_from_item(item)
        if listing is None or listing["price"] <= 0.25:
            continue
        title = listing["title"]
        if (GRADED_RE.search(title) or FOREIGN_LANGUAGE_RE.search(title)
                or BUNDLE_RE.search(title) or REPRODUCTION_RE.search(title)):
            continue
        prices.append(listing["price"])

    if not prices:
        return None
    cheapest = sorted(prices)[:max_results]
    return round(statistics.median(cheapest), 2)
