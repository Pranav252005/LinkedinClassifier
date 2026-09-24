"""Thin OpenRouter client.

OpenRouter speaks the OpenAI chat-completions shape, so one helper covers both
the sourcing agent (text) and resume OCR (vision).
"""

from __future__ import annotations

import json
import re
from typing import Any

import httpx

import meter
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


# Thinking budgets, in tokens. Thinking counts against max_tokens, so each
# budget is added on top of the answer's allowance rather than taken from it.
THINKING = {"low": 512, "medium": 2048, "high": 6144}


async def chat(
    messages: list[dict[str, Any]],
    model: str | None = None,
    max_tokens: int = 1500,
    temperature: float = 0.2,
    thinking: str | None = None,
) -> str:
    """Run a chat completion and return the assistant's text.

    `thinking` (low | medium | high) lets a reasoning model think first, within
    that budget; None leaves it to the model's default.
    """
    payload = {
        "model": model or OPENROUTER_TEXT_MODEL,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        # Ask OpenRouter to report what the call cost, so runs can be priced.
        "usage": {"include": True},
    }
    if thinking in THINKING:
        payload["reasoning"] = {"max_tokens": THINKING[thinking], "exclude": True}
        payload["max_tokens"] = max_tokens + THINKING[thinking]
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
        meter.current().llm(data.get("usage"))
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


async def chat_json_list(messages: list[dict[str, Any]], attempts: int = 2, **kw: Any) -> list:
    """A chat call whose answer must be a JSON array (or an object wrapping one).
    Models occasionally return malformed JSON; one retry fixes nearly all of it."""
    last = "The model did not return a JSON array."
    for _ in range(attempts):
        try:
            data = parse_json(await chat(messages, **kw))
        except OpenRouterError as exc:
            if "JSON" not in str(exc):
                raise                      # a key, credit or network problem: retrying will not help
            last = str(exc)
            continue
        if isinstance(data, dict):
            data = next((v for v in data.values() if isinstance(v, list)), None)
        if isinstance(data, list):
            return data
    raise OpenRouterError(last)


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


async def embed(texts: list[str], model: str) -> list[list[float]]:
    """Embed a batch of texts. One vector per input, in input order."""
    if not texts:
        return []
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                f"{OPENROUTER_BASE_URL}/embeddings",
                headers=_headers(),
                json={"model": model, "input": texts},
            )
    except httpx.HTTPError as exc:
        raise OpenRouterError(f"OpenRouter embeddings request failed: {exc}") from exc
    if resp.status_code == 402:
        raise OpenRouterError("OpenRouter credits exhausted.")
    if resp.status_code >= 400:
        raise OpenRouterError(f"OpenRouter embeddings returned HTTP {resp.status_code}: {resp.text[:200]}")
    try:
        data = resp.json()
        rows = sorted(data["data"], key=lambda r: r.get("index", 0))
        vectors = [list(map(float, r["embedding"])) for r in rows]
    except (ValueError, KeyError, TypeError) as exc:
        raise OpenRouterError(f"Unexpected embeddings response shape: {exc}") from exc
    if len(vectors) != len(texts):
        raise OpenRouterError("Embeddings response did not match the inputs.")
    meter.current().embed(data.get("usage"))
    return vectors
