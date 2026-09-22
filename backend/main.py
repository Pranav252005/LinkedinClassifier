"""LeadClassifier — FastAPI app: auth, billing, sourcing, scoring, frontend."""

from __future__ import annotations

import asyncio
import csv
import hmac
import io
import json
import logging
import secrets
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import AsyncIterator

from fastapi import Depends, FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

import agent
import auth
import billing
import db
import embeddings
import filters
import meter
import openings as openings_pipeline
import outreach
import people
import poller
import registry
import resume as resume_parser
import seeker
import timing
from config import (
    FRONTEND_DIR,
    LOGIN_LIMIT,
    LOGIN_WINDOW_SECONDS,
    MAX_UPLOAD_BYTES,
    POLL_TOKEN,
    PRO_PRICE_LABEL,
    RUN_LIMIT,
    RUN_WINDOW_SECONDS,
    SESSION_COOKIE,
    SIGNUP_LIMIT,
    SIGNUP_WINDOW_SECONDS,
    billing_enabled,
    is_comp_account,
    runs_allowed,
    saved_searches_allowed,
)
from openrouter import OpenRouterError
from ratelimit import RateLimiter, enforce
from scoring import score_candidates
from schemas import (
    AccountInfo,
    Alert,
    ApproachRequest,
    ApproachResponse,
    Credentials,
    FeedbackRequest,
    FeedbackState,
    OpeningsResponse,
    PeopleForOpeningRequest,
    Profile,
    ProfileRequest,
    ResumeUploadResponse,
    SavedSearch,
    SavedSearchRequest,
    ScoredCandidate,
    ScoredOpening,
    SearchPlan,
    SearchRequest,
    SearchResponse,
)
from search import QuerySpec, SearchError, build_queries, run_queries

log = logging.getLogger("jcs")


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    db.init_db()
    try:
        yield
    finally:
        db.close()


app = FastAPI(title="LeadClassifier", version="0.3.0", lifespan=lifespan)

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


# --- structured profile ------------------------------------------------------
@app.post("/api/profile", response_model=Profile)
async def read_profile(req: ProfileRequest, request: Request,
                       user: sqlite3.Row = Depends(auth.current_user)) -> Profile:
    """Read the resume into editable fields. One cheap model call, no run spent."""
    enforce(_draft_limit, request, "Too many requests in a short time. Try again shortly.")
    profile, _warnings = await seeker.extract(req.resume)
    _store_profile(user["id"], profile)
    return profile


@app.get("/api/profile")
async def get_profile(user: sqlite3.Row = Depends(auth.current_user)) -> dict:
    stored = _stored_profile(user["id"])
    return {"profile": stored.model_dump() if stored else None}


@app.put("/api/profile", response_model=Profile)
async def put_profile(profile: Profile, user: sqlite3.Row = Depends(auth.current_user)) -> Profile:
    _store_profile(user["id"], profile)
    return profile


def _stored_profile(user_id: int) -> Profile | None:
    try:
        raw = db.get_profile(user_id)
        return Profile.model_validate_json(raw) if raw else None
    except Exception:
        return None


def _store_profile(user_id: int, profile: Profile) -> None:
    try:
        db.save_profile(user_id, profile.model_dump_json())
    except Exception as exc:
        log.warning("could not store profile: %s", exc)


async def _resolve_profile(given: Profile | None, resume: str, user_id: int,
                           extract: bool = True) -> tuple[Profile | None, list[str]]:
    """The edited profile if sent, else the stored one, else read from the resume."""
    if given is not None:
        _store_profile(user_id, given)
        return given, []
    stored = _stored_profile(user_id)
    if stored is not None or not extract:
        return stored, []
    profile, warnings = await seeker.extract(resume)
    _store_profile(user_id, profile)
    return profile, warnings


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
    return [f"Skipped {skipped} {noun} you have already been shown. Turn off "
            "“only show me new results” to see them again."]


def _nothing_new_note(skipped: int, noun: str) -> str:
    if skipped:
        return (f"Nothing new — every match was one of the {skipped} {noun} you have already "
                "been shown. Try a different role or company, or turn off "
                "“only show me new results”.")
    return f"No matching {noun} were found for these searches."


