"""Card matching against the full pokemontcg.io catalog, plus grade / condition detection.

Match priority:
  1. set_name + card_number  -> exact catalog entry  (confidence: 'exact')
  2. card_name + set_name    -> first matching print  (confidence: 'set+name')
  3. card_name only          -> name-only fallback    (confidence: 'name')

Card numbers are extracted from title patterns like "199/165" or "#199".
Set names and card names are matched longest-first to avoid partial matches.
An exact set+number hit is only accepted when the title also mentions the
card's name, so a mistyped number cannot map a listing to a different card.
"""
from __future__ import annotations

import json
import os
import re
from typing import Mapping

from ebay_api import GRADED_CONDITION_ID, UNGRADED_CONDITION_ID

CATALOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "card_catalog.json")

# (card_id, card_name, set_name, card_number, confidence)
MatchResult = tuple[str | None, str | None, str | None, str | None, str]

_NOISE = frozenset({
    "pokemon", "card", "cards", "tcg", "single",
    "holo", "holofoil", "foil", "reverse",
    "mint", "near", "played", "lightly", "moderately", "heavily",
    "rare", "lot", "pack", "packs", "bundle", "collection",
    "promo", "sealed", "wotc",
    "psa", "bgs", "cgc", "sgc",
    "the", "a", "an",
})

# Checked in order; the first rule with a matching pattern wins.
_CONDITION_RULES = [
    ("DMG", [r"\bdmg\b", r"\bheavily\s+damaged\b", r"\bbad\s+condition\b"]),
    # "HP" next to a number is hit points ("120 HP", "HP 120"), not Heavily Played.
    ("HP",  [r"(?<![0-9]\s)\bhp\b(?!\s?[0-9])", r"\bheavily\s+played\b", r"\bpoor\b",
             r"\bdamaged\b", r"\bcrease[sd]?\b", r"\bbent\b", r"\bwater\s+damage\b",
             r"\bworn\b"]),
    ("LP",  [r"\blp\b", r"\blightly\s+played\b", r"\bexcellent\b",
             r"\bexc\b", r"\blight\s+play\b", r"\bvery\s+good\b"]),
    ("MP",  [r"\bmp\b", r"\bmoderately\s+played\b", r"\bplayed\b",
             r"\bgood\b", r"\bgd\b"]),
    ("NM",  [r"\bnm\b", r"\bnear\s+mint\b", r"\bmint\b",
             r"\bpack\s+fresh\b", r"\bnm/m\b"]),
]

# eBay's ungraded "Card Condition" values (https://pages.ebay.com/cardconditions/):
# Near mint or better / Lightly Played (Excellent) / Moderately Played (Very Good)
# / Heavily Played (Poor), plus the older short forms.
_EBAY_CARD_CONDITIONS = (
    ("near mint", "NM"),
    ("lightly played", "LP"), ("excellent", "LP"),
    ("moderately played", "MP"), ("very good", "MP"),
    ("heavily played", "HP"), ("poor", "HP"),
)

_GRADE_RE   = re.compile(r"\b(psa|bgs|cgc|sgc)\s*(\d+(?:[._]\d+)?)\b", re.I)
_COMPANY_RE = re.compile(r"\b(psa|bgs|cgc|sgc)\b", re.I)
_NUM_SLASH  = re.compile(r"\b(\d{1,4})/\d{1,4}\b")
_NUM_HASH   = re.compile(r"#\s*(\d{1,4})\b")
_GRADER_ABBR_RE = re.compile(r"\(([^)]+)\)\s*$")


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 '\-]", " ", text.lower())).strip()


def _norm_set(name: str) -> str:
    """Normalize a set name for comparison — drops special chars, lowercases."""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", name.lower())).strip()


def _loose(text: str) -> str:
    """Normalize and drop apostrophes / hyphens ("Farfetch'd" == "Farfetchd", "Ho-Oh" == "Ho Oh")."""
    return re.sub(r"\s+", " ", _normalize(text).replace("'", "").replace("-", " ")).strip()


