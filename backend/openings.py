"""The openings pipeline: boards first, search only where boards cannot reach.

  1. Plan      target roles and companies, from the profile and the agent
  2. Resolve   each company to its ATS board (registry.py; cached)
  3. Read      every open posting on those boards, straight from the ATS API,
               plus matching postings already collected from other known boards
  4. Fallback  web search only for companies with no readable board, liveness-checked
  5. Filter    hard rules in plain code: level, years, location, sponsorship, age
  6. Rank      embeddings over everything left, then a model reads the full
               description of the top N (ranking.py)

Postings are keyed (ats, slug, job_id), so the same role found twice is one
result, and a posting the board no longer lists is closed rather than shown.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone

import httpx

import boards
import db
import embeddings
import filters
import ranking
import registry
import seeker
import timing
from config import BOARD_FRESH_HOURS, MAX_BOARDS_PER_RUN, SERPER_API_KEY
from jobs import find_openings as search_web_openings
from openrouter import OpenRouterError
from schemas import Profile, ScoredOpening
from search import SearchError

log = logging.getLogger("jcs.openings")

EMBED_POOL = 800          # most postings embedded per run
DETAIL_POOL = 60          # most descriptions fetched one-by-one (SmartRecruiters, Workday)
DISCOVERY_QUERIES = 8     # web searches used to discover boards when no company is named
LIVENESS_TIMEOUT = 8.0


def _fresh(last_polled: str | None) -> bool:
    if not last_polled:
        return False
    try:
        when = datetime.fromisoformat(last_polled)
    except ValueError:
        return False
    return datetime.now(timezone.utc) - when < timedelta(hours=BOARD_FRESH_HOURS)


def to_opening(p: boards.Posting, company: str = "", first_seen: str | None = None) -> ScoredOpening:
    return ScoredOpening(
        key=p.key, title=p.title[:200], company=(p.company or company)[:80], source=p.ats,
        url=p.url, apply_url=p.apply_url, location=p.location, remote=p.remote,
        department=p.department, description=p.description,
        snippet=p.description[:300].replace("\n", " "),
        # For Workday this is derived from "Posted N Days Ago"; "30+" arrives
        # dated just past the bound so the default age filter removes it.
        posted_at=p.posted_at,
        closes_at=p.closes_at, first_seen=first_seen,
        level=filters.title_level(p.title), min_years=filters.required_years(p.description),
        verified=True,
    )


def row_to_opening(row) -> ScoredOpening:
    return ScoredOpening(
        key=row["key"], title=row["title"], company=row["company"], source=row["ats"],
        url=row["url"], apply_url=row["apply_url"], location=row["location"],
        remote=None if row["remote"] is None else bool(row["remote"]),
        department=row["department"], description=row["description"] or "",
        snippet=(row["description"] or "")[:300].replace("\n", " "),
        posted_at=row["posted_at"], closes_at=row["closes_at"], first_seen=row["first_seen"],
        level=filters.title_level(row["title"]), min_years=filters.required_years(row["description"] or ""),
        verified=True,
    )


def keyword_score(title: str, terms: list[str]) -> int:
    """Crude title relevance, only for choosing which postings get the paid steps."""
    words = set(re.findall(r"[a-z0-9+#.]+", (title or "").lower()))
    return sum(1 for t in terms for w in re.findall(r"[a-z0-9+#.]+", t.lower()) if w in words)


def role_terms(roles: list[str]) -> list[str]:
    """Search terms for the shared index: role phrases minus level words."""
    level_words = re.compile(r"\b(intern(ship)?|new grad(uate)?|junior|senior|entry level|summer|"
                             r"20\d\d|i|ii|1|2)\b", re.I)
    out = []
    for role in roles:
        core = " ".join(level_words.sub(" ", role).split())
        if len(core) >= 4:
            out.append(core)
    return list(dict.fromkeys(out))


async def _read_boards(company_boards: dict[str, tuple[str, str]], roles: list[str]
                       ) -> tuple[list[ScoredOpening], list[str], list[str]]:
    """Open postings on each board: from the DB if polled recently, else live."""
    stale: dict[tuple[str, str], str] = {}
    out: list[ScoredOpening] = []
    names: list[str] = []
    for company, (ats, slug) in company_boards.items():
        names.append(f"{company} ({ats})")
        try:
            row = db.get_board(company)
        except Exception:
            row = None
        if ats != "workday" and row is not None and _fresh(row["last_polled"]):
            try:
                out += [row_to_opening(r) for r in db.open_postings_for([(ats, slug)])]
                continue
            except Exception:
                pass
        stale[(ats, slug)] = company

    warnings: list[str] = []
    if stale:
        started = datetime.now(timezone.utc).isoformat()
        fetched, errors = await boards.fetch_boards(list(stale), search=roles)
        for (ats, slug), postings in fetched.items():
            company = stale[(ats, slug)]
            for p in postings:
                p.company = p.company or company
            try:
                first_seen = db.upsert_postings(postings, started)
                # Workday is searched, not listed in full, so absence proves nothing there.
                if ats != "workday":
                    db.close_missing(ats, slug, started)
                db.mark_polled(ats, slug, len(postings))
            except Exception as exc:
                log.warning("could not store board %s/%s: %s", ats, slug, exc)
                first_seen = {}
            out += [to_opening(p, company, first_seen.get(p.key)) for p in postings]
        for (ats, slug), err in errors.items():
            company = stale[(ats, slug)]
            if "not found" in err:
                registry.forget(company)       # the board moved; re-discover next time
            warnings.append(f"Could not read {company}'s {ats} board ({err.split(': ', 1)[-1]}).")
    return out, names, warnings


async def _discover_boards(roles: list[str]) -> dict[str, tuple[str, str]]:
    """No companies named: use a few web searches to find boards hiring for these roles."""
    if not SERPER_API_KEY:
        return {}
    try:
        found, _queries, _skipped = await search_web_openings(roles[:2], [], 10, 60, frozenset(),
                                                              max_queries=DISCOVERY_QUERIES)
    except SearchError:
        return {}
    out: dict[str, tuple[str, str]] = {}
    for o in found:
        board = registry.board_from_url(o.url)
        if not board or not o.company:
            continue
        out.setdefault(o.company, board)
        try:
            if not db.get_board(o.company):
                db.save_board(o.company, board[0], board[1], "search")
        except Exception:
            pass
    return out


async def _alive(client: httpx.AsyncClient, url: str) -> bool:
    try:
        resp = await client.get(url)
    except httpx.HTTPError:
        return False
    if resp.status_code >= 400:
        return False
    # Boards bounce a closed posting to the careers index or a "no longer
    # available" page; the posting id vanishing from the final URL is the tell.
    final = str(resp.url).rstrip("/")
    if final.count("/") < url.rstrip("/").count("/") - 1:
        return False
    body = resp.text[:20000].lower()
    return not any(p in body for p in ("no longer accepting", "no longer available", "position has been filled",
                                       "job you are looking for", "job not found", "posting has expired"))


async def _web_fallback(companies: list[str], roles: list[str], exclude: frozenset[str],
                        limit: int) -> tuple[list[ScoredOpening], list[str], list[str]]:
    """Companies with no readable board: search the web, keep only live links."""
    if not companies or not SERPER_API_KEY:
        return [], [], []
    try:
        found, queries, _ = await search_web_openings(roles[:3], companies, 10, limit, exclude,
                                                      sources=("linkedin", "web"))
    except SearchError as exc:
        return [], [], [f"Web search for companies without a readable board failed: {exc}"]

    gate = asyncio.Semaphore(8)
    async with httpx.AsyncClient(timeout=LIVENESS_TIMEOUT, follow_redirects=True,
                                 headers={"User-Agent": boards.USER_AGENT}) as client:
        async def check(o):
            async with gate:
                return await _alive(client, o.url)
        alive = await asyncio.gather(*(check(o) for o in found))

    out = [ScoredOpening(**{**o.model_dump(), "key": o.key or o.url, "verified": False,
                            "level": filters.title_level(o.title)})
           for o, ok in zip(found, alive) if ok]
    dead = len(found) - len(out)
    notes = []
    if out:
        notes.append(f"{len(out)} result(s) for {', '.join(companies[:4])}"
                     f"{'…' if len(companies) > 4 else ''} come from web search (no readable job board): "
                     "snippets only, and no posting date.")
    if dead:
        notes.append(f"Dropped {dead} web result(s) whose link was dead or closed.")
    return out, queries, notes


async def gather(profile: Profile, roles: list[str], companies: list[str], exclude: frozenset[str],
                 max_age_days: int, include_older: bool, max_results: int
                 ) -> dict:
    """Everything up to (not including) ranking. Returns a dict of pieces for the caller."""
    warnings: list[str] = []
    queries: list[str] = []

    company_boards, missing = await registry.resolve_many(companies[:MAX_BOARDS_PER_RUN])
    if not companies:
        company_boards.update(await _discover_boards(roles))
        queries.append(f"discovery: {', '.join(roles[:2])} across ATS boards")

    board_openings, board_names, read_warnings = await _read_boards(company_boards, roles)
    warnings += read_warnings

    # The shared index: matching postings on boards other users (or the poller) already read.
    terms = role_terms(roles)
    if terms and len(company_boards) < 6:
        try:
            extra = db.search_open_postings(terms, list(company_boards.values()), limit=600)
            board_openings += [row_to_opening(r) for r in extra]
        except Exception as exc:
            log.warning("shared index search failed: %s", exc)

    web, web_queries, web_notes = await _web_fallback(missing, roles, exclude, max_results)
    queries += web_queries
    warnings += web_notes

    # Dedupe on key; web results pointing at a board already read are the same posting.
    seen: set[str] = set()
    pool: list[ScoredOpening] = []
    skipped = 0
    for o in board_openings + web:
        if o.key in seen:
            continue
        seen.add(o.key)
        if o.key in exclude or o.url in exclude:
            skipped += 1
            continue
        pool.append(o)

    kept, dropped = filters.apply(pool, profile, max_age_days, include_older)

    # Descriptions fetched one-by-one only for the most relevant survivors.
    detail_less = [o for o in kept if o.source in boards.DETAIL_ATS and not o.description]
    if detail_less:
        detail_less.sort(key=lambda o: -keyword_score(o.title, roles + profile.skills))
        await _fill_descriptions(detail_less[:DETAIL_POOL])
        kept, dropped_after = filters.apply(kept, profile, max_age_days, include_older)
        for k, v in dropped_after.items():
            dropped[k] = dropped.get(k, 0) + v

    if len(kept) > EMBED_POOL:
        kept.sort(key=lambda o: -keyword_score(o.title, roles + profile.skills))
        kept = kept[:EMBED_POOL]

    return {"openings": kept, "dropped": dropped, "skipped": skipped, "warnings": warnings,
            "queries": queries, "boards": board_names, "missing": missing,
            "pool_size": len(pool)}


async def _fill_descriptions(openings: list[ScoredOpening]) -> None:
    postings = []
    for o in openings:
        ats, slug, job_id = o.key.split(":", 2)
        tenant_path = ""
        if ats == "workday":
            # Rebuild the detail URL from the public posting URL.
            tenant, host, site = boards.workday_parts(slug)
            path = o.url.split(f"/{site}", 1)[-1] if f"/{site}" in o.url else ""
            tenant_path = f"{boards.workday_base(slug)}/wday/cxs/{tenant}/{site}{path}"
        else:
            tenant_path = f"https://api.smartrecruiters.com/v1/companies/{slug}/postings/{job_id}"
        postings.append(boards.Posting(ats=ats, slug=slug, job_id=job_id, title=o.title,
                                       url=o.url, detail_ref=tenant_path))
    await boards.fill_details(postings, limit=len(postings))
    closed = 0
    for o, p in zip(openings, postings):
        if p.description:
            o.description = p.description
            o.snippet = p.description[:300].replace("\n", " ")
            o.min_years = filters.required_years(p.description)
            if p.posted_at and not p.posted_approx:
                o.posted_at = p.posted_at
        if p.extra.get("closed"):
            closed += 1
            try:
                db.close_posting(o.key)
            except Exception:
                pass
    try:
        db.upsert_postings([p for p in postings if p.description])
    except Exception:
        pass


async def rank(openings: list[ScoredOpening], profile: Profile, resume: str
               ) -> tuple[list[ScoredOpening], list[str]]:
    text = seeker.as_text(profile) + "\n\n" + resume.strip()[:3000]
    warnings: list[str] = []
    try:
        sims = await embeddings.similarities(text, openings)
    except OpenRouterError as exc:
        warnings.append(f"Similarity ranking unavailable ({exc}); using keyword overlap instead.")
        terms = profile.target_roles + profile.skills
        top = max([keyword_score(o.title + " " + o.description[:2000], terms) for o in openings] + [1])
        sims = {o.key: keyword_score(o.title + " " + o.description[:2000], terms) / top for o in openings}
    ranked, review_warnings = await ranking.rank(openings, profile, seeker.as_text(profile), resume, sims)
    return ranked, warnings + review_warnings


def annotate_timing(openings: list[ScoredOpening]) -> None:
    notes: dict[tuple[str, str], str | None] = {}
    for o in openings:
        if not o.company or not o.verified:
            continue
        key = (o.company.lower(), o.level)
        if key not in notes:
            notes[key] = timing.company_note(o.company, o.level)
        o.timing_note = notes[key]
