"""Books Project Gutenberg holds that Wikidata does not say so about (2026-10-12).

wikilinks.py's "gutenberg" platform is Wikidata's ebook ID (P2034): 1,797 works with an
English article. Gutenberg's own catalog, matched to the articles about written works
by TITLE AND AUTHOR, finds 2,066 more (measured that day; of the works that do carry an
ID, the match found the same one for 1,228 and another edition of the same book for 54).

A book matches when its catalog title is the article's title (without the bracket) and
one of its catalog authors is one of the work's authors (P50): the same name, or the
same surname and year of birth ("Ostrovsky, Aleksandr Nikolaevich, 1823-1886" is Alexander
Ostrovsky; a surname spelt another way, Dostoyevsky / Dostoevsky, is not matched). A translator, editor
or illustrator of the catalog row is not an author. Of several editions the oldest
(lowest number) is kept.

The catalog is the offline feed Gutenberg publishes for this ("The catalog data are
granted to the public domain"); one file a run, nothing else is fetched from
gutenberg.org, whose pages are closed to robots. Only a link is made: no book text.

    search_import.py gutenberg-catalog [--file pg_catalog.csv]   -> wiki_links (platform gutenberg-catalog)

Run after `wiki-links`: the rows live in its table, and a full `wiki-links` run keeps them
(wikilinks.OTHER_LOADERS).
"""

from __future__ import annotations

import csv
import io
import re
import time
import unicodedata
import urllib.request

from wikilinks import COLS, complete, fetch, parse_row
from youtube import USER_AGENT, lit

PLATFORM = "gutenberg-catalog"
CATALOG = "https://www.gutenberg.org/cache/epub/feeds/pg_catalog.csv"
MIN_TEXTS = 40_000              # English texts; 62,000 on 2026-10-12. Fewer = a cut-off download

_FILTER = """?item wdt:P50 ?author .
  ?article schema:about ?item ; schema:isPartOf <https://en.wikipedia.org/> .
  ?author rdfs:label ?al . FILTER(LANG(?al) = "en")
  FILTER NOT EXISTS { ?item wdt:P2034 ?has }"""
# One row per work and author. The first seven columns are wikilinks.parse_row's, with
# the author's name where the value goes.
QUERY = """PREFIX wdt: <http://www.wikidata.org/prop/direct/>
PREFIX schema: <http://schema.org/>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX wikibase: <http://wikiba.se/ontology#>
SELECT ?item ?al (SAMPLE(?en) AS ?name) (SAMPLE(?mul) AS ?mname) (SAMPLE(?desc) AS ?about)
       ?article (MAX(?links) AS ?sitelinks) (MIN(?year) AS ?born) WHERE {
  %s
  OPTIONAL { ?item rdfs:label ?en . FILTER(LANG(?en) = "en") }
  OPTIONAL { ?item rdfs:label ?mul . FILTER(LANG(?mul) = "mul") }
  OPTIONAL { ?item schema:description ?desc . FILTER(LANG(?desc) = "en") }
  OPTIONAL { ?item wikibase:sitelinks ?links }
  OPTIONAL { ?author wdt:P569 ?b . BIND(YEAR(?b) AS ?year) }
} GROUP BY ?item ?al ?article""" % _FILTER
COUNT = """PREFIX wdt: <http://www.wikidata.org/prop/direct/>
PREFIX schema: <http://schema.org/>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
SELECT (COUNT(*) AS ?n) WHERE { SELECT DISTINCT ?item ?al ?article WHERE {
  %s } }""" % _FILTER


def key(s: str) -> str:
    """"Brontë, Charlotte" and "Charlotte Bronte" meet: letters and digits, accents folded."""
    return re.sub(r"[^a-z0-9]", "", unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower())


_ROLE = re.compile(r"\[([^\]]+)\]")
_BORN = re.compile(r",\s*(\d{3,4})\??\s*-")
_DATES = re.compile(r",\s*(?:\d{3,4}\??\s*-|-\s*\d{3,4}|\d{1,2}(?:st|nd|rd|th) cent).*$")


def catalog_author(a: str) -> tuple[str, str, int | None] | None:
    """"Melville, Herman, 1819-1891" -> ("hermanmelville", "melville", 1819). None for a
    translator, editor or illustrator ("… [Translator]") and for an empty name."""
    role = _ROLE.search(a)
    if role and role.group(1).strip().lower() != "author":
        return None
    a = _ROLE.sub("", a).strip()
    m = _BORN.search(a)
    born = int(m.group(1)) if m else None
    a = re.sub(r"\(.*?\)", "", _DATES.sub("", a))          # "(John Fitzgerald)": the name spelt out
    parts = [p.strip() for p in a.split(",") if p.strip()]
    if not parts or not key(parts[0]):
        return None
    return key((parts[1] if len(parts) > 1 else "") + parts[0]), key(parts[0]), born


