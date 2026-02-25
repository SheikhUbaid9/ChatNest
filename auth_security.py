"""
auth_security.py - Password hashing + token encryption helpers.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
from functools import lru_cache

try:
    from cryptography.fernet import Fernet, InvalidToken
except Exception:  # pragma: no cover - optional import guard
    Fernet = None  # type: ignore[assignment]
    InvalidToken = Exception  # type: ignore[assignment]

from config import get_settings

logger = logging.getLogger(__name__)

_PBKDF2_ITERATIONS = 390_000
_warned_fallback = False


def hash_password(password: str) -> str:
    full_salt = os.urandom(24)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        full_salt,
        _PBKDF2_ITERATIONS,
    )
    return "pbkdf2_sha256${}${}${}".format(
        _PBKDF2_ITERATIONS,
        base64.urlsafe_b64encode(full_salt).decode("ascii"),
        base64.urlsafe_b64encode(digest).decode("ascii"),
    )


def verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, iterations_s, salt_b64, digest_b64 = encoded.split("$", 3)
        if scheme != "pbkdf2_sha256":
            return False
        iterations = int(iterations_s)
        salt = base64.urlsafe_b64decode(salt_b64.encode("ascii"))
        expected = base64.urlsafe_b64decode(digest_b64.encode("ascii"))
    except Exception:
        return False

    got = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(got, expected)


def _resolve_encryption_seed() -> str:
    global _warned_fallback
    s = get_settings()
    if s.auth_encryption_key.strip():
        return s.auth_encryption_key.strip()

    fallback = s.google_oauth_client_secret.strip() or s.slack_oauth_client_secret.strip()
    if fallback and not _warned_fallback:
        logger.warning(
            "AUTH_ENCRYPTION_KEY is not set; falling back to an OAuth client secret for token encryption."
        )
        _warned_fallback = True
    return fallback


@lru_cache(maxsize=1)
def _get_fernet() -> Fernet:
    if Fernet is None:
        raise RuntimeError("cryptography is not installed. Install dependencies from requirements.txt.")

    seed = _resolve_encryption_seed()
    if not seed:
        raise RuntimeError("Token encryption key is not configured. Set AUTH_ENCRYPTION_KEY.")

    # Accept both an existing Fernet key and arbitrary strings.
    try:
        maybe_key = seed.encode("ascii")
        if len(maybe_key) == 44:
            return Fernet(maybe_key)
    except Exception:
        pass

    derived = hashlib.sha256(seed.encode("utf-8")).digest()
    key = base64.urlsafe_b64encode(derived)
    return Fernet(key)


def encrypt_secret(value: str) -> str:
    if not value:
        return ""
    return _get_fernet().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_secret(value: str) -> str:
    if not value:
        return ""
    try:
        return _get_fernet().decrypt(value.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError) as exc:
        raise RuntimeError("Failed to decrypt stored token value") from exc
