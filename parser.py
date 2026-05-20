import json
import os
import re

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

try:
    import anthropic
except ImportError:
    anthropic = None

CLAUDE_MODEL = "claude-sonnet-4-20250514"
CLAUDE_PROMPT_TEMPLATE = (
    "Parse this eBay Pokemon card listing title and return ONLY JSON:\n"
    "{\n"
    "  cardName, setName, cardNumber, language, isGraded,\n"
    "  gradingCompany, grade, condition, isHolo, isBulk\n"
    "}\n"
    "Title: {title}\n"
    "If bulk lot or non-card item return {isBulk: true}"
)

TOP_POKEMON_NAMES = [
    "Pikachu", "Charizard", "Bulbasaur", "Ivysaur", "Venusaur", "Charmander",
    "Charmeleon", "Squirtle", "Wartortle", "Blastoise", "Pidgeot", "Rattata",
    "Raichu", "Psyduck", "Golduck", "Machop", "Machoke", "Machamp", "Abra",
    "Kadabra", "Alakazam", "Gengar", "Haunter", "Gastly", "Snorlax", "Mewtwo",
    "Mew", "Lugia", "Ho-Oh", "Gyarados", "Magikarp", "Eevee", "Vaporeon",
    "Jolteon", "Flareon", "Gyarados", "Dragonite", "Dratini", "Dragonair",
    "Squirtle", "Wartortle", "Blastoise", "Vulpix", "Ninetales", "Sandshrew",
    "Sandslash", "Clefairy", "Clefable", "Jigglypuff", "Wigglytuff", "Tentacool",
    "Tentacruel", "Geodude", "Graveler", "Golem", "Onix", "Cubone", "Marowak",
    "Hitmonlee", "Hitmonchan", "Chansey", "Scyther", "Pinsir", "Magmar",
    "Electabuzz", "Magikarp", "Lapras", "Ditto", "Aerodactyl", "Articuno",
    "Zapdos", "Moltres", "Sneasel", "Teddiursa", "Ursaring", "Houndour",
    "Houndoom", "Corsola", "Miltank", "Blissey", "Suicune", "Entei", "Raikou",
    "Celebi", "Treecko", "Torchic", "Mudkip", "Lucario", "Greninja", "Garchomp",
    "Metagross", "Salamence", "Tyranitar", "Gardevoir", "Gallade", "Giratina",
    "Reshiram", "Zekrom", "Rayquaza", "Zacian", "Zamazenta", "Eternatus",
    "Cinderace", "Inteleon", "Dragapult", "Toxtricity", "Corviknight", "Urshifu",
    "Zarude", "Mew", "Pichu", "Cleffa", "Igglybuff", "Togepi", "Togekiss",
    "Sylveon", "Umbreon", "Espeon", "Mewtwo", "Entei", "Suicune", "Celebi",
    "Darkrai", "Arceus", "Zekrom", "Reshiram", "Kyurem", "Garchomp", "Lucario",
    "Empoleon", "Infernape", "Torterra", "Blaziken", "Swampert", "Blissey",
    "Glaceon", "Leafeon", "Sylveon", "Gallade", "Azelf", "Uxie", "Mesprit",
    "Dialga", "Palkia", "Heatran", "Regigigas", "Darkrai", "Tornadus", "Thundurus",
    "Landorus", "Keldeo", "Meloetta", "Zygarde", "Solgaleo", "Lunala", "Necrozma",
    "Zeraora", "Calyrex", "Kubfu", "Spectrier", "Glastrier", "Urshifu",
    "Bisharp", "Hydreigon", "Talonflame", "Primarina", "Decidueye", "Incineroar",
    "Rillaboom", "Cinderace", "Inteleon", "Corviknight", "Greedent", "Orbeetle",
    "Falinks", "Zacian", "Zamazenta", "Eternatus", "Sylveon", "Gengar", "Lucario",
    "Charizard", "Blastoise", "Venusaur", "Mewtwo", "Mew", "Pikachu", "Eevee",
    "Umbreon", "Espeon", "Greninja", "Garchomp", "Zoroark", "Lucario", "Greninja",
    "Cinderace", "Inteleon", "Dragapult", "Zacian", "Zamazenta", "Eternatus",
    "Zygarde", "Rayquaza", "Lugia", "Ho-Oh", "Dialga", "Palkia", "Giratina",
    "Arceus", "Darkrai", "Mewtwo", "Mew", "Charizard", "Blastoise", "Venusaur"
]

NAME_PATTERN = re.compile(
    r"\b(" + r"|".join(sorted({re.escape(name) for name in TOP_POKEMON_NAMES}, key=len, reverse=True)) + r")\b",
    re.I,
)

LANGUAGE_KEYWORDS = {
    "Japanese": [r"\bjapanese\b", r"\bjp\b", r"\b旧裏\b", r"\b日版\b", r"\b日本語\b"],
    "Chinese": [r"\bchinese\b", r"\bcn\b", r"\b中文\b", r"\b繁體\b", r"\b简体\b"],
    "Korean": [r"\bkorean\b", r"\bkr\b", r"\b한국어\b", r"\b韓国語\b"],
}

