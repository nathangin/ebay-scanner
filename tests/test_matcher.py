import json

import pytest

from matcher import (
    CardMatcher,
    classify_listing,
    condition_from_label,
    detect_condition,
    detect_grade,
    grade_from_descriptors,
    load_catalog,
)

CATALOG = [
    {"id": "base1-4",    "name": "Charizard",          "number": "4",   "set_name": "Base",             "set_id": "base1"},
    {"id": "base1-2",    "name": "Blastoise",          "number": "2",   "set_name": "Base",             "set_id": "base1"},
    {"id": "base1-27",   "name": "Farfetch'd",         "number": "27",  "set_name": "Base",             "set_id": "base1"},
    {"id": "base1-58",   "name": "Pikachu",            "number": "58",  "set_name": "Base",             "set_id": "base1"},
    {"id": "base4-4",    "name": "Charizard",          "number": "4",   "set_name": "Base Set 2",       "set_id": "base4"},
    {"id": "base2-60",   "name": "Pikachu",            "number": "60",  "set_name": "Jungle",           "set_id": "base2"},
    {"id": "base5-4",    "name": "Dark Charizard",     "number": "4",   "set_name": "Team Rocket",      "set_id": "base5"},
    {"id": "gym2-2",     "name": "Blaine's Charizard", "number": "2",   "set_name": "Gym Challenge",    "set_id": "gym2"},
    {"id": "neo1-9",     "name": "Lugia",              "number": "9",   "set_name": "Neo Genesis",      "set_id": "neo1"},
    {"id": "sv3pt5-199", "name": "Charizard ex",       "number": "199", "set_name": "151",              "set_id": "sv3pt5"},
    {"id": "si1-1",      "name": "Mew",                "number": "1",   "set_name": "Southern Islands", "set_id": "si1"},
]


@pytest.fixture(scope="module")
def matcher():
    return CardMatcher(catalog=CATALOG)


# ── title matching ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("title, expected", [
    ("Charizard 4/102 Base Set Holo Rare WOTC",   ("base1-4", "Charizard", "Base", "4", "exact")),
    ("Charizard 4/130 Base Set 2 Holo",           ("base4-4", "Charizard", "Base Set 2", "4", "exact")),
    ("Charizard ex 199/165 Pokemon 151 SIR",      ("sv3pt5-199", "Charizard ex", "151", "199", "exact")),
    ("Dark Charizard 4/82 Team Rocket Holo",      ("base5-4", "Dark Charizard", "Team Rocket", "4", "exact")),
    ("Farfetchd 27/102 Base Set",                 ("base1-27", "Farfetch'd", "Base", "27", "exact")),
    ("PSA 10 Charizard 4/102 Base Set Holo",      ("base1-4", "Charizard", "Base", "4", "exact")),
    ("Pikachu Jungle Set Pokemon Card",           ("base2-60", "Pikachu", "Jungle", "60", "set+name")),
    ("Blaine's Charizard Holo Gym Challenge",     ("gym2-2", "Blaine's Charizard", "Gym Challenge", "2", "set+name")),
    ("Lugia holo pokemon card",                   (None, "Lugia", None, None, "name")),
    ("Random trading card sleeve",                (None, None, None, None, "none")),
])
def test_match(matcher, title, expected):
    assert matcher.match(title) == expected


def test_wrong_number_does_not_match_a_different_card(matcher):
    # Base #4 is Charizard; a Blastoise title with a mistyped number must not become Charizard.
    assert matcher.match("Blastoise 4/102 Base Set Holo") == ("base1-2", "Blastoise", "Base", "2", "set+name")


def test_card_count_and_catalog_loading(tmp_path):
    path = tmp_path / "card_catalog.json"
    path.write_text(json.dumps(CATALOG), encoding="utf-8")
    assert load_catalog(str(path)) == CATALOG
    assert load_catalog(str(tmp_path / "missing.json")) == []
    assert CardMatcher(catalog=CATALOG).card_count == len(CATALOG)


# ── grade / condition from the title ──────────────────────────────────────────

@pytest.mark.parametrize("title, expected", [
    ("Charizard PSA 10 Base Set",  ("PSA", "10", True)),
    ("BGS 9.5 Umbreon VMAX",       ("BGS", "9.5", True)),
    ("cgc 8_5 Lugia",              ("CGC", "8.5", True)),
    ("Mewtwo psa slab",            ("PSA", None, True)),
    ("Charizard holo",             (None, None, False)),
])
def test_detect_grade(title, expected):
    assert detect_grade(title) == expected


@pytest.mark.parametrize("title, expected", [
    ("Charizard 120 HP Holo NM",        "NM"),       # hit points, not Heavily Played
    ("Charizard HP 120 near mint",      "NM"),
    ("Venusaur heavily played",         "HP"),
    ("Blastoise HP creased",            "HP"),
    ("Pikachu LP",                      "LP"),
    ("Pikachu moderately played",       "MP"),
    ("Mew damaged corner",              "HP"),
    ("Pikachu",                         "Unknown"),
    ("Pikachu PSA 9",                   "Graded"),
])
def test_detect_condition_from_title(title, expected):
    assert detect_condition(title) == expected


@pytest.mark.parametrize("label, expected", [
    ("Near mint or better",            "NM"),
    ("Lightly Played (Excellent)",     "LP"),
    ("Moderately Played (Very Good)",  "MP"),
    ("Heavily Played (Poor)",          "HP"),
    ("Very good",                      "MP"),     # eBay's card scale: Very Good = Moderately Played
    ("Ungraded",                       None),
    (None,                             None),
])
def test_condition_from_ebay_label(label, expected):
    assert condition_from_label(label) == expected


def test_ebay_condition_field_beats_title_rules():
    assert detect_condition("Charizard NM", ebay_condition_field="Heavily Played (Poor)") == "HP"


# ── structured condition from the Browse API ──────────────────────────────────

def test_grade_from_descriptors():
    assert grade_from_descriptors({"Professional Grader": "Beckett Grading Services (BGS)",
                                   "Grade": "9.5"}) == ("BGS", "9.5")
    assert grade_from_descriptors({"Professional Grader": "Other"}) == ("OTHER", None)
    assert grade_from_descriptors(None) == (None, None)


def test_graded_condition_id_with_descriptors():
    descriptors = {"Professional Grader": "Professional Sports Authenticator (PSA)", "Grade": "10"}
    assert classify_listing("Charizard Base Set", "2750", descriptors) == ("PSA", "10", True, "Graded")


def test_graded_condition_id_falls_back_to_title():
    assert classify_listing("BGS 9.5 Charizard", "2750") == ("BGS", "9.5", True, "Graded")
    assert classify_listing("Charizard slab", "2750") == (None, None, True, "Graded")


def test_ungraded_condition_id_ignores_grader_names_in_title():
    assert classify_listing("Charizard NM PSA 10 candidate", "4000") == (None, None, False, "NM")


def test_card_condition_descriptor_wins_over_title():
    descriptors = {"Card Condition": "Lightly Played (Excellent)"}
    assert classify_listing("Charizard NM", "4000", descriptors) == (None, None, False, "LP")


def test_without_structured_data_title_rules_apply():
    assert classify_listing("PSA 9 Pikachu 58/102") == ("PSA", "9", True, "Graded")
    assert classify_listing("Pikachu 58/102 LP") == (None, None, False, "LP")
