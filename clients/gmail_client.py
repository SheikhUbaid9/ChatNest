"""
clients/gmail_client.py — Gmail API client with OAuth2 + mock fallback.

Real mode  : Uses Google OAuth2 credentials to read/send Gmail via the
             googleapis REST client.
Demo mode  : Returns realistic mock data when credentials.json is absent
             or FORCE_MOCK=true.
"""

from __future__ import annotations

import base64
import email as email_lib
import logging
import re
from datetime import datetime, timezone, timedelta
from email.mime.text import MIMEText
from functools import lru_cache
from typing import Any

from config import get_settings

logger = logging.getLogger(__name__)

# ── OAuth2 scopes ─────────────────────────────────────────────────────────────
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
]

# ── Mock data ─────────────────────────────────────────────────────────────────

def _ago(minutes: int = 0, hours: int = 0, days: int = 0) -> str:
    dt = datetime.now(timezone.utc) - timedelta(minutes=minutes, hours=hours, days=days)
    return dt.isoformat()


MOCK_EMAILS: list[dict[str, Any]] = [
    {
        "id": "gmail:mock001",
        "platform": "gmail",
        "sender": "Sarah Johnson",
        "sender_email": "sarah.johnson@acme.com",
        "subject": "Q2 Budget Review — Action Required",
        "preview": "Hi, I've attached the updated budget spreadsheet for Q2. Please review sections 3 and 4 before our call on Friday...",
        "body": (
            "Hi,\n\n"
            "I've attached the updated budget spreadsheet for Q2. Please review "
            "sections 3 and 4 before our call on Friday. The numbers look good "
            "overall but we need sign-off on the marketing allocation.\n\n"
            "Let me know if you have any questions.\n\nBest,\nSarah"
        ),
        "thread_id": "thread_mock001",
        "timestamp": _ago(minutes=4),
        "is_unread": True,
    },
    {
        "id": "gmail:mock002",
        "platform": "gmail",
        "sender": "GitHub Notifications",
        "sender_email": "notifications@github.com",
        "subject": "[mcp-inbox] PR #42 — Add WebSocket support",
        "preview": "alexdev opened a pull request: Add WebSocket support for real-time tool log updates. 3 files changed, +248 −12...",
        "body": (
            "alexdev opened a pull request #42\n\n"
            "Add WebSocket support for real-time tool log updates\n\n"
            "3 files changed, +248 −12\n\n"
            "View the pull request: https://github.com/example/mcp-inbox/pull/42"
        ),
        "thread_id": "thread_mock002",
        "timestamp": _ago(minutes=37),
        "is_unread": True,
    },
    {
        "id": "gmail:mock003",
        "platform": "gmail",
        "sender": "Marcus Chen",
        "sender_email": "m.chen@designstudio.io",
        "subject": "Re: Dashboard mockups v3",
        "preview": "Looking great! A few minor tweaks on the sidebar spacing and we should be good to ship. See my annotations...",
        "body": (
            "Looking great! A few minor tweaks on the sidebar spacing and we "
            "should be good to ship. See my annotations on the Figma link.\n\n"
            "Main changes:\n"
            "- Sidebar: reduce padding from 24px to 20px\n"
            "- Card border radius: 8px → 10px\n"
            "- Muted text colour: bump opacity to 70%\n\n"
            "Marcus"
        ),
        "thread_id": "thread_mock003",
        "timestamp": _ago(hours=2),
        "is_unread": True,
    },
    {
        "id": "gmail:mock004",
        "platform": "gmail",
        "sender": "Stripe",
        "sender_email": "receipts@stripe.com",
        "subject": "Your invoice from Stripe — $49.00",
        "preview": "A payment of $49.00 was successfully processed for your Pro plan subscription on January 20, 2025...",
        "body": (
            "A payment of $49.00 was successfully processed.\n\n"
            "Plan: Pro\nDate: January 20, 2025\nAmount: $49.00\n\n"
            "View your invoice: https://dashboard.stripe.com/invoices/mock"
        ),
        "thread_id": "thread_mock004",
        "timestamp": _ago(hours=5),
        "is_unread": False,
    },
    {
        "id": "gmail:mock005",
        "platform": "gmail",
        "sender": "Priya Patel",
        "sender_email": "priya@startup.dev",
        "subject": "Intro — AI tool collaboration?",
        "preview": "Hey! I came across MCP Inbox and would love to explore a potential collaboration. We're building something...",
        "body": (
            "Hey!\n\n"
            "I came across MCP Inbox and would love to explore a potential "
            "collaboration. We're building a complementary AI tooling layer and "
            "think there could be some great synergy.\n\n"
            "Would you be open to a 20-minute call this week?\n\nPriya"
        ),
        "thread_id": "thread_mock005",
        "timestamp": _ago(days=1),
        "is_unread": True,
    },
]


# ── Real Gmail client ─────────────────────────────────────────────────────────

