"""eBay Pokemon card deal scanner — raw and PSA/BGS/CGC graded cards.

First run:
    python build_card_list.py   # one-time, ~2 minutes
    python main.py
"""
import csv
import itertools
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone

from rich.console import Console

from matcher import CardMatcher, detect_condition, detect_grade
from prices import PriceLookup
from scraper import fetch_ebay_listings

DB_PATH       = "deals.db"
CSV_ALL_PATH  = "deals.csv"
CSV_DEAL_PATH = "deals_only.csv"
CONFIG_PATH   = "config.json"

_DEFAULT_CONFIG = {
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
    "user_agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
}

_CSV_FIELDS = [
    "url", "title", "parsed_card_name", "set_name", "card_number",
    "match_confidence", "ebay_price", "tcg_market_price",
    "condition", "condition_multiplier", "fair_value", "discount_pct",
    "is_deal", "is_graded", "grading_company", "grade", "scraped_at",
]

console = Console()


# ── config ────────────────────────────────────────────────────────────────────

def load_config():
    cfg = _DEFAULT_CONFIG.copy()
    cfg["condition_multipliers"] = _DEFAULT_CONFIG["condition_multipliers"].copy()
    cfg["grade_multipliers"]     = _DEFAULT_CONFIG["grade_multipliers"].copy()
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            user = json.load(fh)
        cfg.update(user)
        for key in ("condition_multipliers", "grade_multipliers"):
            if key in user:
                cfg[key].update(user[key])
    return cfg


# ── database ──────────────────────────────────────────────────────────────────

