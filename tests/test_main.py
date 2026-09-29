import csv
import sqlite3

import pytest
import requests

import main
from conftest import FakeResponse


def _row(**overrides):
    row = {
        "url": "https://www.ebay.com/itm/110000000001",
        "item_id": "v1|110000000001|0",
        "title": "Charizard 4/102 Base Set Holo NM",
        "parsed_card_name": "Charizard",
        "set_name": "Base",
        "card_number": "4",
        "match_confidence": "exact",
        "ebay_price": 180.0,
        "shipping": 5.0,
        "total_price": 185.0,
        "tcg_market_price": 400.0,
        "condition": "NM",
        "condition_multiplier": 1.0,
        "fair_value": 400.0,
        "discount_pct": 53.8,
        "is_deal": True,
        "is_graded": False,
        "grading_company": None,
        "grade": None,
        "scraped_at": "2026-09-28T12:00:00+00:00",
        "image_url": "https://i.ebayimg.com/images/g/abc/s-l225.jpg",
    }
    row.update(overrides)
    return row


# ── discord ───────────────────────────────────────────────────────────────────

def test_discord_payload():
    payload = main.build_discord_payload(_row())
    assert payload["allowed_mentions"] == {"parse": []}      # titles can never ping anyone
    [embed] = payload["embeds"]
    assert embed["title"] == "54% off: Charizard"
    assert embed["url"] == "https://www.ebay.com/itm/110000000001"
    assert embed["description"] == "Charizard 4/102 Base Set Holo NM"
    assert embed["thumbnail"] == {"url": "https://i.ebayimg.com/images/g/abc/s-l225.jpg"}
    assert embed["timestamp"] == "2026-09-28T12:00:00+00:00"
    fields = {f["name"]: f["value"] for f in embed["fields"]}
    assert fields == {
        "eBay total":   "$185.00 (incl. $5.00 shipping)",
        "Fair value":   "$400.00",
        "NM market":    "$400.00",
        "Set":          "Base #4",
        "Grade / cond": "NM",
        "Match":        "exact",
    }


def test_discord_payload_graded_unknown_shipping_and_limits():
    row = _row(is_graded=True, grading_company="PSA", grade="10", condition="Graded",
               shipping=None, total_price=180.0, title="x" * 5000, image_url=None,
               set_name=None, card_number=None, parsed_card_name="C" * 300)
    [embed] = main.build_discord_payload(row)["embeds"]
    fields = {f["name"]: f["value"] for f in embed["fields"]}
    assert fields["Grade / cond"] == "PSA 10"
    assert fields["eBay total"] == "$180.00 + shipping"
    assert "Set" not in fields and "thumbnail" not in embed
    assert len(embed["title"]) == 256 and len(embed["description"]) == 4096


def test_free_shipping_label():
    assert main._total_label(_row(shipping=0.0, total_price=180.0)) == "$180.00 (free shipping)"


def test_webhook_url_env_overrides_config(monkeypatch):
    assert main.discord_webhook_url({"discord_webhook_url": ""}) == ""
    assert main.discord_webhook_url({"discord_webhook_url": "https://discord.test/cfg"}) == "https://discord.test/cfg"
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.test/env")
    assert main.discord_webhook_url({"discord_webhook_url": "https://discord.test/cfg"}) == "https://discord.test/env"


@pytest.mark.parametrize("outcome, expected", [
    (FakeResponse(204, None), True),
    (FakeResponse(400, {"message": "Invalid Form Body"}), False),
    (requests.ConnectionError("down"), False),
])
def test_send_discord_alert(monkeypatch, outcome, expected):
    calls = []

    def fake_post(url, json=None, timeout=None):
        calls.append((url, json, timeout))
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(main.requests, "post", fake_post)
    assert main.send_discord_alert("https://discord.test/hook", _row()) is expected
    assert calls[0][0] == "https://discord.test/hook"
    assert calls[0][1] == main.build_discord_payload(_row())


# ── storage ───────────────────────────────────────────────────────────────────

def test_old_database_is_migrated_and_rows_saved(tmp_path):
    path = str(tmp_path / "deals.db")
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE listings (id INTEGER PRIMARY KEY AUTOINCREMENT, url TEXT UNIQUE NOT NULL,
                    title TEXT NOT NULL, parsed_card_name TEXT, match_confidence TEXT, ebay_price REAL,
                    tcg_market_price REAL, condition TEXT, condition_multiplier REAL, fair_value REAL,
                    discount_pct REAL, is_deal INTEGER NOT NULL DEFAULT 0, scraped_at TEXT NOT NULL)""")
    conn.commit()
    conn.close()

    conn = main.ensure_db(path)
    columns = {r[1] for r in conn.execute("PRAGMA table_info(listings)")}
    assert {"item_id", "shipping", "total_price", "is_graded", "set_name"} <= columns

    main._save_db(conn, _row())
    assert main._already_seen(conn, _row()["url"])
    saved = conn.execute("SELECT total_price, shipping, is_deal, is_graded FROM listings").fetchone()
    assert saved == (185.0, 5.0, 1, 0)
    conn.close()


def test_csv_with_old_header_is_moved_aside(tmp_path):
    path = tmp_path / "deals.csv"
    path.write_text("url,title,ebay_price\nhttps://www.ebay.com/itm/1,old,1.0\n", encoding="utf-8")
    main.prepare_csv(str(path))
    assert not path.exists()
    [backup] = list(tmp_path.glob("deals.*.old.csv"))
    assert backup.read_text(encoding="utf-8").startswith("url,title,ebay_price")

    main._append_csv(str(path), _row())
    main.prepare_csv(str(path))            # current header: left alone
    with open(path, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert rows[0]["total_price"] == "185.0" and rows[0]["item_id"] == "v1|110000000001|0"
    assert "image_url" not in rows[0]
