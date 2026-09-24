"""Check the leads that survived ranking against the posting itself.

The hard filters (filters.py) only see what a board's API or a search snippet
says, and plenty of postings leave "Remote?", "Where?" and "Paid?" to the page.
This stage first confirms each top lead is still open -- through the board's
own record of the posting where it has one, else by opening the page -- so an
expired or filled posting is never shown. It fills in the full text when only
a snippet is in hand, gives the model the whole posting, and asks three narrow
questions:

  work_mode   remote | hybrid | onsite | unknown
  city        where the seeker would work, if on-site or hybrid
  pay         paid | unpaid | unknown

The answers are checked in code against the profile, so the model reads and
the rules decide. A lead is dropped only when the posting says something that
rules it out; "unknown" is kept and shown as unverified.
"""

from __future__ import annotations

import asyncio
import html
import re
from datetime import datetime, timezone

import httpx

import boards
import db
import filters
from config import OPENROUTER_API_KEY, OPENROUTER_SCORING_MODEL
from openrouter import OpenRouterError, chat_json_list
from schemas import Profile, ScoredOpening

BATCH = 5
# Job sites serve a bot wall to anything that does not look like a browser.
BROWSER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
                 "Chrome/126.0 Safari/537.36")
PAGE_CHARS = 5000
# Descriptions shorter than this are snippets; the page has the real text.
MIN_DESCRIPTION = 600

SYSTEM = ("You read job postings and report facts they state about where the work happens "
          "and whether it is paid. You never guess, and you return only JSON.")

_TAGS = re.compile(r"<(script|style|noscript|svg)[^>]*>.*?</\1>|<[^>]+>", re.I | re.S)


def page_text(body: str) -> str:
    return " ".join(html.unescape(_TAGS.sub(" ", body)).split())


_GONE_PHRASES = ("no longer accepting", "no longer available", "position has been filled",
                 "job you are looking for", "job not found", "posting has expired", "this job has expired",
                 "job has been closed", "this position is no longer", "this job is no longer",
                 "errorhasstatus: true")        # Google's results page for a removed job
_VALID_THROUGH = re.compile(r'"validThrough"\s*:\s*"(\d{4}-\d{2}-\d{2})')


def page_closed(url: str, resp: httpx.Response) -> bool:
    """Whether a posting page says the posting is gone. A page that could not
    be read for other reasons (a bot wall, a 500) is not evidence either way."""
    if resp.status_code in (404, 410):
        return True
    if resp.status_code >= 400:
        return False
    # Boards bounce a closed posting to the careers index or a "no longer
    # available" page; the posting id vanishing from the final URL is the tell.
    if any(tell in str(resp.url) for tell in ("expired_jd_redirect", "error=true")):
        return True                             # LinkedIn's and Greenhouse's bounce for a closed posting
    final = str(resp.url).split("?", 1)[0].rstrip("/")
    if final.count("/") < url.split("?", 1)[0].rstrip("/").count("/") - 1:
        return True
    body = resp.text[:60000]
    # Structured data many career sites embed for search engines.
    until = _VALID_THROUGH.search(body)
    if until and until.group(1) < datetime.now(timezone.utc).date().isoformat():
        return True
    low = body.lower()
    return any(p in low for p in _GONE_PHRASES)


def _record_closed(source: str, resp: httpx.Response) -> bool:
    """Whether a board's own record of a posting (boards.detail_url) says it closed."""
    try:
        data = resp.json()
    except ValueError:
        return False
    if not isinstance(data, dict):
        return False
    if source == "smartrecruiters":
        return data.get("active") is False
    if source == "workday":
        info = data.get("jobPostingInfo") or {}
        return info.get("canApply") is False or info.get("posted") is False
    if source == "microsoft":
        return not (data.get("data") or {}).get("id")
    return False


async def still_open(client: httpx.AsyncClient, o: ScoredOpening) -> bool | None:
    """Confirm a lead is still open, filling o.description from its page when
    only a snippet is known. False: gone. True: confirmed. None: could not tell."""
    verdict = None
    record = boards.detail_url(o.key, o.url) if o.verified else ""
    if record:
        try:
            resp = await client.get(record, headers={"Accept": "application/json"})
            if resp.status_code in (404, 410) or (resp.status_code < 400 and _record_closed(o.source, resp)):
                return False
            if resp.status_code < 400:
                verdict = True
        except httpx.HTTPError:
            pass
    short = len(o.description) < MIN_DESCRIPTION
    if verdict and not short:
        return True
    url = o.url or o.apply_url
    try:
        resp = await client.get(url)
    except httpx.HTTPError:
        return verdict
    if page_closed(url, resp):
        return False
    if resp.status_code >= 400:
        return verdict
    if short and "html" in resp.headers.get("content-type", "html"):
        text = page_text(resp.text)
        # Script-rendered boards return a shell; keep the snippet over that.
        if len(text) > len(o.description) + 200:
            o.description = text
    return True


