"""The junk filter's table (search-api drop_junk, 2026-09-30): site_quality, from two sources.

  jev   legacy homepage scores (utility/search-site-quality.py, 283k sites; private file
        site_quality.sql): junk = the conservative v4 rule already in that file
        (unsafe >= 0.85 and useful <= 0.6 on a titled capture; top 2,000 exempt below 0.95).
  ut1   UT1's unsafe lists (Université Toulouse Capitole, CC BY-SA 4.0; site-directories2.sh
        -> kinds-unsafe/ut1-unsafe.tsv.gz): adult, malware, phishing, gambling, cryptojacking,
        warez, stalkerware, ddos, hate. NOT advertising, fake news or drugs — too broad to hide
        search results on (ad networks, news outlets, harm-reduction sites).
Only domains the index can return (a homepage, a bare link or an article's official site) are
kept, so search-api's in-memory set stays small (UT1 alone is 5.1M domains). A site the query
names is never hidden (search_api.query_names_domain).

    search_import.py junk-load --quality /import/site_quality.sql --ut1 /import/kinds-unsafe/ut1-unsafe.tsv.gz
"""
from __future__ import annotations

import gzip
import os
import time

UT1_JUNK = frozenset("adult malware phishing gambling cryptojacking warez stalkerware ddos hate/aggressive".split())


def log(msg: str) -> None:
    print(f"[junk] {msg}", flush=True)


def cmd_junk_load(conn, quality: str | None, ut1: str | None) -> None:
    t0 = time.time()
    conn.execute("DROP TABLE IF EXISTS site_quality_new")
    conn.execute("CREATE TABLE site_quality_new (domain text PRIMARY KEY, useful real, parked real, spam real, "
                 "unsafe real, junk boolean NOT NULL, source text, category text)")
    conn.execute("DROP TABLE IF EXISTS junk_servable")
    conn.execute("CREATE TABLE junk_servable AS SELECT domain FROM docs UNION SELECT domain FROM nav_sites "
                 "UNION SELECT domain FROM wiki_sites")
    conn.execute("CREATE UNIQUE INDEX ON junk_servable (domain)")
    if quality and os.path.exists(quality):
        n, seen = 0, set()
        with open(quality, encoding="utf-8") as f, \
                conn.cursor().copy("COPY site_quality_new (domain, useful, parked, spam, unsafe, junk, source) FROM STDIN") as cp:
            inside = False
            for line in f:
                if line.startswith("COPY site_quality FROM STDIN"):
                    inside = True
                    continue
                if inside:
                    if line.startswith("\\."):
                        break
                    p = line.rstrip("\n").split("\t")
                    if len(p) >= 6 and p[0] not in seen:     # the file repeats a few domains
                        seen.add(p[0])
                        cp.write_row((p[0], p[1], p[2], p[3], p[4], p[5] == "t", "jev"))
                        n += 1
        log(f"jev: {n} legacy homepage rows from {quality}")
    if ut1 and os.path.exists(ut1):
        conn.execute("DROP TABLE IF EXISTS junk_ut1")
        conn.execute("CREATE TABLE junk_ut1 (domain text, category text)")
        n = 0
        with gzip.open(ut1, "rt", encoding="utf-8", errors="replace") as f, \
                conn.cursor().copy("COPY junk_ut1 FROM STDIN") as cp:
            for line in f:
                p = line.rstrip("\n").split("\t")
                if len(p) >= 3 and p[2] in UT1_JUNK and p[0]:
                    cp.write_row((p[0].lower().removeprefix("www."), p[2]))
                    n += 1
        log(f"ut1: {n} unsafe-list rows read")
        conn.execute("""INSERT INTO site_quality_new (domain, junk, source, category)
            SELECT DISTINCT ON (u.domain) u.domain, true, 'ut1', u.category
            FROM junk_ut1 u JOIN junk_servable s USING (domain) ORDER BY u.domain, u.category
            ON CONFLICT (domain) DO UPDATE SET junk = true, source = site_quality_new.source || '+ut1',
                                               category = EXCLUDED.category""")
        conn.execute("DROP TABLE junk_ut1")
    conn.execute("DROP TABLE junk_servable")
    conn.execute("DROP TABLE IF EXISTS site_quality")
    conn.execute("ALTER TABLE site_quality_new RENAME TO site_quality")
    for src, n, j in conn.execute("SELECT source, count(*), count(*) FILTER (WHERE junk) FROM site_quality GROUP BY 1"):
        log(f"site_quality {src}: {n} rows, {j} junk")
    log(f"done in {time.time() - t0:.0f} s")
