"""Sourcing open roles you can actually apply to.

People-search finds humans to ask for a referral. This finds the postings
themselves — the ones with a real application form at the end of them.

Sources are applicant tracking systems the companies publish to directly:
Greenhouse, Lever and Ashby. Those pages are the company's own posting, are
publicly indexed, and open straight onto an apply form with no login. LinkedIn
job pages are included last because they are frequently gated or expired, so a
link that demands a login is the worst of the four to hand someone.
"""

from __future__ import annotations

import asyncio
import re
from urllib.parse import urlparse

import httpx

import meter
from config import SERPER_API_KEY, SERPER_BASE_URL, SERPER_MAX_RESULTS
from schemas import Opening
from search import SearchError, _serper_message

MAX_CONCURRENCY = 8
# Same escalation as people search: dig past page 1 only when what came back is
# already-seen, because each extra page costs a credit per query.
MAX_PAGES = 3

# Ordered best-first: a direct ATS apply form beats a LinkedIn page that may
# ask the user to sign in before showing anything.
SOURCES = [
    ("greenhouse", "job-boards.greenhouse.io"),
    ("greenhouse", "boards.greenhouse.io"),
    ("lever", "jobs.lever.co"),
    ("ashby", "jobs.ashbyhq.com"),
    ("linkedin", "linkedin.com/jobs/view"),
    # Last resort for companies whose careers site is in-house: no site filter,
    # so these are liveness-checked before anyone sees them.
    ("web", ""),
]
# Without an explicit choice, search the ATS boards and LinkedIn as before.
DEFAULT_SOURCES = ("greenhouse", "lever", "ashby", "linkedin")

# Titles arrive dressed in board branding:
#   "Software Engineer, Intern - Stripe Careers"
#   "Software Engineer Intern (Summer 2027) @ Notion - Jobs"
#   "Shield AI - Summer 2027 - Software Engineer Intern - Lever"
_BOARD_TRAILER = re.compile(
    r"\s*[-|@]\s*(?:lever|linkedin|greenhouse|ashby)\s*$", re.IGNORECASE)
# " - <Company> Careers" / " | <Company> Jobs", capturing the company.
_CAREERS_TRAILER = re.compile(
    r"\s*[-|]\s*([\w.&' ]{1,40}?)\s+(?:careers?|jobs?)\s*$", re.IGNORECASE)
_BARE_TRAILER = re.compile(r"\s*[-|]\s*(?:careers?|jobs?)\s*$", re.IGNORECASE)
_AT_COMPANY = re.compile(r"\s+(?:@|at)\s+([A-Z][\w.&' -]{1,40})\s*$")
# Greenhouse serves some pages as "Job Application for <Role>".
_APPLICATION_PREFIX = re.compile(r"^job application for\s+", re.IGNORECASE)
# Slugs bolt the hiring programme onto the employer: "duolingounirecruitment".
_SLUG_SUFFIXES = ("unirecruitment", "universityrecruitment", "universityrecruiting",
                  "campusrecruiting", "recruitment", "recruiting", "campus",
                  "careers", "jobs", "hq", "inc")


def _query(domain: str, role: str, company: str = "") -> str:
    if not domain:
        return f'"{company}" "{role}" careers apply -site:linkedin.com -site:naukri.com -site:indeed.com'
    return f'site:{domain} "{role}"' + (f' "{company}"' if company else "")


def build_queries(roles: list[str], companies: list[str], limit: int = 24,
                  sources: tuple[str, ...] = DEFAULT_SOURCES) -> list[tuple[str, str]]:
    """(source, query) pairs. Companies are optional — without them this finds
    openings anywhere, which is the point when you do not yet have a shortlist."""
    specs: list[tuple[str, str]] = []
    roles = [r.strip() for r in roles if r.strip()]
    companies = [c.strip() for c in companies if c.strip()]

    for source, domain in SOURCES:
        if source not in sources or (not domain and not companies):
            continue
        for role in roles:
            if companies:
                for company in companies:
                    specs.append((source, _query(domain, role, company)))
                    if len(specs) >= limit:
                        return specs
            else:
                specs.append((source, _query(domain, role)))
                if len(specs) >= limit:
                    return specs
    return specs


def _company_from(url: str, title: str, source: str) -> str:
    """ATS URLs carry the employer in the path: /stripe/jobs/8031833."""
    try:
        parts = [p for p in urlparse(url).path.split("/") if p]
    except ValueError:
        parts = []
    if source in ("greenhouse", "lever", "ashby") and parts:
        slug = parts[0].strip().lower()
        if slug in ("jobs", "job", "careers"):
            return ""
        # "duolingounirecruitment" -> "duolingo"; keep the employer, drop the
        # programme name the board tacked on.
        for suffix in _SLUG_SUFFIXES:
            if slug.endswith(suffix) and len(slug) > len(suffix) + 2:
                slug = slug[: -len(suffix)]
                break
        slug = slug.replace("-", " ").replace("_", " ").strip()
        if slug:
            return slug.title()
    found = _AT_COMPANY.search(title)
    return found.group(1).strip() if found else ""


