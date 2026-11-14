#!/usr/bin/env python3
"""Release format v1 — the parts T5 owns (docs/internal/SEARCH-RELEASE-FORMAT.md).

    release_v1.py registry  OUT.sqlite RELEASE_ID [--previous PREV.sqlite] [--from-stage STAGE.sqlite | --own-rank own_rank.tsv] [--places-dir PLACES_RELEASE_DIR | --places-tsv osm.tsv.zst,overture.tsv.zst]
    release_v1.py signals   REGISTRY.sqlite OUT.bin RELEASE_ID --own-rank own_rank.tsv [--no-pageviews] [--allow-inlinks] [--from-dump DIR]
    release_v1.py pictures  REGISTRY.sqlite OUT.sqlite RELEASE_ID
    release_v1.py pictures-from CARD.sqlite REGISTRY.sqlite OUT.sqlite RELEASE_ID   (card_images.py's working file; no Postgres)
    release_v1.py stamp     FILE.sqlite ROLE RELEASE_ID       (application_id, user_version, meta)
    release_v1.py sqlguard  FILE.sqlite...                    (post-build guard for ANY shipped SQLite)
    release_v1.py rowdiff   OLD.sqlite NEW.sqlite             (per-table added/removed rows and bytes)
    release_v1.py sigdump   FILE.bin                          (header of a signals.bin)
    release_v1.py idhash    REG.sqlite|STAGE.sqlite           (per type: count + sha256 of the sorted idents)
    release_v1.py storemap  REL                               (bytes of every Tantivy file kind and SQLite table)
    release_v1.py tvrepro   STOPFILE OUTDIR                   (anchors index built twice: identical?)
    release_v1.py tvnormalize DIR RELEASE_ID ROLE [STOPFILE]  (content-derived segment ids + release.json)
    release_v1.py sha       PATH...                           (sha256 of files / trees)
    release_v1.py tvmonth   INDEX STOPFILE                    (apply month_changes to a copy of text/wiki)
    release_v1.py check     REL [PUBKEY.pem] [--tranco-free]  (the engine-side refusal checks, reference; --tranco-free = the G3 D2 publish gate)
    release_v1.py wikiinput REGISTRY                          (COPY lines for `tantivy-structure build`, registry doc_ids)
    release_v1.py web-input REGISTRY OUT_DIR RELEASE_ID [GOV-STATES.json]  (sites|pages|anchors .rows.tsv.zst: the web indexes'
                                                              build input; with the release's gov-states.json a government page's
                                                              words carry its state)
    release_v1.py anchor-phrases REGISTRY OUT.sqlite RELEASE_ID   (anchor-phrases.sqlite: phrase -> site doc_id, exact_anchor)
    release_v1.py kinds REGISTRY OUT.sqlite RELEASE_ID DUMP.sql.gz [--vecs F] [--embed MODEL_DIR]   (kinds.sqlite: the open kinds tier, D2)

registry.sqlite — ONE doc_id space for everything lookup.sqlite and the text indexes point at.
doc_id = type << 28 | seq (registry_idents.TYPE_SHIFT), so the type is in the id and every type has its own dense block
(signals.bin arrays are dense per type). seq is assigned in a fixed order at the first build
(ORDER BY ... COLLATE "C"); with --previous every surviving document keeps its id, new documents are
appended after the type's highest seq, and removed ids go to `tombstones` (never reused). The types,
their idents and what each registry row holds are in registry_idents.py (one definition, also read by
the lookup builder): 1 wiki, 2 site, 3 page, 4 place, 5 account, 6 youtube, 7 stock, 8 link.

signals.bin — every monthly-changing number, one fixed-width little-endian array per field over its
type's seq range (index = doc_id & SEQ_MASK), replaced whole each month. Header (self-describing,
also its version stamp): b"LOSIGNL1", u32 header length, JSON {format, built_release, fields: [{name,
type, dtype, count, offset, source, licence, note}]}, then the arrays.

Nothing here writes a timestamp into a data file: the same inputs give the same bytes.
"""
import hashlib
import json
import os
import sqlite3
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "importer"))
from own_rank import ORG_BOX, official_key  # noqa: E402  (stdlib only: which official urls stand for their domain)
import export as ex  # noqa: E402  (guards, report, DSNs: one implementation)
import registry_idents as ri  # noqa: E402  (the ident rules, shared with the lookup builder)
import docs_policy  # noqa: E402  (importer/: which homepage captures a release may use, G3 D4c)
import kinds_build  # noqa: E402  (kinds.sqlite: the open kinds tier, its builder and its guard)

FORMAT_VERSION = ex.FORMAT_VERSION
APPLICATION_ID = ex.APPLICATION_ID
TYPE_SHIFT = ri.TYPE_SHIFT
TYPES = ri.TYPES
WIKI_BASE = ri.WIKI_BASE
SIGNALS_MAGIC = b"LOSIGNL1"
# Named signals.bin fields read from build-only tables: the
# wiki_pop TABLE stays refused (G3 D10); only these derived values may ship.
SIGNAL_SQL_FROM_BUILD_ONLY = {
    "wiki_pop": "SELECT page_id, pageviews, sitelinks FROM wiki_pop ORDER BY page_id",
}


OWN_RANK_SOURCE = ("own_rank.tsv (importer/own_rank.py): reciprocal-rank fusion (K=60) of the Common Crawl web-graph rank, the "
                   "pageviews and sitelinks of the article whose official site it is, and the sites linking to it; 0 = none")
OWN_RANK_LICENCE = "Common Crawl web graph (CC ToU) + CC0 (Wikimedia pageviews, Wikidata); M8 layer (CC0)"
import math  # noqa: E402


def rank_pop(r) -> float:
    """suggest.sql's rank_pop on our rank: 1 at rank 1, 0 at 100k or beyond (or no rank)."""
    return 0.0 if not r or r > 100000 else 1 - math.log(r) / math.log(100001)


def article_pop(sl, pv) -> float:
    """suggest.sql's article_pop: sitelinks (250+ languages = 1) blended with readership (500k+ views = 1)."""
    f = min(1.0, math.log(max(sl or 0, 1)) / math.log(250))
    return f if pv is None else 0.5 * f + 0.5 * min(1.0, math.log(1 + pv) / math.log(500001))


def demand_curve(r) -> float:
    """Estimated monthly searches of a site of rank r: suggest.sql's fallback curve 1e7 / r^0.9 (the fitted one needs the
    build table sg_refs); none beyond rank 100k, where suggest.sql has no site item."""
    return 0.0 if not r or r > 100000 else math.exp(math.log(1e7) - 0.9 * math.log(r))


def site_weights(rank, org: bool, apop: float):
    """(pop, score, demand) of a site suggestion from OUR rank, in suggest.sql's rules: an organisation's own site takes the
    better of its rank and its article's fame, any other site its rank plus a small lift (gov.br must not take Brazil's
    fame). The referral searches suggest.sql adds for an organisation (clickstream) are not available here. score is
    log10(1 + demand): the release has no history lift."""
    rp = rank_pop(rank)
    pop = max(rp, apop) if org else min(1.0, rp + 0.1 * apop)
    d = demand_curve(rank)
    return pop, math.log10(1 + d), d


def service_weights(rank):
    """(pop, score, demand) of a subdomain-service suggestion: the site formula on its host rank, demand at a third."""
    d = demand_curve(rank) / 3.0
    return rank_pop(rank), math.log10(1 + d), d


def suggest_rows(rows, own: dict, org_domains, apop_of):
    """suggest_items rows (id, kind, wiki, domain, url, pop, score, demand) -> the same with the weights of every site and
    service item taken from own_rank instead of the build database's Tranco-fitted ones. Everything else passes through.
    apop_of(title) -> article_pop of that article (0.0 when unknown)."""
    for i, kind, wiki, domain, url, p, s_, d in rows:
        if kind == "site":
            host = domain if domain is not None else url_host(url or "")
            p, s_, d = site_weights(own.get(host), domain in org_domains, apop_of(wiki) if wiki else 0.0)
        elif kind == "service":
            p, s_, d = service_weights(own.get(url_host(url or "")))
        yield i, kind, wiki, domain, url, p, s_, d


