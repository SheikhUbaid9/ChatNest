"""
clients/slack_client.py — Slack Bot API client with mock fallback.

Real mode  : Uses SLACK_BOT_TOKEN (xoxb-...) to read channels and send
             messages via the slack-sdk WebClient.
Demo mode  : Returns realistic mock data when token is absent or
             FORCE_MOCK=true.

Required bot scopes:
  channels:history, channels:read, chat:write,
  im:history, im:read, groups:history, groups:read
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone, timedelta
from functools import lru_cache
from typing import Any

from auth_security import decrypt_secret
from config import get_settings
from database import get_provider_token_sync

logger = logging.getLogger(__name__)


# ── Mock data ─────────────────────────────────────────────────────────────────

def _ago(minutes: int = 0, hours: int = 0, days: int = 0) -> str:
    dt = datetime.now(timezone.utc) - timedelta(
        minutes=minutes, hours=hours, days=days
    )
    return dt.isoformat()


MOCK_MESSAGES: list[dict[str, Any]] = [
    {
        "id": "slack:mock001",
        "platform": "slack",
        "sender": "Alex Rivera",
        "sender_email": None,
        "subject": None,
        "preview": "Just pushed the new feature branch — can someone review PR #38 before EOD? It's a small change but touches the auth layer.",
        "body": (
            "Just pushed the new feature branch — can someone review PR #38 "
            "before EOD? It's a small change but touches the auth layer."
        ),
        "thread_id": "slack:thread:mock001",
        "channel": "#dev",
        "timestamp": _ago(minutes=8),
        "is_unread": True,
    },
    {
        "id": "slack:mock002",
        "platform": "slack",
        "sender": "Emma Wilson",
        "sender_email": None,
        "subject": None,
        "preview": "Standup in 10 minutes! Don't forget to update your Jira tickets before joining. Link in the channel description 👆",
        "body": (
            "Standup in 10 minutes! Don't forget to update your Jira tickets "
            "before joining. Link in the channel description 👆"
        ),
        "thread_id": "slack:thread:mock002",
        "channel": "#general",
        "timestamp": _ago(minutes=22),
        "is_unread": True,
    },
    {
        "id": "slack:mock003",
        "platform": "slack",
        "sender": "DataBot",
        "sender_email": None,
        "subject": None,
        "preview": "🚨 Alert: API response time exceeded 2s threshold (avg 2.34s over last 5 min). Affected endpoint: POST /api/messages/all",
        "body": (
            "🚨 Alert: API response time exceeded 2s threshold\n"
            "avg 2.34s over last 5 min\n"
            "Affected endpoint: POST /api/messages/all\n"
            "Environment: production"
        ),
        "thread_id": "slack:thread:mock003",
        "channel": "#alerts",
        "timestamp": _ago(hours=1),
        "is_unread": True,
    },
    {
        "id": "slack:mock004",
        "platform": "slack",
        "sender": "Jordan Kim",
        "sender_email": None,
        "subject": None,
        "preview": "Design review call moved to Thursday 3pm. Updating the calendar invite now. Let me know if that doesn't work for anyone.",
        "body": (
            "Design review call moved to Thursday 3pm. "
            "Updating the calendar invite now. "
            "Let me know if that doesn't work for anyone."
        ),
        "thread_id": "slack:thread:mock004",
        "channel": "#design",
        "timestamp": _ago(hours=3),
        "is_unread": False,
    },
    {
        "id": "slack:mock005",
        "platform": "slack",
        "sender": "Taylor Brooks",
        "sender_email": None,
        "subject": None,
        "preview": "The staging deploy succeeded ✅ All smoke tests passing. Ready for QA sign-off before we push to prod.",
        "body": (
            "The staging deploy succeeded ✅\n"
            "All smoke tests passing.\n"
            "Ready for QA sign-off before we push to prod."
        ),
        "thread_id": "slack:thread:mock005",
        "channel": "#deploys",
        "timestamp": _ago(hours=4),
        "is_unread": True,
    },
]


# ── Real Slack client ─────────────────────────────────────────────────────────

class SlackClient:
    """Thin wrapper around slack_sdk WebClient."""

    def __init__(self, token: str) -> None:
        # Import lazily — safe even without slack-sdk in demo mode
        from slack_sdk import WebClient as _WebClient  # type: ignore
        self._client = _WebClient(token=token)
        self._user_cache: dict[str, str] = {}   # user_id → display name

    # ── Public methods ────────────────────────────────────────────────────

    def get_messages(
        self,
        channel: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """
        Fetch recent messages from one channel or all joined channels.
        Returns messages in our standard format.
        """
        settings = get_settings()
        target = channel or settings.slack_default_channel

        # Resolve channel name → ID if needed
        channel_id = self._resolve_channel(target)
        if not channel_id:
            logger.warning("Slack: channel %r not found", target)
            return []

        response = self._client.conversations_history(
            channel=channel_id, limit=limit
        )
        messages = response.get("messages", [])

        result: list[dict[str, Any]] = []
        for i, msg in enumerate(messages):
            parsed = self._parse_message(msg, channel_name=target)
            if parsed:
                result.append(parsed)

        return result

    def get_all_channel_messages(self, limit_per_channel: int = 5) -> list[dict[str, Any]]:
        """Fetch recent messages from all joined public channels."""
        channels = self._list_channels()
        all_messages: list[dict[str, Any]] = []

        for ch in channels[:10]:  # cap at 10 channels
            try:
                msgs = self._get_channel_messages(
                    channel_id=ch["id"],
                    channel_name=ch["name"],
                    limit=limit_per_channel,
                )
                all_messages.extend(msgs)
            except Exception as exc:
                logger.debug("Slack: skipping channel %s: %s", ch["name"], exc)

        # Sort newest first
        all_messages.sort(key=lambda m: m["timestamp"], reverse=True)
        return all_messages

    def send_message(self, channel: str, text: str, thread_ts: str | None = None) -> bool:
        """Post a message to a channel or thread."""
        kwargs: dict[str, Any] = {"channel": channel, "text": text}
        if thread_ts:
            kwargs["thread_ts"] = thread_ts
        response = self._client.chat_postMessage(**kwargs)
        return bool(response.get("ok"))

    def get_thread_messages(self, channel: str, thread_ts: str) -> list[dict[str, Any]]:
        """Fetch all replies in a thread."""
        channel_id = self._resolve_channel(channel)
        if not channel_id:
            return []

        response = self._client.conversations_replies(
            channel=channel_id, ts=thread_ts
        )
        messages = response.get("messages", [])
        return [
            self._parse_message(m, channel_name=channel)
            for m in messages
            if self._parse_message(m, channel_name=channel)
        ]

    # ── Internal helpers ──────────────────────────────────────────────────

    def _resolve_channel(self, name: str) -> str | None:
        """Return channel ID for a name like 'general' or '#general'."""
        clean = name.lstrip("#")
        for ch in self._list_channels():
            if ch["name"] == clean:
                return ch["id"]
        # Maybe it's already an ID
        if name.startswith("C"):
            return name
        return None

    def _list_channels(self) -> list[dict[str, Any]]:
        response = self._client.conversations_list(
            types="public_channel,private_channel", limit=200
        )
        return response.get("channels", [])

    def _get_channel_messages(
        self, channel_id: str, channel_name: str, limit: int
    ) -> list[dict[str, Any]]:
        response = self._client.conversations_history(
            channel=channel_id, limit=limit
        )
        return [
            self._parse_message(m, channel_name=channel_name)
            for m in response.get("messages", [])
            if self._parse_message(m, channel_name=channel_name)
        ]

    def _resolve_user(self, user_id: str) -> str:
        """Resolve a Slack user_id to a display name (cached)."""
        if user_id in self._user_cache:
            return self._user_cache[user_id]
        try:
            info = self._client.users_info(user=user_id)
            profile = info["user"].get("profile", {})
            name = (
                profile.get("display_name")
                or profile.get("real_name")
                or user_id
            )
        except Exception:
            name = user_id
        self._user_cache[user_id] = name
        return name

    def _parse_message(
        self, msg: dict[str, Any], channel_name: str = ""
    ) -> dict[str, Any] | None:
        """Convert raw Slack message to our standard dict."""
        try:
            # Skip bot messages with no text, join/leave events, etc.
            if msg.get("subtype") in ("channel_join", "channel_leave"):
                return None
            text = msg.get("text", "").strip()
            if not text:
                return None

            user_id = msg.get("user") or msg.get("bot_id", "unknown")
            sender = self._resolve_user(user_id) if msg.get("user") else (
                msg.get("username") or msg.get("bot_id", "Bot")
            )

            ts = msg.get("ts", "0")
            timestamp = _ts_to_iso(ts)
            msg_id = f"slack:{channel_name}:{ts}"

            preview = text[:120] + "..." if len(text) > 120 else text

            return {
                "id": msg_id,
                "platform": "slack",
                "sender": sender,
                "sender_email": None,
                "subject": None,
                "preview": preview,
                "body": text,
                "thread_id": f"slack:thread:{channel_name}:{msg.get('thread_ts', ts)}",
                "channel": f"#{channel_name}",
                "timestamp": timestamp,
                "is_unread": True,   # Slack doesn't expose per-message read state via Bot API
                "raw_json": None,
            }
        except Exception as exc:
            logger.warning("Slack: failed to parse message: %s", exc)
            return None


# ── Helper ────────────────────────────────────────────────────────────────────

def _ts_to_iso(ts: str) -> str:
    """Convert Slack Unix timestamp string '1705744800.000100' to ISO-8601."""
    try:
        dt = datetime.fromtimestamp(float(ts), tz=timezone.utc)
        return dt.isoformat()
    except Exception:
        return datetime.now(timezone.utc).isoformat()


# ── Factory ───────────────────────────────────────────────────────────────────

@lru_cache(maxsize=1)
def get_slack_client() -> SlackClient:
    return SlackClient(token=get_settings().slack_bot_token)


def get_slack_data(limit: int = 20) -> tuple[list[dict[str, Any]], bool]:
    """
    Public entry point used by slack_tools.py.
    Returns (messages, is_mock).
    Falls back to mock data automatically on any error or missing token.
    """
    settings = get_settings()

    if not settings.slack_enabled:
        logger.debug("Slack: returning mock data (not configured)")
        return MOCK_MESSAGES, True

    try:
        client = get_slack_client()
        messages = client.get_all_channel_messages(limit_per_channel=5)
        logger.info("Slack: fetched %d real messages", len(messages))
        return messages, False
    except Exception as exc:
        logger.warning("Slack API error (%s) — falling back to mock data", exc)
        return MOCK_MESSAGES, True


def get_user_slack_token(user_id: str) -> str:
    row = get_provider_token_sync(user_id, "slack")
    if not row or not row.get("access_token"):
        return ""
    try:
        return decrypt_secret(row["access_token"])
    except Exception:
        return ""


def is_slack_connected_for_user(user_id: str) -> bool:
    return bool(get_user_slack_token(user_id))


@lru_cache(maxsize=256)
def get_user_slack_client(user_id: str) -> SlackClient:
    token = get_user_slack_token(user_id)
    if not token:
        raise RuntimeError("Slack is not connected for this user")
    return SlackClient(token=token)


def get_slack_data_for_user(
    user_id: str,
    *,
    channel: str | None = None,
    limit: int = 20,
) -> tuple[list[dict[str, Any]], bool]:
    token = get_user_slack_token(user_id)
    if not token:
        if channel:
            clean = channel.lstrip("#")
            messages = [
                m for m in MOCK_MESSAGES
                if m.get("channel", "").lstrip("#") == clean
            ] or MOCK_MESSAGES
            return messages, True
        return MOCK_MESSAGES, True

    try:
        client = get_user_slack_client(user_id)
        if channel:
            messages = client.get_messages(channel=channel, limit=limit)
        else:
            messages = client.get_all_channel_messages(limit_per_channel=max(1, min(limit, 20)))
        logger.info("Slack(user=%s): fetched %d real messages", user_id, len(messages))
        return messages, False
    except Exception as exc:
        logger.warning("Slack(user=%s) API error (%s) — fallback mock", user_id, exc)
        return MOCK_MESSAGES, True


async def send_slack_message_for_user(
    user_id: str,
    *,
    channel: str,
    text: str,
    thread_ts: str | None = None,
) -> bool:
    client = get_user_slack_client(user_id)
    return await asyncio.to_thread(client.send_message, channel, text, thread_ts)


async def get_slack_thread_for_user(
    user_id: str,
    *,
    channel: str,
    thread_ts: str,
) -> list[dict[str, Any]]:
    client = get_user_slack_client(user_id)
    return await asyncio.to_thread(client.get_thread_messages, channel, thread_ts)
