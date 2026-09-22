"""How ranking features combine into a fit score -- by default, and learned.

The default is a hand-set weighted sum. It is a reasonable guess and nothing
more, so once users have marked enough results (thumbs, "applied", "got a
reply"), `fit()` learns a logistic regression over the same features and it
replaces the default -- but only if it beats the default on held-out labels.

Pure Python on purpose: a few hundred rows by seven features does not need
numpy, and the app should not grow a heavy dependency for it.
"""

from __future__ import annotations

import json
import math
import random
import time

import db
from config import MIN_LABELS_FOR_WEIGHTS

FEATURES = ("sim", "must", "level", "skills", "recency", "verified", "reviewed")

DEFAULT = {"sim": 0.20, "must": 0.40, "level": 0.20, "skills": 0.15, "recency": 0.05}

_CACHE: dict[str, tuple[float, dict | None]] = {}
_CACHE_SECONDS = 600


def default_score(x: dict[str, float]) -> int:
    base = sum(w * float(x.get(k, 0.5)) for k, w in DEFAULT.items())
    # A web-search result could be stale; a board listing is live right now.
    base *= 0.9 + 0.1 * float(x.get("verified", 1.0))
    return max(0, min(100, round(100 * base)))


def _sigmoid(z: float) -> float:
    if z < -30:
        return 0.0
    if z > 30:
        return 1.0
    return 1 / (1 + math.exp(-z))


def learned_score(x: dict[str, float], model: dict) -> int:
    z = model["bias"] + sum(model["w"].get(k, 0.0) * float(x.get(k, 0.5)) for k in FEATURES)
    return max(0, min(100, round(100 * _sigmoid(z))))


def score(x: dict[str, float], model: dict | None) -> int:
    return learned_score(x, model) if model else default_score(x)


def current(kind: str) -> dict | None:
    """The latest learned model for this kind, cached for a few minutes."""
    hit = _CACHE.get(kind)
    if hit and time.monotonic() - hit[0] < _CACHE_SECONDS:
        return hit[1]
    model = None
    try:
        row = db.latest_weights(kind)
        if row:
            model = json.loads(row["weights"])
    except Exception:
        model = None
    _CACHE[kind] = (time.monotonic(), model)
    return model


# --- learning -------------------------------------------------------------------
def label_of(row) -> int | None:
    """1 = worth it, 0 = not, None = no opinion. Actions outrank thumbs: a
    posting someone applied to was worth showing whatever they clicked."""
    if row["replied"] or row["applied"] or row["messaged"]:
        return 1
    if row["rating"] > 0:
        return 1
    if row["rating"] < 0:
        return 0
    return None


def examples(kind: str) -> list[tuple[dict[str, float], int]]:
    out = []
    for row in db.labeled_feedback(kind):
        y = label_of(row)
        try:
            x = json.loads(row["features"] or "{}")
        except ValueError:
            continue
        if y is None or not x:
            continue
        out.append((x, y))
    return out


def train(data: list[tuple[dict[str, float], int]], epochs: int = 400, lr: float = 0.5,
          l2: float = 0.01) -> dict:
    """Batch gradient descent on log-loss with L2."""
    w = {k: 0.0 for k in FEATURES}
    b = 0.0
    n = max(1, len(data))
    for _ in range(epochs):
        gw = {k: 0.0 for k in FEATURES}
        gb = 0.0
        for x, y in data:
            p = _sigmoid(b + sum(w[k] * float(x.get(k, 0.5)) for k in FEATURES))
            err = p - y
            gb += err
            for k in FEATURES:
                gw[k] += err * float(x.get(k, 0.5))
        b -= lr * gb / n
        for k in FEATURES:
            w[k] -= lr * (gw[k] / n + l2 * w[k])
    return {"bias": b, "w": w, "features": list(FEATURES)}


def auc(scores: list[float], labels: list[int]) -> float | None:
    """Probability a random good item outranks a random bad one."""
    pos = [s for s, y in zip(scores, labels) if y == 1]
    neg = [s for s, y in zip(scores, labels) if y == 0]
    if not pos or not neg:
        return None
    wins = sum(1.0 if p > q else 0.5 if p == q else 0.0 for p in pos for q in neg)
    return wins / (len(pos) * len(neg))


def fit(kind: str = "opening", min_labels: int | None = None, seed: int = 7,
        save: bool = True) -> dict:
    """Learn weights from feedback; save them only if they beat the defaults."""
    need = MIN_LABELS_FOR_WEIGHTS if min_labels is None else min_labels
    data = examples(kind)
    report: dict = {"labels": len(data), "needed": need, "saved": False}
    if len(data) < need:
        report["reason"] = f"only {len(data)} labels; need {need}"
        return report
    if len({y for _, y in data}) < 2:
        report["reason"] = "labels are all one class"
        return report

    rng = random.Random(seed)
    rows = data[:]
    rng.shuffle(rows)
    cut = max(1, int(len(rows) * 0.75))
    train_rows, test_rows = rows[:cut], rows[cut:]
    model = train(train_rows)

    labels = [y for _, y in test_rows]
    report["auc_default"] = auc([default_score(x) for x, _ in test_rows], labels)
    report["auc_learned"] = auc([learned_score(x, model) for x, _ in test_rows], labels)
    report["weights"] = {k: round(v, 3) for k, v in model["w"].items()}

    better = (report["auc_learned"] or 0) > (report["auc_default"] or 0)
    if not better:
        report["reason"] = "learned weights did not beat the defaults on held-out labels"
        return report

    final = train(rows)                                  # refit on everything
    if save:
        db.save_weights(kind, json.dumps(final), len(rows), report["auc_learned"])
        _CACHE.pop(kind, None)
        report["saved"] = True
    return report
