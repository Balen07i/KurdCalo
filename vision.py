# vision.py
#
# Sends a food photo + the Kurdish dish glossary (+ any learned user
# corrections) to Gemini's vision API and parses back a structured,
# multi-food calorie + macro estimate, plus a short personalized Kurdish
# nutrition insight.
#
# Uses Google's free Gemini API tier - no paid service anywhere in here.
#
# RESILIENCE NOTE: Gemini is called through the REST API using urllib from
# the Python standard library. We handle retries ourselves so we have full
# control over backoff, server retry hints, and logging.

import io
import json
import logging
import os
import re
import time
import base64
import urllib.error
import urllib.parse
import urllib.request

import knowledge
from knowledge import build_confusable_prompt, build_decision_and_style_prompt, build_glossary_prompt, find_glossary_match

logger = logging.getLogger(__name__)

# 128 MB build/runtime mode: use Gemini's REST API through Python's
# standard library instead of the google-genai SDK. This removes a large
# dependency tree (google-auth, pydantic, cryptography, websockets, etc.)
# while keeping the same Gemini API functionality.
GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

class GeminiAPIError(Exception):
    def __init__(self, code: int, message: str = ""):
        self.code = code
        self.message = message
        super().__init__(f"Gemini API HTTP {code}: {message}")


# --- Raw HTTP-layer accounting (approved observability addition) --------
#
# gemini_queue.py's counters (real_gemini_requests, rate_limited_failures,
# etc.) count DISPATCH OUTCOMES - one worker iteration = one count - not
# raw HTTP calls. A single dispatch can retry internally up to MAX_ATTEMPTS
# times on a transient error or MAX_RATE_LIMIT_ATTEMPTS times on a 429
# before giving up, so the true number of HTTP requests/HTTP 429s hitting
# Gemini's servers can be higher than the dispatch-level numbers alone
# suggest. These counters are ADDITIVE ONLY, incremented at the actual
# HTTP-call layer below - nothing about the existing dispatch-level
# counters (in gemini_queue.py) is changed or reinterpreted.
_http_stats = {
    "raw_http_attempts": 0,
    "raw_http_429_responses": 0,
    "raw_http_successes": 0,
    "raw_http_other_failures": 0,
}


def get_http_stats() -> dict:
    """Read-only snapshot, merged into gemini_queue.get_stats_summary()
    under its own keys - see that function."""
    return dict(_http_stats)


def _gemini_rest_request(model: str, payload: dict, timeout: float = 45.0) -> str:
    _http_stats["raw_http_attempts"] += 1
    api_key = os.environ["GEMINI_API_KEY"]
    encoded_model = urllib.parse.quote(model, safe="-")
    url = GEMINI_API_URL.format(model=encoded_model)
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "x-goog-api-key": api_key,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            _http_stats["raw_http_429_responses"] += 1
        else:
            _http_stats["raw_http_other_failures"] += 1
        detail = exc.read().decode("utf-8", errors="replace")
        raise GeminiAPIError(exc.code, detail) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        _http_stats["raw_http_other_failures"] += 1
        raise ConnectionError(str(exc)) from exc
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        _http_stats["raw_http_other_failures"] += 1
        raise RuntimeError(f"Invalid Gemini response: {exc}") from exc

    try:
        parts = data["candidates"][0]["content"]["parts"]
        text = "".join(part.get("text", "") for part in parts if isinstance(part, dict))
        _http_stats["raw_http_successes"] += 1
        return text
    except (KeyError, IndexError, TypeError) as exc:
        _http_stats["raw_http_other_failures"] += 1
        raise RuntimeError(f"Gemini response had no text candidate: {data}") from exc


MODEL_NAME = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
# Kept for display/backward-compat (e.g. shown in /stats). The real
# routing decision now comes from MODEL_POOL below.

# --- Model pool: the core of this round's capacity redesign -----------
#
# Format: "model:daily_budget[:rpm_limit],model:daily_budget[:rpm_limit],..."
# Example: GEMINI_MODEL_POOL=gemini-2.5-flash:225:9,gemini-2.5-flash-lite:900:14
#
# Each model gets its own daily request budget, tracked locally and
# reset at UTC midnight. gemini_queue.py uses this to route PROACTIVELY -
# stopping before a model's daily quota is exhausted, spreading each
# model's budget across the whole day instead of front-loading it, and
# load-balancing across every model in the pool instead of preferring
# one and only falling back reactively after it's already broken.
#
# The optional third field (rpm_limit) is the RPM-optimization round's
# addition: each model also gets its own requests-per-minute ceiling,
# used by gemini_queue.py to derive a PER-MODEL pacing floor. This
# replaced a single global pacing floor shared by every model in the
# pool regardless of its real RPM ceiling - concretely wrong in both
# directions at once: a faster model (e.g. Flash-Lite, typically a
# noticeably higher RPM ceiling than Flash) was needlessly held to the
# slower model's conservative pace, while a slower model could still be
# eased down (by the adaptive layer's clean-streak logic) past its own
# real ceiling, since the shared floor didn't reflect it. Omit the third
# field to fall back to DEFAULT_RPM_LIMIT for that model.
#
# Budgets and RPM limits here are deliberately conservative (roughly 90%
# of last-known free-tier limits) - Google has changed these before
# without notice, so treat these as a safety margin, not a guarantee.
# Check your actual current limits at
# https://ai.google.dev/gemini-api/docs/rate-limits and adjust if needed.
#
# Falls back to single-model mode (just MODEL_NAME, generous default
# budget/RPM) if GEMINI_MODEL_POOL isn't set - fully backward compatible
# with existing single-model deployments.
DEFAULT_DAILY_BUDGET = 200
DEFAULT_RPM_LIMIT = 8  # conservative single-digit fallback; override per
# model with the third GEMINI_MODEL_POOL field once you've checked your
# project's actual current limit.


