"""eBay Pokemon card deal scanner — raw and PSA/BGS/CGC/SGC graded cards.

Cycles through the search terms in config.json, matches each new eBay listing
to a card in the pokemontcg.io catalog, prices it, and flags listings whose
total cost (price + shipping) is at least `discount_threshold`% below fair
value and that would still make `resale.min_profit` dollars if resold at fair
value after eBay's fees and postage.  Results go to deals.db (SQLite),
deals.csv and deals_only.csv.

First run:
    python build_card_list.py   # one-time, ~2 minutes
    python main.py
"""
from __future__ import annotations

import csv
import itertools
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from typing import Any

import requests
from dotenv import load_dotenv
from rich.console import Console

from deals import DEFAULT_RESALE, resolve_multiplier, score_deal
from ebay_api import CCG_INDIVIDUAL_CARDS_CATEGORY, EbayError, get_default_client
from listings import (
    FOREIGN_LANGUAGE_RE,
    REPRODUCTION_RE,
    add_condition_details,
    fetch_ebay_listings,
)
from matcher import CATALOG_FILE, CardMatcher, MatchResult, classify_listing
from prices import PriceLookup

DB_PATH       = "deals.db"
CSV_ALL_PATH  = "deals.csv"
CSV_DEAL_PATH = "deals_only.csv"
CONFIG_PATH   = "config.json"

_DEFAULT_CONFIG: dict[str, Any] = {
    "discount_threshold": 30,
    "search_terms": [
        "pokemon card single",
        "pokemon holo card",
        "pokemon rare card",
        "pokemon card nm",
        "pokemon vintage card",
        "pokemon psa 10",
        "pokemon psa 9",
        "pokemon bgs graded card",
    ],
    "condition_multipliers": {
        "NM": 1.00, "LP": 0.70, "MP": 0.45,
        "HP": 0.25, "DMG": 0.10, "Unknown": 0.70,
    },
    "grade_multipliers": {
        "PSA 10": 3.0, "PSA 9": 1.5, "PSA 8": 1.0, "PSA 7": 0.75,
        "BGS 9.5": 4.0, "BGS 9": 2.0, "BGS 8.5": 1.2,
        "CGC 10": 2.5, "CGC 9": 1.4,
        "SGC 10": 2.0, "SGC 9": 1.2,
    },
    "poll_interval_seconds": 300,
    "max_listings_per_query": 25,
    "fetch_listing_condition": False,
    "ebay_category_ids": [CCG_INDIVIDUAL_CARDS_CATEGORY],
    "discord_webhook_url": "",
    "resale": dict(DEFAULT_RESALE),
}

# Column order for deals.db inserts and both CSV files.
_FIELDS = [
    "url", "item_id", "title", "parsed_card_name", "set_name", "card_number",
    "match_confidence", "ebay_price", "shipping", "total_price", "tcg_market_price",
    "condition", "condition_multiplier", "fair_value", "discount_pct", "resale_profit",
    "is_deal", "is_graded", "grading_company", "grade", "scraped_at",
]

console = Console()


# ── config ────────────────────────────────────────────────────────────────────

def load_config(path: str = CONFIG_PATH) -> dict[str, Any]:
    """Defaults overlaid with config.json (the multiplier and resale tables are merged key by key)."""
    cfg = _DEFAULT_CONFIG.copy()
    cfg["condition_multipliers"] = _DEFAULT_CONFIG["condition_multipliers"].copy()
    cfg["grade_multipliers"]     = _DEFAULT_CONFIG["grade_multipliers"].copy()
    cfg["resale"]                = _DEFAULT_CONFIG["resale"].copy()
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as fh:
            user = json.load(fh)
        for key in ("condition_multipliers", "grade_multipliers", "resale"):
            cfg[key].update(user.pop(key, None) or {})
        cfg.update(user)
    return cfg


def discord_webhook_url(config: dict[str, Any]) -> str:
    """DISCORD_WEBHOOK_URL from the environment wins over config.json."""
    return (os.getenv("DISCORD_WEBHOOK_URL") or config.get("discord_webhook_url") or "").strip()


# ── database ──────────────────────────────────────────────────────────────────

