"""Dedupe, liveness and feedback bookkeeping in the database."""

import json

import boards
import db
from conftest import load


def _board():
    return boards.parse_greenhouse("stripe", load("greenhouse"))


def test_upsert_is_idempotent_and_keeps_first_seen():
    ps = _board()
    first = db.upsert_postings(ps, "2026-09-01T00:00:00+00:00")
    again = db.upsert_postings(ps, "2026-09-02T00:00:00+00:00")
    assert set(first) == {p.key for p in ps}
    assert first == again                                   # first_seen never moves
    rows = db.open_postings_for([("greenhouse", "stripe")])
    assert len(rows) == len(ps)                             # one row per posting, not two
    assert all(r["last_seen"].startswith("2026-09-02") for r in rows)


def test_missing_postings_close_and_reopen():
    ps = _board()
    db.upsert_postings(ps, "2026-09-01T00:00:00+00:00")
    # The next poll lists only the first posting.
    db.upsert_postings(ps[:1], "2026-09-02T00:00:00+00:00")
    closed = db.close_missing("greenhouse", "stripe", "2026-09-02T00:00:00+00:00")
    assert closed == len(ps) - 1
    assert [r["key"] for r in db.open_postings_for([("greenhouse", "stripe")])] == [ps[0].key]
    # Relisted later: open again.
    db.upsert_postings(ps, "2026-09-03T00:00:00+00:00")
    assert len(db.open_postings_for([("greenhouse", "stripe")])) == len(ps)


def test_blank_description_does_not_erase_stored_one():
    ps = _board()
    db.upsert_postings(ps)
    bare = boards.Posting(ats=ps[0].ats, slug=ps[0].slug, job_id=ps[0].job_id, title=ps[0].title)
    db.upsert_postings([bare])
    assert db.get_posting(ps[0].key)["description"] == ps[0].description


def test_shared_index_search():
    db.upsert_postings(_board())
    title = _board()[0].title.split()[0]
    hits = db.search_open_postings([title])
    assert hits and all(title.lower() in h["title"].lower() for h in hits)
    assert db.search_open_postings([title], [("greenhouse", "stripe")]) == []


def test_embeddings_round_trip():
    import embeddings
    ps = _board()
    db.upsert_postings(ps)
    vec = [0.5, -0.25, 1.0]
    db.set_embeddings([(ps[0].key, embeddings.pack(vec), "h1")])
    stored = db.get_embeddings([p.key for p in ps])
    assert list(stored) == [ps[0].key]
    assert embeddings.unpack(stored[ps[0].key][0]) == vec


def _user(email="a@example.com"):
    return db.create_user(email, "hash")


def test_feedback_merges_changes():
    uid = _user()
    run_id = db.record_run(uid, "Stripe", 1)
    db.save_run_results(run_id, "opening", [("k1", 0, 80, json.dumps({"sim": 0.9}), "https://x", "T")])
    db.upsert_feedback(uid, "opening", "k1", {"rating": 1}, run_id, 80, '{"sim": 0.9}', "https://x", "T")
    row = db.upsert_feedback(uid, "opening", "k1", {"applied": True}, None, None, "{}", "", "")
    assert row["rating"] == 1 and row["applied"] == 1
    assert row["features"] == '{"sim": 0.9}'               # first write's snapshot kept
    assert len(db.labeled_feedback("opening")) == 1
    assert db.run_owner(run_id) == uid


def test_saved_search_alerts_are_unique():
    uid = _user()
    db.upsert_postings(_board())
    sid = db.create_saved_search(uid, "SWE", "{}", "[]", "[]", 60, None)
    key = _board()[0].key
    db.add_alerts([(uid, sid, key, 90)])
    db.add_alerts([(uid, sid, key, 95)])                    # the same posting again
    alerts = db.alerts_for(uid)
    assert len(alerts) == 1 and alerts[0]["score"] == 90 and alerts[0]["seen"] == 0
    assert db.mark_alerts_seen(uid) == 1
    assert db.delete_saved_search(uid, sid) == 1
    assert db.alerts_for(uid) == []


def test_costs_recorded():
    uid = _user()
    run_id = db.record_run(uid, "x", 0)
    db.record_cost(run_id, uid, "opening", {"serper_calls": 3, "cost_usd": 0.0042, "duration_ms": 900})
    stats = db.cost_stats("2000-01-01")
    assert stats[0]["runs"] == 1 and abs(stats[0]["avg_cost"] - 0.0042) < 1e-9