def _check_quota(user: sqlite3.Row) -> tuple[int, int]:
    plan = effective_plan(user)
    used, allowed = db.runs_this_month(user["id"]), runs_allowed(plan)
    if used >= allowed:
        raise HTTPException(
            status_code=402,
            detail=(f"You've used all {allowed} searches this month on the {plan.title()} plan."
                    + ("" if plan == "pro" else " Upgrade to Pro for more.")),
        )
    return used, allowed


def _cap_results(user: sqlite3.Row, requested: int) -> int:
    """The plan's per-run ceiling, enforced here rather than trusted from the page."""
    return min(requested, 200 if effective_plan(user) == "pro" else 60)


def _finish_run(user: sqlite3.Row, kind: str, companies: str, rows: list[tuple]) -> int | None:
    """Count the run, keep what it returned (for feedback and evals), price it.

    rows are (item_key, rank, fit_score, features_json, url, title). Everything
    after the quota count is best-effort: bookkeeping never fails a run.
    """
    run_id = db.record_run(user["id"], companies, len(rows))
    try:
        db.save_run_results(run_id, kind, rows)
    except Exception as exc:
        log.warning("could not store run results: %s", exc)
    try:
        db.record_cost(run_id, user["id"], kind, meter.current().summary())
    except Exception as exc:
        log.warning("could not record run cost: %s", exc)
    return run_id


def _attach_feedback(user_id: int, kind: str, results: list, key_of) -> None:
    try:
        known = db.feedback_for(user_id, kind, [key_of(r) for r in results])
    except Exception:
        return
    for r in results:
        row = known.get(key_of(r))
        if row:
            r.feedback = FeedbackState(rating=row["rating"], applied=bool(row["applied"]),
                                       messaged=bool(row["messaged"]),
                                       replied=bool(row["replied"])).model_dump()


def _person_rows(results: list[ScoredCandidate]) -> list[tuple]:
    return [(c.linkedin_url, i, c.fit_score,
             json.dumps({"plausible": c.plausible_contact,
                         "priority": {"high": 1.0, "medium": 0.65, "low": 0.3}.get(c.priority or "", None),
                         "alumni": 1.0 if c.alumni else 0.0,
                         "location": 1.0 if c.location_match else 0.0}),
             c.linkedin_url, c.name)
            for i, c in enumerate(results)]


async def _run_pipeline(req: SearchRequest, user: sqlite3.Row) -> SearchResponse:
    used, allowed = _check_quota(user)
    meter.start()
    warnings: list[str] = []
    max_results = _cap_results(user, req.max_candidates)

    # A stored profile sharpens planning and alumni ranking, but people search
    # never pays to extract one: the resume alone is enough to plan from.
    profile, _ = await _resolve_profile(req.profile, req.resume, user["id"], extract=False)
    profile_text = seeker.as_text(profile) if profile else ""

    # 1. Plan — the OpenRouter agent expands resume + hints into companies/titles.
    if req.use_agent:
        plan, plan_warnings = await agent.plan_search(
            req.resume, req.companies, req.titles, req.role_target, profile_text=profile_text)
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
    if profile and profile.college:
        # Alumni at each target company: the warmest cold message there is.
        specs = [QuerySpec(query=f'site:linkedin.com/in "{c}" "{profile.college}"', company=c,
                           title=f"{profile.college} alumni") for c in plan.companies[:4]] + specs
    try:
        candidates, ran, skipped = await run_queries(specs, req.per_query_results, max_results, already)
    except SearchError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    warnings += _freshness_note(skipped, "people", req.fresh_only)
    queries = [spec.query for spec in ran]
    if not candidates:
        run_id = _finish_run(user, "person", ", ".join(plan.companies), [])
        return SearchResponse(count=0, plan=plan, queries_run=queries, results=[],
                              warnings=warnings + [_nothing_new_note(skipped, "profiles")],
                              runs_used=used + 1, runs_allowed=allowed, run_id=run_id)

    # 3. Classify, then mark alumni / location matches.
    scored, score_warnings = await score_candidates(candidates, req.resume, plan.role_target)
    people.annotate(scored, profile)
    scored = people.rank(scored)
    run_id = _finish_run(user, "person", ", ".join(plan.companies), _person_rows(scored))
    _remember(user["id"], "person", [c.linkedin_url for c in scored])
    _attach_feedback(user["id"], "person", scored, lambda c: c.linkedin_url)

    return SearchResponse(count=len(scored), plan=plan, queries_run=queries, results=scored,
                          warnings=warnings + score_warnings, runs_used=used + 1,
                          runs_allowed=allowed, run_id=run_id)