def _parse_model_pool() -> tuple[dict[str, int], dict[str, int]]:
    raw = os.environ.get("GEMINI_MODEL_POOL", "")
    pool: dict[str, int] = {}
    rpm: dict[str, int] = {}
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        parts = entry.split(":")
        model = parts[0].strip()
        budget = DEFAULT_DAILY_BUDGET
        rpm_limit = DEFAULT_RPM_LIMIT
        if len(parts) >= 2 and parts[1].strip():
            try:
                budget = int(parts[1].strip())
            except ValueError:
                pass
        if len(parts) >= 3 and parts[2].strip():
            try:
                rpm_limit = int(parts[2].strip())
            except ValueError:
                pass
        pool[model] = budget
        rpm[model] = rpm_limit
    if not pool:
        pool = {MODEL_NAME: DEFAULT_DAILY_BUDGET}
        rpm = {MODEL_NAME: DEFAULT_RPM_LIMIT}
    return pool, rpm


MODEL_POOL, MODEL_RPM_LIMIT = _parse_model_pool()


# IMPORTANT: gemini-2.5-flash spends part of max_output_tokens on internal
# "thinking" before it writes the final answer. Too small a budget was the
# root cause of a real bug where JSON got truncated mid-way and silently
# fell back to a fake "Unknown / 0 kcal" result. thinking_budget caps how
# much of the budget thinking can eat; max_output_tokens leaves generous
# room for the JSON itself on top of that.
THINKING_BUDGET = 512
MAX_OUTPUT_TOKENS = 2400

# Retry tuning. RETRYABLE_STATUS_CODES per Google's own troubleshooting
# guidance (429 rate limit, 408/500/502/503/504 transient server issues).
# 400/401/403/404 are NOT retried - those mean something is wrong with the
# request itself (bad key, bad payload) and retrying won't help.
#
# IMPORTANT (fixes a real production bug): Gemini's 429 error message can
# suggest a retry delay far longer than a per-minute limit would imply -
# under sustained free-tier pressure it can legitimately say "retry in
# 400+ seconds" (e.g. a daily-quota-style reset, not a burst limit). If we
# blindly slept for that full duration, one unlucky photo could block the
# single worker (and therefore every user queued behind it) for many
# minutes. MAX_BACKOFF_SECONDS below is a hard ceiling applied to BOTH
# our own exponential backoff AND any server-suggested delay - we would
# rather fail fast with a clear "try again shortly" message than make
# someone stare at "🔍 analyzing..." for 7 minutes. Worst case total time
# for one photo: MAX_ATTEMPTS-1 retry gaps × MAX_BACKOFF_SECONDS ≈ 90s,
# not minutes.
MAX_ATTEMPTS = 4
RETRYABLE_STATUS_CODES = {408, 429, 500, 502, 503, 504}
BASE_BACKOFF_SECONDS = 2.0
MAX_BACKOFF_SECONDS = 30.0

# RPM-specific retry tuning (added in the RPM-optimization round).
#
# THE FIX for a real waste: a 429 means we're currently OVER the model's
# per-minute ceiling. The generic exponential backoff above (2s, 4s, 8s)
# was designed for transient 5xx errors, which really do often clear in
# a few seconds - but Gemini's RPM window is a ~60s rolling window, so
# retrying a 429 after only 2-4-8s is retrying against a window that has
# almost certainly not moved yet. In production this meant most 429'd
# jobs burned all MAX_ATTEMPTS (4 real requests) doomed from the second
# attempt onward, which (a) wastes requests that count against the very
# limit we just got rejected by round-tripping again, and (b) delays the
# queue's reactive AIMD/circuit-breaker feedback, since that only reacts
# once the whole job finally gives up.
#
# Fix: 429s get their OWN short attempt budget (one real retry) with a
# meaningfully longer backoff - long enough to plausibly land in a new
# per-minute window - instead of sharing the fast 5xx schedule. This cuts
# wasted requests roughly in half per rate-limited job while giving the
# one retry that remains a real chance to land clean, and returns control
# to the outer pacer sooner.
MAX_RATE_LIMIT_ATTEMPTS = 2
RATE_LIMIT_BACKOFF_SECONDS = 15.0

INSIGHT_STYLE_EXAMPLES = """\
- 💪 ئەم خواردنە سەرچاوەیەکی باشی پرۆتینە.
- 🥗 زیادکردنی سەوزە فایبەری خواردنەکەت زیاد دەکات.
- 🔥 بۆ کێش زیادکردن زۆر گونجاوە.
- ⚖️ ئەگەر ئامانجت کەمکردنەوەی کێشە، واباشە قەبارەی برنج کەمتر بێت.
- 💧 بیرت نەچێت لەگەڵ ئەم خواردنە ئاوی تەواو بخۆیت."""


