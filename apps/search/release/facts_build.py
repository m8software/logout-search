#!/usr/bin/env python3
"""facts.sqlite — the Wikidata facts layer of a release (epic stride C).

"population of lisbon", "when was tesla founded", "ceo of nvidia": Wikidata (CC0) holds the answer as a
property value with a source article. This builds one SQLite file keyed by the registry's wiki doc_id:
for every English article, the properties of apps/search/release/facts_templates.tsv with their values,
the qualifiers the card needs (point in time, start/end, unit, date precision) and, where the value is
an item, the value's own article doc_id. The engine reads it through serve/api/facts.py.

    facts_build.py fetch  --cache DIR [--props P1082,P571] [--templates TSV]
        DIR/articles.tsv (Wikidata item -> English article, once) and DIR/<prop>.tsv per property: COUNT first,
        then one unordered stream keyed by item, split by Q-id range when QLever times out or sends a short
        answer; a part that stays short refuses the property (wikilinks.py's completeness rule). One request
        at a time, 5 s apart; a finished file is never fetched again
    facts_build.py fetch-lists --cache DIR
        the list classes (films, albums, video games, elections) with their year and genre
    facts_build.py build REGISTRY SIGNALS OUT.sqlite RELEASE_ID --cache DIR [--templates TSV] [--top N]
        titles -> doc_ids through registry.sqlite (a title shared by two articles belongs to the lowest
        page_id, the registry's rule); pageviews from signals.bin order list rows and, with --top N,
        keep only the N most-viewed subject articles; stamped like every release SQLite
    facts_build.py rank OUT.sqlite SIGNALS [--top 100000]
        per property: how many of the top-N articles by pageviews carry it and their share of the views
        (the "head queries it would match" ranking the template table is ordered by)

Stdlib only (sqlite3, urllib). Nothing is fetched from Wikipedia; pageviews are the ones the release
already carries (signals.bin wiki.pageviews, CC0). No LLM anywhere.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import re
import sqlite3
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATES = os.path.join(HERE, "facts_templates.tsv")
ENDPOINT = "https://qlever.dev/api/wikidata"
USER_AGENT = "logout-search-import/1 (personal search index; facts)"
FORMAT_VERSION, APP_ID = 1, 0x6C6F7331        # the release SQLite stamp (lookups_release.FORMAT_VERSION / APP_ID)
ROLE = "facts"
LICENCE = "CC0-1.0 (Wikidata statements, labels and qualifiers; M8 layer)"
MAX_VALUES = 8                                 # values kept per (article, property): cast, children, awards
# what the card can say of each kind: one date, the newest amount and one fallback (a unit we cannot say), ...
MAX_BY_KIND = {"date": 1, "quantity": 2, "coord": 1, "string": 4, "item": MAX_VALUES}
_WIKI = "https://en.wikipedia.org/wiki/"
TYPE_SHIFT = 28

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
_RANK = "?st wikibase:rank ?rank ."
# kind -> (selected columns, body), keyed by ?item. The article join is done locally against articles.tsv: on
# qlever.dev the join plus ORDER BY paging ran past its 30 s compute limit (reported as HTTP 429, 2026-10-12), while
# the same statements unjoined stream in seconds. Every statement rank is read and deprecated ones dropped in
# parse_row (a preferred-only filter cut 2.06M birth dates to 68,864 on QLever, importer/facts.py 2026-09-25).
QUERY = {
    "date": ("?item ?v ?pr ?rank",
             "?item p:%(p)s ?st . ?st psv:%(p)s [ wikibase:timeValue ?v ; wikibase:timePrecision ?pr ] . " + _RANK),
    "quantity": ("?item ?v ?unit ?at ?rank",
                 "?item p:%(p)s ?st . ?st psv:%(p)s [ wikibase:quantityAmount ?v ; wikibase:quantityUnit ?unit ] . "
                 + _RANK + " OPTIONAL { ?st pq:P585 ?at }"),
    "item": ("?item ?v ?label ?start ?end ?rank",
             "?item p:%(p)s ?st . ?st ps:%(p)s ?v . " + _RANK
             + ' OPTIONAL { ?v rdfs:label ?label . FILTER(LANG(?label) = "en") }'
             + " OPTIONAL { ?st pq:P580 ?start } OPTIONAL { ?st pq:P582 ?end }"),
    "string": ("?item ?v ?rank", "?item p:%(p)s ?st . ?st ps:%(p)s ?v . " + _RANK),
    "coord": ("?item ?lat ?lon ?rank",
              "?item p:%(p)s ?st . ?st psv:%(p)s [ wikibase:geoLatitude ?lat ; wikibase:geoLongitude ?lon ] . " + _RANK),
}
ARTICLES = ("?item ?article", "?article schema:about ?item ; schema:isPartOf <https://en.wikipedia.org/> .")
MAX_DEPTH = 4                                  # Q-id range splits: 1, 10, 100, 1000 parts of a leading digit

# The list card: type + year ("sci-fi films 1982", "albums 1999"). v1: films, albums, video games, elections.
LIST_CLASSES = {
    # cls: (query words, Wikidata class, date property, has genre)
    "film": ("films|movies|motion pictures|flicks", "Q11424", "P577", True),
    "album": ("albums|records|lps", "Q482994", "P577", True),
    "game": ("video games|games|videogames", "Q7889", "P577", True),
    "election": ("elections|votes|polls", "Q40231", "P585", False),
}
LIST_QUERY = PREFIX + """SELECT ?item (MIN(YEAR(?d)) AS ?year) (GROUP_CONCAT(DISTINCT LCASE(?g); SEPARATOR="|") AS ?genres) WHERE {
  ?item wdt:P31/wdt:P279* wd:%(cls)s . ?item wdt:%(date)s ?d .
  %(genre)s
} GROUP BY ?item"""
_GENRE = '''OPTIONAL { ?item wdt:P136 ?gi . ?gi rdfs:label ?g . FILTER(LANG(?g) = "en") }'''
# what people type -> the substring of the genre label that means it
GENRES = {
    "sci-fi": "science fiction", "scifi": "science fiction", "sci fi": "science fiction", "science fiction": "science fiction",
    "horror": "horror", "comedy": "comedy", "comedies": "comedy", "drama": "drama", "dramas": "drama", "action": "action",
    "thriller": "thriller", "thrillers": "thriller", "romance": "romance", "romantic": "romance", "fantasy": "fantasy",
    "animated": "animat", "animation": "animat", "western": "western", "westerns": "western", "war": "war film",
    "crime": "crime", "musical": "musical", "musicals": "musical", "documentary": "documentary", "documentaries": "documentary",
    "adventure": "adventure", "mystery": "mystery", "superhero": "superhero", "disaster": "disaster", "noir": "noir",
    "rock": "rock", "pop": "pop", "jazz": "jazz", "metal": "metal", "hip hop": "hip hop", "hip-hop": "hip hop", "rap": "hip hop",
    "punk": "punk", "country": "country", "electronic": "electronic", "folk": "folk", "blues": "blues", "soul": "soul",
    "rpg": "role-playing", "role playing": "role-playing", "shooter": "shooter", "fps": "shooter", "platform": "platform",
    "platformer": "platform", "puzzle": "puzzle", "racing": "racing", "strategy": "strategy", "fighting": "fighting",
    "sports": "sports", "simulation": "simulation", "survival": "survival", "presidential": "presidential",
    "general": "general", "parliamentary": "parliamentary", "local": "local", "mayoral": "mayoral",
}

# Wikidata unit item -> the symbol the engine formats (serve/api/facts.py UNIT_FAMILY knows these); an
# unknown unit is stored as its Q-id, so the engine can show the number without a unit or skip it.
UNITS = {
    "Q199": "", "Q11573": "m", "Q174728": "cm", "Q828224": "km", "Q3710": "ft", "Q218593": "in", "Q253276": "mi", "Q482798": "yd",
    "Q200323": "dm", "Q175821": "um", "Q178674": "nm", "Q712226": "km2", "Q232291": "mi2", "Q35852": "ha", "Q25343": "m2", "Q81292": "acre",
    "Q857027": "ft2", "Q11570": "kg", "Q100995": "lb", "Q41803": "g", "Q191118": "t", "Q48013": "oz", "Q752079": "lb", "Q4917": "USD",
    "Q4916": "EUR", "Q25224": "GBP", "Q8146": "JPY", "Q39099": "CNY", "Q4596": "CHF", "Q8142": "CAD", "Q259502": "AUD", "Q4910": "BRL",
    "Q80524": "INR", "Q41044": "RUB", "Q122922": "SEK", "Q132643": "ZAR", "Q131473": "ZAR", "Q172524": "SEK", "Q4286": "DKK", "Q132029": "NOK",
    "Q172872": "HKD", "Q202040": "SGD", "Q171503": "RUB", "Q4917_M": "USD", "Q7727": "min", "Q25235": "h", "Q11574": "s", "Q573": "d",
    "Q577": "a", "Q1092296": "a", "Q11229": "%", "Q25267": "°C", "Q42289": "°F", "Q11579": "K", "Q180154": "km/h", "Q211256": "mph",
    "Q182429": "m/s", "Q5195628": "ha", "Q844338": "dollar", "Q750107": "KRW", "Q4713": "ILS", "Q4917_": "USD", "Q200000": "MXN",
    "Q125999": "PLN", "Q131645": "NZD", "Q4917_2": "USD", "Q33652": "CZK", "Q201397": "TRY", "Q47190": "HUF", "Q8148": "EGP",
    "Q177875": "ARS", "Q200050": "CLP", "Q4917_3": "USD", "Q4917_4": "USD", "Q4917_5": "USD", "Q4917_6": "USD",
}
_LIT = re.compile(r'^"(.*)"(?:@[a-zA-Z-]+|\^\^<[^>]*>)?$', re.S)


def log(msg: str) -> None:
    print(f"[facts] {msg}", flush=True)


def lit(v: str) -> str:
    """'"MrBeast"@en' -> 'MrBeast'; '"19900000.0"^^<xsd:decimal>' -> '19900000.0'; '' -> ''."""
    v = v.strip()
    m = _LIT.match(v)
    return m.group(1).replace('\\"', '"').replace("\\\\", "\\") if m else v


def pattern_regex(p: str) -> str:
    """A template pattern -> the anchored regex: the first {x} is the entity, a second {x} must repeat it
    ("why is {x} called {x}"). The engine (serve/api/facts.py) compiles the same way."""
    first, rest = p.split("{x}", 1)
    return "^" + first + "(?P<x>.+?)" + rest.replace("{x}", "(?P=x)") + "$"


def load_templates(path: str = TEMPLATES) -> list[dict]:
    """The checked-in template table: one row per property (prop, name, kind, unit, label, patterns)."""
    with open(path, encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f, delimiter="\t"))
    seen = set()
    for r in rows:
        if r["prop"] in seen:
            sys.exit(f"{path}: property {r['prop']} appears twice")
        seen.add(r["prop"])
        if r["kind"] not in QUERY:
            sys.exit(f"{path}: {r['prop']} has kind {r['kind']!r}, want one of {sorted(QUERY)}")
        for p in r["patterns"].split("||"):
            if "{x}" not in p:
                sys.exit(f"{path}: pattern without {{x}} for {r['prop']}: {p!r}")
            re.compile(pattern_regex(p))
    return rows


def templates_sha256(path: str = TEMPLATES) -> str:
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


# --------------------------------------------------------------------------- QLever
def _range(lo: str | None, hi: str | None) -> str:
    """Items whose IRI sorts in [wd:lo, wd:hi): QLever compares IRIs as strings, so wd:Q1..wd:Q2 is every id that
    starts with 1."""
    f = [f"?item >= wd:{lo}"] if lo else []
    if hi:
        f.append(f"?item < wd:{hi}")
    return f"FILTER({' && '.join(f)})" if f else ""


def query_for(cols: str, body: str, lo: str | None = None, hi: str | None = None) -> str:
    return PREFIX + f"SELECT {cols} WHERE {{ {body} {_range(lo, hi)} }}"


def count_query(body: str, lo: str | None = None, hi: str | None = None) -> str:
    return PREFIX + f"SELECT (COUNT(*) AS ?n) WHERE {{ {body} {_range(lo, hi)} }}"


def split_range(lo: str | None, hi: str | None) -> list[tuple[str | None, str | None]]:
    """A range cut in ten by the next digit: (None, None) -> Q1..Q9; (Q1, Q2) -> [Q1, Q10) (the id Q1 itself),
    [Q10, Q11) ... [Q19, Q2). The parts cover the range exactly."""
    if lo is None:
        b = [f"Q{i}" for i in range(1, 10)] + [None]
        return list(zip(b[:-1], b[1:]))
    b = [lo] + [f"{lo}{i}" for i in range(10)] + [hi]
    return list(zip(b[:-1], b[1:]))


def complete(got: int, expected: int) -> bool:
    """A short answer must stop the load: 0.5% slack for rows QLever itself drops (wikilinks.complete)."""
    return got >= expected * 0.995


class QueryTimeout(Exception):
    """qlever.dev ran past its compute limit: asking again is pointless, ask for less."""


GAP_S = 5.0                                    # one request at a time, this long after each
_last = [0.0]


def fetch(what: str, query: str, timeout: int = 900):
    """The open QLever response (TSV), read line by line. One request at a time, GAP_S apart; a real 429/503 backs
    off exponentially (60 s, 120 s, 240 s, 480 s); a compute timeout (also sent as 429) raises QueryTimeout."""
    req = urllib.request.Request(ENDPOINT, data=urllib.parse.urlencode({"query": query}).encode(),
                                 headers={"User-Agent": USER_AGENT, "Accept": "text/tab-separated-values"})
    for attempt in range(5):
        time.sleep(max(0.0, _last[0] + GAP_S - time.time()))
        try:
            return urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            body = e.read(600).decode("utf-8", "replace")
            if "timed out" in body or "memory" in body.lower():
                raise QueryTimeout(body[:160]) from None
            if e.code not in (429, 503) or attempt == 4:
                raise
            wait = int(e.headers.get("Retry-After") or 60 * 2 ** attempt)
            log(f"{what}: HTTP {e.code}, waiting {wait}s")
            time.sleep(wait)
        finally:
            _last[0] = time.time()
    raise RuntimeError("unreachable")


def read_count(resp) -> int:
    return int(float(lit(resp.read().decode().splitlines()[1]).split("^^")[0].strip('"')))


def _stream(what: str, cols: str, body: str, lo, hi, o, depth: int = 0) -> tuple[int, int]:
    """Append every row of the range to `o`; (rows, expected). COUNT first, then one unordered stream; a timeout or
    a short stream splits the range in ten and tries each part (the parts are disjoint, so nothing repeats)."""
    try:
        with fetch(what, count_query(body, lo, hi)) as resp:
            expected = read_count(resp)
        if expected == 0:
            return 0, 0
        got, mark = 0, o.tell()                            # rows go straight to the file; a short range is cut back off
        with fetch(what, query_for(cols, body, lo, hi)) as resp:
            for line in io.TextIOWrapper(resp, encoding="utf-8", errors="replace"):
                if line.startswith("?"):
                    continue
                o.write(line.replace("\x00", ""))
                got += 1
        _last[0] = time.time()
        if complete(got, expected):
            return got, expected
        o.seek(mark)
        o.truncate()
        why = f"{got:,} of {expected:,}"
    except (QueryTimeout, TimeoutError, ConnectionError, urllib.error.URLError) as e:
        why = f"{type(e).__name__}"
        if "mark" in locals():
            o.seek(mark)
            o.truncate()
    if depth >= MAX_DEPTH:
        raise RuntimeError(f"{what} {lo}..{hi}: {why} at the smallest range")
    log(f"{what} {lo or 'Q'}..{hi or ''}: {why}, splitting")
    n = exp = 0
    for a, b in split_range(lo, hi):
        g, e = _stream(what, cols, body, a, b, o, depth + 1)
        n, exp = n + g, exp + e
    return n, exp


def fetch_file(what: str, cols: str, body: str, out: str) -> tuple[int, int]:
    """One cached TSV (header line first), written under .part and renamed only when complete: a rerun never
    refetches a finished file. Returns (rows, expected)."""
    if os.path.exists(out):
        with open(out, encoding="utf-8") as f:
            n = sum(1 for _ in f) - 1
        return n, n
    t0 = time.time()
    tmp = out + ".part"
    with open(tmp, "w", encoding="utf-8") as o:
        o.write(cols.replace(" ", "\t") + "\n")
        seen, expected = _stream(what, cols, body, None, None, o)
    if not complete(seen, expected):
        os.unlink(tmp)
        raise RuntimeError(f"{what}: QLever sent {seen:,} of {expected:,} rows — not keeping a partial file")
    os.replace(tmp, out)
    log(f"{what}: {seen:,} of {expected:,} rows in {time.time() - t0:.0f}s")
    return seen, expected


JOIN_OVER = 10_000_000                         # statements of a property, all items: past this, ask only for articles' items


def fetch_property(prop: str, kind: str, cache: str) -> tuple[int, int]:
    """A property's statements. Unjoined when the whole property is small enough to stream (most are: a few
    seconds); a property that lives mostly on items without an article (P50 author and P577 publication date are
    tens of millions of scholarly papers) is asked with the article join as a filter, by Q-id range."""
    cols, body = QUERY[kind]
    body = body % {"p": prop}
    out = os.path.join(cache, f"{prop}.tsv")
    if not os.path.exists(out):
        try:
            with fetch(prop, count_query(body)) as resp:
                big = read_count(resp) > JOIN_OVER
        except QueryTimeout:
            big = True
        if big:
            body += " " + ARTICLES[1]
            log(f"{prop}: over {JOIN_OVER:,} statements, fetching the ones of items with an English article")
    return fetch_file(prop, cols, body, out)


def cmd_fetch(cache: str, props: list[str] | None, templates: str) -> None:
    os.makedirs(cache, exist_ok=True)
    rows = load_templates(templates)
    want = [("articles", lambda: fetch_file("articles", ARTICLES[0], ARTICLES[1], os.path.join(cache, "articles.tsv")))]
    want += [(r["prop"], lambda r=r: fetch_property(r["prop"], r["kind"], cache)) for r in rows if not props or r["prop"] in props]
    failed = []
    t0 = time.time()
    for name, job in want:
        for attempt in range(3):
            try:
                job()
                break
            except Exception as e:                        # a QLever 500 on one property
                log(f"{name}: attempt {attempt + 1} failed ({type(e).__name__}: {str(e)[:160]})")
                if attempt == 2:
                    failed.append(name)
                else:
                    time.sleep(120 * (attempt + 1))
    log(f"fetch done in {(time.time() - t0) / 60:.1f} min" + (f"; FAILED: {failed}" if failed else ""))
    if failed:
        raise SystemExit(1)


def cmd_fetch_lists(cache: str) -> None:
    os.makedirs(cache, exist_ok=True)
    for cls, (_words, qid, dateprop, genre) in LIST_CLASSES.items():
        out = os.path.join(cache, f"list-{cls}.tsv")
        if os.path.exists(out):
            continue
        t0 = time.time()
        q = LIST_QUERY % {"cls": qid, "date": dateprop, "genre": _GENRE if genre else ""}
        n = 0
        with fetch(cls, q) as resp, open(out + ".part", "w", encoding="utf-8") as o:
            for line in io.TextIOWrapper(resp, encoding="utf-8", errors="replace"):
                o.write(line.replace("\x00", ""))
                n += 1
        os.replace(out + ".part", out)
        log(f"list {cls}: {n - 1:,} rows in {time.time() - t0:.0f}s")


# --------------------------------------------------------------------------- parsing
def _qid(v: str) -> int | None:
    """'<http://www.wikidata.org/entity/Q42>' -> 42; a blank node ("unknown value") or anything else -> None."""
    m = re.match(r"^<?http://www\.wikidata\.org/entity/Q(\d+)>?$", v.strip())
    return int(m.group(1)) if m else None


def _title(v: str) -> str | None:
    v = v.strip().strip("<>")
    return urllib.parse.unquote(v[len(_WIKI):]).replace("_", " ") if v.startswith(_WIKI) else None


def norm_date(v: str) -> tuple[str, int] | None:
    """Wikidata time '"+1969-02-11T00:00:00Z"^^xsd:dateTime' -> ('1969-02-11', year as int) or None."""
    m = re.match(r'^"?([+-]?)(\d{1,6})-(\d\d)-(\d\d)', lit(v))
    if not m:
        return None
    sign, y, mo, d = m.groups()
    y = int(y)
    return (f"{'-' if sign == '-' else ''}{y:04d}-{mo}-{d}", -y if sign == "-" else y)


def norm_quantity(v: str) -> str | None:
    s = lit(v).strip()
    try:
        f = float(s)
    except ValueError:
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return str(int(f)) if f == int(f) and abs(f) < 1e18 else repr(f)


def unit_symbol(u: str) -> str:
    q = u.strip().strip("<>").rsplit("/", 1)[-1]
    if q in UNITS:
        return UNITS[q]
    return q if re.match(r"^Q\d+$", q) else ""


def _rank_order(r: str) -> int:
    """preferred 0, normal 1, deprecated 2 (dropped)."""
    r = r.strip().strip("<>").rsplit("#", 1)[-1]
    return {"PreferredRank": 0, "NormalRank": 1, "DeprecatedRank": 2}.get(r, 1)


def parse_row(kind: str, line: str):
    """One QLever TSV line -> (item Q number, value, unit, prec, at, until, value Q number, rank, sort_key) or None.
    Deprecated statements are dropped here; the rest keep their rank for ordering."""
    p = line.rstrip("\n").split("\t")
    title = _qid(p[0]) if p else None
    if title is None:
        return None
    if kind == "date":
        if len(p) < 4:
            return None
        d = norm_date(p[1])
        if not d:
            return None
        try:
            prec = int(float(lit(p[2])))
        except ValueError:
            return None
        rank = _rank_order(p[3])
        if rank == 2:
            return None
        # the most precise date first, then the earliest (a day-precise birth over a year-only one)
        return (title, d[0], "", prec, None, None, None, rank, (rank, -prec, d[1]))
    if kind == "quantity":
        if len(p) < 5:
            return None
        v = norm_quantity(p[1])
        if v is None:
            return None
        unit = unit_symbol(p[2])
        at = norm_date(p[3])[0] if p[3].strip() and norm_date(p[3]) else None
        rank = _rank_order(p[4])
        if rank == 2:
            return None
        # the latest point in time first (population: the newest census), dated before undated
        return (title, v, unit, None, at, None, None, rank, (rank, 0 if at else 1, -int(re.sub(r"\D", "", at)[:8] or 0) if at else 0))
    if kind == "item":
        if len(p) < 6:
            return None
        vq = _qid(p[1])
        label = lit(p[2]).strip()
        if vq is None or not label:                        # "unknown value", or a value nobody named in English
            return None
        start = norm_date(p[3])[0] if p[3].strip() and norm_date(p[3]) else None
        end = norm_date(p[4])[0] if p[4].strip() and norm_date(p[4]) else None
        rank = _rank_order(p[5])
        if rank == 2:
            return None
        # current first (no end time), then the latest start
        return (title, label[:200], "", None, start, end, vq, rank, (rank, 1 if end else 0, -int(re.sub(r"\D", "", start)[:8] or 0) if start else 0))
    if kind == "string":
        if len(p) < 3:
            return None
        v = lit(p[1]).strip()
        if not v or len(v) > 200:
            return None
        rank = _rank_order(p[2])
        if rank == 2:
            return None
        return (title, v, "", None, None, None, None, rank, (rank, 0, v))
    if kind == "coord":
        if len(p) < 4:
            return None
        try:
            lat, lon = float(lit(p[1])), float(lit(p[2]))
        except ValueError:
            return None
        rank = _rank_order(p[3])
        if rank == 2:
            return None
        return (title, f"{lat:.5f},{lon:.5f}", "", None, None, None, None, rank, (rank, 0, ""))
    return None


# --------------------------------------------------------------------------- build
def read_pageviews(signals: str) -> tuple[dict, int]:
    """wiki.pageviews of signals.bin as a memoryview of u4 indexed by seq, plus its length (no numpy)."""
    with open(signals, "rb") as f:
        head = f.read(12)
        if head[:8] != b"LOSIGNL1":
            sys.exit(f"{signals}: not a LOSIGNL1 signals file")
        n = struct.unpack("<I", head[8:12])[0]
        h = json.loads(f.read(n))
        base = 12 + n
        fld = next((x for x in h["fields"] if x["name"] == "wiki.pageviews"), None)
        if not fld or fld["dtype"] != "<u4":
            sys.exit(f"{signals}: no wiki.pageviews <u4 field")
        f.seek(base + fld["offset"])
        buf = f.read(fld["count"] * 4)
    return memoryview(buf).cast("I"), fld["count"]


def top_seqs(pv, pv_n: int, top: int) -> list[int]:
    import heapq
    return heapq.nlargest(top, range(pv_n), key=pv.__getitem__)


def item_docs(db: sqlite3.Connection, cache: str, registry: str) -> tuple[int, int]:
    """temp.qdoc(q, doc_id): Wikidata Q number -> the registry doc_id of its English article, joined inside SQLite
    (articles.tsv is 10 M rows: streamed, never held in Python). A title shared by two articles belongs to the
    lowest page_id (the registry's rule). Returns (registry titles, items mapped)."""
    db.execute("ATTACH ? AS reg", (registry,))
    db.execute("CREATE TEMP TABLE tdoc (title TEXT PRIMARY KEY, doc_id INTEGER) WITHOUT ROWID")
    db.execute(f"INSERT OR IGNORE INTO tdoc SELECT title, doc_id FROM reg.registry WHERE doc_id >> {TYPE_SHIFT} = 1 AND title IS NOT NULL ORDER BY ext")
    db.execute("CREATE TEMP TABLE qdoc (q INTEGER PRIMARY KEY, doc_id INTEGER NOT NULL) WITHOUT ROWID")

    def rows():
        with open(os.path.join(cache, "articles.tsv"), encoding="utf-8") as f:
            next(f)
            for line in f:
                p = line.rstrip("\n").split("\t")
                q = _qid(p[0])
                t = _title(p[1]) if len(p) > 1 else None
                if q is not None and t:
                    yield (q, t)
    db.executemany("INSERT OR IGNORE INTO qdoc SELECT ?1, doc_id FROM tdoc WHERE title = ?2", rows())
    n_titles = db.execute("SELECT count(*) FROM tdoc").fetchone()[0]
    db.execute("DROP TABLE tdoc")
    return n_titles, db.execute("SELECT count(*) FROM qdoc").fetchone()[0]


def stamp(db: sqlite3.Connection, release: str, extra: dict) -> None:
    db.execute(f"PRAGMA application_id = {APP_ID}")
    db.execute(f"PRAGMA user_version = {FORMAT_VERSION}")
    db.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
    rows = {"role": ROLE, "format": str(FORMAT_VERSION), "built_release": release, "licence": LICENCE,
            "source": "Wikidata through QLever (qlever.dev/api/wikidata), statements of items with an English Wikipedia article", **extra}
    db.executemany("INSERT OR REPLACE INTO meta VALUES (?, ?)", sorted(rows.items()))


SCHEMA = """
CREATE TABLE props (pid INTEGER PRIMARY KEY, prop TEXT UNIQUE NOT NULL, name TEXT NOT NULL, kind TEXT NOT NULL,
                    unit TEXT NOT NULL, label TEXT NOT NULL, n INTEGER NOT NULL);
CREATE TABLE templates (pid INTEGER NOT NULL, ord INTEGER NOT NULL, pattern TEXT NOT NULL, PRIMARY KEY (pid, ord)) WITHOUT ROWID;
CREATE TABLE facts (doc_id INTEGER NOT NULL, pid INTEGER NOT NULL, ord INTEGER NOT NULL, v TEXT NOT NULL, unit TEXT,
                    prec INTEGER, at TEXT, until TEXT, vdoc INTEGER, PRIMARY KEY (doc_id, pid, ord)) WITHOUT ROWID;
CREATE TABLE lists (cls TEXT NOT NULL, year INTEGER NOT NULL, pv INTEGER NOT NULL, doc_id INTEGER NOT NULL, genre TEXT,
                    PRIMARY KEY (cls, year, pv, doc_id)) WITHOUT ROWID;
CREATE TABLE list_classes (cls TEXT PRIMARY KEY, words TEXT NOT NULL, qid TEXT NOT NULL, dateprop TEXT NOT NULL);
CREATE TABLE genres (word TEXT PRIMARY KEY, genre TEXT NOT NULL);
"""


def write_templates(db: sqlite3.Connection, rows: list[dict]) -> dict[str, int]:
    """props + templates from the TSV rows; returns prop -> pid (pid = row order, 1-based)."""
    pids = {}
    for i, r in enumerate(rows, 1):
        pids[r["prop"]] = i
        db.execute("INSERT INTO props VALUES (?, ?, ?, ?, ?, ?, 0)", (i, r["prop"], r["name"], r["kind"], r["unit"] or "", r["label"]))
        db.executemany("INSERT INTO templates VALUES (?, ?, ?)",
                       [(i, j, p) for j, p in enumerate(r["patterns"].split("||"))])
    db.executemany("INSERT INTO list_classes VALUES (?, ?, ?, ?)", [(c, w, q, d) for c, (w, q, d, _g) in LIST_CLASSES.items()])
    db.executemany("INSERT INTO genres VALUES (?, ?)", sorted(GENRES.items()))
    return pids


def load_property(db: sqlite3.Connection, pid: int, kind: str, path: str) -> tuple[int, int]:
    """DIR/<prop>.tsv -> facts rows for one property. Parsed rows are streamed into a temp table; SQLite joins them
    to the articles (temp.qdoc, and temp.head with --top), collapses equal values, orders each article's values by
    the kind's sort key and keeps MAX_VALUES. Returns (rows read, rows written)."""
    read = [0]

    def rows():
        with open(path, encoding="utf-8") as f:
            next(f)
            for line in f:
                read[0] += 1
                r = parse_row(kind, line)
                if r:
                    yield (r[0], r[1], r[2] or None, r[3], r[4], r[5], r[6], r[8][0], r[8][1], r[8][2])
    db.execute("DROP TABLE IF EXISTS temp.raw")
    db.execute("CREATE TEMP TABLE raw (q INTEGER, v TEXT, unit TEXT, prec INTEGER, at TEXT, until TEXT, vq INTEGER, k1 INTEGER, k2 INTEGER, k3 INTEGER)")
    db.executemany("INSERT INTO raw VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows())
    head = "JOIN temp.head h ON h.doc_id = a.doc_id" if db.execute("SELECT 1 FROM temp.sqlite_master WHERE name = 'head'").fetchone() else ""
    cur = db.execute(f"""INSERT INTO facts
        SELECT doc_id, ?, rn - 1, v, unit, prec, at, until, vdoc FROM (
          SELECT *, row_number() OVER (PARTITION BY doc_id ORDER BY k1, k2, k3, v) AS rn FROM (
            SELECT *, row_number() OVER (PARTITION BY doc_id, lower(v), coalesce(unit, '') ORDER BY k1, k2, k3) AS dup FROM (
              SELECT a.doc_id AS doc_id, r.v AS v, r.unit AS unit, r.prec AS prec, r.at AS at, r.until AS until,
                     (SELECT doc_id FROM temp.qdoc x WHERE x.q = r.vq) AS vdoc, r.k1 AS k1, r.k2 AS k2, r.k3 AS k3
              FROM temp.raw r JOIN temp.qdoc a ON a.q = r.q {head}))
          WHERE dup = 1)
        WHERE rn <= {MAX_BY_KIND[kind]}""", (pid,))
    n = cur.rowcount
    db.execute("DROP TABLE temp.raw")
    return read[0], n


def load_lists(db: sqlite3.Connection, cache: str, pv, pv_n: int) -> dict[str, int]:
    counts = {}
    for cls in LIST_CLASSES:
        path = os.path.join(cache, f"list-{cls}.tsv")
        if not os.path.exists(path):
            continue
        n = 0
        with open(path, encoding="utf-8") as f:
            next(f)
            for line in f:
                p = line.rstrip("\n").split("\t")
                q = _qid(p[0]) if len(p) >= 2 else None
                row = db.execute("SELECT doc_id FROM temp.qdoc WHERE q = ?", (q,)).fetchone() if q is not None else None
                if not row:
                    continue
                d = row[0]
                try:
                    year = int(float(lit(p[1])))
                except ValueError:
                    continue
                if not 1850 <= year <= 2100:
                    continue
                genre = lit(p[2]).strip() if len(p) > 2 else ""
                seq = d & ((1 << TYPE_SHIFT) - 1)
                views = int(pv[seq]) if seq < pv_n else 0
                db.execute("INSERT OR IGNORE INTO lists VALUES (?, ?, ?, ?, ?)", (cls, year, views, d, genre or None))
                n += 1
        counts[cls] = n
    return counts


def cmd_build(registry: str, signals: str, out: str, release: str, cache: str, templates: str, top: int | None) -> None:
    t0 = time.time()
    rows = load_templates(templates)
    missing = [n for n in ["articles"] + [r["prop"] for r in rows] if not os.path.exists(os.path.join(cache, f"{n}.tsv"))]
    if missing:
        sys.exit(f"{cache}: no fetched file for {missing} — run `facts_build.py fetch` first (every property, or the build is partial)")
    if os.path.exists(out):
        os.unlink(out)
    db = sqlite3.connect(out)
    # memory: the page cache is capped (200 MB) and temp tables live on disk beside the output, so the build
    # stays well under 1 GB whatever the row count
    db.executescript("PRAGMA journal_mode = OFF; PRAGMA synchronous = OFF; PRAGMA page_size = 4096; PRAGMA cache_size = -200000; PRAGMA temp_store = FILE;")
    db.execute(f"PRAGMA temp_store_directory = '{os.path.dirname(os.path.abspath(out))}'")
    db.executescript(SCHEMA)
    db.commit()
    log("joining Wikidata items to the registry's articles")
    n_titles, n_items = item_docs(db, cache, registry)
    pv, pv_n = read_pageviews(signals)
    log(f"{n_titles:,} titles, {n_items:,} Wikidata items with an article in the registry, pageviews for {pv_n:,} seqs")
    if top:
        db.execute("CREATE TEMP TABLE head (doc_id INTEGER PRIMARY KEY)")
        db.executemany("INSERT INTO head VALUES (?)", (((1 << TYPE_SHIFT) | s,) for s in top_seqs(pv, pv_n, top)))
        log(f"--top {top:,}: keeping facts of the most viewed articles only")
    pids = write_templates(db, rows)
    total = 0
    for r in rows:
        t1 = time.time()
        read, n = load_property(db, pids[r["prop"]], r["kind"], os.path.join(cache, f"{r['prop']}.tsv"))
        db.execute("UPDATE props SET n = ? WHERE pid = ?", (n, pids[r["prop"]]))
        db.commit()
        total += n
        log(f"  {r['prop']:<6} {r['name']:<20} {read:>10,} read {n:>10,} kept  {time.time() - t1:5.1f}s")
    lists = load_lists(db, cache, pv, pv_n)
    log(f"lists: {lists}")
    stamp(db, release, {"templates_sha256": templates_sha256(templates), "props": str(len(rows)), "rows": str(total),
                        "top": str(top or 0), "lists": json.dumps(lists, sort_keys=True), "registry_titles": str(n_titles), "items": str(n_items)})
    db.commit()
    db.execute("DETACH reg")
    db.execute("VACUUM")
    db.close()
    log(f"{out}: {total:,} facts, {os.path.getsize(out) / 1e6:,.0f} MB in {(time.time() - t0) / 60:.1f} min")


def cmd_rank(path: str, signals: str, top: int) -> None:
    """Per property: of the `top` most-viewed articles, how many carry it and what share of their views they
    hold — the ranking the template table should follow (how many head queries a property can answer)."""
    pv, pv_n = read_pageviews(signals)
    head = {(1 << TYPE_SHIFT) | s: int(pv[s]) for s in top_seqs(pv, pv_n, top)}
    views = sum(head.values()) or 1
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    db.execute("CREATE TEMP TABLE head (doc_id INTEGER PRIMARY KEY, pv INTEGER)")
    db.executemany("INSERT INTO head VALUES (?, ?)", head.items())
    rows = db.execute("""SELECT p.prop, p.name, p.kind, p.n, count(h.doc_id), coalesce(sum(h.pv), 0)
        FROM props p LEFT JOIN (SELECT DISTINCT doc_id, pid FROM facts) f ON f.pid = p.pid LEFT JOIN head h ON h.doc_id = f.doc_id
        GROUP BY p.pid ORDER BY 6 DESC""").fetchall()
    print(f"property\tname\tkind\trows\thead_articles_of_{top}\thead_view_share")
    for prop, name, kind, n, k, v in rows:
        print(f"{prop}\t{name}\t{kind}\t{n}\t{k}\t{v / views:.4f}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch")
    f.add_argument("--cache", required=True)
    f.add_argument("--props", default="")
    f.add_argument("--templates", default=TEMPLATES)
    fl = sub.add_parser("fetch-lists")
    fl.add_argument("--cache", required=True)
    b = sub.add_parser("build")
    b.add_argument("registry")
    b.add_argument("signals")
    b.add_argument("out")
    b.add_argument("release")
    b.add_argument("--cache", required=True)
    b.add_argument("--templates", default=TEMPLATES)
    b.add_argument("--top", type=int, default=0, help="keep only the N most-viewed subject articles (0 = all)")
    r = sub.add_parser("rank")
    r.add_argument("facts")
    r.add_argument("signals")
    r.add_argument("--top", type=int, default=100_000)
    a = ap.parse_args()
    if a.cmd == "fetch":
        cmd_fetch(a.cache, [p for p in a.props.split(",") if p], a.templates)
    elif a.cmd == "fetch-lists":
        cmd_fetch_lists(a.cache)
    elif a.cmd == "build":
        cmd_build(a.registry, a.signals, a.out, a.release, a.cache, a.templates, a.top or None)
    elif a.cmd == "rank":
        cmd_rank(a.facts, a.signals, a.top)


if __name__ == "__main__":
    main()
