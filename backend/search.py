"""Candidate sourcing: public Google results via Serper.dev.

This reads publicly indexed search results. It never logs into LinkedIn and
never scrapes profile pages.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from urllib.parse import urlparse, urlunparse

import httpx

from config import SERPER_API_KEY, SERPER_BASE_URL, SERPER_MAX_RESULTS
from schemas import Candidate

# "Jane Doe - University Recruiter - Acme Corp | LinkedIn"
_TITLE_SPLIT = re.compile(r"\s+[-–|]\s+")
_PROFILE_PATH = re.compile(r"^/(?:[a-z]{2,3}/)?in/[^/]+/?$", re.IGNORECASE)

MAX_CONCURRENCY = 8


class SearchError(RuntimeError):
    """Raised when sourcing cannot proceed at all (bad key, network, quota)."""


@dataclass(frozen=True)
class QuerySpec:
    """One Serper query, plus the company/title it came from."""

    query: str
    company: str
    title: str


def build_queries(companies: list[str], titles: list[str], limit: int = 40) -> list[QuerySpec]:
    """Cartesian product of companies x titles as site-restricted queries."""
    specs: list[QuerySpec] = []
    for company in companies:
        company = company.strip()
        if not company:
            continue
        for title in titles:
            title = title.strip()
            if not title:
                continue
            specs.append(
                QuerySpec(
                    query=f'site:linkedin.com/in "{company}" "{title}"',
                    company=company,
                    title=title,
                )
            )
            if len(specs) >= limit:
                return specs
    return specs


def _canonical_url(url: str) -> str | None:
    """Normalize a LinkedIn profile URL, or return None if it isn't one."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    if "linkedin.com" not in parsed.netloc.lower():
        return None
    path = parsed.path.rstrip("/")
    if not _PROFILE_PATH.match(path + "/"):
        return None
    return urlunparse(("https", "www.linkedin.com", path, "", "", ""))


def _parse_result(item: dict, company: str, title: str) -> Candidate | None:
    url = _canonical_url(item.get("link", "") or "")
    if not url:
        return None

    raw_title = (item.get("title") or "").strip()
    raw_title = re.sub(r"\s*\|\s*LinkedIn\s*$", "", raw_title, flags=re.IGNORECASE)
    parts = [p.strip() for p in _TITLE_SPLIT.split(raw_title) if p.strip()]

    name = parts[0] if parts else url.rsplit("/", 1)[-1].replace("-", " ").title()
    headline = " - ".join(parts[1:]) if len(parts) > 1 else ""

    return Candidate(
        name=name,
        headline=headline,
        snippet=(item.get("snippet") or "").strip(),
        linkedin_url=url,
        company_query=company,
        title_query=title,
    )


def _serper_message(resp: httpx.Response) -> str:
    """Serper explains itself in the body; httpx's status text does not."""
    try:
        payload = resp.json()
    except ValueError:
        return resp.text[:200].strip()
    if isinstance(payload, dict):
        for key in ("message", "error", "detail"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return resp.text[:200].strip()


async def _run_query(
    client: httpx.AsyncClient, api_key: str, spec: QuerySpec, num: int
) -> list[Candidate]:
    if not spec.query.strip():
        raise SearchError("Refusing to send an empty search query.")

    resp = await client.post(
        f"{SERPER_BASE_URL}/search",
        headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
        # Clamped: a free key 400s above SERPER_MAX_RESULTS, and one rejected
        # query fails the whole run.
        json={"q": spec.query, "num": max(1, min(num, SERPER_MAX_RESULTS))},
    )
    if resp.status_code in (401, 403):
        raise SearchError("Serper rejected the API key (check SERPER_API_KEY).")
    if resp.status_code == 429:
        raise SearchError("Serper rate limit / quota exhausted.")
    if resp.status_code >= 400:
        # Pass Serper's own wording through — it names the actual problem,
        # e.g. "Query pattern not allowed for free accounts."
        raise SearchError(f"Serper returned {resp.status_code}: {_serper_message(resp)}")

    organic = resp.json().get("organic") or []
    found = [_parse_result(item, spec.company, spec.title) for item in organic]
    return [c for c in found if c is not None]


async def run_queries(
    specs: list[QuerySpec], per_query_results: int = 10, max_candidates: int = 40
) -> tuple[list[Candidate], list[QuerySpec]]:
    """Run queries until enough candidates are found.

    Returns (candidates, specs_actually_run) — the second differs from the input
    whenever the early stop kicks in, and callers report it so the UI does not
    claim credits that were never spent.
    """
    if not SERPER_API_KEY:
        raise SearchError("SERPER_API_KEY is not set — copy .env.example to .env and fill it in.")
    if not specs:
        return [], []

    candidates: list[Candidate] = []
    seen: set[str] = set()
    first_error: BaseException | None = None
    ran: list[QuerySpec] = []

    # Run in waves and stop once we have enough. Every query costs a Serper
    # credit, and the planner routinely produces far more company x title pairs
    # than a run needs — issuing all of them would spend the budget to throw
    # most of the results away.
    async with httpx.AsyncClient(timeout=20.0) as client:
        for start in range(0, len(specs), MAX_CONCURRENCY):
            wave = specs[start : start + MAX_CONCURRENCY]
            ran.extend(wave)
            results = await asyncio.gather(
                *(_run_query(client, SERPER_API_KEY, spec, per_query_results) for spec in wave),
                return_exceptions=True,
            )
            for batch in results:
                if isinstance(batch, BaseException):
                    first_error = first_error or batch
                    continue
                for candidate in batch:
                    if candidate.linkedin_url in seen:
                        continue
                    seen.add(candidate.linkedin_url)
                    candidates.append(candidate)
            if len(candidates) >= max_candidates:
                break

    if not candidates and first_error is not None:
        raise SearchError(f"All search queries failed: {first_error}")

    return candidates[:max_candidates], ran


async def find_candidates(
    companies: list[str],
    titles: list[str],
    per_query_results: int = 10,
    max_candidates: int = 40,
) -> tuple[list[Candidate], list[QuerySpec]]:
    """Convenience path: build queries from companies x titles, then run them."""
    specs = build_queries(companies, titles)
    candidates, ran = await run_queries(specs, per_query_results, max_candidates)
    return candidates, ran