def optimize_image(image_bytes: bytes) -> bytes:
    """
    128 MB mode: do not decode/re-encode images locally.

    Pillow was previously used for resizing and hashing, but it adds a large
    native dependency and significant peak memory during pip installation and
    image decoding. Telegram already provides compressed photo files, so the
    lowest-memory path is to pass those bytes through unchanged.
    """
    return image_bytes


# --- Local pre-filter ------------------------------------------------------
# In 128 MB mode we avoid decoding images locally. Gemini handles visual
# validity; this filter only catches an obviously empty/tiny byte payload.
MIN_PIXEL_STDDEV = 5.0     # near-uniform/blank frame if variance is this low
MIN_MEAN_BRIGHTNESS = 8.0  # near-total-black frame (0-255 scale)
MIN_DIMENSION_PX = 80


def check_image_locally(image_bytes: bytes) -> tuple[bool, str]:
    """128 MB mode: lightweight byte-size sanity check only.

    Image decoding is intentionally avoided. Gemini remains responsible for
    deciding whether the photo actually contains food.
    """
    if not image_bytes:
        return False, "📷 وێنەکە بەتاڵە. تکایە دووبارە وێنەکە بنێرەوە."
    if len(image_bytes) < 256:
        return False, "📷 وێنەکە زۆر بچووکە. تکایە وێنەیەکی ئاساییی خواردنەکە بنێرە."
    return True, ""


# --- Duplicate detection -------------------------------------------------
# Pillow is intentionally not installed on the 128 MB plan. Exact SHA-256
# caching in gemini_queue still catches identical uploads; dHash is disabled.
def compute_dhash(image_bytes: bytes, hash_size: int = 8) -> int | None:
    return None


def hamming_distance(a: int, b: int) -> int:
    return bin(a ^ b).count("1")

def _build_corrections_prompt(corrections: list[dict]) -> str:
    """
    Folds in real user corrections as extra glossary-like context. This is
    the whole "learning" loop: a mistake fixed once by any user quietly
    improves recognition for everyone afterward, at zero extra cost -
    it's just more text in the same prompt, no retraining, no new service.
    """
    if not corrections:
        return ""
    lines = [
        f"- If something looks like \"{c['wrong_name']}\", a real user "
        f"corrected this before to: {c['correct_name_kurdish']}"
        for c in corrections[:60]  # keep the prompt bounded
    ]
    return (
        "\n\nReal corrections submitted by users - treat these as reliable "
        "signal, they've been confirmed by a human:\n" + "\n".join(lines)
    )


