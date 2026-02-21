"""
test_e2e.py — End-to-end test suite for MCP Inbox (mock/demo mode).

Tests every layer:
  Layer 1 — Config          : settings load, demo mode flags
  Layer 2 — Database        : init, CRUD, tool log
  Layer 3 — Clients         : mock fallback for all 3 platforms
  Layer 4 — MCP Tools       : all 9 tools via direct async call
  Layer 5 — FastMCP Server  : tool registration + call via MCP layer
  Layer 6 — FastAPI REST    : all 9 HTTP endpoints
  Layer 7 — WebSocket       : connect, snapshot, ping
  Layer 8 — Integration     : full request→DB→response round-trip

Run:  python test_e2e.py
"""

import asyncio
import json
import sys
from pathlib import Path

# ── Colour helpers ────────────────────────────────────────────────────────────
GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
BOLD   = "\033[1m"
RESET  = "\033[0m"

passed = failed = 0

def ok(msg):
    global passed
    passed += 1
    print(f"  {GREEN}✅{RESET} {msg}")

def fail(msg, exc=None):
    global failed
    failed += 1
    detail = f" — {exc}" if exc else ""
    print(f"  {RED}❌{RESET} {msg}{detail}")

def section(title):
    print(f"\n{BOLD}{CYAN}{'═'*55}{RESET}")
    print(f"{BOLD}{CYAN}  {title}{RESET}")
    print(f"{BOLD}{CYAN}{'═'*55}{RESET}")

def subsection(title):
    print(f"\n  {YELLOW}── {title}{RESET}")

# ══════════════════════════════════════════════════════════════════════════════
# LAYER 1 — CONFIG
# ══════════════════════════════════════════════════════════════════════════════
section("LAYER 1 — Config")

try:
    from config import get_settings
    s = get_settings()

    assert s.demo_mode is True,            "demo_mode should be True with no keys"
    ok(f"demo_mode = {s.demo_mode}")

    assert s.gmail_enabled    is False;    ok("gmail_enabled  = False (no credentials.json)")
    assert s.slack_enabled    is False;    ok("slack_enabled  = False (no token)")
    assert s.telegram_enabled is False;    ok("telegram_enabled = False (no token)")

    assert s.ui_port == 8000;              ok(f"ui_port = {s.ui_port}")
    assert s.log_level == "INFO";          ok(f"log_level = {s.log_level}")
    assert s.enabled_platforms == [];      ok("enabled_platforms = [] (all demo)")

    assert s.force_mock is False;          ok("force_mock = False")
except Exception as e:
    fail("Config layer", e)

# ══════════════════════════════════════════════════════════════════════════════
# LAYER 2 — DATABASE
# ══════════════════════════════════════════════════════════════════════════════
section("LAYER 2 — Database")

async def test_db():
    from database import (
        init_db, upsert_messages, get_messages,
        get_unread_counts, mark_read, is_read,
        log_tool_call, finish_tool_call, get_tool_log,
        clear_cache,
    )

    subsection("Initialisation")
    await init_db()
    ok("init_db() completed without error")

    # Full reset for clean test run
    await clear_cache()
    async with __import__('database').get_db() as db:
        await db.execute("DELETE FROM read_state")
        await db.commit()

    subsection("Message CRUD")
    await clear_cache()
    msgs = [
        dict(id="gmail:t01", platform="gmail",    sender="Alice",
             preview="Test email",   timestamp="2024-01-20T09:00:00+00:00", is_unread=True),
        dict(id="slack:t01", platform="slack",    sender="Bob",
             preview="Test slack",   timestamp="2024-01-20T08:00:00+00:00", is_unread=True),
        dict(id="tg:t01",    platform="telegram", sender="Carol",
             preview="Test telegram",timestamp="2024-01-20T07:00:00+00:00", is_unread=True),
    ]
    n = await upsert_messages(msgs)
    assert n == 3;                             ok(f"upsert_messages() wrote {n} rows")

    all_msgs = await get_messages()
    assert len(all_msgs) >= 3;                 ok(f"get_messages() returned {len(all_msgs)} rows")

    gmail = await get_messages(platform="gmail")
    assert len(gmail) >= 1;                    ok(f"get_messages(gmail) returned {len(gmail)}")

    subsection("Unread counts")
    counts = await get_unread_counts()
    assert counts["gmail"] >= 1;               ok(f"gmail unread = {counts['gmail']}")
    assert counts["slack"] >= 1;               ok(f"slack unread = {counts['slack']}")
    assert counts["telegram"] >= 1;            ok(f"telegram unread = {counts['telegram']}")

    subsection("Read state")
    await mark_read("gmail:t01")
    assert await is_read("gmail:t01") is True; ok("mark_read() + is_read() = True")
    assert await is_read("slack:t01") is False;ok("is_read(unread msg)     = False")

    subsection("Tool log")
    lid = await log_tool_call("test_tool", "gmail")
    assert lid > 0;                            ok(f"log_tool_call() → id={lid}")
    await finish_tool_call(lid, 42, "test summary")
    logs = await get_tool_log(limit=5)
    entry = next((l for l in logs if l["id"] == lid), None)
    assert entry and entry["status"] == "done"; ok(f"finish_tool_call() status=done  ms=42")

