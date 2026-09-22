"""Candidate scoring — the stage that turns a search hit into a ranked contact.

Two backends produce the same two typed answers:

  plausible_contact : float 0..1  — is this a sensible person to contact?
  priority          : high|medium|low

`openrouter` (default) asks a normal chat model for those answers as JSON,
batching candidates to keep the credit cost down. `typesafe` calls Jev's
System One endpoint, which returns genuinely calibrated probabilities.

Jev is NOT available through OpenRouter — it is served only from TypeSafe's own
API — so the OpenRouter path is an approximation of it, not the same model. A
chat model's "0.83" is a plausible-sounding number, not a calibrated one; it
ranks candidates sensibly but the absolute values mean less than Jev's do.
"""

from __future__ import annotations

import asyncio

from config import (
    OPENROUTER_SCORING_MODEL,
    SCORING_BATCH_SIZE,
    SCORING_PROVIDER,
    OPENROUTER_API_KEY,
    TYPESAFE_API_KEY,
)
from jev_client import combine_fit_score, score_candidates as score_with_jev
from openrouter import OpenRouterError, chat, parse_json
from schemas import Candidate, ScoredCandidate

SYSTEM = (
    "You assess whether a person found in public search results is worth contacting "
    "about a job. You return only JSON in the requested shape — no prose, no markdown."
)

VALID_PRIORITY = {"high", "medium", "low"}


def _candidate_block(index: int, c: Candidate) -> str:
    return (
        f"[{index}]\n"
        f"name: {c.name}\n"
        f"headline: {c.headline or 'unknown'}\n"
        f"company searched: {c.company_query}\n"
        f"title searched: {c.title_query}\n"
        f"search snippet: {c.snippet or 'none'}\n"
    )


def _prompt(resume: str, role_target: str, batch: list[Candidate]) -> str:
    blocks = "\n".join(_candidate_block(i, c) for i, c in enumerate(batch))
    return f"""A job seeker wants to know which of these people are worth reaching out to.

### The seeker's resume
{resume.strip()[:6000]}

### The role they are targeting
{role_target.strip() or "(not stated — infer it from the resume)"}

### Candidates, from public search results
{blocks}

For EACH candidate return one object:

{{
  "i": <the number in brackets>,
  "plausible_contact": <0.0-1.0, the probability this person genuinely works at the
                        target company in a role that could refer, screen or hire
                        for the seeker's target role>,
  "priority": "high" | "medium" | "low",
  "reason": "<one sentence: the specific overlap between this person and the
              seeker that makes contacting them worthwhile>",
  "ask": "<one sentence: what to actually ask this person, phrased so they can
           say yes in under a minute>"
}}

Return a JSON array of exactly {len(batch)} such objects and nothing else.

Calibration matters more than optimism. These scores are used to rank people
against each other, so a batch where everything scores the same is useless.

- 1.0 means certainty. A search snippet almost never justifies it — reserve
  0.9+ for a snippet that states the person's current role AND employer and
  both match the target.
- 0.6-0.8: the role and company look right but the snippet is partial, stale,
  or the title is adjacent rather than exact.
- 0.4-0.6: genuinely ambiguous. A thin snippet belongs here, not higher.
- Below 0.3: wrong company, clearly a past role, or an unrelated function.

Judge each person on the evidence actually present in their own snippet, and
spread the scores to reflect real differences between them.

Priority is about who to contact FIRST, so it must discriminate too: reserve
"high" for the strongest few in this batch, not for everyone who is plausible.

"reason" and "ask" are what turns a connection request into a reply. Ground both
in what the snippet actually says about this person and what the resume actually
says about the seeker — a named team, a shared technology, the specific role
they recruit for. Never invent a shared school, employer or project that is not
evidenced. A recruiter and an engineer warrant different asks: a recruiter can
route an application, an engineer can refer or describe the team. Avoid "I would
love to connect" phrasing; say what you want and why they are the person to ask.
"""


def _text(value: object, limit: int = 400) -> str | None:
    """Model-written prose, trimmed and bounded. Empty becomes None."""
    if not isinstance(value, str):
        return None
    cleaned = " ".join(value.split()).strip()
    return cleaned[:limit] or None


