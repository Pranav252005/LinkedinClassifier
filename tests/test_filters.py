"""Seniority, years, location and sponsorship extraction, and the hard filter."""

from datetime import date

import pytest

import filters
from schemas import Opening, Profile

TODAY = date(2026, 9, 23)


@pytest.mark.parametrize("title,level", [
    ("Software Engineer Intern", "intern"),
    ("Summer 2027 SWE Internship", "intern"),
    ("Lead Generation Intern", "intern"),
    ("Senior Software Engineer", "senior"),
    ("Sr. Backend Engineer", "senior"),
    ("Staff Engineer, Payments", "senior"),
    ("Engineering Manager", "senior"),
    ("SDE II", "mid"),
    ("SDE-1", "entry"),
    ("New Grad Software Engineer 2027", "entry"),
    ("Associate Product Manager", "entry"),
    ("Graduate Engineer Trainee", "entry"),
    ("Software Engineer", ""),
])
def test_title_level(title, level):
    assert filters.title_level(title) == level


@pytest.mark.parametrize("text,years", [
    ("You have 5+ years of experience building distributed systems.", 5),
    ("Requirements: 3-5 years of professional software engineering.", 3),
    ("At least two years of experience with Python.", 2),
    ("1+ year hands-on experience; 7+ years overall experience preferred.", 1),
    ("Founded 10 years ago, the company has grown fast.", None),
    ("Bachelor's degree (4 years) in Computer Science.", None),
    ("", None),
])
def test_required_years(text, years):
    assert filters.required_years(text) == years


def test_location():
    assert filters.location_matches(["Bangalore"], "Bengaluru, Karnataka, India", None, True)
    assert filters.location_matches(["India"], "Pune, Maharashtra", None, True)
    assert filters.location_matches(["India"], "Remote", True, True)
    assert not filters.location_matches(["India"], "Remote - US", True, True)
    assert not filters.location_matches(["India"], "Remote", True, False) is True
    assert filters.location_matches(["Pune"], "London, UK", False, True) is False
    assert filters.location_matches(["Pune"], "", None, True) is None
    assert filters.location_matches(["Pune"], "", True, True)             # remote, no place named
    # A board-level remote flag on a posting that names US cities is not remote for Pune.
    assert filters.location_matches(["India"], "San Francisco, California", True, True) is False
    assert filters.location_matches([], "Anywhere", None, True)


def test_sponsorship():
    assert filters.refuses_sponsorship("We are unable to sponsor visas for this role.")
    assert filters.refuses_sponsorship("Candidates must be authorized to work without sponsorship.")
    assert not filters.refuses_sponsorship("We sponsor visas for exceptional candidates.")


def _o(title, desc="", location="", posted=None, remote=None):
    return Opening(title=title, url="https://x", description=desc, location=location,
                   posted_at=posted, remote=remote)


STUDENT = Profile(seniority="intern", job_types=["internship"], locations=["India"])
NEW_GRAD = Profile(seniority="new_grad", job_types=["full_time"])
MID = Profile(seniority="mid", years_experience=4, job_types=["full_time"])


def test_judge_student():
    j = lambda o, **kw: filters.judge(o, STUDENT, today=TODAY, **kw)
    assert j(_o("Software Engineer Intern", location="Bengaluru")).keep
    assert j(_o("Senior Software Engineer", location="Bengaluru")).reason == "level_not_intern"
    assert j(_o("Software Engineer", "Requires 3+ years of experience", "Pune")).reason == "level_not_intern"
    assert j(_o("Software Engineer", location="Pune")).reason == "level_not_intern"
    assert j(_o("Software Engineer Intern", location="Seattle, WA")).reason == "location"


def test_judge_new_grad_and_mid():
    assert filters.judge(_o("Staff Engineer"), NEW_GRAD, today=TODAY).reason == "level_senior"
    assert filters.judge(_o("SDE II"), NEW_GRAD, today=TODAY).reason == "level_senior"
    assert filters.judge(_o("Software Engineer", "5+ years of experience"), NEW_GRAD, today=TODAY).reason == "years"
    assert filters.judge(_o("Software Engineer", "0-1 years of experience"), NEW_GRAD, today=TODAY).keep
    assert filters.judge(_o("Software Engineer Intern"), NEW_GRAD, today=TODAY).reason == "level_intern"
    assert filters.judge(_o("Senior Software Engineer", "5+ years of experience"), MID, today=TODAY).keep
    assert filters.judge(_o("Software Engineer", "10+ years of experience"), MID, today=TODAY).reason == "years"


