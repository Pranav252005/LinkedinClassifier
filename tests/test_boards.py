"""ATS response parsers, against real (trimmed) responses saved from each API.

These break silently when a vendor changes its format, which is exactly why
they are tested: a parser returning [] looks like "no openings", not an error.
"""

from datetime import date

import boards
from conftest import load

TODAY = date(2026, 9, 23)


def _check(postings, ats, n):
    assert len(postings) == n
    keys = {p.key for p in postings}
    assert len(keys) == n, "keys must be unique"
    for p in postings:
        assert p.ats == ats
        assert p.title and p.job_id and p.url.startswith("https://")
        assert p.key == f"{ats}:{p.slug}:{p.job_id}"


def test_greenhouse():
    ps = boards.parse_greenhouse("stripe", load("greenhouse"))
    _check(ps, "greenhouse", 3)
    p = ps[0]
    assert p.url == f"https://job-boards.greenhouse.io/stripe/jobs/{p.job_id}"
    assert p.company == "Stripe"
    assert p.posted_at and len(p.posted_at) == 10
    # Greenhouse content is escaped HTML; none of it may survive as markup.
    assert p.description and "<" not in p.description and "&lt;" not in p.description


def test_lever():
    ps = boards.parse_lever("palantir", load("lever"))
    _check(ps, "lever", 3)
    assert all(p.posted_at for p in ps)
    assert all(p.description for p in ps)
    assert ps[0].url.startswith("https://jobs.lever.co/palantir/")


def test_ashby():
    ps = boards.parse_ashby("notion", load("ashby"))
    _check(ps, "ashby", 3)
    assert ps[0].title == "Software Engineer, Developer Platform"
    assert ps[0].remote is True
    assert ps[0].department == "Engineering"          # "Engineering / Engineering" collapsed
    assert "New York" in ps[0].location               # secondary locations kept


def test_workable():
    ps = boards.parse_workable("huggingface", load("workable"))
    _check(ps, "workable", 3)
    assert ps[0].company == "Hugging Face"
    assert ps[0].posted_at == "2026-07-30"


def test_recruitee():
    ps = boards.parse_recruitee("bunq", load("recruitee"))
    _check(ps, "recruitee", 3)
    assert ps[0].posted_at == "2026-09-22"


def test_smartrecruiters_list_and_detail():
    ps = boards.parse_smartrecruiters("Ubisoft2", load("smartrecruiters"))
    _check(ps, "smartrecruiters", 3)
    assert not ps[0].description and ps[0].detail_ref.startswith("https://api.smartrecruiters.com/")
    text = boards.smartrecruiters_description(load("smartrecruiters_detail"))
    assert "Ubisoft" in text and "<" not in text


def test_workday_list_and_detail():
    slug = "nvidia|wd5|NVIDIAExternalCareerSite"
    ps = boards.parse_workday(slug, load("workday"), today=TODAY)
    assert ps and all(p.ats == "workday" for p in ps)
    p = ps[0]
    assert p.url.startswith("https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite/job/")
    assert p.detail_ref.startswith("https://nvidia.wd5.myworkdayjobs.com/wday/cxs/nvidia/NVIDIAExternalCareerSite/job/")
    assert p.posted_approx
    boards.apply_workday_detail(p, load("workday_detail"))
    assert p.description and not p.posted_approx
    assert p.posted_at == load("workday_detail")["jobPostingInfo"]["startDate"]


def test_parsers_tolerate_garbage():
    for fn in (boards.parse_greenhouse, boards.parse_ashby, boards.parse_workable,
               boards.parse_recruitee, boards.parse_smartrecruiters):
        assert fn("x", {}) == []
        assert fn("x", {"jobs": [{"id": None}], "offers": [{}], "content": [{}]}) == []
    assert boards.parse_lever("x", {"not": "a list"}) == []


def test_html_to_text():
    assert boards.html_to_text("&lt;p&gt;Hello &amp;amp; welcome&lt;/p&gt;&lt;ul&gt;&lt;li&gt;One&lt;/li&gt;&lt;/ul&gt;") \
        == "Hello & welcome\n\nOne"
    assert boards.html_to_text(None) == ""


def test_dates():
    assert boards.iso_day("2026-09-09T10:50:29-04:00") == "2026-09-09"
    assert boards.iso_day(1711403416463) == "2024-03-25"         # Lever epoch ms
    assert boards.iso_day("next tuesday") is None
    assert boards.relative_day("Posted Today", TODAY) == ("2026-09-23", False)
    assert boards.relative_day("Posted Yesterday", TODAY) == ("2026-09-22", False)
    assert boards.relative_day("Posted 6 Days Ago", TODAY) == ("2026-09-17", False)
    # "30+" is a lower bound, dated past the bound so a 30-day cut-off removes it.
    assert boards.relative_day("Posted 30+ Days Ago", TODAY) == ("2026-08-23", True)
    assert boards.relative_day("", TODAY) == (None, False)


def test_workday_slug_validation():
    import pytest
    with pytest.raises(boards.BoardError):
        boards.workday_parts("nvidia")
