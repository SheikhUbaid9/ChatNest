"""
config.py — Centralised settings for MCP Inbox.
Reads from .env file via Pydantic BaseSettings.
All values fall back to safe defaults so the app
works in Demo Mode without any API keys.
"""

from __future__ import annotations

import base64
import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    """Application-wide configuration loaded from .env."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Gmail ────────────────────────────────────────────────────────────
    gmail_credentials_path: Path = Path("./credentials.json")
    gmail_token_path: Path = Path("./token.json")
    gmail_credentials_json: str = ""
    gmail_token_json: str = ""
    gmail_user: str = "me"

    # ── Slack ────────────────────────────────────────────────────────────
    slack_bot_token: str = ""
    slack_default_channel: str = "general"

    # ── Telegram Bot ─────────────────────────────────────────────────────
    telegram_bot_token: str = ""
    telegram_proxy_url: str = ""   # e.g. "socks5://127.0.0.1:9050"

    # ── Telegram Personal Account (Telethon) ─────────────────────────────
    telegram_api_id: str = ""
    telegram_api_hash: str = ""
    telegram_session_path: Path = Path("./telegram_personal.session")

    # ── App ──────────────────────────────────────────────────────────────
    database_path: Path = Path("./mcp_inbox.db")
    ui_host: str = "0.0.0.0"
    ui_port: int = 8000
    mcp_port: int = 8001
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    force_mock: bool = False

    # ── Derived flags (set by model_validator) ───────────────────────────
    gmail_enabled: bool = False
    slack_enabled: bool = False
    telegram_enabled: bool = False

    @field_validator("gmail_credentials_path", "gmail_token_path", "database_path", mode="before")
    @classmethod
    def expand_path(cls, v: str | Path) -> Path:
        return Path(v).expanduser().resolve()

    @model_validator(mode="after")
    def detect_enabled_platforms(self) -> "Settings":
        """
        Mark each platform as enabled only when the minimum required
        credential exists.  Falls back to mock data otherwise.
        """
        self.gmail_credentials_path = self._materialize_json_secret(
            raw_value=self.gmail_credentials_json,
            preferred_path=self.gmail_credentials_path,
            fallback_name="credentials.json",
        )
        self.gmail_token_path = self._materialize_json_secret(
            raw_value=self.gmail_token_json,
            preferred_path=self.gmail_token_path,
            fallback_name="token.json",
        )

        if not self.force_mock:
            self.gmail_enabled = self.gmail_credentials_path.exists()
            self.slack_enabled = bool(
                self.slack_bot_token
                and self.slack_bot_token not in ("", "xoxb-your-token-here")
            )
            self.telegram_enabled = bool(
                self.telegram_bot_token
                and self.telegram_bot_token not in ("", "your-bot-token-here")
            )

        if not self.gmail_enabled:
            logger.info("Gmail: credentials not found — Demo Mode active")
        if not self.slack_enabled:
            logger.info("Slack: token not configured — Demo Mode active")
        if not self.telegram_enabled:
            logger.info("Telegram: token not configured — Demo Mode active")

        return self

    def _materialize_json_secret(
        self,
        raw_value: str,
        preferred_path: Path,
        fallback_name: str,
    ) -> Path:
        """
        Persist JSON secret from env var to disk so file-based clients can use it.
        Accepts plain JSON or base64-encoded JSON.
        """
        if not raw_value.strip():
            return preferred_path

        payload = raw_value.strip()
        if not payload.startswith("{"):
            try:
                payload = base64.b64decode(payload).decode("utf-8")
            except Exception:
                # Keep original payload if it's not valid base64.
                pass

        try:
            json.loads(payload)
        except Exception:
            logger.warning("Invalid JSON for %s; skipping secret materialization", fallback_name)
            return preferred_path

        candidates = [preferred_path, Path("/tmp") / fallback_name]
        for path in candidates:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(payload, encoding="utf-8")
                return path.resolve()
            except Exception:
                continue

        logger.warning("Could not write %s to disk; Gmail may run in demo mode", fallback_name)
        return preferred_path

    # ── Convenience ──────────────────────────────────────────────────────
    @property
    def demo_mode(self) -> bool:
        """True only when no platform is connected live."""
        return not (self.gmail_enabled or self.slack_enabled or self.telegram_enabled)

    @property
    def enabled_platforms(self) -> list[str]:
        platforms = []
        if self.gmail_enabled:
            platforms.append("gmail")
        if self.slack_enabled:
            platforms.append("slack")
        if self.telegram_enabled:
            platforms.append("telegram")
        return platforms

    def configure_logging(self) -> None:
        logging.basicConfig(
            level=self.log_level,
            format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
            datefmt="%H:%M:%S",
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached singleton Settings instance."""
    settings = Settings()
    settings.configure_logging()
    return settings
