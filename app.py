# app.py
#
# The web entry point. Deliberately NOT a port of bot.py - bot.py is
# Telegram-specific (update objects, chat IDs, PTB's dispatcher) and
# none of that applies here. What IS reused, unmodified, from the
# original project: vision.py (Gemini calls, retries, JSON parsing),
# gemini_queue.py (queue, per-model pacing, daily budget, circuit
# breaker, batching, duplicate-image cache), knowledge.py (the food
# glossary/recognition-rules loader). None of those three files import
# anything Telegram-related - confirmed before reuse, not assumed.
#
# What's NEW here: the HTTP layer itself (this file), and quota.py (a
# small anonymous device-id+IP daily limiter - see its own docstring
# for why it's a new module rather than a reuse of storage.py's
# Telegram-account-shaped reserve_daily_slot()).
#
# NOT included in this MVP by design (present in the original project,
# intentionally left out here for a minimal surface area): storage.py
# (permanent meal history/A-B testing - this MVP stores no per-scan
# history at all), reviewer.py (optional second-opinion quality check,
# needs its own separate API key), bot.py (Telegram-only).

from __future__ import annotations

import logging
import os
import secrets

from fastapi import FastAPI, File, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

import gemini_queue
import quota

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("app")

# --- Configuration (env vars - see .env.example) -------------------------
FRONTEND_ORIGINS = [o.strip() for o in os.environ.get("FRONTEND_ORIGIN", "").split(",") if o.strip()]
MAX_UPLOAD_MB = float(os.environ.get("MAX_UPLOAD_MB", "8"))
MAX_UPLOAD_BYTES = int(MAX_UPLOAD_MB * 1024 * 1024)
DEVICE_COOKIE_NAME = "kc_device_id"
DEVICE_COOKIE_MAX_AGE = 60 * 60 * 24 * 365  # 1 year

ALLOWED_CONTENT_TYPES = {
    "image/jpeg": "image/jpeg",
    "image/jpg": "image/jpeg",
    "image/png": "image/png",
    "image/webp": "image/webp",
}

app = FastAPI(title="KurdCalo API")

if not FRONTEND_ORIGINS:
    logger.warning(
        "[CONFIG] FRONTEND_ORIGIN is not set - CORS will reject every "
        "browser request. Set it to your Cloudflare Pages URL, e.g. "
        "https://kurdcalo.pages.dev (see .env.example)."
    )

