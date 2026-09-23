"""An internship-only seeker must be searched for internships, not have them
filtered out of a pool of full-time roles."""

import asyncio

import boards
import db
import openings
import registry
from schemas import Profile, ScoredOpening

INTERN = Profile(seniority="intern", job_types=["internship"], locations=["Bangalore"])
BOTH = Profile(seniority="intern", job_types=["internship", "full_time"])


def test_search_roles_add_the_level():
    assert openings.search_roles(["software engineer", "SWE Intern"], INTERN) == \
        ["software engineer intern", "SWE Intern"]
    assert openings.search_roles(["software engineer"], BOTH) == ["software engineer"]


def _p(job_id, title):
    return boards.Posting(ats="greenhouse", slug="acme", job_id=job_id, title=title, company="Acme",
                          url=f"https://x/{job_id}", description="Build things.")


def test_index_requires_the_level():
    # Newer full-time roles must not crowd the internship out of the limit.
    db.upsert_postings([_p("1", "Software Engineer Intern")], "2026-09-01T00:00:00+00:00")
    db.upsert_postings([_p(str(i), "Software Engineer") for i in range(2, 12)], "2026-09-02T00:00:00+00:00")
    assert len(db.search_open_postings(["software engineer"], limit=5)) == 5
    hits = db.search_open_postings(["software engineer"], limit=5, require=openings.INTERN_WORDS)
    assert [h["title"] for h in hits] == ["Software Engineer Intern"]


def test_parse_ibm():
    payload = {"hits": {"hits": [{"_id": "h", "_source": {
        "url": "https://careers.ibm.com/careers/JobDetail?jobId=131545", "title": "Software Engineering Intern",
        "description": "Join our team.", "dcdate": "2026-09-16", "field_keyword_17": "Hybrid",
        "field_keyword_19": "Bangalore, IN", "field_keyword_08": "Software Engineering"}}]}}
    [p] = boards.parse_ibm(payload)
    assert p.key == "ibm:ibm:131545" and p.company == "IBM" and p.posted_at == "2026-09-16"
    assert p.location == "Bangalore, IN, Hybrid" and p.remote is None


def test_ibm_resolves_without_a_lookup():
    assert asyncio.run(registry.resolve("IBM", allow_search=False)) == ("ibm", "ibm")


def test_widens_to_seen_before(monkeypatch):
    fresh = [ScoredOpening(title="SWE Intern", url=f"https://x/{i}", key=f"k{i}", location="Bengaluru",
                           verified=True) for i in range(3)]

    async def boards_read(company_boards, roles, where=None):
        return fresh, [], []

    async def none(*a, **kw):
        return {}

    async def no_web(*a, **kw):
        return [], [], []

    monkeypatch.setattr(openings, "_read_boards", boards_read)
    monkeypatch.setattr(openings, "_discover_boards", none)
    monkeypatch.setattr(openings, "_web_fallback", no_web)
    monkeypatch.setattr(openings.registry, "resolve_many", lambda c: _pair())

    got = asyncio.run(openings.gather(INTERN, ["software engineer"], [], frozenset({"k0", "k1", "k2"}), 30, False, 10))
    assert len(got["openings"]) == 3 and all(o.seen_before for o in got["openings"])
    assert any("seen before" in w for w in got["warnings"])


async def _pair():
    return {}, []


def test_board_places():
    assert set(openings.board_places(INTERN)) == {"bangalore", "bengaluru"}
    assert openings.board_places(INTERN.model_copy(update={"work_mode": "remote"})) == []
