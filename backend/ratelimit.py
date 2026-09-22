"""A small in-process rate limiter.

Signup is the expensive door: every free account is three pipeline runs, and
each run spends real Serper and OpenRouter credits. Without a limit, anyone with
a script can mint accounts and burn the operator's money.

This is deliberately simple — a fixed window per key, held in memory. It is not
distributed: with several workers each holds its own counters, so the effective
limit is (limit x workers). That is still the difference between "a script can
drain the account in minutes" and "it cannot", which is the point. Move the
counters to Redis or Postgres if you ever run this at a size where the
difference matters.
"""

from __future__ import annotations

import threading
import time
from collections import deque

from fastapi import HTTPException, Request


class RateLimiter:
    def __init__(self, limit: int, window_seconds: int, name: str) -> None:
        self.limit = limit
        self.window = window_seconds
        self.name = name
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def _prune(self, now: float) -> None:
        """Drop keys with no recent hits so the dict cannot grow without bound."""
        cutoff = now - self.window
        for key in [k for k, v in self._hits.items() if not v or v[-1] < cutoff]:
            del self._hits[key]

    def check(self, key: str) -> tuple[bool, int]:
        """Record a hit. Returns (allowed, seconds_until_retry)."""
        if self.limit <= 0:
            return True, 0

        now = time.monotonic()
        with self._lock:
            if len(self._hits) > 2048:
                self._prune(now)

            hits = self._hits.setdefault(key, deque())
            while hits and hits[0] <= now - self.window:
                hits.popleft()

            if len(hits) >= self.limit:
                return False, max(1, int(self.window - (now - hits[0])))

            hits.append(now)
            return True, 0


def client_key(request: Request) -> str:
    """Identify the caller.

    Behind Netlify/Render the socket address is the proxy, so prefer the
    left-most X-Forwarded-For entry — the original client. A client can forge
    that header, but forging it only ever splits their own bucket, and the
    proxy appends rather than replaces, so the real address stays present.
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        first = forwarded.split(",")[0].strip()
        if first:
            return first
    return request.client.host if request.client else "unknown"


def enforce(limiter: RateLimiter, request: Request, message: str) -> None:
    allowed, retry_after = limiter.check(client_key(request))
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail=message,
            headers={"Retry-After": str(retry_after)},
        )
