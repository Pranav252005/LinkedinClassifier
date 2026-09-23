"""End to end through the real app, with every outside service replaced.

Covers the pieces that only show up when they are wired together: the profile
round trip, board sourcing -> filters -> ranking -> response, feedback copying
the run's features, repeat runs skipping what was shown, people-for-opening,
alerts, and the operator stats.
"""

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

import boards
import db
import embeddings
import main
import ranking
import registry
from conftest import load
from schemas import Candidate

RESUME = "Final-year CS student. Python, SQL, fraud detection project. Interned at a fintech."
PROFILE = {"target_roles": ["Risk Analyst"], "seniority": "new_grad", "years_experience": 0,
           "job_types": ["full_time"], "skills": ["Python", "SQL"], "locations": [],
           "remote_ok": True, "needs_sponsorship": False, "college": "IIT Delhi", "grad_year": 2026,
           "summary": "New grad, risk + data"}


@pytest.fixture
def client(monkeypatch):
    async def resolve_many(companies, allow_search=True):
        return {"Stripe": ("greenhouse", "stripe")}, [c for c in companies if c != "Stripe"]
    monkeypatch.setattr(registry, "resolve_many", resolve_many)

    async def fetch_boards(board_list, search=None, where=None):
        return {("greenhouse", "stripe"): boards.parse_greenhouse("stripe", load("greenhouse"))}, {}
    monkeypatch.setattr(boards, "fetch_boards", fetch_boards)

    async def fake_embed(texts, model):
        # Similarity tracks how many times "risk"/"fraud" appear: deterministic and meaningful.
        return [[1.0, t.lower().count("risk") + t.lower().count("fraud") + 0.1] for t in texts]
    monkeypatch.setattr(embeddings, "embed", fake_embed)

    async def fake_chat(messages, **kw):
        n = messages[-1]["content"].count("\n---\n") + 1
        return json.dumps([{"i": i, "must_haves_met": 0.8 - 0.1 * i, "level_fit": "fit",
                            "skills_matched": ["Python"], "skills_missing": ["Kubernetes"],
                            "why": "Uses Python for risk", "gap": ""} for i in range(n)])
    monkeypatch.setattr(ranking, "chat", fake_chat)
    monkeypatch.setattr(ranking, "OPENROUTER_API_KEY", "test")
    monkeypatch.setattr(ranking.learned, "current", lambda kind: None)

    with TestClient(main.app) as c:
        yield c


def _signup(client, email="seeker@example.com"):
    r = client.post("/api/auth/signup", json={"email": email, "password": "correct horse battery"})
    assert r.status_code == 200, r.text


