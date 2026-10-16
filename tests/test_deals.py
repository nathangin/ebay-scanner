import os

import pytest

import main
from conftest import ROOT
from deals import DEFAULT_RESALE, DealScore, discount_pct, resale_profit, resolve_multiplier, score_deal

GRADE_MULTS = {"PSA 10": 3.0, "PSA 9": 1.5, "BGS 9.5": 4.0, "CGC": 1.1}
COND_MULTS  = {"NM": 1.0, "LP": 0.7, "MP": 0.45, "Unknown": 0.7}


@pytest.mark.parametrize("args, expected", [
    ((True, "PSA", "10", "Graded"), 3.0),       # exact grade key
    ((True, "BGS", "9.5", "Graded"), 4.0),
    ((True, "CGC", "7", "Graded"), 1.1),        # company-only fallback
    ((True, "SGC", "10", "Graded"), 1.0),       # unknown grader -> 1.0
    ((True, None, None, "Graded"), 1.0),
    ((False, None, None, "LP"), 0.7),
    ((False, None, None, "DMG"), 0.7),          # missing condition -> "Unknown"
    ((False, None, None, None), 0.7),
])
def test_resolve_multiplier(args, expected):
    assert resolve_multiplier(*args, GRADE_MULTS, COND_MULTS) == expected


def test_discount_pct():
    assert discount_pct(100.0, 70.0) == 30.0
    assert discount_pct(100.0, 120.0) == -20.0
    assert discount_pct(0.0, 5.0) == 0.0


def test_score_deal_uses_total_cost():
    # NM $100 x LP 0.7 = $70 fair value; $45 + $5 shipping = $50 -> 28.6% off
    assert score_deal(100.0, 0.7, 50.0, threshold=30) == DealScore(70.0, 28.6, False)
    assert score_deal(100.0, 0.7, 45.0, threshold=30) == DealScore(70.0, 35.7, True)


def _config(**overrides):
    cfg = main.load_config(path="does-not-exist.json")
    cfg.update(overrides)
    return cfg


def _listing(price, shipping, title="Charizard 4/102 Base Set Holo", **extra):
    listing = {
        "item_id": "v1|1|0", "title": title, "url": "https://www.ebay.com/itm/1",
        "price": price, "shipping": shipping,
        "total": round(price + shipping, 2) if shipping is not None else price,
        "condition_id": None, "condition_descriptors": {}, "ebay_condition_raw": None,
        "image_url": None,
    }
    listing.update(extra)
    return listing


MATCH = ("base1-4", "Charizard", "Base", "4", "exact")


def test_shipping_can_turn_a_deal_into_a_non_deal():
    cfg = _config(discount_threshold=30)
    # NM $100, NM condition -> fair value $100
    cheap_item_only = main.build_row(_listing(68.0, None, "Charizard 4/102 Base Set NM"), MATCH, 100.0, cfg)
    assert cheap_item_only["total_price"] == 68.0 and cheap_item_only["is_deal"]

    with_shipping = main.build_row(_listing(68.0, 4.5, "Charizard 4/102 Base Set NM"), MATCH, 100.0, cfg)
    assert with_shipping["total_price"] == 72.5
    assert with_shipping["discount_pct"] == 27.5
    assert not with_shipping["is_deal"]


def test_build_row_applies_grade_multiplier_from_api_condition():
    cfg = _config(discount_threshold=30)
    listing = _listing(200.0, 0.0, "Charizard 4/102 Base Set",
                       condition_id="2750",
                       condition_descriptors={"Professional Grader": "Professional Sports Authenticator (PSA)",
                                              "Grade": "10"})
    row = main.build_row(listing, MATCH, 100.0, cfg)
    assert (row["is_graded"], row["grading_company"], row["grade"]) == (True, "PSA", "10")
    assert row["condition_multiplier"] == 3.0          # owner's PSA 10 multiplier
    assert row["fair_value"] == 300.0
    assert row["discount_pct"] == pytest.approx(33.3)
    assert row["is_deal"]


def test_build_row_uses_condition_multiplier_for_raw_cards():
    cfg = _config(discount_threshold=30)
    row = main.build_row(_listing(20.0, 1.0, "Charizard 4/102 Base Set LP"), MATCH, 50.0, cfg)
    assert (row["condition"], row["condition_multiplier"], row["fair_value"]) == ("LP", 0.7, 35.0)
    assert row["discount_pct"] == 40.0


def test_config_merges_multipliers_key_by_key(tmp_path):
    path = tmp_path / "config.json"
    path.write_text('{"grade_multipliers": {"PSA 10": 2.5}, "discount_threshold": 40}', encoding="utf-8")
    cfg = main.load_config(str(path))
    assert cfg["grade_multipliers"]["PSA 10"] == 2.5
    assert cfg["grade_multipliers"]["BGS 9.5"] == 4.0      # default kept
    assert cfg["discount_threshold"] == 40
    assert "user_agent" not in cfg


