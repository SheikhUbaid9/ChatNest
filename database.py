"""
database.py - Async SQLite cache + auth storage for MCP Inbox.

Primary tables:
  messages         - cached messages from all platforms (scoped by user_id)
  read_state       - tracks read markers
  tool_log         - history of tool calls (scoped by user_id)
  users            - app users
  user_sessions    - login sessions
  provider_tokens  - OAuth tokens per user/provider
  oauth_state      - short-lived OAuth state values
"""

from __future__ import annotations

import json
import logging
import secrets
import sqlite3
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncGenerator

import aiosqlite

from config import get_settings

logger = logging.getLogger(__name__)

# - Schema ---------------------------------------------------------------------

_DDL = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS messages (
    id            TEXT PRIMARY KEY,          -- platform:native_id
    user_id       TEXT NOT NULL DEFAULT 'global',
    platform      TEXT NOT NULL,             -- gmail | slack | telegram
    sender        TEXT NOT NULL,
    sender_email  TEXT,
    subject       TEXT,
    preview       TEXT,
    body          TEXT,
    thread_id     TEXT,
    channel       TEXT,
    timestamp     TEXT NOT NULL,             -- ISO-8601 UTC
    is_unread     INTEGER NOT NULL DEFAULT 1,
    raw_json      TEXT,                      -- full API response blob
    cached_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS read_state (
    message_id    TEXT PRIMARY KEY,
    read_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tool_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       TEXT,
    tool_name     TEXT NOT NULL,
    platform      TEXT,
    status        TEXT NOT NULL DEFAULT 'calling',  -- calling | done | error
    duration_ms   INTEGER,
    result_summary TEXT,
    called_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id            TEXT PRIMARY KEY,
    email         TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    display_name  TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS user_sessions (
    session_id    TEXT PRIMARY KEY,
    user_id       TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at    TEXT NOT NULL,
    expires_at    TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS provider_tokens (
    user_id       TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    provider      TEXT NOT NULL,
    access_token  TEXT NOT NULL,
    refresh_token TEXT,
    token_uri     TEXT,
    client_id     TEXT,
    client_secret TEXT,
    scopes        TEXT,
    expiry        TEXT,
    account_email TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    PRIMARY KEY (user_id, provider)
);

CREATE TABLE IF NOT EXISTS oauth_state (
    state         TEXT PRIMARY KEY,
    user_id       TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    provider      TEXT NOT NULL,
    redirect_to   TEXT,
    created_at    TEXT NOT NULL,
    expires_at    TEXT NOT NULL
);
"""

_INDEX_DDL = """
CREATE INDEX IF NOT EXISTS idx_messages_user          ON messages(user_id);
CREATE INDEX IF NOT EXISTS idx_messages_platform      ON messages(platform);
CREATE INDEX IF NOT EXISTS idx_messages_timestamp     ON messages(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_tool_log_called_at     ON tool_log(called_at DESC);
CREATE INDEX IF NOT EXISTS idx_tool_log_user          ON tool_log(user_id);
CREATE INDEX IF NOT EXISTS idx_user_sessions_user     ON user_sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_provider_tokens_user   ON provider_tokens(user_id);
CREATE INDEX IF NOT EXISTS idx_oauth_state_expires_at ON oauth_state(expires_at);
"""


# - Connection helper -----------------------------------------------------------

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


# - Initialisation --------------------------------------------------------------

async def init_db() -> None:
    """Create all tables if they don't exist yet and run lightweight migrations."""
    async with get_db() as db:
        await db.executescript(_DDL)
        await _ensure_column(
            db,
            table="messages",
            column="user_id",
            column_ddl="TEXT NOT NULL DEFAULT 'global'",
        )
        await _ensure_column(
            db,
            table="tool_log",
            column="user_id",
            column_ddl="TEXT",
        )
        await db.executescript(_INDEX_DDL)
        await db.commit()
    logger.info("Database initialised at %s", _get_db_path())


async def _ensure_column(
    db: aiosqlite.Connection,
    table: str,
    column: str,
    column_ddl: str,
) -> None:
    async with db.execute(f"PRAGMA table_info({table})") as cur:
        rows = await cur.fetchall()
    names = {row["name"] for row in rows}
    if column not in names:
        await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_ddl}")


# - Messages -------------------------------------------------------------------

def _scope_user(user_id: str | None) -> str:
    return (user_id or "global").strip() or "global"


def _read_state_key(user_id: str | None, message_id: str) -> str:
    return f"{_scope_user(user_id)}:{message_id}"


async def upsert_messages(messages: list[dict[str, Any]], user_id: str | None = None) -> int:
    """
    Insert or replace a batch of message dicts.
    Returns count of rows written.
    """
    if not messages:
        return 0

    scoped_user = _scope_user(user_id)
    now = _utcnow()
    rows = [
        (
            msg["id"],
            scoped_user,
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
                (id, user_id, platform, sender, sender_email, subject, preview, body,
                 thread_id, channel, timestamp, is_unread, raw_json, cached_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            rows,
        )
        await db.commit()

    return len(rows)


async def get_messages(
    platform: str | None = None,
    unread_only: bool = False,
    limit: int = 50,
    user_id: str | None = None,
) -> list[dict[str, Any]]:
    """
    Fetch cached messages, optionally filtered by platform / unread status.
    Results are ordered newest-first and joined with read_state.
    """
    scoped_user = _scope_user(user_id)
    conditions: list[str] = ["m.user_id = ?"]
    params: list[Any] = [scoped_user]

    if platform:
        conditions.append("m.platform = ?")
        params.append(platform)
    if unread_only:
        conditions.append("m.is_unread = 1")

    where = "WHERE " + " AND ".join(conditions)

    sql = f"""
        SELECT
            m.*,
            CASE WHEN rs.message_id IS NOT NULL THEN 0 ELSE m.is_unread END AS effective_unread
        FROM messages m
        LEFT JOIN read_state rs ON rs.message_id = (m.user_id || ':' || m.id)
        {where}
        ORDER BY m.timestamp DESC
        LIMIT ?
    """
    params.append(limit)

    async with get_db() as db:
        async with db.execute(sql, params) as cur:
            rows = await cur.fetchall()

    return [dict(row) for row in rows]


async def get_unread_counts(user_id: str | None = None) -> dict[str, int]:
    """Return unread message counts per platform."""
    scoped_user = _scope_user(user_id)
    sql = """
        SELECT m.platform, COUNT(*) as cnt
        FROM messages m
        LEFT JOIN read_state rs ON rs.message_id = (m.user_id || ':' || m.id)
        WHERE m.user_id = ? AND m.is_unread = 1 AND rs.message_id IS NULL
        GROUP BY m.platform
    """
    async with get_db() as db:
        async with db.execute(sql, (scoped_user,)) as cur:
            rows = await cur.fetchall()

    counts: dict[str, int] = {"gmail": 0, "slack": 0, "telegram": 0}
    for row in rows:
        counts[row["platform"]] = row["cnt"]
    return counts


# - Read state -----------------------------------------------------------------

async def mark_read(message_id: str, user_id: str | None = None) -> bool:
    """Mark a message as read. Returns True if the row exists after insert."""
    key = _read_state_key(user_id, message_id)
    async with get_db() as db:
        await db.execute(
            "INSERT OR IGNORE INTO read_state (message_id, read_at) VALUES (?, ?)",
            (key, _utcnow()),
        )
        await db.commit()
        async with db.execute(
            "SELECT COUNT(*) FROM read_state WHERE message_id = ?", (key,)
        ) as cur:
            row = await cur.fetchone()
            return bool(row and row[0] > 0)


async def is_read(message_id: str, user_id: str | None = None) -> bool:
    key = _read_state_key(user_id, message_id)
    async with get_db() as db:
        async with db.execute(
            "SELECT 1 FROM read_state WHERE message_id = ?", (key,)
        ) as cur:
            return await cur.fetchone() is not None


# - Tool log -------------------------------------------------------------------

async def log_tool_call(
    tool_name: str,
    platform: str | None = None,
    user_id: str | None = None,
) -> int:
    """Insert a 'calling' entry and return its auto-generated ID."""
    scoped_user = _scope_user(user_id)
    async with get_db() as db:
        async with db.execute(
            """
            INSERT INTO tool_log (user_id, tool_name, platform, status, called_at)
            VALUES (?, ?, ?, 'calling', ?)
            """,
            (scoped_user, tool_name, platform, _utcnow()),
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


async def get_tool_log(limit: int = 30, user_id: str | None = None) -> list[dict[str, Any]]:
    """Return recent tool log entries, newest first."""
    scoped_user = _scope_user(user_id)
    async with get_db() as db:
        if scoped_user == "global":
            query = """
                SELECT * FROM tool_log
                WHERE user_id = 'global' OR user_id IS NULL
                ORDER BY called_at DESC
                LIMIT ?
            """
            params: tuple[Any, ...] = (limit,)
        else:
            query = """
                SELECT * FROM tool_log
                WHERE user_id = ?
                ORDER BY called_at DESC
                LIMIT ?
            """
            params = (scoped_user, limit)
        async with db.execute(query, params) as cur:
            rows = await cur.fetchall()
    return [dict(row) for row in rows]


# - Users / Sessions -----------------------------------------------------------

async def create_user(email: str, password_hash: str, display_name: str = "") -> dict[str, Any]:
    user_id = uuid.uuid4().hex
    now = _utcnow()
    async with get_db() as db:
        await db.execute(
            """
            INSERT INTO users (id, email, password_hash, display_name, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (user_id, email, password_hash, display_name or None, now, now),
        )
        await db.commit()
    user = await get_user_by_id(user_id)
    if not user:
        raise RuntimeError("Failed to create user")
    return user


async def get_user_by_email(email: str) -> dict[str, Any] | None:
    async with get_db() as db:
        async with db.execute(
            "SELECT * FROM users WHERE email = ?",
            (email,),
        ) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None


async def get_user_by_id(user_id: str) -> dict[str, Any] | None:
    async with get_db() as db:
        async with db.execute(
            "SELECT * FROM users WHERE id = ?",
            (user_id,),
        ) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None


async def create_user_session(user_id: str, ttl_hours: int) -> str:
    session_id = secrets.token_urlsafe(48)
    now = _utcnow()
    expires_at = (datetime.now(timezone.utc) + timedelta(hours=max(1, ttl_hours))).isoformat()
    async with get_db() as db:
        await db.execute(
            """
            INSERT INTO user_sessions (session_id, user_id, created_at, expires_at, last_seen_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (session_id, user_id, now, expires_at, now),
        )
        await db.commit()
    return session_id


async def delete_user_session(session_id: str) -> None:
    async with get_db() as db:
        await db.execute("DELETE FROM user_sessions WHERE session_id = ?", (session_id,))
        await db.commit()


async def get_user_by_session(session_id: str, touch: bool = True) -> dict[str, Any] | None:
    now = _utcnow()
    async with get_db() as db:
        async with db.execute(
            """
            SELECT
                u.id,
                u.email,
                u.display_name,
                s.session_id,
                s.expires_at
            FROM user_sessions s
            JOIN users u ON u.id = s.user_id
            WHERE s.session_id = ? AND s.expires_at > ?
            """,
            (session_id, now),
        ) as cur:
            row = await cur.fetchone()

        if row and touch:
            await db.execute(
                "UPDATE user_sessions SET last_seen_at = ? WHERE session_id = ?",
                (now, session_id),
            )
            await db.commit()

    return dict(row) if row else None


async def purge_expired_sessions() -> int:
    now = _utcnow()
    async with get_db() as db:
        async with db.execute(
            "SELECT COUNT(*) AS cnt FROM user_sessions WHERE expires_at <= ?",
            (now,),
        ) as cur:
            row = await cur.fetchone()
            count = int(row["cnt"]) if row else 0
        await db.execute("DELETE FROM user_sessions WHERE expires_at <= ?", (now,))
        await db.commit()
    return count


# - Provider tokens / OAuth state ----------------------------------------------

async def upsert_provider_token(
    user_id: str,
    provider: str,
    *,
    access_token: str,
    refresh_token: str = "",
    token_uri: str = "",
    client_id: str = "",
    client_secret: str = "",
    scopes: list[str] | None = None,
    expiry: str = "",
    account_email: str = "",
) -> None:
    now = _utcnow()
    scopes_json = json.dumps(scopes or [])
    async with get_db() as db:
        await db.execute(
            """
            INSERT INTO provider_tokens
                (user_id, provider, access_token, refresh_token, token_uri, client_id,
                 client_secret, scopes, expiry, account_email, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, provider) DO UPDATE SET
                access_token  = excluded.access_token,
                refresh_token = excluded.refresh_token,
                token_uri     = excluded.token_uri,
                client_id     = excluded.client_id,
                client_secret = excluded.client_secret,
                scopes        = excluded.scopes,
                expiry        = excluded.expiry,
                account_email = excluded.account_email,
                updated_at    = excluded.updated_at
            """,
            (
                user_id,
                provider,
                access_token,
                refresh_token or None,
                token_uri or None,
                client_id or None,
                client_secret or None,
                scopes_json,
                expiry or None,
                account_email or None,
                now,
                now,
            ),
        )
        await db.commit()


async def get_provider_token(user_id: str, provider: str) -> dict[str, Any] | None:
    async with get_db() as db:
        async with db.execute(
            "SELECT * FROM provider_tokens WHERE user_id = ? AND provider = ?",
            (user_id, provider),
        ) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None


async def delete_provider_token(user_id: str, provider: str) -> None:
    async with get_db() as db:
        await db.execute(
            "DELETE FROM provider_tokens WHERE user_id = ? AND provider = ?",
            (user_id, provider),
        )
        await db.commit()


async def create_oauth_state(
    user_id: str,
    provider: str,
    state: str,
    redirect_to: str = "",
    ttl_minutes: int = 10,
) -> None:
    now = datetime.now(timezone.utc)
    expires_at = (now + timedelta(minutes=max(1, ttl_minutes))).isoformat()
    async with get_db() as db:
        await db.execute(
            """
            INSERT OR REPLACE INTO oauth_state
                (state, user_id, provider, redirect_to, created_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                state,
                user_id,
                provider,
                redirect_to or None,
                now.isoformat(),
                expires_at,
            ),
        )
        await db.commit()


async def consume_oauth_state(user_id: str, provider: str, state: str) -> dict[str, Any] | None:
    now = _utcnow()
    async with get_db() as db:
        async with db.execute(
            """
            SELECT * FROM oauth_state
            WHERE state = ? AND user_id = ? AND provider = ? AND expires_at > ?
            """,
            (state, user_id, provider, now),
        ) as cur:
            row = await cur.fetchone()
        await db.execute(
            "DELETE FROM oauth_state WHERE state = ?",
            (state,),
        )
        await db.commit()
    return dict(row) if row else None


async def purge_expired_oauth_state() -> int:
    now = _utcnow()
    async with get_db() as db:
        async with db.execute(
            "SELECT COUNT(*) AS cnt FROM oauth_state WHERE expires_at <= ?",
            (now,),
        ) as cur:
            row = await cur.fetchone()
            count = int(row["cnt"]) if row else 0
        await db.execute("DELETE FROM oauth_state WHERE expires_at <= ?", (now,))
        await db.commit()
    return count


# - Utilities ------------------------------------------------------------------

def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


async def clear_cache(platform: str | None = None, user_id: str | None = None) -> int:
    """Delete cached messages for one or all platforms. Returns deleted count."""
    scoped_user = _scope_user(user_id)
    async with get_db() as db:
        if platform:
            async with db.execute(
                "SELECT COUNT(*) FROM messages WHERE user_id = ? AND platform = ?",
                (scoped_user, platform),
            ) as cur:
                row = await cur.fetchone()
                count = row[0] if row else 0
            await db.execute(
                "DELETE FROM messages WHERE user_id = ? AND platform = ?",
                (scoped_user, platform),
            )
        else:
            async with db.execute(
                "SELECT COUNT(*) FROM messages WHERE user_id = ?",
                (scoped_user,),
            ) as cur:
                row = await cur.fetchone()
                count = row[0] if row else 0
            await db.execute("DELETE FROM messages WHERE user_id = ?", (scoped_user,))
        await db.commit()
    return count


# - Sync helpers (for OAuth credential refresh paths) ---------------------------

def get_provider_token_sync(user_id: str, provider: str) -> dict[str, Any] | None:
    conn = sqlite3.connect(_get_db_path())
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute(
            "SELECT * FROM provider_tokens WHERE user_id = ? AND provider = ?",
            (user_id, provider),
        )
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def upsert_provider_token_sync(
    user_id: str,
    provider: str,
    *,
    access_token: str,
    refresh_token: str = "",
    token_uri: str = "",
    client_id: str = "",
    client_secret: str = "",
    scopes: list[str] | None = None,
    expiry: str = "",
    account_email: str = "",
) -> None:
    now = _utcnow()
    scopes_json = json.dumps(scopes or [])
    conn = sqlite3.connect(_get_db_path())
    try:
        conn.execute(
            """
            INSERT INTO provider_tokens
                (user_id, provider, access_token, refresh_token, token_uri, client_id,
                 client_secret, scopes, expiry, account_email, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, provider) DO UPDATE SET
                access_token  = excluded.access_token,
                refresh_token = excluded.refresh_token,
                token_uri     = excluded.token_uri,
                client_id     = excluded.client_id,
                client_secret = excluded.client_secret,
                scopes        = excluded.scopes,
                expiry        = excluded.expiry,
                account_email = excluded.account_email,
                updated_at    = excluded.updated_at
            """,
            (
                user_id,
                provider,
                access_token,
                refresh_token or None,
                token_uri or None,
                client_id or None,
                client_secret or None,
                scopes_json,
                expiry or None,
                account_email or None,
                now,
                now,
            ),
        )
        conn.commit()
    finally:
        conn.close()
