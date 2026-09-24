"""An internship-only seeker must be searched for internships, not have them
filtered out of a pool of full-time roles."""

import asyncio
import json

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


def test_parse_amazon():
    payload = {"hits": 1, "jobs": [{
        "id_icims": "10552765", "title": "Software Dev Engineer Intern", "job_path": "/en/jobs/10552765/sde-intern",
        "normalized_location": "Bengaluru, Karnataka, IND", "location": "IN, KA, Bengaluru",
        "posted_date": "September 17, 2026", "description": "Build things.<br/>Ship them.",
        "basic_qualifications": "- Enrolled in a CS degree", "job_category": "Software Development",
        "url_next_step": "https://account.amazon.jobs/jobs/10552765/apply"}]}
    [p] = boards.parse_amazon(payload)
    assert p.key == "amazon:amazon:10552765" and p.company == "Amazon" and p.posted_at == "2026-09-17"
    assert p.url == "https://www.amazon.jobs/en/jobs/10552765/sde-intern" and "Enrolled in a CS degree" in p.description


def test_parse_microsoft():
    payload = {"data": {"positions": [{
        "id": 1970393556917520, "name": "Data Science INTERN", "locations": ["India, Multiple Locations"],
        "postedTs": 1790228220, "department": "Data Science", "workLocationOption": "onsite"}]}}
    [p] = boards.parse_microsoft(payload)
    assert p.key == "microsoft:microsoft:1970393556917520" and p.remote is False and p.posted_at == "2026-09-24"
    assert p.detail_ref.endswith("position_id=1970393556917520") and "microsoft" in boards.DETAIL_ATS


def test_parse_google():
    job = ["1099", "Software Engineering Intern", "https://sign.in", [None, "<ul><li>Build</li></ul>"],
           [None, "<p>Pursuing a BS</p>"], "projects/x", None, "Google", "en-US",
           [["Bengaluru, Karnataka, India", [], "Bengaluru", "560038", "KA", "IN"]],
           [None, "<p>A paid internship.</p>"], [4], [1788764944, 0]]
    page = ("<script>AF_initDataCallback({key: 'ds:1', hash: '2', data:" + json.dumps([[job], None, 1, 20])
            + ", sideChannel: {}});</script>")
    [p], total = boards.parse_google(page)
    assert total == 1 and p.key == "google:google:1099" and p.location == "Bengaluru, Karnataka, India"
    assert "paid internship" in p.description and "Pursuing a BS" in p.description
    assert boards.parse_google("<html>changed</html>") == ([], 0)


def test_country_of():
    assert boards.country_of(["bengaluru", "bangalore", "india"]) == "IND"
    assert boards.country_of(["bengaluru", "london"]) is None
    assert boards.country_of([]) is None


def test_recruitee_sample_offers_are_not_jobs():
    payload = {"offers": [{"id": 1, "title": "Senior Marketer (Sample)", "status": "published"}]}
    assert boards.parse_recruitee("google", payload) == []


def test_internship_seekers_search_boards_for_the_plain_word():
    assert openings.board_searches(["Data Scientist intern"], INTERN) == ["intern", "Data Scientist intern"]
    assert openings.board_searches(["Data Scientist"], BOTH) == ["Data Scientist"]


def test_linkedin_results_carry_employer_and_country():
    import jobs
    o = jobs._parse({"link": "https://in.linkedin.com/jobs/view/intern-backend-at-uplabh-4439046038",
                     "title": "Intern - Backend | LinkedIn"}, "linkedin", "q")
    assert (o.title, o.company, o.location) == ("Intern - Backend", "Uplabh", "India")
    o = jobs._parse({"link": "https://in.linkedin.com/jobs/view/x-4400000000",
                     "title": "Adeptmind hiring Data Science Intern in Bengaluru, Karnataka, India | LinkedIn"},
                    "linkedin", "q")
    assert (o.title, o.company, o.location) == ("Data Science Intern", "Adeptmind", "Bengaluru, Karnataka, India")
    # A site-restricted search that returned another site anyway.
    assert jobs._parse({"link": "https://www.reddit.com/r/x/comments/1", "title": "My intern interview"},
                       "linkedin", "q") is None


def test_job_site_results():
    import jobs
    o = jobs._parse({"link": "https://www.naukri.com/job-listings-sde-intern-entnt-0-to-1-years-120825",
                     "title": "Software Engineer Intern - - Entnt - 0 to 1 years of experience",
                     "snippet": "Job Description for Software Engineer Intern in Entnt in Bengaluru for 0 to 1 years",
                     "date": "2026年8月12日"}, "naukri", "q")
    assert (o.title, o.company, o.location, o.posted_at) == ("Software Engineer Intern", "Entnt", "Bengaluru",
                                                             "2026-08-12")
    o = jobs._parse({"link": "https://www.glassdoor.co.in/job-listing/x-JV_1.htm",
                     "title": "Rubrik hiring Software Engineer - Winter Intern Job in Bengaluru"}, "glassdoor", "q")
    assert (o.title, o.company, o.location) == ("Software Engineer - Winter Intern", "Rubrik", "Bengaluru")
    o = jobs._parse({"link": "https://in.indeed.com/viewjob?jk=1",
                     "title": "Software Engineer Intern - Bengaluru, Karnataka - Indeed.com"}, "indeed", "q")
    assert (o.title, o.location) == ("Software Engineer Intern", "Bengaluru, Karnataka")
    assert jobs._parse({"link": "https://www.indeed.com.evil.io/viewjob", "title": "X"}, "indeed", "q") is None


def test_regional_editions():
    import jobs
    [(_, q)] = jobs.build_queries(["data intern"], [], sources=("indeed",), where="Bengaluru", country="IND")
    assert q.startswith("site:in.indeed.com/viewjob")


def test_search_dates():
    import jobs
    from datetime import datetime, timezone
    now = datetime(2026, 9, 24, tzinfo=timezone.utc)
    assert jobs.search_date("2 days ago", now) == "2026-09-22"
    assert jobs.search_date("Sep 16, 2026") == "2026-09-16"
    assert jobs.search_date("3 weeks ago", now) == "2026-09-03"
    assert jobs.search_date("whenever") is None


def test_gigs_from_upwork(monkeypatch):
    import gigs

    async def fake_find(roles, companies, per, limit, exclude, max_queries, sources):
        from schemas import Opening
        assert sources == ("upwork",) and "intern" not in " ".join(roles).lower()
        return [Opening(title="Build a RAG API with FastAPI", source="upwork",
                        url="https://www.upwork.com/freelance-jobs/apply/rag_~01",
                        snippet="Hourly: $15 - $30. Need FastAPI and vector search.", posted_at="2026-09-20"),
                Opening(title="Old scraper", source="upwork", url="https://www.upwork.com/freelance-jobs/apply/old_~02",
                        snippet="Fixed-price", posted_at="2026-01-01")], ["q"], 0
    monkeypatch.setattr(gigs, "find_openings", fake_find)
    seeker = Profile(seniority="intern", job_types=["internship"], skills=["FastAPI", "Python"],
                     target_roles=["Backend Developer Intern"])
    found, _q, _skipped, stale = asyncio.run(gigs.find(seeker, [], frozenset(), 10))
    assert stale == 1 and [g.title for g in found] == ["Build a RAG API with FastAPI"]
    assert found[0].unconfirmed and found[0].pay == "paid" and found[0].department == "Hourly: $15 - $30"
