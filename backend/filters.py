"""Hard filters: the rules that need no model.

A "Senior SWE, 5+ years" posting should never reach a third-year student, and
asking a model to notice that is slower, costlier and less predictable than a
regular expression. Everything here is plain code over the posting's title,
description and location, checked against the seeker's structured profile.

The filters are deliberately conservative: when a posting does not say, it is
kept. Every drop is counted by reason and reported, so a parser mistake shows
up as "38 hidden: senior level" rather than as good results quietly missing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from schemas import Profile

# --- level from the title ------------------------------------------------------
_INTERN = re.compile(r"\b(intern|interns|internship|co-?op|apprentice(ship)?|summer\s+analyst|"
                     r"working\s+student|stagiaire|praktikant)\b", re.I)
_ENTRY = re.compile(r"\b(new\s*grad(uate)?|graduate|grad\b|university\s+grad|entry[\s-]*level|early[\s-]*career|"
                    r"campus|fresher|junior|jr\.?|associate\s+(software|engineer|developer|data)|"
                    r"(sde|swe|engineer|developer)\s*[-\s]?(1|i)\b|level\s*1\b|l[1-3]\b)", re.I)
_MID = re.compile(r"\b((sde|swe|engineer|developer)\s*[-\s]?(2|ii)\b|mid[\s-]*level|l4\b)", re.I)
_SENIOR = re.compile(r"\b(senior|sr\.?|staff|principal|lead|leader|head\s+of|director|manager|"
                     r"architect|vp|vice\s+president|distinguished|fellow|chief|"
                     r"(sde|swe|engineer|developer)\s*[-\s]?(3|iii|iv)\b|l[5-9]\b|expert)\b", re.I)
# Phrases that mark a posting as entry-level even though they contain a word
# the senior pattern would catch ("Associate Product Manager").
_STRONG_ENTRY = re.compile(r"\b(new\s*grad(uate)?|university\s+grad(uate)?|entry[\s-]*level|early[\s-]*career|"
                           r"campus|fresher|graduate\s+(program|programme|engineer|developer|scheme|trainee)|"
                           r"associate\s+product\s+manager|apm|trainee)\b", re.I)
# "Engineering Manager" is senior; "Program Manager Intern" is an internship.
# Intern wins over everything, so it is checked first.


def title_level(title: str) -> str:
    """intern | entry | mid | senior | "" (the title does not say)."""
    t = title or ""
    if _INTERN.search(t):
        return "intern"
    if _STRONG_ENTRY.search(t):
        return "entry"
    if _SENIOR.search(t):
        return "senior"
    if _MID.search(t):
        return "mid"
    if _ENTRY.search(t):
        return "entry"
    return ""


# --- required years from the description ----------------------------------------
_NUM = r"(\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten)"
_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
          "eight": 8, "nine": 9, "ten": 10}
_YEARS = re.compile(
    rf"(?:(?:at\s+least|minimum(?:\s+of)?|min\.?)\s+)?{_NUM}\s*(?:\+|plus)?\s*"
    rf"(?:(?:-|–|to)\s*{_NUM}\s*\+?\s*)?(?:years?|yrs?)(?:\s+of)?\b([^.\n;]{{0,80}})",
    re.I,
)
_EXPERIENCE_WORDS = re.compile(r"experience|industry|professional|working|hands-on|building|developing|"
                               r"engineering|software|in\s+a\s+", re.I)
_NOT_EXPERIENCE = re.compile(r"old|age|degree|program|course|study|history|founded|since|company|"
                             r"warranty|anniversary|in\s+business", re.I)


def _num(token: str) -> int:
    return _WORDS.get(token.lower(), 0) if not token.isdigit() else int(token)


def required_years(text: str) -> int | None:
    """The smallest "N+ years of experience" the posting asks for, or None.

    The smallest, because descriptions list several ("3+ years Python, 5+ years
    overall") and dropping a posting on the largest would be the wrong mistake:
    a filter should only remove what is clearly out of reach.
    """
    found: list[int] = []
    for match in _YEARS.finditer(text or ""):
        low = _num(match.group(1))
        tail = match.group(3) or ""
        window = (text[max(0, match.start() - 40):match.start()] + " " + tail)
        if not _EXPERIENCE_WORDS.search(window) or _NOT_EXPERIENCE.search(tail[:30]):
            continue
        if 0 < low <= 20:
            found.append(low)
    return min(found) if found else None


# --- location -------------------------------------------------------------------
_ALIASES = {
    "bangalore": {"bengaluru", "bangalore"},
    "bengaluru": {"bengaluru", "bangalore"},
    "gurgaon": {"gurgaon", "gurugram"},
    "gurugram": {"gurgaon", "gurugram"},
    "bombay": {"mumbai", "bombay"},
    "mumbai": {"mumbai", "bombay"},
    "delhi": {"delhi", "new delhi", "noida", "gurgaon", "gurugram", "ncr"},
    "ncr": {"delhi", "new delhi", "noida", "gurgaon", "gurugram", "ncr"},
    "madras": {"chennai", "madras"},
    "chennai": {"chennai", "madras"},
    "sf": {"san francisco", "sf", "bay area"},
    "bay area": {"san francisco", "bay area", "palo alto", "mountain view", "menlo park",
                 "sunnyvale", "san jose", "oakland", "redwood city", "south san francisco"},
    "nyc": {"new york", "nyc"},
    "new york": {"new york", "nyc"},
    "us": {"united states", "usa", "u.s.", ", us", "us-", "us remote", "remote - us"},
    "usa": {"united states", "usa", "u.s.", ", us"},
    "united states": {"united states", "usa", "u.s.", ", us"},
    "uk": {"united kingdom", "uk", "london", "england"},
    "united kingdom": {"united kingdom", "uk", "london", "england"},
}
_INDIA = {"india", "bengaluru", "bangalore", "mumbai", "pune", "hyderabad", "chennai", "delhi",
          "new delhi", "gurgaon", "gurugram", "noida", "kolkata", "ahmedabad", "jaipur", ", in",
          "kochi", "coimbatore", "indore", "chandigarh", "thiruvananthapuram", "mysore", "mysuru"}
_ALIASES["india"] = _INDIA
_REMOTE = re.compile(r"\bremote\b|\banywhere\b|work\s+from\s+home|\bwfh\b", re.I)


def location_matches(wanted: list[str], location: str, remote: bool | None, remote_ok: bool) -> bool | None:
    """True/False, or None when the posting gives no location to judge."""
    if not wanted:
        return True
    loc = (location or "").lower()
    if not loc.strip():
        return True if (remote is True and remote_ok) else None
    # When the posting names cities, trust its text over a board-level "remote"
    # flag: Ashby and others set it for roles that are remote within one
    # country, which is not remote for someone on another continent.
    is_remote = bool(_REMOTE.search(loc))
    for place in wanted:
        key = place.strip().lower()
        if not key:
            continue
        terms = _ALIASES.get(key, {key})
        if any(term in loc for term in terms):
            return True
    if is_remote and remote_ok:
        # "Remote - US" is not remote for someone in Pune.
        region_locked = re.search(r"remote\s*[-–(,:]\s*([a-z .]+)", loc)
        if not region_locked:
            return True
        region = region_locked.group(1)
        return any(any(t in region for t in _ALIASES.get(p.lower(), {p.lower()})) for p in wanted) or \
            region.strip() in ("global", "worldwide", "anywhere")
    return False


# --- sponsorship ------------------------------------------------------------------
_NO_SPONSOR = re.compile(
    r"(not|unable\s+to|cannot|can't|won't|will\s+not|do\s+not|does\s+not)\s+(be\s+able\s+to\s+)?"
    r"(provide\s+|offer\s+)?(visa\s+)?sponsor|no\s+(visa\s+)?sponsorship|without\s+(the\s+need\s+for\s+)?"
    r"(current\s+or\s+future\s+)?(visa\s+)?sponsorship", re.I)


def refuses_sponsorship(text: str) -> bool:
    return bool(_NO_SPONSOR.search(text or ""))


# --- age ------------------------------------------------------------------------------
def age_days(posted_at: str | None, today: date | None = None) -> int | None:
    if not posted_at:
        return None
    try:
        day = date.fromisoformat(posted_at[:10])
    except ValueError:
        return None
    today = today or datetime.now(timezone.utc).date()
    return (today - day).days


# --- the filter ---------------------------------------------------------------------
@dataclass
class Verdict:
    keep: bool
    reason: str = ""


# Board postings still listed are dropped for age only past this.
VERIFIED_MAX_AGE_DAYS = 120

REASONS = {
    "level_senior": "too senior for your level",
    "level_intern": "internships (you're not looking for one)",
    "level_not_intern": "not internships (you're looking for one)",
    "years": "ask for more experience than you have",
    "location": "outside your locations",
    "sponsorship": "say they won't sponsor a visa",
    "old": "posted too long ago",
    "closed": "closed on the board",
}


def judge(posting, profile: Profile, max_age_days: int = 30, include_older: bool = False,
          today: date | None = None) -> Verdict:
    """Keep or drop one posting. `posting` needs title, description, location,
    remote, posted_at, and optionally level/min_years already filled in."""
    level = posting.level or title_level(posting.title)
    years = posting.min_years if posting.min_years is not None else required_years(posting.description)
    wants_intern = "internship" in profile.job_types
    wants_full = "full_time" in profile.job_types
    junior_seeker = profile.seniority in ("intern", "new_grad", "junior")

    if level == "intern":
        if not wants_intern:
            return Verdict(False, "level_intern")
    else:
        if wants_intern and not wants_full:
            # Internships say so in the title, almost without exception; a
            # posting that does not is a full-time role.
            return Verdict(False, "level_not_intern")
        if level == "senior" and junior_seeker:
            return Verdict(False, "level_senior")
        if level == "mid" and profile.seniority in ("intern", "new_grad"):
            return Verdict(False, "level_senior")

    if years is not None and level != "intern":
        # Allow some stretch: a new grad can apply to "0-2 years", someone with
        # three years to "5+". Beyond that the application is rarely read.
        slack = 1 if profile.seniority in ("intern", "new_grad") else 2
        if years > profile.years_experience + slack:
            return Verdict(False, "years")

    if profile.locations:
        match = location_matches(profile.locations, posting.location, posting.remote, profile.remote_ok)
        if match is False:
            return Verdict(False, "location")

    if profile.needs_sponsorship and refuses_sponsorship(posting.description):
        return Verdict(False, "sponsorship")

    if not include_older:
        age = age_days(posting.posted_at, today)
        # A web result's age is the main hint that it is stale. A posting the
        # board still lists is open by definition, and internship listings
        # routinely stay up for months, so board postings get a far longer
        # leash and older ones rank lower through the recency feature instead.
        limit = max(max_age_days, VERIFIED_MAX_AGE_DAYS) if getattr(posting, "verified", False) else max_age_days
        if age is not None and age > limit:
            return Verdict(False, "old")

    return Verdict(True)


def apply(postings: list, profile: Profile, max_age_days: int = 30, include_older: bool = False,
          today: date | None = None) -> tuple[list, dict[str, int]]:
    """Returns (kept, {reason: count dropped})."""
    kept, dropped = [], {}
    for p in postings:
        verdict = judge(p, profile, max_age_days, include_older, today)
        if verdict.keep:
            kept.append(p)
        else:
            dropped[verdict.reason] = dropped.get(verdict.reason, 0) + 1
    return kept, dropped


def describe(dropped: dict[str, int]) -> str:
    parts = [f"{n} {REASONS.get(r, r)}" for r, n in sorted(dropped.items(), key=lambda kv: -kv[1]) if n]
    return "Hidden by your profile: " + "; ".join(parts) + "." if parts else ""


def cutoff(days: int) -> str:
    return (datetime.now(timezone.utc).date() - timedelta(days=days)).isoformat()
