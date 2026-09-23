"""Check the leads that survived ranking against the posting itself.

The hard filters (filters.py) only see what a board's API or a search snippet
says, and plenty of postings leave "Remote?", "Where?" and "Paid?" to the page.
This stage opens each top lead's link when the full text is not already in
hand, gives the model the whole posting, and asks three narrow questions:

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

import httpx

import filters
from config import OPENROUTER_API_KEY, OPENROUTER_SCORING_MODEL
from openrouter import OpenRouterError, chat, parse_json
from schemas import Profile, ScoredOpening

BATCH = 5
PAGE_CHARS = 5000
# Descriptions shorter than this are snippets; the page has the real text.
MIN_DESCRIPTION = 600

SYSTEM = ("You read job postings and report facts they state about where the work happens "
          "and whether it is paid. You never guess, and you return only JSON.")

_TAGS = re.compile(r"<(script|style|noscript|svg)[^>]*>.*?</\1>|<[^>]+>", re.I | re.S)


def page_text(body: str) -> str:
    return " ".join(html.unescape(_TAGS.sub(" ", body)).split())


async def _fetch(client: httpx.AsyncClient, o: ScoredOpening) -> None:
    """Fill o.description from the posting page when only a snippet is known."""
    if len(o.description) >= MIN_DESCRIPTION:
        return
    try:
        resp = await client.get(o.apply_url or o.url)
    except httpx.HTTPError:
        return
    if resp.status_code < 400 and "html" in resp.headers.get("content-type", "html"):
        text = page_text(resp.text)
        # Script-rendered boards return a shell; keep the snippet over that.
        if len(text) > len(o.description) + 200:
            o.description = text


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
            raw = await chat([{"role": "system", "content": SYSTEM},
                              {"role": "user", "content": _prompt(profile, batch)}],
                             model=OPENROUTER_SCORING_MODEL, max_tokens=120 * len(batch) + 200,
                             temperature=0.0)
        data = parse_json(raw)
    except OpenRouterError as exc:
        return str(exc)
    if isinstance(data, dict):
        data = next((v for v in data.values() if isinstance(v, list)), None)
    if not isinstance(data, list):
        return "The check model did not return a JSON array."
    by_index: dict[int, object] = {}
    for position, entry in enumerate(data):
        idx = entry.get("i") if isinstance(entry, dict) else None
        by_index.setdefault(idx if isinstance(idx, int) and 0 <= idx < len(batch) else position, entry)
    for i, o in enumerate(batch):
        _apply(o, by_index.get(i))
    return None


async def check(ranked: list[ScoredOpening], profile: Profile, want: int
                ) -> tuple[list[ScoredOpening], dict[str, int], list[str]]:
    """Check leads in rank order until `want` survive or they run out.

    Returns (kept in rank order, {reason: count dropped}, warnings).
    """
    if not OPENROUTER_API_KEY:
        return ranked[:want], {}, ["OPENROUTER_API_KEY is not set — leads were not checked against their pages."]

    kept: list[ScoredOpening] = []
    dropped: dict[str, int] = {}
    errors: list[str] = []
    gate = asyncio.Semaphore(4)
    pos = 0
    async with httpx.AsyncClient(timeout=12.0, follow_redirects=True,
                                 headers={"User-Agent": "Mozilla/5.0 (LeadClassifier)"}) as client:
        # Rounds of up to 2x what is still missing, so a few drops get backfilled
        # without paying to check the whole ranked list.
        limit = min(len(ranked), 3 * want)
        while len(kept) < want and pos < limit:
            chunk = ranked[pos:min(limit, pos + max(BATCH, 2 * (want - len(kept))))]
            pos += len(chunk)
            await asyncio.gather(*(_fetch(client, o) for o in chunk))
            errors += [e for e in await asyncio.gather(*(
                _check_batch(profile, chunk[i:i + BATCH], gate) for i in range(0, len(chunk), BATCH))) if e]
            for o in chunk:
                reason = rules_out(o, profile) if o.checked else None
                if reason:
                    dropped[reason] = dropped.get(reason, 0) + 1
                else:
                    kept.append(o)
    warnings = []
    if errors:
        warnings.append(f"{len(errors)} check batch(es) failed ({errors[0]}); those leads are unverified.")
    note = filters.describe(dropped)
    if note:
        warnings.append(note.replace("Hidden by your profile", "Hidden after reading the postings"))
    return kept[:want], dropped, warnings