def ensure_db(path):
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS listings (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            url                  TEXT    UNIQUE NOT NULL,
            title                TEXT    NOT NULL,
            parsed_card_name     TEXT,
            set_name             TEXT,
            card_number          TEXT,
            match_confidence     TEXT,
            ebay_price           REAL,
            tcg_market_price     REAL,
            condition            TEXT,
            condition_multiplier REAL,
            fair_value           REAL,
            discount_pct         REAL,
            is_deal              INTEGER NOT NULL DEFAULT 0,
            is_graded            INTEGER NOT NULL DEFAULT 0,
            grading_company      TEXT,
            grade                TEXT,
            scraped_at           TEXT    NOT NULL
        )
    """)
    existing = {row[1] for row in conn.execute("PRAGMA table_info(listings)").fetchall()}
    for col, defn in [
        ("is_graded",       "INTEGER NOT NULL DEFAULT 0"),
        ("grading_company", "TEXT"),
        ("grade",           "TEXT"),
        ("set_name",        "TEXT"),
        ("card_number",     "TEXT"),
    ]:
        if col not in existing:
            conn.execute(f"ALTER TABLE listings ADD COLUMN {col} {defn}")
    conn.commit()
    return conn


def _already_seen(conn, url):
    return conn.execute(
        "SELECT 1 FROM listings WHERE url = ?", (url,)
    ).fetchone() is not None


def _save_db(conn, row):
    conn.execute(
        "INSERT OR IGNORE INTO listings "
        "(url, title, parsed_card_name, set_name, card_number, "
        " match_confidence, ebay_price, tcg_market_price, condition, "
        " condition_multiplier, fair_value, discount_pct, is_deal, "
        " is_graded, grading_company, grade, scraped_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            row["url"], row["title"], row["parsed_card_name"],
            row.get("set_name"), row.get("card_number"),
            row["match_confidence"], row["ebay_price"], row["tcg_market_price"],
            row["condition"], row["condition_multiplier"], row["fair_value"],
            row["discount_pct"], 1 if row["is_deal"] else 0,
            1 if row["is_graded"] else 0,
            row.get("grading_company"), row.get("grade"),
            row["scraped_at"],
        ),
    )
    conn.commit()


# ── csv ───────────────────────────────────────────────────────────────────────

def _append_csv(path, row):
    write_header = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=_CSV_FIELDS)
        if write_header:
            writer.writeheader()
        writer.writerow({k: row.get(k, "") for k in _CSV_FIELDS})


# ── output ────────────────────────────────────────────────────────────────────

def _print_deal(row):
    cond  = row["condition"]
    label = (
        f"[bold yellow]{row['grading_company']} {row['grade']}[/]"
        if row["is_graded"]
        else f"[yellow]{cond}[/]"
    )
    set_part = f" | [dim]{row['set_name']}[/]" if row.get("set_name") else ""
    num_part = f" [dim]#{row['card_number']}[/]" if row.get("card_number") else ""
    console.print(
        f"\n[bold red][DEAL 🔥][/] [bold]{row['parsed_card_name']}[/]"
        f"{set_part}{num_part}"
        f" | {label}"
        f" | eBay: [green]${row['ebay_price']:.2f}[/]"
        f" | Fair Value: [cyan]${row['fair_value']:.2f}[/]"
        f" | NM: [cyan]${row['tcg_market_price']:.2f}[/]"
        f" | [bold red]{row['discount_pct']:.0f}% off[/]"
    )
    console.print(f"  {row['url']}")


# ── helpers ───────────────────────────────────────────────────────────────────

def _resolve_multiplier(is_graded, grading_company, grade, condition,
                        grade_mults, cond_mults):
    """Return the appropriate price multiplier depending on card type."""
    if is_graded:
        key     = f"{grading_company} {grade}" if grade else (grading_company or "")
        # Try exact key, then company-only fallback
        return float(grade_mults.get(key, grade_mults.get(grading_company or "", 1.0)))
    return float(cond_mults.get(condition, cond_mults.get("Unknown", 0.70)))


# ── main loop ─────────────────────────────────────────────────────────────────

def main():
    if not os.path.exists("card_catalog.json"):
        console.print(
            "[bold yellow]Run build_card_list.py first to download the card catalog.\n"
            "This is a one-time setup that takes ~2 minutes.[/]"
        )
        sys.exit(1)

    config  = load_config()
    matcher = CardMatcher()
    pricer  = PriceLookup()
    conn    = ensure_db(DB_PATH)

    cond_mults  = config["condition_multipliers"]
    grade_mults = config["grade_multipliers"]
    threshold   = config["discount_threshold"]
    fetch_cond  = config.get("fetch_listing_condition", False)
    search_cycle = itertools.cycle(config["search_terms"])

    console.print(f"\n[bold cyan]=== eBay Pokemon Card Scanner (raw + graded) ===[/]")
    console.print(
        f"Loaded [bold]{matcher.card_count:,}[/] cards in catalog | "
        f"Deal threshold: [bold]{threshold}%[/] below fair value\n"
    )

    scanned = matched = deals = 0

    try:
        while True:
            term = next(search_cycle)
            console.print(f"[dim]Searching: {term}[/]")

            listings = fetch_ebay_listings(
                term,
                max_results=config["max_listings_per_query"],
                user_agent=config.get("user_agent"),
                fetch_condition=fetch_cond,
            )

            for listing in listings:
                scanned += 1

                if _already_seen(conn, listing["url"]):
                    continue

                card_id, card_name, set_name, card_number, confidence = matcher.match(listing["title"])
                if not card_name:
                    continue
                matched += 1

                grading_company, grade, is_graded = detect_grade(listing["title"])

                condition = (
                    "Graded"
                    if is_graded
                    else detect_condition(
                        listing["title"],
                        ebay_condition_field=listing.get("ebay_condition_raw"),
                    )
                )

                nm_price = pricer.get_nm_price(
                    card_id=card_id, card_name=card_name,
                    set_name=set_name, card_number=card_number,
                )
                if not nm_price:
                    continue

                multiplier = _resolve_multiplier(
                    is_graded, grading_company, grade,
                    condition, grade_mults, cond_mults,
                )
                fair_value = round(nm_price * multiplier, 2)
                ebay_price = listing["price"]
                discount   = (
                    round((fair_value - ebay_price) / fair_value * 100, 1)
                    if fair_value > 0 else 0.0
                )
                is_deal = discount >= threshold

                row = {
                    "url":                listing["url"],
                    "title":              listing["title"],
                    "parsed_card_name":   card_name,
                    "set_name":           set_name,
                    "card_number":        card_number,
                    "match_confidence":   confidence,
                    "ebay_price":         ebay_price,
                    "tcg_market_price":   nm_price,
                    "condition":          condition,
                    "condition_multiplier": multiplier,
                    "fair_value":         fair_value,
                    "discount_pct":       discount,
                    "is_deal":            is_deal,
                    "is_graded":          is_graded,
                    "grading_company":    grading_company,
                    "grade":              grade,
                    "scraped_at":         datetime.now(timezone.utc).isoformat(),
                }

                _save_db(conn, row)
                _append_csv(CSV_ALL_PATH, row)

                if is_deal:
                    deals += 1
                    _append_csv(CSV_DEAL_PATH, row)
                    _print_deal(row)

                print(f"  Scraped {scanned} | Matched {matched} | Deals {deals}", end="\r")

            time.sleep(config["poll_interval_seconds"])

    except KeyboardInterrupt:
        conn.close()
        print(f"\n\nStopped. Scraped {scanned} | Matched {matched} | Deals {deals}")
        sys.exit(0)


if __name__ == "__main__":
    main()
