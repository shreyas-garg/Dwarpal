"""Budget protection for the public demo: a per-IP rate limit and a daily request cap. (PR-05)

Both are in memory and per process, which is right for one free-tier Space with one worker;
a restart resets them. Both are off by default (0) so local dev and tests are unaffected;
the Docker image turns them on.

  per-IP   sliding 60 s window. The demo calls the proxy from 127.0.0.1, so every demo visitor
           shares one address; loopback is exempt and the daily cap is what bounds the demo.
  daily    one counter for every chat request, reset at midnight UTC. This is what keeps the
           upstream bill under the project budget.

Behind the Hugging Face proxy the client address is the last X-Forwarded-For entry (the one
the proxy appended); earlier entries come from the client and can be forged.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from datetime import UTC, datetime

from fastapi import Request

LOOPBACK = {"127.0.0.1", "::1", "localhost"}


def client_ip(request: Request, trust_forwarded_for: bool) -> str:
    if trust_forwarded_for:
        forwarded = request.headers.get("x-forwarded-for", "")
        hops = [h.strip() for h in forwarded.split(",") if h.strip()]
        if hops:
            return hops[-1]
    return request.client.host if request.client else "unknown"


class RateLimiter:
    """At most `per_minute` requests per key in any 60-second window. 0 = unlimited."""

    def __init__(self, per_minute: int, clock=time.monotonic):
        self.per_minute = per_minute
        self.clock = clock
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        if self.per_minute <= 0:
            return True
        now = self.clock()
        with self._lock:
            hits = self._hits.setdefault(key, deque())
            while hits and now - hits[0] >= 60:
                hits.popleft()
            if len(hits) >= self.per_minute:
                return False
            hits.append(now)
            # Drop idle keys now and then so the dict can't grow without bound.
            if len(self._hits) > 10_000:
                self._hits = {k: v for k, v in self._hits.items() if v and now - v[-1] < 60}
            return True


class DailyCap:
    """At most `limit` requests per UTC day across all clients. 0 = unlimited."""

    def __init__(self, limit: int, today=lambda: datetime.now(UTC).date()):
        self.limit = limit
        self.today = today
        self._day = today()
        self.count = 0
        self._lock = threading.Lock()

    def allow(self) -> bool:
        if self.limit <= 0:
            return True
        with self._lock:
            if (day := self.today()) != self._day:
                self._day, self.count = day, 0
            if self.count >= self.limit:
                return False
            self.count += 1
            return True
