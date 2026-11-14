"""YouTube channel directory (2026-09-24): "mkbhd youtube", "@mrbeast",
"linus tech tips youtube channel" answered locally.

Source: Wikidata (CC0) — every item with a YouTube channel ID (P2397), its English
label and description, YouTube handle (P11245), English Wikipedia article, follower
count and sitelink count. SPARQL queries to QLever (~6 s each). No request ever goes
to YouTube: there is no open list of channels beyond Wikidata.

Followers (2026-10-11): the count is written in two places, and the first import read
one. "Social media followers" (P8687) qualified by the channel covered 55,178 of
154,065 channels; "number of subscribers" (P3744) on the channel statement itself
covers most of the rest: 132,030 of 154,925 with both. A channel with no count can
never show for its bare name (search_api YT_BARE_MIN) or be suggested, and sorts last.
Every statement is read, not only the best-ranked of an item (an item with a preferred
channel hid its others: +1,298), except a channel whose only statements are deprecated
(DEAD_QUERY, 438). NOT read: the 9,504 items with a handle and no channel ID. A release
keys a channel by its ID (release_v1 YOUTUBE_SQL), so they could not ship without a
format change, and 42 of them have a follower count.

    search_import.py youtube   -> youtube_channels
"""

from __future__ import annotations

import re
import time
import urllib.parse
import urllib.request

from subreddits import norm

ENDPOINT = "https://qlever.dev/api/wikidata"
USER_AGENT = "logout-search-import/1 (personal search index)"

_PREFIXES = """PREFIX wdt: <http://www.wikidata.org/prop/direct/>
PREFIX p: <http://www.wikidata.org/prop/>
PREFIX pq: <http://www.wikidata.org/prop/qualifier/>
PREFIX ps: <http://www.wikidata.org/prop/statement/>
PREFIX schema: <http://schema.org/>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX wikibase: <http://wikiba.se/ontology#>
"""
_SELECT = """SELECT ?ch (SAMPLE(?label) AS ?name) (SAMPLE(?desc) AS ?about) (SAMPLE(?handle) AS ?h)
       (SAMPLE(?article) AS ?wiki) (MAX(?subs) AS ?followers) (MAX(?links) AS ?sitelinks) WHERE {
"""
_ABOUT = """  OPTIONAL { ?item rdfs:label ?label . FILTER(LANG(?label) = "en") }
  OPTIONAL { ?item schema:description ?desc . FILTER(LANG(?desc) = "en") }
  OPTIONAL { ?article schema:about ?item ; schema:isPartOf <https://en.wikipedia.org/> }
  OPTIONAL { ?item wikibase:sitelinks ?links }
} GROUP BY ?ch"""
# isLiteral: a subscriber count stated as "unknown value" is a blank node, and MAX() ranked it above every number.
# Every channel statement, whatever its rank. A rank test in this query (FILTER NOT EXISTS, or a join on
# wikibase:rank) times out on QLever; the deprecated ones are a query of their own.
QUERY = _PREFIXES + _SELECT + """  ?item p:P2397 ?cst . ?cst ps:P2397 ?ch .
  OPTIONAL { ?item wdt:P11245 ?handle }
  OPTIONAL { { ?cst pq:P3744 ?subs . FILTER(isLiteral(?subs)) } UNION { ?item p:P8687 ?st . ?st pq:P2397 ?ch ; ps:P8687 ?subs } }
""" + _ABOUT
# Channels that are only ever stated as deprecated (a closed or wrong channel): not on any item's best statements.
DEAD_QUERY = _PREFIXES + """SELECT DISTINCT ?ch WHERE {
  ?item p:P2397 ?cst . ?cst ps:P2397 ?ch ; wikibase:rank wikibase:DeprecatedRank .
  MINUS { ?other wdt:P2397 ?ch } }"""
_CHANNEL_ID = re.compile(r"^UC[A-Za-z0-9_-]{22}$")
_HANDLE = re.compile(r"^@?([A-Za-z0-9._-]{3,30})$")
_LIT = re.compile(r'^"(.*)"(?:@[a-z-]+|\^\^.*)?$', re.S)
_WIKI = "https://en.wikipedia.org/wiki/"
# "Kurzgesagt – In a Nutshell" is searched as "kurzgesagt youtube": the part before the
# first separator is a second, weaker key (search-api ranks full-name matches first).
_SHORT = re.compile(r"\s+[–—|:-]\s+|:\s+")


def log(msg: str) -> None:
    print(f"[youtube] {msg}", flush=True)


def lit(v: str) -> str:
    """'"MrBeast"@en' -> 'MrBeast'; '19900000.0' -> '19900000.0'; '' -> ''."""
    v = v.strip()
    m = _LIT.match(v)
    return (m.group(1) if m else v).replace('\\"', '"').replace("\\\\", "\\")