class GmailClient:
    """Thin wrapper around the Gmail REST API."""

    def __init__(self) -> None:
        self._service: Any = None

    def _build_service(self) -> Any:
        """Lazy-build the authenticated Gmail service."""
        if self._service:
            return self._service

        # Import here so missing google libs don't crash demo mode
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build

        settings = get_settings()
        creds: Credentials | None = None

        if settings.gmail_token_path.exists():
            creds = Credentials.from_authorized_user_file(
                str(settings.gmail_token_path), SCOPES
            )

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(
                    str(settings.gmail_credentials_path), SCOPES
                )
                creds = flow.run_console()
            settings.gmail_token_path.write_text(creds.to_json())

        self._service = build("gmail", "v1", credentials=creds)
        return self._service

    # ── Public methods ────────────────────────────────────────────────────

    def get_unread_emails(self, max_results: int = 20) -> list[dict[str, Any]]:
        """Fetch up to max_results unread emails from the inbox."""
        service = self._build_service()
        settings = get_settings()

        result = (
            service.users()
            .messages()
            .list(
                userId=settings.gmail_user,
                q="is:unread in:inbox",
                maxResults=max_results,
            )
            .execute()
        )

        messages_meta = result.get("messages", [])
        emails: list[dict[str, Any]] = []

        for meta in messages_meta:
            msg = (
                service.users()
                .messages()
                .get(userId=settings.gmail_user, id=meta["id"], format="full")
                .execute()
            )
            parsed = self._parse_message(msg)
            if parsed:
                emails.append(parsed)

        return emails

    def send_reply(self, thread_id: str, to: str, subject: str, body: str) -> bool:
        """Send a reply in an existing thread."""
        service = self._build_service()
        settings = get_settings()

        mime_msg = MIMEText(body)
        mime_msg["to"] = to
        mime_msg["subject"] = subject if subject.startswith("Re:") else f"Re: {subject}"
        mime_msg["threadId"] = thread_id

        raw = base64.urlsafe_b64encode(mime_msg.as_bytes()).decode()
        service.users().messages().send(
            userId=settings.gmail_user,
            body={"raw": raw, "threadId": thread_id},
        ).execute()
        return True

    def mark_as_read(self, message_id: str) -> bool:
        """Remove the UNREAD label from a message."""
        service = self._build_service()
        settings = get_settings()

        # Strip the platform prefix if present
        native_id = message_id.replace("gmail:", "")
        service.users().messages().modify(
            userId=settings.gmail_user,
            id=native_id,
            body={"removeLabelIds": ["UNREAD"]},
        ).execute()
        return True

    def get_thread(self, thread_id: str) -> list[dict[str, Any]]:
        """Return all messages in a thread."""
        service = self._build_service()
        settings = get_settings()

        thread = (
            service.users()
            .threads()
            .get(userId=settings.gmail_user, id=thread_id, format="full")
            .execute()
        )

        return [
            self._parse_message(m)
            for m in thread.get("messages", [])
            if self._parse_message(m)
        ]

    # ── Parsing helpers ───────────────────────────────────────────────────

    def _parse_message(self, msg: dict[str, Any]) -> dict[str, Any] | None:
        """Convert raw Gmail API message object to our standard dict."""
        try:
            headers = {
                h["name"].lower(): h["value"]
                for h in msg.get("payload", {}).get("headers", [])
            }
            sender_raw = headers.get("from", "Unknown")
            sender_name, sender_email = _parse_sender(sender_raw)
            subject = headers.get("subject", "(no subject)")
            date_str = headers.get("date", "")
            timestamp = _parse_date(date_str)

            body = _extract_body(msg.get("payload", {}))
            preview = _make_preview(body)

            return {
                "id": f"gmail:{msg['id']}",
                "platform": "gmail",
                "sender": sender_name,
                "sender_email": sender_email,
                "subject": subject,
                "preview": preview,
                "body": body,
                "thread_id": msg.get("threadId", ""),
                "timestamp": timestamp,
                "is_unread": "UNREAD" in msg.get("labelIds", []),
                "raw_json": None,  # omit for storage efficiency
            }
        except Exception as exc:
            logger.warning("Failed to parse Gmail message %s: %s", msg.get("id"), exc)
            return None


# ── Helper functions ──────────────────────────────────────────────────────────

def _parse_sender(raw: str) -> tuple[str, str]:
    """Return (display_name, email) from a From: header."""
    match = re.match(r'"?([^"<]+)"?\s*<([^>]+)>', raw)
    if match:
        return match.group(1).strip(), match.group(2).strip()
    if "@" in raw:
        return raw.strip(), raw.strip()
    return raw.strip(), ""


def _parse_date(date_str: str) -> str:
    """Parse RFC 2822 date to ISO-8601 UTC, falling back to now."""
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(date_str)
        return dt.astimezone(timezone.utc).isoformat()
    except Exception:
        return datetime.now(timezone.utc).isoformat()


def _extract_body(payload: dict[str, Any]) -> str:
    """Recursively extract plain-text body from MIME payload."""
    mime_type = payload.get("mimeType", "")

    if mime_type == "text/plain":
        data = payload.get("body", {}).get("data", "")
        return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")

    for part in payload.get("parts", []):
        text = _extract_body(part)
        if text:
            return text

    # Fallback: try body data directly
    data = payload.get("body", {}).get("data", "")
    if data:
        return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")

    return ""


def _make_preview(body: str, length: int = 120) -> str:
    """Strip whitespace and truncate body to a preview string."""
    clean = " ".join(body.split())
    return clean[:length] + "..." if len(clean) > length else clean


# ── Factory ───────────────────────────────────────────────────────────────────

@lru_cache(maxsize=1)
def get_gmail_client() -> GmailClient:
    return GmailClient()


def get_gmail_data(max_results: int = 20) -> tuple[list[dict[str, Any]], bool]:
    """
    Public entry point used by gmail_tools.py.
    Returns (messages, is_mock).
    Falls back to mock data automatically on any error or missing credentials.
    """
    settings = get_settings()

    if not settings.gmail_enabled:
        logger.debug("Gmail: returning mock data (not configured)")
        return MOCK_EMAILS, True

    try:
        client = get_gmail_client()
        emails = client.get_unread_emails(max_results)
        logger.info("Gmail: fetched %d real emails", len(emails))
        return emails, False
    except Exception as exc:
        logger.warning("Gmail API error (%s) — falling back to mock data", exc)
        return MOCK_EMAILS, True
