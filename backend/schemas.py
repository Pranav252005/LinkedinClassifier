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


# --- search ------------------------------------------------------------------
class SearchRequest(BaseModel):
    # "people" finds humans to contact; "jobs" finds openings you can apply to.
    mode: Literal["people", "jobs"] = "people"
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
    plausible_contact: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    priority: Optional[Literal["high", "medium", "low"]] = None
    # Why this person is worth connecting to, and what to actually ask them.
    reason: Optional[str] = Field(default=None, max_length=400)
    ask: Optional[str] = Field(default=None, max_length=400)
    error: Optional[str] = None


# --- openings ----------------------------------------------------------------
class Opening(BaseModel):
    """A job posting found in public search results, before scoring."""

    title: str
    company: str = ""
    source: str = ""          # greenhouse | lever | ashby | linkedin
    url: str
    snippet: str = ""
    query: str = ""
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


class OpeningsResponse(BaseModel):
    count: int
    plan: "SearchPlan"
    queries_run: list[str]
    results: list[ScoredOpening]
    warnings: list[str] = Field(default_factory=list)
    runs_used: int = 0
    runs_allowed: int = 0


class SearchResponse(BaseModel):
    count: int
    plan: SearchPlan
    queries_run: list[str]
    results: list[ScoredCandidate]
    warnings: list[str] = Field(default_factory=list)
    runs_used: int = 0
    runs_allowed: int = 0


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
