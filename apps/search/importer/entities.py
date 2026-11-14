"""Named-entity lists from Wikidata (CC0) "list of every publicly traded
company, every professional sports player, every food, every chain restaurant, every
celebrity, every video game, every country, every state, every make and model of vehicle,
every musician, all the news organizations".

One QLever query per category (paged), the same public endpoint and back-off as
wikilinks.py; nothing is fetched from the entities' own sites. -> entities (category, qid,
name, aliases, sitelinks, extra). Used to synthesize the big query list and to check that
each well-known name finds its answer.

    search_import.py entities [--categories a,b]
"""

from __future__ import annotations

import io
import time

import urllib.error

from wikilinks import fetch as _fetch

PAGE_GAP_S = 15          # between pages and categories: QLever answered 429 at 5 s (2026-09-25)
BACKOFF_S = 300


def fetch(category: str, query: str):
    """wikilinks.fetch (60/120 s retries) plus up to three 5-minute waits on 429/503."""
    for attempt in range(4):
        try:
            return _fetch(category, query)
        except urllib.error.HTTPError as e:
            if e.code not in (429, 503) or attempt == 3:
                raise
            log(f"{category}: HTTP {e.code} after retries — waiting {BACKOFF_S} s")
            time.sleep(BACKOFF_S)
    raise RuntimeError("unreachable")

PFX = """PREFIX wd: <http://www.wikidata.org/entity/> PREFIX wdt: <http://www.wikidata.org/prop/direct/>
PREFIX p: <http://www.wikidata.org/prop/> PREFIX pq: <http://www.wikidata.org/prop/qualifier/>
PREFIX wikibase: <http://wikiba.se/ontology#> PREFIX schema: <http://schema.org/>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#> PREFIX skos: <http://www.w3.org/2004/02/skos/core#>
"""
ENWIKI = "?art schema:about ?item ; schema:isPartOf <https://en.wikipedia.org/> ."


def _label_of(var: str, out: str) -> str:
    return f"OPTIONAL {{ {var} rdfs:label {out} FILTER(LANG({out}) = \"en\") }}"


# category -> (WHERE body selecting ?item, optional pattern binding ?extra). Counts measured
# 2026-09-25 on QLever: public_company 18,339, pro_athlete 364,148, food 14,085, drink 3,647,
# restaurant_chain 2,698, celebrity 28,233, video_game 31,541, country 197, state 598,
# vehicle_model 10,633, motorcycle_model 2,836, musician 150,100, band 47,307, news_org 12,128.
CATEGORIES = {
    # listed on a stock exchange; extra = ticker
    # (an organization: crypto exchanges count as exchanges, and Bitcoin came back "listed")
    "public_company": ("?item wdt:P414 ?ex ; wdt:P31/wdt:P279* wd:Q43229 .",
                       "OPTIONAL { ?item p:P414 ?st . ?st pq:P249 ?extra }"),
    # a person whose occupation is a kind of athlete, who played for a team, with an article
    "pro_athlete": ("?item wdt:P31 wd:Q5 ; wdt:P106/wdt:P279* wd:Q2066131 ; wdt:P54 ?team . " + ENWIKI,
                    "OPTIONAL { ?item wdt:P641 ?sport . " + _label_of("?sport", "?extra") + " }"),
    "food": ("?item (wdt:P31|wdt:P279)/wdt:P279* wd:Q2095 . " + ENWIKI, ""),
    "drink": ("?item (wdt:P31|wdt:P279)/wdt:P279* wd:Q40050 . " + ENWIKI, ""),
    # restaurant chain (Q18534542) and its subclasses, e.g. fast food restaurant chain
    "restaurant_chain": ("?item wdt:P31/wdt:P279* wd:Q18534542 .", ""),
    # famous people: in 20+ Wikipedia language editions. The FILTER on sitelinks returned 0
    # rows once joined to labels on QLever (a count alone said 28,233), so this category is
    # read in sitelinks order instead and cut at MIN_SITELINKS here (see query_for).
    "celebrity": ("?item wdt:P31 wd:Q5 ; wikibase:sitelinks ?sl .",
                  "OPTIONAL { ?item wdt:P106 ?occ . " + _label_of("?occ", "?extra") + " }"),
    "video_game": ("?item wdt:P31/wdt:P279* wd:Q7889 . " + ENWIKI, ""),
    # sovereign states that still exist
    "country": ("?item wdt:P31 wd:Q3624078 . FILTER NOT EXISTS { ?item wdt:P576 ?end }", ""),
    # US states, and states of other federations with an article
    "state": ("{ ?item wdt:P31 wd:Q35657 } UNION { ?item wdt:P31/wdt:P279* wd:Q107390 . " + ENWIKI + " }",
              "OPTIONAL { ?item wdt:P17 ?c . " + _label_of("?c", "?extra") + " }"),
    # automobile / motorcycle models; extra = the maker ("Toyota" + "Camry")
    "vehicle_model": ("?item wdt:P31/wdt:P279* wd:Q3231690 ; wdt:P176 ?maker .",
                      _label_of("?maker", "?extra")),
    "motorcycle_model": ("?item wdt:P31/wdt:P279* wd:Q23866334 .",
                         "OPTIONAL { ?item wdt:P176 ?maker . " + _label_of("?maker", "?extra") + " }"),
    "musician": ("?item wdt:P31 wd:Q5 ; wdt:P106/wdt:P279* wd:Q639669 . " + ENWIKI, ""),
    "band": ("?item wdt:P31/wdt:P279* wd:Q215380 . " + ENWIKI, ""),
    # newspapers, news agencies, online newspapers, news magazines
    "news_org": ("{ ?item wdt:P31/wdt:P279* wd:Q11032 } UNION { ?item wdt:P31/wdt:P279* wd:Q192283 } UNION "
                 "{ ?item wdt:P31/wdt:P279* wd:Q1153191 } UNION { ?item wdt:P31/wdt:P279* wd:Q1684600 } . " + ENWIKI,
                 "OPTIONAL { ?item wdt:P17 ?c . " + _label_of("?c", "?extra") + " }"),
}
PAGE = 200_000
# categories read most-famous-first and cut at a sitelinks floor
MIN_SITELINKS = {"celebrity": 20}
SORTED_PAGE = 20_000


