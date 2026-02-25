"""
oauth_slack.py - Slack OAuth helpers for per-user connect flow.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

import httpx

from config import get_settings

SLACK_AUTHORIZE_URL = "https://slack.com/oauth/v2/authorize"
SLACK_TOKEN_URL = "https://slack.com/api/oauth.v2.access"


def get_slack_oauth_config() -> tuple[str, str, str, list[str]]:
    s = get_settings()
    client_id = s.slack_oauth_client_id.strip()
    client_secret = s.slack_oauth_client_secret.strip()
    redirect_uri = s.slack_oauth_redirect_uri.strip() or f"{s.app_base_url.rstrip('/')}/auth/slack/callback"
    scopes = [x.strip() for x in s.slack_oauth_scopes.split(",") if x.strip()]

    if not client_id or not client_secret:
        raise RuntimeError(
            "Slack OAuth is not configured. Set SLACK_OAUTH_CLIENT_ID and SLACK_OAUTH_CLIENT_SECRET."
        )
    return client_id, client_secret, redirect_uri, scopes


def build_slack_authorize_url(*, state: str, scopes: list[str] | None = None) -> str:
    client_id, _, redirect_uri, default_scopes = get_slack_oauth_config()
    query = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": ",".join(scopes or default_scopes),
        "state": state,
        "granular_bot_scope": "1",
    }
    return f"{SLACK_AUTHORIZE_URL}?{urlencode(query)}"


async def exchange_slack_code(code: str) -> dict[str, Any]:
    client_id, client_secret, redirect_uri, _ = get_slack_oauth_config()
    payload = {
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "redirect_uri": redirect_uri,
    }
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(SLACK_TOKEN_URL, data=payload)
        resp.raise_for_status()
        data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Slack OAuth failed: {data.get('error', 'unknown_error')}")
    return data
