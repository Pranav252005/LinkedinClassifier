"""Request/response models for the contact scout API."""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


# --- auth / account ----------------------------------------------------------
class Credentials(BaseModel):
    email: str = Field(..., max_length=254)
    password: str = Field(..., max_length=200)


class AccountInfo(BaseModel):
    email: str
    plan: Literal["free", "pro"]
    runs_used: int
    runs_allowed: int
    plan_renews_at: Optional[str] = None
    billing_enabled: bool
    price_label: str


# --- structured profile --------------------------------------------------------
Seniority = Literal["intern", "new_grad", "junior", "mid", "senior"]


class Profile(BaseModel):
    """The resume, read once into fields every later stage can filter on.

    Shown to the seeker as editable chips before a run: a wrong seniority or a
    missing location is cheaper to fix here than to discover in the results.
    """

    target_roles: list[str] = Field(default_factory=list, max_length=8)
    seniority: Seniority = "new_grad"
    years_experience: float = Field(default=0.0, ge=0, le=50)
    # What they will accept: an internship, a full-time role, or either.
    job_types: list[Literal["internship", "full_time"]] = Field(default_factory=lambda: ["full_time"])
    skills: list[str] = Field(default_factory=list, max_length=40)
    locations: list[str] = Field(default_factory=list, max_length=10)
    remote_ok: bool = True
    needs_sponsorship: bool = False
    # Hide postings that say they are unpaid. Ones that do not say are kept.
    paid_only: bool = False
    # any: no preference. remote: remote roles only. onsite: roles you go into
    # an office for (hybrid counts), and only in one of `locations`.
    work_mode: Literal["any", "remote", "onsite"] = "any"
    college: str = Field(default="", max_length=120)
    grad_year: Optional[int] = Field(default=None, ge=1970, le=2100)
    summary: str = Field(default="", max_length=400)


class ProfileRequest(BaseModel):
    resume: str = Field(..., min_length=20, max_length=20_000)


# --- search ------------------------------------------------------------------
class SearchRequest(BaseModel):
    # "people" finds humans to contact; "jobs" finds openings you can apply to;
    # "gigs" finds freelance projects (Upwork).
    mode: Literal["people", "jobs", "gigs"] = "people"
    resume: str = Field(..., min_length=20, max_length=20_000)
    companies: list[str] = Field(default_factory=list)
    titles: list[str] = Field(default_factory=list)
    role_target: str = Field(default="", max_length=200)
    # Free Serper keys reject more than 10; see SERPER_MAX_RESULTS.
    per_query_results: int = Field(default=10, ge=1, le=100)
    max_candidates: int = Field(default=40, ge=1, le=200)
    use_agent: bool = Field(default=True, description="Let the OpenRouter agent expand the plan.")
    # Repeat runs otherwise return Google's same top ten. On by default: seeing
    # the same people again is the complaint, not the goal.
    fresh_only: bool = Field(default=True, description="Skip results already shown to this user.")
    # The edited profile. When absent, the stored one is used, and failing that
    # it is read from the resume.
    profile: Optional[Profile] = None
    # Postings older than this are dropped unless the seeker opts in: an old
    # posting is usually filled, and applying late rarely works.
    max_age_days: int = Field(default=30, ge=1, le=365)
    include_older: bool = False


class SearchPlan(BaseModel):
    role_target: str = ""
    companies: list[str] = Field(default_factory=list)
    titles: list[str] = Field(default_factory=list)
    summary: str = ""
    agent_used: bool = False


class Candidate(BaseModel):
    """A person found in public search results, before scoring."""

    name: str
    headline: str = ""
    snippet: str = ""
    linkedin_url: str
    company_query: str = ""
    title_query: str = ""


class ScoredCandidate(Candidate):
    fit_score: Optional[int] = Field(default=None, ge=0, le=100)
    # Signals that make a referral more likely to land, found in the snippet.
    alumni: bool = False
    location_match: bool = False
    # Set when the person was found for a specific opening.
    for_opening: Optional[str] = None
    plausible_contact: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    priority: Optional[Literal["high", "medium", "low"]] = None
    # Why this person is worth connecting to, and what to actually ask them.
    reason: Optional[str] = Field(default=None, max_length=400)
    ask: Optional[str] = Field(default=None, max_length=400)
    error: Optional[str] = None


# --- openings ----------------------------------------------------------------
class Opening(BaseModel):
    """A job posting, before scoring.

    Most come straight from an ATS board's API (`verified`): live, complete,
    with the full description. The rest come from web search for companies
    whose board this app cannot read, and carry only a snippet.
    """

    key: str = ""             # ats:slug:job_id, or the URL for web results
    title: str
    company: str = ""
    source: str = ""          # greenhouse | lever | ashby | workable | ... | web | linkedin
    url: str
    apply_url: str = ""
    snippet: str = ""
    query: str = ""
    location: str = ""
    remote: Optional[bool] = None
    department: str = ""
    level: str = ""           # intern | entry | mid | senior, read from the posting
    min_years: Optional[int] = None
    pay: str = ""             # paid | unpaid | "" (not stated), read from the posting
    work_mode: str = ""       # remote | hybrid | onsite | "" (not stated)
    # Set once the verify stage has read the whole posting (verify.py).
    checked: bool = False
    check_note: Optional[str] = None
    in_location: Optional[bool] = None
    # Shown to this user on an earlier run; back only because too little was new.
    seen_before: bool = False
    verified: bool = False    # listed by the board's own API during this run
    # The site refused an automated visit (Indeed, Glassdoor, Upwork), so the
    # link could not be confirmed open. Shown as such, never as live.
    unconfirmed: bool = False
    # The full description. Used for scoring and never sent to the browser --
    # the board link has it, and forty of them would bloat every response.
    description: str = Field(default="", exclude=True)
    # Filled from the board's own API where one exists. Real dates, not guesses.
    posted_at: Optional[str] = None
    closes_at: Optional[str] = None
    # The first time this app saw the posting. An upper bound on when it opened
    # -- never a substitute for posted_at, and only useful once the app has been
    # running long enough to have seen a posting appear.
    first_seen: Optional[str] = None