def _prompt(profile: Profile, batch: list[ScoredOpening]) -> str:
    blocks = "\n---\n".join(
        f"[{i}] {o.title} — {o.company or 'unknown company'}\nlisted location: {o.location or 'not stated'}\n"
        f"{(o.description or o.snippet or '(no text)')[:PAGE_CHARS]}"
        for i, o in enumerate(batch))
    places = ", ".join(profile.locations) or "(none given)"
    return f"""The candidate wants to work in: {places}

### Postings
{blocks}

For EACH posting return one object:
{{
  "i": <number in brackets>,
  "work_mode": "remote" | "hybrid" | "onsite" | "unknown",
  "city": "<city the role is based in, or empty if fully remote or not stated>",
  "in_candidate_location": true | false | null,
  "pay": "paid" | "unpaid" | "unknown",
  "note": "<under 15 words: the phrase that decided work_mode and pay>"
}}
Return a JSON array of exactly {len(batch)} objects and nothing else.

Rules: "remote" only if the posting says the role can be done fully remotely; a few
office days a week is "hybrid". in_candidate_location is true if the role's office (or
remote region) is in or around one of the candidate's places (Bengaluru = Bangalore),
false if it is clearly elsewhere, null if not stated. "paid" needs a salary, stipend,
pay range or an explicit "paid"; perks alone are not pay. When the text does not say,
answer "unknown" or null."""


def _apply(o: ScoredOpening, entry: object) -> None:
    if not isinstance(entry, dict):
        return
    mode = entry.get("work_mode")
    if mode in ("remote", "hybrid", "onsite"):
        o.work_mode = mode
    pay = entry.get("pay")
    if pay in ("paid", "unpaid"):
        o.pay = pay
    city = entry.get("city")
    if isinstance(city, str) and city.strip() and not o.location:
        o.location = city.strip()[:80]
    here = entry.get("in_candidate_location")
    o.in_location = here if isinstance(here, bool) else None
    note = entry.get("note")
    o.check_note = " ".join(note.split())[:160] if isinstance(note, str) and note.strip() else None
    o.checked = True


def rules_out(o: ScoredOpening, profile: Profile) -> str | None:
    """The reason to drop a checked lead, or None to keep it."""
    if profile.paid_only and o.pay == "unpaid":
        return "unpaid"
    if profile.work_mode == "remote" and o.work_mode in ("onsite", "hybrid"):
        return "not_remote"
    if profile.work_mode == "onsite":
        if o.work_mode == "remote":
            return "remote_only"
        if profile.locations and o.in_location is False:
            return "location"
    elif profile.locations and o.in_location is False and not (
            o.work_mode == "remote" and profile.remote_ok):
        return "location"
    return None


async def _check_batch(profile: Profile, batch: list[ScoredOpening], gate: asyncio.Semaphore) -> str | None:
    try:
        async with gate:
            data = await chat_json_list([{"role": "system", "content": SYSTEM},
                                         {"role": "user", "content": _prompt(profile, batch)}],
                                        model=OPENROUTER_SCORING_MODEL, max_tokens=120 * len(batch) + 200,
                                        temperature=0.0, thinking="low")
    except OpenRouterError as exc:
        return str(exc)
    by_index: dict[int, object] = {}
    for position, entry in enumerate(data):
        idx = entry.get("i") if isinstance(entry, dict) else None
        by_index.setdefault(idx if isinstance(idx, int) and 0 <= idx < len(batch) else position, entry)
    for i, o in enumerate(batch):
        _apply(o, by_index.get(i))
    return None


def _forget_closed(o: ScoredOpening) -> None:
    if not o.verified:
        return
    try:
        db.close_posting(o.key)
    except Exception:
        pass                                    # the index is an optimisation, not a dependency


async def check(ranked: list[ScoredOpening], profile: Profile, want: int
                ) -> tuple[list[ScoredOpening], dict[str, int], list[str]]:
    """Check leads in rank order until `want` survive or they run out.

    Returns (kept in rank order, {reason: count dropped}, warnings).
    """
    kept: list[ScoredOpening] = []
    dropped: dict[str, int] = {}
    errors: list[str] = []
    gate = asyncio.Semaphore(4)
    pos = 0
    async with httpx.AsyncClient(timeout=12.0, follow_redirects=True,
                                 headers={"User-Agent": BROWSER_AGENT}) as client:
        # Rounds of up to 2x what is still missing, so a few drops get backfilled
        # without paying to check the whole ranked list.
        limit = min(len(ranked), 3 * want)
        while len(kept) < want and pos < limit:
            chunk = ranked[pos:min(limit, pos + max(BATCH, 2 * (want - len(kept))))]
            pos += len(chunk)
            open_now = await asyncio.gather(*(still_open(client, o) for o in chunk))
            for o, is_open in zip(chunk, open_now):
                if is_open is False:
                    dropped["closed"] = dropped.get("closed", 0) + 1
                    _forget_closed(o)
                elif is_open:
                    o.unconfirmed = False
                # None leaves it as it was: a page confirmed open minutes ago by
                # the search step stays confirmed if the site now refuses a visit.
            chunk = [o for o, is_open in zip(chunk, open_now) if is_open is not False]
            if not OPENROUTER_API_KEY:
                kept += chunk
                continue
            errors += [e for e in await asyncio.gather(*(
                _check_batch(profile, chunk[i:i + BATCH], gate) for i in range(0, len(chunk), BATCH))) if e]
            for o in chunk:
                reason = rules_out(o, profile) if o.checked else None
                if reason:
                    dropped[reason] = dropped.get(reason, 0) + 1
                else:
                    kept.append(o)
    warnings = []
    if not OPENROUTER_API_KEY:
        warnings.append("OPENROUTER_API_KEY is not set — mode, location and pay were not read from the postings.")
    if errors:
        warnings.append(f"{len(errors)} check batch(es) failed ({errors[0]}); those leads are unverified.")
    note = filters.describe(dropped)
    if note:
        warnings.append(note.replace("Hidden by your profile", "Hidden after reading the postings"))
    return kept[:want], dropped, warnings