def query_for(category: str, page: int) -> str:
    body, extra = CATEGORIES[category]
    ranked = category in MIN_SITELINKS
    sl = "" if ranked else "OPTIONAL { ?item wikibase:sitelinks ?sl } "
    # ?item breaks ties, so the pages neither repeat nor skip anyone
    order = (f"ORDER BY DESC(?sitelinks) ?item LIMIT {SORTED_PAGE} OFFSET {page * SORTED_PAGE}" if ranked
             else f"ORDER BY ?item LIMIT {PAGE} OFFSET {page * PAGE}")
    # descr = Wikidata's English description ("American astronaut"); wiki = the English
    # Wikipedia article's title, the reliable join to suggest_items (a name join matched
    # the band "Louis XIV" to the king's article)
    return (PFX + "SELECT ?item ?label (MAX(?sl) AS ?sitelinks) (SAMPLE(?extra) AS ?x) "
            "(GROUP_CONCAT(DISTINCT ?alias; separator=\"|\") AS ?aliases) (SAMPLE(?d) AS ?descr) "
            "(SAMPLE(?wt) AS ?wiki) WHERE { "
            + body + " ?item rdfs:label ?label FILTER(LANG(?label) = \"en\") " + sl +
            "OPTIONAL { ?item skos:altLabel ?alias FILTER(LANG(?alias) = \"en\") } "
            "OPTIONAL { ?item schema:description ?d FILTER(LANG(?d) = \"en\") } "
            "OPTIONAL { ?art2 schema:about ?item ; schema:isPartOf <https://en.wikipedia.org/> ; schema:name ?wt } "
            + extra + " } GROUP BY ?item ?label " + order)


def _lit(v: str) -> str:
    """'"Toyota Camry"@en' -> 'Toyota Camry'; '"42"^^<…int>' -> '42'; '' -> ''."""
    v = v.strip()
    if v.startswith('"'):
        v = v[1:v.rfind('"')]
    return v.replace('\\"', '"').replace("\\\\", "\\")


