"""
ui/server.py — FastAPI backend for ChatNest UI.

REST endpoints:
  GET  /                        → serve index.html
  GET  /api/status              → platform connection status
  GET  /api/messages/all        → all platforms combined
  GET  /api/messages/gmail      → gmail only
  GET  /api/messages/slack      → slack only
  GET  /api/messages/telegram   → telegram only
  GET  /api/unread-counts       → per-platform unread counts
  POST /api/mark-read           → mark message as read
  POST /api/refresh             → force re-fetch from all platforms
  GET  /api/tool-log            → recent MCP tool call history
  POST /api/summarize           → LLM summarize via configured AI provider
  POST /api/send-reply          → send reply via platform client
  GET  /api/ai/status           → AI provider availability + model info
  GET  /api/ollama/status       → legacy alias to /api/ai/status
  GET  /api/telegram/test      → test Telegram bot connectivity + proxy status

WebSocket:
  WS   /ws/tool-log             → live push of tool log events
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from auth_security import decrypt_secret, encrypt_secret, hash_password, verify_password
from config import get_settings
from database import (
    consume_oauth_state,
    create_oauth_state,
    create_user,
    create_user_session,
    delete_provider_token,
    delete_user_session,
    get_provider_token,
    get_messages,
    get_tool_log,
    get_user_by_session,
    get_user_by_email,
    get_unread_counts,
    init_db,
    mark_read,
    purge_expired_oauth_state,
    purge_expired_sessions,
    upsert_provider_token,
    upsert_messages,
)
from tools.gmail_tools import get_gmail_unread, send_gmail_reply
from tools.slack_tools import get_slack_messages, send_slack_message
from tools.telegram_tools import get_telegram_messages, send_telegram_reply
from clients.telegram_client import get_telegram_data_async as _tg_fetch
from clients.telethon_client import get_personal_telegram_data, get_telethon_client
from clients.gemini_client import (
    GEMINI_MODEL,
    draft_reply_gemini,
    get_ai_provider_preference,
    is_gemini_ready,
    summarize_message_gemini,
)
from clients.ollama_client import (
    OLLAMA_BASE,
    is_ollama_running,
    list_models,
    summarize_message,
    draft_reply,
    get_best_available_model,
)
from oauth_google import (
    GMAIL_OAUTH_SCOPES,
    build_google_authorize_url,
    exchange_code_for_tokens,
    fetch_gmail_profile,
    get_google_oauth_config,
)
from oauth_slack import (
    build_slack_authorize_url,
    exchange_slack_code,
    get_slack_oauth_config,
)
from ui.auth import (
    clear_session_cookie,
    display_name_from_email,
    get_current_user_optional,
    normalize_email,
    require_current_user,
    session_id_from_request,
    set_session_cookie,
)

logger = logging.getLogger(__name__)

# ── Paths ─────────────────────────────────────────────────────────────────────

UI_DIR = Path(__file__).parent
STATIC_DIR = UI_DIR / "static"
TEMPLATES_DIR = UI_DIR / "templates"

# ── WebSocket connection manager ──────────────────────────────────────────────

class ConnectionManager:
    """Manages active WebSocket connections for the tool-log live feed."""

    def __init__(self) -> None:
        self._connections: list[tuple[WebSocket, str]] = []

    async def connect(self, ws: WebSocket, user_id: str) -> None:
        await ws.accept()
        self._connections.append((ws, user_id))
        logger.debug("WS client connected  (total: %d)", len(self._connections))

    def disconnect(self, ws: WebSocket) -> None:
        self._connections = [(conn, uid) for conn, uid in self._connections if conn is not ws]
        logger.debug("WS client disconnected (total: %d)", len(self._connections))

    async def broadcast(self, data: dict[str, Any], user_id: str) -> None:
        """Send a JSON payload to all connected clients for one user."""
        dead: list[WebSocket] = []
        for ws, uid in self._connections:
            if uid != user_id:
                continue
            try:
                await ws.send_json(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)

    @property
    def active(self) -> int:
        return len(self._connections)

    @property
    def active_user_ids(self) -> set[str]:
        return {uid for _, uid in self._connections}


manager = ConnectionManager()

# ── Tool-log poller ───────────────────────────────────────────────────────────

_last_broadcast_by_user: dict[str, int] = {}


async def _poll_tool_log() -> None:
    """Background task: push new tool-log rows to WebSocket clients."""
    while True:
        try:
            await asyncio.sleep(0.5)
            if manager.active == 0:
                continue

            for user_id in manager.active_user_ids:
                logs = await get_tool_log(limit=20, user_id=user_id)
                last_seen = _last_broadcast_by_user.get(user_id, 0)
                new_logs = [l for l in logs if l["id"] > last_seen]
                if not new_logs:
                    continue
                _last_broadcast_by_user[user_id] = max(l["id"] for l in new_logs)
                for entry in reversed(new_logs):  # oldest first
                    await manager.broadcast({"type": "tool_log", "entry": entry}, user_id=user_id)
        except Exception as exc:
            logger.debug("Tool-log poller error: %s", exc)


# ── App lifespan ──────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    # Start background poller
    task = asyncio.create_task(_poll_tool_log())
    logger.info("ChatNest UI server ready")
    yield
    task.cancel()


# ── FastAPI app ───────────────────────────────────────────────────────────────

app = FastAPI(
    title="ChatNest",
    description="AI Communication Hub — Gmail · Slack · Telegram",
    version="1.0.0",
    lifespan=lifespan,
)

# Static files + templates
STATIC_DIR.mkdir(parents=True, exist_ok=True)
TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _refresh_all_platforms(user_id: str) -> dict[str, Any]:
    """Fetch fresh data from all platforms and cache it."""
    results: dict[str, Any] = {}

    gmail_result = await get_gmail_unread(user_id=user_id)
    results["gmail"] = {
        "count": gmail_result["count"],
        "is_mock": gmail_result["is_mock"],
    }

    slack_result = await get_slack_messages(user_id=user_id)
    results["slack"] = {
        "count": slack_result["count"],
        "is_mock": slack_result["is_mock"],
    }

    # Try personal account first (Telethon), fall back to bot (getUpdates)
    personal_msgs, personal_is_mock = await get_personal_telegram_data(
        limit_per_dialog=5, max_dialogs=30
    )
    if personal_msgs:
        from database import upsert_messages
        await upsert_messages(personal_msgs, user_id=user_id)
        results["telegram"] = {"count": len(personal_msgs), "is_mock": False, "source": "personal"}
    else:
        tg_messages, tg_is_mock = await _tg_fetch()
        if tg_messages:
            from database import upsert_messages
            await upsert_messages(tg_messages, user_id=user_id)
        results["telegram"] = {"count": len(tg_messages), "is_mock": tg_is_mock, "source": "bot"}

    return results


def _format_message(row: dict[str, Any]) -> dict[str, Any]:
    """Normalise a DB row for JSON API response."""
    return {
        "id":           row["id"],
        "platform":     row["platform"],
        "sender":       row["sender"],
        "sender_email": row.get("sender_email"),
        "subject":      row.get("subject"),
        "preview":      row.get("preview"),
        "body":         row.get("body"),
        "thread_id":    row.get("thread_id"),
        "channel":      row.get("channel"),
        "timestamp":    row["timestamp"],
        "is_unread":    bool(row.get("effective_unread", row.get("is_unread", False))),
    }


def _format_tool_log(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id":             row["id"],
        "tool_name":      row["tool_name"],
        "platform":       row.get("platform"),
        "status":         row["status"],
        "duration_ms":    row.get("duration_ms"),
        "result_summary": row.get("result_summary"),
        "called_at":      row["called_at"],
    }


def _build_template_draft(
    original_body: str,
    sender: str = "",
    instructions: str = "",
) -> str:
    """Simple non-LLM fallback draft used when Ollama is unavailable."""
    sender_name = (sender or "").strip().split("@")[0].split()[0] if sender else ""
    greeting = f"Hi {sender_name}," if sender_name else "Hi,"

    first_line = " ".join((original_body or "").strip().split())
    if first_line:
        if len(first_line) > 110:
            first_line = first_line[:107].rstrip() + "..."
        ack = f'Thanks for your message about "{first_line}".'
    else:
        ack = "Thanks for your message."

    next_step = instructions.strip() or "I will review this and get back to you shortly."

    return f"{greeting}\n\n{ack} {next_step}\n\nBest,\n"


def _extractive_summary(body: str, sentence_limit: int = 3) -> str:
    """Simple fallback summary from the first N sentences."""
    sentences = [s.strip() for s in body.replace("\n", " ").split(".") if s.strip()]
    return ". ".join(sentences[:sentence_limit]) + ("." if sentences else "")


def _select_ai_provider() -> str:
    """
    Select AI provider using env preference:
    - AI_PROVIDER=gemini: Gemini only (if configured), else none
    - AI_PROVIDER=ollama: Ollama only
    - AI_PROVIDER=auto (default): Gemini first, then Ollama
    """
    preferred = get_ai_provider_preference()
    gemini_ready = is_gemini_ready()

    if preferred == "gemini":
        return "gemini" if gemini_ready else "none"
    if preferred == "ollama":
        return "ollama"
    # auto
    return "gemini" if gemini_ready else "ollama"


def _public_user(user: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": user.get("id"),
        "email": user.get("email"),
        "display_name": user.get("display_name") or display_name_from_email(user.get("email", "")),
    }


async def _gmail_connection_status(user_id: str) -> dict[str, Any]:
    token = await get_provider_token(user_id, "gmail")
    if not token:
        return {
            "connected": False,
            "account_email": None,
            "expires_at": None,
            "connect_url": "/auth/google/start?redirect=/",
        }

    return {
        "connected": True,
        "account_email": token.get("account_email"),
        "expires_at": token.get("expiry"),
        "connect_url": "/auth/google/start?redirect=/",
    }


async def _slack_connection_status(user_id: str) -> dict[str, Any]:
    token = await get_provider_token(user_id, "slack")
    if not token:
        return {
            "connected": False,
            "workspace": None,
            "expires_at": None,
            "connect_url": "/auth/slack/start?redirect=/",
        }

    return {
        "connected": True,
        "workspace": token.get("account_email"),
        "expires_at": token.get("expiry"),
        "connect_url": "/auth/slack/start?redirect=/",
    }


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Serve the main SPA."""
    index_path = TEMPLATES_DIR / "index.html"
    if not index_path.exists():
        return HTMLResponse("<h1>UI not built yet — run Step 12</h1>", status_code=503)
    return templates.TemplateResponse(request, "index.html")


