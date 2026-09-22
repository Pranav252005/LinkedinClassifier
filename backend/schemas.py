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


class ResumeUploadResponse(BaseModel):
    resume: str
    chars: int
    source: str
