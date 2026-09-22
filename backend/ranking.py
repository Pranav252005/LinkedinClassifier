"""Rank openings that passed the hard filters.

Stage one (embeddings.py) orders everything by how close the description is to
the seeker's profile. Stage two, here, sends only the top REVIEW_TOP_N to a
model with the *full* description and asks narrow questions instead of one
vague "is this a match?":

  must_haves_met  probability the seeker meets every hard requirement
  level_fit       under | fit | over
  skills_matched  the posting's skills the resume actually shows
  skills_missing  required skills the resume does not show

Narrow questions are easier to answer honestly and easier to show: the card
says "Matches: Python, FastAPI · Missing: Kubernetes · Level: fits" instead of
a bare number. With SCORING_PROVIDER=typesafe, Jev answers the two typed
questions, which is what it is built for.

The answers become features in [0, 1] and the fit score is a weighted sum of
them. The weights start as sensible defaults and are replaced by weights fitted
to what users marked good once there are enough labels (weights.py).
"""

from __future__ import annotations

import asyncio
import re

import weights as learned
from config import (
    OPENROUTER_API_KEY,
    OPENROUTER_SCORING_MODEL,
    REVIEW_BATCH_SIZE,
    REVIEW_TOP_N,
    SCORING_PROVIDER,
    TYPESAFE_API_KEY,
)
from filters import age_days
from openrouter import OpenRouterError, chat, parse_json
from schemas import Profile, ScoredOpening

LEVEL_VALUE = {"fit": 1.0, "over": 0.5, "under": 0.25}
DESCRIPTION_CHARS = 2500
# Postings this far below a real match are not worth a model call.
MIN_REVIEW_SIMILARITY = 0.3

SYSTEM = ("You screen job postings for one candidate. You answer narrow questions about "
          "requirements and evidence, and you return only JSON in the requested shape.")


def skill_overlap(skills: list[str], text: str) -> list[str]:
    """Profile skills that appear, as whole words, in the posting text."""
    found = []
    low = (text or "").lower()
    for skill in skills:
        s = skill.strip().lower()
        if len(s) < 2:
            continue
        # Word-ish boundaries that still allow "c++", "node.js", "ci/cd".
        if re.search(rf"(?<![a-z0-9]){re.escape(s)}(?![a-z0-9])", low):
            found.append(skill)
    return found


def recency(posted_at: str | None) -> float:
    age = age_days(posted_at)
    if age is None:
        return 0.5
    return max(0.0, min(1.0, 1 - (age - 3) / 27)) if age > 3 else 1.0


def features(o: ScoredOpening, profile: Profile) -> dict[str, float]:
    """Everything the fit score is computed from, each in [0, 1].

    Unreviewed postings get stand-ins for the review answers so they can be
    ranked on the same scale; `reviewed` is itself a feature so learned
    weights can discount them.
    """
    overlap = skill_overlap(profile.skills, f"{o.title}\n{o.description}") if profile.skills else []
    det_skills = len(overlap) / max(1, min(len(profile.skills), 8)) if profile.skills else 0.5
    sim = o.similarity if o.similarity is not None else 0.5
    if o.reviewed:
        matched, missing = len(o.skills_matched), len(o.skills_missing)
        skills = matched / (matched + missing) if (matched + missing) else det_skills
        must = o.must_haves_met if o.must_haves_met is not None else sim
        level = LEVEL_VALUE.get(o.level_fit or "", 0.6)
    else:
        skills, must, level = det_skills, sim, 0.6
    return {
        "sim": round(sim, 4),
        "must": round(must, 4),
        "level": level,
        "skills": round(min(1.0, skills), 4),
        "recency": round(recency(o.posted_at), 4),
        "verified": 1.0 if o.verified else 0.0,
        "reviewed": 1.0 if o.reviewed else 0.0,
    }


# --- stage two: the model reads the full posting ------------------------------------
def _block(i: int, o: ScoredOpening) -> str:
    text = (o.description or o.snippet or "(no description available)")[:DESCRIPTION_CHARS]
    return (f"[{i}] {o.title} — {o.company or 'unknown company'}\n"
            f"location: {o.location or 'not stated'}\n{text}\n")


def _prompt(profile_text: str, resume: str, batch: list[ScoredOpening]) -> str:
    postings = "\n---\n".join(_block(i, o) for i, o in enumerate(batch))
    return f"""Screen these postings for one candidate.

### Candidate profile
{profile_text}

### Candidate resume
{resume.strip()[:3000]}

### Postings
{postings}

For EACH posting return one object:
{{
  "i": <number in brackets>,
  "must_haves_met": <0.0-1.0: probability the candidate meets every REQUIRED qualification
                     (degree, years, must-have skills, eligibility). Ignore "nice to have".>,
  "level_fit": "under" | "fit" | "over",
  "skills_matched": ["up to 6 skills the posting asks for that the resume shows"],
  "skills_missing": ["up to 4 REQUIRED skills the resume does not show"],
  (skills are short names, 1-3 words: "Kubernetes", "Go", "SQL", "fraud analysis" --
   never a sentence copied from the posting)
  "why": "<one sentence: the strongest concrete reason to apply>",
  "gap": "<one sentence: the biggest thing a screener would object to, or empty>"
}}
Return a JSON array of exactly {len(batch)} objects and nothing else.

Judge only on evidence in the resume. Use the exact skill names from the posting. A
posting whose required years or level the candidate clearly lacks is "under" with
low must_haves_met even if the skills match."""


