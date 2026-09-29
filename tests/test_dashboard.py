"""Smoke test: the Streamlit dashboard renders from a small temporary database."""
import os

import pytest

import main
from conftest import ROOT

AppTest = pytest.importorskip("streamlit.testing.v1").AppTest
DASHBOARD = os.path.join(ROOT, "dashboard.py")


@pytest.fixture(autouse=True)
def _fresh_streamlit_cache():
    import streamlit as st
    st.cache_data.clear()      # load_data() is cached across runs in the same process


def _row(n, **overrides):
    row = {
        "url": f"https://example.com/demo/{n}", "item_id": f"v1|{n}|0", "title": f"Demo listing {n}",
        "parsed_card_name": "Charizard", "set_name": "Base", "card_number": "4",
        "match_confidence": "exact", "ebay_price": 100.0, "shipping": 5.0, "total_price": 105.0,
        "tcg_market_price": 200.0, "condition": "NM", "condition_multiplier": 1.0,
        "fair_value": 200.0, "discount_pct": 47.5, "is_deal": True, "is_graded": False,
        "grading_company": None, "grade": None, "scraped_at": "2026-09-28T12:00:00+00:00",
    }
    row.update(overrides)
    return row


def test_dashboard_renders(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    conn = main.ensure_db("deals.db")
    main._save_db(conn, _row(1))
    main._save_db(conn, _row(2, is_graded=True, grading_company="PSA", grade=None, condition="Graded",
                             shipping=None, total_price=100.0, discount_pct=12.0, is_deal=False))
    conn.close()

    at = AppTest.from_file(DASHBOARD, default_timeout=60).run()
    assert not at.exception
    metrics = {m.label: m.value for m in at.metric}
    assert metrics["Total Scanned"] == "2"
    assert metrics["Deals Found"] == "1"
    assert at.subheader[0].value == "Listings — 1 results"     # "Deals only" is on by default


def test_dashboard_without_database(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    at = AppTest.from_file(DASHBOARD, default_timeout=60).run()
    assert not at.exception
    assert "No data yet" in at.info[0].value
