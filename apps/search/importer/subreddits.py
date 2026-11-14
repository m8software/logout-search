"""Subreddit directory (2026-09-24): "r/homelab", "homelab subreddit",
"3d printing reddit" answered locally.

Input: subreddits.tsv.gz from infra/build-aws/jobs/reddit-subs.sh (Arctic Shift's
2025-01 subreddit metadata, filtered on the build instance: public, not NSFW or
quarantined, >= 10 members). Columns: name, title, description, subscribers,
created_utc, lang. Metadata only; personal use, never shipped.

    search_import.py subreddits --file /import/subreddits.tsv.gz  -> subreddits
"""

from __future__ import annotations

import gzip
import re
import time

_KEY = re.compile(r"[^a-z0-9]")


def norm(name: str) -> str:
    """r/3Dprinting, 3d printing, 3D-Printing -> 3dprinting (the lookup key)."""
    return _KEY.sub("", name.lower())


def rows(path: str):
    with gzip.open(path, "rt", encoding="utf-8", errors="replace") as f:
        for line in f:
            p = line.rstrip("\n").split("\t")
            if len(p) < 4 or not p[0] or not p[3].isdigit():
                continue
            name = p[0].strip()
            yield (name, norm(name), p[1][:300], p[2][:1000], int(p[3]),
                   int(p[4]) if len(p) > 4 and p[4].isdigit() else None, (p[5] if len(p) > 5 else "")[:10])


def cmd_subreddits(conn, path: str) -> None:
    t0 = time.time()
    conn.execute("DROP TABLE IF EXISTS subreddits_new")
    conn.execute("CREATE TABLE subreddits_new (name text, key text, title text, description text, "
                 "subscribers integer, created_utc bigint, lang text)")
    n = 0
    with conn.transaction(), conn.cursor().copy(
            "COPY subreddits_new (name, key, title, description, subscribers, created_utc, lang) FROM STDIN") as cp:
        for r in rows(path):
            cp.write_row(tuple(v.replace("\x00", "") if isinstance(v, str) else v for v in r))
            n += 1
    with conn.transaction():
        # One row per key: the biggest community wins a name collision.
        conn.execute("DELETE FROM subreddits_new a USING subreddits_new b WHERE a.key = b.key "
                     "AND (a.subscribers, a.name) < (b.subscribers, b.name)")
        conn.execute("ALTER TABLE subreddits_new ADD COLUMN search_text text GENERATED ALWAYS AS ("
                     "name || ' ' || coalesce(title, '') || ' ' || coalesce(description, '')) STORED")
        conn.execute("DROP TABLE IF EXISTS subreddits")
        conn.execute("ALTER TABLE subreddits_new RENAME TO subreddits")
        conn.execute("ALTER TABLE subreddits ADD PRIMARY KEY (key)")
        conn.execute("CREATE INDEX subreddits_bm25 ON subreddits USING bm25 (search_text) WITH (text_config = 'english')")
    k = conn.execute("SELECT count(*), count(*) FILTER (WHERE subscribers >= 10000) FROM subreddits").fetchone()
    print(f"[subreddits] {k[0]:,} subreddits ({k[1]:,} with 10k+ members) from {n:,} rows in "
          f"{(time.time() - t0) / 60:.1f} min", flush=True)