def load_catalog(f) -> tuple[dict[str, list], int]:
    """pg_catalog.csv -> ({title key: [(ebook id, [catalog_author, …])]}, English texts read).
    English texts only; a title is filed under its first line, and under what stands before
    a subtitle ("Frankenstein; Or, The Modern Prometheus")."""
    cat: dict[str, list] = {}
    n = 0
    for r in csv.DictReader(f):
        if r.get("Type") != "Text" or "en" not in [x.strip() for x in (r.get("Language") or "").split(";")]:
            continue
        try:
            pid = int(r["Text#"])
        except (KeyError, ValueError):
            continue
        n += 1
        authors = [x for x in (catalog_author(a) for a in (r.get("Authors") or "").split(";")) if x]
        title = (r.get("Title") or "").split("\n")[0]
        if not authors:
            continue
        for t in {key(title), key(re.split(r"[:;]| — ", title)[0])}:
            if len(t) >= 3:
                cat.setdefault(t, []).append((pid, authors))
    return cat, n


def match(cat: dict[str, list], wiki_title: str, authors: list[tuple[str, int | None]]) -> int | None:
    """The oldest catalog edition of the work an article is about, or None. authors: the
    work's (name key, year of birth or None) pairs from Wikidata."""
    hits = [pid for pid, cat_authors in cat.get(key(re.sub(r"\s*\(.*\)$", "", wiki_title)), [])
            if any(name == full or (born and year == born and len(sur) >= 3 and name.endswith(sur))
                   for full, sur, born in cat_authors for name, year in authors)]
    return min(hits) if hits else None


def parse_work(line: str):
    """One QLever line -> (the seven wikilinks.parse_row columns with the value left empty,
    (author name key, year of birth)), or None for the header."""
    p = line.rstrip("\n").split("\t")
    if len(p) < 8 or not p[0].startswith("<"):
        return None
    year = lit(p[7]).split("^^")[0].strip('"')
    return [p[0], "", p[2], p[3], p[4], p[5], p[6]], (key(lit(p[1])), int(year) if re.match(r"^-?\d+$", year) else None)


def rows_for(cat: dict[str, list], lines):
    """QLever lines -> (wiki_links rows, works read): one row per matched work."""
    works: dict[str, list] = {}
    for line in lines:
        got = parse_work(line)
        if got:
            cols, author = got
            works.setdefault(cols[0], [cols, []])[1].append(author)
    out = []
    for cols, authors in works.values():
        wiki = parse_row("gutenberg", "\t".join(cols[:1] + ['"1"'] + cols[2:]))     # its title and keys
        pid = match(cat, wiki[6], authors) if wiki and wiki[6] else None
        if pid:
            r = parse_row("gutenberg", "\t".join(cols[:1] + [f'"{pid}"'] + cols[2:]))
            out.append((PLATFORM,) + r[1:])
    return out, len(works)


def log(msg: str) -> None:
    print(f"[gutenberg-catalog] {msg}", flush=True)


def fetch_catalog() -> str:
    req = urllib.request.Request(CATALOG, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=300) as resp:
        return resp.read().decode("utf-8", errors="replace")


def cmd_gutenberg_catalog(conn, path: str | None = None) -> None:
    t0 = time.time()
    if not conn.execute("SELECT to_regclass('wiki_links')").fetchone()[0]:
        raise RuntimeError("no wiki_links table: run `wiki-links` first, the matched books live in it")
    if path:
        with open(path, newline="", encoding="utf-8") as f:
            cat, texts = load_catalog(f)
    else:
        cat, texts = load_catalog(io.StringIO(fetch_catalog(), newline=""))
    if texts < MIN_TEXTS:
        raise RuntimeError(f"the catalog has {texts:,} English texts, under {MIN_TEXTS:,}: a cut-off file, nothing changed")
    log(f"catalog: {texts:,} English texts under {len(cat):,} titles")
    with fetch(PLATFORM, COUNT) as resp:
        expected = int(resp.read().decode().splitlines()[1].strip().strip('"').split("^^")[0].strip('"'))
    with fetch(PLATFORM, QUERY) as resp:
        lines = [line for line in io.TextIOWrapper(resp, encoding="utf-8", errors="replace") if not line.startswith("?")]
    if not complete(len(lines), expected):
        raise RuntimeError(f"QLever sent {len(lines):,} of {expected:,} rows — not replacing the matched books")
    rows, works = rows_for(cat, lines)
    with conn.transaction():
        conn.execute("DELETE FROM wiki_links WHERE platform = %s", (PLATFORM,))
        with conn.cursor().copy(f"COPY wiki_links ({COLS}) FROM STDIN") as cp:
            for r in rows:
                cp.write_row(tuple(v.replace("\x00", "") if isinstance(v, str) else v for v in r))
    log(f"{len(lines):,} of {expected:,} from QLever, {works:,} works with no Gutenberg ID -> {len(rows):,} matched "
        f"in {time.time() - t0:.0f}s")