def _merge(*lists: list[str], limit: int) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for items in lists:
        for item in items or []:
            value = " ".join(str(item).split())
            if value and value.lower() not in seen:
                seen.add(value.lower())
                out.append(value)
    return out[:limit]


async def _run_openings(req: SearchRequest, user: sqlite3.Row) -> OpeningsResponse:
    used, allowed = _check_quota(user)
    meter.start()
    warnings: list[str] = []
    max_results = _cap_results(user, req.max_candidates)

    # 0. Profile — edited chips from the page, else stored, else read now.
    profile, profile_warnings = await _resolve_profile(req.profile, req.resume, user["id"])
    warnings += profile_warnings
    profile_text = seeker.as_text(profile)

    # 1. Plan — roles from the seeker and the profile; companies from both plus the agent.
    if req.use_agent:
        plan, plan_warnings = await agent.plan_search(
            req.resume, req.companies, req.titles or profile.target_roles, req.role_target,
            for_jobs=True, profile_text=profile_text)
        warnings += plan_warnings
    else:
        plan = agent.fallback_plan(req.companies, req.titles, req.role_target)
        # The fallback fills in people-search titles ("university recruiter");
        # here titles are jobs to apply for, so keep only what the seeker gave.
        plan.titles = list(req.titles)
        plan.summary = "Planned from your profile and input (agent off)."
    roles = _merge(req.titles, profile.target_roles, plan.titles,
                   [req.role_target] if req.role_target.strip() else [], limit=6)
    if not roles:
        roles = ["software engineer intern" if profile.seniority == "intern" else "software engineer"]
    plan.titles = roles

    # 2-5. Boards, fallback search, dedupe, hard filters.
    already = _already_seen(user["id"], "opening", req.fresh_only)
    got = await openings_pipeline.gather(profile, roles, plan.companies, already, req.max_age_days,
                                         req.include_older, max_results)
    warnings += got["warnings"]
    warnings += _freshness_note(got["skipped"], "openings", req.fresh_only)
    filtered_note = filters.describe(got["dropped"])
    if filtered_note:
        warnings.append(filtered_note)

    common = dict(plan=plan, queries_run=got["queries"], runs_allowed=allowed, profile=profile,
                  filtered=got["dropped"], boards_read=got["boards"],
                  companies_without_board=got["missing"])

    if not got["openings"]:
        run_id = _finish_run(user, "opening", ", ".join(plan.companies) or "any", [])
        empty = (_nothing_new_note(got["skipped"], "openings") if got["pool_size"] <= got["skipped"]
                 else "Every posting found was hidden by your profile's filters — loosen them "
                      "(level, locations, age) and run again.")
        return OpeningsResponse(count=0, results=[], warnings=warnings + [empty],
                                runs_used=used + 1, run_id=run_id, **common)

    # 6. Rank: embeddings over everything, full-description review of the top N.
    ranked, rank_warnings = await openings_pipeline.rank(got["openings"], profile, req.resume)
    warnings += rank_warnings
    results = ranked[:max_results]
    openings_pipeline.annotate_timing(results)

    rows = [(o.key, i, o.fit_score, json.dumps(o.features), o.url, f"{o.title} — {o.company}")
            for i, o in enumerate(results)]
    run_id = _finish_run(user, "opening", ", ".join(plan.companies) or "any", rows)
    _remember(user["id"], "opening", [o.key for o in results])
    _attach_feedback(user["id"], "opening", results, lambda o: o.key)

    return OpeningsResponse(count=len(results), results=results, warnings=warnings,
                            runs_used=used + 1, run_id=run_id, **common)


@app.post("/api/openings", response_model=OpeningsResponse)
async def openings(
    req: SearchRequest, request: Request, user: sqlite3.Row = Depends(auth.current_user)
) -> OpeningsResponse:
    enforce(_run_limit, request, "Too many runs in a short time. Try again shortly.")
    return await _run_openings(req, user)


