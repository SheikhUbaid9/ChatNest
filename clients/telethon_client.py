"""
clients/telethon_client.py — Personal Telegram account client via Telethon.

Unlike the bot client, this connects to YOUR personal Telegram account using
the MTProto protocol, giving access to all your DMs, group chats, and channels.

Setup (one-time):
  1. Go to https://my.telegram.org/apps
  2. Log in → "API development tools" → Create new app
  3. Copy api_id and api_hash into .env
  4. Run: python telethon_login.py   (enter phone + OTP once)
  5. Session is saved — future startups connect automatically

Proxy: uses the same TELEGRAM_PROXY_URL as the bot client (SOCKS5 recommended
for ISP-blocked regions).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

from config import get_settings

logger = logging.getLogger(__name__)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _ago(minutes: int = 0, hours: int = 0, days: int = 0) -> str:
    dt = datetime.now(timezone.utc) - timedelta(
        minutes=minutes, hours=hours, days=days
    )
    return dt.isoformat()


def _parse_proxy(proxy_url: str) -> tuple | None:
    """Parse proxy URL into Telethon's (type, host, port) tuple."""
    if not proxy_url:
        return None
    try:
        import socks  # type: ignore
        url = proxy_url.strip()
        if url.startswith("socks5://"):
            rest = url[len("socks5://"):]
            host, port = rest.rsplit(":", 1)
            return (socks.SOCKS5, host, int(port))
        elif url.startswith("socks4://"):
            rest = url[len("socks4://"):]
            host, port = rest.rsplit(":", 1)
            return (socks.SOCKS4, host, int(port))
        elif url.startswith("http://"):
            rest = url[len("http://"):]
            host, port = rest.rsplit(":", 1)
            return (socks.HTTP, host, int(port))
    except Exception as exc:
        logger.warning("Failed to parse proxy URL %r: %s", proxy_url, exc)
    return None


# ── Telethon client wrapper ───────────────────────────────────────────────────

class TelethonPersonalClient:
    """
    Async Telethon client for accessing personal Telegram DMs and chats.
    Session is stored on disk — only needs OTP once.
    """

    def __init__(
        self,
        api_id: int,
        api_hash: str,
        session_path: str,
        proxy_url: str = "",
    ) -> None:
        self._api_id = api_id
        self._api_hash = api_hash
        self._session_path = session_path
        self._proxy = _parse_proxy(proxy_url)
        self._client: Any = None

    def _build_client(self) -> Any:
        from telethon import TelegramClient  # type: ignore
        kwargs: dict[str, Any] = {}
        if self._proxy:
            kwargs["proxy"] = self._proxy
            logger.info("Telethon: using proxy %s", self._proxy)
        return TelegramClient(
            self._session_path,
            self._api_id,
            self._api_hash,
            **kwargs,
        )

    async def connect(self) -> bool:
        """Connect using saved session. Returns True if already authorized."""
        if self._client is None:
            self._client = self._build_client()
        await self._client.connect()
        return await self._client.is_user_authorized()

    async def disconnect(self) -> None:
        if self._client:
            await self._client.disconnect()

    async def get_messages(
        self,
        limit_per_dialog: int = 5,
        max_dialogs: int = 20,
    ) -> list[dict[str, Any]]:
        """
        Fetch recent messages from your personal DMs and group chats.
        Returns messages across all dialogs, newest first.
        """
        if self._client is None or not await self._client.is_user_authorized():
            raise RuntimeError(
                "Telethon session not authorized. Run: python telethon_login.py"
            )

        from telethon.tl.types import User, Chat, Channel  # type: ignore

        all_messages: list[dict[str, Any]] = []

        async for dialog in self._client.iter_dialogs(limit=max_dialogs):
            entity = dialog.entity

            # Determine platform label
            if isinstance(entity, User):
                channel = "DM"
                sender_name = (
                    f"{entity.first_name or ''} {entity.last_name or ''}".strip()
                    or entity.username
                    or str(entity.id)
                )
            elif isinstance(entity, Channel):
                channel = entity.title or "Channel"
                sender_name = channel
            else:
                channel = getattr(entity, "title", None) or "Group"
                sender_name = channel

            # Fetch recent messages from this dialog
            async for msg in self._client.iter_messages(entity, limit=limit_per_dialog):
                if not msg.message:
                    continue   # skip media-only messages

                text = msg.message.strip()
                if not text:
                    continue

                # For groups/channels, prefix with the actual sender
                if isinstance(entity, User):
                    body = text
                else:
                    if msg.sender:
                        s = msg.sender
                        name = (
                            f"{getattr(s, 'first_name', '') or ''} "
                            f"{getattr(s, 'last_name', '') or ''}".strip()
                            or getattr(s, "username", None)
                            or str(s.id)
                        )
                        body = f"{name}: {text}"
                    else:
                        body = text

                preview = body[:120] + "..." if len(body) > 120 else body
                ts = msg.date.astimezone(timezone.utc).isoformat()
                msg_id = f"telegram:personal:{msg.id}"

                all_messages.append({
                    "id": msg_id,
                    "platform": "telegram",
                    "sender": sender_name,
                    "sender_email": None,
                    "subject": None,
                    "preview": preview,
                    "body": body,
                    "thread_id": f"telegram:chat:{dialog.id}",
                    "channel": channel,
                    "timestamp": ts,
                    "is_unread": dialog.unread_count > 0,
                    "chat_id": dialog.id,
                    "raw_json": None,
                })

        # Sort newest first
        all_messages.sort(key=lambda m: m["timestamp"], reverse=True)
        logger.info("Telethon: fetched %d personal messages", len(all_messages))
        return all_messages

    async def send_message(self, chat_id: int | str, text: str) -> bool:
        """Send a message to a chat."""
        if self._client is None:
            raise RuntimeError("Not connected")
        await self._client.send_message(chat_id, text)
        return True


# ── Factory ───────────────────────────────────────────────────────────────────

@lru_cache(maxsize=1)
def get_telethon_client() -> TelethonPersonalClient | None:
    """
    Return a TelethonPersonalClient if credentials are configured, else None.
    """
    s = get_settings()
    api_id_str = s.telegram_api_id.strip()
    api_hash = s.telegram_api_hash.strip()

    if not api_id_str or not api_hash:
        return None

    try:
        api_id = int(api_id_str)
    except ValueError:
        logger.error("TELEGRAM_API_ID must be a number, got: %r", api_id_str)
        return None

    return TelethonPersonalClient(
        api_id=api_id,
        api_hash=api_hash,
        session_path=str(s.telegram_session_path),
        proxy_url=s.telegram_proxy_url,
    )


async def get_personal_telegram_data(
    limit_per_dialog: int = 5,
    max_dialogs: int = 20,
) -> tuple[list[dict[str, Any]], bool]:
    """
    Fetch personal Telegram messages.
    Returns (messages, is_mock).
    Falls back to empty list (not mock) if session not authorized.
    """
    client = get_telethon_client()
    if client is None:
        return [], True   # not configured

    try:
        authorized = await client.connect()
        if not authorized:
            logger.warning(
                "Telethon session not authorized — run: python telethon_login.py"
            )
            return [], False

        messages = await client.get_messages(
            limit_per_dialog=limit_per_dialog,
            max_dialogs=max_dialogs,
        )
        return messages, False

    except Exception as exc:
        logger.warning("Telethon error (%s) — returning empty", exc)
        return [], False
