"""How many readers reached each English Wikipedia article FROM A SEARCH ENGINE, per month —
Wikimedia's clickstream dump (CC0). The closest open measure of which topics people
actually search for: part of the big query list and a popularity signal.

The dump: other/clickstream/YYYY-MM/clickstream-enwiki-YYYY-MM.tsv.gz (~0.5 GB), lines
"prev <TAB> curr <TAB> type <TAB> n"; prev "other-search" = arrived from a search engine.
One archive download from dumps.wikimedia.org, streamed.

The same dump's "link" rows count reader clicks from one article to another: what
readers of Paul Rudd open next (Ant-Man, Clueless) — search-api's related articles
("better and more intricate results").

    search_import.py clickstream [--month 2026-08]   -> wiki_search_refs (title, searches),
                                                        wiki_next (title, next_title, clicks)
"""

from __future__ import annotations

import gzip
import time
import urllib.request

URL = "https://dumps.wikimedia.org/other/clickstream/{ym}/clickstream-enwiki-{ym}.tsv.gz"
USER_AGENT = "logout-search-import/1 (personal search index)"


def log(msg: str) -> None:
    print(f"[clickstream] {msg}", flush=True)


def parse_line(line: str) -> tuple[str, int] | None:
    """'other-search\\tCostco\\texternal\\t68489' -> ('Costco', 68489); other referrers -> None."""
    p = line.rstrip("\n").split("\t")
    if len(p) != 4 or p[0] != "other-search" or not p[3].isdigit():
        return None
    title = p[1].replace("_", " ").strip()
    if not title or title == "Main Page":
        return None
    return title, int(p[3])


NEXT_MIN = 50       # clicks a month; Wikimedia already drops pairs under 10
NEXT_N = 8          # kept per article
_NOT_ARTICLE = ("List of ", "Lists of ", "Category:", "Wikipedia:", "Portal:", "Help:", "File:", "Template:")


def parse_link(line: str) -> tuple[str, str, int] | None:
    """'Paul_Rudd\tAnt-Man_(film)\tlink\t5123' -> ('Paul Rudd', 'Ant-Man (film)', 5123) when
    readers clicked often enough; anything else -> None."""
    p = line.rstrip("\n").split("\t")
    if len(p) != 4 or p[2] != "link" or not p[3].isdigit() or int(p[3]) < NEXT_MIN:
        return None
    a, b = p[0].replace("_", " ").strip(), p[1].replace("_", " ").strip()
    if not a or not b or a == b or "Main Page" in (a, b) or b.startswith(_NOT_ARTICLE):
        return None
    return a, b, int(p[3])


def cmd_clickstream(conn, month: str = "2026-08") -> None:
    t0 = time.time()
    req = urllib.request.Request(URL.format(ym=month), headers={"User-Agent": USER_AGENT})
    totals: dict[str, int] = {}
    n = links = 0
    conn.execute("DROP TABLE IF EXISTS wiki_next_stage")
    conn.execute("CREATE TABLE wiki_next_stage (title text, next_title text, clicks integer)")
    # Nothing else runs on this connection while the COPY is open (one statement at a time).
    with urllib.request.urlopen(req, timeout=300) as r, gzip.GzipFile(fileobj=r) as gz, \
            conn.transaction(), conn.cursor().copy("COPY wiki_next_stage FROM STDIN") as cp:
        for raw in gz:
            n += 1
            line = raw.decode("utf-8", "replace")
            got = parse_line(line)
            if got:
                totals[got[0]] = totals.get(got[0], 0) + got[1]
                continue
            nxt = parse_link(line)
            if nxt:
                cp.write_row(nxt)
                links += 1
    with conn.transaction():
        conn.execute("DROP TABLE IF EXISTS wiki_next")
        conn.execute(f"""CREATE TABLE wiki_next AS
            SELECT title, next_title, clicks FROM (
                SELECT title, next_title, sum(clicks)::int AS clicks,
                       row_number() OVER (PARTITION BY title ORDER BY sum(clicks) DESC, next_title) AS rn
                FROM wiki_next_stage GROUP BY title, next_title) x
            WHERE rn <= {NEXT_N}""")
        conn.execute("CREATE INDEX wiki_next_title ON wiki_next (title)")
        conn.execute("DROP TABLE wiki_next_stage")
    log(f"{month}: {links:,} article-to-article pairs with {NEXT_MIN}+ clicks -> wiki_next (top {NEXT_N} each)")
    log(f"{month}: {len(totals):,} articles reached from search engines ({n:,} lines) in {(time.time() - t0) / 60:.1f} min")
    with conn.transaction():
        conn.execute("DROP TABLE IF EXISTS wiki_search_refs")
        conn.execute("CREATE TABLE wiki_search_refs (title text PRIMARY KEY, searches integer)")
        with conn.cursor().copy("COPY wiki_search_refs (title, searches) FROM STDIN") as cp:
            for title, s in totals.items():
                cp.write_row((title, s))
    top = conn.execute("SELECT title, searches FROM wiki_search_refs ORDER BY searches DESC LIMIT 5").fetchall()
    log(f"wiki_search_refs ready; most searched: {top}")
