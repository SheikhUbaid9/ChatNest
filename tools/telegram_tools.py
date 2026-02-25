"""
tools/telegram_tools.py — FastMCP tools for Telegram.

Tools exposed to Claude:
  get_telegram_messages()   → fetch unread messages from bot inbox
  send_telegram_reply()     → reply to a chat
  summarize_telegram_chat() → return a chat formatted for summarisation
"""

from __future__ import annotations

import logging
import time
from typing import Any

from clients.telegram_client import (
    get_telegram_client,
    get_telegram_data,
    get_telegram_data_async,
    send_telegram_reply_async,
    MOCK_MESSAGES,
)
from config import get_settings
from database import (
    finish_tool_call,
    log_tool_call,
    mark_read,
    upsert_messages,
)

logger = logging.getLogger(__name__)


# ── Shared tool-log helper ────────────────────────────────────────────────────

async def _timed_tool(name: str, platform: str = "telegram", user_id: str = "global"):
    log_id = await log_tool_call(name, platform, user_id=user_id)
    start = time.monotonic()

    class _Ctx:
        id = log_id
        t0 = start

        async def done(self, summary: str) -> None:
            ms = int((time.monotonic() - self.t0) * 1000)
            await finish_tool_call(self.id, ms, summary)

        async def error(self, msg: str) -> None:
            ms = int((time.monotonic() - self.t0) * 1000)
            await finish_tool_call(self.id, ms, msg, status="error")

    return _Ctx()


# ── Tool implementations ──────────────────────────────────────────────────────

async def get_telegram_messages(limit: int = 20, user_id: str = "global") -> dict[str, Any]:
    """
    Fetch pending Telegram messages from the bot inbox (real or mock).

    In real mode this calls getUpdates on the Telegram Bot API and
    returns any messages sent to the bot since the last poll.

    Args:
      limit : Maximum messages to return (default 20).

    Returns a dict with:
      messages   : list of message objects
      count      : number returned
      chats      : list of distinct chat/channel names present
      is_mock    : True when using demo data
      demo_mode  : True when token not configured
    """
    ctx = await _timed_tool("get_telegram_messages", user_id=user_id)

    try:
        messages, is_mock = await get_telegram_data_async(limit=limit)

        if messages:
            await upsert_messages(messages, user_id=user_id)

        chats = sorted({m.get("channel", "") for m in messages if m.get("channel")})
        summary = (
            f"{len(messages)} messages from {len(chats)} chats "
            f"{'(mock)' if is_mock else '(live)'}"
        )
        await ctx.done(summary)

        return {
            "tool": "get_telegram_messages",
            "messages": messages,
            "count": len(messages),
            "chats": chats,
            "is_mock": is_mock,
            "demo_mode": is_mock,
        }

    except Exception as exc:
        await ctx.error(str(exc))
        logger.exception("get_telegram_messages failed")
        raise


async def send_telegram_reply(
    chat_id: int | str,
    text: str,
    message_id: str | None = None,
    user_id: str = "global",
) -> dict[str, Any]:
    """
    Send a reply to a Telegram chat or DM.

    Args:
      chat_id    : Numeric Telegram chat ID (from the message's chat_id field)
                   or a username like '@channelname'.
      text       : Message text to send (plain text or HTML).
      message_id : Optional — the id field of the message being replied to,
                   used to mark it as read in the local cache.

    Returns success status and confirmation.
    """
    ctx = await _timed_tool("send_telegram_reply", user_id=user_id)
    settings = get_settings()

    try:
        # Mark the source message read in cache if provided
        if message_id:
            await mark_read(message_id, user_id=user_id)

        if not settings.telegram_enabled:
            await ctx.done(f"[Demo] Reply to chat {chat_id} simulated")
            return {
                "tool": "send_telegram_reply",
                "success": True,
                "demo_mode": True,
                "chat_id": chat_id,
                "message": (
                    f"[Demo Mode] Reply to chat {chat_id} was simulated "
                    "(no token configured)."
                ),
                "text_preview": text[:80] + "..." if len(text) > 80 else text,
            }

        success = await send_telegram_reply_async(chat_id=chat_id, text=text)

        await ctx.done(f"Reply sent to chat {chat_id}")

        return {
            "tool": "send_telegram_reply",
            "success": success,
            "demo_mode": False,
            "chat_id": chat_id,
            "message": f"Reply sent to chat {chat_id}.",
            "text_preview": text[:80] + "..." if len(text) > 80 else text,
        }

    except Exception as exc:
        await ctx.error(str(exc))
        logger.exception("send_telegram_reply failed")
        return {
            "tool": "send_telegram_reply",
            "success": False,
            "demo_mode": not settings.telegram_enabled,
            "chat_id": chat_id,
            "message": f"Failed to send reply: {exc}",
        }


