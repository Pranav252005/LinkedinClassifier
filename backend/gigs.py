"""Freelance gigs: Upwork projects that fit the seeker's skills.

Upwork's own API needs an approved developer application, so gigs are found the
way job-site postings are: through the gig pages search engines have indexed,
one search per skill or role. Upwork refuses automated visits, so a gig cannot
be confirmed open; every one is marked unconfirmed, and the date the search
result shows is used to drop the stale ones, since gigs fill in days.

The job filters (level, years, location) do not apply: a gig is remote, paid,
and open to anyone who can do the work.
"""

from __future__ import annotations

import re

import filters
from jobs import find_openings
from openings import role_terms
from schemas import Profile, ScoredOpening

MAX_TERMS = 6              # one search each
MAX_AGE_DAYS = 21          # gigs are usually awarded within days
_AMOUNT = r"\$\d[\d,]*(?:\.\d+)?"
_BUDGET = re.compile(rf"(Hourly[^.$]{{0,40}}{_AMOUNT}(?:\s*-\s*{_AMOUNT})?|Fixed[- ]price[^.$]{{0,40}}{_AMOUNT}|"
                     rf"Budget:?\s*{_AMOUNT})", re.I)


def terms(profile: Profile, titles: list[str]) -> list[str]:
    """What to search Upwork for: the seeker's own words first, then skills,
    then their target roles without level words ("intern" finds no gigs)."""
    out = [t for t in titles if t.strip()]
    out += [s for s in profile.skills if 2 <= len(s) <= 30][:4]
    out += role_terms(profile.target_roles)
    return list(dict.fromkeys(t.strip() for t in out))[:MAX_TERMS]


def budget(snippet: str) -> str:
    found = _BUDGET.search(snippet or "")
    return " ".join(found.group(1).split()) if found else ""


async def find(profile: Profile, titles: list[str], exclude: frozenset[str], limit: int
               ) -> tuple[list[ScoredOpening], list[str], int, int]:
    """Returns (gigs, queries run, skipped as already shown, dropped as stale)."""
    wanted = terms(profile, titles)
    if not wanted:
        return [], [], 0, 0
    found, queries, skipped = await find_openings(wanted, [], 10, 3 * limit, exclude,
                                                  max_queries=len(wanted), sources=("upwork",))
    gigs, stale = [], 0
    for o in found:
        age = filters.age_days(o.posted_at)
        if age is not None and age > MAX_AGE_DAYS:
            stale += 1
            continue
        gigs.append(ScoredOpening(**{
            **o.model_dump(), "key": o.url, "company": "Upwork client", "department": budget(o.snippet),
            "description": o.snippet, "pay": "paid", "work_mode": "remote", "remote": True,
            "verified": False, "unconfirmed": True}))
    return gigs, queries, skipped, stale