def _clean_title(raw: str, source: str) -> tuple[str, str]:
    """Return (title, company_from_title). Boards brand their page titles, and
    the branding often carries the employer name — worth keeping, not just
    deleting."""
    title = _APPLICATION_PREFIX.sub("", (raw or "").strip())
    title = _BOARD_TRAILER.sub("", title)

    company = ""
    found = _CAREERS_TRAILER.search(title)
    if found:
        company = found.group(1).strip()
        title = title[: found.start()].strip()
    else:
        title = _BARE_TRAILER.sub("", title).strip()

    found = _AT_COMPANY.search(title)
    if found:
        company = company or found.group(1).strip()
        title = _AT_COMPANY.sub("", title).strip()

    # Lever titles lead with the employer: "Shield AI - Summer 2027 - SWE Intern".
    if source == "lever" and " - " in title:
        head, rest = title.split(" - ", 1)
        if rest.strip() and len(head) <= 40:
            company = company or head.strip()
            title = rest.strip()

    return title.strip(" -|@"), company


def _parse(item: dict, source: str, query: str) -> Opening | None:
    url = (item.get("link") or "").strip()
    if not url.startswith("http"):
        return None

    title, from_title = _clean_title(item.get("title") or "", source)
    # A name written out in the title beats a squashed URL slug ("shieldai").
    company = from_title or _company_from(url, title, source)
    if source == "web":
        # An unrestricted search was for one named company; trust that over
        # whatever the page title claims.
        quoted = re.findall(r'"([^"]+)"', query)
        company = quoted[0] if quoted else company
    if not title:
        return None

    return Opening(
        title=title[:160],
        company=company[:80],
        source=source,
        url=url,
        snippet=(item.get("snippet") or "").strip()[:400],
        query=query,
    )


async def _run(client: httpx.AsyncClient, source: str, query: str, num: int,
               page: int = 1) -> list[Opening]:
    body: dict = {"q": query, "num": max(1, min(num, SERPER_MAX_RESULTS))}
    if page > 1:                      # page 1 keeps the exact known-good shape
        body["page"] = page

    meter.current().serper_calls += 1
    resp = await client.post(
        f"{SERPER_BASE_URL}/search",
        headers={"X-API-KEY": SERPER_API_KEY, "Content-Type": "application/json"},
        json=body,
    )
    if resp.status_code in (401, 403):
        raise SearchError("Serper rejected the API key (check SERPER_API_KEY).")
    if resp.status_code == 429:
        raise SearchError("Serper rate limit / quota exhausted.")
    if resp.status_code >= 400:
        raise SearchError(f"Serper returned {resp.status_code}: {_serper_message(resp)}")

    found = [_parse(item, source, query) for item in (resp.json().get("organic") or [])]
    return [o for o in found if o is not None]


async def find_openings(
    roles: list[str],
    companies: list[str],
    per_query_results: int = 10,
    max_openings: int = 30,
    exclude: frozenset[str] = frozenset(),
    max_queries: int = 24,
    sources: tuple[str, ...] = DEFAULT_SOURCES,
) -> tuple[list[Opening], list[str], int]:
    """Return (openings, queries actually run, skipped_as_already_seen).

    Deduped by URL, and postings this user has already been shown are dropped
    so a repeat run surfaces something new rather than the same board page.
    """
    if not SERPER_API_KEY:
        raise SearchError("SERPER_API_KEY is not set — copy .env.example to .env and fill it in.")

    specs = build_queries(roles, companies, max_queries, sources)
    if not specs:
        return [], [], 0

    openings: list[Opening] = []
    seen: set[str] = set()
    skipped = 0
    first_error: BaseException | None = None
    ran: list[str] = []

    # Same wave-and-stop as people search: every query is a paid credit.
    async with httpx.AsyncClient(timeout=20.0) as client:
        for page in range(1, MAX_PAGES + 1):
            page_yield = 0

            for start in range(0, len(specs), MAX_CONCURRENCY):
                wave = specs[start : start + MAX_CONCURRENCY]
                ran.extend(q for _s, q in wave)
                results = await asyncio.gather(
                    *(_run(client, s, q, per_query_results, page) for s, q in wave),
                    return_exceptions=True,
                )
                for batch in results:
                    if isinstance(batch, BaseException):
                        first_error = first_error or batch
                        continue
                    for opening in batch:
                        if opening.url in seen:
                            continue
                        seen.add(opening.url)
                        if opening.url in exclude:
                            skipped += 1
                            continue
                        openings.append(opening)
                        page_yield += 1
                if len(openings) >= max_openings:
                    break

            if len(openings) >= max_openings:
                break
            if page_yield == 0 and not skipped:
                break

    if not openings and first_error is not None:
        raise SearchError(f"All job searches failed: {first_error}")

    return openings[:max_openings], ran, skipped