def _build_system_prompt(corrections: list[dict]) -> str:
    return f"""You are a friendly nutrition-tracking assistant for a Kurdish \
(Sorani) audience in the Kurdistan Region. You will be shown a photo of a meal \
that may contain one or several different foods on the same plate or table.

{build_glossary_prompt()}

{build_confusable_prompt()}

{build_decision_and_style_prompt()}
{_build_corrections_prompt(corrections)}

REASON IN THIS ORDER (internally - only the final JSON is shown to the user):
1. Identify: scan the whole image and list every distinct FOOD item - \
including drinks, sauces, oils, bread, sides, and garnishes, not just the \
main dish. Ignore plates, bowls, cups, cutlery, napkins, hands, tables, and \
background objects - those are not foods. Do not list plain water as a \
food item (it has no calories/macros to track) unless it's genuinely the \
focus of the photo - don't waste space on obvious zero-calorie items. \
Never invent an ingredient you cannot reasonably see (e.g. don't assume \
butter or oil was used unless there's visible sheen/pooling, don't assume \
a sauce is present unless you can actually see it). If two foods are \
touching or slightly overlapping, list them as separate items - never \
merge different foods into one entry.
2. Verify: before finalizing your list, check each item against the photo \
one more time - could you point to the exact pixels showing this food? \
Foods get added by habit/expectation (e.g. assuming bread accompanies a \
meal because it usually does) even when not actually visible - if you \
can't clearly justify an item from what's actually in the photo, remove \
it. A shorter, accurate list is always better than a complete-feeling but \
partly invented one.
3. Portion: for EACH food, estimate its portion PRIMARILY IN GRAMS - this \
is the default unit. Use visible reference objects to judge scale (a \
standard dinner plate is ~25-28cm across, a tablespoon holds ~15ml, a \
person's palm is roughly 100-150g of meat, a teacup is ~200ml, an egg is \
~50g) to convert what you see into a gram estimate. Only fall back to a \
non-gram unit ("١ پلێت" / "١ کاسە") when the food genuinely cannot be \
reasonably expressed in grams (e.g. a mixed stew filling a whole bowl \
with no way to separate components by weight) - never default to cups/\
bowls/spoons just because it's easier to say. Whatever unit you use, a \
photo never gives an exact weight - phrase the portion as a visual \
estimate (e.g. "~250 گرام (خەمڵێنراو)"), never as if it were measured. \
For food that's mixed, stacked, or spread across the plate, estimate \
weight by visualizing it separated out, not just by its 2D footprint.
4. Nutrition: for EACH food, calculate calories, protein, carbs, and fat \
scaled to that estimated gram portion. PROTEIN NEEDS EXTRA CARE - do not \
apply a generic "this food category = X g protein" value:
   - Distinguish cooked vs raw when it changes the number meaningfully \
(cooked meat is denser in protein per gram than raw, since raw meat is \
partly water that cooks off - use the cooked-state reference unless the \
food is visibly raw).
   - For meat/fish/chicken with visible bone, skin, or shell, estimate \
the EDIBLE portion's weight, not the photographed whole-piece weight - a \
180g drumstick with bone and skin is meaningfully less than 180g of \
actual meat protein. Don't pretend the whole photographed weight is pure \
edible meat.
   - Keep kcal/protein/carbs/fat internally consistent with each other \
(kcal should roughly equal protein_g*4 + carbs_g*4 + fat_g*9, allowing \
normal rounding) - if your own numbers would clearly contradict each \
other, reconsider them before finalizing, not after.
   - Factor in the visible cooking method (grilled/fried/boiled/roasted/ \
oily rice, etc. change fat content significantly) and any oil, butter, \
ghee, or sauce you can ACTUALLY see evidence of (sheen, pooling, visible \
glaze) - never add these by default "just in case". When cooking method \
clearly implies added fat but the exact amount isn't visually \
determinable (typical for fried or "oily rice"-style dishes), use a \
reasonable mid-range estimate for that cooking method rather than \
inventing a precise oil quantity you can't actually see - approximate is \
fine, false precision is not.
5. Consistency check: before writing the final JSON, re-read your own \
foods list, note_kurdish, and insight_kurdish together - do they describe \
the SAME foods? If note_kurdish or insight_kurdish mentions any food, \
ingredient, or dish name that is NOT one of the items in your foods list \
(e.g. you list an item as "هەنجیر" but your description talks about \
honey), that's a contradiction - fix it so the item list and the written \
text agree before responding, don't let a leftover assumption from an \
earlier reasoning step slip into the final text.
6. Only after steps 1-5 are done for every item, produce the final JSON.

BRAND RECOGNITION: if a packaged/branded product is visible and its name \
is legible on the packaging or logo, use the actual product name (e.g. \
"Ülker Biskrem") instead of a generic description ("chocolate cream \
biscuit") - this is both more useful to the user and more nutritionally \
accurate, since branded products have a real, specific nutrition profile \
rather than a generic estimate. Keep the brand name as written, don't \
translate it.

RECOGNIZING COMMON STAPLES - be decisive, not cautious:
Rice, bread, grilled or stewed meat, chicken, fish, eggs, milk/yogurt, \
fruit, and vegetables are everyday foods that are usually easy to recognize \
even in an imperfect photo. If any of these (or anything else reasonably \
identifiable) is visible, you MUST attempt a real estimate. Only use \
"no food detected" when the photo genuinely shows no food at all.

HANDLING DIFFICULT PHOTOS:
Low light, partial plates, close-up crops, mild blur, or mixed/overlapping \
meals should NOT stop you from estimating - they should lower your \
confidence instead, with a short honest note about what made it harder.

CONFIDENCE RULES - recognition confidence and nutrition-estimation \
confidence are DIFFERENT things, and the final confidence must reflect \
BOTH, not just whether you recognized the food:
- Recognizing WHAT a food is (e.g. "this is grilled chicken") does not by \
itself justify "بەرز" (high) confidence for the overall result.
- The final confidence must also reflect how certain the PORTION, \
ingredients, and resulting calorie/macro numbers are. If the portion is \
highly uncertain (unclear scale, no reference object, food mostly hidden \
or cut off), the overall confidence must NOT be "بەرز" even if the food \
identity itself is obvious - cap it at "مامناوەند" or lower.
- Base confidence on: image clarity/lighting, how clearly each food is \
visible, how certain the identification is, AND how visible/estimable the \
portion and cooking method are. The NUMBER OF FOOD ITEMS is never itself \
a reason to lower confidence.

UNCERTAIN IDENTIFICATION - be honest, not confidently wrong:
If you cannot confidently identify a SPECIFIC food (not the confusable-pair \
case below, just general uncertainty - e.g. an unfamiliar stew, a dish \
partly hidden by another, a sauce you don't recognize), do NOT invent a \
specific dish name to sound complete. Instead, use an honest general label \
(e.g. "خواردنێکی نەناسراو" / "stew, exact dish unclear") and set that food's \
contribution to "نزم" confidence overall, with note_kurdish explaining what \
made it hard to identify. A vague-but-honest label beats a specific-but-\
invented one - the user can always correct it, but a confident wrong guess \
teaches them nothing and may be trusted at face value.

GENUINE AMBIGUITY - use "alternatives", not a guess:
If (and ONLY if) you genuinely cannot tell apart two visually similar foods \
(see the confusable pairs above) even after looking carefully, do NOT \
silently pick one. Instead, keep your best single guess as the main \
"foods"/totals, but ALSO fill "alternatives" with 1-2 other plausible \
readings, each with what the total_kcal would be under that reading and a \
short explanation of the visual ambiguity. If you ARE reasonably confident, \
leave "alternatives" as an empty list - don't manufacture uncertainty that \
isn't there.

FOOD MATCHING:
Match foods to the glossary by meaning, not exact wording (e.g. "grilled \
chicken" should match a glossary chicken entry even worded differently). \
When matched, reuse the glossary's exact Kurdish name and emoji.

LANGUAGE RULES:
- Every piece of Kurdish text must be natural, everyday spoken Sorani \
Kurdish as used in the Kurdistan Region - never a stiff or literal \
translation. Keep terminology (کالۆری, پرۆتین, کاربۆهایدرەیت, چەوری) \
consistent.
- Do not attach English names to food unless there is genuinely no common \
Kurdish name for it.
- Prefer the authentic local Kurdish dish name over a generic or \
international-sounding translation whenever a real local name exists - \
e.g. use the actual Kurdish name for a traditional dish rather than a \
generic "rice with meat"-style description. The glossary above reflects \
this - reuse its names and aliases rather than inventing a more generic \
alternative.
- The one exception to writing in Kurdish is a recognized brand name (see \
BRAND RECOGNITION above) - keep those exactly as printed on the package.

Write ONE short, natural, non-judgmental nutrition insight in Kurdish \
specific to what was actually detected - not a generic tip. Match this \
tone and length (write a NEW sentence, don't copy these):
{INSIGHT_STYLE_EXAMPLES}

Respond ONLY with a JSON object, no other text, no markdown fences, in this \
exact shape:
{{
  "no_food_detected": true or false,
  "foods": [
    {{
      "name_kurdish": "...", "emoji": "🍚", "portion_kurdish": "...",
      "kcal": integer, "protein_g": integer, "carbs_g": integer, "fat_g": integer,
      "matched_glossary": true or false
    }}
  ],
  "total_kcal": integer, "total_protein_g": integer,
  "total_carbs_g": integer, "total_fat_g": integer,
  "confidence": "بەرز" | "مامناوەند" | "نزم",
  "note_kurdish": "one short sentence",
  "insight_kurdish": "one short sentence, starting with a fitting emoji",
  "alternatives": [
    {{
      "scenario_kurdish": "e.g. ئەگەر سرکەی هەنار بێت",
      "total_kcal": integer,
      "explanation_kurdish": "one short sentence on why this is uncertain"
    }}
  ]
}}

If no_food_detected is true, "foods" must be empty and other numeric fields \
must be 0. "alternatives" must be an empty list unless there's genuine \
visual ambiguity as described above. Macro rule: kcal MUST roughly equal \
protein_g*4 + carbs_g*4 + fat_g*9 for every item and for the totals - \
normal rounding is fine, but do not submit numbers that clearly \
contradict this without a real reason (e.g. significant alcohol or fiber \
content). Totals must equal the sum of all items."""