@app.get("/health")
async def health() -> JSONResponse:
    return JSONResponse({"ok": True})


class AuthRequest(BaseModel):
    email: str
    password: str
    display_name: str = ""


@app.get("/api/auth/me")
async def auth_me(request: Request) -> JSONResponse:
    await purge_expired_sessions()
    user = await get_current_user_optional(request)
    if not user:
        return JSONResponse({"authenticated": False, "user": None})
    return JSONResponse({"authenticated": True, "user": _public_user(user)})


@app.post("/api/auth/register")
async def auth_register(request: Request, body: AuthRequest) -> JSONResponse:
    await purge_expired_sessions()
    settings = get_settings()
    email = normalize_email(body.email)
    password = body.password or ""
    if "@" not in email:
        raise HTTPException(status_code=400, detail="Invalid email")
    if len(password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")

    existing = await get_user_by_email(email)
    if existing:
        raise HTTPException(status_code=409, detail="Email already registered")

    user = await create_user(
        email=email,
        password_hash=hash_password(password),
        display_name=(body.display_name or display_name_from_email(email)).strip(),
    )
    session_id = await create_user_session(user["id"], settings.auth_session_ttl_hours)
    resp = JSONResponse({"success": True, "user": _public_user(user)})
    set_session_cookie(resp, request, session_id)
    return resp


@app.post("/api/auth/login")
async def auth_login(request: Request, body: AuthRequest) -> JSONResponse:
    await purge_expired_sessions()
    settings = get_settings()
    email = normalize_email(body.email)
    password = body.password or ""
    user = await get_user_by_email(email)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if not verify_password(password, user.get("password_hash", "")):
        raise HTTPException(status_code=401, detail="Invalid email/password")

    session_id = await create_user_session(user["id"], settings.auth_session_ttl_hours)
    resp = JSONResponse({"success": True, "user": _public_user(user)})
    set_session_cookie(resp, request, session_id)
    return resp


@app.post("/api/auth/logout")
async def auth_logout(request: Request) -> JSONResponse:
    sid = session_id_from_request(request)
    if sid:
        await delete_user_session(sid)
    resp = JSONResponse({"success": True})
    clear_session_cookie(resp)
    return resp


@app.get("/api/providers/status")
async def providers_status(request: Request) -> JSONResponse:
    user = await require_current_user(request)
    gmail = await _gmail_connection_status(user["id"])
    slack = await _slack_connection_status(user["id"])
    settings = get_settings()
    return JSONResponse(
        {
            "gmail": gmail,
            "slack": slack,
            "telegram": {
                "connected": settings.telegram_enabled,
                "connect_supported": False,
                "message": "Telegram connect flow is not yet implemented in this branch.",
            },
        }
    )


@app.get("/auth/google/start")
async def auth_google_start(request: Request, redirect: str = "/") -> RedirectResponse:
    user = await require_current_user(request)
    safe_redirect = redirect if redirect.startswith("/") else "/"
    state = secrets.token_urlsafe(32)

    await purge_expired_oauth_state()
    await create_oauth_state(
        user_id=user["id"],
        provider="gmail",
        state=state,
        redirect_to=safe_redirect,
        ttl_minutes=10,
    )

    try:
        url = build_google_authorize_url(
            state=state,
            login_hint=user.get("email", ""),
            scopes=GMAIL_OAUTH_SCOPES,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return RedirectResponse(url=url)


@app.get("/auth/google/callback")
async def auth_google_callback(
    request: Request,
    code: str = "",
    state: str = "",
) -> RedirectResponse:
    user = await require_current_user(request)
    if not code or not state:
        raise HTTPException(status_code=400, detail="Missing OAuth code/state")

    state_row = await consume_oauth_state(user_id=user["id"], provider="gmail", state=state)
    if not state_row:
        raise HTTPException(status_code=400, detail="Invalid or expired OAuth state")

    try:
        token_data = await exchange_code_for_tokens(code)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Google token exchange failed") from exc
    access_token = str(token_data.get("access_token", "")).strip()
    refresh_token = str(token_data.get("refresh_token", "")).strip()
    if not access_token:
        raise HTTPException(status_code=400, detail="Google token exchange failed")

    expires_in = int(token_data.get("expires_in", 0) or 0)
    expiry = ""
    if expires_in > 0:
        expiry = (datetime.now(timezone.utc) + timedelta(seconds=expires_in)).isoformat()

    scopes_raw = str(token_data.get("scope", "")).strip()
    scopes = scopes_raw.split() if scopes_raw else GMAIL_OAUTH_SCOPES

    account_email = ""
    try:
        profile = await fetch_gmail_profile(access_token)
        account_email = str(profile.get("emailAddress", "")).strip()
    except Exception:
        # Non-fatal: token is still valid even if profile call fails.
        account_email = ""

    existing = await get_provider_token(user["id"], "gmail")
    if not refresh_token and existing and existing.get("refresh_token"):
        try:
            refresh_token = decrypt_secret(existing["refresh_token"])
        except Exception:
            refresh_token = ""

    try:
        client_id, client_secret, _ = get_google_oauth_config()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    await upsert_provider_token(
        user_id=user["id"],
        provider="gmail",
        access_token=encrypt_secret(access_token),
        refresh_token=encrypt_secret(refresh_token) if refresh_token else "",
        token_uri=encrypt_secret("https://oauth2.googleapis.com/token"),
        client_id=encrypt_secret(client_id),
        client_secret=encrypt_secret(client_secret),
        scopes=scopes,
        expiry=expiry,
        account_email=account_email,
    )
    try:
        from clients.gmail_client import get_user_gmail_client
        get_user_gmail_client.cache_clear()
    except Exception:
        pass

    redirect_to = str(state_row.get("redirect_to") or "/")
    if not redirect_to.startswith("/"):
        redirect_to = "/"
    sep = "&" if "?" in redirect_to else "?"
    return RedirectResponse(url=f"{redirect_to}{sep}gmail=connected")


@app.get("/auth/slack/start")
async def auth_slack_start(request: Request, redirect: str = "/") -> RedirectResponse:
    user = await require_current_user(request)
    safe_redirect = redirect if redirect.startswith("/") else "/"
    state = secrets.token_urlsafe(32)

    await purge_expired_oauth_state()
    await create_oauth_state(
        user_id=user["id"],
        provider="slack",
        state=state,
        redirect_to=safe_redirect,
        ttl_minutes=10,
    )

    try:
        url = build_slack_authorize_url(state=state)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return RedirectResponse(url=url)


@app.get("/auth/slack/callback")
async def auth_slack_callback(
    request: Request,
    code: str = "",
    state: str = "",
) -> RedirectResponse:
    user = await require_current_user(request)
    if not code or not state:
        raise HTTPException(status_code=400, detail="Missing OAuth code/state")

    state_row = await consume_oauth_state(user_id=user["id"], provider="slack", state=state)
    if not state_row:
        raise HTTPException(status_code=400, detail="Invalid or expired OAuth state")

    try:
        token_data = await exchange_slack_code(code)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Slack token exchange failed") from exc
    access_token = str(token_data.get("access_token", "")).strip()
    if not access_token:
        raise HTTPException(status_code=400, detail="Slack token exchange failed")

    team = token_data.get("team") or {}
    team_name = str(team.get("name") or "").strip()
    team_id = str(team.get("id") or "").strip()
    workspace = team_name or team_id
    if team_name and team_id:
        workspace = f"{team_name} ({team_id})"

    scopes_raw = str(token_data.get("scope", "")).strip()
    if scopes_raw:
        scopes = [s.strip() for s in scopes_raw.split(",") if s.strip()]
    else:
        _, _, _, scopes = get_slack_oauth_config()

    try:
        client_id, client_secret, _, _ = get_slack_oauth_config()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    await upsert_provider_token(
        user_id=user["id"],
        provider="slack",
        access_token=encrypt_secret(access_token),
        refresh_token="",
        token_uri=encrypt_secret("https://slack.com/api/oauth.v2.access"),
        client_id=encrypt_secret(client_id),
        client_secret=encrypt_secret(client_secret),
        scopes=scopes,
        expiry="",
        account_email=workspace,
    )

    try:
        from clients.slack_client import get_user_slack_client
        get_user_slack_client.cache_clear()
    except Exception:
        pass

    redirect_to = str(state_row.get("redirect_to") or "/")
    if not redirect_to.startswith("/"):
        redirect_to = "/"
    sep = "&" if "?" in redirect_to else "?"
    return RedirectResponse(url=f"{redirect_to}{sep}slack=connected")


@app.post("/api/providers/gmail/disconnect")
async def gmail_disconnect(request: Request) -> JSONResponse:
    user = await require_current_user(request)
    await delete_provider_token(user["id"], "gmail")
    try:
        from clients.gmail_client import get_user_gmail_client
        get_user_gmail_client.cache_clear()
    except Exception:
        pass
    return JSONResponse({"success": True})


@app.post("/api/providers/slack/disconnect")
async def slack_disconnect(request: Request) -> JSONResponse:
    user = await require_current_user(request)
    await delete_provider_token(user["id"], "slack")
    try:
        from clients.slack_client import get_user_slack_client
        get_user_slack_client.cache_clear()
    except Exception:
        pass
    return JSONResponse({"success": True})


@app.get("/api/status")
async def get_status(request: Request) -> JSONResponse:
    """Return platform connection status and demo mode flags."""
    user = await require_current_user(request)
    settings = get_settings()
    counts = await get_unread_counts(user_id=user["id"])
    gmail = await _gmail_connection_status(user["id"])
    slack = await _slack_connection_status(user["id"])

    gmail_connected = bool(gmail["connected"])
    slack_connected = bool(slack["connected"])
    telegram_connected = settings.telegram_enabled
    demo_mode = not (gmail_connected or slack_connected or telegram_connected)

    return JSONResponse({
        "demo_mode": demo_mode,
        "platforms": {
            "gmail": {
                "connected": gmail_connected,
                "demo": not gmail_connected,
                "unread": counts.get("gmail", 0),
                "account_email": gmail.get("account_email"),
            },
            "slack": {
                "connected": slack_connected,
                "demo": not slack_connected,
                "unread": counts.get("slack", 0),
                "workspace": slack.get("workspace"),
            },
            "telegram": {
                "connected": telegram_connected,
                "demo": not telegram_connected,
                "unread": counts.get("telegram", 0),
            },
        },
        "user": _public_user(user),
        "total_unread": sum(counts.values()),
        "server_time": datetime.now(timezone.utc).isoformat(),
    })


@app.get("/api/messages/all")
async def messages_all(request: Request, limit: int = 50) -> JSONResponse:
    """Return messages from all platforms, newest first."""
    user = await require_current_user(request)
    rows = await get_messages(limit=limit, user_id=user["id"])
    settings = get_settings()
    gmail = await _gmail_connection_status(user["id"])
    slack = await _slack_connection_status(user["id"])
    demo_mode = not (gmail["connected"] or slack["connected"] or settings.telegram_enabled)
    return JSONResponse({
        "messages": [_format_message(r) for r in rows],
        "count": len(rows),
        "demo_mode": demo_mode,
    })


@app.get("/api/messages/gmail")
async def messages_gmail(request: Request, limit: int = 50) -> JSONResponse:
    user = await require_current_user(request)
    rows = await get_messages(platform="gmail", limit=limit, user_id=user["id"])
    gmail = await _gmail_connection_status(user["id"])
    return JSONResponse({
        "messages": [_format_message(r) for r in rows],
        "count": len(rows),
        "demo_mode": not gmail["connected"],
    })


@app.get("/api/messages/slack")
async def messages_slack(request: Request, limit: int = 20) -> JSONResponse:
    user = await require_current_user(request)
    rows = await get_messages(platform="slack", limit=limit, user_id=user["id"])
    slack = await _slack_connection_status(user["id"])
    return JSONResponse({
        "messages": [_format_message(r) for r in rows],
        "count": len(rows),
        "demo_mode": not slack["connected"],
    })


@app.get("/api/messages/telegram")
async def messages_telegram(request: Request, limit: int = 20) -> JSONResponse:
    user = await require_current_user(request)
    rows = await get_messages(platform="telegram", limit=limit, user_id=user["id"])
    settings = get_settings()
    return JSONResponse({
        "messages": [_format_message(r) for r in rows],
        "count": len(rows),
        "demo_mode": not settings.telegram_enabled,
    })


@app.get("/api/unread-counts")
async def unread_counts(request: Request) -> JSONResponse:
    user = await require_current_user(request)
    counts = await get_unread_counts(user_id=user["id"])
    return JSONResponse({
        "gmail":    counts.get("gmail", 0),
        "slack":    counts.get("slack", 0),
        "telegram": counts.get("telegram", 0),
        "total":    sum(counts.values()),
    })


class MarkReadRequest(BaseModel):
    message_id: str


@app.post("/api/mark-read")
async def api_mark_read(request: Request, body: MarkReadRequest) -> JSONResponse:
    """Mark a message as read in the local cache."""
    user = await require_current_user(request)
    success = await mark_read(body.message_id, user_id=user["id"])
    return JSONResponse({"success": success, "message_id": body.message_id})


@app.post("/api/refresh")
async def api_refresh(request: Request) -> JSONResponse:
    """Force re-fetch from all platforms and update the cache."""
    user = await require_current_user(request)
    try:
        results = await _refresh_all_platforms(user["id"])
        counts = await get_unread_counts(user_id=user["id"])
        return JSONResponse({
            "success": True,
            "refreshed": results,
            "unread_counts": counts,
        })
    except Exception as exc:
        logger.exception("Refresh failed")
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/tool-log")
async def api_tool_log(request: Request, limit: int = 30) -> JSONResponse:
    """Return recent MCP tool call history."""
    user = await require_current_user(request)
    rows = await get_tool_log(limit=limit, user_id=user["id"])
    return JSONResponse({
        "entries": [_format_tool_log(r) for r in rows],
        "count": len(rows),
    })


# ── Telegram connectivity test ────────────────────────────────────────────────

@app.get("/api/telegram/test")
async def telegram_test(request: Request) -> JSONResponse:
    """
    Test Telegram bot connectivity and return bot info.
    Shows proxy status so user can confirm proxy is working.
    """
    await require_current_user(request)
    settings = get_settings()

    if not settings.telegram_enabled:
        return JSONResponse({
            "success": False,
            "error": "Telegram token not configured",
            "proxy": settings.telegram_proxy_url or None,
        })

    try:
        from clients.telegram_client import get_telegram_client
        client = get_telegram_client()
        bot_info = await client.get_me()
        return JSONResponse({
            "success": True,
            "bot_id": bot_info.get("id"),
            "bot_name": bot_info.get("first_name"),
            "bot_username": bot_info.get("username"),
            "proxy": settings.telegram_proxy_url or None,
            "proxy_active": bool(settings.telegram_proxy_url),
        })
    except Exception as exc:
        return JSONResponse({
            "success": False,
            "error": str(exc),
            "error_type": type(exc).__name__,
            "proxy": settings.telegram_proxy_url or None,
            "proxy_active": bool(settings.telegram_proxy_url),
            "hint": (
                "api.telegram.org appears to be blocked by your ISP. "
                "Set TELEGRAM_PROXY_URL in .env to a working SOCKS5 or HTTP proxy."
            ) if type(exc).__name__ in ("TimedOut", "ConnectTimeout", "NetworkError") else None,
        })


@app.get("/api/telegram/personal/status")
async def telegram_personal_status(request: Request) -> JSONResponse:
    """Check Telethon personal account status."""
    await require_current_user(request)
    settings = get_settings()
    client = get_telethon_client()
    if client is None:
        return JSONResponse({
            "configured": False,
            "authorized": False,
            "message": "Set TELEGRAM_API_ID and TELEGRAM_API_HASH in .env, then run: python telethon_login.py",
        })
    try:
        authorized = await client.connect()
        if authorized:
            from telethon.tl.types import User  # type: ignore
            me = await client._client.get_me()
            name = f"{me.first_name or ''} {me.last_name or ''}".strip()
            return JSONResponse({
                "configured": True,
                "authorized": True,
                "name": name,
                "username": me.username,
                "phone": me.phone,
                "proxy": settings.telegram_proxy_url or None,
            })
        else:
            return JSONResponse({
                "configured": True,
                "authorized": False,
                "message": "Session not authorized. Run: python telethon_login.py",
            })
    except Exception as exc:
        return JSONResponse({
            "configured": True,
            "authorized": False,
            "error": str(exc),
            "message": "Run: python telethon_login.py",
        })


# ── Ollama / LLM endpoints ────────────────────────────────────────────────────

@app.get("/api/ai/status")
@app.get("/api/ollama/status")
async def ollama_status(request: Request) -> JSONResponse:
    """
    Backward-compatible AI status endpoint.
    Route name is kept for frontend compatibility.
    """
    await require_current_user(request)
    provider = _select_ai_provider()

    if provider == "gemini":
        return JSONResponse({
            "running": True,
            "models": [GEMINI_MODEL],
            "best_model": GEMINI_MODEL,
            "base_url": "https://generativelanguage.googleapis.com",
            "provider": "gemini",
            "configured": True,
        })

    if provider == "none":
        return JSONResponse({
            "running": False,
            "models": [],
            "best_model": None,
            "base_url": "https://generativelanguage.googleapis.com",
            "provider": "gemini",
            "configured": False,
            "message": "GEMINI_API_KEY missing or SDK unavailable",
        })

    running = await is_ollama_running()
    models = await list_models() if running else []
    best = await get_best_available_model() if running else None
    return JSONResponse({
        "running": running,
        "models": models,
        "best_model": best,
        "base_url": OLLAMA_BASE,
        "provider": "ollama",
        "configured": running,
    })


class SummarizeRequest(BaseModel):
    message_id: str
    platform: str
    sender: str = ""
    body: str
    model: str = ""


@app.post("/api/summarize")
async def api_summarize(request: Request, req: SummarizeRequest) -> JSONResponse:
    """
    Summarize a message body using configured AI provider.
    Falls back to extractive summary if provider is unavailable.
    """
    await require_current_user(request)
    provider = _select_ai_provider()

    if provider == "gemini":
        try:
            model = req.model or GEMINI_MODEL
            summary = await summarize_message_gemini(
                body=req.body,
                platform=req.platform,
                sender=req.sender,
                model=model,
            )
            return JSONResponse({
                "summary": summary,
                "model": model,
                "ollama_running": True,  # compatibility with current frontend flag
                "provider": "gemini",
                "message": f"Summarized using {model}",
            })
        except Exception as exc:
            logger.warning("Gemini summarize failed, falling back: %s", exc)

    if provider == "ollama":
        running = await is_ollama_running()
        if running:
            try:
                model = req.model or await get_best_available_model()
                summary = await summarize_message(
                    body=req.body,
                    platform=req.platform,
                    sender=req.sender,
                    model=model,
                )
                return JSONResponse({
                    "summary": summary,
                    "model": model,
                    "ollama_running": True,
                    "provider": "ollama",
                    "message": f"Summarized using {model}",
                })
            except Exception as exc:
                logger.warning("Ollama summarize failed, falling back: %s", exc)

    fallback = _extractive_summary(req.body, sentence_limit=3)
    return JSONResponse({
        "summary": fallback,
        "model": "extractive-fallback",
        "ollama_running": False,
        "provider": "fallback",
        "message": "AI unavailable — showing extractive summary.",
    })


class SendReplyRequest(BaseModel):
    message_id: str
    platform: str                  # gmail | slack | telegram
    thread_id: str = ""
    sender_email: str = ""
    subject: str = ""
    channel: str = ""
    chat_id: str = ""
    body: str                      # reply text to send
    use_ai_draft: bool = False     # if True, draft the reply with Ollama first
    original_body: str = ""        # needed when use_ai_draft=True


@app.post("/api/send-reply")
async def api_send_reply(request: Request, req: SendReplyRequest) -> JSONResponse:
    """
    Send a reply via the appropriate platform client.
    Optionally drafts the reply using Ollama before sending.
    """
    user = await require_current_user(request)
    body = req.body

    # AI-draft mode: generate reply text with configured provider
    if req.use_ai_draft and req.original_body:
        provider = _select_ai_provider()
        try:
            if provider == "gemini":
                body = await draft_reply_gemini(
                    original_body=req.original_body,
                    platform=req.platform,
                    sender=req.sender_email,
                )
            elif provider == "ollama" and await is_ollama_running():
                model = await get_best_available_model()
                body = await draft_reply(
                    original_body=req.original_body,
                    platform=req.platform,
                    sender=req.sender_email,
                    model=model,
                )
            elif not body.strip():
                body = _build_template_draft(
                    original_body=req.original_body,
                    sender=req.sender_email,
                )
        except Exception as exc:
            logger.warning("AI draft failed, using existing body: %s", exc)
            if not body.strip():
                body = _build_template_draft(
                    original_body=req.original_body,
                    sender=req.sender_email,
                )

    # Route to correct platform tool
    try:
        if req.platform == "gmail":
            result = await send_gmail_reply(
                message_id=req.message_id,
                thread_id=req.thread_id,
                to=req.sender_email,
                subject=req.subject,
                body=body,
                user_id=user["id"],
            )
        elif req.platform == "slack":
            result = await send_slack_message(
                channel=req.channel or "#general",
                text=body,
                user_id=user["id"],
            )
        elif req.platform == "telegram":
            result = await send_telegram_reply(
                chat_id=req.chat_id or req.message_id,
                text=body,
                message_id=req.message_id,
                user_id=user["id"],
            )
        else:
            raise HTTPException(status_code=400, detail=f"Unknown platform: {req.platform}")

        # Mark original as read
        await mark_read(req.message_id, user_id=user["id"])

        return JSONResponse({
            "success": result.get("success", True),
            "demo_mode": result.get("demo_mode", False),
            "platform": req.platform,
            "body_sent": body,
            "ai_drafted": req.use_ai_draft,
            "message": result.get("message", "Reply sent."),
        })

    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("send-reply failed")
        raise HTTPException(status_code=500, detail=str(exc))


class DraftReplyRequest(BaseModel):
    original_body: str
    platform: str = ""
    sender: str = ""
    instructions: str = ""
    model: str = ""


@app.post("/api/draft-reply")
async def api_draft_reply(request: Request, req: DraftReplyRequest) -> JSONResponse:
    """Draft a reply using configured AI provider; never returns empty draft."""
    await require_current_user(request)
    provider = _select_ai_provider()

    if provider == "gemini":
        try:
            model = req.model or GEMINI_MODEL
            draft = await draft_reply_gemini(
                original_body=req.original_body,
                platform=req.platform,
                sender=req.sender,
                instructions=req.instructions,
                model=model,
            )
            return JSONResponse({
                "draft": draft,
                "model": model,
                "ollama_running": True,  # compatibility with frontend flag
                "provider": "gemini",
            })
        except Exception as exc:
            logger.warning("Gemini draft failed, falling back: %s", exc)

    if provider == "ollama":
        running = await is_ollama_running()
        if running:
            try:
                model = req.model or await get_best_available_model()
                draft = await draft_reply(
                    original_body=req.original_body,
                    platform=req.platform,
                    sender=req.sender,
                    instructions=req.instructions,
                    model=model,
                )
                return JSONResponse({
                    "draft": draft,
                    "model": model,
                    "ollama_running": True,
                    "provider": "ollama",
                })
            except Exception as exc:
                logger.warning("Ollama draft failed, falling back: %s", exc)

    fallback_draft = _build_template_draft(
        original_body=req.original_body,
        sender=req.sender,
        instructions=req.instructions,
    )
    return JSONResponse({
        "draft": fallback_draft,
        "model": "template-fallback",
        "ollama_running": False,
        "provider": "fallback",
        "message": "AI unavailable — returned template draft",
    })


# ── WebSocket ─────────────────────────────────────────────────────────────────

@app.websocket("/ws/tool-log")
async def ws_tool_log(websocket: WebSocket):
    """
    Live tool-log feed.
    Sends existing log on connect, then pushes new entries as they arrive.
    """
    settings = get_settings()
    sid = websocket.cookies.get(settings.auth_session_cookie_name, "").strip()
    user = await get_user_by_session(sid, touch=True) if sid else None
    if not user:
        await websocket.close(code=4401)
        return

    user_id = user["id"]
    await manager.connect(websocket, user_id=user_id)
    try:
        # Send current log snapshot on connect
        rows = await get_tool_log(limit=30, user_id=user_id)
        await websocket.send_json({
            "type": "snapshot",
            "entries": [_format_tool_log(r) for r in rows],
        })

        # Keep connection alive — new entries pushed by background poller
        while True:
            # Receive pings from client (keepalive)
            try:
                await asyncio.wait_for(websocket.receive_text(), timeout=30)
            except asyncio.TimeoutError:
                await websocket.send_json({"type": "ping"})

    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception as exc:
        logger.debug("WebSocket error: %s", exc)
        try:
            manager.disconnect(websocket)
        except Exception:
            pass