def _num(v: str) -> int | None:
    try:
        return int(float(lit(v)))
    except ValueError:
        return None


def parse_row(line: str):
    """One QLever TSV line -> (channel_id, handle, name, description, wiki_title, followers,
    sitelinks, key, hkey, wkey, skey), or None for the header / an unusable channel value."""
    p = line.rstrip("\n").split("\t")
    if len(p) < 7:
        return None                         # the "?ch …" header fails the channel-id check below
    ch = lit(p[0])
    handle = lit(p[3])
    if not _CHANNEL_ID.match(ch):
        m = _HANDLE.match(ch) if ch.startswith("@") else None
        if not m:
            return None                     # legacy usernames / pasted URLs: 5 of 152,693
        ch, handle = "", handle or m.group(1)
    m = _HANDLE.match(handle) if handle else None
    handle = m.group(1) if m else ""
    if not ch and not handle:
        return None
    name = lit(p[1])[:300]
    wiki = p[4].strip().strip("<>")
    wiki = urllib.parse.unquote(wiki[len(_WIKI):]).replace("_", " ") if wiki.startswith(_WIKI) else ""
    name = name or wiki or handle
    if not name:
        return None
    wkey = norm(re.sub(r"\s*\(.*\)$", "", wiki)) if wiki else ""
    key, skey = norm(name), norm(_SHORT.split(name, 1)[0])
    return (ch or None, handle or None, name, lit(p[2])[:1000], wiki or None,
            _num(p[5]) if p[5].strip() else None, _num(p[6]) or 0,
            key or None, norm(handle) if handle else None, wkey or None,
            skey if skey and skey != key and len(skey) >= 3 else None)


def _ask(query: str) -> list[str]:
    req = urllib.request.Request(ENDPOINT, data=urllib.parse.urlencode({"query": query}).encode(),
                                 headers={"User-Agent": USER_AGENT, "Accept": "text/tab-separated-values"})
    with urllib.request.urlopen(req, timeout=900) as r:
        return r.read().decode("utf-8", "replace").splitlines()


def live_rows(lines: list[str], dead: list[str]) -> list[tuple]:
    """QUERY's answer without the channels DEAD_QUERY names."""
    gone = {lit(x) for x in dead}
    return [r for r in map(parse_row, lines) if r and r[0] not in gone]


def cmd_youtube(conn) -> None:
    t0 = time.time()
    lines = _ask(QUERY)
    dead = _ask(DEAD_QUERY)[1:]
    rows = live_rows(lines, dead)
    if len(lines) - 1 < 100000:
        raise SystemExit(f"only {len(lines) - 1:,} channels from QLever (152,693 on 2026-09-24) — not replacing the table")
    log(f"{len(lines) - 1:,} channels from QLever, {len(dead):,} of them only deprecated -> {len(rows):,} usable "
        f"in {time.time() - t0:.0f}s")
    conn.execute("DROP TABLE IF EXISTS youtube_channels_new")
    conn.execute("CREATE TABLE youtube_channels_new (channel_id text, handle text, name text, description text, "
                 "wiki_title text, followers bigint, sitelinks integer, key text, hkey text, wkey text, skey text)")
    with conn.transaction(), conn.cursor().copy(
            "COPY youtube_channels_new (channel_id, handle, name, description, wiki_title, followers, sitelinks, "
            "key, hkey, wkey, skey) FROM STDIN") as cp:
        for r in rows:
            cp.write_row(tuple(v.replace("\x00", "") if isinstance(v, str) else v for v in r))
    with conn.transaction():
        conn.execute("ALTER TABLE youtube_channels_new ADD COLUMN search_text text GENERATED ALWAYS AS ("
                     "name || ' ' || coalesce(handle, '') || ' ' || coalesce(wiki_title, '') || ' ' "
                     "|| coalesce(description, '')) STORED")
        conn.execute("DROP TABLE IF EXISTS youtube_channels")
        conn.execute("ALTER TABLE youtube_channels_new RENAME TO youtube_channels")
        for col in ("key", "hkey", "wkey", "skey"):
            conn.execute(f"CREATE INDEX youtube_channels_{col} ON youtube_channels ({col})")
        conn.execute("CREATE INDEX youtube_channels_bm25 ON youtube_channels USING bm25 (search_text) "
                     "WITH (text_config = 'english')")
    k = conn.execute("SELECT count(*), count(handle), count(followers), count(*) FILTER (WHERE followers >= 100000) "
                     "FROM youtube_channels").fetchone()
    log(f"{k[0]:,} channels ({k[1]:,} with a handle, {k[2]:,} with a follower count, {k[3]:,} with 100k+) "
        f"in {(time.time() - t0) / 60:.1f} min")
