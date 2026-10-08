"""User feedback on replies: thumbs up or down per request. (PR-04)

This is the online quality signal next to the offline eval set. Each rating is one JSON line
in data/feedback.jsonl: time, request id and rating. No message text, so the file never holds
what a user typed. PR-07's request log joins on request_id.
"""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Literal

Rating = Literal["up", "down"]
REQUEST_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class FeedbackLog:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()

    def record(self, request_id: str, rating: Rating) -> dict:
        if not REQUEST_ID.match(request_id):
            raise ValueError("request_id must be 1-64 letters, digits, '-' or '_'")
        if rating not in ("up", "down"):
            raise ValueError("rating must be 'up' or 'down'")
        entry = {"time": round(time.time(), 3), "request_id": request_id, "rating": rating}
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
        return entry

    def summary(self) -> dict:
        """Latest rating per request (a user can change their mind), then the up rate."""
        latest: dict[str, str] = {}
        if self.path.is_file():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    entry = json.loads(line)
                    latest[entry["request_id"]] = entry["rating"]
        up = sum(r == "up" for r in latest.values())
        return {
            "rated": len(latest),
            "up": up,
            "down": len(latest) - up,
            "up_rate": round(up / len(latest), 4) if latest else None,
        }