def _call_gemini(image_bytes: bytes, media_type: str, strict: bool, corrections: list[dict], model: str = MODEL_NAME) -> str:
    prompt_text = "Analyze every food in this meal photo."
    if strict:
        prompt_text += (
            " Your previous response was not valid JSON. Respond with "
            "ONLY the JSON object described in your instructions - no "
            "markdown fences, no commentary, nothing before or after it."
        )

    payload = {
        "systemInstruction": {
            "parts": [{"text": _build_system_prompt(corrections)}]
        },
        "contents": [{
            "role": "user",
            "parts": [
                {
                    "inline_data": {
                        "mime_type": media_type,
                        "data": base64.b64encode(image_bytes).decode("ascii"),
                    }
                },
                {"text": prompt_text},
            ],
        }],
        "generationConfig": {
            "responseMimeType": "application/json",
            "maxOutputTokens": MAX_OUTPUT_TOKENS,
            "thinkingConfig": {"thinkingBudget": THINKING_BUDGET},
        },
    }
    return _gemini_rest_request(model, payload)


def _extract_json(raw_text: str):
    """Tolerant JSON extraction: handles clean JSON, markdown-fenced JSON,
    and JSON with stray text before/after it."""
    text = raw_text.strip()
    if not text:
        return None

    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:].strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass

    return None


_RETRY_AFTER_PATTERN = re.compile(r"retry in (\d+(?:\.\d+)?)s", re.IGNORECASE)


def _extract_retry_after_seconds(exc: Exception) -> float | None:
    """
    Gemini's REST error body does not guarantee a structured retry delay, but 429
    messages usually
    contain it as text (e.g. "...Please retry in 53.0s."). Best-effort
    extraction from the message; falls back to None (caller uses
    exponential backoff instead) if it's not present.
    """
    match = _RETRY_AFTER_PATTERN.search(str(exc))
    if match:
        return float(match.group(1))
    return None


def _classify_and_log(exc: Exception, attempt: int) -> tuple[bool, float | None]:
    """
    Logs the failure with a clear, grep-able tag and returns
    (is_retryable, suggested_wait_seconds).
    """
    if isinstance(exc, GeminiAPIError):
        code = exc.code
        if code == 429:
            wait = _extract_retry_after_seconds(exc)
            logger.warning(
                "[RATE_LIMIT] Gemini 429 on attempt %d, server suggests %s "
                "(capped to %.0fs max before use): %s",
                attempt, f"{wait}s" if wait else "no delay given", MAX_BACKOFF_SECONDS, exc,
            )
            return True, wait
        if code in RETRYABLE_STATUS_CODES:
            logger.warning("[SERVER_ERROR] Gemini %d on attempt %d: %s", code, attempt, exc)
            return True, None
        logger.error("[API_ERROR] Non-retryable Gemini error %s on attempt %d: %s", code, attempt, exc)
        return False, None

    if isinstance(exc, (TimeoutError, ConnectionError)):
        logger.warning("[TIMEOUT] Network timeout/connection error on attempt %d: %s", attempt, exc)
        return True, None

    logger.exception("[UNEXPECTED] Unhandled exception on attempt %d", attempt)
    return True, None  # unknown errors get one benefit-of-the-doubt retry cycle


