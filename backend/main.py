"""LeadClassifier — FastAPI app: auth, billing, sourcing, scoring, frontend."""

from __future__ import annotations

import csv
import io
import logging
import sqlite3
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import Depends, FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

import agent
import auth
import billing
import db
import outreach
import resume as resume_parser
from config import (
    FRONTEND_DIR,
    LOGIN_LIMIT,
    LOGIN_WINDOW_SECONDS,
    MAX_UPLOAD_BYTES,
    PRO_PRICE_LABEL,
    RUN_LIMIT,
    RUN_WINDOW_SECONDS,
    SESSION_COOKIE,
    SIGNUP_LIMIT,
    SIGNUP_WINDOW_SECONDS,
    billing_enabled,
    is_comp_account,
    runs_allowed,
)
from openrouter import OpenRouterError
from ratelimit import RateLimiter, enforce
import timing
from jobs import find_openings
from scoring import score_candidates, score_openings
from schemas import (
    ApproachRequest,
    ApproachResponse,
    AccountInfo,
    Credentials,
    OpeningsResponse,
    ResumeUploadResponse,
    ScoredCandidate,
    ScoredOpening,
    SearchRequest,
    SearchResponse,
)
from search import SearchError, build_queries, run_queries

log = logging.getLogger("jcs")


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    db.init_db()
    try:
        yield
    finally:
        db.close()


app = FastAPI(title="LeadClassifier", version="0.2.0", lifespan=lifespan)

_signup_limit = RateLimiter(SIGNUP_LIMIT, SIGNUP_WINDOW_SECONDS, "signup")
_login_limit = RateLimiter(LOGIN_LIMIT, LOGIN_WINDOW_SECONDS, "login")
_run_limit = RateLimiter(RUN_LIMIT, RUN_WINDOW_SECONDS, "run")
# Drafting outreach costs one cheap model call and no search credits, so it gets
# its own, looser budget -- clicking through a result list should not burn the
# run limit.
_draft_limit = RateLimiter(RUN_LIMIT * 6, RUN_WINDOW_SECONDS, "draft")


# --- pages -------------------------------------------------------------------
app.mount("/static", StaticFiles(directory=FRONTEND_DIR / "static"), name="static")


def _page(name: str) -> FileResponse:
    return FileResponse(FRONTEND_DIR / name)


@app.get("/", include_in_schema=False)
async def landing() -> FileResponse:
    return _page("index.html")


@app.get("/login", include_in_schema=False)
async def login_page(user: sqlite3.Row | None = Depends(auth.optional_user)):
    return RedirectResponse("/app", status_code=302) if user else _page("login.html")


@app.get("/app", include_in_schema=False)
async def app_page(user: sqlite3.Row | None = Depends(auth.optional_user)):
    return _page("app.html") if user else RedirectResponse("/login", status_code=302)


# --- auth --------------------------------------------------------------------
@app.post("/api/auth/signup", response_model=AccountInfo)
async def signup(creds: Credentials, request: Request, response: Response) -> AccountInfo:
    enforce(_signup_limit, request, "Too many accounts created from this address. Try again later.")
    email = auth.normalize_email(creds.email)
    auth.validate_credentials(email, creds.password)
    if db.get_user_by_email(email):
        raise HTTPException(status_code=409, detail="An account with that email already exists.")
    user_id = db.create_user(email, auth.hash_password(creds.password))
    auth.issue_session(response, user_id)
    return _account(db.get_user(user_id))


