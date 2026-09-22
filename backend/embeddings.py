"""Stage one of ranking: resume-to-description similarity from embeddings.

Cheap enough to run over everything that survives the hard filters -- a few
hundred postings cost a fraction of a cent -- and posting vectors are cached in
the database against a hash of the text they were computed from, so the daily
poll and repeat runs only pay for postings that are new or have changed.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import math
import struct

import db
from config import EMBEDDING_MODEL
from openrouter import OpenRouterError, embed

BATCH = 96
MAX_CHARS = 6000       # ~1500 tokens: title, team, location and the start of the description


def pack(vector: list[float]) -> str:
    return base64.b64encode(struct.pack(f"<{len(vector)}f", *vector)).decode("ascii")


def unpack(blob: str) -> list[float]:
    raw = base64.b64decode(blob)
    return list(struct.unpack(f"<{len(raw) // 4}f", raw))


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def posting_text(o) -> str:
    head = f"{o.title}\n{o.company}\n{o.department}\n{o.location}"
    return f"{head}\n\n{(o.description or o.snippet or '')}"[:MAX_CHARS]


def _hash(text: str) -> str:
    return hashlib.sha1(f"{EMBEDDING_MODEL}\n{text}".encode("utf-8", "ignore")).hexdigest()[:16]


async def embed_texts(texts: list[str]) -> list[list[float]]:
    gate = asyncio.Semaphore(4)

    async def batch(chunk: list[str]) -> list[list[float]]:
        async with gate:
            return await embed(chunk, EMBEDDING_MODEL)

    parts = await asyncio.gather(*(batch(texts[i:i + BATCH]) for i in range(0, len(texts), BATCH)))
    return [v for part in parts for v in part]


async def embed_one(text: str) -> list[float]:
    return (await embed_texts([text[:MAX_CHARS]]))[0]


async def posting_vectors(openings: list) -> dict[str, list[float]]:
    """{opening.key: vector}, reusing stored vectors whose text has not changed."""
    texts = {o.key: posting_text(o) for o in openings if o.key}
    hashes = {k: _hash(t) for k, t in texts.items()}
    try:
        stored = db.get_embeddings(list(texts))
    except Exception:
        stored = {}

    vectors: dict[str, list[float]] = {}
    todo: list[str] = []
    for key in texts:
        cached = stored.get(key)
        if cached and cached[1] == hashes[key]:
            vectors[key] = unpack(cached[0])
        else:
            todo.append(key)

    if todo:
        fresh = await embed_texts([texts[k] for k in todo])
        for key, vec in zip(todo, fresh):
            vectors[key] = vec
        try:
            # Only board postings have a row to write to; web results are not stored.
            db.set_embeddings([(k, pack(vectors[k]), hashes[k]) for k in todo if ":" in k and "://" not in k])
        except Exception:
            pass
    return vectors


def title_text(o) -> str:
    return f"{o.title}. {o.department}".strip(". ")


# Title vectors are tiny and cheap; kept per process rather than in the database.
_TITLE_CACHE: dict[str, list[float]] = {}


async def title_vectors(openings: list) -> dict[str, list[float]]:
    texts = {o.key: title_text(o) for o in openings if o.key}
    todo = sorted({t for t in texts.values() if _hash(t) not in _TITLE_CACHE})
    if todo:
        for text, vec in zip(todo, await embed_texts(todo)):
            _TITLE_CACHE[_hash(text)] = vec
        if len(_TITLE_CACHE) > 50_000:
            _TITLE_CACHE.clear()
    return {k: _TITLE_CACHE[_hash(t)] for k, t in texts.items() if _hash(t) in _TITLE_CACHE}


async def similarities(profile_text: str, openings: list) -> dict[str, float]:
    """{key: similarity mapped to 0..1}. Raises OpenRouterError.

    Blends two views. The full description says what the job involves, but
    every posting at a company shares the same "About us" boilerplate, which
    pulls all of them towards the same score. The title and team say what the
    job *is*, and separate an engineering internship from an operations role
    far more sharply. Half of each.
    """
    if not openings:
        return {}
    seeker, full, titles = await asyncio.gather(
        embed_one(profile_text), posting_vectors(openings), title_vectors(openings))
    return {key: blend(seeker, vec, titles.get(key)) for key, vec in full.items()}


def blend(seeker: list[float], full: list[float], title: list[float] | None) -> float:
    cos = cosine(seeker, full)
    if title is not None:
        cos = 0.5 * cos + 0.5 * cosine(seeker, title)
    return rescale(cos)


def rescale(cos: float) -> float:
    """Blended cosine for text-embedding-3 sits in a narrow band: about 0.35 for
    an unrelated role, 0.55-0.6 for a strong match. Stretch that band onto 0..1
    so it reads as a score and spreads the ranking."""
    lo, hi = 0.35, 0.62
    return max(0.0, min(1.0, (cos - lo) / (hi - lo)))


__all__ = ["OpenRouterError", "similarities", "embed_one", "pack", "unpack", "cosine", "rescale",
           "posting_vectors", "title_vectors", "blend"]