def estimate_calories(
    image_bytes: bytes, media_type: str = "image/jpeg", corrections: list[dict] | None = None,
    model: str = MODEL_NAME,
) -> dict:
    """
    Makes EXACTLY ONE logical analysis per call (retries below are for
    resilience against transient failures, not duplicate work - a photo
    that succeeds on attempt 1 never triggers attempt 2).

    NOTE: expects image_bytes to already be optimized (see optimize_image
    below) - the caller (gemini_queue.submit_photo_job's caller) should
    call optimize_image() BEFORE this, and ideally before even enqueueing,
    so the queue never holds full-size originals under a backlog.

    Returns a dict with a "status" key:
      - "ok": normal result (see _finalize_result for full shape). Also
        includes "model_used" for A/B attribution.
      - "no_food": Gemini determined the photo genuinely has no food
      - "failed": technical failure. Includes a "reason":
          - "rate_limited": all retries exhausted while being rate-limited
            - caller should show the "bot is busy" message, not tips
          - "other": non-rate-limit failure - caller should show photo tips
    """
    corrections = corrections or []
    parsed = None
    last_failure_was_rate_limit = False
    total_attempts = 0
    billable_attempts = 0  # excludes 429s - see _record_daily_request's docstring for why
    rate_limit_attempts = 0  # separate short budget - see MAX_RATE_LIMIT_ATTEMPTS

    for attempt in range(1, MAX_ATTEMPTS + 1):
        strict = attempt > 1  # ask more firmly for clean JSON after the first miss
        total_attempts += 1
        try:
            raw_text = _call_gemini(image_bytes, media_type, strict, corrections, model=model)
            billable_attempts += 1
        except Exception as exc:
            is_retryable, suggested_wait = _classify_and_log(exc, attempt)
            last_failure_was_rate_limit = isinstance(exc, GeminiAPIError) and exc.code == 429
            if not last_failure_was_rate_limit:
                billable_attempts += 1  # a real processed failure (e.g. 503), not a pre-processing rejection
            else:
                rate_limit_attempts += 1

            if not is_retryable or attempt == MAX_ATTEMPTS:
                break
            if last_failure_was_rate_limit and rate_limit_attempts >= MAX_RATE_LIMIT_ATTEMPTS:
                logger.warning(
                    "[RATE_LIMIT] Giving up after %d rate-limited attempt(s) - not burning more "
                    "requests against a per-minute window that just rejected us", rate_limit_attempts,
                )
                break

            if last_failure_was_rate_limit:
                # A meaningfully longer, RPM-window-aware backoff (see
                # MAX_RATE_LIMIT_ATTEMPTS above) - honor a server-suggested
                # delay if it's longer than our floor, capped the same as
                # everything else.
                wait = max(suggested_wait or 0.0, RATE_LIMIT_BACKOFF_SECONDS)
                wait = min(wait, MAX_BACKOFF_SECONDS)
            else:
                # THE FIX: cap suggested_wait too, not just our own computed
                # backoff - a server-suggested delay is honored up to the
                # same ceiling, never blindly trusted for its full duration.
                computed_backoff = min(BASE_BACKOFF_SECONDS * (2 ** (attempt - 1)), MAX_BACKOFF_SECONDS)
                wait = min(suggested_wait, MAX_BACKOFF_SECONDS) if suggested_wait else computed_backoff
            time.sleep(wait)
            continue

        parsed = _extract_json(raw_text)
        if parsed is not None:
            break

        logger.warning("[JSON_PARSE] Could not parse Gemini response on attempt %d: %.200s", attempt, raw_text)
        if attempt < MAX_ATTEMPTS:
            time.sleep(1)  # brief pause before the stricter retry, not rate-limit related

    if parsed is None:
        return {
            "status": "failed",
            "reason": "rate_limited" if last_failure_was_rate_limit else "other",
            "total_attempts": total_attempts,
            "billable_attempts": billable_attempts,
        }

    result = _finalize_result(parsed)
    result["model_used"] = model
    result["total_attempts"] = total_attempts
    result["billable_attempts"] = billable_attempts
    return result


# --- Batch estimation ------------------------------------------------
#
# The single highest-impact quota optimization available: Gemini's RPM
# limit counts REQUESTS, not images-per-request. A single request can
# contain multiple images. So when several photos are ALREADY queued at
# once (real concurrent load - the exact scenario the free tier struggles
# with), analyzing them in ONE request instead of N directly and
# proportionally reduces request count, which is the actual bottleneck.
#
# This does NOT engage for the common case of one photo at a time (see
# gemini_queue.py - batching is opportunistic, never adds artificial
# wait time hoping more photos arrive). It also intentionally caps batch
# size low (see gemini_queue.BATCH_SIZE) - a batched request's fate is
# shared across all images in it, so an unbounded batch size would mean
# one bad request fails many users at once instead of one.

BATCH_MAX_OUTPUT_TOKENS_PER_IMAGE = 900  # extra headroom per image beyond the base budget


