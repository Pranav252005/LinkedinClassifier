import json
import random

import db
import weights


def _data(n=300, seed=1):
    rng = random.Random(seed)
    rows = []
    for _ in range(n):
        must = rng.random()
        x = {"sim": rng.random(), "must": must, "level": rng.choice([0.25, 0.5, 1.0]), "skills": rng.random(),
             "recency": rng.random(), "verified": 1.0, "reviewed": 1.0}
        rows.append((x, 1 if must + 0.1 * rng.random() > 0.6 else 0))
    return rows


def test_train_learns_the_signal():
    model = weights.train(_data())
    assert model["w"]["must"] > 2 * abs(model["w"]["recency"])
    test = _data(200, seed=2)
    assert weights.auc([weights.learned_score(x, model) for x, _ in test], [y for _, y in test]) > 0.9


def test_auc_edges():
    assert weights.auc([1, 2], [1, 1]) is None
    assert weights.auc([0.9, 0.1], [1, 0]) == 1.0


def test_label_of():
    row = lambda **kw: {"rating": 0, "applied": 0, "messaged": 0, "replied": 0, **kw}
    assert weights.label_of(row(rating=1)) == 1
    assert weights.label_of(row(rating=-1)) == 0
    assert weights.label_of(row(rating=-1, applied=1)) == 1      # actions outrank thumbs
    assert weights.label_of(row()) is None


def test_fit_needs_enough_labels_then_saves():
    uid = db.create_user("w@example.com", "h")
    assert weights.fit("opening", min_labels=50)["saved"] is False
    for i, (x, y) in enumerate(_data(120)):
        db.upsert_feedback(uid, "opening", f"k{i}", {"rating": 1 if y else -1}, None, None,
                           json.dumps(x), "", "")
    report = weights.fit("opening", min_labels=50)
    assert report["labels"] == 120
    assert report["saved"] is True, report
    assert weights.current("opening") is not None


def test_default_score_bounds():
    assert weights.default_score({}) == 50                      # all neutral
    top = {k: 1.0 for k in weights.FEATURES}
    assert weights.default_score(top) == 100
