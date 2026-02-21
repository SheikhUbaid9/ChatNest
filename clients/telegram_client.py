"""
clients/telegram_client.py — Telegram Bot API client with mock fallback.

Real mode  : Uses TELEGRAM_BOT_TOKEN from @BotFather to poll for updates
             and send replies via the python-telegram-bot library (async).
Demo mode  : Returns realistic mock data when token is absent or
             FORCE_MOCK=true.

How Telegram bots receive messages:
  - Bots cannot read arbitrary chats — users must first send a message TO
    your bot (or add it to a group).
  - This client uses getUpdates (long-poll) to retrieve pending messages.
  - For production use, consider switching to webhooks.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone, timedelta
from functools import lru_cache
from typing import Any

from config import get_settings

logger = logging.getLogger(__name__)


# ── Mock data ─────────────────────────────────────────────────────────────────

def _ago(minutes: int = 0, hours: int = 0, days: int = 0) -> str:
    dt = datetime.now(timezone.utc) - timedelta(
        minutes=minutes, hours=hours, days=days
    )
    return dt.isoformat()


MOCK_MESSAGES: list[dict[str, Any]] = [
    {
        "id": "telegram:mock001",
        "platform": "telegram",
        "sender": "Lena Müller",
        "sender_email": None,
        "subject": None,
        "preview": "Hey! Did you see the MCP announcement? This could be huge for our workflow automation project 🚀",
        "body": (
            "Hey! Did you see the MCP announcement? "
            "This could be huge for our workflow automation project 🚀"
        ),
        "thread_id": "telegram:chat:1001",
        "channel": "DM",
        "timestamp": _ago(minutes=3),
        "is_unread": True,
        "chat_id": 1001,
    },
    {
        "id": "telegram:mock002",
        "platform": "telegram",
        "sender": "Dev Team Group",
        "sender_email": None,
        "subject": None,
        "preview": "Omar: The prod deployment is scheduled for tonight at 23:00 UTC. Make sure you're on standby in case rollback is needed.",
        "body": (
            "Omar: The prod deployment is scheduled for tonight at 23:00 UTC. "
            "Make sure you're on standby in case rollback is needed."
        ),
        "thread_id": "telegram:chat:-2001",
        "channel": "Dev Team",
        "timestamp": _ago(minutes=45),
        "is_unread": True,
        "chat_id": -2001,
    },
    {
        "id": "telegram:mock003",
        "platform": "telegram",
        "sender": "Nina Petrova",
        "sender_email": None,
        "subject": None,
        "preview": "Can you send me the API docs link again? The one I bookmarked seems to be broken now.",
        "body": "Can you send me the API docs link again? The one I bookmarked seems to be broken now.",
        "thread_id": "telegram:chat:1002",
        "channel": "DM",
        "timestamp": _ago(hours=1, minutes=15),
        "is_unread": True,
        "chat_id": 1002,
    },
    {
        "id": "telegram:mock004",
        "platform": "telegram",
        "sender": "AI News Channel",
        "sender_email": None,
        "subject": None,
        "preview": "📰 New paper: 'Agents with persistent memory outperform stateless LLMs on long-horizon tasks by 34%' — link in bio",
        "body": (
            "📰 New paper: 'Agents with persistent memory outperform stateless "
            "LLMs on long-horizon tasks by 34%' — link in bio"
        ),
        "thread_id": "telegram:chat:-3001",
        "channel": "AI News",
        "timestamp": _ago(hours=2),
        "is_unread": False,
        "chat_id": -3001,
    },
    {
        "id": "telegram:mock005",
        "platform": "telegram",
        "sender": "Rafael Souza",
        "sender_email": None,
        "subject": None,
        "preview": "Quick question — are you free for a call tomorrow morning? I have some feedback on the latest prototype.",
        "body": (
            "Quick question — are you free for a call tomorrow morning? "
            "I have some feedback on the latest prototype."
        ),
        "thread_id": "telegram:chat:1003",
        "channel": "DM",
        "timestamp": _ago(days=1),
        "is_unread": True,
        "chat_id": 1003,
    },
]


# ── Real Telegram client ──────────────────────────────────────────────────────

class TelegramClient:
    """
    Async Telegram Bot API client using httpx directly.

    Uses httpx.AsyncHTTPTransport for every request, which correctly routes
    through SOCKS5/HTTP proxies (required when api.telegram.org is ISP-blocked).
    PTB's Bot class is NOT used for HTTP calls — only for response parsing.

    Proxy: set TELEGRAM_PROXY_URL in .env (e.g. socks5://127.0.0.1:9050).
    """

    _BASE = "https://api.telegram.org/bot{token}/{method}"

    def __init__(self, token: str, proxy_url: str = "") -> None:
        self._token = token
        self._proxy_url = proxy_url.strip()
        self._last_update_id: int = 0
        self._timeout = 30 if proxy_url else 10   # Tor is slow

    def _make_client(self) -> Any:
        """Create a fresh httpx.AsyncClient (with proxy transport if set)."""
        import httpx  # type: ignore
        kwargs: dict[str, Any] = {"timeout": self._timeout}
        if self._proxy_url:
            logger.info("Telegram: using proxy %s", self._proxy_url)
            kwargs["transport"] = httpx.AsyncHTTPTransport(proxy=self._proxy_url)
        return httpx.AsyncClient(**kwargs)

    async def _call(self, method: str, retries: int = 3, **params: Any) -> Any:
        """Call a Telegram Bot API method. Returns the 'result' field."""
        url = self._BASE.format(token=self._token, method=method)
        last_exc: Exception = RuntimeError("No attempts made")
        for attempt in range(retries):
            try:
                async with self._make_client() as client:
                    resp = await client.get(url, params=params)
                data = resp.json()
                if not data.get("ok"):
                    raise RuntimeError(f"Telegram API error: {data.get('description')}")
                return data["result"]
            except Exception as exc:
                last_exc = exc
                logger.debug("Telegram %s attempt %d failed: %s", method, attempt + 1, exc)
                if attempt < retries - 1:
                    await asyncio.sleep(1)
        raise last_exc

    async def _post_json(self, method: str, payload: dict[str, Any]) -> Any:
        """POST JSON to a Telegram Bot API method."""
        import httpx  # type: ignore
        url = self._BASE.format(token=self._token, method=method)
        async with self._make_client() as client:
            resp = await client.post(url, json=payload)
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram API error: {data.get('description')}")
        return data["result"]

    # ── Public async methods ──────────────────────────────────────────────

    async def get_me(self) -> dict[str, Any]:
        """Return bot info (used for connectivity tests)."""
        return await self._call("getMe")

    async def get_messages(self, limit: int = 20) -> list[dict[str, Any]]:
        """
        Poll Telegram for pending updates and return messages.
        Uses offset to avoid re-fetching already-seen updates.
        """
        params: dict[str, Any] = {
            "timeout": 0,   # instant poll — avoids long-poll read-timeout issues over Tor
            "limit": limit,
            "allowed_updates": "message",
        }
        if self._last_update_id:
            params["offset"] = self._last_update_id + 1

        updates = await self._call("getUpdates", **params)

        messages: list[dict[str, Any]] = []
        for update in updates:
            uid = update.get("update_id", 0)
            if uid > self._last_update_id:
                self._last_update_id = uid

            parsed = self._parse_update_dict(update)
            if parsed:
                messages.append(parsed)

        logger.info("Telegram: fetched %d messages", len(messages))
        return messages

    async def send_reply(self, chat_id: int | str, text: str) -> bool:
        """Send a text message to a chat."""
        await self._post_json("sendMessage", {"chat_id": chat_id, "text": text})
        return True

    async def get_chat_info(self, chat_id: int | str) -> dict[str, Any]:
        """Return basic info about a chat or user."""
        return await self._call("getChat", chat_id=chat_id)

    # ── Parsing helpers ───────────────────────────────────────────────────

    def _parse_update_dict(self, update: dict[str, Any]) -> dict[str, Any] | None:
        """Convert a raw Telegram API update dict to our standard message dict."""
        try:
            msg = update.get("message")
            if not msg:
                return None

            text = msg.get("text") or msg.get("caption") or ""
            if not text.strip():
                return None

            # Determine sender name
            user = msg.get("from") or {}
            sender = (
                f"{user.get('first_name', '')} {user.get('last_name', '')}".strip()
                or user.get("username")
                or str(user.get("id", "Unknown"))
            )

            # Determine channel/chat label
            chat = msg.get("chat", {})
            chat_type = chat.get("type", "private")
            chat_id = chat.get("id", 0)

            if chat_type == "private":
                channel = "DM"
            else:
                channel = chat.get("title") or f"Group {chat_id}"

            # If message is from a group, prefix with sender name
            body = text
            if chat_type != "private":
                body = f"{sender}: {text}"

            preview = body[:120] + "..." if len(body) > 120 else body

            # Parse timestamp
            import time as _time
            ts_unix = msg.get("date", 0)
            ts = datetime.fromtimestamp(ts_unix, tz=timezone.utc).isoformat()

            msg_id = f"telegram:{msg.get('message_id', _time.time_ns())}"

            return {
                "id": msg_id,
                "platform": "telegram",
                "sender": sender if chat_type == "private" else channel,
                "sender_email": None,
                "subject": None,
                "preview": preview,
                "body": body,
                "thread_id": f"telegram:chat:{chat_id}",
                "channel": channel,
                "timestamp": ts,
                "is_unread": True,
                "chat_id": chat_id,
                "raw_json": None,
            }
        except Exception as exc:
            logger.warning("Telegram: failed to parse update: %s", exc)
            return None


# ── Sync wrapper ──────────────────────────────────────────────────────────────

def _run_async(coro: Any) -> Any:
    """Run a coroutine from sync context safely."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(asyncio.run, coro)
                return future.result(timeout=15)
        return loop.run_until_complete(coro)
    except RuntimeError:
        return asyncio.run(coro)


# ── Factory ───────────────────────────────────────────────────────────────────

@lru_cache(maxsize=1)
def get_telegram_client() -> TelegramClient:
    s = get_settings()
    return TelegramClient(token=s.telegram_bot_token, proxy_url=s.telegram_proxy_url)


def get_telegram_data(limit: int = 20) -> tuple[list[dict[str, Any]], bool]:
    """
    Sync entry point — used by non-async callers.
    Returns (messages, is_mock). Falls back to mock data on error.
    """
    settings = get_settings()

    if not settings.telegram_enabled:
        logger.debug("Telegram: returning mock data (not configured)")
        return MOCK_MESSAGES, True

    try:
        client = get_telegram_client()
        messages = _run_async(client.get_messages(limit=limit))
        logger.info("Telegram: fetched %d real messages", len(messages))
        return messages, False
    except Exception as exc:
        logger.warning("Telegram API error (%s) — falling back to mock data", exc)
        return MOCK_MESSAGES, True


async def get_telegram_data_async(limit: int = 20) -> tuple[list[dict[str, Any]], bool]:
    """
    Async entry point — used by FastAPI/asyncio callers (tools, server.py).
    Returns (messages, is_mock). Falls back to mock data on error.
    """
    settings = get_settings()

    if not settings.telegram_enabled:
        logger.debug("Telegram: returning mock data (not configured)")
        return MOCK_MESSAGES, True

    try:
        client = get_telegram_client()
        messages = await client.get_messages(limit=limit)
        logger.info("Telegram: fetched %d real messages", len(messages))
        return messages, False
    except Exception as exc:
        logger.warning("Telegram API error (%s) — falling back to mock data", exc)
        return MOCK_MESSAGES, True


async def send_telegram_reply_async(chat_id: int | str, text: str) -> bool:
    """Async helper for sending replies (used by telegram_tools.py)."""
    settings = get_settings()
    if not settings.telegram_enabled:
        logger.debug("Telegram send: demo mode — message not sent")
        return True   # pretend success in demo mode
    client = get_telegram_client()
    return await client.send_reply(chat_id=chat_id, text=text)
