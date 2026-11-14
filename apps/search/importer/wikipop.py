"""How well known each English Wikipedia article is (step 1, 2026-09-24).

Prefix completion has to rank the 9,183 titles that start with "lin"; nothing in
wiki_docs says which one people mean. Wikidata's sitelink count — how many Wikipedia
language editions have an article about the item — is a free, CC0 proxy for fame:
Linux 250+, a minor village 3. One paged, count-checked QLever read (wikilinks.fetch).

    search_import.py wiki-pop   -> wiki_pop (page_id, sitelinks)
"""

from __future__ import annotations

import io
import time
import urllib.parse

from wikilinks import PAGE, complete, fetch
from youtube import lit

_WIKI = "https://en.wikipedia.org/wiki/"

QUERY = """PREFIX schema: <http://schema.org/>
PREFIX wikibase: <http://wikiba.se/ontology#>
SELECT ?article ?n WHERE {
  ?article schema:about ?item ; schema:isPartOf <https://en.wikipedia.org/> .
  ?item wikibase:sitelinks ?n .
}"""
COUNT = """PREFIX schema: <http://schema.org/>
PREFIX wikibase: <http://wikiba.se/ontology#>
SELECT (COUNT(*) AS ?c) WHERE {
  ?article schema:about ?item ; schema:isPartOf <https://en.wikipedia.org/> .
  ?item wikibase:sitelinks ?n .
}"""


def page_query(page: int) -> str:
    return QUERY + f"\nORDER BY ?article LIMIT {PAGE} OFFSET {page * PAGE}"


def parse_row(line: str) -> tuple[str, int] | None:
    """'<https://en.wikipedia.org/wiki/Linux>\\t"251"^^<…int>' -> ("Linux", 251)."""
    p = line.rstrip("\n").split("\t")
    if len(p) < 2:
        return None
    iri = p[0].strip().strip("<>")
    if not iri.startswith(_WIKI):
        return None                                        # also the "?article" header
    try:
        n = int(float(lit(p[1])))
    except ValueError:
        return None
    return urllib.parse.unquote(iri[len(_WIKI):]).replace("_", " "), n


def log(msg: str) -> None:
    print(f"[wiki-pop] {msg}", flush=True)


def cmd_wiki_pop(conn) -> None:
    t0 = time.time()
    with fetch("wiki-pop", COUNT) as resp:
        expected = int(lit(resp.read().decode().splitlines()[1]))
    conn.execute("CREATE TEMP TABLE wp (title text, sitelinks integer)")
    seen = n = page = 0
    while True:
        got = 0
        with fetch("wiki-pop", page_query(page)) as resp, conn.transaction(), \
                conn.cursor().copy("COPY wp (title, sitelinks) FROM STDIN") as cp:
            for line in io.TextIOWrapper(resp, encoding="utf-8", errors="replace"):
                if line.startswith("?"):
                    continue
                got += 1
                r = parse_row(line)
                if r:
                    cp.write_row(r)
                    n += 1
        seen += got
        log(f"page {page}: {got:,} rows")
        if got < PAGE:
            break
        page += 1
        time.sleep(5)                                      # one query at a time, spaced out
    if not complete(seen, expected):
        raise RuntimeError(f"QLever sent {seen:,} of {expected:,} rows — not replacing wiki_pop")
    with conn.transaction():
        conn.execute("DROP TABLE IF EXISTS wiki_pop")
        conn.execute("CREATE TABLE wiki_pop AS SELECT DISTINCT ON (w.page_id) w.page_id, wp.sitelinks "
                     "FROM wp JOIN wiki_docs w ON w.title = wp.title ORDER BY w.page_id, wp.sitelinks DESC")
        conn.execute("ALTER TABLE wiki_pop ADD PRIMARY KEY (page_id)")
    k = conn.execute("SELECT count(*), count(*) FILTER (WHERE sitelinks >= 10), "
                     "count(*) FILTER (WHERE sitelinks >= 30) FROM wiki_pop").fetchone()
    log(f"{seen:,} of {expected:,} from QLever; {k[0]:,} articles matched ({k[1]:,} in 10+ languages, "
        f"{k[2]:,} in 30+) in {(time.time() - t0) / 60:.1f} min")