app.add_middleware(
    CORSMiddleware,
    allow_origins=FRONTEND_ORIGINS,
    allow_credentials=True,  # required for the device-id cookie to be sent cross-origin
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def on_startup():
    gemini_queue.start_workers()
    logger.info(
        "[STARTUP] Model pool: %s | daily scan limit per device/IP: %d | max upload: %.1fMB",
        list(gemini_queue.MODEL_POOL), quota.DAILY_SCAN_LIMIT, MAX_UPLOAD_MB,
    )


def _get_device_id(request: Request) -> tuple[str, str | None]:
    """Returns (device_id, new_id_to_persist_or_None). Pure - does NOT
    touch any Response object itself. See _respond() below for why: a
    path operation that returns its own Response/JSONResponse (every
    error path here does) makes FastAPI use THAT object directly as
    the final response - mutations made earlier on a
    separately-injected `response: Response` parameter are discarded in
    that case, a well-known FastAPI/Starlette gotcha. Rather than rely on
    which return path is "the one connected to the injected response"
    staying correct as this file evolves, every response this app
    returns is built and cookie-stamped in one place: _respond()."""
    existing = request.cookies.get(DEVICE_COOKIE_NAME)
    if existing:
        return existing, None
    new_id = secrets.token_urlsafe(24)
    return new_id, new_id


def _respond(status_code: int, content: dict, new_device_id: str | None) -> JSONResponse:
    resp = JSONResponse(status_code=status_code, content=content)
    if new_device_id:
        resp.set_cookie(
            key=DEVICE_COOKIE_NAME,
            value=new_device_id,
            max_age=DEVICE_COOKIE_MAX_AGE,
            httponly=True,
            secure=True,       # required for SameSite=None; also correct for an HTTPS-only deployment
            samesite="none",   # cross-origin (Pages domain -> Render domain) requires this, not "lax"
        )
    return resp


def _client_ip(request: Request) -> str:
    # Render (like most PaaS) terminates TLS at a proxy in front of the
    # app and forwards the real client IP via X-Forwarded-For. Falling
    # back to request.client.host covers local/direct testing.
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


@app.get("/api/health")
async def health():
    return {"ok": True}


@app.get("/api/quota")
async def get_quota(request: Request):
    device_id, new_device_id = _get_device_id(request)
    ip = _client_ip(request)
    return _respond(200, {"ok": True, **quota.status(device_id, ip)}, new_device_id)


@app.post("/api/analyze")
async def analyze(request: Request, photo: UploadFile = File(...)):
    device_id, new_device_id = _get_device_id(request)
    ip = _client_ip(request)

    def error(code: str, message_kurdish: str, status_code: int, **extra) -> JSONResponse:
        content = {"ok": False, "error_code": code, "message": message_kurdish, **extra}
        return _respond(status_code, content, new_device_id)

    # --- 1. File validation -------------------------------------------
    content_type = ALLOWED_CONTENT_TYPES.get((photo.content_type or "").lower())
    if not content_type:
        return error(
            "invalid_file_type",
            "جۆری فایلەکە پشتگیری ناکرێت. تکایە وێنەیەکی JPEG، PNG یان WEBP هەڵبژێرە.",
            400,
        )

    image_bytes = await photo.read()
    if not image_bytes:
        return error("empty_file", "فایلەکە بەتاڵە.", 400)
    if len(image_bytes) > MAX_UPLOAD_BYTES:
        return error(
            "file_too_large",
            f"وێنەکە زۆر گەورەیە. قەبارەی زۆرترین ڕێگەپێدراو {MAX_UPLOAD_MB:.0f}MB.",
            413,
        )

    # --- 2. Per-device/IP short-window rate limit (reuses gemini_queue's
    #        existing cooldown logic as-is - it only needs a hashable key,
    #        an int Telegram user_id in the original code, a string
    #        device_id here works identically) ------------------------
    cooldown_remaining = gemini_queue.check_user_cooldown(device_id)
    if cooldown_remaining > 0:
        return error(
            "rate_limited",
            f"تکایە {cooldown_remaining:.0f} چرکە چاوەڕێ بە، پاشان دووبارە هەوڵ بدەوە.",
            429,
            retry_after_seconds=round(cooldown_remaining, 1),
        )
    gemini_queue.mark_user_request(device_id)
    gemini_queue.record_photo_received()

    # --- 3. Daily quota (server-side only - see quota.py) --------------
    granted, used, limit = quota.check_and_reserve(device_id, ip)
    if not granted:
        gemini_queue.record_daily_limit_block()
        return error(
            "quota_exceeded",
            f"سنووری ڕۆژانەت بەسەرچووە ({limit} سکان لە ڕۆژێکدا). "
            "تکایە بەیانی دووبارە هەوڵ بدەوە.",
            429,
            used=used, limit=limit,
        )

    # --- 4. The actual AI call - full existing pipeline, unmodified:
    #        queue, per-model pacing, daily Gemini budget, retries,
    #        circuit breaker, duplicate-image cache, batching. -------
    try:
        result, _queue_position, _dhash = await gemini_queue.submit_photo_job(
            image_bytes, content_type, []
        )
    except Exception:
        logger.exception("[ANALYZE] Unexpected error calling gemini_queue.submit_photo_job")
        quota.release_both(device_id, ip)  # not the user's fault - don't charge their quota
        return error("internal_error", "هەڵەیەکی چاوەڕواننەکراو ڕوویدا. تکایە دواتر هەوڵ بدەوە.", 500)

    status_value = result.get("status")

    if status_value == "ok":
        return _respond(200, {
            "ok": True,
            "status": "ok",
            "foods": result.get("foods", []),
            "total_kcal": result.get("total_kcal"),
            "total_protein_g": result.get("total_protein_g"),
            "total_carbs_g": result.get("total_carbs_g"),
            "total_fat_g": result.get("total_fat_g"),
            "confidence": result.get("confidence"),
            "note_kurdish": result.get("note_kurdish") or "",
            "insight_kurdish": result.get("insight_kurdish") or "",
            "alternatives": result.get("alternatives", []),
            "quota": {"used": used, "limit": limit, "remaining": max(limit - used, 0)},
        }, new_device_id)

    if status_value == "no_food":
        # A real, successful analysis outcome - still counts against
        # quota (already reserved above), not released.
        return _respond(200, {
            "ok": True,
            "status": "no_food",
            "message": "هیچ خواردنێک لە وێنەکەدا نەدۆزرایەوە. تکایە وێنەیەکی ڕوونتر هەڵبژێرە.",
            "quota": {"used": used, "limit": limit, "remaining": max(limit - used, 0)},
        }, new_device_id)

    # status_value == "failed" - a server/Gemini-side problem, not the
    # user's fault: release their quota reservation.
    quota.release_both(device_id, ip)
    reason = result.get("reason")
    if reason == "rate_limited":
        return error(
            "service_busy",
            "سیستەمەکە لە ئێستادا قەرەباڵغە. تکایە چەند خولەکێک دواتر هەوڵ بدەوە.",
            503,
        )
    return error("analysis_failed", "شیکردنەوەی وێنەکە سەرکەوتوو نەبوو. تکایە دووبارە هەوڵ بدەوە.", 502)