def _build_batch_system_prompt(corrections: list[dict], batch_size: int) -> str:
    base_prompt = _build_system_prompt(corrections)
    return base_prompt + f"""

BATCH MODE: you will be shown {batch_size} SEPARATE, UNRELATED meal photos in \
this one request, labeled "Image 1", "Image 2", etc. Analyze EACH one \
completely independently - they are different meals from possibly \
different people, never combine or cross-reference foods between them.

Respond ONLY with a JSON object of this exact shape, no other text:
{{
  "results": [
    <result for Image 1, using the exact single-image JSON shape described above>,
    <result for Image 2, same shape>,
    ...
  ]
}}
"results" must contain exactly {batch_size} objects, in the same order as \
the images were shown."""


def _call_gemini_batch(
    images: list[tuple[bytes, str]], strict: bool, corrections: list[dict], model: str = MODEL_NAME
) -> str:
    contents_parts = []
    for i, (image_bytes, media_type) in enumerate(images, start=1):
        contents_parts.append({"text": f"Image {i}:"})
        contents_parts.append({
            "inline_data": {
                "mime_type": media_type,
                "data": base64.b64encode(image_bytes).decode("ascii"),
            }
        })

    prompt_text = "Analyze every food in each of these meal photos independently."
    if strict:
        prompt_text += (
            " Your previous response was not valid JSON. Respond with "
            "ONLY the JSON object described in your instructions - no "
            "markdown fences, no commentary, nothing before or after it."
        )
    contents_parts.append({"text": prompt_text})

    payload = {
        "systemInstruction": {
            "parts": [{"text": _build_batch_system_prompt(corrections, len(images))}]
        },
        "contents": [{"role": "user", "parts": contents_parts}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "maxOutputTokens": MAX_OUTPUT_TOKENS + BATCH_MAX_OUTPUT_TOKENS_PER_IMAGE * len(images),
            "thinkingConfig": {"thinkingBudget": THINKING_BUDGET},
        },
    }
    return _gemini_rest_request(model, payload)


def estimate_calories_batch(
    images: list[tuple[bytes, str]], corrections: list[dict] | None = None, model: str = MODEL_NAME
) -> dict:
    """
    images: list of (image_bytes, media_type) tuples, all already optimized.
    All images in a batch share ONE model - callers (gemini_queue) must
    only combine jobs that were assigned the same model for A/B testing.

    Returns a dict with a "status" key:
      - "ok": "results" is a list of finalized per-image results, same
        length and order as `images`, each shaped exactly like a single
        estimate_calories() return value.
      - "failed": the WHOLE batch failed (see "reason", same semantics as
        estimate_calories) - caller must apply this outcome to every job
        in the batch, since it was one shared request.
    """
    corrections = corrections or []
    parsed = None
    last_failure_was_rate_limit = False
    total_attempts = 0
    billable_attempts = 0
    rate_limit_attempts = 0  # see MAX_RATE_LIMIT_ATTEMPTS - same reasoning as estimate_calories

    for attempt in range(1, MAX_ATTEMPTS + 1):
        strict = attempt > 1
        total_attempts += 1
        try:
            raw_text = _call_gemini_batch(images, strict, corrections, model=model)
            billable_attempts += 1
        except Exception as exc:
            is_retryable, suggested_wait = _classify_and_log(exc, attempt)
            last_failure_was_rate_limit = isinstance(exc, GeminiAPIError) and exc.code == 429
            if not last_failure_was_rate_limit:
                billable_attempts += 1
            else:
                rate_limit_attempts += 1

            if not is_retryable or attempt == MAX_ATTEMPTS:
                break
            if last_failure_was_rate_limit and rate_limit_attempts >= MAX_RATE_LIMIT_ATTEMPTS:
                logger.warning(
                    "[RATE_LIMIT] [batch] Giving up after %d rate-limited attempt(s) - not burning "
                    "more requests against a per-minute window that just rejected us", rate_limit_attempts,
                )
                break

            if last_failure_was_rate_limit:
                wait = max(suggested_wait or 0.0, RATE_LIMIT_BACKOFF_SECONDS)
                wait = min(wait, MAX_BACKOFF_SECONDS)
            else:
                computed_backoff = min(BASE_BACKOFF_SECONDS * (2 ** (attempt - 1)), MAX_BACKOFF_SECONDS)
                wait = min(suggested_wait, MAX_BACKOFF_SECONDS) if suggested_wait else computed_backoff
            time.sleep(wait)
            continue

        parsed = _extract_json(raw_text)
        if parsed is not None and isinstance(parsed.get("results"), list) and len(parsed["results"]) == len(images):
            break
        logger.warning(
            "[JSON_PARSE] Batch response invalid or wrong length on attempt %d: %.200s",
            attempt, raw_text,
        )
        parsed = None
        if attempt < MAX_ATTEMPTS:
            time.sleep(1)

    if parsed is None:
        return {
            "status": "failed",
            "reason": "rate_limited" if last_failure_was_rate_limit else "other",
            "total_attempts": total_attempts,
            "billable_attempts": billable_attempts,
        }

    finalized = [_finalize_result(r) for r in parsed["results"]]
    for r in finalized:
        r["model_used"] = model
    return {
        "status": "ok", "results": finalized,
        "total_attempts": total_attempts, "billable_attempts": billable_attempts,
    }


