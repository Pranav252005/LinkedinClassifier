"""People worth asking about one specific opening.

The best referral is not a random recruiter at a company; it is someone on the
team that has an opening right now. So this starts from a posting and searches
for the people around it: the recruiter for that kind of role, the manager of
that department, engineers on that team -- and, ranked up, anyone who went to
the seeker's college or works in the posting's city.

Alumni and location are read from the public search snippet only. They are
signals, not facts: a snippet that mentions the college is shown as "alumni",
never asserted in a message unless the seeker confirms it.
"""

from __future__ import annotations

import re

from schemas import Candidate, Profile, ScoredCandidate
from search import QuerySpec

# Who to look for, by the kind of role the opening is.
_RECRUITERS = {
    "intern": ["university recruiter", "campus recruiter", "early careers recruiter"],
    "entry": ["university recruiter", "campus recruiter", "technical recruiter"],
}
_DEFAULT_RECRUITERS = ["technical recruiter", "talent acquisition"]
_TEAM_STOPWORDS = re.compile(r"\b(\d{3,}|the|and|of|team|dept|department|group|org)\b", re.I)


def _team(department: str, title: str) -> str:
    """A short team phrase: "8611 Security Analytics" -> "Security Analytics"."""
    dept = _TEAM_STOPWORDS.sub(" ", department.split("/")[-1].split(",")[0])
    dept = " ".join(dept.split())
    if len(dept) >= 3:
        return dept[:40]
    # Fall back to the discipline in the title: "Backend Engineer Intern" -> "Backend".
    words = re.findall(r"[A-Za-z+#]+", title)
    return words[0] if words else ""


def _city(location: str) -> str:
    first = re.split(r"[;,/|]", location or "")[0].strip()
    return "" if first.lower() in ("remote", "anywhere", "") else first[:40]


def build_queries(company: str, title: str, department: str, location: str, level: str,
                  college: str = "") -> list[QuerySpec]:
    """Queries around one opening, most useful first."""
    company = company.strip()
    team = _team(department, title)
    city = _city(location)
    specs: list[QuerySpec] = []

    def add(query_title: str, extra: str = "") -> None:
        q = f'site:linkedin.com/in "{company}" "{query_title}"' + (f' "{extra}"' if extra else "")
        specs.append(QuerySpec(query=q, company=company, title=query_title))

    if college:
        # Alumni at the company first: the warmest possible cold message.
        specs.append(QuerySpec(query=f'site:linkedin.com/in "{company}" "{college}"',
                               company=company, title=f"{college} alumni"))
    for r in _RECRUITERS.get(level, _DEFAULT_RECRUITERS)[:2]:
        add(r)
    technical = bool(re.search(r"engineer|developer|sde|swe|scientist|data|ml|ai\b", title, re.I))
    if team:
        add("engineering manager" if technical else "manager", team)
        add("software engineer" if technical else team, team if technical else "")
    else:
        add("hiring manager")
    if city:
        add("recruiter", city)
    return specs[:6]


def _mentions(text: str, phrase: str) -> bool:
    """Does the snippet mention this college, by name or by its usual acronym?

    "Indian Institute of Technology Delhi" is written "IIT Delhi" on most
    profiles, so the acronym of all but the last word is checked as well.
    """
    phrase = " ".join((phrase or "").lower().split())
    if len(phrase) < 3:
        return False
    text = " ".join(text.lower().split())
    if phrase in text:
        return True
    words = [w for w in re.findall(r"[a-z]+", phrase) if w not in ("of", "the", "and", "at", "for")]
    if len(words) >= 3:
        acronym = "".join(w[0] for w in words[:-1])
        return f"{acronym} {words[-1]}" in text or f"{acronym}-{words[-1]}" in text
    return False


def annotate(candidates: list[ScoredCandidate], profile: Profile | None, location: str = "") -> None:
    """Mark alumni and location matches, and nudge the score for them.

    The nudge is small on purpose: a plausible recruiter who is not an alum
    should still outrank an alum in an unrelated department.
    """
    college = (profile.college if profile else "") or ""
    wanted = [_city(location)] if location else []
    if profile:
        wanted += profile.locations
    for c in candidates:
        text = f"{c.headline} {c.snippet}"
        c.alumni = bool(college) and _mentions(text, college)
        c.location_match = any(w and w.lower() in text.lower() for w in wanted)
        if c.fit_score is not None:
            c.fit_score = min(100, c.fit_score + (8 if c.alumni else 0) + (3 if c.location_match else 0))


def rank(candidates: list[ScoredCandidate]) -> list[ScoredCandidate]:
    return sorted(candidates, key=lambda s: (s.fit_score is None, -(s.fit_score or 0), not s.alumni, s.name))


__all__ = ["build_queries", "annotate", "rank", "Candidate"]
