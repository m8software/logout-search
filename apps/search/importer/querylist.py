"""A giant synthetic query list from legally usable sources only ("can you
synthesize a giant query list … one that is usable for us though legally").

    query, kind, source, weight, license — one row per (query, kind, source)

  entities (Wikidata, CC0)          name forms per category ("{company} stock", "{chain} near
                                    me", "{game} walkthrough"), plus English aliases;
                                    weight = Wikipedia language editions
  wiki_search_refs (clickstream, CC0) article titles people reach from search engines;
                                    weight = monthly search referrals
  AmazonQAC (CDLA-Permissive-2.0)   real shopping searches; weight = times searched (--qac)
  typos (ours)                      one realistic misspelling of the top names

Weights are only comparable within a source. Used for eval sampling, coverage checks and
completion — never shipped raw without the licence column honoured.

    search_import.py query-list [--qac FILE] [--typos N]   -> query_list
"""

from __future__ import annotations

import gzip
import random
import re
import time

TEMPLATES = {
    "public_company": ["{n}", "{n} stock", "{n} earnings"],
    "pro_athlete": ["{n}", "{n} stats"],
    "food": ["{n}", "{n} recipe"],
    "drink": ["{n}", "{n} recipe"],
    "restaurant_chain": ["{n}", "{n} near me", "{n} menu"],
    "celebrity": ["{n}", "{n} age"],
    "video_game": ["{n}", "{n} walkthrough", "{n} release date"],
    "country": ["{n}", "{n} capital", "{n} population"],
    "state": ["{n}", "{n} capital"],
    "vehicle_model": ["{n}", "{n} reviews", "{n} price"],
    "motorcycle_model": ["{n}", "{n} reviews"],
    "musician": ["{n}", "{n} songs", "{n} tour"],
    "band": ["{n}", "{n} songs", "{n} tour"],
    "news_org": ["{n}", "{n} news"],
}
_PAREN = re.compile(r"\s*\([^)]*\)$")
_TICKER = re.compile(r"^[A-Z][A-Z.]{0,5}$")


def log(msg: str) -> None:
    print(f"[query-list] {msg}", flush=True)


def clean(name: str) -> str:
    """'Nirvana (band)' -> 'nirvana'; whitespace collapsed; lowercase."""
    return " ".join(_PAREN.sub("", name).split()).lower()


def entity_queries(category: str, name: str, extra: str) -> list[str]:
    n = clean(name)
    if not (2 <= len(n) <= 80):
        return []
    out = [t.format(n=n) for t in TEMPLATES.get(category, ["{n}"])]
    if category == "public_company" and extra and _TICKER.match(extra):
        out.append(f"{extra.lower()} stock")
    return out


def typo(q: str, rnd: random.Random) -> str | None:
    """One keyboard slip — swap, drop or double a letter inside a word of 5+ letters."""
    words = q.split()
    idx = [i for i, w in enumerate(words) if len(w) >= 5 and w.isalpha()]
    if not idx:
        return None
    i = rnd.choice(idx)
    w = list(words[i])
    j = rnd.randrange(1, len(w) - 1)
    op = rnd.choice(("swap", "drop", "double"))
    if op == "swap":
        w[j], w[j + 1] = w[j + 1], w[j]
    elif op == "drop":
        del w[j]
    else:
        w.insert(j, w[j])
    words[i] = "".join(w)
    t = " ".join(words)
    return t if t != q else None


def cmd_query_list(conn, qac: str | None = None, typos: int = 50_000, qac_min: int = 20) -> None:
    t0 = time.time()
    conn.execute("DROP TABLE IF EXISTS query_list_new")
    conn.execute("CREATE TABLE query_list_new (query text, kind text, source text, weight double precision, license text)")
    # Read first, then copy: iterating a query on the same connection while a COPY is open
    # stalled the first build for 42 minutes (one statement at a time per connection).
    ents = conn.execute("SELECT category, name, aliases, sitelinks, extra FROM entities").fetchall()
    with conn.transaction(), conn.cursor().copy("COPY query_list_new FROM STDIN") as cp:
        n = 0
        for cat, name, aliases, sl, extra in ents:
            for q in entity_queries(cat, name, extra or ""):
                cp.write_row((q, cat, "wikidata", float(sl or 0), "CC0"))
                n += 1
            for a in (aliases or [])[:5]:
                a = clean(a)
                if 2 <= len(a) <= 60:
                    cp.write_row((a, cat, "wikidata-alias", 0.3 * float(sl or 0), "CC0"))
                    n += 1
        log(f"entities: {n:,} rows")
    if conn.execute("SELECT to_regclass('wiki_search_refs')").fetchone()[0]:
        conn.execute(r"""INSERT INTO query_list_new
            SELECT lower(regexp_replace(title, '\s*\([^)]*\)$', '')), 'topic', 'wikipedia-clickstream', searches, 'CC0'
            FROM wiki_search_refs WHERE searches >= 10 AND length(title) BETWEEN 2 AND 80
              AND title !~ '^(List of|Category:|File:|Template:|Wikipedia:|Help:|Portal:)'""")
    if qac:
        with conn.transaction(), conn.cursor().copy("COPY query_list_new FROM STDIN") as cp:
            k = 0
            with gzip.open(qac, "rt", encoding="utf-8", errors="replace") as f:
                for line in f:
                    p = line.rstrip("\n").split("\t")
                    # 31.8M terms were searched twice or more; a list of the searched-often
                    # is what completion and the eval need
                    if len(p) == 2 and p[1].isdigit() and int(p[1]) >= qac_min and 2 <= len(p[0]) <= 120:
                        cp.write_row((p[0].replace("\x00", ""), "shop", "amazonqac", float(p[1]), "CDLA-Permissive-2.0"))
                        k += 1
        log(f"AmazonQAC: {k:,} terms")
    if typos:
        rnd = random.Random(2026)
        rows = conn.execute("""SELECT query, weight FROM query_list_new WHERE source = 'wikidata'
                               AND query !~ '\\.' ORDER BY weight DESC LIMIT %s""", (typos,)).fetchall()
        with conn.transaction(), conn.cursor().copy("COPY query_list_new FROM STDIN") as cp:
            for q, w in rows:
                t = typo(q, rnd)
                if t:
                    cp.write_row((t, "typo", "generated", w, "ours"))
    with conn.transaction():
        conn.execute("DROP TABLE IF EXISTS query_list")
        conn.execute("""CREATE TABLE query_list AS
            SELECT query, kind, source, max(weight) AS weight, min(license) AS license
            FROM query_list_new WHERE query <> '' GROUP BY query, kind, source""")
        conn.execute("DROP TABLE query_list_new")
        conn.execute("CREATE INDEX query_list_kind ON query_list (kind, weight DESC)")
    stats = conn.execute("SELECT source, count(*), count(DISTINCT query) FROM query_list GROUP BY 1 ORDER BY 2 DESC").fetchall()
    total = conn.execute("SELECT count(DISTINCT query) FROM query_list").fetchone()[0]
    log(f"query_list: {total:,} distinct queries in {(time.time() - t0) / 60:.1f} min; by source: {stats}")