# --- people behind one opening ---------------------------------------------------
@app.post("/api/people-for-opening", response_model=SearchResponse)
async def people_for_opening(
    req: PeopleForOpeningRequest, request: Request, user: sqlite3.Row = Depends(auth.current_user)
) -> SearchResponse:
    """The recruiter, manager and team members around one specific posting.

    Counts as a run: it spends the same search credits a people run does.
    """
    enforce(_run_limit, request, "Too many runs in a short time. Try again shortly.")
    used, allowed = _check_quota(user)
    meter.start()
    profile, _ = await _resolve_profile(req.profile, req.resume, user["id"], extract=False)
    specs = people.build_queries(req.company, req.title, req.department, req.location, req.level,
                                 profile.college if profile else "")
    already = _already_seen(user["id"], "person", True)
    try:
        candidates, ran, skipped = await run_queries(specs, 10, req.max_candidates, already)
    except SearchError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    target = f"{req.title} at {req.company}"
    plan = SearchPlan(role_target=target, companies=[req.company], titles=[s.title for s in specs],
                      summary=f"People around the {req.title} opening at {req.company}: its recruiter, "
                              "the team's manager and engineers, and alumni of your college.",
                      agent_used=False)
    warnings = _freshness_note(skipped, "people", True)
    if not candidates:
        run_id = _finish_run(user, "person", req.company, [])
        return SearchResponse(count=0, plan=plan, queries_run=[s.query for s in ran], results=[],
                              warnings=warnings + [_nothing_new_note(skipped, "profiles")],
                              runs_used=used + 1, runs_allowed=allowed, run_id=run_id)

    scored, score_warnings = await score_candidates(candidates, req.resume, target)
    people.annotate(scored, profile, req.location)
    for c in scored:
        c.for_opening = req.title
    scored = people.rank(scored)
    run_id = _finish_run(user, "person", req.company, _person_rows(scored))
    _remember(user["id"], "person", [c.linkedin_url for c in scored])
    return SearchResponse(count=len(scored), plan=plan, queries_run=[s.query for s in ran],
                          results=scored, warnings=warnings + score_warnings,
                          runs_used=used + 1, runs_allowed=allowed, run_id=run_id)


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
    # The full description lives in the database, not the browser.
    posting_key = req.opening_key or (req.url if req.kind == "opening" else "")
    description = ""
    if posting_key:
        try:
            row = db.get_posting(posting_key)
            description = (row["description"] or "") if row else ""
        except Exception:
            description = ""
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
                "description": description,
                "opening_title": req.opening_title,
                "opening_url": req.opening_url,
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


# --- background runs ---------------------------------------------------------------
# A run takes 30-90 seconds, longer than Netlify's proxy waits (about 26), so
# the page starts one here and polls for the result. Kept in memory: there is
# one instance, and a run lost to a restart is simply started again.
_RUN_TTL = timedelta(minutes=15)
_runs: dict[str, dict] = {}


def _prune_runs() -> None:
    now = datetime.now(timezone.utc)
    for rid in [r for r, job in _runs.items() if now - job["at"] > _RUN_TTL]:
        _runs.pop(rid, None)


@app.post("/api/runs")
async def start_run(
    req: SearchRequest, request: Request, user: sqlite3.Row = Depends(auth.current_user)
) -> dict:
    enforce(_run_limit, request, "Too many runs in a short time. Try again shortly.")
    _prune_runs()
    rid = secrets.token_urlsafe(12)
    job: dict = {"user": user["id"], "status": "running", "at": datetime.now(timezone.utc)}
    _runs[rid] = job

    async def work() -> None:
        try:
            run = _run_openings if req.mode == "jobs" else _run_pipeline
            job["result"] = (await run(req, user)).model_dump(mode="json")
            job["status"] = "done"
        except HTTPException as exc:
            job.update(status="error", code=exc.status_code, detail=exc.detail)
        except Exception:
            log.exception("background run failed")
            job.update(status="error", code=500, detail="The run failed. Try again.")

    job["task"] = asyncio.create_task(work())
    return {"id": rid}


@app.get("/api/runs/{rid}")
async def get_run(rid: str, user: sqlite3.Row = Depends(auth.current_user)) -> dict:
    job = _runs.get(rid)
    if not job or job["user"] != user["id"]:
        raise HTTPException(404, "That run is gone, most likely after a restart. Run it again.")
    if job["status"] == "running":
        return {"status": "running"}
    _runs.pop(rid, None)
    if job["status"] == "error":
        raise HTTPException(job["code"], job["detail"])
    return {"status": "done", "result": job["result"]}


