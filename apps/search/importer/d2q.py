"""Queries written for each site by a language model (doc2query, 2026-09-29).

For each homepage a model wrote ~15 searches a person might type to find that site
without knowing its name: what it is, what task it solves, what people call such a
site. They were written on the dev workstation from indexhost's crawl; only the queries
come here (ADR-059: no page text ships).

    search_import.py d2q-load --file queries.json   {domain: [query, ...]} -> d2q_docs
"""

from __future__ import annotations

import json
import sys
import time


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] d2q: {msg}", file=sys.stderr, flush=True)


def clean(queries: list) -> list[str]:
    """Lowercase, 1-7 words, de-duplicated, order kept."""
    out, seen = [], set()
    for q in queries:
        q = " ".join(str(q).lower().split())
        if q and len(q.split()) <= 7 and q not in seen:
            seen.add(q)
            out.append(q)
    return out


def cmd_load(conn, path: str) -> None:
    """{domain: [queries]} -> d2q_docs (domain, body) with a BM25 index: search-api's d2q list.
    Only domains present in docs are kept."""
    t0 = time.time()
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    conn.execute("DROP TABLE IF EXISTS d2q_new")
    conn.execute("CREATE TABLE d2q_new (domain text, body text)")
    n = 0
    with conn.transaction(), conn.cursor().copy("COPY d2q_new (domain, body) FROM STDIN") as cp:
        for domain, queries in data.items():
            qs = clean(queries)
            if qs:
                cp.write_row((domain.lower(), " . ".join(qs)))
                n += 1
    with conn.transaction():
        conn.execute("DROP TABLE IF EXISTS d2q_docs")
        conn.execute("""CREATE TABLE d2q_docs AS SELECT DISTINCT ON (n.domain) n.domain, n.body
                        FROM d2q_new n JOIN docs d ON d.domain = n.domain AND d.error IS NULL""")
        conn.execute("DROP TABLE d2q_new")
        conn.execute("ALTER TABLE d2q_docs ADD PRIMARY KEY (domain)")
        conn.execute("CREATE INDEX d2q_docs_bm25 ON d2q_docs USING bm25 (body) WITH (text_config = 'english')")
    k = conn.execute("SELECT count(*) FROM d2q_docs").fetchone()[0]
    log(f"{n:,} sites in the file -> {k:,} in d2q_docs in {time.time() - t0:.0f} s")
