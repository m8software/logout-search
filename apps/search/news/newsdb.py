"""The news database for poller.py and wxall.py: Postgres (PGDATABASE, the default — boxes) or one
SQLite file (NEWS_SQLITE=<path> — search.logout.cloud, which retires its Postgres, ADR-063).

The callers write their SQL once in Postgres' parameter style (%s, %(name)s) with no server-side
clock: every timestamp is a parameter made by ts(). On SQLite the parameters are rewritten to ? and
:name, timestamps are stored as Unix seconds (REAL), lists and dicts as JSON text, and the
headline search is an FTS5 table (schema-sqlite.sql). Two writers (the poller's threads and
wxall) share the file: WAL, and a busy wait instead of an error while the other one commits.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path

SQLITE_PATH = os.environ.get("NEWS_SQLITE", "")
SQLITE_SCHEMA = Path(__file__).with_name("schema-sqlite.sql")
BUSY_MS = 15_000

_NAMED = re.compile(r"%\((\w+)\)s")


def is_sqlite() -> bool:
    return bool(SQLITE_PATH)


def now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def ts(t: dt.datetime | None):
    """A timestamp parameter: the datetime for Postgres, Unix seconds for SQLite."""
    if t is None or not is_sqlite():
        return t
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.timezone.utc)
    return t.timestamp()


def sqlite_sql(sql: str) -> str:
    """Postgres parameter style -> SQLite's (%(name)s -> :name, %s -> ?)."""
    return _NAMED.sub(r":\1", sql).replace("%s", "?")


def _param(v):
    if isinstance(v, (list, tuple, dict)):
        return json.dumps(v)
    if isinstance(v, dt.datetime):
        return ts(v)
    return v


def _params(p):
    if p is None:
        return ()
    if isinstance(p, dict):
        return {k: _param(v) for k, v in p.items()}
    return tuple(_param(v) for v in p)


class SqliteConn:
    """The few psycopg connection calls the writers use, on one SQLite file."""

    def __init__(self, path: str):
        self.db = sqlite3.connect(path, timeout=BUSY_MS / 1000, isolation_level=None, check_same_thread=False)
        self.db.execute(f"PRAGMA busy_timeout = {BUSY_MS}")
        self.db.execute("PRAGMA journal_mode = WAL")
        self.db.execute("PRAGMA synchronous = NORMAL")    # WAL: a crash loses the last commits, never the file
        self.db.execute("PRAGMA foreign_keys = ON")       # items go with their feed (ON DELETE CASCADE)

    def execute(self, sql: str, params=None):
        return self.db.execute(sqlite_sql(sql), _params(params))

    def executemany(self, sql: str, rows):
        return self.db.executemany(sqlite_sql(sql), (_params(r) for r in rows))

    def executescript(self, script: str):
        return self.db.executescript(script)

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")                # take the write lock now, not mid-way
        try:
            yield self
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        self.db.execute("COMMIT")

    def close(self):
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class PgConn:
    """psycopg with the same small surface (executemany/executescript)."""

    def __init__(self, conn):
        self.conn = conn

    def execute(self, sql: str, params=None):
        return self.conn.execute(sql, params)

    def executemany(self, sql: str, rows):
        with self.conn.cursor() as cur:
            cur.executemany(sql, list(rows))

    def executescript(self, script: str):
        return self.conn.execute(script)

    def transaction(self):
        return self.conn.transaction()

    def close(self):
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def connect(**kw):
    if is_sqlite():
        return SqliteConn(SQLITE_PATH)
    import psycopg
    return PgConn(psycopg.connect(autocommit=True, **kw))


def ensure_sqlite_schema() -> None:
    """Create the news file's tables (both writers call it; every statement is IF NOT EXISTS)."""
    os.makedirs(os.path.dirname(os.path.abspath(SQLITE_PATH)), exist_ok=True)
    with SqliteConn(SQLITE_PATH) as c:
        c.executescript(SQLITE_SCHEMA.read_text())
