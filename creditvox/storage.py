"""
storage.py
==========
Persistence layer for consent, audit, and feedback. ONE file decides the backend:

  * No DATABASE_URL set  -> SQLite at DB_PATH (dev default, pure stdlib, zero deps).
  * DATABASE_URL set     -> Postgres via psycopg (pip install -r requirements-postgres.txt).

The rest of the app (audit.py, feedback.py) is written against a single tiny
connection interface, so switching backends is an env var — no code change.

Design: one short-lived connection per operation. SQLite connections aren't safe
across threads and FastAPI runs sync endpoints in a threadpool; opening per call
(cheap) avoids all thread-affinity problems. For Postgres this is also fine at
this scale; add a pool later if you need one.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional, Sequence

from .config import settings


def _backend() -> str:
    return "postgres" if settings.database_url else "sqlite"


class _Conn:
    """Thin uniform wrapper so callers write one SQL dialect (`?` placeholders,
    dict-style row access) regardless of backend."""

    def __init__(self, raw: Any, backend: str):
        self._raw = raw
        self._backend = backend

    def execute(self, sql: str, params: Sequence[Any] = ()):
        if self._backend == "postgres":
            cur = self._raw.cursor()
            cur.execute(sql.replace("?", "%s"), tuple(params))
            return cur
        return self._raw.execute(sql, tuple(params))

    def executescript(self, script: str) -> None:
        if self._backend == "sqlite":
            self._raw.executescript(script)
        else:
            cur = self._raw.cursor()
            for stmt in (s.strip() for s in script.split(";") if s.strip()):
                cur.execute(stmt)


@contextmanager
def get_conn(db_path: Optional[str] = None) -> Iterator[_Conn]:
    backend = _backend()
    if backend == "postgres":
        import psycopg                     # lazy: only needed for Postgres
        from psycopg.rows import dict_row
        raw = psycopg.connect(settings.database_url, row_factory=dict_row)
    else:
        raw = sqlite3.connect(db_path or settings.db_path, timeout=10)
        raw.row_factory = sqlite3.Row
        raw.execute("PRAGMA journal_mode=WAL;")
        raw.execute("PRAGMA foreign_keys=ON;")

    conn = _Conn(raw, backend)
    try:
        yield conn
        raw.commit()
    except Exception:
        raw.rollback()
        raise
    finally:
        raw.close()


# DDL is portable across SQLite and Postgres (TEXT / INTEGER / REAL, IF NOT EXISTS).
_SCHEMA = """
CREATE TABLE IF NOT EXISTS consents (
    consent_id          TEXT PRIMARY KEY,
    consumer_token_hash TEXT NOT NULL,
    purpose             TEXT NOT NULL,
    disclosure_version  TEXT NOT NULL,
    granted             INTEGER NOT NULL,
    granted_at          TEXT NOT NULL,
    expires_at          TEXT NOT NULL,
    ip                  TEXT
);

CREATE TABLE IF NOT EXISTS audit_log (
    entry_id            TEXT PRIMARY KEY,
    ts                  TEXT NOT NULL,
    event               TEXT NOT NULL,
    provider            TEXT NOT NULL,
    inquiry_type        TEXT NOT NULL,
    consent_id          TEXT NOT NULL,
    consumer_token_hash TEXT NOT NULL,
    report_id           TEXT,
    detail              TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS feedback (
    feedback_id                TEXT PRIMARY KEY,
    ts                         TEXT NOT NULL,
    consumer_token_hash        TEXT NOT NULL,
    card_id                    TEXT NOT NULL,
    outcome                    TEXT NOT NULL,
    score                      INTEGER,
    utilization                REAL,
    inquiries                  INTEGER,
    new_accounts_24mo          INTEGER,
    report_id                  TEXT,
    approval_probability_shown REAL,
    fit_score_shown            REAL,
    notes                      TEXT
);

CREATE TABLE IF NOT EXISTS users (
    user_id       TEXT PRIMARY KEY,
    email         TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    session_hash  TEXT PRIMARY KEY,
    user_id       TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    expires_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS saved_checks (
    check_id          TEXT PRIMARY KEY,
    user_id           TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    score             INTEGER,
    utilization       REAL,
    inquiries         INTEGER,
    new_accounts_24mo INTEGER,
    rank_by           TEXT,
    source            TEXT,
    top_card_id       TEXT,
    top_card_name     TEXT,
    top_probability   REAL,
    result_count      INTEGER
);

CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts);
CREATE INDEX IF NOT EXISTS idx_consent_token ON consents(consumer_token_hash);
CREATE INDEX IF NOT EXISTS idx_feedback_card ON feedback(card_id);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_checks_user ON saved_checks(user_id, created_at);
"""


def init_db(db_path: Optional[str] = None) -> None:
    if _backend() == "sqlite":
        Path(db_path or settings.db_path).parent.mkdir(parents=True, exist_ok=True)
    with get_conn(db_path) as conn:
        conn.executescript(_SCHEMA)