def _clean_for_match(title: str) -> str:
    stripped = _GRADE_RE.sub(" ", title)
    stripped = _COMPANY_RE.sub(" ", stripped)
    return " ".join(w for w in _normalize(stripped).split() if w not in _NOISE)


def _word_pattern(text: str, extra_word_chars: str = "") -> re.Pattern:
    chars = "a-z0-9" + extra_word_chars
    return re.compile(rf"(?<![{chars}]){re.escape(text)}(?![{chars}])")


def _name_in_title(card_name: str, title: str) -> bool:
    """True when any significant word of the card name appears in the title."""
    loose_title = _loose(title)
    words = [w for w in _loose(card_name).split() if len(w) >= 3 and w not in _NOISE]
    return not words or any(_word_pattern(w).search(loose_title) for w in words)


def load_catalog(path: str = CATALOG_FILE) -> list[dict]:
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


# ── grade / condition ─────────────────────────────────────────────────────────

def detect_grade(title: str) -> tuple[str | None, str | None, bool]:
    """(grading_company, grade, is_graded) read from a listing title."""
    m = _GRADE_RE.search(title)
    if m:
        return m.group(1).upper(), re.sub(r"[_]", ".", m.group(2)), True
    m2 = _COMPANY_RE.search(title)
    if m2:
        return m2.group(1).upper(), None, True
    return None, None, False


def condition_from_label(label: str | None) -> str | None:
    """Map an eBay card-condition label ("Lightly Played (Excellent)") to NM/LP/MP/HP."""
    if not label:
        return None
    lower = label.lower()
    for key, grade in _EBAY_CARD_CONDITIONS:
        if key in lower:
            return grade
    return None


def _condition_from_title(title: str) -> str:
    lower_title = title.lower()
    for cond, patterns in _CONDITION_RULES:
        for pat in patterns:
            if re.search(pat, lower_title):
                return cond
    return "Unknown"


def detect_condition(title: str, ebay_condition_field: str | None = None) -> str:
    """Raw-card condition (NM/LP/MP/HP/DMG/Unknown), or "Graded" if the title names a grader."""
    if _COMPANY_RE.search(title) or _GRADE_RE.search(title):
        return "Graded"
    return condition_from_label(ebay_condition_field) or _condition_from_title(title)


def grade_from_descriptors(descriptors: Mapping[str, str] | None) -> tuple[str | None, str | None]:
    """(company, grade) from getItem condition descriptors.

    "Professional Sports Authenticator (PSA)" -> "PSA"; the grade is used as given ("10").
    """
    descriptors = descriptors or {}
    grader = descriptors.get("Professional Grader")
    company = None
    if grader:
        m = _GRADER_ABBR_RE.search(grader)
        company = (m.group(1) if m else grader).strip().upper()
    grade = (descriptors.get("Grade") or "").strip() or None
    return company, grade


def classify_listing(
    title: str,
    condition_id: str | None = None,
    descriptors: Mapping[str, str] | None = None,
    ebay_condition: str | None = None,
) -> tuple[str | None, str | None, bool, str]:
    """Return (grading_company, grade, is_graded, condition) for a listing.

    eBay's structured data wins over the title: condition 2750 means graded and
    4000 means ungraded in trading-card categories, so an ungraded card titled
    "PSA 10 ready?" is not mistaken for a slab.  Descriptors (from getItem)
    supply grader / grade / card condition when available; otherwise the
    title is parsed as before.
    """
    descriptors = descriptors or {}
    condition_id = str(condition_id) if condition_id is not None else None

    if condition_id == GRADED_CONDITION_ID or descriptors.get("Professional Grader"):
        company, grade = grade_from_descriptors(descriptors)
        title_company, title_grade, _ = detect_grade(title)
        return company or title_company, grade or title_grade, True, "Graded"

    if condition_id == UNGRADED_CONDITION_ID:
        condition = (condition_from_label(descriptors.get("Card Condition"))
                     or _condition_from_title(title))
        return None, None, False, condition

    company, grade, is_graded = detect_grade(title)
    if is_graded:
        return company, grade, True, "Graded"
    return None, None, False, detect_condition(title, ebay_condition_field=ebay_condition)


