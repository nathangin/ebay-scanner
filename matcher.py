"""Card matching against the full pokemontcg.io catalog.

Match priority:
  1. set_name + card_number  → exact catalog entry  (confidence: 'exact')
  2. card_name + set_name    → first matching print  (confidence: 'set+name')
  3. card_name only          → name-only fallback    (confidence: 'name')

Card numbers are extracted from title patterns like "199/165" or "#199".
Set names are matched longest-first to avoid partial matches.
"""
import json
import os
import re

CATALOG_FILE = os.path.join(os.path.dirname(__file__), "card_catalog.json")

_NOISE = frozenset({
    "pokemon", "card", "cards", "tcg", "single",
    "holo", "holofoil", "foil", "reverse",
    "mint", "near", "played", "lightly", "moderately", "heavily",
    "rare", "lot", "pack", "packs", "bundle", "collection",
    "promo", "sealed", "wotc",
    "psa", "bgs", "cgc", "sgc",
    "the", "a", "an",
})

_CONDITION_RULES = [
    ("DMG", [r"\bdmg\b", r"\bheavily\s+damaged\b", r"\bbad\s+condition\b"]),
    ("HP",  [r"\bhp\b", r"\bheavily\s+played\b", r"\bpoor\b", r"\bdamaged\b",
             r"\bcrease[sd]?\b", r"\bbent\b", r"\bwater\s+damage\b", r"\bworn\b"]),
    ("LP",  [r"\blp\b", r"\blightly\s+played\b", r"\bexcellent\b",
             r"\bexc\b", r"\blight\s+play\b", r"\bvery\s+good\b"]),
    ("MP",  [r"\bmp\b", r"\bmoderately\s+played\b", r"\bplayed\b",
             r"\bgood\b", r"\bgd\b"]),
    ("NM",  [r"\bnm\b", r"\bnear\s+mint\b", r"\bmint\b",
             r"\bpack\s+fresh\b", r"\bnm/m\b"]),
]

_EBAY_LABEL_MAP = {
    "brand new": "NM", "like new": "NM", "new": "NM",
    "very good": "LP", "good": "MP", "acceptable": "HP",
}

_GRADE_RE   = re.compile(r"\b(psa|bgs|cgc|sgc)\s*(\d+(?:[._]\d+)?)\b", re.I)
_COMPANY_RE = re.compile(r"\b(psa|bgs|cgc|sgc)\b", re.I)
_NUM_SLASH  = re.compile(r"\b(\d{1,4})/\d{1,4}\b")
_NUM_HASH   = re.compile(r"#\s*(\d{1,4})\b")


def _normalize(text):
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 '\-]", " ", text.lower())).strip()


def _norm_set(name):
    """Normalize a set name for comparison — drops special chars, lowercases."""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", name.lower())).strip()


def _clean_for_match(title):
    stripped = _GRADE_RE.sub(" ", title)
    stripped = _COMPANY_RE.sub(" ", stripped)
    return " ".join(w for w in _normalize(stripped).split() if w not in _NOISE)


def load_catalog():
    if not os.path.exists(CATALOG_FILE):
        return []
    with open(CATALOG_FILE, "r", encoding="utf-8") as fh:
        return json.load(fh)


def detect_grade(title):
    m = _GRADE_RE.search(title)
    if m:
        return m.group(1).upper(), re.sub(r"[_]", ".", m.group(2)), True
    m2 = _COMPANY_RE.search(title)
    if m2:
        return m2.group(1).upper(), None, True
    return None, None, False


def detect_condition(title, ebay_condition_field=None):
    if _COMPANY_RE.search(title) or _GRADE_RE.search(title):
        return "Graded"
    if ebay_condition_field:
        lower = ebay_condition_field.lower().strip()
        for key, grade in _EBAY_LABEL_MAP.items():
            if key in lower:
                return grade
    lower_title = title.lower()
    for cond, patterns in _CONDITION_RULES:
        for pat in patterns:
            if re.search(pat, lower_title):
                return cond
    return "Unknown"


class CardMatcher:
    def __init__(self):
        catalog = load_catalog()
        self.card_count = len(catalog)

        # (norm_set, stripped_number) → catalog entry  [primary index]
        self._by_set_num: dict[tuple, dict] = {}
        # norm_name → [catalog entries]  [name fallback index]
        self._by_name: dict[str, list] = {}
        # norm_set → display set_name  [for set scanning]
        _set_map: dict[str, str] = {}

        for card in catalog:
            if not card.get("name"):
                continue
            ns   = _norm_set(card["set_name"])
            num  = (card["number"] or "").lstrip("0") or ""
            norm = _normalize(card["name"])

            if ns and num:
                self._by_set_num.setdefault((ns, num), card)

            self._by_name.setdefault(norm, []).append(card)

            if ns and ns not in _set_map:
                _set_map[ns] = card["set_name"]

        # Longest norm set first — greedily match the most specific set name
        self._sets_sorted = sorted(_set_map.items(), key=lambda kv: len(kv[0]), reverse=True)
        # Longest norm card name first — greedily match
        self._names_sorted = sorted(self._by_name.keys(), key=len, reverse=True)

    def match(self, title):
        """Return (card_id, card_name, set_name, card_number, confidence).

        card_id     — pokemontcg.io ID for direct price lookup, or None
        card_name   — official card name from catalog, or None
        set_name    — official set name, or None
        card_number — card number string (e.g. '199'), or None
        confidence  — 'exact' | 'set+name' | 'name' | 'none'
        """
        norm_title = _normalize(title)
        cleaned    = _clean_for_match(title)

        number              = self._extract_number(title)
        set_norm, set_disp  = self._find_set(norm_title)

        # 1. Set + number → exact catalog entry
        if set_norm and number:
            card = self._by_set_num.get((set_norm, number.lstrip("0")))
            if card:
                return card["id"], card["name"], card["set_name"], card["number"], "exact"

        # 2. Card name + set → first matching printing in that set
        name_norm = self._find_name(cleaned)
        if name_norm and set_norm:
            for c in self._by_name.get(name_norm, []):
                if _norm_set(c["set_name"]) == set_norm:
                    return c["id"], c["name"], c["set_name"], c["number"], "set+name"

        # 3. Card name only → price lookup will use name-based median
        if name_norm:
            cards = self._by_name.get(name_norm, [])
            if cards:
                return None, cards[0]["name"], None, None, "name"

        return None, None, None, None, "none"

    # ── helpers ───────────────────────────────────────────────────────────────

    def _extract_number(self, title):
        """Return numerator from '4/102' or number from '#199', else None."""
        m = _NUM_SLASH.search(title)
        if m:
            return m.group(1)
        m = _NUM_HASH.search(title)
        if m:
            return m.group(1)
        return None

    def _find_set(self, norm_title):
        """Return (norm_set, display_set_name) of the longest matching set, or (None, None)."""
        for ns, disp in self._sets_sorted:
            if not ns:
                continue
            pat = r"(?<![a-z0-9])" + re.escape(ns) + r"(?![a-z0-9])"
            if re.search(pat, norm_title):
                return ns, disp
        return None, None

    def _find_name(self, cleaned):
        """Return the longest normalized card name found in the cleaned title, or None."""
        for norm in self._names_sorted:
            if not norm:
                continue
            pat = r"(?<![a-z0-9'])" + re.escape(norm) + r"(?![a-z0-9'])"
            if re.search(pat, cleaned):
                return norm
        return None