BULK_PATTERN = re.compile(r"\b(lot|lots|collection|bulk|bundle|mixed cards|cards only)\b", re.I)
GRADE_PATTERN = re.compile(r"\b(PSA|BGS|CGC|SGC)\s*([0-9]{1,2}(?:\.[05])?)\b", re.I)
CONDITION_PATTERN = re.compile(r"\b(NM|Mint|LP|Light Play|MP|Moderate Play|HP|Heavy Play|DMG|Damaged)\b", re.I)
HOLO_PATTERN = re.compile(r"\b(holo|foil|rainbow|full art|secret rare|shiny|shining)\b", re.I)
VINTAGE_PATTERN = re.compile(r"\b(Base Set|Jungle|Fossil|Team Rocket|Neo Genesis|Gym Heroes|Gym Challenge|Neo Discovery|Neo Revelation|Base|Vintage|1st Edition|1st Ed|First Edition)\b", re.I)
FIRST_EDITION_PATTERN = re.compile(r"\b(1st Edition|1st Ed|First Edition|1st|First)\b", re.I)


def _create_anthropic_client():
    if not ANTHROPIC_API_KEY or not anthropic:
        return None

    try:
        return anthropic.Client(api_key=ANTHROPIC_API_KEY)
    except Exception:
        return None


def _parse_claude_response(raw_text):
    try:
        return json.loads(raw_text)
    except json.JSONDecodeError:
        start = raw_text.find("{")
        end = raw_text.rfind("}")
        if start != -1 and end != -1:
            try:
                return json.loads(raw_text[start:end + 1])
            except json.JSONDecodeError:
                return {}
    return {}


def _query_claude(title):
    client = _create_anthropic_client()
    if not client:
        return {}

    prompt = CLAUDE_PROMPT_TEMPLATE.format(title=title)
    try:
        response = client.completions.create(
            model=CLAUDE_MODEL,
            prompt=prompt,
            max_tokens_to_sample=350,
            stop_sequences=["\n\n"],
        )
        raw = getattr(response, "completion", None) or response.get("completion")
        if not raw:
            raw = response
        return _parse_claude_response(raw)
    except Exception:
        return {}


def _detect_language(title):
    normalized = title.replace("/", " ")
    for language, patterns in LANGUAGE_KEYWORDS.items():
        for pattern in patterns:
            if re.search(pattern, normalized, re.I):
                return language
    return "English"


def _is_bulk(title):
    return bool(BULK_PATTERN.search(title))


def _is_graded(title):
    return bool(GRADE_PATTERN.search(title))


def _extract_grade(title):
    match = GRADE_PATTERN.search(title)
    if match:
        return match.group(2), match.group(1).upper()
    return None, None


def _extract_card_number(title):
    match = re.search(r"\b(\d{1,3}/\d{1,3}|\d{1,4})\b", title)
    if match:
        return match.group(1)
    return None


def _extract_set_name(title):
    keywords = [
        "Base Set", "Jungle", "Fossil", "Team Rocket", "Neo Genesis", "Gym Heroes",
        "Gym Challenge", "Neo Discovery", "Neo Revelation", "Shining Fates", "Evolving Skies",
        "Sword & Shield", "Legendary", "Mint", "EX", "XY", "Sun & Moon", "Sword",
        "Shield", "Scarlet", "Violet",
    ]
    for keyword in keywords:
        if keyword.lower() in title.lower():
            return keyword
    return None


def _extract_card_name(title):
    normalized = title.replace("’", "'").replace("é", "e")
    match = NAME_PATTERN.search(normalized)
    if match:
        return match.group(1).strip()
    return None


def _extract_condition(title):
    match = CONDITION_PATTERN.search(title)
    if match:
        return match.group(1).upper()
    return None


def _extract_is_first_edition(title):
    return bool(FIRST_EDITION_PATTERN.search(title))


def _naive_card_name(title):
    text = re.sub(r"\b(PSA|BGS|CGC|SGC|graded|raw|holo|holofoil|reverse holo|first edition|1st ed|lot|collection|bundle)\b", "", title, flags=re.I)
    text = re.sub(r"[^A-Za-z0-9 &'\-\/]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:120] if text else None


def parse_listing_title(title, mode="regex"):
    parsed = {
        "cardName": None,
        "setName": None,
        "cardNumber": None,
        "language": _detect_language(title),
        "isGraded": _is_graded(title),
        "gradingCompany": None,
        "grade": None,
        "condition": _extract_condition(title),
        "isHolo": bool(HOLO_PATTERN.search(title)),
        "isBulk": _is_bulk(title),
        "isVintage": bool(VINTAGE_PATTERN.search(title)),
        "isFirstEdition": _extract_is_first_edition(title),
    }

    if parsed["isBulk"]:
        return parsed

    if mode == "claude" and ANTHROPIC_API_KEY and anthropic:
        ai_parsed = _query_claude(title)
        if ai_parsed:
            parsed.update({k: ai_parsed.get(k, parsed[k]) for k in parsed})
            if isinstance(parsed.get("isBulk"), str):
                parsed["isBulk"] = parsed["isBulk"].lower() == "true"
            return parsed

    parsed["cardName"] = _extract_card_name(title) or _naive_card_name(title)
    parsed["setName"] = _extract_set_name(title)
    parsed["cardNumber"] = _extract_card_number(title)
    if parsed["isGraded"]:
        parsed["grade"], parsed["gradingCompany"] = _extract_grade(title)
    if not parsed["condition"]:
        parsed["condition"] = "graded" if parsed["isGraded"] else "raw"
    return parsed
