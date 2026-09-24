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


def _resp(url, status=200, body="", final=None):
    import httpx
    return httpx.Response(status, text=body, request=httpx.Request("GET", final or url))


def test_page_closed():
    url = "https://jobs.example.com/acme/jobs/123"
    assert verify.page_closed(url, _resp(url, 404))
    assert not verify.page_closed(url, _resp(url, 403))            # a bot wall is not evidence
    assert verify.page_closed(url, _resp(url, final="https://jobs.example.com/acme"))
    assert verify.page_closed(url, _resp(url, body="Sorry, this job has expired."))
    assert verify.page_closed(url, _resp(url, body='{"@type":"JobPosting","validThrough":"2020-01-31"}'))
    assert not verify.page_closed(url, _resp(url, body='{"@type":"JobPosting","validThrough":"2099-01-31"}'))
    assert not verify.page_closed(url, _resp(url, body="Apply now: Software Engineer Intern"))


def test_board_record_says_closed():
    import httpx
    closed = httpx.Response(200, json={"jobPostingInfo": {"canApply": False}})
    assert verify._record_closed("workday", closed)
    assert verify._record_closed("smartrecruiters", httpx.Response(200, json={"active": False}))
    assert not verify._record_closed("smartrecruiters", httpx.Response(200, json={"active": True}))
    assert verify._record_closed("microsoft", httpx.Response(200, json={"data": {}}))


def test_malformed_json_is_retried_once(monkeypatch):
    import asyncio
    import openrouter
    answers = iter(["Sure! Here you go: [{broken", '{"results": [{"i": 0}]}'])

    async def fake_chat(messages, **kw):
        return next(answers)
    monkeypatch.setattr(openrouter, "chat", fake_chat)
    assert asyncio.run(openrouter.chat_json_list([])) == [{"i": 0}]