def _coerce(entry: object) -> tuple[float | None, str | None]:
    """Pull a clamped probability and a valid priority out of one model object."""
    if not isinstance(entry, dict):
        return None, None

    prob = entry.get("plausible_contact")
    if isinstance(prob, bool) or not isinstance(prob, (int, float)):
        prob = None
    else:
        prob = max(0.0, min(1.0, float(prob)))

    pri = entry.get("priority")
    pri = pri.strip().lower() if isinstance(pri, str) else None
    if pri not in VALID_PRIORITY:
        pri = None

    return prob, pri


async def _score_batch(
    resume: str, role_target: str, batch: list[Candidate], semaphore: asyncio.Semaphore
) -> list[ScoredCandidate]:
    out = [ScoredCandidate(**c.model_dump()) for c in batch]
    try:
        async with semaphore:
            raw = await chat(
                messages=[
                    {"role": "system", "content": SYSTEM},
                    {"role": "user", "content": _prompt(resume, role_target, batch)},
                ],
                model=OPENROUTER_SCORING_MODEL,
                max_tokens=180 * len(batch) + 300,
                temperature=0.0,
            )
        data = parse_json(raw)
    except OpenRouterError as exc:
        for s in out:
            s.error = f"Scoring failed: {exc}"
        return out

    if isinstance(data, dict):
        # Some models wrap the array in an object; take the first list they offer.
        data = next((v for v in data.values() if isinstance(v, list)), None)
    if not isinstance(data, list):
        for s in out:
            s.error = "Scoring model did not return a JSON array."
        return out

    # Prefer the model's own index; fall back to position when it omits one.
    by_index: dict[int, object] = {}
    for position, entry in enumerate(data):
        idx = entry.get("i") if isinstance(entry, dict) else None
        if not isinstance(idx, int) or not 0 <= idx < len(batch):
            idx = position
        if 0 <= idx < len(batch):
            by_index.setdefault(idx, entry)

    for i, scored in enumerate(out):
        entry = by_index.get(i)
        prob, pri = _coerce(entry)
        if prob is None and pri is None:
            scored.error = "Scoring model returned no verdict for this candidate."
            continue
        scored.plausible_contact = prob
        scored.priority = pri
        scored.fit_score = combine_fit_score(prob, pri)
        if isinstance(entry, dict):
            scored.reason = _text(entry.get("reason"))
            scored.ask = _text(entry.get("ask"))

    return out


async def _score_with_openrouter(
    candidates: list[Candidate], resume: str, role_target: str
) -> tuple[list[ScoredCandidate], list[str]]:
    if not OPENROUTER_API_KEY:
        unscored = [
            ScoredCandidate(**c.model_dump(), error="OPENROUTER_API_KEY is not set.")
            for c in candidates
        ]
        return unscored, ["OPENROUTER_API_KEY is not set — candidates are unscored, in search order."]

    size = max(1, SCORING_BATCH_SIZE)
    batches = [candidates[i : i + size] for i in range(0, len(candidates), size)]
    semaphore = asyncio.Semaphore(4)

    results = await asyncio.gather(
        *(_score_batch(resume, role_target, b, semaphore) for b in batches)
    )
    scored = [s for batch in results for s in batch]

    warnings: list[str] = []
    failed = [s for s in scored if s.error]
    if failed:
        warnings.append(f"{len(failed)} of {len(scored)} candidates could not be scored: {failed[0].error}")

    return scored, warnings


def _rank(scored: list[ScoredCandidate]) -> list[ScoredCandidate]:
    """Highest fit first; anything unscored sinks to the bottom."""
    return sorted(scored, key=lambda s: (s.fit_score is None, -(s.fit_score or 0), s.name))


async def score_candidates(
    candidates: list[Candidate], resume: str, role_target: str = ""
) -> tuple[list[ScoredCandidate], list[str]]:
    """Score and rank every candidate with the configured provider."""
    if not candidates:
        return [], []

    if SCORING_PROVIDER == "typesafe":
        scored, warnings = await score_with_jev(candidates, resume, role_target)
        return scored, warnings  # jev_client already ranks

    scored, warnings = await _score_with_openrouter(candidates, resume, role_target)

    # Offer the fallback only when it is actually usable.
    if TYPESAFE_API_KEY and any(s.error for s in scored):
        warnings.append("Set SCORING_PROVIDER=typesafe in .env to score with Jev instead.")

    return _rank(scored), warnings
