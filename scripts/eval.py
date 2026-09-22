#!/usr/bin/env python
"""Measure result quality before and after a change.

  # 1. Dump recent results to label by hand (fill each "label" with 1 or 0):
  python scripts/eval.py export --runs 10 --out labels.jsonl

  # 2. Report precision@10 (as shown, and re-ranked by default/learned weights)
  #    from in-app feedback plus your labels, and check the top 10 for dead links:
  python scripts/eval.py report --labels labels.jsonl --check-links

Run `report` before and after every sourcing or scoring change. See
backend/evaluation.py for what the numbers mean.
"""

import argparse
import asyncio
import json

import _path  # noqa: F401

import db
import evaluation


def export(kind: str, runs: int, out: str, user: str | None) -> None:
    user_id = None
    if user:
        row = db.get_user_by_email(user.strip().lower())
        if row is None:
            raise SystemExit(f"No user {user!r}")
        user_id = row["id"]
    run_ids = db.recent_run_ids(kind, runs, user_id)
    existing = set()
    try:
        existing = {(r.get("run_id"), r["item_key"]) for r in map(json.loads, open(out)) if r}
    except FileNotFoundError:
        pass
    added = 0
    with open(out, "a") as fh:
        for row in db.results_for_runs(run_ids):
            if (row["run_id"], row["item_key"]) in existing:
                continue
            fh.write(json.dumps({"run_id": row["run_id"], "rank": row["rank"], "item_key": row["item_key"],
                                 "title": row["title"], "url": row["url"], "fit_score": row["fit_score"],
                                 "label": None}) + "\n")
            added += 1
    print(f"Appended {added} results from {len(run_ids)} runs to {out}. Set each label to 1 or 0.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    ex = sub.add_parser("export")
    ex.add_argument("--kind", default="opening", choices=["opening", "person"])
    ex.add_argument("--runs", type=int, default=10)
    ex.add_argument("--out", default="labels.jsonl")
    ex.add_argument("--user", help="only this user's runs (email)")
    rp = sub.add_parser("report")
    rp.add_argument("--kind", default="opening", choices=["opening", "person"])
    rp.add_argument("--labels", help="hand-labelled JSONL from `export`")
    rp.add_argument("--runs", type=int, default=50)
    rp.add_argument("--check-links", action="store_true")
    args = parser.parse_args()

    db.init_db()
    if args.cmd == "export":
        export(args.kind, args.runs, args.out, args.user)
    else:
        print(json.dumps(asyncio.run(evaluation.report(args.kind, args.labels, args.runs, args.check_links)),
                         indent=2))
    db.close()


if __name__ == "__main__":
    main()
