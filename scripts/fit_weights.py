#!/usr/bin/env python
"""Learn ranking weights from what users marked good.

Saves the new weights only if they beat the defaults on held-out labels, and
only once there are MIN_LABELS_FOR_WEIGHTS labels (default 200). Safe to run
from a cron: it does nothing until it has enough evidence.

  python scripts/fit_weights.py [--kind opening] [--min-labels 200] [--dry-run]
"""

import argparse
import json

import _path  # noqa: F401

import db
import weights


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--kind", default="opening", choices=["opening", "person"])
    parser.add_argument("--min-labels", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    db.init_db()
    print(json.dumps(weights.fit(args.kind, args.min_labels, save=not args.dry_run), indent=2))
    db.close()


if __name__ == "__main__":
    main()
