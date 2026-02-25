"""
tools/slack_tools.py — FastMCP tools for Slack.

Tools exposed to Claude:
  get_slack_messages()      → fetch unread messages from channels
  send_slack_message()      → post a message to a channel or thread
  summarize_slack_thread()  → return a thread formatted for summarisation
"""

from __future__ import annotations

import logging
import time
from typing import Any

from clients.slack_client import (
    get_slack_client,
    get_slack_data,
    get_slack_data_for_user,
    get_slack_thread_for_user,
    is_slack_connected_for_user,
    MOCK_MESSAGES,
    send_slack_message_for_user,
)
from config import get_settings
from database import (
    finish_tool_call,
    log_tool_call,
    mark_read,
    upsert_messages,
)

logger = logging.getLogger(__name__)


# ── Shared tool-log helper (mirrors gmail_tools pattern) ─────────────────────

async def _timed_tool(name: str, platform: str = "slack", user_id: str = "global"):
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

async def get_slack_messages(
    channel: str | None = None,
    limit: int = 20,
    user_id: str = "global",
) -> dict[str, Any]:
    """
    Fetch recent Slack messages (real or mock).

    Args:
      channel : Optional channel name e.g. 'general' or '#dev'.
                When omitted, returns messages from all joined channels.
      limit   : Maximum messages to return (default 20).

    Returns a dict with:
      messages   : list of message objects
      count      : number returned
      channels   : list of distinct channels present in results
      is_mock    : True when using demo data
      demo_mode  : True when token not configured
    """
    ctx = await _timed_tool("get_slack_messages", user_id=user_id)

    try:
        if user_id != "global":
            messages, is_mock = get_slack_data_for_user(
                user_id=user_id,
                channel=channel,
                limit=limit,
            )
        elif channel:
            # Filter mock data by channel when in demo mode
            settings = get_settings()
            if not settings.slack_enabled:
                clean = channel.lstrip("#")
                messages = [
                    m for m in MOCK_MESSAGES
                    if m.get("channel", "").lstrip("#") == clean
                ] or MOCK_MESSAGES   # fallback to all if channel not in mock
                is_mock = True
            else:
                client = get_slack_client()
                messages = client.get_messages(channel=channel, limit=limit)
                is_mock = False
        else:
            messages, is_mock = get_slack_data(limit=limit)

        if messages:
            await upsert_messages(messages, user_id=user_id)

        channels = sorted({m.get("channel", "") for m in messages if m.get("channel")})
        summary = f"{len(messages)} messages from {len(channels)} channels {'(mock)' if is_mock else '(live)'}"
        await ctx.done(summary)

        return {
            "tool": "get_slack_messages",
            "messages": messages,
            "count": len(messages),
            "channels": channels,
            "is_mock": is_mock,
            "demo_mode": is_mock,
        }

    except Exception as exc:
        await ctx.error(str(exc))
        logger.exception("get_slack_messages failed")
        raise


async def send_slack_message(
    channel: str,
    text: str,
    thread_ts: str | None = None,
    user_id: str = "global",
) -> dict[str, Any]:
    """
    Post a message to a Slack channel or reply in a thread.

    Args:
      channel   : Channel name e.g. '#general' or 'general'
      text      : Message text (supports Slack mrkdwn formatting)
      thread_ts : Optional Slack thread timestamp to reply in-thread.
                  Pass the ts value from the original message.

    Returns success status and confirmation.
    """
    ctx = await _timed_tool("send_slack_message", user_id=user_id)
    settings = get_settings()

    try:
        if user_id != "global" and not is_slack_connected_for_user(user_id):
            dest = f"{channel}" + (f" (thread {thread_ts})" if thread_ts else "")
            await ctx.done(f"[Demo] Message to {dest} simulated")
            return {
                "tool": "send_slack_message",
                "success": True,
                "demo_mode": True,
                "channel": channel,
                "message": "[Demo Mode] Connect Slack to send real messages.",
                "text_preview": text[:80] + "..." if len(text) > 80 else text,
            }

        if user_id == "global" and not settings.slack_enabled:
            dest = f"{channel}" + (f" (thread {thread_ts})" if thread_ts else "")
            await ctx.done(f"[Demo] Message to {dest} simulated")
            return {
                "tool": "send_slack_message",
                "success": True,
                "demo_mode": True,
                "channel": channel,
                "message": f"[Demo Mode] Message to {channel} was simulated (no token configured).",
                "text_preview": text[:80] + "..." if len(text) > 80 else text,
            }

        if user_id != "global":
            success = await send_slack_message_for_user(
                user_id=user_id,
                channel=channel,
                text=text,
                thread_ts=thread_ts,
            )
        else:
            client = get_slack_client()
            success = client.send_message(
                channel=channel,
                text=text,
                thread_ts=thread_ts,
            )

        dest = channel + (f" (thread)" if thread_ts else "")
        await ctx.done(f"Message sent to {dest}")

        return {
            "tool": "send_slack_message",
            "success": success,
            "demo_mode": False,
            "channel": channel,
            "message": f"Message posted to {channel}.",
            "text_preview": text[:80] + "..." if len(text) > 80 else text,
        }

    except Exception as exc:
        await ctx.error(str(exc))
        logger.exception("send_slack_message failed")
        return {
            "tool": "send_slack_message",
            "success": False,
            "demo_mode": (user_id == "global" and not settings.slack_enabled) or (
                user_id != "global" and not is_slack_connected_for_user(user_id)
            ),
            "channel": channel,
            "message": f"Failed to send message: {exc}",
        }


