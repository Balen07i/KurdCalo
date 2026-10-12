# knowledge.py
#
# Loads the project's knowledge layer (data/foods_database.json,
# data/recognition_rules.json, data/prompt_fragments.md) once at import
# time and exposes it to vision.py as prompt-building functions and a
# fuzzy-match lookup - replacing kurdish_foods.py's role.
#
# WHY A SEPARATE MODULE INSTEAD OF EDITING kurdish_foods.py IN PLACE:
# kurdish_foods.py mixed DATA (the glossary list, confusable pairs) with
# CODE (prompt builders, fuzzy matching) in one file, which is exactly
# what the knowledge-layer redesign moved away from - food/recognition
# knowledge is now content files under data/, reviewable and editable
# without touching Python at all. This module is the thin code layer
# that reads that content; it should stay thin. kurdish_foods.py is kept
# in the repo for history/reference but is no longer imported anywhere -
# see its module docstring.
#
# ADDING OR EDITING A FOOD: edit data/foods_database.json directly (or
# review it with another AI, as intended) - no code change needed here.
# Same for data/recognition_rules.json and data/prompt_fragments.md.

from __future__ import annotations

import difflib
import json
import logging
import os

logger = logging.getLogger(__name__)

_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
_FOODS_PATH = os.path.join(_DATA_DIR, "foods_database.json")
_RULES_PATH = os.path.join(_DATA_DIR, "recognition_rules.json")
_PROMPT_FRAGMENT_PATH = os.path.join(_DATA_DIR, "prompt_fragments.md")
_REVIEWER_INSTRUCTIONS_PATH = os.path.join(_DATA_DIR, "reviewer_instructions.md")

# Cap how many alias/misspelling terms are shown per food in the prompt -
# keeps each glossary line bounded even for entries with long alias lists,
# same "keep the prompt bounded" philosophy already used elsewhere in this
# codebase (see vision.py's corrections[:60] cap).
_MAX_ALIASES_SHOWN = 6


