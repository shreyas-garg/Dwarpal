"""The request log: one SQLite row per proxied request, and the stats computed from it. (PR-07)

A row holds what the dashboard and GET /v1/dwarpal/stats need: policies and versions, each
guard's decision and time, upstream time and tokens, costs, total and added latency. The prompt
itself is stored only as a SHA-256 of the user text unless LOG_PROMPTS is on (a PII filter
shouldn't hoard PII), and guard reasons are left out because some quote the text they matched.

Each call opens its own short-lived connection, so the background writer and the stats
endpoint never share one. WAL mode lets them read and write at the same time.
"""

from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

COLUMNS = (
    "request_id",
    "ts",
    "policies",
    "guards",
    "blocked_by",
    "blocked_stage",
    "model",
    "prompt_tokens",
    "completion_tokens",
    "upstream_ms",
    "total_ms",
    "added_ms",
    "upstream_cost_usd",
    "guard_cost_usd",
    "cost_usd",
    "prompt_sha256",
    "prompt",
)
JSON_COLUMNS = ("policies", "guards")

SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    request_id TEXT PRIMARY KEY,
    ts REAL NOT NULL,              -- unix time the pipeline finished
    policies TEXT NOT NULL,        -- JSON list of name@version that were active
    guards TEXT NOT NULL,          -- JSON list, one entry per guard that ran (shadow ones too)
    blocked_by TEXT,
    blocked_stage TEXT,
    model TEXT,
    prompt_tokens INTEGER NOT NULL,
    completion_tokens INTEGER NOT NULL,
    upstream_ms REAL NOT NULL,
    total_ms REAL NOT NULL,
    added_ms REAL NOT NULL,        -- total - upstream: what Dwarpal added
    upstream_cost_usd REAL NOT NULL,
    guard_cost_usd REAL NOT NULL,
    cost_usd REAL NOT NULL,
    prompt_sha256 TEXT NOT NULL,
    prompt TEXT                    -- NULL unless LOG_PROMPTS=true
);
CREATE INDEX IF NOT EXISTS requests_ts ON requests (ts);
"""


class RequestStore:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=10)
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA synchronous=NORMAL")
            with db:  # one transaction, committed on success
                yield db
        finally:
            db.close()

    def insert(self, rows: list[dict[str, Any]]) -> None:
        values = [
            tuple(json.dumps(r[c]) if c in JSON_COLUMNS else r[c] for c in COLUMNS) for r in rows
        ]
        placeholders = ", ".join("?" for _ in COLUMNS)
        with self._connect() as db:
            db.executemany(
                f"INSERT OR REPLACE INTO requests ({', '.join(COLUMNS)}) VALUES ({placeholders})",
                values,
            )

    def since(self, ts: float, until: float | None = None) -> list[dict[str, Any]]:
        """Rows that finished at or after ts (and at or before until), oldest first."""
        query, params = "SELECT * FROM requests WHERE ts >= ?", [ts]
        if until is not None:
            query, params = query + " AND ts <= ?", [ts, until]
        with self._connect() as db:
            return [_decode(r) for r in db.execute(query + " ORDER BY ts", params)]

    def recent(self, limit: int) -> list[dict[str, Any]]:
        """The newest rows, newest first."""
        with self._connect() as db:
            cursor = db.execute("SELECT * FROM requests ORDER BY ts DESC LIMIT ?", (limit,))
            return [_decode(r) for r in cursor]


def _decode(row: sqlite3.Row) -> dict[str, Any]:
    out = dict(row)
    for column in JSON_COLUMNS:
        out[column] = json.loads(out[column])
    return out


def percentile(values: list[float], q: float) -> float | None:
    """Nearest-rank percentile, the same definition the eval harness uses."""
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[max(0, math.ceil(q * len(ordered)) - 1)], 3)


def _caught(guard: dict[str, Any]) -> bool:
    """Blocked or redacted. A pii restore changes the reply but catches nothing."""
    return guard["action"] in ("block", "redact") and not guard.get("restored")


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """What GET /v1/dwarpal/stats returns, for any set of request-log rows."""
    count = len(rows)
    blocked = sum(1 for r in rows if r["blocked_by"])
    guards: dict[str, dict[str, Any]] = {}
    for row in rows:
        for g in row["guards"]:
            ref = f"{g['guard']}@{g['version']}"
            entry = guards.setdefault(
                ref,
                {
                    "guard": g["guard"],
                    "version": g["version"],
                    "shadow": g["shadow"],
                    "runs": 0,
                    "blocks": 0,
                    "redacts": 0,
                    "errors": 0,
                    "_latencies": [],
                },
            )
            entry["runs"] += 1
            entry["blocks"] += g["action"] == "block"
            entry["redacts"] += g["action"] == "redact" and not g.get("restored")
            entry["errors"] += bool(g["error"])
            entry["_latencies"].append(g["latency_ms"])
    for entry in guards.values():
        latencies = entry.pop("_latencies")
        entry["block_rate"] = round(entry["blocks"] / entry["runs"], 4)
        entry["redact_rate"] = round(entry["redacts"] / entry["runs"], 4)
        entry["latency_ms"] = {
            "p50": percentile(latencies, 0.5),
            "p99": percentile(latencies, 0.99),
        }

    def latency(column: str) -> dict[str, float | None]:
        values = [r[column] for r in rows]
        return {q: percentile(values, p) for q, p in (("p50", 0.5), ("p95", 0.95), ("p99", 0.99))}

    total_cost = sum(r["cost_usd"] for r in rows)
    return {
        "requests": count,
        "blocked": blocked,
        "block_rate": round(blocked / count, 4) if count else None,
        "latency_ms": {
            "added": latency("added_ms"),
            "total": latency("total_ms"),
            "upstream": latency("upstream_ms"),
        },
        "cost_usd": {
            "total": total_cost,
            "mean_per_request": total_cost / count if count else None,
            "mean_upstream": sum(r["upstream_cost_usd"] for r in rows) / count if count else None,
            "mean_guards": sum(r["guard_cost_usd"] for r in rows) / count if count else None,
        },
        "guards": dict(sorted(guards.items())),
        "shadow": shadow_disagreements(rows),
    }


def shadow_disagreements(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """For each shadow policy: how often its decision differed from what was enforced.

    A shadow result is compared with the enforcing version of the same policy at the same
    stage in the same request. A shadow policy with no enforcing version is compared with
    "nothing caught", so its disagreements are the requests it would have blocked or redacted.
    """
    pairs: dict[tuple[str, str | None], dict[str, Any]] = {}
    for row in rows:
        enforced = {(g["guard"], g["stage"]): g for g in row["guards"] if not g["shadow"]}
        for g in row["guards"]:
            if not g["shadow"]:
                continue
            other = enforced.get((g["guard"], g["stage"]))
            key = (
                f"{g['guard']}@{g['version']}",
                f"{other['guard']}@{other['version']}" if other else None,
            )
            pair = pairs.setdefault(
                key,
                {
                    "shadow": key[0],
                    "enforce": key[1],
                    "compared": 0,
                    "disagreements": 0,
                    "example_request_ids": [],
                },
            )
            pair["compared"] += 1
            if _caught(g) != (_caught(other) if other else False):
                pair["disagreements"] += 1
                if len(pair["example_request_ids"]) < 5:
                    pair["example_request_ids"].append(row["request_id"])
    return list(pairs.values())