def _strings(value: object, limit: int) -> list[str]:
    """Short skill names only. A model that pastes a requirement sentence gets
    it dropped, not truncated into something that reads as a broken skill."""
    if not isinstance(value, list):
        return []
    out = []
    for v in value:
        if not isinstance(v, str):
            continue
        name = " ".join(v.split()).strip(" .,;")
        if name and len(name) <= 32 and len(name.split()) <= 4:
            out.append(name)
    return out[:limit]


def _text(value: object, limit: int = 300) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = " ".join(value.split())
    return cleaned[:limit] or None


def apply_review(o: ScoredOpening, entry: object) -> bool:
    if not isinstance(entry, dict):
        return False
    must = entry.get("must_haves_met")
    must = max(0.0, min(1.0, float(must))) if isinstance(must, (int, float)) and not isinstance(must, bool) else None
    level = entry.get("level_fit")
    level = level.strip().lower() if isinstance(level, str) else None
    if level not in LEVEL_VALUE:
        level = None
    if must is None and level is None:
        return False
    o.reviewed = True
    o.must_haves_met = must
    o.matches_profile = must
    o.level_fit = level
    o.skills_matched = _strings(entry.get("skills_matched"), 6)
    o.skills_missing = _strings(entry.get("skills_missing"), 4)
    o.why = _text(entry.get("why"))
    o.gap = _text(entry.get("gap"))
    return True


async def _review_batch(profile_text: str, resume: str, batch: list[ScoredOpening],
                        gate: asyncio.Semaphore) -> str | None:
    try:
        async with gate:
            raw = await chat(
                [{"role": "system", "content": SYSTEM},
                 {"role": "user", "content": _prompt(profile_text, resume, batch)}],
                model=OPENROUTER_SCORING_MODEL, max_tokens=260 * len(batch) + 200, temperature=0.0,
            )
        data = parse_json(raw)
    except OpenRouterError as exc:
        return str(exc)
    if isinstance(data, dict):
        data = next((v for v in data.values() if isinstance(v, list)), None)
    if not isinstance(data, list):
        return "The review model did not return a JSON array."

    by_index: dict[int, object] = {}
    for position, entry in enumerate(data):
        idx = entry.get("i") if isinstance(entry, dict) else None
        if not isinstance(idx, int) or not 0 <= idx < len(batch):
            idx = position
        by_index.setdefault(idx, entry)
    for i, o in enumerate(batch):
        if not apply_review(o, by_index.get(i)):
            o.error = "The review model returned no verdict for this posting."
    return None


async def review(openings: list[ScoredOpening], profile_text: str, resume: str) -> list[str]:
    """Stage two, in place, on the openings given. Returns warnings."""
    if not openings:
        return []
    if SCORING_PROVIDER == "typesafe" and TYPESAFE_API_KEY:
        from jev_client import review_openings
        return await review_openings(openings, profile_text, resume)
    if not OPENROUTER_API_KEY:
        return ["OPENROUTER_API_KEY is not set — openings are ranked by similarity only."]

    size = max(1, REVIEW_BATCH_SIZE)
    gate = asyncio.Semaphore(4)
    errors = await asyncio.gather(*(
        _review_batch(profile_text, resume, openings[i:i + size], gate)
        for i in range(0, len(openings), size)))
    failed = [e for e in errors if e]
    if failed:
        return [f"{len(failed)} review batch(es) failed ({failed[0]}); those postings are ranked "
                "by similarity only."]
    return []


def finalize(openings: list[ScoredOpening], profile: Profile) -> list[ScoredOpening]:
    """Compute features and fit scores, fill in the deterministic reasons, rank."""
    model = learned.current("opening")
    for o in openings:
        o.features = features(o, profile)
        o.fit_score = learned.score(o.features, model)
        if not o.skills_matched and profile.skills:
            # Unreviewed, or the model named none: say what plain matching finds.
            o.skills_matched = skill_overlap(profile.skills, f"{o.title}\n{o.description}")[:6]
        o.priority = "high" if o.fit_score >= 75 else "medium" if o.fit_score >= 50 else "low"
    return sorted(openings, key=lambda s: (-(s.fit_score or 0), not s.reviewed, s.title))


async def rank(openings: list[ScoredOpening], profile: Profile, profile_text: str, resume: str,
               similarity: dict[str, float]) -> tuple[list[ScoredOpening], list[str]]:
    for o in openings:
        o.similarity = similarity.get(o.key)
    by_sim = sorted(openings, key=lambda o: -(o.similarity or 0))
    worth = [o for o in by_sim[:REVIEW_TOP_N] if (o.similarity or 0) >= MIN_REVIEW_SIMILARITY]
    warnings = await review(worth, profile_text, resume)
    return finalize(openings, profile), warnings
