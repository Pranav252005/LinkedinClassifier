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


def test_slug_matches_only_the_companys_own_board():
    m = registry.slug_matches
    assert m("Razorpay", "razorpaysoftwareprivatelimited")
    assert m("Zomato", "Zomato1")
    assert m("CrowdStrike", "crowdstrike|wd5|crowdstrikecareers")
    # Boards a search once returned because a snippet mentioned the company.
    assert not m("Flipkart", "philips|wd3|jobs-and-careers")
    assert not m("Microsoft", "galvestoncountytx|wd503|CountyofGalvestonCareers")
    assert not m("Amazon", "mellow-sleep")
    assert not m("Ola", "globaldimensionsllc")
    assert not m("Ola", "olasolar")                 # a short name is not a prefix match


def test_a_mislabelled_cached_board_is_looked_up_again(monkeypatch):
    import asyncio
    import db
    db.save_board("Flipkart", "workday", "philips|wd3|jobs-and-careers", "search")
    db.save_board("Razorpay", "greenhouse", "razorpaysoftwareprivatelimited", "search")

    async def no_guess(company):
        return None
    monkeypatch.setattr(registry, "_guess", no_guess)
    assert asyncio.run(registry.resolve("Razorpay", allow_search=False)) == \
        ("greenhouse", "razorpaysoftwareprivatelimited")
    assert asyncio.run(registry.resolve("Flipkart", allow_search=False)) is None


def test_in_house_careers_sites_resolve_without_a_lookup():
    import asyncio
    for name, ats in (("Amazon", "amazon"), ("AWS", "amazon"), ("Microsoft", "microsoft"), ("Google", "google")):
        assert asyncio.run(registry.resolve(name, allow_search=False)) == (ats, ats)