@app.post("/api/search.csv")
async def search_csv(
    req: SearchRequest, request: Request, user: sqlite3.Row = Depends(auth.current_user)
) -> StreamingResponse:
    enforce(_run_limit, request, "Too many runs in a short time. Try again shortly.")
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
         "priority", "alumni", "reason", "ask", "note"]
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
                "yes" if row.alumni else "",
                row.reason or "",
                row.ask or "",
                row.error or "",
            ]
        )
    return buffer.getvalue()


OPENING_CSV_HEADER = ["fit_score", "title", "company", "source", "url", "location", "posted_at",
                      "closes_at", "first_seen", "level_fit", "must_haves_met", "skills_matched",
                      "skills_missing", "why", "gap", "verified", "note"]


def opening_csv_row(row: ScoredOpening) -> list:
    return [
        "" if row.fit_score is None else row.fit_score,
        row.title, row.company, row.source, row.url, row.location,
        row.posted_at or "", row.closes_at or "", row.first_seen or "", row.level_fit or "",
        "" if row.must_haves_met is None else f"{row.must_haves_met:.3f}",
        "; ".join(row.skills_matched), "; ".join(row.skills_missing),
        row.why or "", row.gap or "", "yes" if row.verified else "no", row.error or "",
    ]


@app.post("/api/openings.csv")
async def openings_csv(
    req: SearchRequest, request: Request, user: sqlite3.Row = Depends(auth.current_user)
) -> StreamingResponse:
    enforce(_run_limit, request, "Too many runs in a short time. Try again shortly.")
    response = await _run_openings(req, user)
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(OPENING_CSV_HEADER)
    for row in response.results:
        writer.writerow(opening_csv_row(row))
    return StreamingResponse(
        io.BytesIO(buffer.getvalue().encode("utf-8")),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="openings.csv"'},
    )


# --- feedback ------------------------------------------------------------------------
@app.post("/api/feedback", response_model=FeedbackState)
async def feedback(req: FeedbackRequest, user: sqlite3.Row = Depends(auth.current_user)) -> FeedbackState:
    """A thumb, or "applied / messaged / got a reply", on one result.

    The features the result was ranked on are copied from the run that showed
    it, so the label can later be learned from even after the posting closes.
    """
    changes = {k: v for k, v in (("rating", req.rating), ("applied", req.applied),
                                 ("messaged", req.messaged), ("replied", req.replied)) if v is not None}
    if not changes:
        raise HTTPException(status_code=400, detail="Nothing to record.")
    features, fit_score, run_id = "{}", None, None
    if req.run_id is not None and db.run_owner(req.run_id) == user["id"]:
        run_id = req.run_id
        row = db.get_run_result(req.run_id, req.item_key)
        if row:
            features, fit_score = row["features"], row["fit_score"]
    saved = db.upsert_feedback(user["id"], req.kind, req.item_key, changes, run_id, fit_score,
                               features, req.url, req.title)
    return FeedbackState(rating=saved["rating"], applied=bool(saved["applied"]),
                         messaged=bool(saved["messaged"]), replied=bool(saved["replied"]))


# --- saved searches and alerts ----------------------------------------------------------
def _saved(row) -> SavedSearch:
    return SavedSearch(id=row["id"], name=row["name"], roles=json.loads(row["roles"] or "[]"),
                       companies=json.loads(row["companies"] or "[]"), min_score=row["min_score"],
                       created_at=row["created_at"], last_checked=row["last_checked"])


@app.get("/api/saved-searches", response_model=list[SavedSearch])
async def list_saved(user: sqlite3.Row = Depends(auth.current_user)) -> list[SavedSearch]:
    return [_saved(r) for r in db.saved_searches(user["id"])]


