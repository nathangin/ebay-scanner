"""One-time script to download the full card catalog from pokemontcg.io.

Each entry contains: id, name, number, set_name, set_id.
This gives us the exact "Charizard ex #199 Scarlet & Violet 151" style identifiers
needed to match eBay listings to specific card printings.

Run before main.py:
    python build_card_list.py
"""
import json
import time

import requests

CARDS_URL = "https://api.pokemontcg.io/v2/cards"
PAGE_SIZE = 250


def fetch_full_catalog():
    catalog = []
    page    = 1
    while True:
        resp = requests.get(
            CARDS_URL,
            params={
                "pageSize": PAGE_SIZE,
                "page":     page,
                "select":   "id,name,number,set",
            },
            headers={"Accept": "application/json"},
            timeout=30,
        )
        resp.raise_for_status()
        data  = resp.json()
        cards = data.get("data", [])
        total = data.get("totalCount", 0)
        if not cards:
            break

        for card in cards:
            s = card.get("set") or {}
            name = (card.get("name") or "").strip()
            if not name:
                continue
            catalog.append({
                "id":       (card.get("id") or "").strip(),
                "name":     name,
                "number":   (card.get("number") or "").strip(),
                "set_name": (s.get("name") or "").strip(),
                "set_id":   (s.get("id") or "").strip(),
            })

        fetched = (page - 1) * PAGE_SIZE + len(cards)
        print(f"  Page {page:3d} | +{len(cards)} cards | {len(catalog):,} total | {fetched}/{total}")
        if fetched >= total:
            break
        page += 1
        time.sleep(0.5)

    return catalog


if __name__ == "__main__":
    print("Downloading full card catalog from pokemontcg.io …")
    print("Each entry: id + name + number + set  (e.g. 'Charizard ex #199 Scarlet & Violet 151')")
    print("This takes ~2 minutes.\n")

    catalog = fetch_full_catalog()

    with open("card_catalog.json", "w", encoding="utf-8") as fh:
        json.dump(catalog, fh, ensure_ascii=False, indent=2)

    unique_names = len({c["name"] for c in catalog})
    unique_sets  = len({c["set_name"] for c in catalog})
    print(f"\nSaved {len(catalog):,} cards | {unique_names:,} unique names | {unique_sets} sets → card_catalog.json")
    print("Done — you can now run main.py")
