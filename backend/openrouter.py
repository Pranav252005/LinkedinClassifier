"""Thin OpenRouter client.

OpenRouter speaks the OpenAI chat-completions shape, so one helper covers both
the sourcing agent (text) and resume OCR (vision).
"""

from __future__ import annotations

import json
import re
from typing import Any

import httpx

from config import (
    OPENROUTER_API_KEY,
    OPENROUTER_BASE_URL,
    OPENROUTER_TEXT_MODEL,
    OPENROUTER_VISION_MODEL,
    PUBLIC_BASE_URL,
)

_JSON_BLOCK = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


class OpenRouterError(RuntimeError):
    pass


def _headers() -> dict[str, str]:
    if not OPENROUTER_API_KEY:
        raise OpenRouterError("OPENROUTER_API_KEY is not set.")
    return {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        # OpenRouter uses these for attribution on its dashboard/leaderboards.
        "HTTP-Referer": PUBLIC_BASE_URL,
        "X-Title": "LeadClassifier",
    }


async def chat(
    messages: list[dict[str, Any]],
    model: str | None = None,
    max_tokens: int = 1500,
    temperature: float = 0.2,
) -> str:
    """Run a chat completion and return the assistant's text."""
    payload = {
        "model": model or OPENROUTER_TEXT_MODEL,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    try:
        async with httpx.AsyncClient(timeout=90.0) as client:
            resp = await client.post(
                f"{OPENROUTER_BASE_URL}/chat/completions", headers=_headers(), json=payload
            )
    except httpx.HTTPError as exc:
        raise OpenRouterError(f"OpenRouter request failed: {exc}") from exc

    if resp.status_code in (401, 403):
        raise OpenRouterError("OpenRouter rejected the API key (check OPENROUTER_API_KEY).")
    if resp.status_code == 402:
        raise OpenRouterError("OpenRouter credits exhausted.")
    if resp.status_code == 429:
        raise OpenRouterError("OpenRouter rate limit reached — try again shortly.")
    if resp.status_code >= 400:
        raise OpenRouterError(f"OpenRouter returned HTTP {resp.status_code}: {resp.text[:200]}")

    try:
        data = resp.json()
        return (data["choices"][0]["message"]["content"] or "").strip()
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise OpenRouterError(f"Unexpected OpenRouter response shape: {exc}") from exc


def parse_json(text: str) -> Any:
    """Models like to wrap JSON in prose or fences. Dig it out."""
    fenced = _JSON_BLOCK.search(text)
    if fenced:
        text = fenced.group(1)
    text = text.strip()
    try:
        return json.loads(text)
    except ValueError:
        pass
    # Fall back to the outermost {...} or [...] span.
    for opener, closer in (("{", "}"), ("[", "]")):
        start, end = text.find(opener), text.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except ValueError:
                continue
    raise OpenRouterError("Model did not return usable JSON.")


async def read_image(data_url: str, instruction: str) -> str:
    """Send an image (as a data: URL) to the vision model and return its text."""
    return await chat(
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": instruction},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }
        ],
        model=OPENROUTER_VISION_MODEL,
        max_tokens=3000,
        temperature=0.0,
    )
