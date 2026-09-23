"""The daily poll: re-read every known board, then check saved searches.

Waiting for someone to happen to search a company builds history slowly and
unevenly. Polling every board in `company_boards` once a day instead gives:

  - true first-seen dates for every posting, across all companies, which is
    what hiring-cycle notes (timing.py) are built from;
  - closed postings marked closed the day they disappear;
  - alerts: each saved search is matched against postings first seen since it
    was last checked, so "new intern role at Razorpay posted today, 92 match"
    reaches the seeker while applying early still matters.

Alert matching is deliberately cheap -- hard filters plus embedding similarity,
no per-alert model call -- so a poll over hundreds of boards costs cents.

Run it with `python scripts/poll_boards.py` (the Render cron job does), or
POST /api/admin/poll with the POLL_TOKEN.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone

import boards
import db
import embeddings
import filters
import meter
from openrouter import OpenRouterError
from schemas import Profile

log = logging.getLogger("jcs.poller")

# Boards read concurrently during a poll; Workday is excluded from the full
# poll (it is searched per role, not listed) and refreshed by runs instead.
POLL_CONCURRENCY = 4
POLL_SKIP = boards.SEARCHED_ATS
ALERTS_PER_SEARCH = 20


async def poll_boards(limit: int | None = None) -> dict:
    rows = [r for r in db.known_boards() if r["ats"] not in POLL_SKIP]
    if limit:
        rows = rows[:limit]
    # One company per board, even if several names resolved to the same one.
    by_board: dict[tuple[str, str], str] = {}
    for r in rows:
        by_board.setdefault((r["ats"], r["slug"]), r["company"])

    stats = {"boards": len(by_board), "postings": 0, "new": 0, "closed": 0, "errors": 0}
    items = list(by_board.items())
    for start in range(0, len(items), POLL_CONCURRENCY * 4):
        chunk = items[start:start + POLL_CONCURRENCY * 4]
        started = datetime.now(timezone.utc).isoformat()
        fetched, errors = await boards.fetch_boards([b for b, _ in chunk])
        stats["errors"] += len(errors)
        for (ats, slug), postings in fetched.items():
            company = by_board[(ats, slug)]
            for p in postings:
                p.company = p.company or company
            first_seen = db.upsert_postings(postings, started)
            stats["postings"] += len(postings)
            stats["new"] += sum(1 for v in first_seen.values() if v == started)
            stats["closed"] += db.close_missing(ats, slug, started)
            db.mark_polled(ats, slug, len(postings))
        for board, err in errors.items():
            log.warning("poll: %s/%s failed: %s", board[0], board[1], err)
    return stats


def _profile(raw: str) -> Profile:
    try:
        return Profile.model_validate_json(raw or "{}")
    except ValueError:
        return Profile()


async def match_saved_searches() -> dict:
    """Alert every saved search about matching postings first seen since its last check."""
    searches = db.saved_searches()
    stats = {"searches": len(searches), "alerts": 0}
    if not searches:
        return stats
    oldest = min(s["last_checked"] for s in searches)
    fresh = db.postings_first_seen_since(oldest)
    if not fresh:
        for s in searches:
            db.touch_saved_search(s["id"], datetime.now(timezone.utc).isoformat())
        return stats

    from openings import row_to_opening
    candidates = [row_to_opening(r) for r in fresh]
    try:
        vectors, titles = await asyncio.gather(embeddings.posting_vectors(candidates),
                                               embeddings.title_vectors(candidates))
    except OpenRouterError as exc:
        log.warning("poll: embeddings unavailable, alerts skipped: %s", exc)
        return stats

    checked_at = datetime.now(timezone.utc).isoformat()
    for s in searches:
        profile = _profile(s["profile"])
        roles = json.loads(s["roles"] or "[]")
        companies = {c.lower() for c in json.loads(s["companies"] or "[]")}
        pool = [o for o in candidates if o.first_seen and o.first_seen >= s["last_checked"]]
        if companies:
            pool = [o for o in pool if o.company.lower() in companies]
        if roles:
            from openings import keyword_score
            pool = [o for o in pool if keyword_score(o.title, roles + profile.target_roles) > 0] or pool
        kept, _ = filters.apply(pool, profile, max_age_days=7)
        if not kept or not s["embedding"]:
            db.touch_saved_search(s["id"], checked_at)
            continue
        seeker_vec = embeddings.unpack(s["embedding"])
        scored = []
        for o in kept:
            vec = vectors.get(o.key)
            if vec is None:
                continue
            score = round(100 * embeddings.blend(seeker_vec, vec, titles.get(o.key)))
            if score >= s["min_score"]:
                scored.append((score, o))
        scored.sort(key=lambda t: -t[0])
        rows = [(s["user_id"], s["id"], o.key, score) for score, o in scored[:ALERTS_PER_SEARCH]]
        db.add_alerts(rows)
        stats["alerts"] += len(rows)
        db.touch_saved_search(s["id"], checked_at)
    return stats


async def run(limit: int | None = None) -> dict:
    meter.start()
    db.init_db()
    polled = await poll_boards(limit)
    alerts = await match_saved_searches()
    return {"poll": polled, "alerts": alerts, "cost": meter.current().summary()}


if __name__ == "__main__":      # pragma: no cover
    logging.basicConfig(level=logging.INFO)
    print(json.dumps(asyncio.run(run()), indent=2))
