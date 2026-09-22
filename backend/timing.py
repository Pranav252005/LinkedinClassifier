"""When a posting actually opened, and when it closes.

Greenhouse and Lever publish their boards as unauthenticated JSON, and those
feeds carry real timestamps -- `first_published` and `application_deadline` on
Greenhouse, `createdAt` on Lever. That is the difference between telling someone
a date and making one up, so dates only ever come from here.

What this cannot do is reconstruct history. Both feeds return the postings that
are open right now, so the current cycle is visible and previous ones are not.
Predicting next year's opening date needs history, which the app has to
accumulate by recording what it sees -- see `record_sightings` in db.py.

Companies off these two boards (most Indian employers -- Razorpay, PhonePe,
Zomato and Swiggy all run their own portals) publish no such feed, so they carry
no dates at all rather than invented ones.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import httpx

GREENHOUSE_BOARD = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
LEVER_BOARD = "https://api.lever.co/v0/postings/{slug}?mode=json"

TIMEOUT = 15.0
# One run returns at most 30 openings, which in practice span fewer than two
# dozen employers, so the cap is set to cover a whole run rather than a
# fraction of it -- the old limit of 6 was why most results showed no date.
MAX_BOARDS = 24
MAX_CONCURRENT_BOARDS = 6   # the feeds are public JSON; do not hammer them

# The only two boards that publish dated feeds. Everything else (Ashby,
# LinkedIn, in-house portals) carries no date, and that is a property of the
# source, not a lookup that failed.
DATED_SOURCES = ("greenhouse", "lever")


def _slug_from(url: str) -> str | None:
    """The employer slug sits in the first path segment of an ATS URL."""
    try:
        parts = [p for p in url.split("//", 1)[-1].split("/")[1:] if p]
    except Exception:
        return None
    return parts[0].lower() if parts else None


def _job_id(url: str) -> str | None:
    """Match on the posting id, not the URL.

    Greenhouse's feed reports absolute_url as the employer's own careers page
    (stripe.com/jobs/search?gh_jid=8026689) while search returns the board URL
    (job-boards.greenhouse.io/stripe/jobs/8026689). The id is the only part
    common to both. Lever uses a uuid in the same position.
    """
    cleaned = url.split("#")[0]
    if "gh_jid=" in cleaned:
        return cleaned.split("gh_jid=")[1].split("&")[0].strip() or None
    path = cleaned.split("?")[0].rstrip("/")
    tail = path.rsplit("/", 1)[-1] if "/" in path else ""
    return tail.strip() or None


def _iso_day(value: object) -> str | None:
    if isinstance(value, str) and len(value) >= 10:
        return value[:10]
    if isinstance(value, (int, float)) and value > 0:
        # Lever uses epoch milliseconds.
        seconds = value / 1000 if value > 10_000_000_000 else value
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc).date().isoformat()
        except (OverflowError, OSError, ValueError):
            return None
    return None


async def _greenhouse(client: httpx.AsyncClient, slug: str) -> dict[str, dict]:
    resp = await client.get(GREENHOUSE_BOARD.format(slug=slug))
    if resp.status_code != 200:
        return {}
    out: dict[str, dict] = {}
    for job in resp.json().get("jobs", []):
        key = str(job.get("id") or "").strip()
        if not key:
            continue
        out[key] = {
            "posted_at": _iso_day(job.get("first_published") or job.get("updated_at")),
            "closes_at": _iso_day(job.get("application_deadline")),
        }
    return out


async def _lever(client: httpx.AsyncClient, slug: str) -> dict[str, dict]:
    resp = await client.get(LEVER_BOARD.format(slug=slug))
    if resp.status_code != 200:
        return {}
    out: dict[str, dict] = {}
    payload = resp.json()
    if not isinstance(payload, list):
        return {}
    for job in payload:
        key = str(job.get("id") or "").strip()
        if key:
            out[key] = {"posted_at": _iso_day(job.get("createdAt")), "closes_at": None}
    return out


async def enrich(openings: list) -> list[str]:
    """Attach real posted/closing dates in place. Returns warnings."""
    wanted: dict[tuple[str, str], None] = {}
    datable = 0
    for o in openings:
        if o.source not in DATED_SOURCES:
            continue
        datable += 1
        slug = _slug_from(o.url)
        if slug:
            wanted.setdefault((o.source, slug), None)

    boards = list(wanted)[:MAX_BOARDS]
    skipped_boards = len(wanted) - len(boards)
    if not boards:
        return _no_feed_warning(openings, datable)

    limit = asyncio.Semaphore(MAX_CONCURRENT_BOARDS)

    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:

        async def fetch(source: str, slug: str) -> dict[str, dict]:
            async with limit:
                fn = _greenhouse if source == "greenhouse" else _lever
                return await fn(client, slug)

        fetched = await asyncio.gather(
            *[fetch(src, slug) for src, slug in boards],
            return_exceptions=True,
        )

    index: dict[str, dict] = {}
    failures = 0
    for result in fetched:
        if isinstance(result, BaseException):
            failures += 1
            continue
        index.update(result)

    matched = 0
    for o in openings:
        if o.source not in DATED_SOURCES:
            continue
        job_id = _job_id(o.url)
        hit = index.get(job_id) if job_id else None
        if hit:
            o.posted_at = hit.get("posted_at")
            o.closes_at = hit.get("closes_at")
            matched += 1

    warnings = _no_feed_warning(openings, datable)
    missed = datable - matched
    if missed > 0:
        detail = f"{missed} of {datable} postings on a dated board could not be matched to its feed"
        if failures:
            detail += f" ({failures} board lookup(s) failed)"
        if skipped_boards:
            detail += f"; {skipped_boards} board(s) were skipped past the per-run cap of {MAX_BOARDS}"
        warnings.append(detail + " — those show no date rather than a guessed one.")
    return warnings


def _no_feed_warning(openings: list, datable: int) -> list[str]:
    """Openings whose board publishes no dated feed at all."""
    undated = len(openings) - datable
    if not undated:
        return []
    return [
        f"{undated} of {len(openings)} openings come from boards that publish no dated "
        "feed (Ashby, LinkedIn, in-house portals), so they carry no posting date."
    ]
