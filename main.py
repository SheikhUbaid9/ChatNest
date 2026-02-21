from __future__ import annotations

import argparse
import asyncio
import logging

from fastapi import FastAPI
import uvicorn

from fastmcp import FastMCP

from config import get_settings
from database import init_db
from tools.gmail_tools import *
from tools.slack_tools import *
from tools.telegram_tools import *

logger = logging.getLogger(__name__)

# =========================================================
# ✅ REQUIRED FOR VERCEL — FastAPI instance must be global
# =========================================================
app = FastAPI(title="MCP Inbox API")

# =========================================================
# MCP SERVER SETUP (same as your original)
# =========================================================

mcp = FastMCP(
    name="MCP Inbox",
    instructions="Access Gmail, Slack, Telegram messages safely."
)

@mcp.tool()
async def gmail_get_unread(max_results: int = 20):
    return await get_gmail_unread(max_results=max_results)

@mcp.tool()
async def gmail_send_reply(message_id: str, thread_id: str, to: str, subject: str, body: str):
    return await send_gmail_reply(
        message_id=message_id,
        thread_id=thread_id,
        to=to,
        subject=subject,
        body=body,
    )

@mcp.tool()
async def gmail_mark_read(message_id: str):
    return await mark_gmail_read(message_id=message_id)

@mcp.tool()
async def gmail_summarize_thread(thread_id: str):
    return await summarize_gmail_thread(thread_id=thread_id)

@mcp.tool()
async def slack_get_messages(channel: str = "", limit: int = 20):
    return await get_slack_messages(channel=channel or None, limit=limit)

@mcp.tool()
async def slack_send_message(channel: str, text: str, thread_ts: str = ""):
    return await send_slack_message(
        channel=channel,
        text=text,
        thread_ts=thread_ts or None,
    )

@mcp.tool()
async def slack_summarize_thread(thread_id: str, channel: str = ""):
    return await summarize_slack_thread(thread_id=thread_id, channel=channel or None)

@mcp.tool()
async def telegram_get_messages(limit: int = 20):
    return await get_telegram_messages(limit=limit)

@mcp.tool()
async def telegram_send_reply(chat_id: str, text: str, message_id: str = ""):
    return await send_telegram_reply(
        chat_id=chat_id,
        text=text,
        message_id=message_id or None,
    )

@mcp.tool()
async def telegram_summarize_chat(chat_id: str = "", thread_id: str = "", limit: int = 20):
    return await summarize_telegram_chat(
        chat_id=chat_id or None,
        thread_id=thread_id or None,
        limit=limit,
    )

# =========================================================
# FASTAPI ROUTES (needed for Vercel health + testing)
# =========================================================

@app.on_event("startup")
async def startup():
    await init_db()
    logger.info("MCP Inbox started")

@app.get("/")
async def root():
    return {"status": "MCP Inbox running on Vercel 🚀"}

@app.get("/health")
async def health():
    return {"ok": True}

# =========================================================
# CLI MODE (keeps your original behaviour)
# =========================================================

def _parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--http", action="store_true")
    p.add_argument("--ui", action="store_true")
    return p.parse_args()

def main():
    args = _parse_args()
    settings = get_settings()

    asyncio.run(init_db())

    if args.http:
        mcp.run(transport="sse", host="0.0.0.0", port=settings.mcp_port)
    else:
        mcp.run(transport="stdio")

if __name__ == "__main__":
    main()