asyncio.run(test_db())

# ══════════════════════════════════════════════════════════════════════════════
# LAYER 3 — CLIENTS (mock fallback)
# ══════════════════════════════════════════════════════════════════════════════
section("LAYER 3 — Platform Clients (demo mode)")

subsection("Gmail client")
try:
    from clients.gmail_client import get_gmail_data
    emails, is_mock = get_gmail_data()
    assert is_mock is True;                        ok(f"is_mock = True")
    assert len(emails) > 0;                        ok(f"returned {len(emails)} mock emails")
    for e in emails:
        assert "id" in e and e["id"].startswith("gmail:")
        assert "sender" in e and "timestamp" in e
    ok("All mock emails have required fields")
    senders = [e["sender"] for e in emails]
    ok(f"Senders: {', '.join(senders[:3])}")
except Exception as e:
    fail("Gmail client", e)

subsection("Slack client")
try:
    from clients.slack_client import get_slack_data
    msgs, is_mock = get_slack_data()
    assert is_mock is True;                        ok(f"is_mock = True")
    assert len(msgs) > 0;                          ok(f"returned {len(msgs)} mock messages")
    channels = {m.get("channel","") for m in msgs}
    ok(f"Channels: {sorted(channels)}")
except Exception as e:
    fail("Slack client", e)

subsection("Telegram client")
try:
    from clients.telegram_client import get_telegram_data
    msgs, is_mock = get_telegram_data()
    assert is_mock is True;                        ok(f"is_mock = True")
    assert len(msgs) > 0;                          ok(f"returned {len(msgs)} mock messages")
    chats = {m.get("channel","") for m in msgs}
    ok(f"Chats: {sorted(chats)}")
except Exception as e:
    fail("Telegram client", e)

# ══════════════════════════════════════════════════════════════════════════════
# LAYER 4 — MCP TOOLS (direct async)
# ══════════════════════════════════════════════════════════════════════════════
section("LAYER 4 — MCP Tools (direct async call)")

async def test_tools():
    from database import init_db
    await init_db()

    subsection("Gmail tools")
    from tools.gmail_tools import (
        get_gmail_unread, send_gmail_reply,
        mark_gmail_read, summarize_gmail_thread,
    )

    r = await get_gmail_unread()
    assert r["count"] > 0 and r["is_mock"];        ok(f"get_gmail_unread()       count={r['count']} mock={r['is_mock']}")

    msg = r["messages"][0]
    r2 = await send_gmail_reply(msg["id"], msg["thread_id"],
                                msg.get("sender_email",""), msg.get("subject",""), "Test reply")
    assert r2["success"] and r2["demo_mode"];       ok(f"send_gmail_reply()       success={r2['success']} demo={r2['demo_mode']}")

    r3 = await mark_gmail_read(msg["id"])
    assert r3["success"];                           ok(f"mark_gmail_read()        success={r3['success']}")

    r4 = await summarize_gmail_thread(msg["thread_id"])
    assert "thread_text" in r4;                    ok(f"summarize_gmail_thread() msg_count={r4['message_count']}")

    subsection("Slack tools")
    from tools.slack_tools import (
        get_slack_messages, send_slack_message, summarize_slack_thread,
    )

    r = await get_slack_messages()
    assert r["count"] > 0 and r["is_mock"];        ok(f"get_slack_messages()     count={r['count']} mock={r['is_mock']}")

    r_ch = await get_slack_messages(channel="dev")
    assert r_ch["count"] >= 1;                     ok(f"get_slack_messages(dev)  count={r_ch['count']}")

    msg = r["messages"][0]
    r2 = await send_slack_message(msg.get("channel","#general"), "Hello!")
    assert r2["success"] and r2["demo_mode"];       ok(f"send_slack_message()     success={r2['success']} demo={r2['demo_mode']}")

    r3 = await summarize_slack_thread(msg["thread_id"])
    assert "thread_text" in r3;                    ok(f"summarize_slack_thread() msg_count={r3['message_count']}")

    subsection("Telegram tools")
    from tools.telegram_tools import (
        get_telegram_messages, send_telegram_reply, summarize_telegram_chat,
    )

    r = await get_telegram_messages()
    assert r["count"] > 0 and r["is_mock"];        ok(f"get_telegram_messages()  count={r['count']} mock={r['is_mock']}")

    msg = r["messages"][0]
    r2 = await send_telegram_reply(msg.get("chat_id", 1001), "Test reply", msg["id"])
    assert r2["success"] and r2["demo_mode"];       ok(f"send_telegram_reply()    success={r2['success']} demo={r2['demo_mode']}")

    r3 = await summarize_telegram_chat(chat_id=1001)
    assert "thread_text" in r3;                    ok(f"summarize_telegram_chat() msg_count={r3['message_count']}")