def test_judge_age_and_sponsorship():
    old = _o("Software Engineer Intern", posted="2026-07-01", location="India")
    assert filters.judge(old, STUDENT, today=TODAY).reason == "old"
    assert filters.judge(old, STUDENT, include_older=True, today=TODAY).keep
    # Still listed on the board: open, so only a much older posting is dropped.
    old.verified = True
    assert filters.judge(old, STUDENT, today=TODAY).keep
    ancient = _o("Software Engineer Intern", posted="2025-01-01", location="India")
    ancient.verified = True
    assert filters.judge(ancient, STUDENT, today=TODAY).reason == "old"
    sponsor = Profile(seniority="new_grad", needs_sponsorship=True)
    no_visa = _o("Software Engineer", "We will not sponsor visas.")
    assert filters.judge(no_visa, sponsor, today=TODAY).reason == "sponsorship"
    assert filters.judge(no_visa, NEW_GRAD, today=TODAY).keep


def test_apply_counts_reasons():
    postings = [_o("Senior Engineer"), _o("Staff Engineer"), _o("New Grad SWE")]
    kept, dropped = filters.apply(postings, NEW_GRAD, today=TODAY)
    assert [p.title for p in kept] == ["New Grad SWE"]
    assert dropped == {"level_senior": 2}
    assert "2 too senior" in filters.describe(dropped)


@pytest.mark.parametrize("text,pay", [
    ("This is an unpaid internship for students.", "unpaid"),
    ("Volunteer position, no compensation.", "unpaid"),
    ("Equity-only role for an early founding engineer.", "unpaid"),
    ("Stipend of ₹25,000 per month.", "paid"),
    ("Pay range: $120,000 - $150,000", "paid"),
    ("This is a paid internship.", "paid"),
    ("Benefits include unpaid leave and paid time off.", ""),
    ("Build services in Go.", ""),
])
def test_pay_status(text, pay):
    assert filters.pay_status(text) == pay


def test_paid_only_drops_unpaid_keeps_unknown():
    paid_only = NEW_GRAD.model_copy(update={"paid_only": True})
    assert filters.judge(_o("Software Engineer", desc="This role is unpaid."), paid_only,
                         today=TODAY).reason == "unpaid"
    assert filters.judge(_o("Software Engineer", desc="Build things."), paid_only, today=TODAY).keep
    assert filters.judge(_o("Software Engineer", desc="This role is unpaid."), NEW_GRAD, today=TODAY).keep


@pytest.mark.parametrize("location,text,remote,mode", [
    ("Remote - India", "", None, "remote"),
    ("Bengaluru (Hybrid)", "", None, "hybrid"),
    ("Bengaluru", "You will work 3 days a week in the office.", None, "hybrid"),
    ("Bengaluru", "This is an on-site role.", None, "onsite"),
    ("", "This is a fully remote position.", None, "remote"),
    ("Bengaluru", "Collaborate with remote teams.", None, ""),
    ("", "", True, "remote"),
])
def test_work_mode(location, text, remote, mode):
    assert filters.work_mode(location, text, remote) == mode


def test_work_mode_filter():
    remote = NEW_GRAD.model_copy(update={"work_mode": "remote"})
    onsite = NEW_GRAD.model_copy(update={"work_mode": "onsite", "locations": ["Bangalore"]})
    j = lambda o, p: filters.judge(o, p, today=TODAY)
    assert j(_o("Software Engineer", location="Remote"), remote).keep
    assert j(_o("Software Engineer", location="Pune", desc="This is an on-site role."), remote).reason == "not_remote"
    assert j(_o("Software Engineer", location="Bengaluru"), onsite).keep
    assert j(_o("Software Engineer", location="Remote - India"), onsite).reason == "location"
    assert j(_o("Software Engineer", location="Mumbai"), onsite).reason == "location"
    # Not stated either way: kept for the verify stage to read.
    assert j(_o("Software Engineer"), remote).keep