def ensure_db(path: str) -> sqlite3.Connection:
    """Open deals.db, creating the listings table or adding any missing columns.

    ``scraped_at`` is the UTC time the listing was found; the column keeps its
    original name so existing databases keep working.
    """
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS listings (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            url                  TEXT    UNIQUE NOT NULL,
            item_id              TEXT,
            title                TEXT    NOT NULL,
            parsed_card_name     TEXT,
            set_name             TEXT,
            card_number          TEXT,
            match_confidence     TEXT,
            ebay_price           REAL,
            shipping             REAL,
            total_price          REAL,
            tcg_market_price     REAL,
            condition            TEXT,
            condition_multiplier REAL,
            fair_value           REAL,
            discount_pct         REAL,
            resale_profit        REAL,
            is_deal              INTEGER NOT NULL DEFAULT 0,
            is_graded            INTEGER NOT NULL DEFAULT 0,
            grading_company      TEXT,
            grade                TEXT,
            scraped_at           TEXT    NOT NULL
        )
    """)
    # Add columns that older databases are missing.
    existing = {row[1] for row in conn.execute("PRAGMA table_info(listings)").fetchall()}
    for col, defn in [
        ("is_graded",       "INTEGER NOT NULL DEFAULT 0"),
        ("grading_company", "TEXT"),
        ("grade",           "TEXT"),
        ("set_name",        "TEXT"),
        ("card_number",     "TEXT"),
        ("item_id",         "TEXT"),
        ("shipping",        "REAL"),
        ("total_price",     "REAL"),
        ("resale_profit",   "REAL"),
    ]:
        if col not in existing:
            conn.execute(f"ALTER TABLE listings ADD COLUMN {col} {defn}")
    conn.commit()
    return conn


def _already_seen(conn: sqlite3.Connection, url: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM listings WHERE url = ?", (url,)
    ).fetchone() is not None


def _save_db(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
    values = [row.get(col) for col in _FIELDS]
    values[_FIELDS.index("is_deal")]   = 1 if row["is_deal"] else 0
    values[_FIELDS.index("is_graded")] = 1 if row["is_graded"] else 0
    conn.execute(
        f"INSERT OR IGNORE INTO listings ({', '.join(_FIELDS)}) "
        f"VALUES ({', '.join('?' for _ in _FIELDS)})",
        values,
    )
    conn.commit()


# ── csv ───────────────────────────────────────────────────────────────────────

def prepare_csv(path: str) -> None:
    """Move aside a CSV written with a different column layout.

    Appending rows with new columns under an old header would misalign
    every column, so the old file is renamed to <name>.<timestamp>.old.csv.
    """
    if not os.path.exists(path):
        return
    with open(path, "r", newline="", encoding="utf-8") as fh:
        header = next(csv.reader(fh), None)
    if header and header != _FIELDS:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = f"{os.path.splitext(path)[0]}.{stamp}.old.csv"
        os.replace(path, backup)
        console.print(f"[dim]{path} had an older column layout; moved it to {backup}[/]")


def _append_csv(path: str, row: dict[str, Any]) -> None:
    write_header = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=_FIELDS)
        if write_header:
            writer.writeheader()
        writer.writerow({k: row.get(k, "") for k in _FIELDS})


# ── scoring ───────────────────────────────────────────────────────────────────

def is_excluded_title(title: str) -> bool:
    """Foreign-language cards and proxies/customs would be compared to the wrong price."""
    return bool(FOREIGN_LANGUAGE_RE.search(title) or REPRODUCTION_RE.search(title))


def build_row(listing: dict[str, Any], match: MatchResult, nm_price: float,
              config: dict[str, Any]) -> dict[str, Any]:
    """Classify grade/condition, apply the multiplier and score one matched listing."""
    card_id, card_name, set_name, card_number, confidence = match
    grading_company, grade, is_graded, condition = classify_listing(
        listing["title"],
        condition_id=listing.get("condition_id"),
        descriptors=listing.get("condition_descriptors"),
        ebay_condition=listing.get("ebay_condition_raw"),
    )
    multiplier = resolve_multiplier(
        is_graded, grading_company, grade, condition,
        config["grade_multipliers"], config["condition_multipliers"],
    )
    total = listing.get("total", listing["price"])
    score = score_deal(nm_price, multiplier, total, config["discount_threshold"],
                       graded=is_graded, resale=config.get("resale"))

    return {
        "url":                  listing["url"],
        "item_id":              listing.get("item_id"),
        "title":                listing["title"],
        "parsed_card_name":     card_name,
        "set_name":             set_name,
        "card_number":          card_number,
        "match_confidence":     confidence,
        "ebay_price":           listing["price"],
        "shipping":             listing.get("shipping"),
        "total_price":          total,
        "tcg_market_price":     nm_price,
        "condition":            condition,
        "condition_multiplier": multiplier,
        "fair_value":           score.fair_value,
        "discount_pct":         score.discount_pct,
        "resale_profit":        score.resale_profit,
        "is_deal":              score.is_deal,
        "is_graded":            is_graded,
        "grading_company":      grading_company,
        "grade":                grade,
        "scraped_at":           datetime.now(timezone.utc).isoformat(),
        "image_url":            listing.get("image_url"),   # alerts only, not stored
    }


# ── output ────────────────────────────────────────────────────────────────────

def _grade_label(row: dict[str, Any]) -> str:
    if row["is_graded"]:
        return " ".join(p for p in (row.get("grading_company"), row.get("grade")) if p) or "Graded"
    return row["condition"]


def _total_label(row: dict[str, Any]) -> str:
    if row.get("shipping") is None:
        return f"${row['total_price']:.2f} + shipping"
    if row["shipping"] == 0:
        return f"${row['total_price']:.2f} (free shipping)"
    return f"${row['total_price']:.2f} (incl. ${row['shipping']:.2f} shipping)"


def _profit_label(row: dict[str, Any]) -> str:
    """Resale profit after fees and postage; '~' when shipping is not known yet."""
    profit = row.get("resale_profit")
    if profit is None:
        return ""
    approx = "~" if row.get("shipping") is None else ""
    sign = "-" if profit < 0 else ""
    return f"{approx}{sign}${abs(profit):.2f}"


def _print_deal(row: dict[str, Any]) -> None:
    style = "bold yellow" if row["is_graded"] else "yellow"
    set_part = f" | [dim]{row['set_name']}[/]" if row.get("set_name") else ""
    num_part = f" [dim]#{row['card_number']}[/]" if row.get("card_number") else ""
    profit = _profit_label(row)
    console.print(
        f"\n[bold red][DEAL 🔥][/] [bold]{row['parsed_card_name']}[/]"
        f"{set_part}{num_part}"
        f" | [{style}]{_grade_label(row)}[/]"
        f" | eBay: [green]{_total_label(row)}[/]"
        f" | Fair Value: [cyan]${row['fair_value']:.2f}[/]"
        f" | NM: [cyan]${row['tcg_market_price']:.2f}[/]"
        f" | [bold red]{row['discount_pct']:.0f}% off[/]"
        + (f" | Resale profit: [bold green]{profit}[/]" if profit else "")
    )
    console.print(f"  {row['url']}")


# ── discord ───────────────────────────────────────────────────────────────────

def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def build_discord_payload(row: dict[str, Any]) -> dict[str, Any]:
    """Discord webhook body (one embed) for a deal row.

    Limits from Discord's docs: embed title 256 chars, description 4096,
    field values 1024.  Mentions are disabled so a listing title can never
    ping @everyone.
    """
    card = row.get("parsed_card_name") or "Unknown card"
    where = row.get("set_name") or ""
    if row.get("card_number"):
        where = f"{where} #{row['card_number']}".strip()
    fields = [
        {"name": "eBay total",   "value": _total_label(row),                  "inline": True},
        {"name": "Fair value",   "value": f"${row['fair_value']:.2f}",        "inline": True},
        {"name": "NM market",    "value": f"${row['tcg_market_price']:.2f}",  "inline": True},
        {"name": "Grade / cond", "value": _grade_label(row),                  "inline": True},
        {"name": "Match",        "value": row.get("match_confidence") or "-", "inline": True},
    ]
    if where:
        fields.insert(3, {"name": "Set", "value": _clip(where, 1024), "inline": True})
    if row.get("resale_profit") is not None:
        fields.append({"name": "Resale profit", "value": _profit_label(row) + " after eBay fees + postage",
                       "inline": True})

    embed: dict[str, Any] = {
        "title":       _clip(f"{row['discount_pct']:.0f}% off: {card}", 256),
        "url":         row["url"],
        "description": _clip(row.get("title") or "", 4096),
        "color":       0xF38BA8,
        "fields":      fields,
        "footer":      {"text": "eBay Pokemon deal scanner"},
    }
    if row.get("scraped_at"):
        embed["timestamp"] = row["scraped_at"]
    if row.get("image_url"):
        embed["thumbnail"] = {"url": row["image_url"]}
    return {"embeds": [embed], "allowed_mentions": {"parse": []}}


def send_discord_alert(webhook_url: str, row: dict[str, Any], timeout: float = 10) -> bool:
    """POST a deal to a Discord webhook.  Failures are reported, never fatal."""
    try:
        resp = requests.post(webhook_url, json=build_discord_payload(row), timeout=timeout)
    except requests.RequestException as exc:
        console.print(f"[dim]Discord alert failed: {exc}[/]")
        return False
    if resp.status_code >= 400:
        console.print(f"[dim]Discord alert failed: HTTP {resp.status_code}[/]")
        return False
    return True


# ── main loop ─────────────────────────────────────────────────────────────────

def main() -> None:
    load_dotenv()

    if not os.path.exists(CATALOG_FILE):
        console.print(
            "[bold yellow]Run build_card_list.py first to download the card catalog.\n"
            "This is a one-time setup that takes ~2 minutes.[/]"
        )
        sys.exit(1)

    config = load_config()
    try:
        get_default_client()
    except EbayError as exc:
        console.print(f"[bold red]{exc}[/]")
        sys.exit(1)

    matcher = CardMatcher()
    pricer  = PriceLookup()
    conn    = ensure_db(DB_PATH)
    for path in (CSV_ALL_PATH, CSV_DEAL_PATH):
        prepare_csv(path)

    threshold    = config["discount_threshold"]
    min_profit   = (config.get("resale") or {}).get("min_profit")
    fetch_cond   = config.get("fetch_listing_condition", False)
    categories   = config.get("ebay_category_ids") or None
    webhook      = discord_webhook_url(config)
    search_cycle = itertools.cycle(config["search_terms"])

    console.print("\n[bold cyan]=== eBay Pokemon Card Scanner (raw + graded) ===[/]")
    console.print(
        f"Loaded [bold]{matcher.card_count:,}[/] cards in catalog | "
        f"Deal threshold: [bold]{threshold}%[/] below fair value"
        + (f" and [bold]${min_profit:.2f}[/]+ resale profit after fees" if min_profit is not None else "")
        + (" | Discord alerts: [bold]on[/]" if webhook else "") + "\n"
    )

    scanned = matched = deals = 0
    try:
        while True:
            term = next(search_cycle)
            console.print(f"[dim]Searching: {term}[/]")

            listings = fetch_ebay_listings(
                term,
                max_results=config["max_listings_per_query"],
                category_ids=categories,
            )

            for listing in listings:
                scanned += 1
                if _already_seen(conn, listing["url"]) or is_excluded_title(listing["title"]):
                    continue

                match = matcher.match(listing["title"])
                card_id, card_name, set_name, card_number, _ = match
                if not card_name:
                    continue
                matched += 1

                nm_price = pricer.get_nm_price(
                    card_id=card_id, card_name=card_name,
                    set_name=set_name, card_number=card_number,
                )
                if not nm_price:
                    continue

                if fetch_cond:
                    add_condition_details(listing)   # one getItem call per priced listing

                row = build_row(listing, match, nm_price, config)
                _save_db(conn, row)
                _append_csv(CSV_ALL_PATH, row)

                if row["is_deal"]:
                    deals += 1
                    _append_csv(CSV_DEAL_PATH, row)
                    _print_deal(row)
                    if webhook:
                        send_discord_alert(webhook, row)

                print(f"  Scanned {scanned} | Matched {matched} | Deals {deals}", end="\r")

            time.sleep(config["poll_interval_seconds"])

    except KeyboardInterrupt:
        print(f"\n\nStopped. Scanned {scanned} | Matched {matched} | Deals {deals}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
