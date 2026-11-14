"""Facts about the things English Wikipedia has an article for, from Wikidata (CC0) —
what the answer box says for "how old is jennifer aniston", "how tall is tom hanks",
"when was costco founded", "population of new jersey" ("I'd like to be
able to do things like how old is Jennifer aniston how tall is dean withers").

One QLever query per fact, paged and checked against a COUNT like wikilinks.py; the table
is swapped in only when every fact loaded. Keyed by the article's title, so search-api
answers only for a name it already resolved to that article.

    search_import.py facts [--facts a,b]   -> wiki_facts (title, fact, value, unit, precision, at)

value is an ISO date (dates), a number (quantities) or a label (people, places);
precision is Wikidata's (11 = day, 10 = month, 9 = year); at is a quantity's "point in
time" — the loader keeps the latest population, not every census.
"""

from __future__ import annotations

import io
import re
import time
import urllib.parse

from wikilinks import PAGE, complete, fetch
from youtube import lit

_WIKI = "https://en.wikipedia.org/wiki/"

PREFIX = """PREFIX wd: <http://www.wikidata.org/entity/>
PREFIX wdt: <http://www.wikidata.org/prop/direct/>
PREFIX p: <http://www.wikidata.org/prop/>
PREFIX ps: <http://www.wikidata.org/prop/statement/>
PREFIX psv: <http://www.wikidata.org/prop/statement/value/>
PREFIX pq: <http://www.wikidata.org/prop/qualifier/>
PREFIX wikibase: <http://wikiba.se/ontology#>
PREFIX schema: <http://schema.org/>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
"""
_ARTICLE = "?article schema:about ?item ; schema:isPartOf <https://en.wikipedia.org/> ."

# fact -> (kind, Wikidata property). kind: date | quantity | item
FACTS = {
    "born":        ("date", "P569"),
    "died":        ("date", "P570"),
    "height":      ("quantity", "P2048"),
    # a mountain, a town, a building's site: "how tall is mount everest" has no P2048 (2026-10-10)
    "elevation":   ("quantity", "P2044"),
    "birthplace":  ("item", "P19"),
    "deathplace":  ("item", "P20"),
    "spouse":      ("item", "P26"),
    "founded":     ("date", "P571"),
    "founder":     ("item", "P112"),
    "ceo":         ("item", "P169"),
    "headquarters": ("item", "P159"),
    "population":  ("quantity", "P1082"),
    "capital":     ("item", "P36"),
    "area":        ("quantity", "P2046"),
    "released":    ("date", "P577"),
    "director":    ("item", "P57"),
    "author":      ("item", "P50"),
    # which league a team plays in: search-api puts a major-league team first for its
    # nickname ("devils" -> New Jersey Devils)
    "league":      ("item", "P118"),
}

# No statement-rank filter: on QLever it cut 2.06M birth dates to 68,864 (2026-09-25). Names
# and places come through wdt: (the best-ranked value, deprecated ones already left out);
# dates and quantities need the full value (precision, unit, point in time) and keep every
# statement — the load keeps one per (title, fact) anyway.
QUERY = {
    "date": PREFIX + """SELECT ?article ?v ?pr WHERE {
  ?item p:%(prop)s ?st . ?st psv:%(prop)s [ wikibase:timeValue ?v ; wikibase:timePrecision ?pr ] . """ + _ARTICLE + " }",
    "quantity": PREFIX + """SELECT ?article ?v ?unit ?at WHERE {
  ?item p:%(prop)s ?st . ?st psv:%(prop)s [ wikibase:quantityAmount ?v ; wikibase:quantityUnit ?unit ] .
  OPTIONAL { ?st pq:P585 ?at } """ + _ARTICLE + " }",
    "item": PREFIX + """SELECT ?article ?label WHERE {
  ?item wdt:%(prop)s ?o . ?o rdfs:label ?label . FILTER(LANG(?label) = "en") """ + _ARTICLE + " }",
}
COUNT = {k: "SELECT (COUNT(*) AS ?count) WHERE {" + v.split("WHERE {", 1)[1].replace(PREFIX, "") for k, v in QUERY.items()}
COUNT = {k: PREFIX + v for k, v in COUNT.items()}

# Units search-api can say: Wikidata unit item -> short name.
UNITS = {"Q174728": "cm", "Q11573": "m", "Q218593": "in", "Q3710": "ft", "Q712226": "km2",
         "Q232291": "mi2", "Q35852": "ha", "Q25343": "m2", "Q199": ""}


def log(msg: str) -> None:
    print(f"[facts] {msg}", flush=True)


def query_for(fact: str, page: int | None = None) -> str:
    kind, prop = FACTS[fact]
    q = QUERY[kind] % {"prop": prop}
    if page is not None:
        # every selected column: ?article alone ties on items with several values, and OFFSET
        # paging over ties can repeat or skip rows at a page edge
        order = {"date": "?article ?v ?pr", "quantity": "?article ?v ?unit ?at", "item": "?article ?label"}[kind]
        q += f"\nORDER BY {order} LIMIT {PAGE} OFFSET {page * PAGE}"
    return q


def count_query(fact: str) -> str:
    kind, prop = FACTS[fact]
    return COUNT[kind] % {"prop": prop}


def _title(v: str) -> str | None:
    v = v.strip().strip("<>")
    return urllib.parse.unquote(v[len(_WIKI):]).replace("_", " ") if v.startswith(_WIKI) else None


