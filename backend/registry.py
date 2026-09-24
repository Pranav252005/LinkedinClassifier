"""Which ATS a company uses, and what its board is called there.

This is the only job search still has for Serper: discovery, once per company.
The answer goes in `company_boards` and every later run reads the board's API
directly.

Discovery, cheapest first:

1. Cache. A found board is kept indefinitely (and re-checked if it ever fails);
   a miss is kept for MISS_TTL_DAYS so a company with an in-house portal does
   not cost a search on every run.
2. Guess. Most boards are named after the company -- "stripe", "notion",
   "palantir". Probing the public APIs with a few spellings costs no credits.
   A guess is accepted only if the board has postings, and, where the API
   reports an employer name, only if that name matches.
3. Search. One Serper query across every supported ATS domain, reading the
   board name out of whichever posting URL comes back. This is also the only
   route to Workday, whose board references (tenant, data centre, site name)
   cannot be guessed.
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import httpx

import boards
import db
import meter
from config import SERPER_API_KEY, SERPER_BASE_URL

MISS_TTL_DAYS = 14
# Order matters only for ties; each is one cheap GET.
GUESS_ATS = ("greenhouse", "lever", "ashby", "workable", "recruitee", "smartrecruiters")

SEARCH_DOMAINS = (
    "job-boards.greenhouse.io", "boards.greenhouse.io", "jobs.lever.co", "jobs.ashbyhq.com",
    "apply.workable.com", "jobs.smartrecruiters.com", "recruitee.com", "myworkdayjobs.com",
)

_WORKDAY_URL = re.compile(
    r"^https?://([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([A-Za-z0-9_-]+)",
)
_SUFFIXES = re.compile(r"\b(inc|llc|ltd|limited|pvt|private|corp|corporation|co|company|"
                       r"technologies|technology|labs|group|hq|india)\b\.?", re.IGNORECASE)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def slug_guesses(company: str) -> list[str]:
    """"Hugging Face, Inc." -> ["huggingface", "hugging-face", "hugging"]"""
    base = _SUFFIXES.sub(" ", company.lower())
    words = re.findall(r"[a-z0-9]+", base)
    if not words:
        return []
    out = ["".join(words), "-".join(words)]
    if len(words) > 1 and len(words[0]) > 3:
        out.append(words[0])
    return list(dict.fromkeys(g for g in out if len(g) >= 2))


def _norm(name: str) -> str:
    return "".join(re.findall(r"[a-z0-9]+", _SUFFIXES.sub(" ", (name or "").lower())))


def names_match(wanted: str, reported: str) -> bool:
    """Loose employer-name check for guessed boards ("Stripe" vs "Stripe, Inc.")."""
    a, b = _norm(wanted), _norm(reported)
    return bool(a and b) and (a == b or a in b or b in a)


def slug_matches(company: str, slug: str) -> bool:
    """Whether a board's name could be this company's: "razorpaysoftwareprivatelimited"
    is Razorpay's, "zomato1" is Zomato's; "philips" is not Flipkart's.

    Search results mention companies in passing (a Philips posting that names
    Flipkart as a partner), so a board is only ever taken on its own name.
    Workday boards are named after the tenant, the first part of the slug.
    """
    a = _norm(company)
    b = _norm(slug.split("|")[0]).rstrip("0123456789")
    if not a or not b:
        return False
    if a == b:
        return True
    # A prefix either way, but not a short fragment: "ola" must not claim "olasolar".
    return (b.startswith(a) and len(a) >= 5) or (a.startswith(b) and len(b) >= 5)


def board_from_url(url: str) -> tuple[str, str] | None:
    """The (ats, slug) a posting URL lives on, or None if it is not an ATS board."""
    match = _WORKDAY_URL.match(url or "")
    if match:
        tenant, host, site = match.groups()
        return "workday", f"{tenant}|{host}|{site}"
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    host = (parsed.netloc or "").lower()
    parts = [p for p in parsed.path.split("/") if p]
    if host.endswith(".recruitee.com") and host.count(".") == 2:
        sub = host.split(".")[0]
        return ("recruitee", sub) if sub not in ("www", "app", "api") else None
    if not parts:
        return None
    first = parts[0]
    if host in ("job-boards.greenhouse.io", "boards.greenhouse.io"):
        if first in ("embed",):
            return None
        return "greenhouse", first
    if host == "jobs.lever.co":
        return "lever", first
    if host == "jobs.ashbyhq.com":
        return "ashby", first
    if host == "jobs.smartrecruiters.com":
        return "smartrecruiters", first
    if host == "apply.workable.com" and first not in ("j", "api"):
        return "workable", first
    return None


async def _probe(client: httpx.AsyncClient, ats: str, slug: str) -> list[boards.Posting]:
    try:
        return await boards.fetch_board(client, ats, slug)
    except boards.BoardError:
        return []
    except Exception:
        return []


async def _guess(company: str) -> tuple[str, str, int] | None:
    guesses = slug_guesses(company)
    if not guesses:
        return None
    async with boards._client() as client:
        for slug in guesses:                       # best spelling first, all ATSes at once
            found = await asyncio.gather(*(_probe(client, ats, slug) for ats in GUESS_ATS))
            for ats, postings in zip(GUESS_ATS, found):
                if not postings:
                    continue
                reported = next((p.company for p in postings if p.company), "")
                # A shared word ("meta", "notion") can be somebody else's board.
                # Where the API names its employer, insist on a match.
                if reported and not names_match(company, reported):
                    continue
                if not reported and slug != guesses[0]:
                    continue                        # a loose spelling with nothing to confirm it
                # The probe already downloaded the whole board; keep it. Lever and
                # Ashby do not name the employer, so label postings with ours.
                for p in postings:
                    p.company = p.company or company
                try:
                    db.upsert_postings(postings)
                except Exception:
                    pass
                return ats, slug, len(postings)
    return None


async def _search(company: str) -> tuple[str, str] | None:
    if not SERPER_API_KEY:
        return None
    sites = " OR ".join(f"site:{d}" for d in SEARCH_DOMAINS)
    query = f'"{company}" ({sites})'
    meter.current().serper_calls += 1
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(f"{SERPER_BASE_URL}/search",
                                     headers={"X-API-KEY": SERPER_API_KEY, "Content-Type": "application/json"},
                                     json={"q": query, "num": 10})
        if resp.status_code >= 400:
            return None
        organic = resp.json().get("organic") or []
    except (httpx.HTTPError, ValueError):
        return None

    votes: dict[tuple[str, str], int] = {}
    for item in organic:
        board = board_from_url(item.get("link") or "")
        if board and slug_matches(company, board[1]):
            votes[board] = votes.get(board, 0) + 1
    if not votes:
        return None
    return max(votes, key=votes.get)


async def _confirm(company: str, ats: str, slug: str) -> bool:
    """A board found by search must actually be readable before it is cached:
    search results include boards a company has since abandoned. Workday is
    searched per role, so an empty search there proves nothing and is accepted."""
    if ats == "workday":
        return True
    async with boards._client() as client:
        postings = await _probe(client, ats, slug)
    if not postings:
        return False
    reported = next((p.company for p in postings if p.company), "")
    if reported and not names_match(company, reported):
        return False
    for p in postings:
        p.company = p.company or company
    try:
        db.upsert_postings(postings)
    except Exception:
        pass
    return True


# Companies whose own careers site has an adapter of its own (boards.py).
# Keyed by _norm(name); a hosted-ATS guess for these finds somebody else's board.
KNOWN = {
    "ibm": ("ibm", "ibm"),
    "amazon": ("amazon", "amazon"), "aws": ("amazon", "amazon"), "amazonwebservices": ("amazon", "amazon"),
    "microsoft": ("microsoft", "microsoft"),
    "google": ("google", "google"), "alphabet": ("google", "google"), "googledeepmind": ("google", "google"),
    "deepmind": ("google", "google"),
}


async def resolve(company: str, allow_search: bool = True) -> tuple[str, str] | None:
    """(ats, slug) for a company, or None if it has no board this app can read."""
    company = " ".join((company or "").split())
    if not company:
        return None
    known = KNOWN.get(_norm(company))
    if known:
        return known
    try:
        row = db.get_board(company)
    except Exception:
        row = None
    if row is not None and row["ats"]:
        # Boards cached before names were checked can belong to someone else;
        # those fall through and are looked up again.
        if slug_matches(company, row["slug"]):
            return row["ats"], row["slug"]
    elif row is not None:
        checked = datetime.fromisoformat(row["checked_at"])
        if _now() - checked < timedelta(days=MISS_TTL_DAYS):
            return None

    guessed = await _guess(company)
    if guessed:
        ats, slug, count = guessed
        _save(company, ats, slug, "guess", count)
        try:
            db.mark_polled(ats, slug, count)      # save_board ran after the upsert
        except Exception:
            pass
        return ats, slug

    if not allow_search:
        return None
    found = await _search(company)
    if found and await _confirm(company, *found):
        ats, slug = found
        _save(company, ats, slug, "search", 0)
        return found
    _save(company, None, None, "search" if SERPER_API_KEY else "guess", 0)
    return None


def _save(company: str, ats: str | None, slug: str | None, via: str, count: int) -> None:
    try:
        db.save_board(company, ats, slug, via, count)
    except Exception:
        pass                                    # the cache is an optimisation, not a dependency


def forget(company: str) -> None:
    """Drop a cached board that turned out to be wrong or dead, so it is re-discovered."""
    _save(company, None, None, "stale", 0)


async def resolve_many(companies: list[str], allow_search: bool = True
                       ) -> tuple[dict[str, tuple[str, str]], list[str]]:
    """Resolve several companies. Returns ({company: (ats, slug)}, [companies with no board])."""
    gate = asyncio.Semaphore(4)

    async def one(c: str):
        async with gate:
            return c, await resolve(c, allow_search)

    results = await asyncio.gather(*(one(c) for c in dict.fromkeys(companies)))
    found = {c: b for c, b in results if b}
    missing = [c for c, b in results if not b]
    return found, missing