@app.post("/api/auth/login", response_model=AccountInfo)
async def login(creds: Credentials, request: Request, response: Response) -> AccountInfo:
    enforce(_login_limit, request, "Too many sign-in attempts. Try again shortly.")
    email = auth.normalize_email(creds.email)
    user = db.get_user_by_email(email)
    # Same message either way — don't reveal which emails have accounts.
    if user is None or not auth.verify_password(creds.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Email or password is incorrect.")
    auth.issue_session(response, user["id"])
    return _account(user)


@app.post("/api/auth/logout")
async def logout(response: Response) -> dict[str, bool]:
    auth.clear_session(response)
    return {"ok": True}


def effective_plan(user: sqlite3.Row) -> str:
    """The plan actually in force — a comp account is Pro regardless of billing."""
    if is_comp_account(user["email"]):
        return "pro"
    return user["plan"] if user["plan"] in ("free", "pro") else "free"


def _account(user: sqlite3.Row) -> AccountInfo:
    plan = effective_plan(user)
    comp = is_comp_account(user["email"])
    return AccountInfo(
        email=user["email"],
        plan=plan,
        runs_used=db.runs_this_month(user["id"]),
        runs_allowed=runs_allowed(plan),
        plan_renews_at=None if comp else user["plan_renews_at"],
        # A comp account has nothing to buy, so don't offer it an upgrade button.
        billing_enabled=billing_enabled() and not comp,
        price_label=PRO_PRICE_LABEL,
    )


@app.get("/api/account", response_model=AccountInfo)
async def account(user: sqlite3.Row = Depends(auth.current_user)) -> AccountInfo:
    return _account(user)


# --- billing -----------------------------------------------------------------
@app.post("/api/billing/checkout")
async def checkout(user: sqlite3.Row = Depends(auth.current_user)) -> dict[str, str]:
    if effective_plan(user) == "pro":
        raise HTTPException(status_code=400, detail="You're already on Pro.")
    try:
        return {"url": billing.create_checkout_session(user)}
    except billing.BillingError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:  # Stripe API errors
        log.exception("checkout failed")
        raise HTTPException(status_code=502, detail=f"Stripe error: {exc}") from exc


@app.post("/api/billing/portal")
async def portal(user: sqlite3.Row = Depends(auth.current_user)) -> dict[str, str]:
    try:
        return {"url": billing.create_portal_session(user)}
    except billing.BillingError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/billing/webhook", include_in_schema=False)
async def webhook(request: Request) -> dict[str, str]:
    payload = await request.body()
    try:
        event = billing.verify_event(payload, request.headers.get("stripe-signature"))
    except billing.BillingError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    result = billing.apply_event(event)
    log.info("stripe webhook: %s", result)
    return {"status": result}


# --- resume upload -----------------------------------------------------------
@app.post("/api/resume", response_model=ResumeUploadResponse)
async def upload_resume(
    file: UploadFile = File(...), user: sqlite3.Row = Depends(auth.current_user)
) -> ResumeUploadResponse:
    data = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413, detail=f"File is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB."
        )
    if not data:
        raise HTTPException(status_code=400, detail="That file is empty.")
    try:
        text, source = await resume_parser.extract(data, file.content_type or "", file.filename or "")
    except resume_parser.ResumeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return ResumeUploadResponse(resume=text, chars=len(text), source=source)


# --- the pipeline ------------------------------------------------------------

# --- freshness ---------------------------------------------------------------
# Identical queries return Google's identical top ten, so without this every
# repeat run showed the same faces. None of it is fatal: if the bookkeeping
# fails, the run still happens, it just repeats itself.

def _already_seen(user_id: int, kind: str, fresh_only: bool) -> frozenset[str]:
    if not fresh_only:
        return frozenset()
    try:
        return frozenset(db.seen_urls(user_id, kind))
    except Exception as exc:
        log.warning("could not load seen %ss: %s", kind, exc)
        return frozenset()


def _remember(user_id: int, kind: str, urls: list[str]) -> None:
    try:
        db.record_seen(user_id, kind, urls)
    except Exception as exc:
        log.warning("could not record seen %ss: %s", kind, exc)


def _freshness_note(skipped: int, noun: str, fresh_only: bool) -> list[str]:
    if not fresh_only or skipped <= 0:
        return []
    return [f"Skipped {skipped} {noun} you have already been shown, and searched "
            "deeper pages instead. Turn off “only show me new results” to see them again."]


def _nothing_new_note(skipped: int, noun: str) -> str:
    if skipped:
        return (f"Nothing new — every match was one of the {skipped} {noun} you have already "
                "been shown. Try a different role or company, or turn off "
                "“only show me new results”.")
    return f"No matching {noun} were publicly indexed for these searches."