def read_own_rank(path: str) -> dict:
    """own_rank.tsv (own_rank.py compute: host, domain, own_rank, cc_pos, lists) -> {host: rank}. A malformed line,
    a rank below 1 or a host twice aborts: a rank file is never half-read."""
    own = {}
    with open(path, encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            p = line.rstrip("\n").split("\t")
            if len(p) != 5 or not p[2].isdigit() or int(p[2]) < 1 or p[0] in own:
                sys.exit(f"{path}: line {n} is not 'host domain own_rank cc_pos lists' (or repeats a host) — refusing")
            own[p[0]] = int(p[2])
    if not own:
        sys.exit(f"{path}: no rows — refusing")
    return own


def own_rank_uses_inlinks(path: str) -> bool:
    """True when any host of own_rank.tsv was ranked with the inlink list ('in' among its lists)."""
    with open(path, encoding="utf-8") as f:
        return any("in" in line.rstrip("\n").split("\t")[-1].split(",") for line in f)


# `signals --from-dump DIR`: every row the builder reads from Postgres can instead come from a TSV (COPY text format) per
# SIGNAL_SQL key, written once by a guarded export. The build then needs no Postgres (the profiled build database
# lives on one box; a copy of the previous registry and these files are enough anywhere).
DUMP_TYPES = {"wiki_pop": "iii", "wiki_sites": "isss", "serve_sites": "si", "host_nav": "s", "accounts": "si",
              "youtube": "sii", "stocks": "si", "items": "issssfff"}
SIGNAL_DUMP = None


def read_dump(path: str, types: str):
    """Rows of a COPY-text TSV as tuples typed by `types` (i int, f float, s str); \\N is None."""
    def cell(x, t):
        if x == "\\N":
            return None
        if t == "i":
            return int(x)
        if t == "f":
            return float(x)
        return x.replace("\\t", "\t").replace("\\n", "\n").replace("\\\\", "\\")
    with open(path, encoding="utf-8") as f:
        for line in f:
            p = line.rstrip("\n").split("\t")
            if len(p) != len(types):
                sys.exit(f"{path}: a row has {len(p)} columns, {len(types)} expected - refusing")
            yield tuple(cell(x, t) for x, t in zip(p, types))


def feed(name: str, dsn: str = None, sql: str = None):
    """The rows of SIGNAL_SQL[name]: from the dump when signals runs --from-dump, else from Postgres."""
    if SIGNAL_DUMP:
        return read_dump(os.path.join(SIGNAL_DUMP, name + ".tsv"), DUMP_TYPES[name])
    db, q = SIGNAL_SQL[name]
    return stream(dsn or _dsn(db), sql or q)


def own_rows(own: dict):
    """(host, rank) for every host of the rank file, in host order: site.own_rank and site.nav_rank."""
    return sorted(own.items())


def wiki_site_rows(triples, own: dict):
    """(page_id, domain, url) of wiki_sites -> (page_id, own rank of the official site; 0 when the official url is not the
    front page of the domain itself (own_rank.official_key) or the domain has no rank)."""
    return ((pid, own.get(official_key(url, dom), 0)) for pid, dom, url in triples)


def link_rows(urls, own: dict):
    """host_nav urls -> (url, own rank of the url's host; 0 when it has none): link.nav_rank."""
    return ((u, own.get(url_host(u), 0)) for u in urls)


def url_host(url: str) -> str:
    h = url.split("://", 1)[-1].split("/", 1)[0].split("?", 1)[0].lower()
    return h.rsplit("@", 1)[-1].rsplit(":", 1)[0] if ":" in h else h


def did(type_name: str, seq: int) -> int:
    return (TYPES[type_name] << TYPE_SHIFT) | seq


def pg(dsn):
    c = ex.psycopg.connect(dsn)
    c.execute("SET synchronize_seqscans = off")      # a scan never starts mid-table: stable order
    # The clone has primarycache=metadata: an index scan is one random disk read per row (the
    # signals stage ran 3.6 h on wiki_pop's primary key, 2026-10-02). Sequential scan + sort.
    c.execute("SET enable_indexscan = off")
    c.execute("SET enable_bitmapscan = off")
    c.execute("SET enable_indexonlyscan = off")
    c.execute("SET work_mem = '256MB'")
    return c


def stream(dsn, sql, n=20000):
    with pg(dsn) as c, c.cursor(name="v1") as cur:
        cur.itersize = n
        cur.execute(sql)
        yield from cur


def new_db(path):
    if os.path.exists(path):
        os.remove(path)
    db = sqlite3.connect(path)
    # cache_size: the clone does not cache file data (primarycache=metadata), so SQLite's own
    # page cache is all there is — 2 MB by default made every B-tree insert a disk read.
    db.executescript("PRAGMA journal_mode = OFF; PRAGMA synchronous = OFF; PRAGMA page_size = 4096; "
                     "PRAGMA secure_delete = ON; PRAGMA cache_size = -500000; PRAGMA temp_store = FILE;")
    return db


stamp = ex.stamp            # one implementation (export.py stamps its lookup files the same way)


def finish(db, path: str = None) -> None:
    db.commit()
    if db.execute("PRAGMA freelist_count").fetchone()[0]:
        db.execute("VACUUM")                               # no free pages left behind
    if path is None:
        path = db.execute("PRAGMA database_list").fetchone()[2]
    db.close()
    normalize_header(path)


def normalize_header(path: str) -> None:
    """The SQLite header counts transactions (file change counter, offsets 24 and 92): two builds of
    the same data with a different number of commits would differ in those four+four bytes only.
    Fixed to 1 on the closed file (journal off, nothing reads the counter but a live connection)."""
    with open(path, "r+b") as f:
        f.seek(24)
        f.write(struct.pack(">I", 1))
        f.seek(92)
        f.write(struct.pack(">I", 1))


# --------------------------------------------------------------------------- registry

def _origin_path(url: str):
    i = url.find("://")
    if i < 0:
        return None, None
    j = url.find("/", i + 3)
    return (url, "") if j < 0 else (url[:j], url[j:])


def _templates(rows) -> dict:
    """platform -> (prefix, suffix) that most of its URLs follow around the handle; rows (platform, value, url)."""
    from collections import Counter
    cnt: dict = {}
    for platform, value, url in rows:
        i = url.find(value)
        if i < 0:
            continue
        cnt.setdefault(platform, Counter())[(url[:i], url[i + len(value):])] += 1
    # ties broken by the template itself, not by scan order: the same data gives the same choice
    return {p: max(c.items(), key=lambda kv: (kv[1], kv[0]))[0] for p, c in cnt.items()}


# The universe, one statement per source. Same rules as the lookup builder's staged universe
# (registry_idents.py documents the ident of each type); every statement goes through
# export.check_sql_one, so no refused table, no history, no Jev column, no subreddit, no WDC.
SITE_SQL = """
SELECT DISTINCT ON (host COLLATE "C") host, title, url FROM (
    SELECT domain AS host, title, url, 0 AS pr, fetched_at, crawl FROM docs WHERE error IS NULL AND domain NOT IN (__OUT__)
    UNION ALL SELECT domain, NULL, NULL, 1, NULL, NULL FROM serve_sites
    UNION ALL SELECT domain, NULL, NULL, 1, NULL, NULL FROM wiki_sites WHERE domain IS NOT NULL
    UNION ALL SELECT domain, NULL, NULL, 1, NULL, NULL FROM suggest_items WHERE kind = 'site' AND domain IS NOT NULL AND kind <> 'subreddit'
    UNION ALL SELECT domain, NULL, NULL, 1, NULL, NULL FROM site_quality WHERE source IN ('ut1', 'jev+ut1')
) u WHERE host IS NOT NULL
ORDER BY host COLLATE "C", pr, fetched_at DESC NULLS LAST, crawl COLLATE "C\"""".replace("__OUT__", docs_policy.PG_DOMAINS_OUT)
# an inner page follows what its site SAID on its homepage (G3 D4c, audit 2026-10-22 G4): a site whose latest homepage
# capture says noindex / none ships none of its pages either. A Wayback-only homepage does not (docs_policy.
# PG_DOMAINS_NOINDEX says why). page_docs.domain is the registrable domain.
# A page without words never ships: a stub capture (bot wall, JavaScript shell, redirect note) parses to an
# empty body, and a page nobody can find by any word only takes a slot. ccfetch marks such rows with an error now; the
# condition here also keeps out rows stored before it did.
PAGE_WORDS_SQL = "coalesce(body, '') <> ''"
PAGE_SQL = """
SELECT DISTINCT ON (url COLLATE "C") url, title FROM page_docs WHERE error IS NULL AND __WORDS__ AND domain NOT IN (__OUT__)
ORDER BY url COLLATE "C", fetched_at DESC NULLS LAST, crawl COLLATE "C\"""".replace("__OUT__", docs_policy.PG_DOMAINS_NOINDEX).replace("__WORDS__", PAGE_WORDS_SQL)
# what that rule keeps out, for the stage reports: pages, and how many of them came from an open dump (crawl 'open:…')
PAGES_OUT_SQL = ("SELECT count(DISTINCT url), count(DISTINCT url) FILTER (WHERE left(crawl, 5) = 'open:') FROM page_docs "
                 "WHERE error IS NULL AND domain IN (" + docs_policy.PG_DOMAINS_NOINDEX + ")")
PLACE_SQL = "SELECT osm FROM places WHERE osm NOT LIKE 'wdc:%' ORDER BY osm COLLATE \"C\""
ACCOUNT_SQL = """
SELECT DISTINCT ON (platform COLLATE "C", value COLLATE "C", qid COLLATE "C") platform, value, qid, url FROM wiki_links
WHERE platform IS NOT NULL AND value IS NOT NULL
ORDER BY platform COLLATE "C", value COLLATE "C", qid COLLATE "C", sitelinks DESC NULLS LAST, url COLLATE "C\""""
YOUTUBE_SQL = "SELECT DISTINCT ON (channel_id COLLATE \"C\") channel_id, name FROM youtube_channels ORDER BY channel_id COLLATE \"C\""
STOCK_SQL = "SELECT DISTINCT ON (ticker COLLATE \"C\") ticker, coalesce(label, name) FROM stock_tickers ORDER BY ticker COLLATE \"C\""
LINK_HOSTNAV_SQL = "SELECT url, title FROM host_nav WHERE url IS NOT NULL"
LINK_ITEMS_SQL = """SELECT url, name FROM suggest_items WHERE kind <> 'subreddit' AND url IS NOT NULL
    AND kind <> 'wiki' AND NOT (kind = 'site' AND domain IS NOT NULL)"""
# kind='wiki' items whose title is no article would be links too: found in SQLite against the
# staged wiki titles (the registry holds them), not by a third scan of wiki_docs
LINK_WIKIITEMS_SQL = "SELECT wiki, url, name FROM suggest_items WHERE kind = 'wiki' AND kind <> 'subreddit'"
# Stack Exchange attribution (owner decision 2026-10-22): who asked each question and who wrote the answer whose words
# the page is indexed by (importer/stackexchange.py se-authors). The url is the page's, as openpages.se_page builds it.
AUTHORS_SQL = """SELECT 'https://' || site || '/questions/' || qid, role, user_id, display_name FROM se_authors
ORDER BY 1 COLLATE "C", 2"""
REGISTRY_SQL = [SITE_SQL, PAGE_SQL, PAGES_OUT_SQL, AUTHORS_SQL, PLACE_SQL, ACCOUNT_SQL, YOUTUBE_SQL, STOCK_SQL, LINK_HOSTNAV_SQL, LINK_ITEMS_SQL,
                LINK_WIKIITEMS_SQL, "SELECT page_id, title FROM wiki_docs ORDER BY page_id"]
_QID = None


def _qid_number(qid):
    """'Q123' -> 123; None -> None; anything else aborts (the ident would not rebuild)."""
    import re
    global _QID
    if _QID is None:
        _QID = re.compile(r"^Q([0-9]+)$")
    if qid is None:
        return None
    m = _QID.match(qid)
    if not m:
        sys.exit(f"account qid {qid!r} is not Q+digits — the registry ident rule needs that; refusing")
    return int(m.group(1))


class PgSource:
    """The universe from the profiled Postgres (the release pipeline: indexhost, testbox)."""
    name = "postgres"

    def __init__(self, dsn, own=None):
        self.d = dsn
        # nav_sites is Tranco's list and is no longer read: the navigable hosts are own_rank.tsv's (G3 D2)
        self.own = own if own is not None else sys.exit("registry from Postgres needs --own-rank own_rank.tsv "
                                                        "(nav_sites is Tranco-derived and is no longer read) — refusing")

    def check(self):
        ex.check_source()
        for q in REGISTRY_SQL:
            ex.check_sql_one(q)

    def wiki(self):
        return stream(self.d, "SELECT page_id, title FROM wiki_docs ORDER BY page_id")

    def sites(self):
        seen = set()
        for row in stream(self.d, SITE_SQL):
            seen.add(row[0])
            yield row
        for host in sorted(self.own):
            if host not in seen:
                yield host, None, None

    def pages(self):
        return stream(self.d, PAGE_SQL)

    def pages_out(self) -> dict:
        (n, n_open), = list(stream(self.d, PAGES_OUT_SQL))          # one row; read to the end so the cursor closes
        return {"pages": n, "of_which_open_dump": n_open}

    def authors(self):
        with pg(self.d) as c:
            have = c.execute("SELECT to_regclass('se_authors')").fetchone()[0] is not None
        return stream(self.d, AUTHORS_SQL) if have else iter(())

    def places(self):
        return (r[0] for r in stream(self.d, PLACE_SQL))

    def accounts(self):
        return stream(self.d, ACCOUNT_SQL)

    def youtube(self):
        return stream(self.d, YOUTUBE_SQL)

    def stocks(self):
        return stream(self.d, STOCK_SQL)

    def hostnav(self):
        # host_nav's rows come from Tranco's subdomain list: only the hosts of own_rank.tsv are kept
        return (r for r in stream(self.d, LINK_HOSTNAV_SQL) if url_host(r[0]) in self.own)

    def items(self):
        return stream(self.d, LINK_ITEMS_SQL)

    def wikiitems(self):
        return stream(self.d, LINK_WIKIITEMS_SQL)

    def templates(self):
        return _templates(stream(self.d, "SELECT platform, value, url FROM wiki_links "
                                         "WHERE url IS NOT NULL AND value IS NOT NULL"))


class StageSource:
    """The same universe from the lookup builder's staged file (apps/search/tools/sqlite_lookups/
    build.py: tables s_*), for a machine that has the stage and no Postgres (buildbox). That builder
    staged through the same release guards. Pages come from s_pages (rc5 W5); an older stage has none."""
    name = "stage"

    def __init__(self, path):
        self.c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        self.c.execute("PRAGMA cache_size = -400000")
        self.c.execute("PRAGMA temp_store = FILE")

    def check(self):
        have = {r[0] for r in self.c.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        need = {"s_wiki", "s_docs", "s_serve", "s_nav", "s_wsites", "s_items", "s_unsafe", "s_hostnav", "s_links",
                "s_yt", "s_stock", "s_places", "s_own"}
        if need - have:
            sys.exit(f"stage file lacks {sorted(need - have)} (s_own: a stage built before G3 D2 read Tranco's nav_sites) — refusing")

    def _q(self, sql):
        return self.c.execute(sql)

    def wiki(self):
        return self._q("SELECT page_id, title FROM s_wiki ORDER BY page_id")

    def sites(self):
        return self._q("""SELECT host, title, url FROM (SELECT host, title, url, row_number() OVER (PARTITION BY host ORDER BY pr) rn FROM (
            SELECT domain AS host, title, url, 0 AS pr FROM s_docs
            UNION ALL SELECT domain, NULL, NULL, 1 FROM s_serve
            UNION ALL SELECT host, NULL, NULL, 1 FROM s_nav
            UNION ALL SELECT domain, NULL, NULL, 1 FROM s_wsites WHERE domain IS NOT NULL
            UNION ALL SELECT domain, NULL, NULL, 1 FROM s_items WHERE kind = 'site' AND domain IS NOT NULL
            UNION ALL SELECT domain, NULL, NULL, 1 FROM s_unsafe) WHERE host IS NOT NULL) WHERE rn = 1 ORDER BY host""")

    def pages(self):
        # rc5 W5: a stage made before then has no s_pages, and gives no page rows as before
        if not self._q("SELECT 1 FROM sqlite_master WHERE name = 's_pages'").fetchone():
            return iter(())
        return self._q("SELECT url, title FROM s_pages ORDER BY url")

    def pages_out(self):
        # the lookup builder drops them from s_pages (stage_capture_policy) and records the count; an older stage did not
        if not self._q("SELECT 1 FROM sqlite_master WHERE name = 's_policy'").fetchone():
            return None
        v = dict(self._q("SELECT key, value FROM s_policy")).get("pages_capture_out")
        return json.loads(v) if v else None

    def authors(self):
        # a stage made before the attribution has no s_se_authors: no authors rows, as before
        if not self._q("SELECT 1 FROM sqlite_master WHERE name = 's_se_authors'").fetchone():
            return iter(())
        return self._q("SELECT url, role, user_id, name FROM s_se_authors ORDER BY url, role")

    def places(self):
        return (r[0] for r in self._q("SELECT osm FROM s_places ORDER BY osm"))

    def accounts(self):
        return self._q("""SELECT platform, value, qid, url FROM (SELECT platform, value, qid, url, row_number() OVER (
            PARTITION BY platform, value, qid ORDER BY sitelinks DESC NULLS LAST, url) rn FROM s_links
            WHERE platform IS NOT NULL AND value IS NOT NULL) WHERE rn = 1 ORDER BY platform, value, qid""")

    def youtube(self):
        return self._q("SELECT channel_id, name FROM s_yt ORDER BY channel_id")

    def stocks(self):
        return self._q("SELECT ticker, coalesce(label, name) FROM s_stock ORDER BY ticker")

    def hostnav(self):
        return self._q("SELECT url, title FROM s_hostnav WHERE url IS NOT NULL")

    def items(self):
        return self._q("SELECT url, name FROM s_items WHERE url IS NOT NULL AND kind <> 'wiki' "
                       "AND NOT (kind = 'site' AND domain IS NOT NULL)")

    def wikiitems(self):
        return self._q("SELECT wiki, url, name FROM s_items WHERE kind = 'wiki'")

    def templates(self):
        return _templates(self._q("SELECT platform, value, url FROM s_links WHERE url IS NOT NULL AND value IS NOT NULL"))


def places_from_tsv(paths: list, expect_rows: dict = None):
    """Place idents from the release's dated place files (utility/places/build.sh: places-osm.tsv.zst,
    places-overture.tsv.zst; first column = the id: an OSM id like node/123 or ov:<GERS uuid>).
    Sorted; a wdc: id aborts (G3 D3); a duplicate id aborts; a first line that is no id is a header.
    expect_rows: {file name: rows the manifest promises}."""
    import re
    import subprocess
    ok = re.compile(r"^(node|way|relation)/[0-9]+$|^ov:\S+$")
    ids = set()
    for path in paths:
        if path.endswith(".zst"):
            proc = subprocess.Popen(["zstd", "-dc", path], stdout=subprocess.PIPE)
            lines, closer = proc.stdout, proc
        else:
            lines, closer = open(path, "rb"), None
        rows = 0
        for n, raw in enumerate(lines):
            first = raw.split(b"\t", 1)[0].decode("utf-8").rstrip("\n")
            if first.startswith("wdc:"):
                sys.exit(f"{path}: line {n + 1} is a wdc: place ({first!r}) — WDC never ships (G3 D3); refusing")
            if not ok.match(first):
                if n == 0:
                    continue                                     # header
                sys.exit(f"{path}: line {n + 1}: first column {first!r} is no OSM or ov: id — refusing")
            if first in ids:
                sys.exit(f"{path}: line {n + 1}: place id {first!r} appears twice — refusing")
            ids.add(first)
            rows += 1
        if closer is not None:
            if closer.wait() != 0:
                sys.exit(f"zstd failed on {path}")
        else:
            lines.close()
        want = (expect_rows or {}).get(os.path.basename(path))
        if want is not None and want != rows:
            sys.exit(f"{path}: {rows} rows, the manifest says {want} — refusing")
    return sorted(ids)                     # str order = byte order for these ASCII ids


def places_from_release(directory: str):
    """The registry's place idents from a dated places release folder: manifest.json's sha256, byte
    size and row count of places-osm.tsv.zst and places-overture.tsv.zst are verified first."""
    m = json.load(open(os.path.join(directory, "manifest.json")))
    if m.get("columns", {}).get("places", [None])[0] != "osm":
        sys.exit(f"{directory}/manifest.json: the first places column is not the id ('osm') — refusing")
    paths, rows = [], {}
    for name in ("places-osm.tsv.zst", "places-overture.tsv.zst"):
        f = m["files"].get(name)
        if f is None:
            sys.exit(f"{directory}/manifest.json lists no {name} — refusing")
        path = os.path.join(directory, name)
        if os.path.getsize(path) != f["bytes"] or ex.sha256_file(path) != f["sha256"]:
            sys.exit(f"{path}: size or sha256 differs from manifest.json — refusing")
        paths.append(path)
        rows[name] = f["rows"]
    return places_from_tsv(paths, rows), {"release": m.get("release"), "overture": m["sources"].get("overture", {}).get("release")}


def cmd_registry(out: str, release: str, previous: str = None, stage_file: str = None, places_tsv: list = None,
                 places_dir: str = None, own_rank_file: str = None) -> None:
    t0 = time.time()
    src = StageSource(stage_file) if stage_file else PgSource(ex.PG_DSN, read_own_rank(own_rank_file) if own_rank_file else None)
    src.check()
    place_note = None
    if places_dir:
        pl, place_note = places_from_release(places_dir)
        src.places = lambda: iter(pl)
    elif places_tsv:
        src.places = lambda: iter(places_from_tsv(places_tsv))
    db = new_db(out)
    # ident is only needed to match --previous; without it the stage stays 1 GB smaller
    # the working tables live in a private temporary database: the registry's own schema is then
    # created in the same steps whatever the mode, so the same data gives the same bytes (SQLite's
    # schema_version counts every CREATE/DROP in the file, including an index that was dropped again)
    db.execute("ATTACH DATABASE '' AS stg")
    db.execute("PRAGMA stg.cache_size = -500000")
    db.execute("PRAGMA stg.journal_mode = OFF")
    db.execute("PRAGMA stg.synchronous = OFF")
    db.executescript("""
      CREATE TABLE stg.stage (type INTEGER, ident TEXT, ext INTEGER, origin TEXT, path TEXT, title TEXT, alt TEXT, ord INTEGER);
    """)
    ins = "INSERT INTO stage VALUES (?,?,?,?,?,?,?,?)"
    counts, skipped = {}, {}
    want_ident = previous is not None

    def load(type_name, rows):
        n = 0
        batch = []
        t = TYPES[type_name]
        for ident, ext, origin, path, title, alt in rows:
            if ext is None and path is None and alt is None:     # a source row with no key: no thing
                skipped[type_name] = skipped.get(type_name, 0) + 1
                continue
            batch.append((t, ident if want_ident else None, ext, origin, path, title, alt, n))
            n += 1
            if len(batch) >= 50000:
                db.executemany(ins, batch)
                batch.clear()
        if batch:
            db.executemany(ins, batch)
        counts[type_name] = n

    load("wiki", ((str(pid), pid, None, None, title, None) for pid, title in src.wiki()))

    def sites():
        for host, title, url in src.sites():
            alt = url if url is not None and url != f"https://{host}/" else None
            yield host, None, None, host, title, alt
    load("site", sites())

    def pages():
        for url, title in src.pages():
            o, p = _origin_path(url)
            yield url, None, o, p, title, (None if o else url)
    load("page", pages())
    # place rows: the OSM id only — names, addresses etc. stay in places-osm.sqlite (ODbL, G3 D5/D6)
    load("place", ((osm, None, None, osm, None, None) for osm in src.places()))
    tpl = src.templates()

    def accounts():
        for platform, value, qid, url in src.accounts():
            n = _qid_number(qid)
            pre, suf = tpl.get(platform, ("", ""))
            alt = None if url is None or url == pre + value + suf else url
            yield (platform + "\t" + value + "\t" + (f"Q{n}" if n is not None else ""), n, platform, value, None, alt)
    load("account", accounts())
    load("youtube", ((cid, None, None, cid, name, None) for cid, name in src.youtube()))
    load("stock", ((tk, None, None, tk, title, None) for tk, title in src.stocks()))

    # links: host_nav first (its title wins), then the suggest items that are neither articles nor
    # sites; kind='wiki' items without an article join them (none today: counted)
    db.execute("CREATE TEMP TABLE lk (url TEXT, title TEXT, pr INTEGER)")
    db.executemany("INSERT INTO lk VALUES (?,?,0)", src.hostnav())
    db.executemany("INSERT INTO lk VALUES (?,?,1)", src.items())
    db.execute("CREATE TEMP TABLE iw (wiki TEXT, url TEXT, name TEXT)")
    db.executemany("INSERT INTO iw VALUES (?,?,?)", ((w, u, n) for w, u, n in src.wikiitems() if u is not None))
    db.execute("CREATE INDEX stg.stage_title ON stage (title) WHERE type = 1")
    orphan = db.execute("""INSERT INTO lk SELECT url, name, 2 FROM iw
        WHERE NOT EXISTS (SELECT 1 FROM stage s WHERE s.type = 1 AND s.title = iw.wiki)""").rowcount
    db.execute("DROP TABLE iw")
    db.execute("DROP INDEX stg.stage_title")

    def links():
        for url, title in db.execute("""SELECT url, title FROM (SELECT url, title, row_number() OVER
                (PARTITION BY url ORDER BY pr, rowid) rn FROM lk) WHERE rn = 1 ORDER BY url""").fetchall():
            o, p = _origin_path(url)
            yield url, None, o, p, title, (None if o else url)
    load("link", links())
    db.execute("DROP TABLE lk")
    db.commit()

    # dictionaries: origins (pages, links) and platforms, stable across months like the ids
    db.executescript("""
      CREATE TABLE hosts (id INTEGER PRIMARY KEY, origin TEXT UNIQUE NOT NULL);
      CREATE TABLE platforms (id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, prefix TEXT, suffix TEXT);
      CREATE TABLE registry (doc_id INTEGER PRIMARY KEY, ext INTEGER, host_id INTEGER, path TEXT, title TEXT, alt TEXT);
      CREATE TABLE tombstones (doc_id INTEGER PRIMARY KEY, removed_release TEXT);
      CREATE TABLE authors (doc_id INTEGER NOT NULL, role INTEGER NOT NULL, user_id INTEGER, name TEXT,
                            PRIMARY KEY (doc_id, role)) WITHOUT ROWID;
    """)
    prev_note = None
    if previous:
        db.execute("ATTACH ? AS prev", (previous,))
        lay = dict(db.execute("SELECT key, value FROM prev.meta")).get("layout")
        if lay != ri.LAYOUT:
            sys.exit(f"{previous}: registry layout {lay!r}, this build writes {ri.LAYOUT!r}: build without --previous")
        db.executescript("""
          INSERT INTO hosts SELECT id, origin FROM prev.hosts;
          INSERT INTO platforms SELECT id, name, prefix, suffix FROM prev.platforms;
          INSERT INTO tombstones SELECT doc_id, removed_release FROM prev.tombstones;
        """)
    db.execute("""INSERT INTO hosts (origin) SELECT DISTINCT origin FROM stage WHERE type IN (3, 8) AND origin IS NOT NULL
          AND origin NOT IN (SELECT origin FROM hosts) ORDER BY origin""")
    for (name,) in db.execute("SELECT DISTINCT origin FROM stage WHERE type = 5 ORDER BY origin").fetchall():
        if not db.execute("SELECT 1 FROM platforms WHERE name = ?", (name,)).fetchone():
            pre, suf = tpl.get(name, ("", ""))
            db.execute("INSERT INTO platforms (name, prefix, suffix) VALUES (?, ?, ?)", (name, pre, suf))
    if previous:
        db.execute("CREATE INDEX stg.stage_ident ON stage (type, ident)")
        # identity of every previous doc, rebuilt from its own columns by the ONE rule (registry_idents)
        db.executescript(f"""
          CREATE TEMP TABLE prev_ident AS {ri.ident_select('prev.registry', 'prev.hosts', 'prev.platforms')};
          CREATE INDEX prev_ident_i ON prev_ident (type, ident);
        """)
        db.executescript(f"""
          CREATE TEMP TABLE ids AS
            SELECT s.rowid AS srow, s.type, s.ord, pi.doc_id FROM stage s
            LEFT JOIN prev_ident pi ON pi.type = s.type AND pi.ident = s.ident;
        """)
        # new docs: after the type's highest seq ever used (live or tombstoned)
        for t in sorted(TYPES.values()):
            hi = db.execute(f"""SELECT max(m) FROM (SELECT max(doc_id) AS m FROM prev.registry WHERE doc_id >> {TYPE_SHIFT} = ?
                                UNION ALL SELECT max(doc_id) FROM prev.tombstones WHERE doc_id >> {TYPE_SHIFT} = ?)""",
                            (t, t)).fetchone()[0]
            nxt = (hi + 1) if hi is not None else (t << TYPE_SHIFT)
            db.execute("DROP TABLE IF EXISTS newids")
            db.execute("""CREATE TEMP TABLE newids AS SELECT srow, row_number() OVER (ORDER BY ord) AS rn
                          FROM ids WHERE type = ? AND doc_id IS NULL""", (t,))
            db.execute(f"UPDATE ids SET doc_id = {nxt} - 1 + n.rn FROM newids n WHERE n.srow = ids.srow")
        db.execute("""INSERT INTO tombstones SELECT doc_id, ? FROM prev_ident
                      WHERE doc_id NOT IN (SELECT doc_id FROM ids)""", (release,))
        moved = db.execute("SELECT count(*) FROM ids i JOIN prev_ident p USING (doc_id)").fetchone()[0]
        prev_note = {"kept": moved, "new": sum(counts.values()) - moved,
                     "tombstoned_now": db.execute("SELECT count(*) FROM tombstones WHERE removed_release = ?",
                                                  (release,)).fetchone()[0]}
    else:
        db.execute(f"CREATE TEMP TABLE ids AS SELECT rowid AS srow, type, ord, (type << {TYPE_SHIFT}) | ord AS doc_id FROM stage")
    db.execute("CREATE INDEX ids_srow ON ids (srow)")
    # a seq past the type's range would spill into the next type's ids
    for t, mx in db.execute(f"SELECT type, max(doc_id - (type << {TYPE_SHIFT})) FROM ids GROUP BY type"):
        if mx >= 1 << TYPE_SHIFT:
            sys.exit(f"type {t} ran out of ids ({mx})")
    db.execute("""INSERT INTO registry
        SELECT i.doc_id, s.ext,
               CASE WHEN s.type IN (3, 8) THEN h.id WHEN s.type = 5 THEN p.id END,
               s.path, s.title, s.alt
        FROM stage s JOIN ids i ON i.srow = s.rowid
        LEFT JOIN hosts h ON h.origin = s.origin AND s.type IN (3, 8)
        LEFT JOIN platforms p ON p.name = s.origin AND s.type = 5
        ORDER BY i.doc_id""")
    n_authors = load_authors(db, src.authors())
    db.execute("DROP TABLE stg.stage")
    if previous:
        db.commit()
        db.execute("DETACH prev")
    stamp(db, "registry", release, {"layout": ri.LAYOUT, "types": json.dumps(TYPES, sort_keys=True),
                                    "type_shift": TYPE_SHIFT, "wiki_base": WIKI_BASE})
    finish(db)
    ex.report("registry", t0, path=out, counts=counts, bytes=os.path.getsize(out), previous=prev_note,
              platforms_with_template=len(tpl), wiki_items_without_article=orphan, source=src.name,
              skipped_without_key=skipped, places_release=place_note, pages_capture_out=src.pages_out(), authors=n_authors)


AUTHOR_ROLES = (1, 2)          # 1 asked the question, 2 wrote the answer the page's words come from


def load_authors(db, rows) -> dict:
    """Stack Exchange credits -> registry `authors`, keyed by the PAGE's doc_id: (url, role, user_id, name) rows are
    matched to the staged page whose origin + path is that url. A credit whose page is not in the registry (cut by the
    site rank, a block list) is dropped and counted. user_id is kept only as a positive integer (the profile link is
    https://<the page's host>/users/<user_id>); a name is trimmed text or NULL (the reader says "a deleted user").
    Needs stg.stage and ids (cmd_registry, before stg.stage is dropped)."""
    db.execute("CREATE TABLE stg.au (url TEXT, role INTEGER, user_id INTEGER, name TEXT)")
    bad = 0

    def clean():
        nonlocal bad
        for url, role, uid, name in rows:
            if role not in AUTHOR_ROLES or not isinstance(url, str):
                bad += 1
                continue
            uid = uid if isinstance(uid, int) and not isinstance(uid, bool) and uid > 0 else None
            name = (name.replace("\x00", "").strip() or None) if isinstance(name, str) else None
            yield url, role, uid, name
    db.executemany("INSERT INTO stg.au VALUES (?,?,?,?)", clean())
    n_in = db.execute("SELECT count(*) FROM stg.au").fetchone()[0]
    db.execute("CREATE INDEX stg.au_url ON au (url)")
    db.execute("""INSERT OR IGNORE INTO authors
        SELECT i.doc_id, a.role, a.user_id, a.name FROM stg.stage s JOIN ids i ON i.srow = s.rowid
        JOIN stg.au a ON a.url = s.origin || s.path
        WHERE s.type = 3 AND s.origin IS NOT NULL
        ORDER BY i.doc_id, a.role, a.user_id, a.name""")
    db.execute("DROP TABLE stg.au")
    n = db.execute("SELECT count(*) FROM authors").fetchone()[0]
    return {"rows": n, "source_rows": n_in, "dropped_not_in_registry": n_in - n, "refused": bad,
            "pages": db.execute("SELECT count(DISTINCT doc_id) FROM authors").fetchone()[0],
            "without_profile": db.execute("SELECT count(*) FROM authors WHERE user_id IS NULL").fetchone()[0]}


# --------------------------------------------------------------------------- signals.bin

SIGNAL_SQL = {   # (database, statement) per source; the first column is the key
    "wiki_pop": ("search", SIGNAL_SQL_FROM_BUILD_ONLY["wiki_pop"]),
    # the site rank is our own (own_rank.tsv, G3 D2): these two read only the key (the official site's domain, the link's
    # url); nothing Tranco-derived (nav_sites, tranco_rank, host_nav.rank) is selected
    "wiki_sites": ("serve", "SELECT page_id, domain, url, coalesce(box, '') FROM wiki_sites ORDER BY page_id"),
    "serve_sites": ("serve", 'SELECT domain, cc_hc_pos FROM serve_sites ORDER BY domain COLLATE "C"'),
    "host_nav": ("serve", 'SELECT url FROM host_nav WHERE url IS NOT NULL GROUP BY url ORDER BY url COLLATE "C"'),
    "accounts": ("serve", "SELECT id, sl FROM (SELECT platform || E'\\t' || value || E'\\t' || coalesce(qid, '') AS id, "
                          "max(sitelinks) AS sl FROM wiki_links WHERE platform IS NOT NULL AND value IS NOT NULL "
                          'GROUP BY 1) x ORDER BY id COLLATE "C"'),
    "youtube": ("serve", 'SELECT channel_id, max(followers), max(sitelinks) FROM youtube_channels GROUP BY channel_id ORDER BY channel_id COLLATE "C"'),
    "stocks": ("serve", 'SELECT ticker, max(sitelinks) FROM stock_tickers GROUP BY ticker ORDER BY ticker COLLATE "C"'),
    "items": ("serve", "SELECT id, kind, wiki, domain, url, pop, score, demand FROM suggest_items WHERE kind <> 'subreddit' ORDER BY id DESC"),
}


def cmd_signals(registry: str, out: str, release: str, *flags) -> None:
    import numpy as np
    t0 = time.time()
    if "--own-rank" not in flags or flags.index("--own-rank") + 1 >= len(flags):
        sys.exit("signals needs --own-rank own_rank.tsv (importer/own_rank.py): the site rank is our own, not Tranco's (G3 D2)")
    global SIGNAL_DUMP
    if "--from-dump" in flags:
        SIGNAL_DUMP = flags[flags.index("--from-dump") + 1]
    own_file = flags[flags.index("--own-rank") + 1]
    own = read_own_rank(own_file)
    if own_rank_uses_inlinks(own_file) and "--allow-inlinks" not in flags:
        sys.exit(f"{own_file}: the rank was fused with the inlink counts (site_inlinks, a build-only table): shipping a rank "
                 "derived from it needs the owner's OK (G3 D10). Rebuild it with own_rank.py compute --no-inlinks, "
                 "or pass --allow-inlinks once the owner has said yes — refusing")
    if not SIGNAL_DUMP:          # a dump was written by a guarded export of the same statements
        ex.check_source()
        ex.check_history(ex.PG_SEARCH_DSN, ["wiki_pop"])
    for name, (_db, q) in SIGNAL_SQL.items():
        ex.check_sql_one(q, allow_tables=("wiki_pop",) if name == "wiki_pop" else ())
    with_pv = "--no-pageviews" not in flags
    tmp = os.path.join(os.path.dirname(os.path.abspath(out)), f".signals-{os.getpid()}.tmp")
    sg = sqlite3.connect(tmp)
    sg.executescript("PRAGMA temp_store = FILE; PRAGMA cache_size = -400000; PRAGMA journal_mode = OFF; PRAGMA synchronous = OFF;")
    try:
        _signals(sg, np, registry, out, release, with_pv, t0, own)
    finally:
        sg.close()
        for suffix in ("", "-journal"):
            if os.path.exists(tmp + suffix):
                os.remove(tmp + suffix)


def _signals(sg, np, registry, out, release, with_pv, t0, own):
    mask = ri.SEQ_MASK
    ri.idmap(sg, registry, "idmap", types=(TYPES["site"], TYPES["account"], TYPES["youtube"], TYPES["stock"], TYPES["link"]))
    sg.execute("ATTACH ? AS reg", (registry,))
    size = {t: (sg.execute(f"SELECT max(doc_id & {mask}) FROM reg.registry WHERE doc_id >> {TYPE_SHIFT} = ?",
                           (v,)).fetchone()[0] or -1) + 1 for t, v in TYPES.items()}
    # wiki: page_id -> seq (sorted arrays; 6.8M ids stay small) and title -> seq of the LOWEST page_id
    pids, seqs = [], []
    cur = sg.execute(f"SELECT ext, doc_id & {mask} FROM reg.registry WHERE doc_id >> {TYPE_SHIFT} = 1 ORDER BY ext")
    while True:
        b = cur.fetchmany(500000)
        if not b:
            break
        a = np.array(b, dtype=np.int64)
        pids.append(a[:, 0])
        seqs.append(a[:, 1])
    wk_pid, wk_seq = np.concatenate(pids), np.concatenate(seqs)
    sg.execute(f"""CREATE TABLE wtitle (title TEXT PRIMARY KEY, seq INTEGER) WITHOUT ROWID""")
    sg.execute(f"""INSERT INTO wtitle SELECT title, seq FROM (SELECT title, doc_id & {mask} AS seq,
                   row_number() OVER (PARTITION BY title ORDER BY ext) rn FROM reg.registry
                   WHERE doc_id >> {TYPE_SHIFT} = 1 AND title IS NOT NULL) WHERE rn = 1 ORDER BY title""")
    fields, arrays, unmatched = [], [], {}

    def field(name, type_name, dtype, source, licence, note=""):
        a = np.zeros(size[type_name], dtype=dtype)
        fields.append({"name": name, "type": type_name, "dtype": np.dtype(dtype).str, "count": len(a),
                       "source": source, "licence": licence, "note": note})
        arrays.append(a)
        return a

    def clamp(col, dtype):
        v = np.array([0 if x is None else x for x in col], dtype=np.int64)
        return np.minimum(v, np.iinfo(dtype).max).astype(dtype)

    def wiki_rows(src):
        """rows (page_id, v...) -> (seq array, columns) for the pages the registry knows."""
        for batch in _batches(src):
            keys = np.array([r[0] for r in batch], dtype=np.int64)
            idx = np.minimum(np.searchsorted(wk_pid, keys), len(wk_pid) - 1)
            ok = wk_pid[idx] == keys
            yield wk_seq[idx][ok], [[r[i] for r, k in zip(batch, ok) if k] for i in range(1, len(batch[0]))], int((~ok).sum())

    def ident_rows(dbname, sql, type_name, ncols, label, rows=None):
        """rows (ident, v...) -> joined to the registry through idmap inside SQLite."""
        sg.execute("DROP TABLE IF EXISTS src")
        sg.execute(f"CREATE TABLE src (k TEXT PRIMARY KEY, {', '.join(f'v{i}' for i in range(ncols))}) WITHOUT ROWID")
        # a NULL key (youtube_channels has a channel_id IS NULL group) can match no registry ident: skipped
        sg.executemany(f"INSERT OR REPLACE INTO src VALUES (?{',?' * ncols})", (r for r in (rows if rows is not None else feed(next(k for k, (_d, q) in SIGNAL_SQL.items() if q == sql))) if r[0] is not None))
        n_src = sg.execute("SELECT count(*) FROM src").fetchone()[0]
        cur = sg.execute(f"SELECT m.doc_id & {mask}, {', '.join(f's.v{i}' for i in range(ncols))} FROM src s "
                         f"JOIN idmap m ON m.type = {TYPES[type_name]} AND m.ident = s.k ORDER BY m.doc_id")
        got = 0
        while True:
            b = cur.fetchmany(500000)
            if not b:
                break
            got += len(b)
            yield np.array([r[0] for r in b], dtype=np.int64), [[r[i] for r in b] for i in range(1, ncols + 1)]
        unmatched[label] = n_src - got

    # ---- wiki
    if with_pv:
        pv = field("wiki.pageviews", "wiki", "<u4", "wiki_pop.pageviews (monthly)", "CC0 (Wikimedia pageviews)")
    sl = field("wiki.sitelinks", "wiki", "<u2", "wiki_pop.sitelinks", "CC0 (Wikidata)")
    miss = 0
    for s_, cols, m_ in wiki_rows(feed("wiki_pop", ex.PG_SEARCH_DSN)):
        miss += m_
        if with_pv:
            pv[s_] = clamp(cols[0], np.uint32)
        sl[s_] = clamp(cols[1], np.uint16)
    unmatched["wiki_pop"] = miss
    sr = field("wiki.site_rank", "wiki", "<u4", "own_rank of the article's official site (wiki_sites.domain; 0 = none)", OWN_RANK_LICENCE)
    miss = 0
    wsites = list(feed("wiki_sites"))
    org_domains = {official_key(u, d) for _p, d, u, b in wsites if ORG_BOX.search(b or "")} - {None}
    for s_, cols, m_ in wiki_rows(wiki_site_rows(((p_, d, u) for p_, d, u, _b in wsites), own)):
        miss += m_
        sr[s_] = clamp(cols[0], np.uint32)
    unmatched["wiki_sites"] = miss
    # ---- site
    ow = field("site.own_rank", "site", "<u4", OWN_RANK_SOURCE, OWN_RANK_LICENCE)
    nr = field("site.nav_rank", "site", "<u4", "own_rank of every navigable host (own_rank.tsv's hosts; 0 = none)", OWN_RANK_LICENCE)
    for s_, cols in ident_rows(None, None, "site", 1, "own_rank", rows=own_rows(own)):
        ow[s_] = clamp(cols[0], np.uint32)
        nr[s_] = clamp(cols[0], np.uint32)
    cc = field("site.cc_hc_pos", "site", "<u4", "serve_sites.cc_hc_pos (0 = none)", "Common Crawl web graph (CC ToU)")
    for s_, cols in ident_rows("serve", SIGNAL_SQL["serve_sites"][1], "site", 1, "serve_sites"):
        cc[s_] = clamp(cols[0], np.uint32)
    # ---- link
    ln = field("link.nav_rank", "link", "<u4", "own_rank of the host of the link's url (0 = none)", OWN_RANK_LICENCE)
    for s_, cols in ident_rows("serve", SIGNAL_SQL["host_nav"][1], "link", 1, "host_nav",
                               rows=link_rows((u for (u,) in feed("host_nav")), own)):
        ln[s_] = clamp(cols[0], np.uint32)
    # ---- account, youtube, stock
    asl = field("account.sitelinks", "account", "<u2", "wiki_links.sitelinks, max per (platform, value, qid)", "CC0 (Wikidata)")
    for s_, cols in ident_rows("serve", SIGNAL_SQL["accounts"][1], "account", 1, "accounts"):
        asl[s_] = clamp(cols[0], np.uint16)
    yf = field("youtube.followers", "youtube", "<u8", "youtube_channels.followers (0 = none)", "CC0 (Wikidata)")
    ys = field("youtube.sitelinks", "youtube", "<u2", "youtube_channels.sitelinks", "CC0 (Wikidata)")
    for s_, cols in ident_rows("serve", SIGNAL_SQL["youtube"][1], "youtube", 2, "youtube"):
        yf[s_] = np.array([0 if x is None else x for x in cols[0]], dtype=np.uint64)
        ys[s_] = clamp(cols[1], np.uint16)
    ss = field("stock.sitelinks", "stock", "<u2", "stock_tickers.sitelinks", "CC0 (Wikidata)")
    for s_, cols in ident_rows("serve", SIGNAL_SQL["stocks"][1], "stock", 1, "stocks"):
        ss[s_] = clamp(cols[0], np.uint16)
    # ---- suggestion weights (float4 -> f32 exactly). Items map to docs as the lookup builder's
    # idoc does: kind='wiki' -> the article (lowest page_id of that title); kind='site' with a
    # domain -> the site; everything else -> the link by url. Several items per doc: the LOWEST
    # id wins (the row the lookup's suggest_items keeps), so items are read id DESC and the last
    # assignment stays.
    sug = {typ: [field(f"{typ}.suggest_{c}", typ, "<f4",
                       f"suggest_items.{c} (lowest id per doc)" + ("; site and service items from own_rank (suggest_rows)" if typ != "wiki" else ""),
                       "M8 layer (CC0)") for c in ("pop", "score", "demand")]
           for typ in ("wiki", "site", "link")}

    def apop_of(title):
        r = sg.execute("SELECT seq FROM wtitle WHERE title = ?", (title,)).fetchone()
        return article_pop(int(sl[r[0]]), int(pv[r[0]]) if with_pv else None) if r else 0.0
    sg.execute("CREATE TABLE it (id INTEGER PRIMARY KEY, t INTEGER, k TEXT, u TEXT, pop REAL, score REAL, demand REAL)")
    sg.executemany("INSERT INTO it VALUES (?,?,?,?,?,?,?)",
                   ((i, 1 if kind == "wiki" else (2 if kind == "site" and domain is not None else 3),
                     wiki if kind == "wiki" else (domain if kind == "site" and domain is not None else url), url, p, s_, d)
                    for i, kind, wiki, domain, url, p, s_, d in suggest_rows(feed("items"),
                                                                             own, org_domains, apop_of)))
    sg.execute("CREATE INDEX it_tk ON it (t, k)")
    n_items = sg.execute("SELECT count(*) FROM it").fetchone()[0]
    mapped = 0
    for typ, t_id in (("wiki", 1), ("site", 2), ("link", 3)):
        if typ == "wiki":
            q = (f"SELECT m.seq, i.pop, i.score, i.demand FROM it i JOIN wtitle m ON m.title = i.k "
                 f"WHERE i.t = 1 ORDER BY i.id DESC")
        elif typ == "site":
            q = (f"SELECT m.doc_id & {mask}, i.pop, i.score, i.demand FROM it i JOIN idmap m ON m.type = {TYPES['site']} "
                 f"AND m.ident = i.k WHERE i.t = 2 ORDER BY i.id DESC")
        else:   # the lookup builder's third rule: everything not yet mapped goes to the link by url
            q = (f"SELECT m.doc_id & {mask}, i.pop, i.score, i.demand FROM it i JOIN idmap m ON m.type = {TYPES['link']} "
                 f"AND m.ident = i.u WHERE i.t = 3 OR (i.t = 1 AND NOT EXISTS (SELECT 1 FROM wtitle w WHERE w.title = i.k)) "
                 f"OR (i.t = 2 AND NOT EXISTS (SELECT 1 FROM idmap s WHERE s.type = {TYPES['site']} AND s.ident = i.k)) "
                 f"ORDER BY i.id DESC")
        cur = sg.execute(q)
        while True:
            b = cur.fetchmany(500000)
            if not b:
                break
            mapped += len(b)
            idx = np.array([r[0] for r in b], dtype=np.int64)
            for j, a in enumerate(sug[typ]):
                a[idx] = np.array([0.0 if r[j + 1] is None else r[j + 1] for r in b], dtype=np.float32)
    unmatched["suggest_items"] = {"items": n_items, "mapped_to_a_doc": mapped}

    off = 0
    for f, a in zip(fields, arrays):
        f["offset"] = off
        off += a.nbytes
    header = json.dumps({"format": FORMAT_VERSION, "built_release": release, "type_shift": TYPE_SHIFT,
                         "types": TYPES, "fields": fields,
                         "none": "0 means none for every field",
                         "not_doc_keyed": {
                             "suggest_keys.score": "per (key, item) — stays in the suggest tables (T4)",
                             "suggest_top": "per prefix ordering — suggest tables (T4)",
                             "wiki_next.clicks": "per article pair — lookup (T4)",
                             "anchors.sources": "per (domain, anchor) — lookup / text/sites",
                             "suggest_phrases.counts": "per prefix — lookup-phrases"}},
                        sort_keys=True, separators=(",", ":")).encode()
    with open(out, "wb") as f:
        f.write(SIGNALS_MAGIC + struct.pack("<I", len(header)) + header)
        for a in arrays:
            f.write(a.tobytes())
    ex.report("signals", t0, path=out, bytes=os.path.getsize(out), pageviews=with_pv,
              fields={f["name"]: f["count"] * np.dtype(f["dtype"]).itemsize for f in fields}, unmatched=unmatched)


def _batches(rows, n=500000):
    batch = []
    for r in rows:
        batch.append(r)
        if len(batch) >= n:
            yield batch
            batch = []
    if batch:
        yield batch


def _dsn(name):
    return ex.PG_SEARCH_DSN if name == "search" else ex.PG_DSN


def cmd_sigdump(path: str) -> None:
    with open(path, "rb") as f:
        if f.read(8) != SIGNALS_MAGIC:
            sys.exit("not a signals.bin")
        n = struct.unpack("<I", f.read(4))[0]
        print(json.dumps(json.loads(f.read(n)), indent=1))


# --------------------------------------------------------------------------- pictures.sqlite

OLD_CREDIT_CUT = 80     # card_images.author_of cut a credit to 79 characters + "…" until 2026-10-22 (audit G3)


def _needs_credit(licence) -> bool:
    """Every licence but public domain and CC0 asks for the creator's name (CC BY / BY-SA); an unknown one is
    treated as asking."""
    import re
    return not re.match(r"(public domain|pd|cc0)\b", (licence or "").strip(), re.I)


def fix_credits(db) -> dict:
    """The credits of the pictures table just written (audit 2026-10-22 G3, owner decision: drop, do not refetch).
    A credit the old cut shortened (exactly OLD_CREDIT_CUT characters, ending in "…") may have lost a creator: the
    row is DROPPED when its licence asks for credit, and kept as is on a public-domain / CC0 row, which asks for
    none. A credit over card_images.AUTHOR_MAX is treated the same (it is never cut). Every other credit has its
    HTML entities decoded until stable (card_images.decode_entities: some Commons values were escaped twice).
    Returns the counts, which the stage reports and the file's meta keeps."""
    import card_images as ci
    n = {"dropped_cut_credit": 0, "dropped_over_cap": 0, "kept_cut_credit_no_credit_needed": 0, "decoded": 0}
    drop, upd = [], []
    for d, author, lic in db.execute("SELECT doc_id, author, licence FROM pictures WHERE author IS NOT NULL ORDER BY doc_id").fetchall():
        cut = len(author) == OLD_CREDIT_CUT and author.endswith("…")
        over = len(author) > ci.AUTHOR_MAX
        if cut or over:
            if _needs_credit(lic):
                n["dropped_cut_credit" if cut else "dropped_over_cap"] += 1
                drop.append((d,))
                continue
            n["kept_cut_credit_no_credit_needed"] += 1
        dec = ci.decode_entities(author)
        if dec != author:
            n["decoded"] += 1
            upd.append((dec, d))
    db.executemany("DELETE FROM pictures WHERE doc_id = ?", drop)
    db.executemany("UPDATE pictures SET author = ? WHERE doc_id = ?", upd)
    db.commit()
    return n


def cmd_pictures(registry: str, out: str, release: str) -> None:
    """card_images -> pictures.sqlite keyed by doc_id. The pictures are TOASTed bytea: reading them
    row by row is one random disk read per picture on the uncached clone (a 2 GB read crawled at
    ~6 MB/min, run 5c). So the toast table is read SEQUENTIALLY (chunk_id order) and reassembled
    here; only pictures stored compressed or inline are taken the normal way."""
    t0 = time.time()
    ex.check_source()
    rdb = sqlite3.connect(f"file:{registry}?mode=ro", uri=True)
    # a title shared by two articles belongs to the lowest page_id (the lookup builder's tdoc rule):
    # read highest first, the last assignment stays
    title_doc = dict(rdb.execute(f"SELECT title, doc_id FROM registry WHERE doc_id >> {TYPE_SHIFT} = 1 ORDER BY ext DESC"))
    rdb.close()
    meta, miss = {}, 0
    inline, compressed = {}, []
    with pg(ex.PG_DSN) as c:
        toast = c.execute("SELECT reltoastrelid::regclass::text FROM pg_class WHERE relname = 'card_images'").fetchone()[0]
        with c.cursor(name="pm") as cur:
            cur.itersize = 20000
            cur.execute("""SELECT title, file, w, h, author, licence, licence_url,
                                  pg_column_toast_chunk_id(img), pg_column_compression(img),
                                  CASE WHEN pg_column_toast_chunk_id(img) IS NULL THEN img END
                           FROM card_images ORDER BY title COLLATE "C", file COLLATE "C" """)
            for title, file, w, h, author, lic, lic_url, chunk, comp, img in cur:
                d = title_doc.get(title)
                if d is None:
                    miss += 1
                    continue
                if d in meta:                          # first (title, file) wins
                    continue
                meta[d] = (file, w, h, author, lic, lic_url, chunk)
                if chunk is None:
                    inline[d] = bytes(img) if img is not None else None
                elif comp is not None:
                    compressed.append((d, file))
    want = {m[6]: d for d, m in meta.items() if m[6] is not None and d not in {x[0] for x in compressed}}
    blobs, cur_id, parts = {}, None, []
    with pg(ex.PG_DSN) as c, c.cursor(name="tc") as cur:
        cur.itersize = 2000
        cur.execute(f"SELECT chunk_id, chunk_seq, chunk_data FROM {toast} ORDER BY chunk_id, chunk_seq")
        for cid, seq, data in cur:
            if cid != cur_id:
                if cur_id in want:
                    blobs[want[cur_id]] = b"".join(parts)
                cur_id, parts = cid, []
            parts.append(bytes(data))
        if cur_id in want:
            blobs[want[cur_id]] = b"".join(parts)
    with pg(ex.PG_DSN) as c:                          # the few compressed ones, the normal way
        for d, file in compressed:
            blobs[d] = bytes(c.execute("SELECT img FROM card_images WHERE file = %s LIMIT 1", (file,)).fetchone()[0])
    blobs.update(inline)
    db = new_db(out)
    db.execute("CREATE TABLE pictures (doc_id INTEGER PRIMARY KEY, file TEXT, img BLOB, w INTEGER, h INTEGER, "
               "author TEXT, licence TEXT, licence_url TEXT)")
    lost = 0
    for d in sorted(meta):
        f, w, h, a_, l_, u_, _ = meta[d]
        img = blobs.get(d)
        if img is None:
            lost += 1
        db.execute("INSERT INTO pictures VALUES (?,?,?,?,?,?,?,?)", (d, f, img, w, h, a_, l_, u_))
    credits = fix_credits(db)
    rows = db.execute("SELECT count(*) FROM pictures").fetchone()[0]
    stamp(db, "pictures", release, {"licence": "per row (Wikimedia Commons)", "credits": json.dumps(credits, sort_keys=True)})
    finish(db)
    ex.report("pictures", t0, path=out, rows=rows, titles_not_in_registry=miss, inline=len(inline),
              toast_sequential=len(want), compressed_fetched=len(compressed), missing_image=lost, credits=credits,
              bytes=os.path.getsize(out))


def cmd_pictures_from(card: str, registry: str, out: str, release: str) -> None:
    """card-images.sqlite (apps/search/importer/card_images.py's working file: `pictures`, title-keyed,
    ADR-063) -> pictures.sqlite keyed by doc_id, the same schema, stamp and title rule as cmd_pictures,
    with no Postgres. A title the registry does not know (an article that left) is counted and skipped.
    The join runs inside SQLite (memory stays at its page cache: a Python title map of 7M articles
    peaked at 3.5 GB on buildbox, 2026-10-04)."""
    t0 = time.time()
    db = new_db(out)
    db.execute("CREATE TABLE pictures (doc_id INTEGER PRIMARY KEY, file TEXT, img BLOB, w INTEGER, h INTEGER, "
               "author TEXT, licence TEXT, licence_url TEXT)")
    # plain paths: a "file:...?mode=ro" ATTACH needs a URI-enabled connection, which an SQLite build without
    # SQLITE_USE_URI does not give (nothing here writes to either file)
    db.execute("ATTACH ? AS reg", (registry,))
    db.execute("ATTACH ? AS card", (card,))
    # a title shared by two articles belongs to the lowest page_id (the lookup builder's tdoc rule);
    # SQLite takes the bare columns of the row min() picked
    db.execute(f"""CREATE TEMP TABLE tdoc AS SELECT title, doc_id, min(ext) AS ext FROM reg.registry
                   WHERE doc_id >> {TYPE_SHIFT} = 1 AND title IN (SELECT title FROM card.pictures) GROUP BY title""")
    db.execute("CREATE UNIQUE INDEX temp.tdoc_title ON tdoc (title)")
    db.execute("""INSERT INTO pictures SELECT t.doc_id, p.file, p.img, p.w, p.h, p.author, p.licence, p.licence_url
                  FROM card.pictures p JOIN tdoc t USING (title) ORDER BY t.doc_id""")
    joined = db.execute("SELECT count(*) FROM pictures").fetchone()[0]
    miss = db.execute("SELECT count(*) FROM card.pictures").fetchone()[0] - joined
    db.execute("DROP TABLE temp.tdoc")
    db.commit()
    db.execute("DETACH reg")
    db.execute("DETACH card")
    credits = fix_credits(db)
    rows = db.execute("SELECT count(*) FROM pictures").fetchone()[0]
    stamp(db, "pictures", release, {"licence": "per row (Wikimedia Commons)", "credits": json.dumps(credits, sort_keys=True)})
    finish(db)
    ex.report("pictures-from", t0, path=out, rows=rows, titles_not_in_registry=miss, credits=credits, bytes=os.path.getsize(out))


# --------------------------------------------------------------------------- stamps and guards

def cmd_stamp(path: str, role: str, release: str) -> None:
    db = sqlite3.connect(path)
    db.execute("PRAGMA secure_delete = ON")
    stamp(db, role, release)
    finish(db)
    print(json.dumps({"stage": "stamp", "path": path, "role": role}))


GUARD_TABLES = ex.FORBIDDEN_TABLES | {"subreddits"}
GUARD_COLUMNS = {"history_visits", "hist", "jev", "useful", "parked", "spam", "unsafe"}


def sqlguard_file(path: str) -> dict:
    """Refuse a SQLite file that carries refused tables or columns, WDC places, free pages (deleted
    rows still on disk), or no version stamp."""
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    problems = []
    tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
    role = dict(db.execute("SELECT key, value FROM meta")).get("role") if "meta" in tables else None
    if role == "kinds":                  # the open kinds tier (D2): its own allow-list instead of the blanket refusal
        problems += kinds_build.kinds_guard(db)               # (the generic loop below still runs: forbidden tables, columns)
    for t in tables:
        if (t in GUARD_TABLES and not (role == "kinds" and t in kinds_build.KINDS_TABLES)) \
                or (role != "kinds" and (t.startswith("kind") or t.startswith("kinds_"))):
            problems.append(f"table {t}")
        try:
            cols = [r[1] for r in db.execute(f'PRAGMA table_info("{t}")')]
        except sqlite3.DatabaseError:
            continue
        for c in cols:
            if c in GUARD_COLUMNS or "jev" in c.lower():
                problems.append(f"column {t}.{c}")
        if "osm" in cols:
            n = db.execute(f"SELECT count(*) FROM \"{t}\" WHERE osm LIKE 'wdc:%'").fetchone()[0]
            if n:
                problems.append(f"{n} WDC rows in {t}")
        if t == "suggest_items" and "kind" in cols:
            n = db.execute("SELECT count(*) FROM suggest_items WHERE kind = 'subreddit'").fetchone()[0]
            if n:
                problems.append(f"{n} subreddit suggestions")
    free = db.execute("PRAGMA freelist_count").fetchone()[0]
    if free:
        problems.append(f"freelist_count = {free} (deleted rows may remain on disk)")
    if db.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID:
        problems.append("application_id not set")
    if db.execute("PRAGMA user_version").fetchone()[0] != FORMAT_VERSION:
        problems.append("user_version not the format version")
    if "meta" not in tables:
        problems.append("no meta table")
    db.close()
    return {"path": path, "tables": len(tables), "problems": problems}


def cmd_sqlguard(*paths) -> None:
    res = [sqlguard_file(p) for p in paths]
    print(json.dumps({"stage": "sqlguard", "files": res}))
    if any(r["problems"] for r in res):
        sys.exit(1)


def cmd_rowdiff(old: str, new: str) -> None:
    """Rows a SQLite row diff would carry from OLD to NEW, per table (added + removed, with the bytes
    of their values) — the monthly update size before compression."""
    db = sqlite3.connect(f"file:{new}?mode=ro", uri=True)
    db.execute("ATTACH ? AS o", (f"file:{old}?mode=ro",))
    res = {}
    for (t,) in db.execute("SELECT name FROM main.sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
                           "ORDER BY name").fetchall():
        if not db.execute("SELECT 1 FROM o.sqlite_master WHERE type = 'table' AND name = ?", (t,)).fetchone():
            res[t] = {"new_table": True}
            continue
        cols = [r[1] for r in db.execute(f'PRAGMA main.table_info("{t}")')]
        sz = " + ".join(f'coalesce(length(CAST("{c}" AS BLOB)), 0)' for c in cols) or "0"
        add = db.execute(f'SELECT count(*), coalesce(sum({sz}), 0) FROM (SELECT * FROM main."{t}" EXCEPT SELECT * FROM o."{t}")').fetchone()
        rem = db.execute(f'SELECT count(*), coalesce(sum({sz}), 0) FROM (SELECT * FROM o."{t}" EXCEPT SELECT * FROM main."{t}")').fetchone()
        tot = db.execute(f'SELECT count(*) FROM main."{t}"').fetchone()[0]
        res[t] = {"rows": tot, "added": add[0], "added_bytes": add[1], "removed": rem[0], "removed_bytes": rem[1]}
    print(json.dumps({"stage": "rowdiff", "old": old, "new": new, "tables": res,
                      "added_bytes": sum(v.get("added_bytes", 0) for v in res.values()),
                      "removed_rows": sum(v.get("removed", 0) for v in res.values())}))


def cmd_idhash(path: str) -> None:
    print(json.dumps({"stage": "idhash", "path": path, "types": ri.idhash(path)}))


def cmd_sha(*paths) -> None:
    out = {}
    for p in paths:
        if os.path.isdir(p):
            for d, _, fs in sorted(os.walk(p)):
                for f in sorted(fs):
                    q = os.path.join(d, f)
                    out[os.path.relpath(q, p)] = ex.sha256_file(q)
        else:
            out[p] = ex.sha256_file(p)
    print(json.dumps({"stage": "sha", "files": out}))


def cmd_storemap(rel: str) -> None:
    """Where displayable data is stored, in bytes: Tantivy files by kind (.store = stored fields)
    per index, and every SQLite table (dbstat) — the input to the store-once map."""
    out = {"tantivy": {}, "sqlite": {}}
    for d, _, fs in sorted(os.walk(rel)):
        if "meta.json" in fs:
            by = {}
            for f in fs:
                ext_ = f.rsplit(".", 1)[-1] if "." in f else f
                by[ext_] = by.get(ext_, 0) + os.path.getsize(os.path.join(d, f))
            out["tantivy"][os.path.relpath(d, rel)] = dict(sorted(by.items()))
        for f in fs:
            if f.endswith(".sqlite"):
                p = os.path.join(d, f)
                db = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
                try:
                    out["sqlite"][os.path.relpath(p, rel)] = dict(
                        db.execute("SELECT name, sum(pgsize) FROM dbstat GROUP BY name ORDER BY 2 DESC").fetchall())
                except sqlite3.OperationalError as e:
                    out["sqlite"][os.path.relpath(p, rel)] = {"error": str(e)}
                db.close()
    print(json.dumps({"stage": "storemap", **out}))


def tv_normalize(path: str, release: str, role: str, stopfile: str = None, extra: dict = None) -> dict:
    """Make a finished Tantivy index directory reproducible (release format v1, rule 7): segment ids
    are random UUIDs in Tantivy 0.26, so each segment is renamed to a UUID derived from the sha256 of
    its own files; meta.json and .managed.json are rewritten to match (.managed.json sorted). Then a
    sidecar release.json stamps it (rule 8). Same input + one writer thread -> same bytes. meta.json's
    random `payload` is dropped too (Tantivy 0.26 writes a fresh one per commit; nothing reads it)."""
    import re
    import uuid
    meta_p = os.path.join(path, "meta.json")
    meta_txt = open(meta_p, encoding="utf-8").read()
    segs = [s_["segment_id"] for s_ in json.loads(meta_txt).get("segments", [])]
    names = os.listdir(path)
    mapping = {}
    for sid in segs:
        short = sid.replace("-", "")
        h = hashlib.sha256()
        for f in sorted(n for n in names if n.startswith(short)):
            h.update(f[len(short):].encode())
            h.update(bytes.fromhex(ex.sha256_file(os.path.join(path, f))))
        new = str(uuid.UUID(bytes=h.digest()[:16], version=4))
        mapping[sid] = new
    for sid, new in mapping.items():
        short, nshort = sid.replace("-", ""), new.replace("-", "")
        for f in names:
            if f.startswith(short):
                os.rename(os.path.join(path, f), os.path.join(path, nshort + f[len(short):]))
        meta_txt = meta_txt.replace(sid, new)
    meta_txt = re.sub(r',\s*"payload":\s*"[^"]*"', "", meta_txt)
    open(meta_p, "w", encoding="utf-8").write(meta_txt)
    man_p = os.path.join(path, ".managed.json")
    if os.path.exists(man_p):
        man = json.load(open(man_p))
        for sid, new in mapping.items():
            man = [m.replace(sid.replace("-", ""), new.replace("-", "")) for m in man]
        open(man_p, "w").write(json.dumps(sorted(man)))
    for f in (".tantivy-meta.lock", ".tantivy-writer.lock"):
        if os.path.exists(os.path.join(path, f)) and os.path.getsize(os.path.join(path, f)) == 0:
            os.remove(os.path.join(path, f))
    side = {"format": FORMAT_VERSION, "built_release": release, "role": role, "tantivy": ex.TANTIVY_VERSION,
            "analyzer": ex.ANALYZER}
    if stopfile:
        side["stopwords_sha256"] = ex.sha256_file(stopfile)
    # keys a builder stamped earlier (schema, builder) survive a second normalize
    try:
        old = json.load(open(os.path.join(path, "release.json")))
        side.update({k: old[k] for k in ("schema", "builder") if k in old})
    except (OSError, ValueError):
        pass
    side.update(extra or {})
    open(os.path.join(path, "release.json"), "w").write(json.dumps(side, sort_keys=True) + "\n")
    return {"segments": len(segs)}


def cmd_tvnormalize(path: str, release: str, role: str, stopfile: str = None, *kv) -> None:
    """tvnormalize DIR RELEASE ROLE [STOPFILE] [key=value ...]: extra keys go into release.json."""
    if stopfile and "=" in stopfile and not os.path.exists(stopfile):
        kv, stopfile = (stopfile,) + kv, None
    print(json.dumps({"stage": "tvnormalize", "path": path,
                      **tv_normalize(path, release, role, stopfile, dict(x.split("=", 1) for x in kv))}))


def tv_id_field(index: str) -> str | None:
    """The id field of a Tantivy folder: "doc_id" (release, u64), "page_id" (proto export, i64) or None."""
    names = [f["name"] for f in json.load(open(os.path.join(index, "meta.json")))["schema"]]
    return next((f for f in ("doc_id", "page_id") if f in names), None)


def tv_delete_ids(writer, schema, field: str, ids) -> int:
    """Delete documents by an integer id field. writer.delete_documents_by_term(field, n) builds an
    i64 term from a Python int and silently deletes NOTHING on a u64 field (measured on
    tantivy-py 0.26.2); a term QUERY takes the field's own type, so it works on u64 and i64."""
    import tantivy
    n = 0
    for i in ids:
        writer.delete_documents_by_query(tantivy.Query.term_query(schema, field, int(i)))
        n += 1
    return n


def cmd_tvmonth(index: str, stopfile: str) -> None:
    """Apply a simulated month to a COPY of text/wiki: delete + re-add every page_id in the clone's
    month_changes table (op: change | delete | new), one commit, like a monthly update would. The
    update download = the files that were not in the index before (new segments + delete bitsets)."""
    import tantivy
    t0 = time.time()
    if tv_id_field(index) != "page_id":
        # It re-adds page_id documents; a release index keys on the registry's u64 doc_id.
        raise SystemExit(f"tvmonth: {index} is not a page_id index (proto export); release indexes update through update_pkg.py")
    ex.check_source()
    before = set(os.listdir(index))
    idx = tantivy.Index.open(index)
    idx.register_tokenizer(ex.ANALYZER, ex.analyzer(stopfile))
    w = idx.writer(heap_size=500_000_000, num_threads=1)
    n = {"change": 0, "delete": 0, "new": 0}
    for pid, op in stream(ex.PG_DSN, "SELECT page_id, op FROM month_changes ORDER BY page_id"):
        tv_delete_ids(w, idx.schema, "page_id", (pid,))
        n[op] += 1
    for pid, title, aliases, infobox, headings, lead in stream(ex.PG_DSN, """
            SELECT w.page_id, w.title, w.aliases, w.infobox, w.headings, w.lead FROM wiki_docs w
            JOIN month_changes m USING (page_id) WHERE m.op <> 'delete' ORDER BY w.page_id"""):
        d = tantivy.Document()
        d.add_integer("page_id", pid)
        d.add_text("title", title or "")
        d.add_text("body", " ".join(x for x in (title, aliases, infobox, headings, lead) if x))
        d.add_bytes("snippet", (lead or "")[:300].encode("utf-8"))
        w.add_document(d)
    w.commit()
    w.wait_merging_threads()
    after = set(os.listdir(index))
    new = sorted(after - before)
    ex.report("tvmonth", t0, ops=n, new_files=len(new), new_bytes=sum(os.path.getsize(os.path.join(index, f)) for f in new),
              removed_files=len(before - after), segments=ex.segments(index), index_bytes=ex.tree_bytes(index))


def cmd_wikiinput(registry: str) -> None:
    """The Rust index builder's input (tantivy-structure build < this): one COPY text line per article,
    `doc_id TAB title TAB aliases TAB infobox TAB headings TAB lead`, in page_id order, with the
    registry's doc_id in place of the page_id. Registry and wiki_docs must hold the same page_ids:
    they are walked side by side, and any difference aborts."""
    ex.check_source()
    ex.check_sql_one(WIKIINPUT_SQL)
    rdb = sqlite3.connect(f"file:{registry}?mode=ro", uri=True)
    reg = rdb.execute(f"SELECT ext, doc_id FROM registry WHERE doc_id >> {TYPE_SHIFT} = {TYPES['wiki']} ORDER BY ext")
    out = sys.stdout.buffer
    n = 0
    with pg(ex.PG_DSN) as c, c.cursor() as cur, cur.copy(f"COPY ({WIKIINPUT_SQL}) TO STDOUT") as cp:
        tail = b""
        for chunk in cp:
            data = tail + bytes(chunk)
            lines = data.split(b"\n")
            tail = lines.pop()
            buf = []
            for line in lines:
                tab = line.index(b"\t")
                pid = int(line[:tab])
                want = next(reg, None)
                if want is None or want[0] != pid:
                    sys.exit(f"registry and wiki_docs disagree at page_id {pid} (registry: {want and want[0]}) — refusing")
                buf.append(str(want[1]).encode() + line[tab:] + b"\n")
                n += 1
            out.write(b"".join(buf))
        if tail:
            sys.exit("COPY stream ended mid-line")
    if next(reg, None) is not None:
        sys.exit("the registry has wiki docs that wiki_docs lacks — refusing")
    out.flush()
    print(json.dumps({"stage": "wikiinput", "rows": n}), file=sys.stderr)


WIKIINPUT_SQL = "SELECT page_id, title, aliases, infobox, headings, lead FROM wiki_docs ORDER BY page_id"


# --------------------------------------------------------------------------- web index inputs

# serve/box-profile.sql page_words(), inlined (as tools/sites-tantivy.py does): no function is needed on the
# source, and a body that is already words (release-profile.sql) comes out unchanged (idempotent), so no readable
# page text can reach the input even when check_source's 4000-row sample missed a row (ADR-059).
_PAGE_WORDS = ("(SELECT coalesce(string_agg(w, ' ' ORDER BY w COLLATE \"C\"), '') FROM "
               "(SELECT lower(regexp_replace(x, '^\\W+|\\W+$', '', 'g')) AS w "
               "FROM regexp_split_to_table({col}, '\\s+') AS x) pw_ WHERE w <> '')")
# One statement per index; column 0 is the ident (host / url / host), the rest are web_input.py's columns for
# that KIND (its docstring is the contract). One row per ident: the latest crawl, the registry's own tie-break
# (SITE_SQL / PAGE_SQL). Every statement goes through export.check_sql_one (block list on anchor_docs, no refused
# table, no history, no Jev column).
WEB_INPUT_SQL = {
    "sites": ("SELECT DISTINCT ON (d.domain COLLATE \"C\") d.domain, "
              "regexp_replace(d.domain, '[.-]+', ' ', 'g') || ' ' || coalesce(d.title, '') || ' ' || coalesce(d.h1, ''), "
              "regexp_replace(d.domain, '[.-]+', ' ', 'g') || ' ' || coalesce(d.title, '') || ' ' || coalesce(d.description, '') "
              "|| ' ' || coalesce(d.h1, '') || ' ' || " + _PAGE_WORDS.format(col="d.body") + " "
              "FROM docs d WHERE d.error IS NULL AND d.domain NOT IN (" + docs_policy.PG_DOMAINS_OUT + ") "
              "ORDER BY d.domain COLLATE \"C\", d.fetched_at DESC NULLS LAST, d.crawl COLLATE \"C\""),
    # a page's description never ships as prose (G3 D4c: no opt-out signal is stored for pages): it joins the body's
    # sorted words. Title and h1 stay as written (the registry ships the title anyway).
    # A site whose homepage said noindex ships no pages (PAGE_SQL's rule, audit 2026-10-22 G4).
    "pages": ("SELECT DISTINCT ON (p.url COLLATE \"C\") p.url, "
              "coalesce(p.title, '') || ' ' || coalesce(p.h1, '') || ' ' || "
              + _PAGE_WORDS.format(col="(coalesce(p.description, '') || ' ' || coalesce(p.body, ''))") + " "
              "FROM page_docs p WHERE p.error IS NULL AND coalesce(p.body, '') <> '' AND p.domain NOT IN (" + docs_policy.PG_DOMAINS_NOINDEX + ") "
              "ORDER BY p.url COLLATE \"C\", p.fetched_at DESC NULLS LAST, p.crawl COLLATE \"C\""),
    # anchor text loses every block-list word (G3 D9), as export.ANCHORS_SQL; an anchor document is its site
    "anchors": (f"SELECT domain, body FROM (SELECT a.domain, regexp_replace(a.body, '{ex.BLOCK_SQL}', ' ', 'gi') AS body "
                f"FROM anchor_docs a) x WHERE body ~ '\\w' ORDER BY domain COLLATE \"C\""),
}
WEB_KIND_TYPE = {"sites": "site", "pages": "page", "anchors": "site"}
# anchor-phrases.sqlite: phrase -> sites, what export.EXACT_ANCHOR_SQL answers from `anchors JOIN docs`. Only sites
# with a fetched page (docs.error IS NULL) keep a row; a distinct-domain hash join, not EXISTS: pg() turns index
# scans off. Block-listed phrases never ship (G3 D9).
ANCHOR_PHRASE_SQL = (f"SELECT a.domain, a.anchor, coalesce(a.sources, 0) FROM anchors a "
                     f"JOIN (SELECT DISTINCT domain FROM docs WHERE error IS NULL) f ON f.domain = a.domain "
                     f"WHERE a.anchor !~* '{ex.BLOCK_SQL}' "
                     f"ORDER BY a.anchor COLLATE \"C\", a.domain COLLATE \"C\"")
ANCHOR_PHRASE_LICENCE = "CC0-1.0 (M8 Software's own layer); Common Crawl anchor phrases (third-party link text, block-listed)"


def _web_input_mod():
    import importlib.util
    here = os.path.dirname(os.path.abspath(__file__))
    spec = importlib.util.spec_from_file_location(
        "web_input", os.path.join(here, "..", "tools", "tantivy-structure", "web_input.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _registry_ids(registry: str, type_name: str) -> dict:
    """ident -> doc_id of one registry type, through registry_idents.idmap (one definition of an ident)."""
    c = sqlite3.connect(":memory:")
    ri.idmap(c, registry, "idmap", types=(TYPES[type_name],))
    ids = dict(c.execute("SELECT ident, doc_id FROM idmap WHERE type = ?", (TYPES[type_name],)))
    c.close()
    return ids


def _gov_state_mod():
    """serve/api/gov_state.py (standard library): a government site's state and the word that marks it."""
    import importlib.util
    here = os.path.dirname(os.path.abspath(__file__))
    spec = importlib.util.spec_from_file_location("gov_state", os.path.join(here, "..", "serve", "api", "gov_state.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _pages_store_mod():
    import pages_store
    return pages_store


def _store_page_input(store: str):
    """WEB_INPUT_SQL["pages"]'s rows from the pages store: (url, title h1 page_words(description body))."""
    return _pages_store_mod().page_inputs(store)


def _store_pages_out(store: str) -> tuple[int, int]:
    """PAGES_OUT_SQL from the pages store: fetched pages on a noindex site, and those of an open dump."""
    ps = _pages_store_mod()
    out = ps.noindex(store)
    n = n_open = 0
    for domain, crawl in ps.rows(store, ["domain", "crawl"], order=False):
        if domain in out:
            n += 1
            n_open += (crawl or "").startswith("open:")
    return n, n_open


def cmd_webinput(registry: str, out_dir: str, release: str, gov_states: str = None) -> None:
    """The three web Tantivy indexes' input rows (SEARCH-RELEASE-FORMAT, the web text indexes), one file per
    KIND: OUT_DIR/{sites,pages,anchors}.rows.tsv.zst = `ident TAB columns`, COPY text escapes, exactly what
    tools/tantivy-structure/local-build.sh reads (`web_input.py map` turns the ident into the registry doc_id).
    A row whose ident the registry lacks is dropped HERE and counted, so `map` (which refuses an unknown ident)
    never sees one. Sorted by ident, one row per ident: the same inputs give the same bytes (sha256 reported).
    A registry without pages gives an empty pages file. Popularity order is the builder's job (signals.bin).
    With GOV_STATES (the release's gov-states.json, release/gov_states.py) a page on a state's government site
    gets that state's word (gov_state.state_token, "govstatenj") in its bag of words, so the engine can ask the
    pages index for one state's pages instead of reading hundreds of hits and keeping the state's. The
    file given must be the one the release ships: the engine's query-time states come from it too."""
    import hashlib
    import subprocess
    from urllib.parse import urlsplit
    t0 = time.time()
    ex.check_source()
    for q in list(WEB_INPUT_SQL.values()) + [PAGES_OUT_SQL]:
        ex.check_sql_one(q)
    if not subprocess.run(["sh", "-c", "command -v zstd"], capture_output=True).stdout.strip():
        sys.exit("web-input: zstd is not installed — the rows are written as .tsv.zst")
    wi = _web_input_mod()
    doms, gs, gov_mod = {}, None, None
    if gov_states:
        import gov_states as gsm
        errs = gsm.check(gov_states)
        if errs:
            sys.exit(f"web-input: {gov_states}: " + "; ".join(errs))
        gov_mod = _gov_state_mod()
        doms = gov_mod.load(gov_states)
        gs = {"file": os.path.basename(gov_states), "sha256": hashlib.sha256(open(gov_states, "rb").read()).hexdigest(),
              "domains": len(doms), "pages_with_state": 0, "states": 0}
    os.makedirs(out_dir, exist_ok=True)
    res = {}
    store = _pages_store_mod().store_dir()                # SEARCH_PAGES_STORE: the pages from files (pages_store.py)
    for kind, sql in WEB_INPUT_SQL.items():
        ids = _registry_ids(registry, WEB_KIND_TYPE[kind])
        path = os.path.join(out_dir, f"{kind}.rows.tsv.zst")
        part = path + ".part"
        z = subprocess.Popen(["zstd", "-q", "-3", "-T1", "-f", "-o", part], stdin=subprocess.PIPE)
        sha, n_src, kept, prev, states = hashlib.sha256(), 0, 0, None, set()
        try:
            for row in (_store_page_input(store) if kind == "pages" and store else stream(ex.PG_DSN, sql)):
                n_src += 1
                if len(row) != 1 + wi.COLS[kind]:
                    sys.exit(f"web-input {kind}: the statement returned {len(row)} columns, web_input wants {1 + wi.COLS[kind]}")
                ident = row[0]
                if prev is not None and ident <= prev:
                    sys.exit(f"web-input {kind}: {ident!r} after {prev!r} — not one row per ident in order, refusing")
                prev = ident
                if ident not in ids:
                    continue
                if kind == "pages" and doms:
                    host = urlsplit(ident).hostname or ""
                    st = gov_mod.host_state(host, doms)
                    if st:
                        # the state's word and the site's (item 8: one town's or county's pages)
                        site = gov_mod.host_domain(host, doms)
                        extra = gov_mod.state_token(st) + (" " + gov_mod.site_token(site) if site else "")
                        row = (ident, f"{row[1] or ''} {extra}".strip())
                        gs["pages_with_state"] += 1
                        gs["pages_with_site"] = gs.get("pages_with_site", 0) + (1 if site else 0)
                        states.add(st)
                line = ("\t".join(wi.esc(v) for v in row) + "\n").encode("utf-8", "surrogateescape")
                sha.update(line)
                z.stdin.write(line)
                kept += 1
        finally:
            z.stdin.close()
            rc = z.wait()
        if rc:
            sys.exit(f"web-input {kind}: zstd exited {rc}")
        os.replace(part, path)
        res[kind] = {"rows": kept, "source_rows": n_src, "dropped_not_in_registry": n_src - kept,
                     "registry_without_row": len(ids) - kept, "registry_idents": len(ids),
                     "bytes": os.path.getsize(path), "tsv_sha256": sha.hexdigest()}
        if kind == "pages" and gs is not None:
            gs["states"] = len(states)
            res[kind]["gov_states"] = gs
    (n, n_open), = [_store_pages_out(store)] if store else list(stream(ex.PG_DSN, PAGES_OUT_SQL))
    res["pages"]["dropped_capture_rule"] = {"pages": n, "of_which_open_dump": n_open}
    ex.report("web-input", t0, release=release, registry=os.path.basename(registry), out=out_dir, kinds=res)


def cmd_anchorphrases(registry: str, out: str, release: str) -> None:
    """anchor-phrases.sqlite: what many sites call one site, keyed by the SITE's registry doc_id (the engine's
    exact_anchor). One table, anchor_phrase(anchor, sources, doc_id), PRIMARY KEY (anchor, sources DESC, doc_id)
    WITHOUT ROWID: `WHERE anchor = ?` already reads the sites in the order exact_anchor wants (most linking
    sites first, doc_id breaks a tie), so LIMIT 2 reads two index entries. Rows of a domain the registry lacks are
    dropped and counted; a (doc_id, anchor) pair seen twice keeps its largest `sources`."""
    t0 = time.time()
    ex.check_source()
    ex.check_sql_one(ANCHOR_PHRASE_SQL)
    db = new_db(out)
    ri.idmap(db, registry, "idmap", types=(TYPES["site"],))
    db.execute("CREATE TEMP TABLE src (k TEXT, anchor TEXT, sources INTEGER)")
    db.executemany("INSERT INTO src VALUES (?,?,?)", stream(ex.PG_DSN, ANCHOR_PHRASE_SQL))
    n_src, n_anchor = db.execute("SELECT count(*), count(DISTINCT anchor) FROM src").fetchone()
    db.execute("CREATE TABLE anchor_phrase (anchor TEXT NOT NULL, sources INTEGER NOT NULL, doc_id INTEGER NOT NULL, "
               "PRIMARY KEY (anchor, sources DESC, doc_id)) WITHOUT ROWID")
    db.execute(f"""INSERT INTO anchor_phrase SELECT s.anchor, max(s.sources), m.doc_id FROM src s
                   JOIN idmap m ON m.type = {TYPES['site']} AND m.ident = s.k
                   GROUP BY m.doc_id, s.anchor ORDER BY s.anchor, max(s.sources) DESC, m.doc_id""")
    n_ok, n_distinct = db.execute("SELECT count(*), count(DISTINCT anchor) FROM anchor_phrase").fetchone()
    joined = db.execute(f"SELECT count(*) FROM src s JOIN idmap m ON m.type = {TYPES['site']} AND m.ident = s.k").fetchone()[0]
    stamp(db, "anchor-phrases", release, {"licence": ANCHOR_PHRASE_LICENCE, "source": "anchors (Common Crawl link text)"})
    finish(db, out)
    ex.report("anchor-phrases", t0, path=out, source_rows=n_src, rows=n_ok, distinct_anchors=n_distinct,
              source_distinct_anchors=n_anchor, dropped_not_in_registry=n_src - joined, collapsed_duplicates=joined - n_ok,
              bytes=os.path.getsize(out))


def cmd_tvrepro(stopfile: str, outdir: str) -> None:
    """Build the anchors index twice, one thread, same input: are the files byte-identical? Tantivy
    names segments with random UUIDs, so this also compares contents with the names mapped away."""
    import shutil
    import tantivy
    t0 = time.time()
    ex.check_source()
    rows = list(ex.stream(ex.PG_DSN, ex.ANCHORS_SQL))
    res = []
    for k in (1, 2):
        path = os.path.join(outdir, f"anchors-{k}")
        shutil.rmtree(path, ignore_errors=True)
        sb = tantivy.SchemaBuilder()
        sb.add_text_field("domain", stored=True, tokenizer_name="raw", index_option="basic")
        sb.add_unsigned_field("sources", stored=True, indexed=False, fast=True)
        sb.add_text_field("body", stored=False, tokenizer_name=ex.ANALYZER, index_option="freq")

        def doc(r):
            d = tantivy.Document()
            d.add_text("domain", r[0])
            d.add_unsigned("sources", r[1] or 0)
            d.add_text("body", r[2] or "")
            return d
        os.environ["TANTIVY_THREADS"] = "1"
        ex.THREADS = 1
        res.append(ex.build_index(path, stopfile, sb.build(), iter(rows), doc))
    files = []
    for k in (1, 2):
        path = os.path.join(outdir, f"anchors-{k}")
        meta = json.load(open(os.path.join(path, "meta.json")))
        segs = [s_["segment_id"] for s_ in meta.get("segments", [])]
        names = sorted(os.listdir(path))
        content = {}
        for f in names:
            key = f
            for i, sid in enumerate(segs):
                key = key.replace(sid.replace("-", ""), f"SEG{i}").replace(sid, f"SEG{i}")
            content[key] = ex.sha256_file(os.path.join(path, f))
        files.append({"names": names, "segments": segs, "content": content})
    same_names = files[0]["names"] == files[1]["names"]
    keys = sorted(set(files[0]["content"]) | set(files[1]["content"]))
    diff = [k for k in keys if files[0]["content"].get(k) != files[1]["content"].get(k)]
    # after tv_normalize the two trees must be byte-identical and still open and answer
    trees = []
    for k in (1, 2):
        path = os.path.join(outdir, f"anchors-{k}")
        tv_normalize(path, "repro", "sites/anchors", stopfile)
        trees.append({f: ex.sha256_file(os.path.join(path, f)) for f in sorted(os.listdir(path))})
    idx = tantivy.Index.open(os.path.join(outdir, "anchors-1"))
    idx.register_tokenizer(ex.ANALYZER, ex.analyzer(stopfile))
    idx.reload()
    hits = idx.searcher().search(tantivy.Query.all_query(), 3).count
    ex.report("tvrepro", t0, builds=res, identical_file_names=same_names, files_compared=len(keys),
              files_differing_after_name_mapping=diff, normalized_trees_identical=trees[0] == trees[1],
              normalized_files=len(trees[0]), normalized_index_opens_docs=hits)


# Engine-side check: release_check.py (no build-side imports); these names stay here for the build side.
from release_check import (ENGINE_FORMATS, ENGINE_TANTIVY, REQUIRED_SIGNALS, SQLITE_ROLES,  # noqa: E402,F401
                           check_release)


def cmd_kinds(registry: str, out: str, release: str, dump: str, *flags) -> None:
    """kinds.sqlite from the judged open-tier pg_dump text (kinds_build.py documents the file). Reads no Postgres."""
    opts = {f: flags[i + 1] for i, f in enumerate(flags) if f in ("--vecs", "--embed") and i + 1 < len(flags)}
    kinds_build.build(dump, registry, out, release, opts.get("--vecs"), opts.get("--embed"),
                      stamp=stamp, finish=finish, new_db=new_db)


def cmd_check(rel: str, *rest) -> None:
    tf = "--tranco-free" in rest
    pubs = [r for r in rest if r != "--tranco-free"]
    errs = check_release(rel, pubs[0] if pubs else None, tranco_free=tf)
    print(json.dumps({"stage": "check", "release": rel, "ok": not errs, "errors": errs}))
    if errs:
        sys.exit(1)


if __name__ == "__main__":
    cmd, args = sys.argv[1], sys.argv[2:]
    if cmd == "registry":
        prev = args[args.index("--previous") + 1] if "--previous" in args else None
        stg = args[args.index("--from-stage") + 1] if "--from-stage" in args else None
        tsv = args[args.index("--places-tsv") + 1].split(",") if "--places-tsv" in args else None
        pdir = args[args.index("--places-dir") + 1] if "--places-dir" in args else None
        own = args[args.index("--own-rank") + 1] if "--own-rank" in args else None
        cmd_registry(args[0], args[1], prev, stg, tsv, pdir, own)
    else:
        {"signals": cmd_signals, "sigdump": cmd_sigdump, "pictures": cmd_pictures, "pictures-from": cmd_pictures_from, "stamp": cmd_stamp,
         "sqlguard": cmd_sqlguard, "rowdiff": cmd_rowdiff, "sha": cmd_sha, "idhash": cmd_idhash, "storemap": cmd_storemap,
         "tvrepro": cmd_tvrepro, "tvnormalize": cmd_tvnormalize,
         "tvmonth": cmd_tvmonth, "check": cmd_check, "wikiinput": cmd_wikiinput,
         "web-input": cmd_webinput, "anchor-phrases": cmd_anchorphrases, "kinds": cmd_kinds}[cmd](*args)
