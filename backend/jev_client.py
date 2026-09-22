"""Jev (TypeSafe) System One client + fit-score combination.

Jev returns calibrated *typed* answers rather than generated text, so each
candidate is scored with two questions:

  plausible_contact : boolean  -> probability this is a sensible person to contact
  priority          : enum     -> high / medium / low outreach priority

Jev is early access and its request/response schema has moved; parsing here is
deliberately tolerant and every unknown shape surfaces as a per-candidate error
rather than a crash. See https://docs.typesafe.ai/concepts/system-one
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from config import TYPESAFE_API_KEY, TYPESAFE_BASE_URL
from schemas import Candidate, ScoredCandidate

SYSTEM_ONE_PATH = "/v1/systemone"

MAX_CONCURRENCY = 8
PRIORITY_WEIGHT = {"high": 1.0, "medium": 0.65, "low": 0.3}


def _build_prompt(resume: str, role_target: str, candidate: Candidate) -> str:
    wanted = role_target.strip() or "a relevant role"
    return (
        "You are helping a job seeker pick LinkedIn outreach targets.\n\n"
        f"### Job seeker's resume\n{resume.strip()[:6000]}\n\n"
        f"### Role they are targeting\n{wanted}\n\n"
        "### Candidate contact (from public search results)\n"
        f"Name: {candidate.name}\n"
        f"Headline: {candidate.headline or 'unknown'}\n"
        f"Company searched: {candidate.company_query}\n"
        f"Title searched: {candidate.title_query}\n"
        f"Search snippet: {candidate.snippet or 'none'}\n"
    )


def _questions() -> list[dict[str, Any]]:
    return [
        {
            "name": "plausible_contact",
            "type": "boolean",
            "question": (
                "Is this person a plausible and sensible outreach contact for this job "
                "seeker — i.e. do they actually appear to work at the target company in a "
                "role that could influence or refer for the targeted position?"
            ),
        },
        {
            "name": "priority",
            "type": "enum",
            "values": ["high", "medium", "low"],
            "question": (
                "How should outreach to this person be prioritized relative to other "
                "contacts at the same company, given the seeker's background?"
            ),
        },
    ]


def _extract_answers(payload: dict[str, Any]) -> dict[str, Any]:
    """Pull {name: answer-ish} out of whichever envelope Jev returns."""
    answers = payload.get("answers") or payload.get("results") or payload.get("data")
    if isinstance(answers, dict):
        return answers
    if isinstance(answers, list):
        out: dict[str, Any] = {}
        for item in answers:
            if isinstance(item, dict) and "name" in item:
                out[item["name"]] = item
        return out
    return payload


def _as_probability(answer: Any) -> float | None:
    """Jev may return a bare probability, or {value, probability/confidence}."""
    if isinstance(answer, (int, float)) and not isinstance(answer, bool):
        return max(0.0, min(1.0, float(answer)))
    if isinstance(answer, bool):
        return 1.0 if answer else 0.0
    if isinstance(answer, dict):
        for key in ("probability", "confidence", "score", "p_true"):
            value = answer.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                prob = max(0.0, min(1.0, float(value)))
                # A confidence attached to a False verdict is confidence in "no".
                if answer.get("value") is False or answer.get("answer") is False:
                    return 1.0 - prob
                return prob
        for key in ("value", "answer"):
            if key in answer:
                return _as_probability(answer[key])
    return None


def _as_priority(answer: Any) -> str | None:
    if isinstance(answer, str):
        value = answer.strip().lower()
        return value if value in PRIORITY_WEIGHT else None
    if isinstance(answer, dict):
        for key in ("value", "answer", "label", "choice"):
            if key in answer:
                return _as_priority(answer[key])
    return None


def combine_fit_score(plausible: float | None, priority: str | None) -> int | None:
    """Blend the two typed answers into a single 0-100 ranking score.

    Plausibility dominates (a wrong person is useless however 'high priority'),
    priority breaks ties between similarly plausible contacts.
    """
    if plausible is None and priority is None:
        return None
    plausible = 0.5 if plausible is None else plausible
    weight = PRIORITY_WEIGHT.get(priority or "", 0.65)
    return round(100 * (0.7 * plausible + 0.3 * weight))


async def _score_one(
    client: httpx.AsyncClient,
    api_key: str,
    resume: str,
    role_target: str,
    candidate: Candidate,
    semaphore: asyncio.Semaphore,
) -> ScoredCandidate:
    scored = ScoredCandidate(**candidate.model_dump())
    try:
        async with semaphore:
            resp = await client.post(
                f"{TYPESAFE_BASE_URL}{SYSTEM_ONE_PATH}",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "context": _build_prompt(resume, role_target, candidate),
                    "questions": _questions(),
                },
            )
    except httpx.HTTPError as exc:
        scored.error = f"Jev request failed: {exc}"
        return scored

    if resp.status_code in (401, 403):
        scored.error = "Jev rejected the API key (check TYPESAFE_API_KEY)."
        return scored
    if resp.status_code == 429:
        scored.error = "Jev rate limit reached."
        return scored
    if resp.status_code >= 400:
        scored.error = f"Jev returned HTTP {resp.status_code}: {resp.text[:200]}"
        return scored

    try:
        answers = _extract_answers(resp.json())
    except ValueError:
        scored.error = "Jev returned a non-JSON response."
        return scored

    scored.plausible_contact = _as_probability(answers.get("plausible_contact"))
    scored.priority = _as_priority(answers.get("priority"))
    scored.fit_score = combine_fit_score(scored.plausible_contact, scored.priority)

    if scored.fit_score is None:
        scored.error = "Jev response did not contain the expected typed answers."
    return scored


async def score_candidates(
    candidates: list[Candidate], resume: str, role_target: str = ""
) -> tuple[list[ScoredCandidate], list[str]]:
    """Score every candidate. Returns (scored, warnings)."""
    api_key = TYPESAFE_API_KEY
    if not api_key:
        unscored = [ScoredCandidate(**c.model_dump(), error="TYPESAFE_API_KEY is not set.") for c in candidates]
        return unscored, [
            "TYPESAFE_API_KEY is not set — candidates are listed unscored, in search order."
        ]

    semaphore = asyncio.Semaphore(MAX_CONCURRENCY)
    async with httpx.AsyncClient(timeout=30.0) as client:
        scored = await asyncio.gather(
            *(
                _score_one(client, api_key, resume, role_target, candidate, semaphore)
                for candidate in candidates
            )
        )

    warnings: list[str] = []
    failed = [s for s in scored if s.error]
    if failed:
        warnings.append(f"{len(failed)} of {len(scored)} candidates could not be scored: {failed[0].error}")

    # Highest score first; unscored candidates sink to the bottom.
    ordered = sorted(scored, key=lambda s: (s.fit_score is None, -(s.fit_score or 0), s.name))
    return ordered, warnings