async def _run_pipeline(req: SearchRequest, user: sqlite3.Row) -> SearchResponse:
    plan = effective_plan(user)
    used, allowed = db.runs_this_month(user["id"]), runs_allowed(plan)
    if used >= allowed:
        raise HTTPException(
            status_code=402,
            detail=(
                f"You've used all {allowed} searches this month on the "
                f"{plan.title()} plan."
                + ("" if plan == "pro" else " Upgrade to Pro for more.")
            ),
        )

    warnings: list[str] = []

    # 1. Plan — the OpenRouter agent expands resume + hints into companies/titles.
    if req.use_agent:
        plan, plan_warnings = await agent.plan_search(
            req.resume, req.companies, req.titles, req.role_target
        )
        warnings += plan_warnings
    else:
        plan = agent.fallback_plan(req.companies, req.titles, req.role_target)

    if not plan.companies:
        raise HTTPException(
            status_code=400,
            detail="No target companies — name at least one, or let the agent suggest some.",
        )

    # 2. Source — Serper over site:linkedin.com/in, skipping anyone this user
    # has already been shown so a repeat run finds new people.
    already = _already_seen(user["id"], "person", req.fresh_only)
    specs = build_queries(plan.companies, plan.titles)
    try:
        candidates, ran, skipped = await run_queries(
            specs, req.per_query_results, req.max_candidates, already
        )
    except SearchError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    warnings += _freshness_note(skipped, "people", req.fresh_only)

    # Report the queries actually sent, not the ones merely planned.
    queries = [spec.query for spec in ran]
    if not candidates:
        db.record_run(user["id"], ", ".join(plan.companies), 0)
        return SearchResponse(
            count=0,
            plan=plan,
            queries_run=queries,
            results=[],
            warnings=warnings + [_nothing_new_note(skipped, "profiles")],
            runs_used=used + 1,
            runs_allowed=allowed,
        )

    # 3. Classify — Jev scores every candidate against the resume.
    scored, score_warnings = await score_candidates(candidates, req.resume, plan.role_target)
    db.record_run(user["id"], ", ".join(plan.companies), len(scored))
    _remember(user["id"], "person", [c.linkedin_url for c in scored])

    return SearchResponse(
        count=len(scored),
        plan=plan,
        queries_run=queries,
        results=scored,
        warnings=warnings + score_warnings,
        runs_used=used + 1,
        runs_allowed=allowed,
    )


async def _run_openings(req: SearchRequest, user: sqlite3.Row) -> OpeningsResponse:
    plan_ = effective_plan(user)
    used, allowed = db.runs_this_month(user["id"]), runs_allowed(plan_)
    if used >= allowed:
        raise HTTPException(
            status_code=402,
            detail=(
                f"You've used all {allowed} searches this month on the {plan_.title()} plan."
                + ("" if plan_ == "pro" else " Upgrade to Pro for more.")
            ),
        )

    warnings: list[str] = []

    # The agent plans job titles to search for here, not people to contact.
    if req.use_agent:
        plan, plan_warnings = await agent.plan_search(
            req.resume, req.companies, req.titles, req.role_target, for_jobs=True
        )
        warnings += plan_warnings
    else:
        plan = agent.fallback_plan(req.companies, req.titles, req.role_target)

    roles = plan.titles or [req.role_target.strip()] or ["software engineer intern"]
    already = _already_seen(user["id"], "opening", req.fresh_only)
    try:
        openings, queries, skipped = await find_openings(
            roles, plan.companies, req.per_query_results, req.max_candidates, already
        )
    except SearchError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    warnings += _freshness_note(skipped, "openings", req.fresh_only)

    if not openings:
        db.record_run(user["id"], ", ".join(plan.companies) or "any", 0)
        return OpeningsResponse(
            count=0, plan=plan, queries_run=queries, results=[],
            warnings=warnings + [_nothing_new_note(skipped, "openings")],
            runs_used=used + 1, runs_allowed=allowed,
        )

    # Real posted/closing dates from the boards' own feeds, before scoring so
    # the ranking can see them.
    timing_warnings = await timing.enrich(openings)

    # Record what this run saw. The boards publish no history, so the only
    # honest route to "when does this company open applications" is to
    # accumulate observations -- see the note in db.record_sightings.
    try:
        first_seen = db.record_sightings(openings)
        for opening in openings:
            opening.first_seen = first_seen.get(opening.url)
    except Exception as exc:  # history is a nice-to-have; never fail a run for it
        log.warning("could not record opening sightings: %s", exc)

    scored, score_warnings = await score_openings(openings, req.resume, plan.role_target)
    db.record_run(user["id"], ", ".join(plan.companies) or "any", len(scored))
    _remember(user["id"], "opening", [o.url for o in scored])

    return OpeningsResponse(
        count=len(scored), plan=plan, queries_run=queries, results=scored,
        warnings=warnings + timing_warnings + score_warnings,
        runs_used=used + 1, runs_allowed=allowed,
    )


