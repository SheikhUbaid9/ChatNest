"""
test_real_apis.py — Live API connection tests for MCP Inbox.

Tests only the platforms whose credentials are configured in .env.
Skips gracefully when a platform is in demo mode.

Usage:
  python test_real_apis.py              # test all configured platforms
  python test_real_apis.py --gmail      # test Gmail only
  python test_real_apis.py --slack      # test Slack only
  python test_real_apis.py --telegram   # test Telegram only
"""

from __future__ import annotations

import argparse
import asyncio
import sys

# ── Colour helpers ────────────────────────────────────────────────────────────
GREEN  = "\033[92m"
YELLOW = "\033[93m"
RED    = "\033[91m"
CYAN   = "\033[96m"
BOLD   = "\033[1m"
RESET  = "\033[0m"

passed = failed = skipped = 0

def ok(msg):
    global passed; passed += 1
    print(f"  {GREEN}✅{RESET} {msg}")

def skip(msg):
    global skipped; skipped += 1
    print(f"  {YELLOW}⏭  {RESET} {msg}")

def fail(msg, exc=None):
    global failed; failed += 1
    detail = f"\n     {RED}{exc}{RESET}" if exc else ""
    print(f"  {RED}❌{RESET} {msg}{detail}")

def section(title):
    print(f"\n{BOLD}{CYAN}{'═'*55}{RESET}")
    print(f"{BOLD}{CYAN}  {title}{RESET}")
    print(f"{BOLD}{CYAN}{'═'*55}{RESET}")

def subsection(title):
    print(f"\n  {YELLOW}── {title}{RESET}")


# ══════════════════════════════════════════════════════════════════════════════
# GMAIL
# ══════════════════════════════════════════════════════════════════════════════

def test_gmail():
    section("GMAIL — Live API Test")

    from config import get_settings
    s = get_settings()

    if not s.gmail_enabled:
        skip("Gmail not configured — set GMAIL_CREDENTIALS_PATH in .env")
        skip("Run: python test_real_apis.py  (after setting up credentials.json)")
        return

    subsection("OAuth2 connection")
    try:
        from clients.gmail_client import get_gmail_client
        client = get_gmail_client()
        service = client._build_service()
        ok("OAuth2 token valid / refreshed successfully")
    except Exception as e:
        fail("OAuth2 authentication failed", e)
        return

    subsection("Fetch unread emails")
    try:
        from clients.gmail_client import get_gmail_data
        emails, is_mock = get_gmail_data(max_results=5)
        assert not is_mock, "Still returning mock data despite gmail_enabled=True"
        ok(f"Fetched {len(emails)} real unread email(s)")
        for e in emails[:3]:
            ok(f"  [{e['sender']}] {e.get('subject','(no subject)')[:60]}")
    except Exception as e:
        fail("get_gmail_data() failed", e)
        return

    subsection("MCP tool: gmail_get_unread")
    try:
        result = asyncio.run(_run_tool_gmail())
        assert result["count"] >= 0
        assert result["is_mock"] is False
        ok(f"gmail_get_unread() → count={result['count']}  is_mock={result['is_mock']}")
    except Exception as e:
        fail("gmail_get_unread MCP tool failed", e)

    subsection("Mark first email read (optional — non-destructive test)")
    try:
        from clients.gmail_client import get_gmail_data
        emails, _ = get_gmail_data(max_results=1)
        if emails:
            from tools.gmail_tools import mark_gmail_read
            result = asyncio.run(mark_gmail_read(emails[0]["id"]))
            assert result["success"]
            ok(f"mark_gmail_read({emails[0]['id'][:30]}…) → success")
        else:
            skip("No unread emails to mark read")
    except Exception as e:
        fail("mark_gmail_read failed", e)


async def _run_tool_gmail():
    from database import init_db
    await init_db()
    from tools.gmail_tools import get_gmail_unread
    return await get_gmail_unread(max_results=5)


# ══════════════════════════════════════════════════════════════════════════════
# SLACK
# ══════════════════════════════════════════════════════════════════════════════

