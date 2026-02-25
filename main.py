"""
main.py — FastMCP server entry point for MCP Inbox.

Registers all Gmail, Slack, and Telegram tools with FastMCP so
Claude.ai (or any MCP-compatible client) can call them directly.

Usage:
  # Run as MCP server (stdio transport — for Claude Desktop / Claude.ai)
  python main.py

  # Run as HTTP server (for testing with curl / Postman)
  python main.py --http

  # Run UI only
  python main.py --ui
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from fastmcp import FastMCP # pyright: ignore[reportMissingImports]

from config import get_settings
from database import init_db
from tools.gmail_tools import (
    get_gmail_unread,
    mark_gmail_read,
    send_gmail_reply,
    summarize_gmail_thread,
)
from tools.slack_tools import (
    get_slack_messages,
    send_slack_message,
    summarize_slack_thread,
)
from tools.telegram_tools import (
    get_telegram_messages,
    send_telegram_reply,
    summarize_telegram_chat,
)

logger = logging.getLogger(__name__)

# ── Build MCP server ──────────────────────────────────────────────────────────

mcp = FastMCP(
    name="MCP Inbox",
    instructions=(
        "MCP Inbox gives you access to Gmail, Slack, and Telegram messages. "
        "Use the tools below to read messages, send replies, and summarise threads. "
        "All tools work in Demo Mode (with realistic mock data) when API keys are "
        "not configured — so you can always call them safely.\n\n"
        "Platforms available: Gmail · Slack · Telegram\n"
        "Demo mode is active when credentials are not set in the .env file."
    ),
)

# ── Register Gmail tools ──────────────────────────────────────────────────────

@mcp.tool(
    description=(
        "Fetch the latest unread Gmail messages (up to 20). "
        "Returns sender, subject, preview, timestamp, and thread_id for each message. "
        "Works in Demo Mode when Gmail credentials are not configured."
    )
)
async def gmail_get_unread(max_results: int = 20) -> dict:
    """Get unread Gmail messages."""
    return await get_gmail_unread(max_results=max_results)


@mcp.tool(
    description=(
        "Send a reply to a Gmail thread. "
        "Requires the thread_id and sender_email from a message returned by gmail_get_unread. "
        "In Demo Mode the reply is simulated and not actually sent."
    )
)
async def gmail_send_reply(
    message_id: str,
    thread_id: str,
    to: str,
    subject: str,
    body: str,
) -> dict:
    """Reply to a Gmail thread."""
    return await send_gmail_reply(
        message_id=message_id,
        thread_id=thread_id,
        to=to,
        subject=subject,
        body=body,
    )


@mcp.tool(
    description=(
        "Mark a Gmail message as read. "
        "Pass the id field from a message returned by gmail_get_unread. "
        "Updates both the local cache and the Gmail API (when credentials are set)."
    )
)
async def gmail_mark_read(message_id: str) -> dict:
    """Mark a Gmail message as read."""
    return await mark_gmail_read(message_id=message_id)


@mcp.tool(
    description=(
        "Fetch all messages in a Gmail thread so you can summarise them. "
        "Pass the thread_id from any message. "
        "Returns the full thread text and an instruction prompt for summarisation."
    )
)
async def gmail_summarize_thread(thread_id: str) -> dict:
    """Summarise a Gmail thread."""
    return await summarize_gmail_thread(thread_id=thread_id)


# ── Register Slack tools ──────────────────────────────────────────────────────

@mcp.tool(
    description=(
        "Fetch recent Slack messages from all joined channels (or a specific channel). "
        "Returns sender, channel, message preview, and thread_id. "
        "Works in Demo Mode when the Slack bot token is not configured."
    )
)
async def slack_get_messages(
    channel: str = "",
    limit: int = 20,
) -> dict:
    """Get Slack messages from channels."""
    return await get_slack_messages(
        channel=channel or None,
        limit=limit,
    )


@mcp.tool(
    description=(
        "Send a message to a Slack channel or reply in an existing thread. "
        "Set thread_ts to the Slack timestamp of the parent message to reply in-thread. "
        "In Demo Mode the message is simulated and not actually sent."
    )
)
async def slack_send_message(
    channel: str,
    text: str,
    thread_ts: str = "",
) -> dict:
    """Post a Slack message or thread reply."""
    return await send_slack_message(
        channel=channel,
        text=text,
        thread_ts=thread_ts or None,
    )


@mcp.tool(
    description=(
        "Fetch all messages in a Slack thread so you can summarise them. "
        "Pass the thread_id from any message returned by slack_get_messages. "
        "Returns the full thread text and an instruction prompt for summarisation."
    )
)
async def slack_summarize_thread(
    thread_id: str,
    channel: str = "",
) -> dict:
    """Summarise a Slack thread."""
    return await summarize_slack_thread(
        thread_id=thread_id,
        channel=channel or None,
    )


# ── Register Telegram tools ───────────────────────────────────────────────────

@mcp.tool(
    description=(
        "Fetch pending Telegram messages from the bot inbox. "
        "Returns sender, chat name, message preview, and chat_id. "
        "Works in Demo Mode when the Telegram bot token is not configured."
    )
)
async def telegram_get_messages(limit: int = 20) -> dict:
    """Get pending Telegram messages."""
    return await get_telegram_messages(limit=limit)


@mcp.tool(
    description=(
        "Send a reply to a Telegram chat or DM. "
        "Pass the chat_id from a message returned by telegram_get_messages. "
        "Optionally pass the message_id to mark it as read in the local cache. "
        "In Demo Mode the reply is simulated and not actually sent."
    )
)
async def telegram_send_reply(
    chat_id: str,
    text: str,
    message_id: str = "",
) -> dict:
    """Send a Telegram reply."""
    return await send_telegram_reply(
        chat_id=chat_id,
        text=text,
        message_id=message_id or None,
    )


@mcp.tool(
    description=(
        "Fetch messages from a Telegram chat so you can summarise them. "
        "Pass either chat_id (numeric) or thread_id from a message object. "
        "Returns the full conversation text and an instruction prompt for summarisation."
    )
)
async def telegram_summarize_chat(
    chat_id: str = "",
    thread_id: str = "",
    limit: int = 20,
) -> dict:
    """Summarise a Telegram chat."""
    return await summarize_telegram_chat(
        chat_id=chat_id or None,
        thread_id=thread_id or None,
        limit=limit,
    )


# ── Startup ───────────────────────────────────────────────────────────────────

async def _startup() -> None:
    """Initialise DB and log platform status."""
    settings = get_settings()
    await init_db()

    logger.info("═" * 50)
    logger.info("  MCP Inbox — starting up")
    logger.info("═" * 50)
    logger.info("  Gmail    : %s", "✅ live" if settings.gmail_enabled    else "🟡 demo")
    logger.info("  Slack    : %s", "✅ live" if settings.slack_enabled    else "🟡 demo")
    logger.info("  Telegram : %s", "✅ live" if settings.telegram_enabled else "🟡 demo")
    logger.info("  Demo mode: %s", settings.demo_mode)
    logger.info("  Tools registered: %d", 9)
    logger.info("═" * 50)


# ── CLI entry point ───────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="MCP Inbox server")
    p.add_argument(
        "--http",
        action="store_true",
        help="Run as HTTP/SSE MCP server instead of stdio",
    )
    p.add_argument(
        "--ui",
        action="store_true",
        help="Launch the FastAPI UI server only (no MCP transport)",
    )
    p.add_argument(
        "--host",
        default=None,
        help="Host override (default: from .env UI_HOST)",
    )
    p.add_argument(
        "--port",
        type=int,
        default=None,
        help="Port override (default: from .env UI_PORT / MCP_PORT)",
    )
    return p.parse_args()


def main() -> None:
    args = _parse_args()
    settings = get_settings()

    # Always run startup (init DB etc.)
    asyncio.run(_startup())

    if args.ui:
        # Launch FastAPI UI server
        import uvicorn
        from ui.server import app as ui_app

        host = args.host or settings.ui_host
        port = args.port or settings.ui_port
        logger.info("Starting UI server at http://%s:%d", host, port)
        uvicorn.run(ui_app, host=host, port=port, log_level=settings.log_level.lower())

    elif args.http:
        # Run MCP over HTTP/SSE transport (useful for testing)
        host = args.host or "0.0.0.0"
        port = args.port or settings.mcp_port
        logger.info("Starting MCP HTTP server at http://%s:%d", host, port)
        mcp.run(transport="sse", host=host, port=port)

    else:
        # Default: stdio transport for Claude Desktop / Claude.ai
        logger.info("Starting MCP server (stdio transport)")
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
