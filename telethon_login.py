"""
telethon_login.py — One-time interactive login for Telethon personal account.

Run this ONCE to authenticate your personal Telegram account:
    python telethon_login.py

You'll be asked for your phone number and a verification code sent by Telegram.
After login, a session file is saved — future app starts connect automatically.

Prerequisites:
  1. Go to https://my.telegram.org/apps
  2. Log in with your phone number
  3. Click "API development tools" → Create app (or use existing)
  4. Copy api_id and api_hash into .env:
       TELEGRAM_API_ID=12345678
       TELEGRAM_API_HASH=abcdef1234567890abcdef1234567890
  5. Run this script
"""

import asyncio
import os
import sys

# Make sure we can import from the mcp-inbox package
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


async def main() -> None:
    from config import get_settings
    from clients.telethon_client import _parse_proxy

    s = get_settings()

    api_id_str = s.telegram_api_id.strip()
    api_hash = s.telegram_api_hash.strip()

    if not api_id_str or not api_hash:
        print(
            "\n❌  TELEGRAM_API_ID and TELEGRAM_API_HASH are not set in .env\n\n"
            "   Steps:\n"
            "   1. Visit https://my.telegram.org/apps\n"
            "   2. Log in → 'API development tools' → Create app\n"
            "   3. Copy api_id (number) and api_hash (hex) into .env\n"
            "   4. Re-run this script\n"
        )
        sys.exit(1)

    try:
        api_id = int(api_id_str)
    except ValueError:
        print(f"❌  TELEGRAM_API_ID must be a number, got: {api_id_str!r}")
        sys.exit(1)

    session_path = str(s.telegram_session_path)
    proxy = _parse_proxy(s.telegram_proxy_url)

    print(f"\n📱  Telethon Personal Account Login")
    print(f"    Session: {session_path}")
    if proxy:
        print(f"    Proxy  : {s.telegram_proxy_url}")
    print()

    from telethon import TelegramClient  # type: ignore
    from telethon.errors import SessionPasswordNeededError  # type: ignore

    kwargs = {}
    if proxy:
        kwargs["proxy"] = proxy

    client = TelegramClient(session_path, api_id, api_hash, **kwargs)
    await client.connect()

    if await client.is_user_authorized():
        me = await client.get_me()
        name = f"{me.first_name or ''} {me.last_name or ''}".strip() or me.username
        print(f"✅  Already logged in as: {name} (@{me.username})")
        print(f"    Session file: {session_path}")
        print(f"\n    You can now start the server:  python -m uvicorn ui.server:app --port 8000")
        await client.disconnect()
        return

    # Need to authenticate
    phone = input("Enter your phone number (with country code, e.g. +923001234567): ").strip()
    await client.send_code_request(phone)

    code = input("Enter the OTP code Telegram sent you: ").strip()

    try:
        await client.sign_in(phone, code)
    except SessionPasswordNeededError:
        # 2FA is enabled
        password = input("Two-step verification is enabled. Enter your password: ").strip()
        await client.sign_in(password=password)

    me = await client.get_me()
    name = f"{me.first_name or ''} {me.last_name or ''}".strip() or me.username
    print(f"\n✅  Logged in as: {name} (@{me.username})")
    print(f"    Session saved to: {session_path}")
    print(f"\n    You can now start the server:  python -m uvicorn ui.server:app --port 8000")

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
