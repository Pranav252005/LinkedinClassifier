"""When a company tends to open roles -- from what has actually been observed.

Board APIs return only what is open right now; nothing public says when a
company opened the same role last year. So this cannot be looked up, only
accumulated: the daily poll records the first time each posting appears
(`postings.first_seen`), and after a few months that is a genuine record of
when each company opens, say, intern roles.

Until there is enough of it the honest answer is "not enough history yet", and
that is what this returns -- a prediction from two weeks of data would be a
guess dressed up as a fact.
"""

from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timezone

import db
from filters import title_level

MIN_SPAN_DAYS = 90        # observed for at least a quarter before saying anything
MIN_POSTINGS = 4
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _day(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def summarize(rows: list, level: str = "", today: date | None = None) -> dict:
    """Pure: rows of (title, first_seen, posted_at, ...) -> a timing summary."""
    today = today or datetime.now(timezone.utc).date()
    seen = [_day(r["first_seen"]) for r in rows]
    seen = [d for d in seen if d]
    if not seen:
        return {"enough": False, "observed_since": None, "note": None}
    since = min(seen)
    span = (today - since).days

    relevant = [r for r in rows if not level or title_level(r["title"]) == level]
    # A posting's own date beats our first sighting, when the board gives one.
    opened = [(_day(r["posted_at"]) or _day(r["first_seen"])) for r in relevant]
    opened = [d for d in opened if d and d >= since]
    months = Counter(d.month for d in opened)

    out = {"enough": False, "observed_since": since.isoformat(), "span_days": span,
           "postings": len(opened), "by_month": {MONTHS[m - 1]: n for m, n in sorted(months.items())},
           "note": None}
    if span < MIN_SPAN_DAYS or len(opened) < MIN_POSTINGS:
        return out

    peak = [MONTHS[m - 1] for m, n in months.most_common(2) if n >= 2]
    if not peak:
        return out
    kind = {"intern": "intern roles", "entry": "entry-level roles"}.get(level, "roles like this")
    out["enough"] = True
    out["note"] = (f"Usually posts {kind} in {' and '.join(peak)} "
                   f"(observed since {since.strftime('%b %Y')}).")
    return out


def company_note(company: str, level: str = "") -> str | None:
    if not company:
        return None
    try:
        rows = db.company_postings(company)
    except Exception:
        return None
    return summarize(rows, level).get("note")


def company_timing(company: str, level: str = "") -> dict:
    try:
        rows = db.company_postings(company)
    except Exception:
        rows = []
    return summarize(rows, level)