def parse_row(category: str, line: str):
    """One QLever TSV line -> (category, qid, name, aliases, sitelinks, extra, descr, wiki), or None."""
    p = line.rstrip("\n").split("\t")
    if len(p) == 5:                      # the older 5-column shape: no description / article
        p += ["", ""]
    if len(p) != 7 or not p[0].startswith("<http://www.wikidata.org/entity/Q"):
        return None
    qid = p[0].rsplit("/", 1)[1].rstrip(">")
    name = _lit(p[1])
    if not name or name == qid:
        return None
    try:
        sl = int(_lit(p[2]).split("^^")[0] or 0)
    except ValueError:
        sl = 0
    aliases = [a for a in _lit(p[4]).split("|") if a and a != name][:20]
    return (category, qid, name[:300], aliases, sl, _lit(p[3])[:200], _lit(p[5])[:200], _lit(p[6])[:300])


def log(msg: str) -> None:
    print(f"[entities] {msg}", flush=True)


def cmd_entities(conn, categories: list[str] | None = None) -> None:
    t0 = time.time()
    want = categories or list(CATEGORIES)
    conn.execute("CREATE TABLE IF NOT EXISTS entities (category text, qid text, name text, aliases text[], "
                 "sitelinks integer, extra text, PRIMARY KEY (category, qid))")
    conn.execute("ALTER TABLE entities ADD COLUMN IF NOT EXISTS descr text")
    conn.execute("ALTER TABLE entities ADD COLUMN IF NOT EXISTS wiki text")
    # Each category loads into a staging table and replaces its old rows only once every
    # page arrived: a 429 mid-run deleted all 88,863 famous people (2026-09-25).
    conn.execute("CREATE TEMP TABLE IF NOT EXISTS entities_stage (LIKE entities INCLUDING DEFAULTS)")
    failed = []
    for cat in want:
        t1 = time.time()
        conn.execute("TRUNCATE entities_stage")
        try:
            n = _load_category(conn, cat)
        except Exception as e:                   # keep the old rows; try the next category
            failed.append(cat)
            log(f"{cat}: FAILED ({type(e).__name__}: {e}) — previous rows kept")
            time.sleep(PAGE_GAP_S)
            continue
        with conn.transaction():
            conn.execute("DELETE FROM entities WHERE category = %s", (cat,))
            conn.execute("INSERT INTO entities SELECT * FROM entities_stage")
        log(f"{cat}: {n:,} in {time.time() - t1:.0f}s")
        time.sleep(PAGE_GAP_S)
    conn.execute("CREATE INDEX IF NOT EXISTS entities_name ON entities (lower(name))")
    conn.execute("CREATE INDEX IF NOT EXISTS entities_wiki ON entities (wiki)")
    k = conn.execute("SELECT count(*) FROM entities").fetchone()[0]
    log(f"entities ready: {k:,} rows in {(time.time() - t0) / 60:.1f} min"
        + (f"; FAILED, kept as before: {failed}" if failed else ""))
    if failed:
        raise SystemExit(1)


def _load_category(conn, cat: str) -> int:
    """One category, every page, into entities_stage; returns rows kept."""
    n = page = 0
    seen: set[str] = set()      # an item with two English labels ("Fei-fei Feng" /
                                # "Feng Fei-fei", Q700792) comes back twice: keep one
    floor = MIN_SITELINKS.get(cat)
    size = SORTED_PAGE if floor else PAGE
    while True:
        got = low = rows = 0
        with fetch(cat, query_for(cat, page)) as resp, conn.transaction(), \
                conn.cursor().copy("COPY entities_stage (category, qid, name, aliases, sitelinks, extra, descr, wiki) FROM STDIN") as cp:
            for line in io.TextIOWrapper(resp, encoding="utf-8", errors="replace"):
                row = parse_row(cat, line)
                rows += row is not None
                if row and floor and row[4] < floor:
                    low += 1                         # below the fame floor: this page is the last
                    continue
                if row and row[1] not in seen:
                    seen.add(row[1])
                    cp.write_row(row)
                    got += 1
        n += got
        # a short page is the last one; count what QLever returned, not what was kept
        # (one duplicate made the first famous-people page look short: 19,999 of 20,000)
        if rows < size or low:
            break
        page += 1
        time.sleep(PAGE_GAP_S)                       # one query at a time, spaced
    return n