def _load_json(path: str, what: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        logger.error(
            "[KNOWLEDGE] %s not found at %s - the bot will run with an EMPTY "
            "%s. Recognition quality will silently degrade to Gemini's raw "
            "judgment with no local reference data. Check the data/ folder "
            "was deployed alongside the code.", what, path, what,
        )
        return {}
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        logger.error(
            "[KNOWLEDGE] %s at %s failed to parse (%s) - the bot will run "
            "with an EMPTY %s rather than crash on startup. Fix the file's "
            "JSON syntax and redeploy.", what, path, exc, what,
        )
        return {}


def _load_text(path: str, what: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        logger.warning(
            "[KNOWLEDGE] %s not found at %s - continuing without it "
            "(non-fatal, this file only adds supplementary prompt "
            "guidance).", what, path,
        )
        return ""


_FOODS_DOC = _load_json(_FOODS_PATH, "foods_database.json")
_RULES_DOC = _load_json(_RULES_PATH, "recognition_rules.json")
_PROMPT_FRAGMENT = _load_text(_PROMPT_FRAGMENT_PATH, "prompt_fragments.md")
_REVIEWER_INSTRUCTIONS = _load_text(_REVIEWER_INSTRUCTIONS_PATH, "reviewer_instructions.md")

FOODS: list[dict] = _FOODS_DOC.get("foods", [])
FOODS_BY_ID: dict[str, dict] = {f["id"]: f for f in FOODS if "id" in f}

CONFUSION_PAIRS: list[dict] = _RULES_DOC.get("confusion_pairs", [])
BRAND_MAPPINGS: list[dict] = _RULES_DOC.get("brand_mappings", [])
VISUAL_HINTS: list[dict] = _RULES_DOC.get("visual_hints", [])

if not FOODS:
    logger.warning(
        "[KNOWLEDGE] Food glossary is empty - Gemini will identify foods "
        "with no local reference data at all. This is a degraded but "
        "non-fatal state (see _load_json above), not a crash."
    )


def _default_serving_nutrition(food: dict) -> dict | None:
    """Each food has a serving_sizes list with exactly one entry flagged
    default:true - this is the 'typical single serving' the glossary
    prompt quotes kcal/macros for. Falls back to the first serving size
    if no entry is explicitly flagged (defensive - shouldn't happen with
    the current data, but a malformed edit during review shouldn't crash
    the bot)."""
    servings = food.get("serving_sizes") or []
    for s in servings:
        if s.get("default"):
            return s
    return servings[0] if servings else None


# --- Glossary prompt (replaces kurdish_foods.build_glossary_prompt) -----

def build_glossary_prompt() -> str:
    """
    Returns a formatted string block to inject into the vision model's
    prompt so it recognizes dishes across every cuisine in
    foods_database.json by name, alias, or misspelling - and reuses the
    same Kurdish name + emoji every time for consistency.

    Same purpose and same call signature as the old
    kurdish_foods.build_glossary_prompt() - vision.py's call site is
    unchanged.
    """
    if not FOODS:
        return ""

    lines = []
    for food in FOODS:
        names = food.get("names", {})
        kurdish_name = names.get("kurdish") or names.get("english") or food.get("id", "؟")
        emoji = food.get("emoji", "🍽️")
        per_serving = food.get("nutrition_per_serving") or {}

        match_terms = list(dict.fromkeys(  # de-dupe while preserving order
            (food.get("aliases") or []) + (food.get("common_misspellings") or [])
        ))[:_MAX_ALIASES_SHOWN]
        alias_str = ", ".join(match_terms) if match_terms else "-"

        kcal = per_serving.get("kcal", "?")
        protein = per_serving.get("protein_g", "?")
        carbs = per_serving.get("carbs_g", "?")
        fat = per_serving.get("fat_g", "?")

        lines.append(
            f"- {emoji} {kurdish_name} (matches: {alias_str}): "
            f"~{kcal} kcal, {protein}g protein, {carbs}g carbs, {fat}g fat "
            f"per typical serving"
        )

    return (
        "Reference list of common dishes (Kurdish, Iraqi, Turkish, "
        "Iranian, Syrian/Lebanese, and international foods commonly eaten "
        "in the Kurdistan Region) with their exact Kurdish name, emoji, "
        "and typical calories/macros per one serving. If a food in the "
        "photo closely matches one of these (by name, alias, or visual "
        "similarity), use this exact name and emoji, and use these "
        "numbers as your primary estimate, adjusted for the visible "
        "portion size. If a food does not match any of these, give your "
        "own best-guess Kurdish name, a fitting emoji, and mark it "
        "clearly as not matched.\n\n" + "\n".join(lines)
    )


# --- Confusable/recognition prompt (replaces kurdish_foods.build_confusable_prompt) --

def build_confusable_prompt() -> str:
    """
    Returns a formatted string block covering: documented confusion
    pairs, standalone visual hints, and brand recognition cues - drawn
    from recognition_rules.json. Same purpose and call signature as the
    old kurdish_foods.build_confusable_prompt().
    """
    sections = []

    if CONFUSION_PAIRS:
        pair_lines = []
        for p in CONFUSION_PAIRS:
            differentiators = "; ".join(p.get("visual_differentiators", [])[:2])
            pair_lines.append(
                f"- {p.get('food_a')} vs {p.get('food_b')}: {differentiators}. "
                f"If genuinely unclear: {p.get('default_if_uncertain', '')}"
            )
        sections.append(
            "Pairs of foods that are easy to visually confuse - actively "
            "look for the distinguishing visual cue before deciding, and "
            "if you genuinely can't tell them apart, this is exactly the "
            "case where you should offer alternative estimates instead of "
            "guessing:\n" + "\n".join(pair_lines)
        )

    # Standalone visual hints not already tied to a confusion pair above
    # (distinguish_from_food_id is null for these - see recognition_rules.json)
    standalone_hints = [h for h in VISUAL_HINTS if not h.get("distinguish_from_food_id")]
    if standalone_hints:
        hint_lines = [f"- {h.get('hint')}: {h.get('rule')}" for h in standalone_hints]
        sections.append(
            "Additional visual recognition cues:\n" + "\n".join(hint_lines)
        )

    if BRAND_MAPPINGS:
        brand_lines = [
            f"- {b.get('brand')}: {', '.join(b.get('visual_cues', []))}"
            for b in BRAND_MAPPINGS
        ]
        sections.append(
            "Common branded products and their visual cues (see BRAND "
            "RECOGNITION above for how to use a matched brand name):\n"
            + "\n".join(brand_lines)
        )

    return "\n\n".join(sections)


# --- Supplementary style/decision guidance (new - no old equivalent) ----

def build_decision_and_style_prompt() -> str:
    """
    Returns the condensed, LLM-ready excerpt of docs/kurdish_style.md and
    docs/decision_rules.md (see data/prompt_fragments.md for what's
    included and why it's a curated excerpt, not the full docs).
    """
    return _PROMPT_FRAGMENT


# --- Fuzzy matching (replaces kurdish_foods.find_glossary_match) --------
#
# Gemini sometimes identifies a food correctly but phrases it slightly
# differently than our database entry. This catches those cases in code,
# independent of the model's own judgment, and re-labels the food with
# our canonical Kurdish name + emoji so the bot stays visually consistent
# no matter how Gemini phrased it - including across Kurdish, English,
# Arabic, and Latin-transliterated names now that the database covers
# more than one language per entry.

def _build_search_index() -> list[tuple[str, dict]]:
    index: list[tuple[str, dict]] = []
    for food in FOODS:
        names = food.get("names", {})
        for key in ("kurdish", "kurdish_latin", "english", "arabic"):
            val = names.get(key)
            if val:
                index.append((val.lower(), food))
        for alias in food.get("aliases") or []:
            index.append((alias.lower(), food))
        for misspelling in food.get("common_misspellings") or []:
            index.append((misspelling.lower(), food))
    return index


_SEARCH_INDEX = _build_search_index()
_SEARCH_TERMS = [term for term, _ in _SEARCH_INDEX]


def find_glossary_match(name: str, cutoff: float = 0.6) -> dict | None:
    """
    Fuzzy-matches a food name (any language Gemini wrote it in) against
    every database entry's names/aliases/misspellings. Returns a dict
    shaped like {"name": ..., "emoji": ..., "id": ...} - the same
    "name"/"emoji" keys the old kurdish_foods.find_glossary_match
    returned, so vision.py's existing consuming code
    (match["name"], match["emoji"]) needs no changes. "id" is added for
    future use (e.g. looking up full nutrition/serving data), ignored by
    any caller that doesn't use it.

    Three-stage lookup, most-specific first:
    1. Exact term match (query == a full name/alias/misspelling).
    2. Substring containment, preferring the LONGEST matching term - NOT
       just the first one found in list order. An earlier version of
       this function used first-match-wins substring containment, which
       had a real bug: "pepsi zero" contains "pepsi" as a substring, and
       if drink_pepsi_regular's "pepsi" alias happened to be indexed
       before drink_pepsi_zero's "pepsi zero" alias, the query would
       match the WRONG product - exactly the kind of high-severity
       confusion recognition_rules.json exists to prevent, caused by the
       matching code itself rather than the vision model. Preferring the
       longest (most specific) matching term fixes this class of bug in
       general, not just for this one pair.
    3. difflib fuzzy ratio, for genuine spelling variations/typos.
    """
    query = (name or "").strip().lower()
    if not query or len(query) < 3:
        return None

    exact = [food for term, food in _SEARCH_INDEX if term == query]
    if exact:
        return _to_match_dict(exact[0])

    candidates = [
        (term, food) for term, food in _SEARCH_INDEX
        if query in term or term in query
    ]
    if candidates:
        best_term, best_food = max(candidates, key=lambda tf: len(tf[0]))
        return _to_match_dict(best_food)

    matches = difflib.get_close_matches(query, _SEARCH_TERMS, n=1, cutoff=cutoff)
    if not matches:
        return None

    matched_term = matches[0]
    for term, food in _SEARCH_INDEX:
        if term == matched_term:
            return _to_match_dict(food)
    return None


def _to_match_dict(food: dict) -> dict:
    names = food.get("names", {})
    return {
        "name": names.get("kurdish") or names.get("english") or food.get("id", "؟"),
        "emoji": food.get("emoji", "🍽️"),
        "id": food.get("id"),
    }


def get_food(food_id: str) -> dict | None:
    """Direct ID lookup for full entry data (macros, all serving sizes,
    confidence tag, notes) - useful outside the prompt-building/matching
    flow, e.g. for a future /food_info command or analytics."""
    return FOODS_BY_ID.get(food_id)


# --- Targeted retrieval for the second-opinion reviewer (reviewer.py) ---
#
# Unlike build_glossary_prompt() above (which has to cover every food
# because Gemini's FIRST call doesn't yet know what's in the photo - see
# that function's docstring), the reviewer runs AFTER identification, so
# it already knows which food_id(s) it's checking. That turns this into
# an ordinary lookup by known ID, not a retrieval-before-knowing problem -
# these functions pull only the entries relevant to a specific result,
# never the full 127-food database.

def get_food_context_for_review(food_ids: list[str]) -> list[dict]:
    """Trimmed entries (nutrition + confidence tag + notes only - no
    aliases/misspellings/multiple serving sizes, which the reviewer
    doesn't need) for a specific, already-known set of food_ids."""
    context = []
    for fid in dict.fromkeys(food_ids):  # de-dupe, preserve order
        food = FOODS_BY_ID.get(fid)
        if not food:
            continue
        names = food.get("names", {})
        context.append({
            "id": fid,
            "name_kurdish": names.get("kurdish"),
            "name_english": names.get("english"),
            "nutrition_per_serving": food.get("nutrition_per_serving"),
            "nutrition_per_100g": food.get("nutrition_per_100g"),
            "confidence": food.get("confidence"),  # verified/estimated/assumption
            "notes": food.get("notes"),
        })
    return context


def get_relevant_recognition_context(food_ids: list[str]) -> dict:
    """Confusion pairs, visual hints, and brand mappings that actually
    involve one of the given food_ids - typically 0-2 entries per
    request out of the full 11/7/8, not the whole recognition_rules.json.

    confusion_pairs.food_a/food_b are free-text (sometimes a clean
    food_id, sometimes a slash-separated list or an id embedded in a
    parenthetical - see recognition_rules.json) rather than a strict ID
    field, so this uses substring containment for that one field, and
    exact equality for visual_hints/brand_mappings which do use clean
    food_id fields.
    """
    ids = set(food_ids)
    if not ids:
        return {"confusion_pairs": [], "visual_hints": [], "brand_mappings": []}

    pairs = [
        p for p in CONFUSION_PAIRS
        if any(fid in p.get("food_a", "") or fid in p.get("food_b", "") for fid in ids)
    ]
    hints = [
        h for h in VISUAL_HINTS
        if h.get("likely_food_id") in ids or h.get("distinguish_from_food_id") in ids
    ]
    brands = [b for b in BRAND_MAPPINGS if b.get("food_id") in ids]

    return {"confusion_pairs": pairs, "visual_hints": hints, "brand_mappings": brands}


def build_reviewer_instructions() -> str:
    """Fixed, condensed instructions for the second-opinion reviewer -
    see data/reviewer_instructions.md for the actual content and why
    it's a separate file from data/prompt_fragments.md (different
    audience: this one is for the reviewer's critique role, not for
    Gemini's initial recognition call)."""
    return _REVIEWER_INSTRUCTIONS
