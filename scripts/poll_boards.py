#!/usr/bin/env python
"""The daily poll: re-read every known job board, close what disappeared, send alerts.

Scheduled as a Render cron job (render.yaml). Run by hand with:
  python scripts/poll_boards.py [--limit N]
"""

import argparse
import asyncio
import json
import logging

import _path  # noqa: F401

import db
import poller


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None, help="poll at most N boards")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    try:
        print(json.dumps(asyncio.run(poller.run(args.limit)), indent=2))
    finally:
        db.close()


if __name__ == "__main__":
    main()
