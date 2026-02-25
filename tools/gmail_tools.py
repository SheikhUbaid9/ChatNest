"""
tools/gmail_tools.py — FastMCP tools for Gmail.

Tools exposed to Claude:
  get_gmail_unread()        → fetch last 20 unread emails
  send_gmail_reply()        → reply to an email thread
  mark_gmail_read()         → mark a message as read
  summarize_gmail_thread()  → summarize a thread for Claude to process
"""

from __future__ import annotations

import logging
import time
from typing import Any

from clients.gmail_client import (
    get_gmail_client,
    get_gmail_data,
    get_gmail_data_for_user,
    get_gmail_thread_for_user,
    mark_gmail_read_for_user,
    send_gmail_reply_for_user,
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

async def _timed_tool(name: str, platform: str = "gmail", user_id: str = "global"):
    """Context manager that logs start/finish to the tool_log table."""
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

async def get_gmail_unread(max_results: int = 50, user_id: str = "global") -> dict[str, Any]:
    """
    Fetch unread Gmail messages (real or mock).

    Returns a dict with:
      messages   : list of message objects
      count      : number returned
      is_mock    : True when using demo data
      demo_mode  : True when credentials not configured
    """
    ctx = await _timed_tool("get_gmail_unread", user_id=user_id)

    try:
        if user_id != "global":
            emails, is_mock = await get_gmail_data_for_user(user_id=user_id, max_results=max_results)
        else:
            emails, is_mock = get_gmail_data(max_results)

        # Cache in SQLite
        if emails:
            await upsert_messages(emails, user_id=user_id)

        summary = f"{len(emails)} emails {'(mock)' if is_mock else '(live)'}"
        await ctx.done(summary)

        return {
            "tool": "get_gmail_unread",
            "messages": emails,
            "count": len(emails),
            "is_mock": is_mock,
            "demo_mode": is_mock,
        }

    except Exception as exc:
        await ctx.error(str(exc))
        logger.exception("get_gmail_unread failed")
        raise


async def send_gmail_reply(
    message_id: str,
    thread_id: str,
    to: str,
    subject: str,
    body: str,
    user_id: str = "global",
) -> dict[str, Any]:
    """
    Reply to an existing Gmail thread.

    Args:
      message_id : The id field of the message being replied to
      thread_id  : The thread_id field from the message object
      to         : Recipient email address
      subject    : Email subject (Re: will be prepended if missing)
      body       : Plain-text reply body

    Returns success status and a confirmation message.
    """
    ctx = await _timed_tool("send_gmail_reply", user_id=user_id)
    settings = get_settings()

    try:
        if user_id == "global" and not settings.gmail_enabled:
            # Demo mode — simulate success
            await ctx.done(f"[Demo] Reply to {to} simulated")
            return {
                "tool": "send_gmail_reply",
                "success": True,
                "demo_mode": True,
                "message": f"[Demo Mode] Reply to {to} was simulated (no credentials configured).",
                "to": to,
                "subject": subject,
            }

        native_thread = thread_id.replace("gmail:", "").split(":")[0]
        if user_id != "global":
            success = await send_gmail_reply_for_user(
                user_id=user_id,
                thread_id=native_thread,
                to=to,
                subject=subject,
                body=body,
            )
        else:
            client = get_gmail_client()
            success = client.send_reply(
                thread_id=native_thread,
                to=to,
                subject=subject,
                body=body,
            )

        await ctx.done(f"Reply sent to {to}")
        return {
            "tool": "send_gmail_reply",
            "success": success,
            "demo_mode": False,
            "message": f"Reply sent to {to}.",
            "to": to,
            "subject": subject,
        }

    except Exception as exc:
        await ctx.error(str(exc))
        logger.exception("send_gmail_reply failed")
        return {
            "tool": "send_gmail_reply",
            "success": False,
            "demo_mode": (user_id == "global" and not settings.gmail_enabled),
            "message": f"Failed to send reply: {exc}",
        }


async def mark_gmail_read(message_id: str, user_id: str = "global") -> dict[str, Any]:
    """
    Mark a Gmail message as read — both in the local cache and via the API.

    Args:
      message_id : The id field from the message object (e.g. 'gmail:abc123')

    Returns success status.
    """
    ctx = await _timed_tool("mark_gmail_read", user_id=user_id)
    settings = get_settings()

    try:
        # Always update local cache
        await mark_read(message_id, user_id=user_id)

        api_success = False
        if user_id != "global":
            api_success = await mark_gmail_read_for_user(user_id=user_id, message_id=message_id)
        elif settings.gmail_enabled:
            client = get_gmail_client()
            api_success = client.mark_as_read(message_id)
        else:
            api_success = True   # demo: pretend API call succeeded

        summary = f"Marked {message_id} as read {'(live)' if settings.gmail_enabled else '(demo)'}"
        await ctx.done(summary)

        return {
            "tool": "mark_gmail_read",
            "success": api_success,
            "demo_mode": (user_id == "global" and not settings.gmail_enabled),
            "message_id": message_id,
            "message": f"Message {message_id} marked as read.",
        }

    except Exception as exc:
        await ctx.error(str(exc))
        logger.exception("mark_gmail_read failed")
        return {
            "tool": "mark_gmail_read",
            "success": False,
            "message_id": message_id,
            "message": f"Failed to mark as read: {exc}",
        }


async def summarize_gmail_thread(thread_id: str, user_id: str = "global") -> dict[str, Any]:
    """
    Fetch all messages in a Gmail thread and return them formatted
    so Claude can produce a natural-language summary.

    Args:
      thread_id : The thread_id field from a message object

    Returns a structured thread object with all messages.
    """
    ctx = await _timed_tool("summarize_gmail_thread", user_id=user_id)
    settings = get_settings()

    try:
        if user_id == "global" and not settings.gmail_enabled:
            # Demo: return the matching mock message as a single-message thread
            from clients.gmail_client import MOCK_EMAILS
            mock_msgs = [m for m in MOCK_EMAILS if m.get("thread_id") == thread_id]
            if not mock_msgs:
                mock_msgs = MOCK_EMAILS[:1]   # fallback to first mock

            thread_text = _format_thread_for_summary(mock_msgs)
            await ctx.done(f"Thread summary ready ({len(mock_msgs)} messages, demo)")

            return {
                "tool": "summarize_gmail_thread",
                "thread_id": thread_id,
                "message_count": len(mock_msgs),
                "demo_mode": True,
                "thread_text": thread_text,
                "messages": mock_msgs,
                "instruction": (
                    "Please summarize this email thread concisely, "
                    "highlighting key decisions, action items, and next steps."
                ),
            }

        native_thread = thread_id.replace("gmail:", "").split(":")[0]
        if user_id != "global":
            messages = await get_gmail_thread_for_user(user_id=user_id, thread_id=native_thread)
        else:
            client = get_gmail_client()
            messages = client.get_thread(native_thread)

        thread_text = _format_thread_for_summary(messages)
        await ctx.done(f"Thread fetched ({len(messages)} messages)")

        return {
            "tool": "summarize_gmail_thread",
            "thread_id": thread_id,
            "message_count": len(messages),
            "demo_mode": False,
            "thread_text": thread_text,
            "messages": messages,
            "instruction": (
                "Please summarize this email thread concisely, "
                "highlighting key decisions, action items, and next steps."
            ),
        }

    except Exception as exc:
        await ctx.error(str(exc))
        logger.exception("summarize_gmail_thread failed")
        return {
            "tool": "summarize_gmail_thread",
            "thread_id": thread_id,
            "success": False,
            "message": f"Failed to fetch thread: {exc}",
        }


# ── Formatting helpers ────────────────────────────────────────────────────────

def _format_thread_for_summary(messages: list[dict[str, Any]]) -> str:
    """Return a readable plain-text representation of a thread."""
    parts: list[str] = []
    for i, msg in enumerate(messages, 1):
        sender = msg.get("sender", "Unknown")
        email = msg.get("sender_email", "")
        ts = msg.get("timestamp", "")[:16].replace("T", " ")
        subject = msg.get("subject", "")
        body = msg.get("body") or msg.get("preview", "")

        header = f"── Message {i} ──"
        if subject:
            header += f"  [{subject}]"
        parts.append(
            f"{header}\n"
            f"From : {sender} <{email}>\n"
            f"Date : {ts}\n\n"
            f"{body.strip()}\n"
        )
    return "\n".join(parts)
