"""
oauth_google.py - Google OAuth web-flow helpers for per-user Gmail connect.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx

from config import get_settings

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GMAIL_PROFILE_URL = "https://gmail.googleapis.com/gmail/v1/users/me/profile"

GMAIL_OAUTH_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.modify",
]


def _load_google_client_from_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data.get("web") or data.get("installed") or {}


def get_google_oauth_config() -> tuple[str, str, str]:
    s = get_settings()
    client_id = s.google_oauth_client_id.strip()
    client_secret = s.google_oauth_client_secret.strip()
    redirect_uri = s.google_oauth_redirect_uri.strip()

    if not (client_id and client_secret):
        client = _load_google_client_from_file(s.gmail_credentials_path)
        client_id = client_id or str(client.get("client_id", "")).strip()
        client_secret = client_secret or str(client.get("client_secret", "")).strip()
        if not redirect_uri:
            uris = client.get("redirect_uris") or []
            if uris:
                redirect_uri = str(uris[0]).strip()

    if not redirect_uri:
        base = s.app_base_url.rstrip("/")
        redirect_uri = f"{base}/auth/google/callback"

    if not client_id or not client_secret:
        raise RuntimeError(
            "Google OAuth is not configured. Set GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET."
        )

    return client_id, client_secret, redirect_uri


def build_google_authorize_url(
    *,
    state: str,
    login_hint: str = "",
    scopes: list[str] | None = None,
) -> str:
    client_id, _, redirect_uri = get_google_oauth_config()
    query = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(scopes or GMAIL_OAUTH_SCOPES),
        "access_type": "offline",
        "include_granted_scopes": "true",
        "prompt": "consent",
        "state": state,
    }
    if login_hint:
        query["login_hint"] = login_hint
    return f"{GOOGLE_AUTH_URL}?{urlencode(query)}"


async def exchange_code_for_tokens(code: str) -> dict[str, Any]:
    client_id, client_secret, redirect_uri = get_google_oauth_config()
    payload = {
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(GOOGLE_TOKEN_URL, data=payload)
        resp.raise_for_status()
        return resp.json()


async def fetch_gmail_profile(access_token: str) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {access_token}"}
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(GMAIL_PROFILE_URL, headers=headers)
        resp.raise_for_status()
        return resp.json()