def _validate_consistency(result: dict) -> dict:
    """
    Lightweight, zero-dependency sanity check (Stage 1 priority #3 and
    part of #6) - catches the class of bug where Gemini's food LIST and
    its written note_kurdish/insight_kurdish TEXT disagree (e.g. an item
    is labeled "هەنجیر"/fig, but the description talks about honey - a
    real observed failure). This is a plain substring scan over the
    already-loaded glossary name list, not an ML/embedding check - cheap
    enough to run on every result on a 128 MB server, no new dependency.

    Only checks glossary food names (knowledge.FOODS) since those are the
    only names we can check against something concrete - it can't catch
    every possible contradiction, just this specific, previously-observed
    failure pattern, which is exactly what "lightweight" means here.
    """
    foods = result.get("foods") or []
    detected_names = {f.get("name_kurdish", "") for f in foods}
    text_to_check = f"{result.get('note_kurdish', '')} {result.get('insight_kurdish', '')}"
    if not text_to_check.strip():
        return result

    for food_entry in knowledge.FOODS:
        glossary_name = (food_entry.get("names") or {}).get("kurdish", "")
        if len(glossary_name) < 3:  # avoid short-name false positives
            continue
        if glossary_name in detected_names:
            continue  # this food IS actually in the result - not a contradiction
        # Word-boundary check, not raw substring containment: Kurdish is
        # agglutinative (suffixes like -تر/-ترین/-یش attach directly, no
        # space), so a short glossary name like "شیر" (milk) can appear
        # as a coincidental substring inside a longer, unrelated word
        # like "شیرینترین" (sweetest) - a real false positive caught
        # during testing. Requiring non-letter/edge-of-string on both
        # sides of the match avoids that without needing a real
        # tokenizer.
        pattern = r"(?<![\u0600-\u06FF])" + re.escape(glossary_name) + r"(?![\u0600-\u06FF])"
        if re.search(pattern, text_to_check):
            logger.warning(
                "[CONSISTENCY] note/insight text mentions %r, which is not in the "
                "detected foods list (%s) - clearing the mismatched text rather "
                "than showing a self-contradictory result.",
                glossary_name, sorted(detected_names),
            )
            result["note_kurdish"] = ""
            result["insight_kurdish"] = ""
            break  # one contradiction is enough to know the text isn't trustworthy

    # Passive macro-consistency check (Stage 1 priority #6) - logged for
    # visibility/prompt-tuning purposes only. Deliberately does NOT alter
    # the displayed numbers: unlike reviewer.py's corrections, there is no
    # grounded reference value to correct TO here, so silently changing
    # Gemini's numbers could just as easily make them worse.
    for food in foods:
        kcal = food.get("kcal", 0)
        implied = food.get("protein_g", 0) * 4 + food.get("carbs_g", 0) * 4 + food.get("fat_g", 0) * 9
        if kcal > 50 and implied > 0 and abs(kcal - implied) / kcal > 0.35:
            logger.warning(
                "[CONSISTENCY] %r: kcal=%d but protein/carbs/fat imply ~%d kcal "
                "(>35%% mismatch) - not auto-corrected, logged for visibility.",
                food.get("name_kurdish", "?"), kcal, implied,
            )

    return result


def _finalize_result(result: dict) -> dict:
    """Fills in safe fallbacks, applies fuzzy glossary matching as a
    safety net, and recomputes totals from the items so the numbers stay
    internally consistent even if the model's own math drifts."""

    if result.get("no_food_detected") and not result.get("foods"):
        return {"status": "no_food"}

    foods = result.get("foods") or []
    if not foods:
        # No explicit "no food" flag but also nothing returned - treat as
        # a technical failure, never show a fake 0-kcal "Unknown" result.
        return {"status": "failed"}

    for food in foods:
        food.setdefault("name_kurdish", "خواردنێکی نەناسراو")
        food.setdefault("emoji", "🍽️")
        food.setdefault("portion_kurdish", "نادیار")
        food.setdefault("kcal", 0)
        food.setdefault("protein_g", 0)
        food.setdefault("carbs_g", 0)
        food.setdefault("fat_g", 0)
        food.setdefault("matched_glossary", False)

        if not food["matched_glossary"]:
            match = find_glossary_match(food["name_kurdish"])
            if match:
                food["matched_glossary"] = True
                food["name_kurdish"] = match["name"]
                food["emoji"] = match["emoji"]
                food["food_id"] = match["id"]  # persisted for reviewer.py's targeted
                # knowledge retrieval (get_food_context_for_review) - previously
                # computed here and immediately discarded. Purely additive: no
                # existing consumer of this dict reads "food_id", so this cannot
                # change any existing behavior or output.

    result["foods"] = foods
    result.setdefault("total_kcal", sum(f["kcal"] for f in foods))
    result.setdefault("total_protein_g", sum(f["protein_g"] for f in foods))
    result.setdefault("total_carbs_g", sum(f["carbs_g"] for f in foods))
    result.setdefault("total_fat_g", sum(f["fat_g"] for f in foods))
    result.setdefault("confidence", "مامناوەند")
    result.setdefault("note_kurdish", "")
    result.setdefault("insight_kurdish", "")
    result.setdefault("alternatives", [])

    result = _validate_consistency(result)

    result["status"] = "ok"
    return result