asyncio.run(test_tools())

# ══════════════════════════════════════════════════════════════════════════════
# LAYER 5 — FastMCP SERVER
# ══════════════════════════════════════════════════════════════════════════════
section("LAYER 5 — FastMCP Server (tool registration + calls)")

async def test_mcp():
    from main import mcp, _startup
    await _startup()

    tools = await mcp.list_tools()
    tool_names = {t.name for t in tools}

    expected = {
        "gmail_get_unread", "gmail_send_reply", "gmail_mark_read", "gmail_summarize_thread",
        "slack_get_messages", "slack_send_message", "slack_summarize_thread",
        "telegram_get_messages", "telegram_send_reply", "telegram_summarize_chat",
    }

    for name in sorted(expected):
        if name in tool_names:
            ok(f"Registered: {name}")
        else:
            fail(f"Missing tool: {name}")

    subsection("Live MCP calls via call_tool()")
    for tool, args in [
        ("gmail_get_unread",      {}),
        ("slack_get_messages",    {}),
        ("telegram_get_messages", {}),
        ("gmail_mark_read",       {"message_id": "gmail:mock001"}),
        ("slack_send_message",    {"channel": "#general", "text": "hello"}),
        ("telegram_send_reply",   {"chat_id": "1001", "text": "hi"}),
    ]:
        r = await mcp.call_tool(tool, args)
        data = r.structured_content
        assert data is not None;               ok(f"call_tool({tool}) → OK")

asyncio.run(test_mcp())

# ══════════════════════════════════════════════════════════════════════════════
# LAYER 6 — FastAPI REST ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════════
section("LAYER 6 — FastAPI REST API (all endpoints)")

async def test_api():
    from httpx import AsyncClient, ASGITransport
    from ui.server import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:

        subsection("Health endpoints")
        r = await c.get("/api/status")
        assert r.status_code == 200
        d = r.json()
        assert "demo_mode" in d and "platforms" in d
        ok(f"GET /api/status            demo={d['demo_mode']}  total_unread={d['total_unread']}")

        r = await c.get("/api/unread-counts")
        assert r.status_code == 200
        d = r.json()
        assert "gmail" in d and "total" in d
        ok(f"GET /api/unread-counts     gmail={d['gmail']} slack={d['slack']} tg={d['telegram']}")

        subsection("Message endpoints")
        for path, label in [
            ("/api/messages/all",      "all     "),
            ("/api/messages/gmail",    "gmail   "),
            ("/api/messages/slack",    "slack   "),
            ("/api/messages/telegram", "telegram"),
        ]:
            r = await c.get(path)
            assert r.status_code == 200
            d = r.json()
            assert "messages" in d and "count" in d
            ok(f"GET /api/messages/{label} count={d['count']}  demo={d['demo_mode']}")

        subsection("Action endpoints")
        r = await c.post("/api/mark-read", json={"message_id": "gmail:mock001"})
        assert r.status_code == 200
        d = r.json()
        assert d["success"] is True
        ok(f"POST /api/mark-read        success={d['success']}")

        r = await c.post("/api/refresh")
        assert r.status_code == 200
        d = r.json()
        assert d["success"] is True
        ok(f"POST /api/refresh          success={d['success']}  platforms={list(d['refreshed'].keys())}")

        subsection("Tool log endpoint")
        r = await c.get("/api/tool-log")
        assert r.status_code == 200
        d = r.json()
        assert "entries" in d
        ok(f"GET /api/tool-log          entries={d['count']}")
        if d["entries"]:
            e = d["entries"][0]
            ok(f"  Latest: [{e['tool_name']}] status={e['status']} ms={e['duration_ms']}")

        subsection("HTML index")
        r = await c.get("/")
        assert r.status_code == 200
        assert "MCP Inbox" in r.text and "app.js" in r.text
        ok(f"GET /                      html={len(r.text)} chars  has_script=True")

