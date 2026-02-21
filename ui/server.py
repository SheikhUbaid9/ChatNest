"""
ui/server.py — FastAPI backend for MCP Inbox UI.

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
  POST /api/summarize           → LLM summarize via Ollama
  POST /api/send-reply          → send reply via platform client
  GET  /api/ollama/status       → Ollama availability + models
  GET  /api/telegram/test      → test Telegram bot connectivity + proxy status

WebSocket:
  WS   /ws/tool-log             → live push of tool log events
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi import Request
from pydantic import BaseModel

from config import get_settings
from database import (
    get_messages,
    get_tool_log,
    get_unread_counts,
    init_db,
    mark_read,
    upsert_messages,
)
from tools.gmail_tools import get_gmail_unread, send_gmail_reply
from tools.slack_tools import get_slack_messages, send_slack_message
from tools.telegram_tools import get_telegram_messages, send_telegram_reply
from clients.telegram_client import get_telegram_data_async as _tg_fetch
from clients.telethon_client import get_personal_telegram_data, get_telethon_client
from clients.ollama_client import (
    is_ollama_running,
    list_models,
    summarize_message,
    draft_reply,
    get_best_available_model,
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
        self._connections: list[WebSocket] = []

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._connections.append(ws)
        logger.debug("WS client connected  (total: %d)", len(self._connections))

    def disconnect(self, ws: WebSocket) -> None:
        self._connections.remove(ws)
        logger.debug("WS client disconnected (total: %d)", len(self._connections))

    async def broadcast(self, data: dict[str, Any]) -> None:
        """Send a JSON payload to all connected clients."""
        dead: list[WebSocket] = []
        for ws in self._connections:
            try:
                await ws.send_json(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._connections.remove(ws)

    @property
    def active(self) -> int:
        return len(self._connections)


manager = ConnectionManager()

# ── Tool-log poller ───────────────────────────────────────────────────────────

_last_broadcast_id: int = 0


async def _poll_tool_log() -> None:
    """Background task: push new tool-log rows to WebSocket clients."""
    global _last_broadcast_id
    while True:
        try:
            await asyncio.sleep(0.5)
            if manager.active == 0:
                continue

            logs = await get_tool_log(limit=20)
            new_logs = [l for l in logs if l["id"] > _last_broadcast_id]
            if new_logs:
                _last_broadcast_id = max(l["id"] for l in new_logs)
                for entry in reversed(new_logs):   # oldest first
                    await manager.broadcast({"type": "tool_log", "entry": entry})
        except Exception as exc:
            logger.debug("Tool-log poller error: %s", exc)


# ── App lifespan ──────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    # Pre-warm cache with mock/real data on startup
    try:
        await _refresh_all_platforms()
    except Exception as exc:
        logger.warning("Startup prefetch failed: %s", exc)
    # Start background poller
    task = asyncio.create_task(_poll_tool_log())
    logger.info("MCP Inbox UI server ready")
    yield
    task.cancel()


# ── FastAPI app ───────────────────────────────────────────────────────────────

app = FastAPI(
    title="MCP Inbox",
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

async def _refresh_all_platforms() -> dict[str, Any]:
    """Fetch fresh data from all platforms and cache it."""
    results: dict[str, Any] = {}

    gmail_result = await get_gmail_unread()
    results["gmail"] = {
        "count": gmail_result["count"],
        "is_mock": gmail_result["is_mock"],
    }

    slack_result = await get_slack_messages()
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
        await upsert_messages(personal_msgs)
        results["telegram"] = {"count": len(personal_msgs), "is_mock": False, "source": "personal"}
    else:
        tg_messages, tg_is_mock = await _tg_fetch()
        if tg_messages:
            from database import upsert_messages
            await upsert_messages(tg_messages)
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


@app.get("/api/status")
async def get_status() -> JSONResponse:
    """Return platform connection status and demo mode flags."""
    settings = get_settings()
    counts = await get_unread_counts()

    return JSONResponse({
        "demo_mode": settings.demo_mode,
        "platforms": {
            "gmail": {
                "connected": settings.gmail_enabled,
                "demo": not settings.gmail_enabled,
                "unread": counts.get("gmail", 0),
            },
            "slack": {
                "connected": settings.slack_enabled,
                "demo": not settings.slack_enabled,
                "unread": counts.get("slack", 0),
            },
            "telegram": {
                "connected": settings.telegram_enabled,
                "demo": not settings.telegram_enabled,
                "unread": counts.get("telegram", 0),
            },
        },
        "total_unread": sum(counts.values()),
        "server_time": datetime.now(timezone.utc).isoformat(),
    })


@app.get("/api/messages/all")
async def messages_all(limit: int = 50) -> JSONResponse:
    """Return messages from all platforms, newest first."""
    rows = await get_messages(limit=limit)
    settings = get_settings()
    return JSONResponse({
        "messages": [_format_message(r) for r in rows],
        "count": len(rows),
        "demo_mode": settings.demo_mode,
    })


@app.get("/api/messages/gmail")
async def messages_gmail(limit: int = 50) -> JSONResponse:
    rows = await get_messages(platform="gmail", limit=limit)
    settings = get_settings()
    return JSONResponse({
        "messages": [_format_message(r) for r in rows],
        "count": len(rows),
        "demo_mode": not settings.gmail_enabled,
    })


@app.get("/api/messages/slack")
async def messages_slack(limit: int = 20) -> JSONResponse:
    rows = await get_messages(platform="slack", limit=limit)
    settings = get_settings()
    return JSONResponse({
        "messages": [_format_message(r) for r in rows],
        "count": len(rows),
        "demo_mode": not settings.slack_enabled,
    })


@app.get("/api/messages/telegram")
async def messages_telegram(limit: int = 20) -> JSONResponse:
    rows = await get_messages(platform="telegram", limit=limit)
    settings = get_settings()
    return JSONResponse({
        "messages": [_format_message(r) for r in rows],
        "count": len(rows),
        "demo_mode": not settings.telegram_enabled,
    })


@app.get("/api/unread-counts")
async def unread_counts() -> JSONResponse:
    counts = await get_unread_counts()
    return JSONResponse({
        "gmail":    counts.get("gmail", 0),
        "slack":    counts.get("slack", 0),
        "telegram": counts.get("telegram", 0),
        "total":    sum(counts.values()),
    })


class MarkReadRequest(BaseModel):
    message_id: str


@app.post("/api/mark-read")
async def api_mark_read(body: MarkReadRequest) -> JSONResponse:
    """Mark a message as read in the local cache."""
    success = await mark_read(body.message_id)
    return JSONResponse({"success": success, "message_id": body.message_id})


@app.post("/api/refresh")
async def api_refresh() -> JSONResponse:
    """Force re-fetch from all platforms and update the cache."""
    try:
        results = await _refresh_all_platforms()
        counts = await get_unread_counts()
        return JSONResponse({
            "success": True,
            "refreshed": results,
            "unread_counts": counts,
        })
    except Exception as exc:
        logger.exception("Refresh failed")
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/tool-log")
async def api_tool_log(limit: int = 30) -> JSONResponse:
    """Return recent MCP tool call history."""
    rows = await get_tool_log(limit=limit)
    return JSONResponse({
        "entries": [_format_tool_log(r) for r in rows],
        "count": len(rows),
    })


# ── Telegram connectivity test ────────────────────────────────────────────────

@app.get("/api/telegram/test")
async def telegram_test() -> JSONResponse:
    """
    Test Telegram bot connectivity and return bot info.
    Shows proxy status so user can confirm proxy is working.
    """
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
async def telegram_personal_status() -> JSONResponse:
    """Check Telethon personal account status."""
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

@app.get("/api/ollama/status")
async def ollama_status() -> JSONResponse:
    """Check if Ollama is running and return available models."""
    running = await is_ollama_running()
    models = await list_models() if running else []
    best = await get_best_available_model() if running else None
    return JSONResponse({
        "running": running,
        "models": models,
        "best_model": best,
        "base_url": "http://localhost:11434",
    })


class SummarizeRequest(BaseModel):
    message_id: str
    platform: str
    sender: str = ""
    body: str
    model: str = ""


@app.post("/api/summarize")
async def api_summarize(req: SummarizeRequest) -> JSONResponse:
    """
    Summarize a message body using the local Ollama LLM.
    Falls back to an extractive summary if Ollama is not running.
    """
    running = await is_ollama_running()

    if not running:
        # Extractive fallback — first 3 sentences
        sentences = [s.strip() for s in req.body.replace("\n", " ").split(".") if s.strip()]
        fallback = ". ".join(sentences[:3]) + ("." if sentences else "")
        return JSONResponse({
            "summary": fallback,
            "model": "extractive-fallback",
            "ollama_running": False,
            "message": "Ollama not running — showing extractive summary. Start Ollama for AI summaries.",
        })

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
            "message": f"Summarized using {model}",
        })
    except Exception as exc:
        logger.exception("Summarize failed")
        raise HTTPException(status_code=500, detail=str(exc))


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
async def api_send_reply(req: SendReplyRequest) -> JSONResponse:
    """
    Send a reply via the appropriate platform client.
    Optionally drafts the reply using Ollama before sending.
    """
    body = req.body

    # AI-draft mode: generate reply text with Ollama
    if req.use_ai_draft and req.original_body:
        running = await is_ollama_running()
        if running:
            try:
                model = await get_best_available_model()
                body = await draft_reply(
                    original_body=req.original_body,
                    platform=req.platform,
                    sender=req.sender_email,
                    model=model,
                )
            except Exception as exc:
                logger.warning("AI draft failed, using original body: %s", exc)

    # Route to correct platform tool
    try:
        if req.platform == "gmail":
            result = await send_gmail_reply(
                message_id=req.message_id,
                thread_id=req.thread_id,
                to=req.sender_email,
                subject=req.subject,
                body=body,
            )
        elif req.platform == "slack":
            result = await send_slack_message(
                channel=req.channel or "#general",
                text=body,
            )
        elif req.platform == "telegram":
            result = await send_telegram_reply(
                chat_id=req.chat_id or req.message_id,
                text=body,
                message_id=req.message_id,
            )
        else:
            raise HTTPException(status_code=400, detail=f"Unknown platform: {req.platform}")

        # Mark original as read
        await mark_read(req.message_id)

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
async def api_draft_reply(req: DraftReplyRequest) -> JSONResponse:
    """Use Ollama to draft a reply — returns draft text without sending."""
    running = await is_ollama_running()
    if not running:
        return JSONResponse({
            "draft": "",
            "ollama_running": False,
            "message": "Ollama not running — start it with: ollama serve",
        })
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
        })
    except Exception as exc:
        logger.exception("draft-reply failed")
        raise HTTPException(status_code=500, detail=str(exc))


# ── WebSocket ─────────────────────────────────────────────────────────────────

@app.websocket("/ws/tool-log")
async def ws_tool_log(websocket: WebSocket):
    """
    Live tool-log feed.
    Sends existing log on connect, then pushes new entries as they arrive.
    """
    await manager.connect(websocket)
    try:
        # Send current log snapshot on connect
        rows = await get_tool_log(limit=30)
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
