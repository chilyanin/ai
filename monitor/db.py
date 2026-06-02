"""SQLite layer for the monitor service.

Schema is intentionally small: one row per scheduled snapshot in `runs`,
plus a per-run breakdown of the tasks observed in `tasks`. Both tables are
append-only — we never UPDATE; pruning old data is a separate concern (call
`prune(retain_days=30)` from a cron if it ever grows).
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .classifier import BUCKETS

DB_PATH = Path(__file__).resolve().parent.parent / "monitor.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ran_at      TEXT NOT NULL,
    duration_ms INTEGER NOT NULL,
    task_count  INTEGER NOT NULL,
    offboarding INTEGER NOT NULL DEFAULT 0,
    access      INTEGER NOT NULL DEFAULT 0,
    procurement INTEGER NOT NULL DEFAULT 0,
    other       INTEGER NOT NULL DEFAULT 0,
    error       TEXT
);

CREATE TABLE IF NOT EXISTS tasks (
    run_id        INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    gid           TEXT NOT NULL,
    name          TEXT NOT NULL,
    bucket        TEXT NOT NULL,
    due_on        TEXT,
    permalink_url TEXT
);

CREATE INDEX IF NOT EXISTS idx_tasks_run ON tasks(run_id);
CREATE INDEX IF NOT EXISTS idx_runs_ran_at ON runs(ran_at);

CREATE TABLE IF NOT EXISTS actions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ran_at     TEXT NOT NULL,
    kind       TEXT NOT NULL,         -- 'deactivate' | 'grant'
    due_date   TEXT,                  -- the --date the run targeted
    service    TEXT NOT NULL,
    target     TEXT NOT NULL,
    status     TEXT NOT NULL,
    gid        TEXT,
    screenshot TEXT                   -- relative path to proof screenshot
);

CREATE INDEX IF NOT EXISTS idx_actions_ran_at ON actions(ran_at);

CREATE TABLE IF NOT EXISTS triage (
    gid              TEXT PRIMARY KEY,
    title_hash       TEXT NOT NULL,
    model            TEXT,
    subcategory      TEXT,
    target_service   TEXT,
    suggested_action TEXT,
    confidence       TEXT,
    rationale        TEXT,
    created_at       TEXT NOT NULL
);
"""


@contextmanager
def _conn():
    c = sqlite3.connect(str(DB_PATH))
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init_db() -> None:
    """Create tables if they don't exist. Safe to call on every startup."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _conn() as c:
        c.executescript(_SCHEMA)
        # Migration: add actions.screenshot to pre-existing DBs.
        cols = {r["name"] for r in c.execute("PRAGMA table_info(actions)").fetchall()}
        if "screenshot" not in cols:
            c.execute("ALTER TABLE actions ADD COLUMN screenshot TEXT")


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def insert_run(
    *,
    counts: dict[str, int],
    enriched_tasks: Iterable[dict],
    duration_ms: int,
    error: str | None = None,
    ran_at: str | None = None,
) -> int:
    """Record a snapshot. Returns the new run id."""
    ran_at = ran_at or _utcnow_iso()
    task_count = sum(counts.get(b, 0) for b in BUCKETS)
    with _conn() as c:
        cur = c.execute(
            """
            INSERT INTO runs (ran_at, duration_ms, task_count,
                              offboarding, access, procurement, other, error)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ran_at,
                int(duration_ms),
                task_count,
                int(counts.get("offboarding", 0)),
                int(counts.get("access", 0)),
                int(counts.get("procurement", 0)),
                int(counts.get("other", 0)),
                error,
            ),
        )
        run_id = cur.lastrowid
        c.executemany(
            """
            INSERT INTO tasks (run_id, gid, name, bucket, due_on, permalink_url)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                (
                    run_id,
                    t.get("gid", ""),
                    t.get("name", ""),
                    t.get("bucket", "other"),
                    t.get("due_on"),
                    t.get("permalink_url"),
                )
                for t in enriched_tasks
            ),
        )
        return run_id


def recent_runs(limit: int = 96) -> list[dict]:
    """Return the most recent `limit` runs (newest first), as plain dicts.
    96 = 24h of 15-minute runs.
    """
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM runs ORDER BY id DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
    return [dict(r) for r in rows]


def latest_run() -> dict | None:
    with _conn() as c:
        row = c.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    return dict(row) if row else None


def latest_tasks() -> list[dict]:
    """Tasks recorded in the most recent run (any bucket)."""
    last = latest_run()
    if not last:
        return []
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM tasks WHERE run_id = ? ORDER BY bucket, name",
            (last["id"],),
        ).fetchall()
    return [dict(r) for r in rows]


def insert_actions(
    *,
    kind: str,
    due_date: str | None,
    results: Iterable[dict],
    ran_at: str | None = None,
) -> int:
    """Record one row per deactivation/grant result. Returns rows inserted.

    Each `results` item is {gid, service, target, status}.
    """
    ran_at = ran_at or _utcnow_iso()
    rows = [
        (
            ran_at,
            kind,
            due_date,
            r.get("service", ""),
            r.get("target", ""),
            r.get("status", ""),
            r.get("gid"),
            r.get("screenshot") or None,
        )
        for r in results
    ]
    if not rows:
        return 0
    with _conn() as c:
        c.executemany(
            """
            INSERT INTO actions (ran_at, kind, due_date, service, target, status, gid, screenshot)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
    return len(rows)


def recent_actions(limit: int = 200, kind: str | None = None) -> list[dict]:
    """Most recent action-log rows (newest first)."""
    with _conn() as c:
        if kind:
            rows = c.execute(
                "SELECT * FROM actions WHERE kind = ? ORDER BY id DESC LIMIT ?",
                (kind, int(limit)),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM actions ORDER BY id DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
    return [dict(r) for r in rows]


def get_triage(gid: str) -> dict | None:
    with _conn() as c:
        row = c.execute("SELECT * FROM triage WHERE gid = ?", (gid,)).fetchone()
    return dict(row) if row else None


def upsert_triage(gid: str, title_hash: str, result: dict) -> None:
    """Store/replace a triage result for a task."""
    with _conn() as c:
        c.execute(
            """
            INSERT INTO triage (gid, title_hash, model, subcategory, target_service,
                                suggested_action, confidence, rationale, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(gid) DO UPDATE SET
                title_hash=excluded.title_hash,
                model=excluded.model,
                subcategory=excluded.subcategory,
                target_service=excluded.target_service,
                suggested_action=excluded.suggested_action,
                confidence=excluded.confidence,
                rationale=excluded.rationale,
                created_at=excluded.created_at
            """,
            (
                gid,
                title_hash,
                result.get("model"),
                result.get("subcategory"),
                result.get("target_service"),
                result.get("suggested_action"),
                result.get("confidence"),
                result.get("rationale"),
                _utcnow_iso(),
            ),
        )


def prune(retain_days: int = 30) -> int:
    """Delete runs older than `retain_days`. Returns rows deleted."""
    cutoff = datetime.now(timezone.utc).timestamp() - retain_days * 86400
    cutoff_iso = datetime.fromtimestamp(cutoff, tz=timezone.utc).isoformat(
        timespec="seconds"
    )
    with _conn() as c:
        cur = c.execute("DELETE FROM runs WHERE ran_at < ?", (cutoff_iso,))
        return cur.rowcount
