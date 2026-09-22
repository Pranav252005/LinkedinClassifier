from datetime import date

import evaluation
import people
import seeker
import timing
from schemas import Profile, ScoredCandidate


def test_precision_at_k_ignores_unlabelled():
    assert evaluation.precision_at_k([1, None, 0, 1]) == 2 / 3
    assert evaluation.precision_at_k([None, None]) is None
    assert evaluation.precision_at_k([1] * 5 + [0] * 10) == 0.5          # only the top 10 count


def test_seeker_coerce_clamps_model_output():
    p = seeker.coerce({"seniority": "wizard", "years_experience": -3, "job_types": ["gig"],
                       "skills": ["Python", "python", "", 7, "SQL"], "grad_year": 3000,
                       "needs_sponsorship": "yes", "remote_ok": False, "college": "IIT Delhi"})
    assert p.seniority == "intern" and p.years_experience == 0 and p.job_types == ["internship"]
    assert p.skills == ["Python", "SQL"] and p.grad_year is None
    assert p.needs_sponsorship is False and p.remote_ok is False and p.college == "IIT Delhi"
    assert seeker.coerce("nonsense") == Profile()
    assert seeker.coerce({"years_experience": 4}).seniority == "mid"


def test_seeker_as_text_mentions_fields():
    text = seeker.as_text(Profile(skills=["Go"], locations=["Pune"], college="COEP", grad_year=2027))
    assert "Go" in text and "Pune" in text and "COEP" in text and "2027" in text


def test_timing_needs_history():
    rows = [{"title": "SWE Intern", "first_seen": "2026-09-01", "posted_at": None}] * 5
    assert timing.summarize(rows, "intern", today=date(2026, 9, 23))["enough"] is False
    history = [{"title": "SWE Intern", "first_seen": f"2026-{m:02d}-05", "posted_at": None}
               for m in (1, 1, 2, 8, 8, 8)]
    out = timing.summarize(history, "intern", today=date(2026, 9, 23))
    assert out["enough"] and "Aug" in out["note"] and "Jan" in out["note"]
    assert timing.summarize([], today=date(2026, 9, 23))["note"] is None


def test_people_alumni_and_location():
    c1 = ScoredCandidate(name="A", headline="SWE at Stripe", snippet="IIT Delhi alum · Bengaluru",
                         linkedin_url="https://www.linkedin.com/in/a", fit_score=70)
    c2 = ScoredCandidate(name="B", headline="Recruiter", snippet="Stanford",
                         linkedin_url="https://www.linkedin.com/in/b", fit_score=75)
    people.annotate([c1, c2], Profile(college="Indian Institute of Technology Delhi"), "Bengaluru, India")
    assert c1.alumni and c1.location_match and c1.fit_score == 81
    assert not c2.alumni and c2.fit_score == 75
    assert [c.name for c in people.rank([c2, c1])] == ["A", "B"]


def test_people_queries_start_from_the_opening():
    qs = people.build_queries("Stripe", "Backend Engineer Intern", "8611 Payments Infra", "Bengaluru", "intern",
                              "IIT Delhi")
    assert qs[0].query == 'site:linkedin.com/in "Stripe" "IIT Delhi"'
    assert any('"university recruiter"' in q.query for q in qs)
    assert any('"Payments Infra"' in q.query for q in qs)
    assert len(qs) <= 6
