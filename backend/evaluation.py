"""Is a change an improvement? Measure it instead of guessing.

Two metrics, run before and after any sourcing or scoring change:

  precision@10   of the top ten results, the share worth applying to / contacting
  dead-link rate of the top ten, the share whose link no longer leads to an open posting

Labels come from two places: the feedback buttons in the app (thumbs, "applied",
"messaged", "got a reply"), and a hand-labelled JSONL file built with
`scripts/eval.py export` -- label 150-200 results from your own searches and
you have an eval set that does not depend on anyone else using the app.

`rerank` replays stored runs under a different weighting, which is how a
scoring change is compared offline without spending a single API credit.
Sourcing changes cannot be replayed; for those, run the same searches before
and after and compare the reports.
"""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from pathlib import Path

import httpx

import boards
import db
import weights

K = 10


def precision_at_k(labels_in_rank_order: list[int | None], k: int = K) -> float | None:
    """Share of *labelled* items in the top k that are good; None if none are labelled.

    Unlabelled items are left out rather than counted as bad: a result nobody
    looked at is not evidence against it.
    """
    judged = [y for y in labels_in_rank_order[:k] if y is not None]
    return sum(judged) / len(judged) if judged else None


def load_labels(path: str | Path) -> dict[tuple[int | None, str], int]:
    """JSONL rows of {"item_key", "label": 0|1, "run_id"?}. Blank labels are skipped."""
    out: dict[tuple[int | None, str], int] = {}
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        if row.get("label") in (0, 1, True, False):
            out[(row.get("run_id"), row["item_key"])] = int(row["label"])
    return out


def feedback_labels(kind: str) -> dict[tuple[int | None, str], int]:
    out: dict[tuple[int | None, str], int] = {}
    for row in db.labeled_feedback(kind):
        y = weights.label_of(row)
        if y is not None:
            out[(row["run_id"], row["item_key"])] = y
            out.setdefault((None, row["item_key"]), y)       # item-level, for any run
    return out


def _label(labels: dict, run_id: int, key: str) -> int | None:
    y = labels.get((run_id, key))
    return y if y is not None else labels.get((None, key))


def run_precisions(labels: dict, run_ids: list[int], order: str = "stored",
                   model: dict | None = None) -> dict[int, float]:
    """precision@10 per run, in the stored order or re-ranked by `model`
    (order="rerank"; model=None means the default weights)."""
    by_run: dict[int, list] = defaultdict(list)
    for row in db.results_for_runs(run_ids):
        by_run[row["run_id"]].append(row)
    out: dict[int, float] = {}
    for run_id, rows in by_run.items():
        if order == "rerank":
            def key(r):
                try:
                    return -weights.score(json.loads(r["features"] or "{}"), model)
                except ValueError:
                    return 0
            rows = sorted(rows, key=key)
        p = precision_at_k([_label(labels, run_id, r["item_key"]) for r in rows])
        if p is not None:
            out[run_id] = p
    return out


def mean(values) -> float | None:
    values = list(values)
    return round(sum(values) / len(values), 4) if values else None


def feedback_report(kind: str = "opening") -> dict:
    """In-app quality numbers, from feedback alone."""
    rows = db.labeled_feedback(kind)
    labels = feedback_labels(kind)
    run_ids = sorted({r["run_id"] for r in rows if r["run_id"] is not None})
    stored = run_precisions(labels, run_ids)
    return {
        "kind": kind,
        "labelled_items": len(rows),
        "good": sum(1 for r in rows if weights.label_of(r) == 1),
        "applied": sum(1 for r in rows if r["applied"]),
        "messaged": sum(1 for r in rows if r["messaged"]),
        "replied": sum(1 for r in rows if r["replied"]),
        "runs_with_labels": len(stored),
        "precision_at_10": mean(stored.values()),
    }


# --- dead links ------------------------------------------------------------------------
async def _link_alive(client: httpx.AsyncClient, key: str, url: str) -> bool:
    """A board posting is alive if the board still lists it; anything else, if the page loads."""
    if ":" in key and "://" not in key:
        row = db.get_posting(key)
        if row is not None:
            return row["closed_at"] is None
    from openings import _alive
    return await _alive(client, url)


async def dead_link_rate(items: list[tuple[str, str]]) -> dict:
    """items of (item_key, url). Returns {"checked", "dead", "rate"}."""
    if not items:
        return {"checked": 0, "dead": 0, "rate": None}
    gate = asyncio.Semaphore(8)
    async with httpx.AsyncClient(timeout=10.0, follow_redirects=True,
                                 headers={"User-Agent": boards.USER_AGENT}) as client:
        async def one(key, url):
            async with gate:
                return await _link_alive(client, key, url)
        alive = await asyncio.gather(*(one(k, u) for k, u in items))
    dead = sum(1 for a in alive if not a)
    return {"checked": len(items), "dead": dead, "rate": round(dead / len(items), 4)}


async def report(kind: str = "opening", label_file: str | None = None, runs: int = 50,
                 check_links: bool = False) -> dict:
    """The full eval: precision@10 as stored and re-ranked, plus dead links."""
    labels = feedback_labels(kind)
    if label_file:
        labels.update(load_labels(label_file))
    run_ids = sorted({rid for rid, _ in labels if rid is not None})[-runs:]
    out = {
        "kind": kind,
        "labels": len({k for _, k in labels}),
        "runs": len(run_ids),
        "precision_at_10": {
            "as_shown": mean(run_precisions(labels, run_ids).values()),
            "default_weights": mean(run_precisions(labels, run_ids, "rerank", None).values()),
        },
    }
    learned = weights.current(kind)
    if learned:
        out["precision_at_10"]["learned_weights"] = mean(
            run_precisions(labels, run_ids, "rerank", learned).values())
    if check_links:
        top = [(r["item_key"], r["url"]) for r in db.results_for_runs(run_ids) if r["rank"] < K]
        out["dead_links_top_10"] = await dead_link_rate(top)
    return out
