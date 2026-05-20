"""eBay listing scraper using Playwright (real Chromium) to pass JS challenges.

Selectors confirmed against live eBay search pages (May 2026):
  item container : li[id^=item]
  title          : .s-card__title
  price          : .s-card__price
  link           : a.s-card__link
"""
import atexit
import re
import time
from urllib.parse import urlencode, urlparse

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

_PRICE_RE = re.compile(r"\d[\d,.]*")
_SKIP_RE = re.compile(
    r"\b(lot|lots|bundle|collection|booster|box|"
    r"sleeve|sleeves|binder|playmat|toploader|album|"
    r"tin|case|display|promo pack|blister)\b",
    re.I,
)

_DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

_pw      = None
_browser = None
_ctx     = None


def _ensure_browser(ua):
    global _pw, _browser, _ctx
    if _ctx is not None:
        return _ctx

    print("  [browser] Starting Chromium…", flush=True)
    _pw      = sync_playwright().start()
    _browser = _pw.chromium.launch(headless=True)
    _ctx     = _browser.new_context(
        user_agent=ua,
        viewport={"width": 1280, "height": 900},
        locale="en-US",
        timezone_id="America/New_York",
    )
    _ctx.add_init_script(
        "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
    )
    page = _ctx.new_page()
    try:
        page.goto("https://www.ebay.com/", timeout=20_000, wait_until="domcontentloaded")
        time.sleep(2)
    except Exception:
        pass
    finally:
        page.close()

    atexit.register(_shutdown)
    print("  [browser] Ready.", flush=True)
    return _ctx


def _shutdown():
    global _pw, _browser, _ctx
    for obj, method in ((_ctx, "close"), (_browser, "close"), (_pw, "stop")):
        try:
            if obj:
                getattr(obj, method)()
        except Exception:
            pass
    _pw = _browser = _ctx = None


def _base_url(href):
    """Strip eBay tracking params — keep only scheme + host + path."""
    p = urlparse(href)
    return f"{p.scheme}://{p.netloc}{p.path}"


def _parse_price(text):
    if not text:
        return None
    m = _PRICE_RE.search(text.replace(",", ""))
    if not m:
        return None
    try:
        return float(m.group())
    except ValueError:
        return None


def _get_html(search_term, ua, sort="10"):
    params = {
        "_nkw":   search_term,
        "_sop":   sort,    # 10=newly listed, 15=price+shipping low→high
        "LH_BIN": "1",     # Buy It Now only
        "_ipg":   "50",
    }
    url  = "https://www.ebay.com/sch/i.html?" + urlencode(params)
    ctx  = _ensure_browser(ua)
    page = ctx.new_page()
    try:
        page.goto(url, timeout=30_000, wait_until="domcontentloaded")
        try:
            page.wait_for_selector("li[id^=item]", timeout=8_000)
        except Exception:
            pass
        return page.content()
    finally:
        page.close()


def _scrape_condition_from_page(url):
    ctx  = _ensure_browser(_DEFAULT_UA)
    page = ctx.new_page()
    try:
        page.goto(url, timeout=20_000, wait_until="domcontentloaded")
        html = page.content()
    except Exception:
        return None
    finally:
        page.close()

    soup = BeautifulSoup(html, "html.parser")
    for sel in (
        ".x-item-condition-value .ux-textspans",
        ".condText",
        "[data-testid='x-item-condition'] .ux-textspans",
    ):
        el = soup.select_one(sel)
        if el:
            text = el.get_text(strip=True)
            if text:
                return text
    return None


def fetch_ebay_listings(search_term, max_results=25, user_agent=None, fetch_condition=False, sort="10", filter_bundles=True):
    """Return listing dicts (title, url, price, ebay_condition_raw).

    sort: "10"=newly listed, "15"=price+shipping low→high
    filter_bundles: set False when searching for boxes/sealed product (skips the lot/bundle/box filter)
    Returns an empty list on any error so the main loop keeps running.
    """
    ua = user_agent or _DEFAULT_UA
    try:
        html = _get_html(search_term, ua, sort=sort)
    except Exception as exc:
        print(f"\n  [scraper] {search_term!r} — skipped ({exc})")
        return []

    time.sleep(2)

    soup    = BeautifulSoup(html, "html.parser")
    results = []

    for item in soup.select("li[id^=item]"):
        title_el = item.select_one(".s-card__title")
        price_el = item.select_one(".s-card__price")
        link_el  = item.select_one("a.s-card__link") or item.select_one("a[href*=itm]")

        if not title_el or not price_el or not link_el:
            continue

        title      = title_el.get_text(separator=" ", strip=True)
        title      = re.sub(r"\s*Opens in a new window or tab\s*", "", title, flags=re.I).strip()
        price_text = price_el.get_text(strip=True)
        url        = _base_url(link_el.get("href", ""))

        if not title or not url:
            continue
        if " to " in price_text.lower():
            continue

        price = _parse_price(price_text)
        if price is None or price < 0.50:
            continue

        if filter_bundles and _SKIP_RE.search(title):
            continue

        condition_raw = None
        if fetch_condition:
            condition_raw = _scrape_condition_from_page(url)
            time.sleep(1)

        results.append({
            "title":              title,
            "url":                url,
            "price":              price,
            "ebay_condition_raw": condition_raw,
        })

        if len(results) >= max_results:
            break

    return results