class ScoredOpening(Opening):
    fit_score: Optional[int] = Field(default=None, ge=0, le=100)
    matches_profile: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    priority: Optional[Literal["high", "medium", "low"]] = None
    # One line on why it fits, and the single biggest thing working against it.
    why: Optional[str] = Field(default=None, max_length=400)
    gap: Optional[str] = Field(default=None, max_length=400)
    error: Optional[str] = None
    # Stage one: embedding similarity between resume and description, 0..1.
    similarity: Optional[float] = None
    # Stage two, only for the top of the list: narrow judgements on the full text.
    reviewed: bool = False
    must_haves_met: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    level_fit: Optional[Literal["under", "fit", "over"]] = None
    skills_matched: list[str] = Field(default_factory=list)
    skills_missing: list[str] = Field(default_factory=list)
    # What went into fit_score, kept so feedback can be learned from.
    features: dict[str, float] = Field(default_factory=dict)
    # "When does this company usually post roles like this", if observed.
    timing_note: Optional[str] = None
    feedback: Optional[dict] = None


class OpeningsResponse(BaseModel):
    count: int
    plan: "SearchPlan"
    queries_run: list[str]
    results: list[ScoredOpening]
    warnings: list[str] = Field(default_factory=list)
    runs_used: int = 0
    runs_allowed: int = 0
    run_id: Optional[int] = None
    profile: Optional[Profile] = None
    # What the hard filters removed, by reason -- shown so a parser mistake
    # is visible instead of silently deleting good results.
    filtered: dict[str, int] = Field(default_factory=dict)
    boards_read: list[str] = Field(default_factory=list)
    companies_without_board: list[str] = Field(default_factory=list)


class SearchResponse(BaseModel):
    count: int
    plan: SearchPlan
    queries_run: list[str]
    results: list[ScoredCandidate]
    warnings: list[str] = Field(default_factory=list)
    runs_used: int = 0
    runs_allowed: int = 0
    run_id: Optional[int] = None


class PeopleForOpeningRequest(BaseModel):
    """Find the people behind one specific opening."""

    opening_key: str = Field(..., max_length=600)
    title: str = Field(..., max_length=200)
    company: str = Field(..., max_length=120)
    department: str = Field(default="", max_length=120)
    location: str = Field(default="", max_length=200)
    level: str = Field(default="", max_length=20)
    resume: str = Field(..., min_length=20, max_length=20_000)
    profile: Optional[Profile] = None
    max_candidates: int = Field(default=15, ge=1, le=40)


class ApproachRequest(BaseModel):
    """Draft outreach for one result the seeker clicked on."""

    kind: Literal["person", "opening"]
    resume: str = Field(..., min_length=20, max_length=20_000)
    role_target: str = Field(default="", max_length=200)
    # Whatever the card already knows about the target. Kept loose because the
    # two modes carry different fields, and the drafter only reads the ones it
    # needs for that kind.
    name: str = Field(default="", max_length=120)
    headline: str = Field(default="", max_length=300)
    title: str = Field(default="", max_length=200)
    company: str = Field(default="", max_length=120)
    snippet: str = Field(default="", max_length=600)
    posted_at: str = Field(default="", max_length=40)
    url: str = Field(default="", max_length=600)
    # For a person found through an opening: the role to mention by name.
    opening_title: str = Field(default="", max_length=200)
    opening_url: str = Field(default="", max_length=600)
    opening_key: str = Field(default="", max_length=600)


class ApproachResponse(BaseModel):
    headline: str = ""
    connection_note: str = ""
    message: str = ""
    talking_points: list[str] = Field(default_factory=list)
    gap: str = ""


class ResumeUploadResponse(BaseModel):
    resume: str
    chars: int
    source: str


# --- feedback ------------------------------------------------------------------
class FeedbackRequest(BaseModel):
    kind: Literal["person", "opening", "gig"]
    item_key: str = Field(..., max_length=600)
    run_id: Optional[int] = None
    url: str = Field(default="", max_length=600)
    title: str = Field(default="", max_length=200)
    rating: Optional[Literal[-1, 0, 1]] = None
    applied: Optional[bool] = None
    messaged: Optional[bool] = None
    replied: Optional[bool] = None


class FeedbackState(BaseModel):
    rating: int = 0
    applied: bool = False
    messaged: bool = False
    replied: bool = False


# --- saved searches and alerts ---------------------------------------------------
class SavedSearchRequest(BaseModel):
    name: str = Field(default="", max_length=120)
    resume: str = Field(..., min_length=20, max_length=20_000)
    profile: Optional[Profile] = None
    roles: list[str] = Field(default_factory=list, max_length=8)
    companies: list[str] = Field(default_factory=list, max_length=25)
    min_score: int = Field(default=60, ge=0, le=100)


class SavedSearch(BaseModel):
    id: int
    name: str
    roles: list[str]
    companies: list[str]
    min_score: int
    created_at: str
    last_checked: str


class Alert(BaseModel):
    id: int
    search_id: int
    search_name: str
    posting_key: str
    score: int
    title: str
    company: str
    url: str
    location: str = ""
    posted_at: Optional[str] = None
    first_seen: str
    created_at: str
    seen: bool
    closed: bool = False