@app.post("/api/openings", response_model=OpeningsResponse)
async def openings(
    req: SearchRequest, request: Request, user: sqlite3.Row = Depends(auth.current_user)
) -> OpeningsResponse:
    enforce(_run_limit, request, "Too many runs in a short time. Try again shortly.")
    return await _run_openings(req, user)



@app.post("/api/approach", response_model=ApproachResponse)
async def approach(
    req: ApproachRequest, request: Request, user: sqlite3.Row = Depends(auth.current_user)
) -> ApproachResponse:
    """What to actually say to one person, or about one posting.

    Deliberately not counted against the monthly run quota: it spends no search
    credits, and charging a run to find out how to approach someone you already
    paid to find would be a strange thing to do to a user.
    """
    enforce(_draft_limit, request, "Too many drafts in a short time. Try again shortly.")
    try:
        drafted = await outreach.draft(
            req.kind,
            {
                "name": req.name,
                "headline": req.headline,
                "title": req.title,
                "company": req.company,
                "snippet": req.snippet,
                "posted_at": req.posted_at,
            },
            req.resume,
            req.role_target,
        )
    except OpenRouterError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return ApproachResponse(**drafted)


@app.post("/api/search", response_model=SearchResponse)
async def search(
    req: SearchRequest, request: Request, user: sqlite3.Row = Depends(auth.current_user)
) -> SearchResponse:
    enforce(_run_limit, request, "Too many runs in a short time. Try again shortly.")
    return await _run_pipeline(req, user)


@app.post("/api/search.csv")
async def search_csv(
    req: SearchRequest, user: sqlite3.Row = Depends(auth.current_user)
) -> StreamingResponse:
    response = await _run_pipeline(req, user)
    return StreamingResponse(
        io.BytesIO(_to_csv(response.results).encode("utf-8")),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="contacts.csv"'},
    )


def _to_csv(rows: list[ScoredCandidate]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        ["name", "headline", "company", "linkedin_url", "fit_score", "plausible_contact",
         "priority", "reason", "ask", "note"]
    )
    for row in rows:
        writer.writerow(
            [
                row.name,
                row.headline,
                row.company_query,
                row.linkedin_url,
                "" if row.fit_score is None else row.fit_score,
                "" if row.plausible_contact is None else f"{row.plausible_contact:.3f}",
                row.priority or "",
                row.reason or "",
                row.ask or "",
                row.error or "",
            ]
        )
    return buffer.getvalue()


@app.post("/api/openings.csv")
async def openings_csv(
    req: SearchRequest, request: Request, user: sqlite3.Row = Depends(auth.current_user)
) -> StreamingResponse:
    enforce(_run_limit, request, "Too many runs in a short time. Try again shortly.")
    response = await _run_openings(req, user)
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["fit_score", "title", "company", "source", "url", "posted_at",
                     "closes_at", "first_seen", "matches_profile", "priority",
                     "why", "gap", "note"])
    for row in response.results:
        writer.writerow([
            "" if row.fit_score is None else row.fit_score,
            row.title, row.company, row.source, row.url,
            row.posted_at or "", row.closes_at or "", row.first_seen or "",
            "" if row.matches_profile is None else f"{row.matches_profile:.3f}",
            row.priority or "", row.why or "", row.gap or "", row.error or "",
        ])
    return StreamingResponse(
        io.BytesIO(buffer.getvalue().encode("utf-8")),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="openings.csv"'},
    )


@app.get("/api/health")
async def health() -> dict[str, object]:
    from config import OPENROUTER_API_KEY, SERPER_API_KEY, TYPESAFE_API_KEY

    from config import COMP_ACCOUNTS, SCORING_PROVIDER

    return {
        "ok": True,
        "storage": db.backend_name(),
        "serper_key_set": bool(SERPER_API_KEY),
        "openrouter_key_set": bool(OPENROUTER_API_KEY),
        "typesafe_key_set": bool(TYPESAFE_API_KEY),
        "scoring_provider": SCORING_PROVIDER,
        # Count only — never echo the addresses back over a public endpoint.
        "comp_accounts": len(COMP_ACCOUNTS),
        "scoring_ready": bool(OPENROUTER_API_KEY) if SCORING_PROVIDER == "openrouter" else bool(TYPESAFE_API_KEY),
        "billing_enabled": billing_enabled(),
    }
