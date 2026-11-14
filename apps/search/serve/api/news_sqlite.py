"""The news file (apps/search/news/schema-sqlite.sql) as a stand-in for the Postgres news database
(SEARCH_NEWS_BACKEND=sqlite, SEARCH_NEWS_SQLITE=<path>; search.logout.cloud, ADR-063).

search_api keeps its Postgres SQL; each news/weather statement is registered here with its SQLite
form, a parameter step and a row step, and the connection answers execute(<the Postgres SQL>) with
rows of the same Python types psycopg gives (datetimes, lists, dicts). A statement that is not
registered (the home forecast, the scoreboard — boxes only) raises NewsError like a missing table
does on Postgres, so the callers' existing error paths apply.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import re
import sqlite3
import time

import psycopg


class NewsError(psycopg.Error):
    """Caught where psycopg.Error is: the news box then shows nothing, as with Postgres down."""


_QUERIES: dict[str, tuple[str, object, object]] = {}


def register(pg_sql: str, sqlite_sql: str, params=None, row=None) -> None:
    _QUERIES[pg_sql] = (sqlite_sql, params, row)


def epoch_dt(v):
    return None if v is None else dt.datetime.fromtimestamp(float(v), dt.timezone.utc)


def from_json(v):
    return None if v is None else json.loads(v)


def row_with(**cols):
    """A row step that converts columns by position: row_with(**{"3": epoch_dt})."""
    conv = {int(k): f for k, f in cols.items()}
    return lambda r: tuple(conv[i](v) if i in conv else v for i, v in enumerate(r))


_WORD = re.compile(r"\w+", re.UNICODE)


def fts_query(q: str, stop: frozenset = frozenset()) -> str | None:
    """A search box query -> an FTS5 MATCH string: every word that is not a stop word, quoted
    (no FTS5 syntax gets through), any of them (BM25 ranks the ones with more); None when no
    word is left."""
    words = [w for w in dict.fromkeys(_WORD.findall(q.lower())) if w not in stop]
    return " OR ".join('"' + w.replace('"', "") + '"' for w in words) or None


class _Cursor:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def __iter__(self):
        return iter(self._rows)


class NewsSqlite:
    """psycopg's connection surface as search_api's pools use it (execute, closed, broken, close)."""

    def __init__(self, path: str, timeout_ms: int = 1500):
        try:
            self.db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=timeout_ms / 1000,
                                      check_same_thread=False)
        except sqlite3.Error as e:
            raise NewsError(f"news file {path}: {e}") from e
        self.db.create_function("now_s", 0, time.time)
        self.db.create_function("power", 2, lambda a, b: None if a is None or b is None else math.pow(a, b), deterministic=True)
        self.db.create_function("cos", 1, lambda a: None if a is None else math.cos(a), deterministic=True)
        self.db.create_function("radians", 1, lambda a: None if a is None else math.radians(a), deterministic=True)
        self.closed = False
        self.broken = False

    def execute(self, sql: str, params=None):
        q = _QUERIES.get(sql)
        if q is None:
            raise NewsError("statement not available on the news file")
        lite, pstep, rstep = q
        p = dict(params or {})
        if pstep:
            p = pstep(p)
            if p is None:                       # nothing to look for (a query of stop words only)
                return _Cursor([])
        p = {k: json.dumps(v) if isinstance(v, (list, tuple)) else v for k, v in p.items()}
        try:
            rows = self.db.execute(lite, p).fetchall()
        except sqlite3.Error as e:
            self.broken = True
            raise NewsError(str(e)) from e
        return _Cursor([rstep(r) for r in rows] if rstep else rows)

    def close(self):
        self.closed = True
        self.db.close()
