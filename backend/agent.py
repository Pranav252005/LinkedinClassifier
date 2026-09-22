"""The sourcing agent: a cheap OpenRouter model turns a resume into a search plan.

It does the judgement work Serper can't (which companies, which titles, which
phrasings are worth a query) and Jev shouldn't (Jev scores, it doesn't plan).
Everything it returns is validated and clamped here — the model's output is
treated as a suggestion, never as trusted input.
"""

from __future__ import annotations

from openrouter import OpenRouterError, chat, parse_json
from schemas import SearchPlan

MAX_COMPANIES = 12
MAX_TITLES = 8
MAX_QUERIES = 24

DEFAULT_TITLES = [
    "university recruiter",
    "technical recruiter",
    "engineering manager",
    "talent acquisition",
]

SYSTEM = (
    "You plan LinkedIn outreach searches for a job seeker. You do not write prose. "
    "You return only JSON matching the requested shape."
)


JOB_RULES = """Rules:
- Always include every company and title the seeker explicitly named, first.
- "titles" here are JOB TITLES TO APPLY FOR, as they appear on real postings —
  e.g. "Software Engineer Intern", "Summer 2027 Software Engineering Intern".
  Not the titles of people to contact.
- Match the seeker's actual level. A student wants intern and new-grad postings,
  not staff or principal ones.
- Suggest companies that genuinely hire this profile at this level, in or open
  to the seeker's locations. Openings are read from each company's own job
  board, so prefer companies with a real hiring programme for this level over
  famous names that rarely hire it. Mix large employers with strong startups.
- No commentary outside the JSON."""

PEOPLE_RULES = """Rules:
- Always include every company and title the seeker explicitly named, first.
- Only add companies that plausibly hire this person's profile and seniority.
- Titles should be roles that can refer, screen, or hire — recruiters, hiring
  managers, team leads, and engineers on the relevant team. Not executives.
- Use the plain title wording that appears on real LinkedIn profiles.
- No commentary outside the JSON."""


def _prompt(resume: str, companies: list[str], titles: list[str], role: str,
            for_jobs: bool = False, profile_text: str = "") -> str:
    goal = ("build a plan for finding job openings they should apply to"
            if for_jobs else
            "build a plan for finding people worth reaching out to")
    return f"""Given this job seeker's resume, {goal}.

### Resume
{resume.strip()[:8000]}

### Structured profile (confirmed by the seeker — trust it over the resume)
{profile_text.strip() or "(none)"}

### Stated target role
{role.strip() or "(not stated — infer it from the resume)"}

### Companies the seeker named
{", ".join(companies) if companies else "(none — suggest companies that fit this background)"}

### Contact titles the seeker named
{", ".join(titles) if titles else "(none — choose the titles most likely to respond usefully)"}

Return JSON exactly like:
{{
  "role_target": "short description of the role being targeted",
  "companies": ["at most {MAX_COMPANIES} company names"],
  "titles": ["at most {MAX_TITLES} {"job titles to search postings for" if for_jobs else "job titles of people to contact"}"],
  "summary": "one sentence explaining the angle you took"
}}

{JOB_RULES if for_jobs else PEOPLE_RULES}"""


def _clean_list(raw: object, limit: int) -> list[str]:
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, str):
            continue
        value = " ".join(item.split()).strip(' "\'')[:80]
        key = value.lower()
        if value and key not in seen:
            seen.add(key)
            out.append(value)
        if len(out) >= limit:
            break
    return out


def _merge(explicit: list[str], suggested: list[str], limit: int) -> list[str]:
    """Explicit user input always wins and always comes first."""
    out: list[str] = []
    seen: set[str] = set()
    for value in [*explicit, *suggested]:
        value = " ".join(value.split()).strip()
        key = value.lower()
        if value and key not in seen:
            seen.add(key)
            out.append(value)
        if len(out) >= limit:
            break
    return out


def fallback_plan(companies: list[str], titles: list[str], role: str) -> SearchPlan:
    """Used when OpenRouter is unavailable — the user's own input, unexpanded."""
    return SearchPlan(
        role_target=role.strip(),
        companies=_merge(companies, [], MAX_COMPANIES),
        titles=_merge(titles or DEFAULT_TITLES, [], MAX_TITLES),
        summary="Planned directly from your input (the sourcing model was unavailable).",
        agent_used=False,
    )


async def plan_search(
    resume: str, companies: list[str], titles: list[str], role: str = "",
    for_jobs: bool = False, profile_text: str = "",
) -> tuple[SearchPlan, list[str]]:
    """Return (plan, warnings). Never raises — falls back to the user's own input."""
    companies = [c.strip() for c in companies if c.strip()]
    titles = [t.strip() for t in titles if t.strip()]

    try:
        raw = await chat(
            messages=[
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": _prompt(resume, companies, titles, role, for_jobs, profile_text)},
            ],
            max_tokens=900,
        )
        data = parse_json(raw)
    except OpenRouterError as exc:
        plan = fallback_plan(companies, titles, role)
        if not plan.companies:
            return plan, [f"Sourcing model unavailable ({exc}) and no companies were given."]
        return plan, [f"Sourcing model unavailable ({exc}) — used your companies and titles as-is."]

    if not isinstance(data, dict):
        return fallback_plan(companies, titles, role), ["Sourcing model returned an unexpected shape."]

    merged_companies = _merge(companies, _clean_list(data.get("companies"), MAX_COMPANIES), MAX_COMPANIES)
    merged_titles = _merge(titles, _clean_list(data.get("titles"), MAX_TITLES), MAX_TITLES)
    if not merged_titles:
        merged_titles = [role.strip()] if (for_jobs and role.strip()) else DEFAULT_TITLES.copy()

    role_target = data.get("role_target")
    summary = data.get("summary")

    return (
        SearchPlan(
            role_target=role.strip() or (role_target.strip()[:160] if isinstance(role_target, str) else ""),
            companies=merged_companies,
            titles=merged_titles,
            summary=summary.strip()[:300] if isinstance(summary, str) else "",
            agent_used=True,
        ),
        [],
    )
