"""
clients/ollama_client.py — Ollama local LLM client.

Calls the Ollama REST API (http://localhost:11434) to run
local models like llama3.2, mistral, gemma3.

No API key needed. Fully free and local.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import AsyncGenerator

import httpx

logger = logging.getLogger(__name__)

# Supports remote/self-hosted Ollama in deployed environments.
OLLAMA_BASE = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")
DEFAULT_MODEL = "llama3.2:3b"

# ── Prompts ───────────────────────────────────────────────────────────────────

SUMMARIZE_SYSTEM = (
    "You are a concise communication assistant. "
    "Summarize messages clearly in 2-4 sentences. "
    "Highlight: main topic, any action items, and suggested next steps. "
    "Be direct. No filler phrases."
)

REPLY_SYSTEM = (
    "You are a professional communication assistant. "
    "Draft a short, polite reply to the message below. "
    "Keep it under 3 sentences unless more detail is clearly needed. "
    "Match the tone of the original message."
)


# ── Core async helpers ────────────────────────────────────────────────────────

async def is_ollama_running() -> bool:
    """Return True if the Ollama server is reachable."""
    try:
        async with httpx.AsyncClient(timeout=2) as client:
            r = await client.get(f"{OLLAMA_BASE}/api/tags")
            return r.status_code == 200
    except Exception:
        return False


async def list_models() -> list[str]:
    """Return names of locally available Ollama models."""
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(f"{OLLAMA_BASE}/api/tags")
            data = r.json()
            return [m["name"] for m in data.get("models", [])]
    except Exception:
        return []


async def chat(
    prompt: str,
    system: str = "",
    model: str = DEFAULT_MODEL,
    temperature: float = 0.3,
) -> str:
    """
    Send a single prompt to Ollama and return the full response string.
    Non-streaming — waits for the complete reply.
    """
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "options": {"temperature": temperature},
    }

    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(
            f"{OLLAMA_BASE}/api/chat",
            json=payload,
        )
        r.raise_for_status()
        data = r.json()
        return data["message"]["content"].strip()


async def stream_chat(
    prompt: str,
    system: str = "",
    model: str = DEFAULT_MODEL,
    temperature: float = 0.3,
) -> AsyncGenerator[str, None]:
    """
    Stream tokens from Ollama one chunk at a time.
    Yields text deltas as they arrive.
    """
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    payload = {
        "model": model,
        "messages": messages,
        "stream": True,
        "options": {"temperature": temperature},
    }

    async with httpx.AsyncClient(timeout=120) as client:
        async with client.stream(
            "POST", f"{OLLAMA_BASE}/api/chat", json=payload
        ) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line.strip():
                    continue
                try:
                    chunk = json.loads(line)
                    delta = chunk.get("message", {}).get("content", "")
                    if delta:
                        yield delta
                    if chunk.get("done"):
                        break
                except json.JSONDecodeError:
                    continue


# ── High-level task functions ─────────────────────────────────────────────────

async def summarize_message(
    body: str,
    platform: str = "",
    sender: str = "",
    model: str = DEFAULT_MODEL,
) -> str:
    """
    Summarize a single message or thread body.
    Returns a 2-4 sentence plain-text summary.
    """
    context = ""
    if platform:
        context += f"Platform: {platform}\n"
    if sender:
        context += f"From: {sender}\n"

    prompt = f"{context}\nMessage:\n{body}\n\nProvide a concise summary."

    try:
        return await chat(prompt, system=SUMMARIZE_SYSTEM, model=model)
    except Exception as exc:
        logger.warning("Ollama summarize failed: %s", exc)
        raise


async def draft_reply(
    original_body: str,
    platform: str = "",
    sender: str = "",
    instructions: str = "",
    model: str = DEFAULT_MODEL,
) -> str:
    """
    Draft a reply to a message.
    Returns a ready-to-send plain-text reply draft.
    """
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

    try:
        return await chat(prompt, system=REPLY_SYSTEM, model=model, temperature=0.5)
    except Exception as exc:
        logger.warning("Ollama draft_reply failed: %s", exc)
        raise


async def get_best_available_model() -> str:
    """Return the best available local model, falling back through options."""
    preferred = [
        "llama3.2:3b",
        "llama3.2",
        "llama3:8b",
        "llama3",
        "mistral",
        "gemma3:4b",
        "gemma3",
        "phi3",
    ]
    available = await list_models()
    available_names = {m.split(":")[0] for m in available} | set(available)

    for model in preferred:
        if model in available or model.split(":")[0] in available_names:
            # Return the exact name that's available
            for a in available:
                if a == model or a.startswith(model.split(":")[0] + ":"):
                    return a
    return available[0] if available else DEFAULT_MODEL
