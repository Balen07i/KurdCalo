# quota.py
#
# Server-side daily scan quota for anonymous web users - a NEW module,
# not a port of storage.py's reserve_daily_slot(). storage.py's version
# is built around real, permanent Telegram user accounts and a meal-
# history table; this web MVP has neither (no accounts, no permanent
# photo/meal storage by design). Reusing storage.py as-is here would
# mean either inventing fake "users" or storing more than we should.
#
# What this DOES reuse: the same atomic-SQL philosophy storage.py uses
# (a single conditional statement, not a check-then-write race), applied
# to a much simpler one-table schema.
#
# THREAT MODEL: the only server-side identity we have for an anonymous
# visitor is (a) a random ID we hand them in a cookie, and (b) their
# source IP. Neither is unforgeable - a motivated user can clear
# cookies or (less easily) change IP. This is a reasonable, honest
# free-MVP tier limiter, not a strong anti-abuse system. We check BOTH
# and require BOTH to have headroom, which at least raises the bar
# above "just clear your cookies." Documented as a known limitation in
# the README - do not oversell this.
from __future__ import annotations

import os
import sqlite3
import threading
from datetime import datetime, timezone

DB_PATH = os.environ.get("QUOTA_DB_PATH", "quota.db")
DAILY_SCAN_LIMIT = int(os.environ.get("DAILY_SCAN_LIMIT", "4"))

# One connection per thread (FastAPI/Starlette runs sync path-ops in a
# thread pool) - sqlite3 connections are not safe to share across
# threads. WAL mode lets reads and writes interleave without the whole-
# file lock SQLite's default journal mode would require here.
_local = threading.local()


def _conn() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS daily_counts (
                scope TEXT NOT NULL,   -- 'device' or 'ip'
                key TEXT NOT NULL,
                date TEXT NOT NULL,
                count INTEGER NOT NULL,
                PRIMARY KEY (scope, key, date)
            )
            """
        )
        conn.commit()
        _local.conn = conn
    return conn


def _today_str() -> str:
    # UTC, deliberately simple - a per-user "midnight" is a nicety this
    # MVP doesn't need. Matches the spirit of gemini_queue.py's own UTC
    # daily-budget reset, kept consistent across both quota layers.
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _try_increment(scope: str, key: str, limit: int) -> int | None:
    """Atomically increments (scope, key, today)'s counter IF it's still
    under limit, in one statement - no separate read-then-write, so no
    race window between two concurrent requests from the same key.
    Returns the new count if granted, None if already at/over limit."""
    conn = _conn()
    today = _today_str()
    cur = conn.execute(
        """
        INSERT INTO daily_counts (scope, key, date, count) VALUES (?, ?, ?, 1)
        ON CONFLICT(scope, key, date) DO UPDATE SET count = count + 1 WHERE count < ?
        RETURNING count
        """,
        (scope, key, today, limit),
    )
    row = cur.fetchone()
    conn.commit()
    return row[0] if row else None


def _current_count(scope: str, key: str) -> int:
    conn = _conn()
    row = conn.execute(
        "SELECT count FROM daily_counts WHERE scope = ? AND key = ? AND date = ?",
        (scope, key, _today_str()),
    ).fetchone()
    return row[0] if row else 0


def check_and_reserve(device_id: str, ip: str, limit: int = DAILY_SCAN_LIMIT) -> tuple[bool, int, int]:
    """Call ONCE per incoming scan request, before submitting to Gemini.
    Reserves a slot against BOTH the device-id and IP counters - only
    succeeds if both have headroom. Returns (granted, used_after, limit).
    If granted is False, nothing was incremented (or see release_on_
    mismatch below for the partial-grant edge case) and the caller must
    not proceed to call Gemini.
    """
    device_count = _try_increment("device", device_id, limit)
    if device_count is None:
        return False, _current_count("device", device_id), limit

    ip_count = _try_increment("ip", ip, limit)
    if ip_count is None:
        # Device had headroom but IP didn't (e.g. several devices behind
        # one IP, or cookie cleared then reused). Release the device-side
        # reservation we just took so it isn't silently wasted - a
        # legitimate other device on a fresh cookie but the same IP
        # shouldn't be charged for a request that was actually rejected.
        _release("device", device_id)
        return False, _current_count("ip", ip), limit

    return True, max(device_count, ip_count), limit


def _release(scope: str, key: str):
    conn = _conn()
    conn.execute(
        "UPDATE daily_counts SET count = count - 1 WHERE scope = ? AND key = ? AND date = ? AND count > 0",
        (scope, key, _today_str()),
    )
    conn.commit()


def release_both(device_id: str, ip: str):
    """Call if a reserved slot's analysis then fails for a reason that
    shouldn't count against the user's daily allowance (e.g. a server-
    side/Gemini error, not the user's fault - mirrors storage.py's own
    reserve-then-release-on-failure pattern for the same reason)."""
    _release("device", device_id)
    _release("ip", ip)


def status(device_id: str, ip: str, limit: int = DAILY_SCAN_LIMIT) -> dict:
    """Read-only check for displaying remaining quota, without reserving."""
    used = max(_current_count("device", device_id), _current_count("ip", ip))
    return {"used": used, "limit": limit, "remaining": max(limit - used, 0)}