def parse_row(fact: str, line: str):
    """One QLever TSV line -> (title, fact, value, unit, precision, at) or None."""
    p = line.rstrip("\n").split("\t")
    kind = FACTS[fact][0]
    title = _title(p[0]) if p else None
    if not title or len(p) < 2:
        return None
    if kind == "date":
        m = re.match(r'^"?([+-]?\d{1,6}-\d\d-\d\d)', lit(p[1]))
        if not m or len(p) < 3:
            return None
        try:
            prec = int(float(lit(p[2])))
        except ValueError:
            return None
        return (title, fact, m.group(1), None, prec, None)
    if kind == "quantity":
        if len(p) < 3:
            return None
        try:
            v = float(lit(p[1]))
        except ValueError:
            return None
        unit = UNITS.get(p[2].strip().strip("<>").rsplit("/", 1)[-1])
        if unit is None:
            return None                                  # a unit we cannot say (yards, feet+inches as one)
        at = None
        if len(p) > 3 and p[3].strip():
            m = re.match(r'^"?([+-]?\d{1,6}-\d\d-\d\d)', lit(p[3]))
            at = m.group(1) if m else None
        return (title, fact, repr(v) if v != int(v) else str(int(v)), unit, None, at)
    label = lit(p[1]).strip()
    return (title, fact, label[:200], None, None, None) if label else None


def cmd_facts(conn, facts: list[str] | None = None) -> None:
    t0 = time.time()
    want = facts or list(FACTS)
    conn.execute("DROP TABLE IF EXISTS wiki_facts_new")
    conn.execute("CREATE TABLE wiki_facts_new (title text NOT NULL, fact text NOT NULL, value text NOT NULL, "
                 "unit text, precision smallint, at text)")
    if facts and conn.execute("SELECT to_regclass('wiki_facts')").fetchone()[0]:
        conn.execute("INSERT INTO wiki_facts_new SELECT * FROM wiki_facts WHERE fact <> ALL(%s)", (want,))
    failed = []
    for fact in want:
        for attempt in range(3):
            try:
                _load_fact(conn, fact)
                break
            except Exception as e:                        # a QLever 500 / timeout on one fact
                conn.execute("DELETE FROM wiki_facts_new WHERE fact = %s", (fact,))
                log(f"{fact}: attempt {attempt + 1} failed ({type(e).__name__}: {str(e)[:120]})")
                if attempt == 2:
                    failed.append(fact)
                    # keep what the previous load had for it rather than losing the fact
                    if conn.execute("SELECT to_regclass('wiki_facts')").fetchone()[0]:
                        conn.execute("INSERT INTO wiki_facts_new SELECT * FROM wiki_facts WHERE fact = %s", (fact,))
                else:
                    time.sleep(120)
        time.sleep(5)                                      # one query at a time, spaced out
    with conn.transaction():
        # One value per (title, fact) for dates and quantities: the latest "point in time"
        # (population), else the most precise, else the earliest date; labels keep all
        # (several founders, spouses over the years).
        conn.execute("""DELETE FROM wiki_facts_new w USING (
            SELECT ctid, row_number() OVER (PARTITION BY title, fact
                ORDER BY at DESC NULLS LAST, precision DESC NULLS LAST, value) AS rn
            FROM wiki_facts_new WHERE fact IN (SELECT unnest(%s::text[]))) d
            WHERE w.ctid = d.ctid AND d.rn > 1""",
                     ([f for f, (k, _) in FACTS.items() if k in ("date", "quantity")],))
        conn.execute("DROP TABLE IF EXISTS wiki_facts")
        conn.execute("ALTER TABLE wiki_facts_new RENAME TO wiki_facts")
        conn.execute("CREATE INDEX wiki_facts_title ON wiki_facts (title, fact)")
    for fact, n in conn.execute("SELECT fact, count(*) FROM wiki_facts GROUP BY 1 ORDER BY 2 DESC"):
        log(f"  {fact:<13} {n:>10,}")
    log(f"done in {(time.time() - t0) / 60:.1f} min" + (f"; FAILED, kept as before: {failed}" if failed else ""))
    if failed:
        raise SystemExit(1)


def _load_fact(conn, fact: str) -> None:
    t1 = time.time()
    with fetch(fact, count_query(fact)) as resp:
        expected = int(resp.read().decode().splitlines()[1].strip().strip('"').split("^^")[0].strip('"'))
    n = seen = page = 0
    while True:
        got = 0
        with fetch(fact, query_for(fact, page)) as resp, conn.transaction(), \
                conn.cursor().copy("COPY wiki_facts_new FROM STDIN") as cp:
            for line in io.TextIOWrapper(resp, encoding="utf-8", errors="replace"):
                if line.startswith("?"):
                    continue
                got += 1
                r = parse_row(fact, line)
                if r:
                    cp.write_row(tuple(v.replace("\x00", "") if isinstance(v, str) else v for v in r))
                    n += 1
        seen += got
        if got < PAGE:
            break
        page += 1
        time.sleep(5)
    if not complete(seen, expected):
        raise RuntimeError(f"{fact}: QLever sent {seen:,} of {expected:,} rows")
    log(f"{fact}: {seen:,} of {expected:,} -> {n:,} usable in {time.time() - t1:.0f}s")
