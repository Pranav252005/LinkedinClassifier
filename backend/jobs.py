"""Sourcing open roles you can actually apply to.

People-search finds humans to ask for a referral. This finds the postings
themselves — the ones with a real application form at the end of them.

Sources are applicant tracking systems the companies publish to directly:
Greenhouse, Lever and Ashby. Those pages are the company's own posting, are
publicly indexed, and open straight onto an apply form with no login. LinkedIn
job pages are included last because they are frequently gated or expired, so a
link that demands a login is the worst of the four to hand someone.

Job sites that offer no API of their own -- Naukri, Indeed, Glassdoor -- and
Upwork's freelance gigs are reached the same way, through the pages search
engines have indexed. Indeed, Glassdoor and Upwork refuse automated visits, so
their links cannot be confirmed open and are labelled as such.
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import httpx

import filters
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
    ("naukri", "naukri.com/job-listings"),
    ("indeed", "indeed.com/viewjob"),
    ("glassdoor", "glassdoor.com/job-listing"),
    ("upwork", "upwork.com/freelance-jobs/apply"),
    # Last resort for companies whose careers site is in-house: no site filter,
    # so these are liveness-checked before anyone sees them.
    ("web", ""),
]
# Country editions, keyed by ISO-3 code (boards.country_of): Indian postings live
# on in.indeed.com and glassdoor.co.in, not the .com sites.
REGIONAL = {"indeed": {"IND": "in.indeed.com/viewjob", "GBR": "uk.indeed.com/viewjob"},
            "glassdoor": {"IND": "glassdoor.co.in/job-listing", "GBR": "glassdoor.co.uk/job-listing"}}
# The hosts every result of a site-restricted source must be on.
_HOSTS = {"greenhouse": ("greenhouse.io",), "lever": ("jobs.lever.co",), "ashby": ("jobs.ashbyhq.com",),
          "linkedin": ("linkedin.com",), "naukri": ("naukri.com",), "indeed": ("indeed.com",),
          "glassdoor": ("glassdoor.com", "glassdoor.co.in", "glassdoor.co.uk"), "upwork": ("upwork.com",)}
# Without an explicit choice, search the ATS boards and LinkedIn as before.
DEFAULT_SOURCES = ("greenhouse", "lever", "ashby", "linkedin")

# Titles arrive dressed in board branding:
#   "Software Engineer, Intern - Stripe Careers"
#   "Software Engineer Intern (Summer 2027) @ Notion - Jobs"
#   "Shield AI - Summer 2027 - Software Engineer Intern - Lever"
_BOARD_TRAILER = re.compile(
    r"\s*[-|@]\s*(?:lever|linkedin(?:\s+\S+)?|greenhouse|ashby)\s*$", re.IGNORECASE)
# " - <Company> Careers" / " | <Company> Jobs", capturing the company.
_CAREERS_TRAILER = re.compile(
    r"\s*[-|]\s*([\w.&' ]{1,40}?)\s+(?:careers?|jobs?)\s*$", re.IGNORECASE)
_BARE_TRAILER = re.compile(r"\s*[-|]\s*(?:careers?|jobs?)\s*$", re.IGNORECASE)
_AT_COMPANY = re.compile(r"\s+(?:@|at)\s+([A-Z][\w.&' -]{1,40})\s*$")
# LinkedIn job pages: "Acme hiring Data Intern in Bengaluru, India" and
# "Data Intern at Acme — Bengaluru".
_LINKEDIN_HIRING = re.compile(r"^(?P<company>.{1,60}?)\s+hiring\s+(?P<title>.+?)(?:\s+in\s+(?P<place>[^|]+))?$",
                              re.IGNORECASE)
_LINKEDIN_AT = re.compile(r"^(?P<title>.+?)\s+at\s+(?P<company>[^—–|]{1,60}?)\s*(?:[—–]\s*(?P<place>.+))?$")
# The job's country is in LinkedIn's subdomain: in.linkedin.com is India.
_LINKEDIN_COUNTRIES = {
    "in": "India", "uk": "United Kingdom", "ca": "Canada", "au": "Australia", "sg": "Singapore",
    "de": "Germany", "fr": "France", "nl": "Netherlands", "ie": "Ireland", "es": "Spain", "it": "Italy",
    "pl": "Poland", "hu": "Hungary", "br": "Brazil", "mx": "Mexico", "ae": "United Arab Emirates",
    "jp": "Japan", "cn": "China", "hk": "Hong Kong", "ph": "Philippines", "my": "Malaysia", "za": "South Africa",
    "pk": "Pakistan", "bd": "Bangladesh", "lk": "Sri Lanka", "np": "Nepal", "tr": "Turkey", "se": "Sweden",
    "ch": "Switzerland", "be": "Belgium", "pt": "Portugal", "ro": "Romania", "cz": "Czechia",
    "id": "Indonesia", "vn": "Vietnam", "th": "Thailand", "kr": "South Korea", "tw": "Taiwan", "nz": "New Zealand",
    "sa": "Saudi Arabia", "qa": "Qatar", "eg": "Egypt", "ng": "Nigeria", "ke": "Kenya", "ar": "Argentina",
    "cl": "Chile", "co": "Colombia", "pe": "Peru", "at": "Austria", "dk": "Denmark", "no": "Norway", "fi": "Finland",
    "gr": "Greece", "il": "Israel", "ua": "Ukraine", "tv": "Tuvalu",
}
# /jobs/view/backend-intern-at-uplabh-4439046038
_LINKEDIN_URL_COMPANY = re.compile(r"/jobs/view/[^/?]*?-at-([a-z0-9-]+?)-\d{6,}")
# Greenhouse serves some pages as "Job Application for <Role>".
_APPLICATION_PREFIX = re.compile(r"^job application for\s+", re.IGNORECASE)
# Slugs bolt the hiring programme onto the employer: "duolingounirecruitment".
_SLUG_SUFFIXES = ("unirecruitment", "universityrecruitment", "universityrecruiting",
                  "campusrecruiting", "recruitment", "recruiting", "campus",
                  "careers", "jobs", "hq", "inc")


def _query(domain: str, role: str, company: str = "", where: str = "") -> str:
    place = f' "{where}"' if where else ""
    if not domain:
        return f'"{company}" "{role}"{place} careers apply -site:linkedin.com -site:naukri.com -site:indeed.com'
    return f'site:{domain} "{role}"' + (f' "{company}"' if company else "") + place


def build_queries(roles: list[str], companies: list[str], limit: int = 24,
                  sources: tuple[str, ...] = DEFAULT_SOURCES, where: str = "",
                  country: str | None = None) -> list[tuple[str, str]]:
    """(source, query) pairs. Companies are optional — without them this finds
    openings anywhere, which is the point when you do not yet have a shortlist.
    `where` pins every query to one place ("Bengaluru")."""
    specs: list[tuple[str, str]] = []
    roles = [r.strip() for r in roles if r.strip()]
    companies = [c.strip() for c in companies if c.strip()]

    for source, domain in SOURCES:
        if source not in sources or (not domain and not companies):
            continue
        domain = REGIONAL.get(source, {}).get(country or "", domain)
        for role in roles:
            if companies:
                for company in companies:
                    specs.append((source, _query(domain, role, company, where)))
                    if len(specs) >= limit:
                        return specs
            else:
                specs.append((source, _query(domain, role, where=where)))
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
    if source == "linkedin":
        found = _LINKEDIN_HIRING.match(title) or _LINKEDIN_AT.match(title)
        if found:
            return found.group("title").strip(" -|"), found.group("company").strip()

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


_NAUKRI_SNIPPET = re.compile(r"Job Description for (?P<title>.+?) in (?P<company>.+?) in ?(?P<place>.*?) ?for \d")
_SITE_TRAILER = re.compile(r"\s*[-|]\s*(?:naukri\.com|naukri|indeed(?:\.com)?|glassdoor|upwork)\s*$", re.I)
_EXPERIENCE = re.compile(r"^\d+\s*(?:to|-)\s*\d+\s*years?", re.I)
_GLASSDOOR_HIRING = re.compile(r"^(?P<company>.{1,60}?)\s+hiring\s+(?P<title>.+?)\s+Job(?:\s+in\s+(?P<place>.*))?$")


def _site_details(source: str, raw_title: str, snippet: str) -> tuple[str, str, str]:
    """(title, company, location) for the job sites without an API. Any may be empty."""
    title = _SITE_TRAILER.sub("", (raw_title or "").strip()).rstrip(" .…")
    if source == "naukri":
        # "Software Engineer Intern - Bengaluru - Acme - 0 to 2 years of experience";
        # the snippet says the same in a fixed sentence, which is easier to trust.
        found = _NAUKRI_SNIPPET.search(snippet or "")
        if found:
            return found.group("title"), found.group("company"), found.group("place")
        parts = [p.strip(" -") for p in re.split(r"\s+-\s+", title)]
        parts = [p for p in parts if p and not _EXPERIENCE.match(p)]
        if len(parts) >= 3:
            return parts[0], parts[2], parts[1]
        if len(parts) == 2:
            # "Title - Bengaluru" or "Title - Acme": only a known place is a place.
            second = parts[1]
            if any(second.lower() in names for names in filters._ALIASES.values()):
                return parts[0], "", second
            return parts[0], second, ""
        return title, "", ""
    if source == "glassdoor":
        found = _GLASSDOOR_HIRING.match(title)
        if found:
            return found.group("title"), found.group("company"), (found.group("place") or "").strip()
        head, _, tail = title.rpartition(" - ")     # "Title - Company"
        return (head, tail, "") if head else (title, "", "")
    if source == "indeed":
        # "Software Engineer Intern - Bengaluru, Karnataka"
        head, _, tail = title.rpartition(" - ")
        return (head, "", tail) if head and "," in tail else (title, "", "")
    if source == "upwork":
        title = re.sub(r"\s*-\s*Freelance Job in .*$", "", title, flags=re.I)
    return title, "", ""


_AGO = re.compile(r"(\d+)\s+(hour|day|week|month)s?\s+ago", re.I)


def search_date(text: str | None, today: datetime | None = None) -> str | None:
    """The date a search result shows ("2 days ago", "Sep 16, 2026", "2026年8月12日") as YYYY-MM-DD."""
    text = (text or "").strip()
    if not text:
        return None
    now = today or datetime.now(timezone.utc)
    found = _AGO.search(text)
    if found:
        n, unit = int(found.group(1)), found.group(2).lower()
        days = {"hour": 0, "day": n, "week": 7 * n, "month": 30 * n}[unit]
        return (now - timedelta(days=days)).date().isoformat()
    found = re.match(r"(\d{4})年(\d{1,2})月(\d{1,2})日", text)
    if found:
        return f"{found.group(1)}-{int(found.group(2)):02d}-{int(found.group(3)):02d}"
    for fmt in ("%b %d, %Y", "%B %d, %Y", "%d %b %Y"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _linkedin_details(url: str, raw_title: str) -> tuple[str, str]:
    """(company, location) from a LinkedIn job URL and page title, either may be empty."""
    company, place = "", ""
    found = _LINKEDIN_URL_COMPANY.search(url)
    if found:
        company = found.group(1).replace("-", " ").title()
    title = _BOARD_TRAILER.sub("", (raw_title or "").strip())
    found = _LINKEDIN_HIRING.match(title) or _LINKEDIN_AT.match(title)
    if found and found.group("place"):
        place = found.group("place").strip(" |")
    if not place:
        host = (urlparse(url).hostname or "").lower()
        sub = host.split(".")[0] if host.endswith(".linkedin.com") else ""
        # www is the US and worldwide edition. Any other two-letter edition is a
        # country; an unlisted one is still named, so a location filter can drop it.
        place = _LINKEDIN_COUNTRIES.get(sub, sub.upper() if len(sub) == 2 else "")
    return company, place


def _parse(item: dict, source: str, query: str) -> Opening | None:
    url = (item.get("link") or "").strip()
    if not url.startswith("http"):
        return None
    # Search engines sometimes ignore site:, and a Reddit thread is not a job.
    host, want = (urlparse(url).hostname or "").lower(), _HOSTS.get(source, ())
    if want and not any(host == w or host.endswith("." + w) for w in want):
        return None

    raw = item.get("title") or ""
    title, from_title = _clean_title(raw, source)
    # A name written out in the title beats a squashed URL slug ("shieldai").
    company = from_title or _company_from(url, title, source)
    location = ""
    if source == "linkedin":
        from_url, location = _linkedin_details(url, raw)
        company = company or from_url
    elif source in ("naukri", "indeed", "glassdoor", "upwork"):
        title, company, location = _site_details(source, raw, item.get("snippet") or "")
    if company.strip().lower() in ("linkedin", "linkedin jobs"):
        company = ""                            # the site, not the employer
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
        location=location[:200],
        snippet=(item.get("snippet") or "").strip()[:400],
        posted_at=search_date(item.get("date")),
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
    where: str = "",
    country: str | None = None,
) -> tuple[list[Opening], list[str], int]:
    """Return (openings, queries actually run, skipped_as_already_seen).

    Deduped by URL, and postings this user has already been shown are dropped
    so a repeat run surfaces something new rather than the same board page.
    """
    if not SERPER_API_KEY:
        raise SearchError("SERPER_API_KEY is not set — copy .env.example to .env and fill it in.")

    specs = build_queries(roles, companies, max_queries, sources, where, country)
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