async def summarize_slack_thread(
    thread_id: str,
    channel: str | None = None,
    user_id: str = "global",
) -> dict[str, Any]:
    """
    Fetch all messages in a Slack thread and return them formatted
    so Claude can produce a natural-language summary.

    Args:
      thread_id : The thread_id field from a message object
                  (format: 'slack:thread:#channel:timestamp')
      channel   : Optional channel name override.

    Returns a structured thread object with all messages.
    """
    ctx = await _timed_tool("summarize_slack_thread", user_id=user_id)
    settings = get_settings()

    try:
        if (user_id != "global" and not is_slack_connected_for_user(user_id)) or (
            user_id == "global" and not settings.slack_enabled
        ):
            # Demo: return the matching mock message as a single-item thread
            mock_msgs = [m for m in MOCK_MESSAGES if m.get("thread_id") == thread_id]
            if not mock_msgs:
                mock_msgs = MOCK_MESSAGES[:1]

            thread_text = _format_thread_for_summary(mock_msgs)
            await ctx.done(f"Thread ready ({len(mock_msgs)} messages, demo)")

            return {
                "tool": "summarize_slack_thread",
                "thread_id": thread_id,
                "message_count": len(mock_msgs),
                "demo_mode": True,
                "thread_text": thread_text,
                "messages": mock_msgs,
                "instruction": (
                    "Please summarize this Slack thread concisely. "
                    "Highlight key decisions, action items, and any blockers."
                ),
            }

        # Real mode: parse channel and ts from thread_id
        # Format: slack:thread:#channel:timestamp
        ch_name, thread_ts = _parse_thread_id(thread_id, channel)
        if not ch_name or not thread_ts:
            await ctx.error("Could not parse thread_id")
            return {
                "tool": "summarize_slack_thread",
                "success": False,
                "message": f"Invalid thread_id format: {thread_id}",
            }

        if user_id != "global":
            messages = await get_slack_thread_for_user(
                user_id=user_id,
                channel=ch_name,
                thread_ts=thread_ts,
            )
        else:
            client = get_slack_client()
            messages = client.get_thread_messages(
                channel=ch_name,
                thread_ts=thread_ts,
            )

        thread_text = _format_thread_for_summary(messages)
        await ctx.done(f"Thread fetched ({len(messages)} messages)")

        return {
            "tool": "summarize_slack_thread",
            "thread_id": thread_id,
            "channel": ch_name,
            "message_count": len(messages),
            "demo_mode": False,
            "thread_text": thread_text,
            "messages": messages,
            "instruction": (
                "Please summarize this Slack thread concisely. "
                "Highlight key decisions, action items, and any blockers."
            ),
        }

    except Exception as exc:
        await ctx.error(str(exc))
        logger.exception("summarize_slack_thread failed")
        return {
            "tool": "summarize_slack_thread",
            "thread_id": thread_id,
            "success": False,
            "message": f"Failed to fetch thread: {exc}",
        }


# ── Formatting helpers ────────────────────────────────────────────────────────

def _format_thread_for_summary(messages: list[dict[str, Any]]) -> str:
    """Return a readable plain-text representation of a Slack thread."""
    parts: list[str] = []
    for i, msg in enumerate(messages, 1):
        sender = msg.get("sender", "Unknown")
        channel = msg.get("channel", "")
        ts = msg.get("timestamp", "")[:16].replace("T", " ")
        body = msg.get("body") or msg.get("preview", "")

        parts.append(
            f"── Message {i} ──  [{channel}]\n"
            f"From : {sender}\n"
            f"Time : {ts}\n\n"
            f"{body.strip()}\n"
        )
    return "\n".join(parts)


def _parse_thread_id(thread_id: str, channel_override: str | None) -> tuple[str, str]:
    """
    Extract channel name and thread timestamp from a thread_id string.
    Format: 'slack:thread:#channel:1705744800.000100'
    Returns (channel_name, thread_ts) or ('', '') on failure.
    """
    if channel_override:
        # If caller passed channel explicitly, just extract the ts
        parts = thread_id.rsplit(":", 1)
        ts = parts[-1] if len(parts) == 2 else ""
        return channel_override.lstrip("#"), ts

    # Parse 'slack:thread:#general:1705744800.000100'
    try:
        parts = thread_id.split(":")
        # Find the numeric ts (contains a dot)
        ts = next((p for p in reversed(parts) if "." in p), "")
        # Channel is the segment before ts that may start with #
        ch = next(
            (p.lstrip("#") for p in reversed(parts) if p and "." not in p and p not in ("slack", "thread")),
            "",
        )
        return ch, ts
    except Exception:
        return "", ""
