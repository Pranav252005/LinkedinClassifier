"""The verify stage: page text, applying the model's answers, and the drop rules."""

import verify
from schemas import Profile, ScoredOpening


def _o(**kw):
    return ScoredOpening(title="SWE", url="https://x", **kw)


def test_page_text_strips_markup():
    assert verify.page_text("<style>a{}</style><p>Hybrid &amp; paid</p><script>x()</script>") == "Hybrid & paid"


def test_apply_and_rules():
    onsite = Profile(work_mode="onsite", locations=["Bangalore"], paid_only=True)
    o = _o()
    verify._apply(o, {"work_mode": "hybrid", "city": "Bengaluru", "in_candidate_location": True, "pay": "paid"})
    assert o.checked and o.location == "Bengaluru" and verify.rules_out(o, onsite) is None

    elsewhere = _o()
    verify._apply(elsewhere, {"work_mode": "onsite", "in_candidate_location": False, "pay": "unknown"})
    assert verify.rules_out(elsewhere, onsite) == "location"

    unpaid = _o()
    verify._apply(unpaid, {"work_mode": "onsite", "in_candidate_location": True, "pay": "unpaid"})
    assert verify.rules_out(unpaid, onsite) == "unpaid"

    remote = _o()
    verify._apply(remote, {"work_mode": "remote", "in_candidate_location": None, "pay": "paid"})
    assert verify.rules_out(remote, onsite) == "remote_only"
    assert verify.rules_out(remote, Profile(work_mode="remote")) is None


def test_unknown_is_kept():
    o = _o()
    verify._apply(o, {"work_mode": "unknown", "in_candidate_location": None, "pay": "unknown"})
    assert verify.rules_out(o, Profile(work_mode="remote", paid_only=True, locations=["Bangalore"])) is None
