"""Read a resume once into a structured profile.

Every later stage used to be handed the raw resume and asked to work out, again,
whether this person is a student, where they want to work and what they know.
Reading it once into fields means the hard filters can run in plain code, the
seeker can correct a wrong guess before it costs them a run, and the scorer is
told the answers instead of re-deriving them.

The model's output is a suggestion: every field is validated and clamped, and
anything unusable falls back to a conservative default rather than failing.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from openrouter import OpenRouterError, chat, parse_json
from schemas import Profile

SYSTEM = ("You read resumes into structured fields. You never guess beyond what the "
          "resume says. You return only JSON in the requested shape.")

PROMPT = """Read this resume into JSON exactly like:
{{
  "target_roles": ["up to 5 job titles this person should apply for, as they appear on postings"],
  "seniority": "intern" | "new_grad" | "junior" | "mid" | "senior",
  "years_experience": <full-time professional years; internships do not count>,
  "job_types": ["internship" and/or "full_time"],
  "skills": ["up to 25 concrete skills: languages, frameworks, tools, domains"],
  "locations": ["cities or countries they live in or say they want to work in"],
  "remote_ok": true | false,
  "needs_sponsorship": true | false,
  "college": "most recent university, as written, or empty",
  "grad_year": <year of most recent graduation or expected graduation, or null>,
  "summary": "one line: who this is, e.g. 'Third-year CS student at IIT Delhi, backend + ML'"
}}

Rules:
- A current student is "intern" and wants "internship" -- add "full_time" only if they
  graduate within about a year. Someone who graduated in the last year with no full-time
  job yet is "new_grad". Then junior is 1-2 years, mid 3-5, senior 6+.
- needs_sponsorship is true only if the resume states it; otherwise false.
- remote_ok defaults to true unless they say otherwise.
- Today is {today}.

### Resume
{resume}"""

_LEVELS = ("intern", "new_grad", "junior", "mid", "senior")


def _strings(raw: object, limit: int, width: int = 60) -> list[str]:
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, str):
            continue
        value = " ".join(item.split()).strip(" ,;.")[:width]
        if value and value.lower() not in seen:
            seen.add(value.lower())
            out.append(value)
        if len(out) >= limit:
            break
    return out


def level_for_years(years: float) -> str:
    if years < 1:
        return "new_grad"
    if years < 3:
        return "junior"
    if years < 6:
        return "mid"
    return "senior"


def coerce(data: object) -> Profile:
    """Turn whatever the model returned into a valid Profile."""
    if not isinstance(data, dict):
        return Profile()
    years = data.get("years_experience")
    years = float(years) if isinstance(years, (int, float)) and not isinstance(years, bool) else 0.0
    years = max(0.0, min(50.0, years))

    seniority = data.get("seniority")
    seniority = seniority if seniority in _LEVELS else ("intern" if years == 0 else level_for_years(years))

    job_types = [t for t in _strings(data.get("job_types"), 2) if t in ("internship", "full_time")]
    if not job_types:
        job_types = ["internship"] if seniority == "intern" else ["full_time"]

    grad = data.get("grad_year")
    grad = int(grad) if isinstance(grad, (int, float)) and not isinstance(grad, bool) and 1970 <= grad <= 2100 else None

    college = data.get("college")
    summary = data.get("summary")
    return Profile(
        target_roles=_strings(data.get("target_roles"), 5, 80),
        seniority=seniority,
        years_experience=round(years, 1),
        job_types=job_types,
        skills=_strings(data.get("skills"), 25, 40),
        locations=_strings(data.get("locations"), 6, 60),
        remote_ok=data.get("remote_ok") is not False,
        needs_sponsorship=data.get("needs_sponsorship") is True,
        college=college.strip()[:120] if isinstance(college, str) else "",
        grad_year=grad,
        summary=summary.strip()[:400] if isinstance(summary, str) else "",
    )


_YEAR = re.compile(r"\b(19[89]\d|20[0-4]\d)\b")
_STUDENT = re.compile(r"\b(b\.?tech|b\.?e\.?|b\.?s\.?c?|bachelor|undergraduate|student|expected)\b", re.I)


def heuristic(resume: str) -> Profile:
    """A usable profile without a model: enough for the filters to be conservative."""
    this_year = datetime.now(timezone.utc).year
    years = [int(y) for y in _YEAR.findall(resume) if int(y) <= this_year + 5]
    grad = max(years) if years else None
    student = bool(_STUDENT.search(resume)) and (grad is None or grad >= this_year)
    return Profile(
        seniority="intern" if student else "new_grad",
        job_types=["internship", "full_time"] if student else ["full_time"],
        grad_year=grad,
    )


async def extract(resume: str) -> tuple[Profile, list[str]]:
    """Profile from resume text. Never raises."""
    today = datetime.now(timezone.utc).date().isoformat()
    try:
        raw = await chat(
            [{"role": "system", "content": SYSTEM},
             {"role": "user", "content": PROMPT.format(today=today, resume=resume.strip()[:9000])}],
            max_tokens=900, temperature=0.0, thinking="medium",
        )
        return coerce(parse_json(raw)), []
    except OpenRouterError as exc:
        return heuristic(resume), [f"Could not read the resume into a profile ({exc}); "
                                   "using a rough guess — check the profile fields."]


def as_text(profile: Profile) -> str:
    """The profile as prose, for embedding and for prompts."""
    parts = [profile.summary]
    if profile.target_roles:
        parts.append("Target roles: " + ", ".join(profile.target_roles))
    parts.append(f"Level: {profile.seniority.replace('_', ' ')}, "
                 f"{profile.years_experience:g} years of full-time experience")
    if profile.skills:
        parts.append("Skills: " + ", ".join(profile.skills))
    if profile.locations:
        parts.append("Locations: " + ", ".join(profile.locations)
                     + (" (open to remote)" if profile.remote_ok else ""))
    if profile.college:
        parts.append(f"Education: {profile.college}"
                     + (f", graduating {profile.grad_year}" if profile.grad_year else ""))
    return "\n".join(p for p in parts if p)
