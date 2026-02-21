"""
database.py — Async SQLite cache for MCP Inbox.

Tables:
  messages   — cached messages from all platforms
  read_state — tracks which messages have been marked read
  tool_log   — history of MCP tool calls for the UI log panel
"""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, AsyncGenerator

import aiosqlite

from config import get_settings

logger = logging.getLogger(__name__)

# ── Schema ────────────────────────────────────────────────────────────────────

_DDL = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS messages (
    id          TEXT PRIMARY KEY,          -- platform:native_id
    platform    TEXT NOT NULL,             -- gmail | slack | telegram
    sender      TEXT NOT NULL,
    sender_email TEXT,
    subject     TEXT,
    preview     TEXT,
    body        TEXT,
    thread_id   TEXT,
    channel     TEXT,
    timestamp   TEXT NOT NULL,             -- ISO-8601 UTC
    is_unread   INTEGER NOT NULL DEFAULT 1,
    raw_json    TEXT,                      -- full API response blob
    cached_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS read_state (
    message_id  TEXT PRIMARY KEY REFERENCES messages(id) ON DELETE CASCADE,
    read_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tool_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    tool_name   TEXT NOT NULL,
    platform    TEXT,
    status      TEXT NOT NULL DEFAULT 'calling',  -- calling | done | error
    duration_ms INTEGER,
    result_summary TEXT,
    called_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_platform  ON messages(platform);
CREATE INDEX IF NOT EXISTS idx_messages_timestamp ON messages(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_tool_log_called_at ON tool_log(called_at DESC);
"""

# ── Connection helper ─────────────────────────────────────────────────────────

_db_path: str | None = None


def _get_db_path() -> str:
    global _db_path
    if _db_path is None:
        _db_path = str(get_settings().database_path)
    return _db_path


@asynccontextmanager
async def get_db() -> AsyncGenerator[aiosqlite.Connection, None]:
    """Yield an open, row-factory-enabled DB connection."""
    async with aiosqlite.connect(_get_db_path()) as conn:
        conn.row_factory = aiosqlite.Row
        yield conn


# ── Initialisation ────────────────────────────────────────────────────────────

async def init_db() -> None:
    """Create all tables if they don't exist yet."""
    async with get_db() as db:
        await db.executescript(_DDL)
        await db.commit()
    logger.info("Database initialised at %s", _get_db_path())


# ── Messages ──────────────────────────────────────────────────────────────────

async def upsert_messages(messages: list[dict[str, Any]]) -> int:
    """
    Insert or replace a batch of message dicts.
    Returns count of rows written.
    """
    now = _utcnow()
    rows = [
        (
            msg["id"],
            msg["platform"],
            msg["sender"],
            msg.get("sender_email"),
            msg.get("subject"),
            msg.get("preview"),
            msg.get("body"),
            msg.get("thread_id"),
            msg.get("channel"),
            msg["timestamp"],
            1 if msg.get("is_unread", True) else 0,
            json.dumps(msg.get("raw_json")) if msg.get("raw_json") else None,
            now,
        )
        for msg in messages
    ]

    async with get_db() as db:
        await db.executemany(
            """
            INSERT OR REPLACE INTO messages
                (id, platform, sender, sender_email, subject, preview, body,
                 thread_id, channel, timestamp, is_unread, raw_json, cached_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            rows,
        )
        await db.commit()

    return len(rows)


async def get_messages(
    platform: str | None = None,
    unread_only: bool = False,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """
    Fetch cached messages, optionally filtered by platform / unread status.
    Results are ordered newest-first and joined with read_state.
    """
    conditions: list[str] = []
    params: list[Any] = []

    if platform:
        conditions.append("m.platform = ?")
        params.append(platform)
    if unread_only:
        conditions.append("m.is_unread = 1")

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    sql = f"""
        SELECT
            m.*,
            CASE WHEN rs.message_id IS NOT NULL THEN 0 ELSE m.is_unread END AS effective_unread
        FROM messages m
        LEFT JOIN read_state rs ON rs.message_id = m.id
        {where}
        ORDER BY m.timestamp DESC
        LIMIT ?
    """
    params.append(limit)

    async with get_db() as db:
        async with db.execute(sql, params) as cur:
            rows = await cur.fetchall()

    return [dict(row) for row in rows]


async def get_unread_counts() -> dict[str, int]:
    """Return unread message counts per platform."""
    sql = """
        SELECT m.platform, COUNT(*) as cnt
        FROM messages m
        LEFT JOIN read_state rs ON rs.message_id = m.id
        WHERE m.is_unread = 1 AND rs.message_id IS NULL
        GROUP BY m.platform
    """
    async with get_db() as db:
        async with db.execute(sql) as cur:
            rows = await cur.fetchall()

    counts: dict[str, int] = {"gmail": 0, "slack": 0, "telegram": 0}
    for row in rows:
        counts[row["platform"]] = row["cnt"]
    return counts


# ── Read state ────────────────────────────────────────────────────────────────

async def mark_read(message_id: str) -> bool:
    """Mark a message as read. Returns True if the row was inserted."""
    async with get_db() as db:
        await db.execute(
            "INSERT OR IGNORE INTO read_state (message_id, read_at) VALUES (?, ?)",
            (message_id, _utcnow()),
        )
        await db.commit()
        async with db.execute(
            "SELECT COUNT(*) FROM read_state WHERE message_id = ?", (message_id,)
        ) as cur:
            row = await cur.fetchone()
            return bool(row and row[0] > 0)


async def is_read(message_id: str) -> bool:
    async with get_db() as db:
        async with db.execute(
            "SELECT 1 FROM read_state WHERE message_id = ?", (message_id,)
        ) as cur:
            return await cur.fetchone() is not None


# ── Tool log ──────────────────────────────────────────────────────────────────

async def log_tool_call(tool_name: str, platform: str | None = None) -> int:
    """Insert a 'calling' entry and return its auto-generated ID."""
    async with get_db() as db:
        async with db.execute(
            """
            INSERT INTO tool_log (tool_name, platform, status, called_at)
            VALUES (?, ?, 'calling', ?)
            """,
            (tool_name, platform, _utcnow()),
        ) as cur:
            row_id = cur.lastrowid
        await db.commit()
    return row_id  # type: ignore[return-value]


async def finish_tool_call(
    log_id: int,
    duration_ms: int,
    result_summary: str,
    status: str = "done",
) -> None:
    """Update a tool log entry with its outcome."""
    async with get_db() as db:
        await db.execute(
            """
            UPDATE tool_log
            SET status = ?, duration_ms = ?, result_summary = ?
            WHERE id = ?
            """,
            (status, duration_ms, result_summary, log_id),
        )
        await db.commit()


async def get_tool_log(limit: int = 30) -> list[dict[str, Any]]:
    """Return recent tool log entries, newest first."""
    async with get_db() as db:
        async with db.execute(
            "SELECT * FROM tool_log ORDER BY called_at DESC LIMIT ?", (limit,)
        ) as cur:
            rows = await cur.fetchall()
    return [dict(row) for row in rows]


# ── Utilities ─────────────────────────────────────────────────────────────────

def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


async def clear_cache(platform: str | None = None) -> int:
    """Delete cached messages for one or all platforms. Returns deleted count."""
    async with get_db() as db:
        if platform:
            async with db.execute(
                "SELECT COUNT(*) FROM messages WHERE platform = ?", (platform,)
            ) as cur:
                row = await cur.fetchone()
                count = row[0] if row else 0
            await db.execute("DELETE FROM messages WHERE platform = ?", (platform,))
        else:
            async with db.execute("SELECT COUNT(*) FROM messages") as cur:
                row = await cur.fetchone()
                count = row[0] if row else 0
            await db.execute("DELETE FROM messages")
        await db.commit()
    return count
