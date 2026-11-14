"""Copy the news database (PG* env, Postgres) into the news file (NEWS_SQLITE) once, so a host that
switches to SQLite (search.logout.cloud, ADR-063) keeps its week of headlines and its weather
instead of starting empty. Run it with the pollers stopped; it refuses a file that already holds
items unless --force, and checks every table's row count after the copy.

    docker compose run --rm -e NEWS_SQLITE=/news/news.sqlite news python /app/pg_to_sqlite.py
"""
from __future__ import annotations

import datetime as dt
import json
import sys

import newsdb

# table -> (columns, the ones that are timestamps, the ones that are jsonb/arrays)
TABLES = {
    "feeds": (("url", "section", "name", "lean", "kind", "poll_min", "resolved_url", "etag", "modified", "next_due",
               "last_ok", "last_status", "last_error", "fails", "fails_4xx", "idle_polls", "items_total", "disabled"),
              {"next_due", "last_ok"}, set()),
    "items": (("feed_url", "guid", "url", "title", "summary", "author", "published", "fetched"),
              {"published", "fetched"}, set()),
    "wx_towns": (("key", "name", "state", "lat", "lon", "pop", "counties", "tz", "kind", "county"), set(), {"counties"}),
    "wx_stations": (("id", "lat", "lon"), set(), set()),
    "wx_obs": (("id", "obs", "at"), {"at"}, {"obs"}),
    "wx_fc": (("id", "hours", "days", "cycle", "days_cycle"), {"cycle", "days_cycle"}, {"hours", "days"}),
    "wx_alerts": (("id", "event", "severity", "headline", "ends", "same", "poly"), set(), {"same", "poly"}),
    "games": (("league", "game_id", "start_utc", "state", "detail", "away_name", "away_nick", "away_abbr", "away_score",
               "home_name", "home_nick", "home_abbr", "home_score", "away_keys", "home_keys", "venue", "broadcasts",
               "url", "updated"), {"start_utc", "updated"}, {"away_keys", "home_keys", "broadcasts"}),
    "teams": (("league", "name", "nick", "keys", "updated"), {"updated"}, {"keys"}),
    "weather": (("place", "payload", "source", "updated"), {"updated"}, {"payload"}),
}


def convert(cols, times, blobs, row):
    out = []
    for c, v in zip(cols, row):
        if v is not None and c in times:
            v = v.timestamp() if isinstance(v, dt.datetime) else v
        elif v is not None and c in blobs:
            v = json.dumps(v)
        elif isinstance(v, bool):
            v = int(v)
        out.append(v)
    return out


def main(force: bool = False) -> int:
    import psycopg
    if not newsdb.is_sqlite():
        sys.exit("NEWS_SQLITE=<file> is required")
    newsdb.ensure_sqlite_schema()
    lite = newsdb.SqliteConn(newsdb.SQLITE_PATH)
    have = lite.execute("SELECT count(*) FROM items").fetchone()[0]
    if have and not force:
        sys.exit(f"{newsdb.SQLITE_PATH} already holds {have} items; --force replaces its contents")
    counts = {}
    with psycopg.connect() as pg:
        present = {r[0] for r in pg.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")}
        with lite.transaction():
            for t in reversed(list(TABLES)):                     # items before feeds (foreign key)
                lite.execute(f"DELETE FROM {t}")
            for t, (cols, times, blobs) in TABLES.items():
                if t not in present:
                    print(f"{t}: not in Postgres, skipped")
                    continue
                rows = pg.execute(f"SELECT {', '.join(cols)} FROM {t}").fetchall()
                lite.db.executemany(f"INSERT INTO {t} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                                    [convert(cols, times, blobs, r) for r in rows])
                counts[t] = len(rows)
    bad = 0
    for t, n in counts.items():
        got = lite.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
        print(f"{t}: {n} -> {got}" + ("" if got == n else "  MISMATCH"))
        bad += got != n
    fts = lite.execute("SELECT count(*) FROM items_fts_docsize").fetchone()[0]   # rows actually indexed
    print(f"items_fts: {fts}")
    lite.close()
    return 1 if bad or fts != counts.get("items", 0) else 0


if __name__ == "__main__":
    sys.exit(main(force="--force" in sys.argv))
