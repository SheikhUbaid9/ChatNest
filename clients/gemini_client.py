"""
clients/gemini_client.py - Gemini API client helpers.

Provides async wrappers for summary/draft generation using Gemini.
Falls back to caller-level behavior if API key or SDK is unavailable.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

AI_PROVIDER = os.getenv("AI_PROVIDER", "auto").strip().lower()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip() or "gemini-2.5-flash"

try:
    import google.generativeai as genai
except Exception:  # pragma: no cover - optional dependency at runtime
    genai = None


SUMMARIZE_SYSTEM = (
    "You are a concise communication assistant. "
    "Summarize messages clearly in 2-4 sentences. "
    "Highlight: main topic, action items, and suggested next steps. "
    "Be direct and avoid filler."
)

REPLY_SYSTEM = (
    "You are a professional communication assistant. "
    "Draft a short, polite reply to the message below. "
    "Keep it under 3 sentences unless more detail is clearly needed. "
    "Match the tone of the original message."
)


def get_ai_provider_preference() -> str:
    """Return configured provider preference: auto|gemini|ollama."""
    return AI_PROVIDER if AI_PROVIDER in {"auto", "gemini", "ollama"} else "auto"


def is_gemini_ready() -> bool:
    """True when Gemini SDK is available and API key is configured."""
    return bool(genai and GEMINI_API_KEY)


def _extract_response_text(response: Any) -> str:
    text = getattr(response, "text", "") or ""
    if text.strip():
        return text.strip()

    parts: list[str] = []
    for candidate in getattr(response, "candidates", []) or []:
        content = getattr(candidate, "content", None)
        for part in getattr(content, "parts", []) or []:
            part_text = getattr(part, "text", "") or ""
            if part_text.strip():
                parts.append(part_text.strip())
    return "\n".join(parts).strip()


def _generate_sync(
    prompt: str,
    system: str,
    model: str,
    temperature: float,
) -> str:
    if not genai:
        raise RuntimeError("google-generativeai is not installed")
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY is not configured")

    genai.configure(api_key=GEMINI_API_KEY)
    llm = genai.GenerativeModel(
        model_name=model or GEMINI_MODEL,
        system_instruction=system,
    )
    response = llm.generate_content(
        prompt,
        generation_config={"temperature": temperature},
    )
    text = _extract_response_text(response)
    if not text:
        raise RuntimeError("Gemini returned an empty response")
    return text


async def generate_text(
    prompt: str,
    system: str,
    model: str = "",
    temperature: float = 0.3,
) -> str:
    """Async wrapper for Gemini text generation."""
    return await asyncio.to_thread(
        _generate_sync,
        prompt,
        system,
        model or GEMINI_MODEL,
        temperature,
    )


async def summarize_message_gemini(
    body: str,
    platform: str = "",
    sender: str = "",
    model: str = "",
) -> str:
    context = ""
    if platform:
        context += f"Platform: {platform}\n"
    if sender:
        context += f"From: {sender}\n"
    prompt = f"{context}\nMessage:\n{body}\n\nProvide a concise summary."
    return await generate_text(
        prompt=prompt,
        system=SUMMARIZE_SYSTEM,
        model=model or GEMINI_MODEL,
        temperature=0.2,
    )


async def draft_reply_gemini(
    original_body: str,
    platform: str = "",
    sender: str = "",
    instructions: str = "",
    model: str = "",
) -> str:
    context = ""
    if platform:
        context += f"Platform: {platform}\n"
    if sender:
        context += f"From: {sender}\n"
    if instructions:
        context += f"Instructions: {instructions}\n"

    prompt = (
        f"{context}\nOriginal message:\n{original_body}\n\n"
        "Draft a professional reply."
    )
    return await generate_text(
        prompt=prompt,
        system=REPLY_SYSTEM,
        model=model or GEMINI_MODEL,
        temperature=0.4,
    )