def _run(client, **kw):
    body = {"mode": "jobs", "resume": RESUME, "companies": ["Stripe"], "use_agent": False,
            "include_older": True, "profile": PROFILE, **kw}
    r = client.post("/api/openings", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def test_openings_end_to_end(client):
    _signup(client)
    data = _run(client)
    assert data["count"] >= 1 and data["run_id"]
    assert data["boards_read"] == ["Stripe (greenhouse)"]
    top = data["results"][0]
    assert top["verified"] and top["source"] == "greenhouse" and top["key"].startswith("greenhouse:stripe:")
    assert top["reviewed"] and top["level_fit"] == "fit" and top["skills_missing"] == ["Kubernetes"]
    assert set(top["features"]) >= {"sim", "must", "level", "skills", "recency"}
    assert "description" not in top                            # never shipped to the browser
    scores = [r["fit_score"] for r in data["results"]]
    assert scores == sorted(scores, reverse=True)

    # The profile edit was stored.
    assert client.get("/api/profile").json()["profile"]["college"] == "IIT Delhi"

    # Feedback copies the features the run ranked on.
    r = client.post("/api/feedback", json={"kind": "opening", "item_key": top["key"], "run_id": data["run_id"],
                                           "rating": 1, "url": top["url"], "title": top["title"]})
    assert r.json() == {"rating": 1, "applied": False, "messaged": False, "replied": False}
    r = client.post("/api/feedback", json={"kind": "opening", "item_key": top["key"], "applied": True})
    assert r.json()["applied"] and r.json()["rating"] == 1
    row = db.labeled_feedback("opening")[0]
    assert json.loads(row["features"]) == top["features"]

    # A repeat run finds nothing new, so it falls back to what was shown, marked as such.
    again = _run(client)
    assert any("already been shown" in w for w in again["warnings"])
    assert again["count"] >= 1 and all(r["seen_before"] for r in again["results"])
    assert any("seen before" in w for w in again["warnings"])
    both = _run(client, fresh_only=False)
    assert both["count"] == data["count"]
    assert both["results"][0]["feedback"]["applied"] is True

    # Cost was recorded for each run.
    assert db.cost_stats("2000-01-01")[0]["runs"] == 3


def test_hard_filters_are_reported(client):
    _signup(client)
    profile = {**PROFILE, "seniority": "intern", "job_types": ["internship"]}
    data = _run(client, profile=profile)
    # The Stripe fixture has no internships: every posting is filtered, and said so.
    assert data["count"] == 0
    assert data["filtered"].get("level_not_intern", 0) + data["filtered"].get("level_senior", 0) >= 1
    assert any("Hidden by your profile" in w for w in data["warnings"])


def test_people_for_opening(client, monkeypatch):
    _signup(client)
    async def run_queries(specs, per_query, max_c, exclude):
        assert specs[0].title == "IIT Delhi alumni"
        return [Candidate(name="Asha", headline="Risk Engineer at Stripe", snippet="IIT Delhi · Dublin",
                          linkedin_url="https://www.linkedin.com/in/asha", company_query="Stripe",
                          title_query="engineer")], specs, 0
    monkeypatch.setattr(main, "run_queries", run_queries)
    client.put("/api/profile", json=PROFILE)
    r = client.post("/api/people-for-opening", json={
        "opening_key": "greenhouse:stripe:1", "title": "Risk Analyst", "company": "Stripe",
        "department": "Risk", "location": "Dublin", "level": "entry", "resume": RESUME})
    assert r.status_code == 200, r.text
    person = r.json()["results"][0]
    assert person["alumni"] and person["location_match"] and person["for_opening"] == "Risk Analyst"


def test_alerts_and_saved_search_limits(client, monkeypatch):
    _signup(client)
    r = client.post("/api/saved-searches", json={"resume": RESUME, "profile": PROFILE, "min_score": 0})
    assert r.status_code == 200, r.text
    # Free plan keeps one.
    r2 = client.post("/api/saved-searches", json={"resume": RESUME, "profile": PROFILE})
    assert r2.status_code == 402

    # A poll finds a posting first seen after the search was saved.
    import poller
    postings = boards.parse_greenhouse("stripe", load("greenhouse"))
    for p in postings:
        p.title, p.posted_at = "Fraud Risk Analyst", None
    db.upsert_postings(postings, "2999-01-01T00:00:00+00:00")
    stats = asyncio.run(poller.match_saved_searches())
    assert stats["alerts"] >= 1
    alerts = client.get("/api/alerts").json()
    assert alerts and alerts[0]["title"] == "Fraud Risk Analyst" and alerts[0]["seen"] is False
    assert client.post("/api/alerts/seen").json()["marked"] == len(alerts)


def test_operator_endpoints(client):
    _signup(client)
    assert client.get("/api/admin/stats").status_code == 403
    client.post("/api/auth/logout")
    _signup(client, "operator@example.com")
    r = client.get("/api/admin/stats")
    assert r.status_code == 200 and "quality" in r.json()
    assert client.get("/api/company-timing", params={"company": "Stripe"}).json()["enough"] is False


def test_poll_endpoint_token(client, monkeypatch):
    import main as app_main
    calls = []

    async def fake_run(limit=None):
        calls.append(limit)
        return {"poll": {}, "alerts": {}}
    monkeypatch.setattr(app_main.poller, "run", fake_run)
    monkeypatch.setattr(app_main, "POLL_TOKEN", "s3cret")
    assert client.post("/api/admin/poll", headers={"X-Poll-Token": "wrong"}).status_code == 403
    r = client.post("/api/admin/poll", headers={"X-Poll-Token": "s3cret"})
    assert r.json() == {"started": True}
    r = client.post("/api/admin/poll?wait=true", headers={"X-Poll-Token": "s3cret"})
    assert r.json() == {"poll": {}, "alerts": {}}


def test_background_run(client):
    import time
    _signup(client)
    body = {"mode": "jobs", "resume": RESUME, "companies": ["Stripe"], "use_agent": False,
            "include_older": True, "profile": PROFILE}
    rid = client.post("/api/runs", json=body).json()["id"]
    for _ in range(100):
        job = client.get(f"/api/runs/{rid}").json()
        if job["status"] == "done":
            break
        time.sleep(0.05)
    assert job["status"] == "done" and job["result"]["count"] >= 1
    # Collected once, then gone.
    assert client.get(f"/api/runs/{rid}").status_code == 404