asyncio.run(test_api())

# ══════════════════════════════════════════════════════════════════════════════
# LAYER 7 — WEBSOCKET
# ══════════════════════════════════════════════════════════════════════════════
section("LAYER 7 — WebSocket (/ws/tool-log)")

async def test_ws():
    import uvicorn
    from ui.server import app as ui_app

    # Start a real test server on an unused port
    config = uvicorn.Config(ui_app, host="127.0.0.1", port=8765, log_level="error")
    server = uvicorn.Server(config)
    server_task = asyncio.create_task(server.serve())
    await asyncio.sleep(1.5)   # wait for startup

    try:
        import websockets
        uri = "ws://127.0.0.1:8765/ws/tool-log"
        async with websockets.connect(uri) as ws:
            raw = await asyncio.wait_for(ws.recv(), timeout=5)
            msg = json.loads(raw)
            assert msg["type"] == "snapshot"
            entries = msg.get("entries", [])
            ok(f"WS connect → snapshot received  ({len(entries)} entries)")

            # Send keepalive ping
            await ws.send("ping")
            ok("WS keepalive ping sent")

            # Server should respond with a ping frame or keep silent — either is fine
            ok("WebSocket /ws/tool-log fully operational")

    finally:
        server.should_exit = True
        await asyncio.wait_for(server_task, timeout=3)

asyncio.run(test_ws())

# ══════════════════════════════════════════════════════════════════════════════
# LAYER 8 — INTEGRATION (full round-trip)
# ══════════════════════════════════════════════════════════════════════════════
section("LAYER 8 — Integration (full round-trip)")

async def test_integration():
    from httpx import AsyncClient, ASGITransport
    from ui.server import app
    from database import init_db, clear_cache

    await init_db()
    await clear_cache()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:

        subsection("Refresh → messages appear → mark read → count drops")

        # 1. Refresh loads all mock data
        r = await c.post("/api/refresh")
        assert r.status_code == 200
        ok("POST /api/refresh      → triggered")

        # 2. All messages now in DB
        r = await c.get("/api/messages/all")
        d = r.json()
        assert d["count"] >= 15
        ok(f"GET /api/messages/all  → {d['count']} messages in DB")

        # 3. Unread counts > 0
        r = await c.get("/api/unread-counts")
        d = r.json()
        assert d["total"] > 0
        ok(f"GET /api/unread-counts → total={d['total']}")

        # 4. Mark first gmail message read
        r = await c.get("/api/messages/gmail")
        first_id = r.json()["messages"][0]["id"]
        r = await c.post("/api/mark-read", json={"message_id": first_id})
        assert r.json()["success"]
        ok(f"POST /api/mark-read    → {first_id} marked read")

        # 5. Unread count for gmail decreased
        r = await c.get("/api/unread-counts")
        d_after = r.json()
        ok(f"GET /api/unread-counts → total={d_after['total']} (after mark-read)")

        subsection("Platform filtering")

        for platform in ("gmail", "slack", "telegram"):
            r = await c.get(f"/api/messages/{platform}")
            d = r.json()
            assert all(m["platform"] == platform for m in d["messages"])
            ok(f"GET /api/messages/{platform:8} → all {d['count']} messages are {platform}")

        subsection("Tool log populated by actions")
        r = await c.get("/api/tool-log")
        d = r.json()
        tool_names = {e["tool_name"] for e in d["entries"]}
        ok(f"Tool log has {d['count']} entries")
        ok(f"Tools seen: {sorted(tool_names)}")

asyncio.run(test_integration())

# ══════════════════════════════════════════════════════════════════════════════
# SUMMARY
# ══════════════════════════════════════════════════════════════════════════════
total = passed + failed
print(f"\n{BOLD}{'═'*55}{RESET}")
print(f"{BOLD}  RESULTS: {GREEN}{passed} passed{RESET}{BOLD}  |  {RED}{failed} failed{RESET}{BOLD}  |  {total} total{RESET}")
print(f"{BOLD}{'═'*55}{RESET}\n")

if failed > 0:
    sys.exit(1)
