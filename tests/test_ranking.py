import asyncio

import ranking
import weights
from schemas import Profile, ScoredOpening

PROFILE = Profile(seniority="intern", job_types=["internship"], skills=["Python", "FastAPI", "C++", "Kubernetes"])


def _o(key, sim, **kw):
    return ScoredOpening(key=key, title=kw.pop("title", "Software Engineer Intern"), url="https://x/" + key,
                         similarity=sim, verified=True, **kw)


def test_skill_overlap_word_boundaries():
    text = "We use Python 3, FastAPI and C++; no pythonic nonsense. Kubernetes nice-to-have."
    assert ranking.skill_overlap(["Python", "FastAPI", "C++", "Go", "Kubernetes"], text) == \
        ["Python", "FastAPI", "C++", "Kubernetes"]
    assert ranking.skill_overlap(["Go"], "Google") == []


def test_apply_review_coerces_and_rejects():
    o = _o("a", 0.5)
    assert not ranking.apply_review(o, {"must_haves_met": "yes", "level_fit": "perfect"})
    assert ranking.apply_review(o, {"must_haves_met": 1.7, "level_fit": "FIT",
                                    "skills_matched": ["Python", 3, ""], "skills_missing": ["Kubernetes"],
                                    "why": "  strong   match ", "gap": ""})
    assert o.reviewed and o.must_haves_met == 1.0 and o.level_fit == "fit"
    assert o.skills_matched == ["Python"] and o.skills_missing == ["Kubernetes"]
    assert o.why == "strong match" and o.gap is None


def test_finalize_prefers_reviewed_fits(monkeypatch):
    monkeypatch.setattr(weights, "current", lambda kind: None)
    good = _o("good", 0.7, description="Python FastAPI")
    ranking.apply_review(good, {"must_haves_met": 0.9, "level_fit": "fit", "skills_matched": ["Python"]})
    under = _o("under", 0.8, description="Python")
    ranking.apply_review(under, {"must_haves_met": 0.1, "level_fit": "under", "skills_missing": ["Go", "Rust"]})
    unreviewed = _o("plain", 0.4, description="C++")
    ranked = ranking.finalize([under, unreviewed, good], PROFILE)
    assert [o.key for o in ranked][0] == "good"
    assert ranked[0].features["reviewed"] == 1.0 and ranked[0].priority in ("high", "medium")
    plain = next(o for o in ranked if o.key == "plain")
    assert plain.skills_matched == ["C++"]                   # deterministic reasons without review
    assert all(0 <= o.fit_score <= 100 for o in ranked)


def test_rank_reviews_only_top_n(monkeypatch):
    monkeypatch.setattr(ranking, "REVIEW_TOP_N", 2)
    monkeypatch.setattr(weights, "current", lambda kind: None)
    seen = []

    async def fake_review(openings, profile_text, resume):
        seen.extend(o.key for o in openings)
        return []
    monkeypatch.setattr(ranking, "review", fake_review)
    items = [_o(k, None) for k in "abcd"]
    sims = {"a": 0.1, "b": 0.9, "c": 0.5, "d": 0.7}
    asyncio.run(ranking.rank(items, PROFILE, "p", "resume", sims))
    assert seen == ["b", "d"]


def test_recency():
    assert ranking.recency(None) == 0.5