def test_repo_config_has_no_browser_settings():
    cfg = main.load_config(os.path.join(ROOT, "config.json"))   # the committed config
    assert "user_agent" not in cfg
    assert cfg["ebay_category_ids"] == ["183454"]


@pytest.mark.parametrize("title, excluded", [
    ("Charizard 4/102 Base Set Japanese", True),
    ("Charizard ex custom proxy card", True),
    ("Pokemon TCG Live code card Charizard", True),
    ("Charizard 4/102 Base Set", False),
])
def test_excluded_titles(title, excluded):
    assert main.is_excluded_title(title) is excluded


# ── resale profit ─────────────────────────────────────────────────────────────

def test_resale_profit_after_ebay_fees_and_postage():
    # $100 sale: 13.6% fee on $107 (sale + 7% tax) = $14.552, + $0.40 order fee, + $1 postage
    assert resale_profit(100.0, 60.0, graded=False) == pytest.approx(100 - 14.552 - 0.40 - 1.00 - 60, abs=0.01)
    # graded cards go tracked: $5 postage
    assert resale_profit(100.0, 60.0, graded=True) == pytest.approx(100 - 14.552 - 0.40 - 5.00 - 60, abs=0.01)
    # $10-and-under orders pay the $0.30 order fee
    assert resale_profit(10.0, 5.0, graded=False) == pytest.approx(10 - 1.4552 - 0.30 - 1.00 - 5, abs=0.01)
    # every cost can be overridden, e.g. an eBay Store's lower fee
    assert resale_profit(100.0, 60.0, False, {"fee_pct": 12.35, "sales_tax_pct": 0}) == \
        pytest.approx(100 - 12.35 - 0.40 - 1.00 - 60, abs=0.01)


def test_cheap_cards_are_not_deals_once_fees_are_counted():
    resale = dict(DEFAULT_RESALE)
    # 50% off a $12 card: discount clears 30%, but it only makes ~$2.85 resold
    cheap = score_deal(12.0, 1.0, 6.0, threshold=30, resale=resale)
    assert cheap.discount_pct == 50.0
    assert cheap.resale_profit == pytest.approx(2.85, abs=0.01)
    assert not cheap.is_deal
    # 35% off a $60 card makes ~$10.87: a deal
    big = score_deal(60.0, 1.0, 39.0, threshold=30, resale=resale)
    assert big.is_deal and big.resale_profit == pytest.approx(10.87, abs=0.01)
    # min_profit None judges on discount alone, but still reports the profit
    loose = score_deal(12.0, 1.0, 6.0, threshold=30, resale={**resale, "min_profit": None})
    assert loose.is_deal and loose.resale_profit == cheap.resale_profit
    # without a resale table the score is the old discount-only one
    assert score_deal(12.0, 1.0, 6.0, threshold=30) == DealScore(12.0, 50.0, True)


def test_build_row_reports_resale_profit_and_uses_graded_postage():
    cfg = _config(discount_threshold=30)
    raw = main.build_row(_listing(32.0, 2.0, "Charizard 4/102 Base Set NM"), MATCH, 50.0, cfg)
    # $50 fair value, $34 cost: 50 - 50*1.07*0.136 - 0.40 - 1.00 - 34 = 7.32
    assert raw["discount_pct"] == 32.0 and raw["resale_profit"] == pytest.approx(7.32, abs=0.01)
    assert raw["is_deal"]
    # a PSA 8 (multiplier 1.0) at the same price ships tracked for $5: $3.32, under the $5 minimum
    graded = main.build_row(_listing(32.0, 2.0, "Charizard 4/102 Base Set PSA 8"), MATCH, 50.0, cfg)
    assert graded["is_graded"] and graded["fair_value"] == 50.0
    assert graded["resale_profit"] == pytest.approx(3.32, abs=0.01)
    assert not graded["is_deal"]


def test_config_resale_table_merges_key_by_key(tmp_path):
    path = tmp_path / "config.json"
    path.write_text('{"resale": {"min_profit": 10, "fee_pct": 12.7}}', encoding="utf-8")
    cfg = main.load_config(str(path))
    assert cfg["resale"]["min_profit"] == 10 and cfg["resale"]["fee_pct"] == 12.7
    assert cfg["resale"]["postage_graded"] == DEFAULT_RESALE["postage_graded"]     # default kept
    assert main.load_config("does-not-exist.json")["resale"] == DEFAULT_RESALE
