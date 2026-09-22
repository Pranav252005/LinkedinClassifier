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


def _prompt(resume: str, companies: list[str], titles: list[str], role: str) -> str:
    return f"""Given this job seeker's resume, build a plan for finding people worth reaching out to.

### Resume
{resume.strip()[:8000]}

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
  "titles": ["at most {MAX_TITLES} job titles of people to contact"],
  "summary": "one sentence explaining the angle you took"
}}

Rules:
- Always include every company and title the seeker explicitly named, first.
- Only add companies that plausibly hire this person's profile and seniority.
- Titles should be roles that can refer, screen, or hire — recruiters, hiring
  managers, team leads, and engineers on the relevant team. Not executives.
- Use the plain title wording that appears on real LinkedIn profiles.
- No commentary outside the JSON."""


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
    resume: str, companies: list[str], titles: list[str], role: str = ""
) -> tuple[SearchPlan, list[str]]:
    """Return (plan, warnings). Never raises — falls back to the user's own input."""
    companies = [c.strip() for c in companies if c.strip()]
    titles = [t.strip() for t in titles if t.strip()]

    try:
        raw = await chat(
            messages=[
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": _prompt(resume, companies, titles, role)},
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
        merged_titles = DEFAULT_TITLES.copy()

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