def test_slack():
    section("SLACK — Live API Test")

    from config import get_settings
    s = get_settings()

    if not s.slack_enabled:
        skip("Slack not configured — set SLACK_BOT_TOKEN in .env")
        return

    subsection("Bot token authentication")
    try:
        from slack_sdk import WebClient
        client = WebClient(token=s.slack_bot_token)
        resp = client.auth_test()
        assert resp["ok"]
        ok(f"Authenticated as: {resp['user']} on {resp['team']}")
    except Exception as e:
        fail("Slack auth.test failed", e)
        return

    subsection("List joined channels")
    try:
        from slack_sdk import WebClient
        client = WebClient(token=s.slack_bot_token)
        resp = client.conversations_list(types="public_channel,private_channel", limit=20)
        channels = resp.get("channels", [])
        ok(f"Bot is in {len(channels)} channel(s)")
        for ch in channels[:5]:
            ok(f"  #{ch['name']}")
    except Exception as e:
        fail("conversations.list failed", e)

    subsection("Fetch messages")
    try:
        from clients.slack_client import get_slack_data
        msgs, is_mock = get_slack_data(limit=5)
        assert not is_mock
        ok(f"Fetched {len(msgs)} real message(s)")
        for m in msgs[:3]:
            ok(f"  [{m.get('channel','?')}] {m['sender']}: {m['preview'][:50]}")
    except Exception as e:
        fail("get_slack_data() failed", e)

    subsection("MCP tool: slack_get_messages")
    try:
        result = asyncio.run(_run_tool_slack())
        assert result["is_mock"] is False
        ok(f"slack_get_messages() → count={result['count']}  is_mock={result['is_mock']}")
    except Exception as e:
        fail("slack_get_messages MCP tool failed", e)

    subsection("Send test message (to default channel)")
    try:
        from config import get_settings
        ch = get_settings().slack_default_channel
        result = asyncio.run(_send_slack_test(ch))
        assert result["success"]
        ok(f"Test message sent to #{ch}")
    except Exception as e:
        fail("send_slack_message failed", e)


async def _run_tool_slack():
    from database import init_db
    await init_db()
    from tools.slack_tools import get_slack_messages
    return await get_slack_messages(limit=5)


async def _send_slack_test(channel: str):
    from database import init_db
    await init_db()
    from tools.slack_tools import send_slack_message
    return await send_slack_message(
        channel=channel,
        text="🤖 MCP Inbox — live API test ping (you can delete this)",
    )


# ══════════════════════════════════════════════════════════════════════════════
# TELEGRAM
# ══════════════════════════════════════════════════════════════════════════════

def test_telegram():
    section("TELEGRAM — Live API Test")

    from config import get_settings
    s = get_settings()

    if not s.telegram_enabled:
        skip("Telegram not configured — set TELEGRAM_BOT_TOKEN in .env")
        return

    subsection("Bot token authentication")
    try:
        result = asyncio.run(_tg_get_me())
        ok(f"Bot authenticated: @{result.get('username','?')}  (id={result.get('id')})")
        ok(f"Bot name: {result.get('first_name','?')}")
    except Exception as e:
        fail("getMe failed", e)
        return

    subsection("Fetch pending updates (messages sent to bot)")
    try:
        from clients.telegram_client import get_telegram_data
        msgs, is_mock = get_telegram_data(limit=10)
        assert not is_mock
        ok(f"Fetched {len(msgs)} real message(s) from bot inbox")
        if msgs:
            for m in msgs[:3]:
                ok(f"  [{m.get('channel','DM')}] {m['sender']}: {m['preview'][:50]}")
        else:
            skip("No pending messages — send a message to your bot first")
    except Exception as e:
        fail("get_telegram_data() failed", e)

    subsection("MCP tool: telegram_get_messages")
    try:
        result = asyncio.run(_run_tool_telegram())
        assert result["is_mock"] is False
        ok(f"telegram_get_messages() → count={result['count']}  is_mock={result['is_mock']}")
    except Exception as e:
        fail("telegram_get_messages MCP tool failed", e)


async def _tg_get_me():
    from telegram import Bot
    bot = Bot(token=__import__('config').get_settings().telegram_bot_token)
    me = await bot.get_me()
    return me.to_dict()


async def _run_tool_telegram():
    from database import init_db
    await init_db()
    from tools.telegram_tools import get_telegram_messages
    return await get_telegram_messages(limit=10)


# ══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="MCP Inbox — live API tests")
    parser.add_argument("--gmail",    action="store_true")
    parser.add_argument("--slack",    action="store_true")
    parser.add_argument("--telegram", action="store_true")
    args = parser.parse_args()

    run_all = not (args.gmail or args.slack or args.telegram)

    if run_all or args.gmail:    test_gmail()
    if run_all or args.slack:    test_slack()
    if run_all or args.telegram: test_telegram()

    total = passed + failed + skipped
    print(f"\n{BOLD}{'═'*55}{RESET}")
    print(
        f"{BOLD}  RESULTS: "
        f"{GREEN}{passed} passed{RESET}{BOLD}  |  "
        f"{RED}{failed} failed{RESET}{BOLD}  |  "
        f"{YELLOW}{skipped} skipped{RESET}{BOLD}  |  "
        f"{total} total{RESET}"
    )
    print(f"{BOLD}{'═'*55}{RESET}\n")

    if failed > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
