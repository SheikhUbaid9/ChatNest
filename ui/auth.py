"""
ui/auth.py - Session and user auth helpers for ChatNest UI.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Request, Response, status

from config import get_settings
from database import get_user_by_session


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def display_name_from_email(email: str) -> str:
    local = normalize_email(email).split("@")[0]
    if not local:
        return "User"
    parts = [p for p in local.replace(".", " ").replace("_", " ").split() if p]
    if not parts:
        return "User"
    return " ".join(p[:1].upper() + p[1:].lower() for p in parts[:2])


def _cookie_secure(request: Request) -> bool:
    xf_proto = request.headers.get("x-forwarded-proto", "")
    if xf_proto:
        return xf_proto.lower().startswith("https")
    return request.url.scheme == "https"


def set_session_cookie(response: Response, request: Request, session_id: str) -> None:
    s = get_settings()
    response.set_cookie(
        key=s.auth_session_cookie_name,
        value=session_id,
        max_age=s.auth_session_ttl_hours * 3600,
        httponly=True,
        samesite="lax",
        secure=_cookie_secure(request),
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    s = get_settings()
    response.delete_cookie(
        key=s.auth_session_cookie_name,
        path="/",
    )


def session_id_from_request(request: Request) -> str:
    s = get_settings()
    return request.cookies.get(s.auth_session_cookie_name, "").strip()


async def get_current_user_optional(request: Request) -> dict[str, Any] | None:
    sid = session_id_from_request(request)
    if not sid:
        return None
    return await get_user_by_session(sid, touch=True)


async def require_current_user(request: Request) -> dict[str, Any]:
    user = await get_current_user_optional(request)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
        )
    return user