async def summarize_telegram_chat(
    chat_id: int | str | None = None,
    thread_id: str | None = None,
    limit: int = 20,
    user_id: str = "global",
) -> dict[str, Any]:
    """
    Fetch messages from a Telegram chat and return them formatted
    so Claude can produce a natural-language summary.

    Provide either:
      chat_id   : Numeric Telegram chat ID (preferred)
      thread_id : The thread_id field from a message object
                  (format: 'telegram:chat:12345')

    Args:
      limit : Max messages to include in the summary context (default 20).

    Returns a structured chat object with all messages.
    """
    ctx = await _timed_tool("summarize_telegram_chat", user_id=user_id)
    settings = get_settings()

    try:
        # Resolve chat_id from thread_id if not given directly
        resolved_chat_id = chat_id or _extract_chat_id(thread_id or "")

        if not settings.telegram_enabled:
            # Demo: find matching mock messages by chat_id or thread_id
            mock_msgs = _find_mock_messages(resolved_chat_id, thread_id)

            thread_text = _format_chat_for_summary(mock_msgs)
            await ctx.done(f"Chat summary ready ({len(mock_msgs)} messages, demo)")

            return {
                "tool": "summarize_telegram_chat",
                "chat_id": resolved_chat_id,
                "message_count": len(mock_msgs),
                "demo_mode": True,
                "thread_text": thread_text,
                "messages": mock_msgs,
                "instruction": (
                    "Please summarize this Telegram conversation concisely. "
                    "Highlight the main topic, any questions asked, and "
                    "suggested next steps."
                ),
            }

        # Real mode — fetch recent messages from the chat via getUpdates
        # (We retrieve all pending updates and filter by chat_id)
        all_messages, _ = await get_telegram_data_async(limit=limit)

        if resolved_chat_id:
            messages = [
                m for m in all_messages
                if str(m.get("chat_id", "")) == str(resolved_chat_id)
            ]
        else:
            messages = all_messages[:limit]

        if not messages:
            messages = all_messages[:5]   # fallback: show recent messages

        thread_text = _format_chat_for_summary(messages)
        await ctx.done(f"Chat fetched ({len(messages)} messages)")

        return {
            "tool": "summarize_telegram_chat",
            "chat_id": resolved_chat_id,
            "message_count": len(messages),
            "demo_mode": False,
            "thread_text": thread_text,
            "messages": messages,
            "instruction": (
                "Please summarize this Telegram conversation concisely. "
                "Highlight the main topic, any questions asked, and "
                "suggested next steps."
            ),
        }

    except Exception as exc:
        await ctx.error(str(exc))
        logger.exception("summarize_telegram_chat failed")
        return {
            "tool": "summarize_telegram_chat",
            "chat_id": chat_id,
            "success": False,
            "message": f"Failed to fetch chat: {exc}",
        }


# ── Formatting helpers ────────────────────────────────────────────────────────

def _format_chat_for_summary(messages: list[dict[str, Any]]) -> str:
    """Return a readable plain-text representation of a Telegram chat."""
    parts: list[str] = []
    for i, msg in enumerate(messages, 1):
        sender = msg.get("sender", "Unknown")
        channel = msg.get("channel", "DM")
        ts = msg.get("timestamp", "")[:16].replace("T", " ")
        body = msg.get("body") or msg.get("preview", "")

        parts.append(
            f"── Message {i} ──  [{channel}]\n"
            f"From : {sender}\n"
            f"Time : {ts}\n\n"
            f"{body.strip()}\n"
        )
    return "\n".join(parts)


def _extract_chat_id(thread_id: str) -> str:
    """
    Extract chat ID from thread_id string.
    Format: 'telegram:chat:12345'  →  '12345'
    """
    try:
        parts = thread_id.split(":")
        # Last segment is the chat id (may be negative for groups)
        return parts[-1] if parts else ""
    except Exception:
        return ""


def _find_mock_messages(
    chat_id: int | str | None,
    thread_id: str | None,
) -> list[dict[str, Any]]:
    """Find relevant mock messages by chat_id or thread_id."""
    if chat_id:
        matched = [
            m for m in MOCK_MESSAGES
            if str(m.get("chat_id", "")) == str(chat_id)
        ]
        if matched:
            return matched

    if thread_id:
        matched = [
            m for m in MOCK_MESSAGES
            if m.get("thread_id") == thread_id
        ]
        if matched:
            return matched

    # Fallback: first mock message
    return MOCK_MESSAGES[:1]