@app.post("/api/saved-searches", response_model=SavedSearch)
async def create_saved(req: SavedSearchRequest, request: Request,
                       user: sqlite3.Row = Depends(auth.current_user)) -> SavedSearch:
    enforce(_draft_limit, request, "Too many requests in a short time. Try again shortly.")
    plan = effective_plan(user)
    limit = saved_searches_allowed(plan)
    if len(db.saved_searches(user["id"])) >= limit:
        raise HTTPException(status_code=402, detail=(
            f"The {plan.title()} plan keeps {limit} saved search{'es' if limit != 1 else ''}."
            + ("" if plan == "pro" else " Upgrade to Pro for more.")))
    profile, _ = await _resolve_profile(req.profile, req.resume, user["id"])
    try:
        vector = embeddings.pack(await embeddings.embed_one(seeker.as_text(profile) + "\n\n"
                                                            + req.resume[:3000]))
    except OpenRouterError as exc:
        raise HTTPException(status_code=502, detail=f"Could not index this search: {exc}") from exc
    roles = _merge(req.roles, profile.target_roles, limit=8)
    name = req.name.strip() or (", ".join(roles[:2]) or "My search")
    search_id = db.create_saved_search(user["id"], name, profile.model_dump_json(), json.dumps(roles),
                                       json.dumps(req.companies), req.min_score, vector)
    # Make sure every named company's board is known, so tomorrow's poll reads it.
    if req.companies:
        await registry.resolve_many(req.companies[:25])
    return _saved(next(r for r in db.saved_searches(user["id"]) if r["id"] == search_id))


@app.delete("/api/saved-searches/{search_id}")
async def delete_saved(search_id: int, user: sqlite3.Row = Depends(auth.current_user)) -> dict:
    if not db.delete_saved_search(user["id"], search_id):
        raise HTTPException(status_code=404, detail="No such saved search.")
    return {"ok": True}


@app.get("/api/alerts", response_model=list[Alert])
async def alerts(user: sqlite3.Row = Depends(auth.current_user)) -> list[Alert]:
    return [Alert(id=r["id"], search_id=r["search_id"], search_name=r["search_name"],
                  posting_key=r["posting_key"], score=r["score"], title=r["title"],
                  company=r["company"], url=r["apply_url"] or r["url"], location=r["location"],
                  posted_at=r["posted_at"], first_seen=r["first_seen"], created_at=r["created_at"],
                  seen=bool(r["seen"]), closed=bool(r["closed_at"]))
            for r in db.alerts_for(user["id"])]


@app.post("/api/alerts/seen")
async def alerts_seen(user: sqlite3.Row = Depends(auth.current_user)) -> dict:
    return {"marked": db.mark_alerts_seen(user["id"])}


@app.get("/api/company-timing")
async def company_timing(company: str, level: str = "",
                         user: sqlite3.Row = Depends(auth.current_user)) -> dict:
    """When this company has been seen opening roles -- or that it is too early to say."""
    return timing.company_timing(company[:120], level[:20])


# --- operator ---------------------------------------------------------------------------
def _operator(user: sqlite3.Row = Depends(auth.current_user)) -> sqlite3.Row:
    if not is_comp_account(user["email"]):
        raise HTTPException(status_code=403, detail="Operator only.")
    return user


@app.get("/api/admin/stats")
async def admin_stats(days: int = 30, user: sqlite3.Row = Depends(_operator)) -> dict:
    """What a run costs, and how good the results are, over the last N days."""
    import evaluation
    since = (datetime.now(timezone.utc) - timedelta(days=max(1, min(days, 365)))).isoformat()
    costs = [dict(r) for r in db.cost_stats(since)]
    return {"since": since, "costs": costs, "quality": evaluation.feedback_report()}


_poll_task: asyncio.Task | None = None


@app.post("/api/admin/poll")
async def admin_poll(request: Request, limit: int | None = None, wait: bool = False) -> dict:
    """Run the daily poll. Authenticated by POLL_TOKEN (for an external cron),
    or by an operator session.

    Returns at once and polls in the background: external crons such as
    cron-job.org give up after ~30 s, and a poll over many boards takes longer.
    Pass ?wait=true to block and get the stats back.
    """
    global _poll_task
    token = request.headers.get("x-poll-token", "")
    if not (POLL_TOKEN and hmac.compare_digest(token, POLL_TOKEN)):
        user = auth._user_from_token(request.cookies.get(SESSION_COOKIE))
        if user is None or not is_comp_account(user["email"]):
            raise HTTPException(status_code=403, detail="Operator only.")
    if wait:
        return await poller.run(limit)
    if _poll_task is not None and not _poll_task.done():
        return {"started": False, "reason": "a poll is already running"}

    async def _run() -> None:
        try:
            log.info("poll finished: %s", await poller.run(limit))
        except Exception:
            log.exception("poll failed")

    _poll_task = asyncio.create_task(_run())
    return {"started": True}


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
