import registry


def test_board_from_url():
    f = registry.board_from_url
    assert f("https://job-boards.greenhouse.io/stripe/jobs/123") == ("greenhouse", "stripe")
    assert f("https://boards.greenhouse.io/duolingo/jobs/5") == ("greenhouse", "duolingo")
    assert f("https://jobs.lever.co/palantir/ac97-81") == ("lever", "palantir")
    assert f("https://jobs.ashbyhq.com/notion/1fc3") == ("ashby", "notion")
    assert f("https://apply.workable.com/huggingface/j/F4C0/") == ("workable", "huggingface")
    assert f("https://apply.workable.com/j/F4C0") is None
    assert f("https://jobs.smartrecruiters.com/Ubisoft2/7440001") == ("smartrecruiters", "Ubisoft2")
    assert f("https://bunq.recruitee.com/o/some-role") == ("recruitee", "bunq")
    assert f("https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite/job/X_JR1") == \
        ("workday", "nvidia|wd5|NVIDIAExternalCareerSite")
    assert f("https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite/job/X") == \
        ("workday", "nvidia|wd5|NVIDIAExternalCareerSite")
    assert f("https://careers.razorpay.com/jobs/1") is None


def test_slug_guesses():
    assert registry.slug_guesses("Hugging Face, Inc.") == ["huggingface", "hugging-face", "hugging"]
    assert registry.slug_guesses("Stripe") == ["stripe"]
    assert registry.slug_guesses("Razorpay Software Pvt Ltd")[0] == "razorpaysoftware"


def test_names_match():
    assert registry.names_match("Stripe", "Stripe, Inc.")
    assert registry.names_match("Hugging Face", "Hugging Face")
    assert not registry.names_match("Notion", "Meta")


def test_resolve_uses_cache(monkeypatch):
    import asyncio
    import db
    db.save_board("Stripe", "greenhouse", "stripe", "guess")
    async def boom(*a, **k):
        raise AssertionError("network used despite cache")
    monkeypatch.setattr(registry, "_guess", boom)
    assert asyncio.run(registry.resolve("stripe")) == ("greenhouse", "stripe")
    # A recent miss is cached too.
    db.save_board("Razorpay", None, None, "search")
    assert asyncio.run(registry.resolve("Razorpay")) is None