# ── catalog matching ──────────────────────────────────────────────────────────

class CardMatcher:
    """Match listing titles to catalog cards (see module docstring)."""

    def __init__(self, catalog: list[dict] | None = None):
        if catalog is None:
            catalog = load_catalog()
        self.card_count = len(catalog)

        # (norm_set, stripped_number) -> catalog entry  [primary index]
        self._by_set_num: dict[tuple[str, str], dict] = {}
        # norm_name -> [catalog entries]  [name fallback index]
        self._by_name: dict[str, list[dict]] = {}
        # norm_set -> display set_name
        set_map: dict[str, str] = {}

        for card in catalog:
            if not card.get("name"):
                continue
            ns   = _norm_set(card.get("set_name") or "")
            num  = (card.get("number") or "").lstrip("0")
            norm = _normalize(card["name"])

            if ns and num:
                self._by_set_num.setdefault((ns, num), card)
            self._by_name.setdefault(norm, []).append(card)
            if ns and ns not in set_map:
                set_map[ns] = card["set_name"]

        # Longest first so the most specific set / card name wins.  Patterns are
        # compiled once here; a cheap substring test runs before each regex.
        self._set_patterns = [
            (ns, disp, _word_pattern(ns))
            for ns, disp in sorted(set_map.items(), key=lambda kv: len(kv[0]), reverse=True)
        ]
        self._name_patterns = [
            (norm, _word_pattern(norm, extra_word_chars="'"))
            for norm in sorted(self._by_name, key=len, reverse=True) if norm
        ]

    def match(self, title: str) -> MatchResult:
        """Return (card_id, card_name, set_name, card_number, confidence).

        card_id     — pokemontcg.io ID for direct price lookup, or None
        card_name   — official card name from catalog, or None
        set_name    — official set name, or None
        card_number — card number string (e.g. '199'), or None
        confidence  — 'exact' | 'set+name' | 'name' | 'none'
        """
        norm_title = _normalize(title)
        cleaned    = _clean_for_match(title)
        number     = self._extract_number(title)
        set_norm, _set_disp = self._find_set(norm_title)

        # 1. Set + number -> exact catalog entry (if the title names that card)
        if set_norm and number:
            card = self._by_set_num.get((set_norm, number.lstrip("0")))
            if card and _name_in_title(card["name"], title):
                return card["id"], card["name"], card["set_name"], card["number"], "exact"

        # 2. Card name + set -> first matching printing in that set
        name_norm = self._find_name(cleaned)
        if name_norm and set_norm:
            for c in self._by_name.get(name_norm, []):
                if _norm_set(c["set_name"]) == set_norm:
                    return c["id"], c["name"], c["set_name"], c["number"], "set+name"

        # 3. Card name only -> price lookup will use the median across printings
        if name_norm:
            cards = self._by_name.get(name_norm, [])
            if cards:
                return None, cards[0]["name"], None, None, "name"

        return None, None, None, None, "none"

    # ── helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _extract_number(title: str) -> str | None:
        """Return numerator from '4/102' or number from '#199', else None."""
        m = _NUM_SLASH.search(title) or _NUM_HASH.search(title)
        return m.group(1) if m else None

    def _find_set(self, norm_title: str) -> tuple[str | None, str | None]:
        """(norm_set, display_set_name) of the longest set named in the title."""
        for ns, disp, pattern in self._set_patterns:
            if ns in norm_title and pattern.search(norm_title):
                return ns, disp
        return None, None

    def _find_name(self, cleaned: str) -> str | None:
        """Longest normalized card name found in the cleaned title, or None."""
        for norm, pattern in self._name_patterns:
            if norm in cleaned and pattern.search(cleaned):
                return norm
        return None
