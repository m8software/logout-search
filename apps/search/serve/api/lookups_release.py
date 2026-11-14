"""search_api's exact lookups from a release folder (format v1, SEARCH-RELEASE-FORMAT.md) — T4.

SEARCH_LOOKUP_BACKEND=release, SEARCH_RELEASE_DIR=<release>:
  lookup.sqlite      names (every exact-match tier, one table), cards (one record per article),
                     accounts, entities, sites, links, youtube, stocks, suggestions, FTS5 trigrams
  places-osm.sqlite  OpenStreetMap places (ODbL); places-overture.sqlite Overture gap-fill (CDLA); hours-web.sqlite
                     the businesses' own published hours (facts) — one file per licence
  registry.sqlite    doc_id -> type, key, url, title (T5; a T4 stand-in until it lands)
  signals.bin(.json) one fixed-width record per doc_id: every popularity number
  text/typo/         Tantivy fuzzy index of the suggestion keys
Everything is keyed by doc_id. The release id and format version of every part must agree, or
the engine refuses to start.

ReleaseLookups.execute(SQL, params) answers a search_api SQL constant with the rows Postgres
returns, like lookups_sqlite.SqliteLookups does for the flat v0 file. The exact-match tiers
read names once per search: search_api calls prefetch() with every key the query yields, and
each tier's handler takes its rows from that one result (stage "names").
"""
from __future__ import annotations

import collections
import contextvars
import json
import math
import os
import re
import sqlite3
import struct
import threading
import zlib

import lookups_sqlite as v0
import release_root
from lookups_sqlite import NoPort, Rows, d_km, phrase, pg_lower, qm, sim_counts, similarity, trigrams

FORMAT_VERSION, APP_ID = 1, 0x6C6F7331
WIKI, SITE, PLACE, ACCOUNT, YOUTUBE, STOCK, LINK = 1, 2, 4, 5, 6, 7, 8        # T5's numbering
REGC_MAX, CARDS_MAX, DF_MAX = 100_000, 20_000, 200_000      # shared cache caps (entries), whole process
SITE_CLAIMS_MAX = 100           # site_claims: articles on one domain whose cards are all opened
CHAIN_HOURS_KEEP = 2000          # chain_hours: chains whose web hours are kept between searches (cleared when full)
# SQLite page cache per connection, KiB (was 32 MB: 12 search workers + request threads each grew one;
# the files are read-only and the OS page cache holds them anyway)
SQLITE_CACHE_KB = int(os.environ.get("SEARCH_SQLITE_CACHE_KB", "8000"))
# Released request-thread connections kept for the next request. ThreadingHTTPServer runs each request on a
# new thread; its first lookup opened a connection (lookup + 7 attached files, ~22 MB of page cache, 4 temp fts5vocab
# tables) that outlived the thread: a sqlite3.Connection sits in a reference cycle (its statement cache, CPython 3.11+)
# and is freed only by a full GC, which a heap this size rarely runs. Measured on rc8 at 4 GB: 43-50 open connections
# for 12 workers, SQLite heap 27 MB idle -> 380 MB after 1,000 queries. Now a finished request hands its connection
# back here; past SQLITE_POOL it is closed at once.
SQLITE_POOL = int(os.environ.get("SEARCH_SQLITE_POOL", "4"))
# A byte bound on SQLite's whole heap in this process (sqlite3_soft_heap_limit64), MB; 0 = none. cache_size is per
# connection and per file: PRAGMA cache_size reaches only lookup.sqlite, the 7 attached files keep SQLite's default
# 2,000 KiB each, so one connection holds up to ~22 MB and 16 of them ~350 MB. Under the limit SQLite drops
# clean cache pages instead of growing; results do not change.
SQLITE_HEAP_MB = int(os.environ.get("SEARCH_SQLITE_HEAP_MB", "0"))
N_TITLE, N_ALIAS, N_SITE_LABEL, N_HOST, N_DOMAIN, N_HOST_NAV, N_ACCOUNT, N_YOUTUBE, N_STOCK, N_OFFICIAL, N_SELF, N_TICKER = range(1, 13)
F_KEY, F_HKEY, F_WKEY, F_SKEY = 1, 2, 4, 8
CELL, NLON, SEQ_BITS = 0.5, 720, 20

_prefetched: contextvars.ContextVar = contextvars.ContextVar("names", default=None)

OWNED = v0.OWNED | {"subreddits", "youtube_channels"}


def _null(v, ints=True):
    if ints:
        return None if v < 0 else int(v)
    return None if v != v else float(v)


T5_NAMES = {1: "wiki", 2: "site", 3: "page", 4: "place", 5: "account", 6: "youtube", 7: "stock", 8: "link"}
# our signal name -> T5's field name (type.field) — T5 writes 0 for "none" in integer fields
T5_SIGNAL = {"tranco": "tranco_rank", "cc_hc": "cc_hc_pos", "nav_rank": "nav_rank", "site_rank": "site_rank",
             "sitelinks": "sitelinks", "followers": "followers", "pop": "suggest_pop", "score": "suggest_score",
             "demand": "suggest_demand", "pageviews": "pageviews"}      # pageviews: facts.py's choice between namesakes
ZERO_IS_NONE = frozenset(("tranco", "cc_hc", "nav_rank", "site_rank"))
# G3 D2: the site popularity rank is our own (site.own_rank). The reader keeps asking for "tranco" (the name every
# caller uses); a release that carries site.own_rank answers it from there, an older one from site.tranco_rank.
T5_OWN = {"tranco": "own_rank"}


class Signals:
    """signals.bin: T5's LOSIGNL1 file (one array per type.field, index = doc_id & (1 << type_shift) - 1), or the
    fixed-width stand-in build.py writes without --registry (one record per doc_id)."""

    def __init__(self, root):
        import numpy as np
        self.np = np
        head = open(f"{root}/signals.bin", "rb").read(12)
        self.t5 = head[:8] == b"LOSIGNL1"
        if self.t5:
            n = struct.unpack("<I", head[8:12])[0]
            h = json.loads(open(f"{root}/signals.bin", "rb").read(12 + n)[12:])
            self.shift = h.get("type_shift", 28)
            base = 12 + n
            self.f = {f["name"]: np.memmap(f"{root}/signals.bin", dtype=np.dtype(f["dtype"]), mode="r", offset=base + f["offset"],
                                           shape=(f["count"],)) for f in h["fields"]}
            self.release = h.get("built_release")
            self.own = "site.own_rank" in self.f
        else:
            meta = json.load(open(f"{root}/signals.json"))
            self.arr = np.memmap(f"{root}/signals.bin", dtype=np.dtype([(n, t) for n, t in meta["dtype"]]), mode="r")
            self.release = meta.get("release_id")

    def get(self, d, field, ints=True):
        if self.t5:
            arr = self.f.get(f"{T5_NAMES.get(d >> self.shift)}.{T5_OWN[field] if self.own and field in T5_OWN else T5_SIGNAL[field]}")
            i = d & ((1 << self.shift) - 1)
            if arr is None or i >= len(arr):
                return None
            v = arr[i].item()
            if v != v or (v == 0 and field in ZERO_IS_NONE):
                return None
            return int(v) if ints else float(v)
        return _null(self.arr[d][field], ints)


# The misspelling fallback's candidates (2026-10-08). candidates() reads the m rarest trigram lists, and the
# fallback then looked up and compared every row in any of them: 326,000 article titles a query (40 typo queries,
# up to 1.1 M), which was 3.7 s at the median of the requests that got this far and 21 s at the worst, in no
# named stage. A row in the m rarest lists needs one hit there to be a candidate; once one more list is read it
# needs two, and so on, and most rows have exactly one. The rows that can pass the similarity test are all still
# there, so the answers are the same. Reading is not free (1 µs a row against 20 to fetch and compare a title),
# so the next list is read only while it is shorter than READ_MORE times the rows still standing: with 8 the
# same 40 queries read 721,000 rows instead of 402,000 and compare 13,300 titles (4: 18,700; 16: 9,300, and
# no faster).
READ_MORE = 8


def few_candidates(rare: list, m: int, read, more: float = READ_MORE) -> dict:
    """{rowid: hits} for every row that can still hold all but m - 1 of the trigrams. rare = [(rows, trigram)],
    rarest first; read(trigram) -> its rowids. The m rarest lists are read whole, then more lists while that is
    cheaper than checking the rows left: each raises the hits a row must have by one."""
    cnt = collections.Counter()
    for _n, g in rare[:max(m, 0)]:
        cnt.update(read(g))
    need = 1
    for n, g in rare[max(m, 0):]:
        if n > more * len(cnt):
            break
        ids, need = set(read(g)), need + 1
        cnt = {d: h for d, c in cnt.items() if (h := c + (d in ids)) >= need}
    return cnt


def _front(url: str) -> str:
    """scheme://host/ of a url; the url itself when it has no such form."""
    m = re.match(r"(https?://[^/?#]+)", url or "")
    return m.group(1) + "/" if m else url


class Registry:
    """doc_id -> (type, key, url, title) from T5's registry.sqlite or the stand-in doc table. key is
    the identity build.py keyed things by (wiki page_id, site host, osm id, 'platform TAB handle TAB Q..',
    channel id, ticker, link url); the layout follows build.use_registry."""

    def __init__(self, db):
        self.db = db
        tabs = {r[0] for r in db.execute("SELECT name FROM r.sqlite_master WHERE type = 'table'")}
        self.t5 = "registry" in tabs
        meta = dict(db.execute("SELECT key, value FROM r.meta")) if "meta" in tabs else {}
        self.shift = int(meta.get("type_shift", 28))        # T5: doc_id = type << type_shift | seq
        self.hosts, self.plats = {}, {}

    def _plat(self, hid):
        if hid not in self.plats:
            self.plats[hid] = self.db.execute("SELECT name, prefix, suffix FROM r.platforms WHERE id = ?", (hid,)).fetchone() or ("", "", "")
        return self.plats[hid]

    def row(self, d):
        db = self.db
        if not self.t5:
            r = db.execute("SELECT type, key, url, title FROM r.doc WHERE doc_id = ?", (d,)).fetchone()
            return r or (None, None, None, None)
        r = db.execute("SELECT ext, host_id, path, title, alt FROM r.registry WHERE doc_id = ?", (d,)).fetchone()
        if not r:
            return (None, None, None, None)
        ext, hid, path, title, alt = r
        t = d >> self.shift
        if t == WIKI:
            return (t, str(ext), "https://en.wikipedia.org/wiki/" + (title or "").replace(" ", "_"), title)
        if t == SITE:
            # alt is the page the crawl captured, which may be one level down (gog.com/en, realmadrid.com/en-US,
            # weather.gov/aprfc: the crawl takes such a page when it has no front page). A site result names the
            # site: scheme and host of the capture, nothing after it (rc5 plan W1).
            return (t, path, _front(alt) if alt else f"https://{path}/", title)
        if t == LINK:
            if hid not in self.hosts:
                self.hosts[hid] = (db.execute("SELECT origin FROM r.hosts WHERE id = ?", (hid,)).fetchone() or ("",))[0]
            url = alt or (self.hosts[hid] + (path or ""))
            return (t, url, url, title)
        if t == ACCOUNT:
            name, pre, suf = self._plat(hid)
            return (t, f"{name}\t{path}\tQ{ext}", alt or f"{pre}{path}{suf}", title)
        if t == YOUTUBE:
            return (t, path, f"https://www.youtube.com/channel/{path}", title)
        return (t, path, alt, title)

    def titles(self, ids):
        """(doc_id, title) of many docs, in doc_id order: row(d)[3] in one statement per 30,000 (the fuzzy
        fallback compares thousands of titles and uses nothing else of the row)."""
        tab = "r.registry" if self.t5 else "r.doc"
        for i in range(0, len(ids), 30000):
            chunk = ids[i:i + 30000]
            yield from self.db.execute(f"SELECT doc_id, title FROM {tab} WHERE doc_id IN ({qm(chunk)}) ORDER BY doc_id", chunk)

    def account(self, d):
        """(platform, handle, url, entity Q number) of an account doc."""
        if not self.t5:
            _t, key, url, _ti = self.row(d)
            platform, handle, q = key.split("\t")
            return platform, handle, url, int(q[1:]) if q[1:].isdigit() else None
        ext, hid, path, alt = self.db.execute("SELECT ext, host_id, path, alt FROM r.registry WHERE doc_id = ?", (d,)).fetchone()
        name, pre, suf = self._plat(hid)
        return name, path, alt or f"{pre}{path}{suf}", ext


def title_extra(title: str | None, key: str) -> int:
    """How many characters a title has beyond the key it normalises to: 0 for "CNN" / "cnn", 1 for "CNN+",
    2 for "Get Out!!" / "getout". Spaces do not count."""
    return max(0, len(re.sub(r"\s", "", title or "")) - len(key))


def soft_heap_limit(n_bytes: int) -> str:
    """Ask the SQLite library this process uses to keep its heap under n_bytes (sqlite3_soft_heap_limit64, a soft
    limit: cache pages are released, nothing fails). -> one line for the log; best effort, the engine runs on without
    it when the library cannot be reached (another libsqlite3 than Python's would be a no-op, not a bound)."""
    import ctypes
    import ctypes.util
    try:
        lib = ctypes.CDLL(ctypes.util.find_library("sqlite3") or "libsqlite3.so.0")
        lib.sqlite3_soft_heap_limit64.restype = ctypes.c_int64
        lib.sqlite3_soft_heap_limit64.argtypes = [ctypes.c_int64]
        was = int(lib.sqlite3_soft_heap_limit64(ctypes.c_int64(n_bytes)))
        return f"soft limit {n_bytes >> 20} MB (was {was >> 20} MB)"
    except (OSError, AttributeError) as e:
        return f"soft limit not set ({type(e).__name__}: {e})"


class ReleaseLookups:
    name = "release"

    def __init__(self, root: str, s, stages=None):
        import numpy as np
        self.root, self.s = release_root.resolve(root).rstrip("/"), s
        self._local = threading.local()
        self._free: list = []                  # (connection, Registry) released by finished request threads (SQLITE_POOL)
        self._free_lock = threading.Lock()
        if SQLITE_HEAP_MB:
            print(f"sqlite heap: {soft_heap_limit(SQLITE_HEAP_MB << 20)}", flush=True)
        self.sig = Signals(self.root)
        import os
        self.pfiles = [("p", "places-osm")] + ([("o", "places-overture")] if os.path.exists(f"{self.root}/places-overture.sqlite") else [])
        self.has_hw = os.path.exists(f"{self.root}/hours-web.sqlite")
        self.has_pics = os.path.exists(f"{self.root}/pictures.sqlite")      # card pictures: an optional download
        self.has_anchor_phrases = os.path.exists(f"{self.root}/anchor-phrases.sqlite")   # exact_anchor (own licence)
        self.has_kinds = os.path.exists(f"{self.root}/kinds.sqlite")        # the open kinds tier (own licence, D2); absent = kinds off
        db = self.db
        # Stack Exchange attribution (2026-10-22): registry.authors, absent in a release built before it (rc9 and older)
        self.has_authors = db.execute("SELECT 1 FROM r.sqlite_master WHERE type = 'table' AND name = 'authors'").fetchone() is not None
        for alias, role in ((("main", "lookup"),) + tuple(self.pfiles) + ((("h", "hours-web"),) if self.has_hw else ())
                            + ((("pic", "pictures"),) if self.has_pics else ())
                            + ((("ap", "anchor-phrases"),) if self.has_anchor_phrases else ())
                            + ((("kd", "kinds"),) if self.has_kinds else ())):
            appid = db.execute(f"PRAGMA {alias}.application_id").fetchone()[0]
            uv = db.execute(f"PRAGMA {alias}.user_version").fetchone()[0]
            meta = dict(db.execute(f"SELECT key, value FROM {alias}.meta"))
            if appid != APP_ID or uv != FORMAT_VERSION or meta.get("role") != role or not meta.get("built_release"):
                raise RuntimeError(f"release {self.root}: {alias} is application {appid:#x} / format {uv} / role {meta.get('role')}, "
                                   f"want {APP_ID:#x} / {FORMAT_VERSION} / {role} — refusing")
        stamp = json.load(open(f"{self.root}/text/typo/release.json"))
        if stamp.get("format") != FORMAT_VERSION or stamp.get("role") != "typo":
            raise RuntimeError(f"release {self.root}: typo index is stamped {stamp} — refusing")
        self.release_id = dict(db.execute("SELECT key, value FROM meta"))["built_release"]
        self.dict = dict(db.execute("SELECT id, s FROM dict"))
        self.sid = {v: k for k, v in self.dict.items()}
        self.pdict = {a: dict(db.execute(f"SELECT id, s FROM {a}.dict")) for a, _r in self.pfiles}
        self.pkind = {a: {v: k for k, v in d.items()} for a, d in self.pdict.items()}
        self.place_doc: dict = {}              # place id (as the engine sees it) -> (doc_id, places file, pid), for hours
        self.tables = frozenset(r[0] for r in db.execute("SELECT name FROM sqlite_schema WHERE type = 'table'")) | {"places"}
        import tantivy
        self.tv = tantivy
        import tv_open                  # read-only release folders need writable lock files
        idx = tv_open.open_index(tantivy, f"{self.root}/text/typo")
        self.tv_searcher, self.tv_schema = idx.searcher(), idx.schema
        self.typo_keys = self.tv_searcher.num_docs
        self.names_queries = 0
        s_ = s
        P = s_.PLACES_SQL
        self._pid_map, self._pid_lock = None, threading.Lock()
        self._junk = None
        self._chain_hours: dict = {}           # chain name keys -> its branches' web hours (chain_hours)
        # Shared by every thread, each capped (cleared when full). They were per thread: 12 search
        # workers plus each request's own thread kept a card cache of up to 20,000 decoded cards and
        # an unbounded trigram-count cache; the release engine was OOM-killed at 2 GB (rc3 acceptance).
        self._regc: dict = {}
        self._cards: dict = {}
        self._df: dict = {}
        self._claims: dict = {}
        self._yt_ix = None
        self.h = {
            s_.SUGGEST_V2_SQL: lambda p: [(True,)],
            s_.SEM_IVF_DOCS_SQL: self.ivf_docs,
            P.format(match=s_.PLACE_MATCH_KINDS): self.places_kinds,
            P.format(match=s_.PLACE_MATCH_NAME): self.places_name,
            s_.PLACE_EXACT_SQL: self.place_exact,
            s_.PLACE_TOWN_SQL: self.place_town,
            s_.HOURS_INFO_SQL: self.hours_info,
            s_.CHAIN_HOURS_SQL: self.chain_hours,
            s_.TYPO_SQL: self.typo,
            s_.TYPO_NEAR_SQL: self.near_names,
            s_.TYPO_NEAR_MORE_SQL: self.near_more,
            s_.SUGGEST_TOP2_SQL: self.suggest_top2,
            s_.SUGGEST_SCAN2_SQL: self.suggest_scan2,
            s_.SUGGEST_EXACT2_SQL: lambda p: self.items_rows(
                [(d, ps) for d, ps in self.db.execute("SELECT doc_id, pos FROM suggest_keys WHERE key = ?", (p["p"],))]),
            s_.SUGGEST_PHRASES_SQL: self.phrases,
            s_.WIKI_EXACT_SITE_SQL: self.wiki_exact_site,
            s_.WIKI_EXACT_SQL: lambda p: [(self.title(d), self.card(d).get("l")) for d in self.names1(p["nq"], N_ALIAS)][:3],
            s_.FAMOUS_LEAD_SQL: lambda p: [(self.card(d).get("l"),) for d in self.names1(p["t"], N_TITLE)][:1],
            s_.TOWN_TITLE_SQL: lambda p: [(self.title(d),) for t in p["t"] for d in self.names1(t, N_TITLE)],
            s_.FACTS_SQL: self.facts,
            s_.FACT_TITLES_SQL: lambda p: [(t,) for t in dict.fromkeys(self.title(d) for k in p["keys"] for d in self.names1(k, N_ALIAS))][:3],
            s_.PERSON_SQL: lambda p: [(1,)] if any(self.has_fact(d, "born") for d in self.names1(p["t"], N_TITLE)) else [],
            s_.PROFILES_SQL: self.profiles,
            s_.ACCOUNT_SQL: self.account,
            s_.LINK_SITE_SQL: lambda p: self.link_accounts(p["key"], "site", 1),
            s_.DESCRIBED_SQL: lambda p: self.link_accounts(p["key"], "described", 3),
            s_.RELATED_SQL: self.related,
            s_.CARD_SITE_SQL: lambda p: [((self.card(d).get("o") or [None])[0],) for d in self.names1(p["t"], N_TITLE)[:1]],
            s_.CARD_DESCR_SQL: lambda p: [(c["d"],) for c in (self.card(d) for d in self.names1(p["t"], N_TITLE)) if c.get("d")][:1],
            s_.WIKI_DEMAND_SQL: self.wiki_demand,
            s_.FAMOUS_SQL: self.famous,
            s_.ORG_SQL: self.org,
            s_.ORG_BY_SITE_SQL: self.org_by_site,
            s_.MAJOR_TEAM_SQL: lambda p: self.team(p, with_box=False),
            s_.PRODUCT_SQL: self.product,
            s_.BROWSER_TEAM_SQL: lambda p: self.team(p, with_box=True),
            s_.NAV_SITES_SQL: self.nav_sites,
            s_.HOST_NAV_SQL: self.host_nav,
            s_.DOMAIN_NAV_SQL: self.domain_nav,
            s_.DOMAIN_DOC_SQL: self.domain_doc,
            s_.NAV_SQL: self.nav,
            s_.SELF_NAMED_SQL: self.self_named,
            s_.FUZZY_SITE_SQL: self.fuzzy_site,
            s_.FUZZY_WIKI_SQL: self.fuzzy_wiki,
            s_.JUNK_SQL: self.junk,
            s_.STOCK_SQL: self.stock,
            s_.STOCK_BARE_SQL: self.stock_bare,
            s_.YT_EXACT_SQL: self.yt_exact,
            s_.YT_BARE_SQL: self.yt_bare,
            s_.YT_SEARCH_SQL: self.yt_search,
        }
        self.kinds = None
        if self.has_kinds:
            import kinds_release
            self.kinds = kinds_release.KindsRelease(self)

    # ------------------------------------------------------------------ plumbing
    @property
    def db(self):
        c = getattr(self._local, "db", None)
        if c is None:
            with self._free_lock:
                kept = self._free.pop() if self._free else None
            if kept is not None:                # a connection a finished request released, caches warm
                self._local.db, self._local.registry = kept
                return kept[0]
            ro = "?mode=ro&immutable=1"
            c = sqlite3.connect(f"file:{self.root}/lookup.sqlite{ro}", uri=True, check_same_thread=False)
            for a, role in self.pfiles:
                c.execute(f"ATTACH 'file:{self.root}/{role}.sqlite{ro}' AS {a}")
            if self.has_hw:
                c.execute(f"ATTACH 'file:{self.root}/hours-web.sqlite{ro}' AS h")
            if self.has_pics:
                c.execute(f"ATTACH 'file:{self.root}/pictures.sqlite{ro}' AS pic")
            if self.has_anchor_phrases:
                c.execute(f"ATTACH 'file:{self.root}/anchor-phrases.sqlite{ro}' AS ap")
            if self.has_kinds:
                c.execute(f"ATTACH 'file:{self.root}/kinds.sqlite{ro}' AS kd")
            c.execute(f"ATTACH 'file:{self.root}/registry.sqlite{ro}' AS r")
            c.execute(f"PRAGMA cache_size = -{SQLITE_CACHE_KB}")     # per connection (each worker thread has one)
            for v, fts in ((f"pn_vocab_{a}", f"{a}, place_names") for a, _r in self.pfiles):
                c.execute(f"CREATE VIRTUAL TABLE IF NOT EXISTS temp.{v} USING fts5vocab({fts}, row)")
            for v, fts in (("wt_vocab", "main, wiki_trgm"), ("lt_vocab", "main, label_trgm")):
                c.execute(f"CREATE VIRTUAL TABLE IF NOT EXISTS temp.{v} USING fts5vocab({fts}, row)")
            self._local.db = c
            self._local.registry = Registry(c)
        return c

    def release(self) -> None:
        """The calling thread is done with its connection (a request answered): it goes to the free list for the next
        request thread, or is closed when SQLITE_POOL are already waiting — never left to the cyclic GC. The
        12 search workers never call this; they keep theirs."""
        c = getattr(self._local, "db", None)
        if c is None:
            return
        pair = (c, self._local.registry)
        self._local.db = self._local.registry = None
        with self._free_lock:
            if len(self._free) < SQLITE_POOL:
                self._free.append(pair)
                return
        c.close()

    def q(self, sql, *args):
        return self.db.execute(sql, args).fetchall()

    def present(self, pg_tables: frozenset) -> frozenset:
        mine = {t for t in OWNED if t in self.tables or (t == "site_quality" and "sites" in self.tables)
                or (t == "wiki_aliases" and "names" in self.tables) or (t in ("wiki_sites", "wiki_links", "wiki_next", "wiki_facts")
                                                                      and "cards" in self.tables)
                or (t in ("nav_sites", "host_nav") and "names" in self.tables) or (t == "stock_tickers" and "stocks" in self.tables)
                or (t == "stock_keys" and "stocks" in self.tables) or (t == "youtube_channels" and "youtube" in self.tables)}
        return frozenset((pg_tables - OWNED) | mine | (self.kinds.tables() if self.kinds else frozenset()))

    def execute(self, sql: str, params=None) -> Rows:
        h = self.h.get(sql)
        if h is not None:
            return Rows(h(params or {}))
        rows = self.kinds.handle(sql, params) if self.kinds else None
        if rows is not None:
            return Rows(rows)
        raise NoPort("lookup SQL with no release port: " + " ".join(sql.split())[:100])

    def junk(self, _p=None):
        """JUNK_SQL: the hosts of the UT1-flagged sites, read once (search_api asks again every hour; the release
        does not change, and the read is a pass over sites plus a registry row for each, ~0.2 s)."""
        if self._junk is None:
            self._junk = [(self.reg(d)[1],) for (d,) in self.db.execute("SELECT doc_id FROM sites WHERE ut1 >= 1").fetchall()]
        return self._junk

    def reg(self, d):
        """doc_id -> (type, key, url, title), cached (shared, REGC_MAX)."""
        self.db
        cache = self._regc
        r = cache.get(d)
        if r is None:
            r = self._local.registry.row(d)
            if len(cache) > REGC_MAX:
                cache.clear()
            cache[d] = r
        return r

    def title(self, d):
        return self.reg(d)[3]

    def page_credits(self, d):
        """{role: (user_id, name)} of a Stack Exchange page (registry.authors: 1 asked, 2 answered), or None for any other
        page and for a release without the table."""
        if not self.has_authors:
            return None
        rows = self.db.execute("SELECT role, user_id, name FROM r.authors WHERE doc_id = ?", (d,)).fetchall()
        return {role: (uid, name) for role, uid, name in rows} or None

    def anchor_phrase(self, key: str):
        """[(site doc_id, linking sites)] for an anchor phrase, most-linked first, at most 2 (exact_anchor)."""
        if not self.has_anchor_phrases:
            return None
        return self.db.execute("SELECT doc_id, sources FROM ap.anchor_phrase WHERE anchor = ? "
                               "ORDER BY sources DESC, doc_id LIMIT 2", (key,)).fetchall()

    # ------------------------------------------------------------------ meaning search hits
    def pid_map(self):
        """Wikipedia page_id -> doc_id as two sorted arrays (8 bytes an article, built once): the
        IVF folder's ids.i32 are page_ids and the registry keeps them in `ext`, unindexed — with
        stable ids (registry --previous) doc_id order is no longer page_id order."""
        if self._pid_map is None:
            with self._pid_lock:
                if self._pid_map is None:
                    import numpy as np
                    self.db                                   # opens this thread's connection + registry
                    reg = self._local.registry
                    if reg.t5:
                        lo, hi = WIKI << reg.shift, (WIKI + 1) << reg.shift
                        rows = self.db.execute("SELECT ext, doc_id FROM r.registry WHERE doc_id >= ? AND doc_id < ?", (lo, hi))
                    else:
                        rows = self.db.execute("SELECT CAST(key AS INTEGER), doc_id FROM r.doc WHERE type = ?", (WIKI,))
                    a = np.array(rows.fetchall(), dtype=np.int64).reshape(-1, 2)
                    a = a[np.argsort(a[:, 0], kind="stable")]
                    # page_ids and wiki doc_ids (1 << 28 | seq) both fit int32: 8 bytes an article, not 16
                    self._pid_map = (a[:, 0].astype(np.int32), a[:, 1].astype(np.int32))
                    del a
        return self._pid_map

    def ivf_docs(self, p):
        """SEM_IVF_DOCS_SQL: (page_id, title, lead) for the IVF hits, from the registry and the cards."""
        import numpy as np
        pids, docs = self.pid_map()
        out = []
        for pid in p["ids"]:
            i = int(np.searchsorted(pids, pid))
            if i < len(pids) and pids[i] == pid:
                d = int(docs[i])
                out.append((pid, self.title(d), self.card(d).get("l")))
        return out

    def card(self, d) -> dict:
        self.db
        cache = self._cards
        c = cache.get(d)
        if c is None:
            row = self.db.execute("SELECT c FROM cards WHERE doc_id = ?", (d,)).fetchone()
            c = json.loads(zlib.decompress(row[0]) if isinstance(row[0], bytes) else row[0]) if row else {}
            if len(cache) > CARDS_MAX:
                cache.clear()
            cache[d] = c
        return c

    def has_fact(self, d, fact):
        fid = self.sid.get(fact)
        return any(f[0] == fid for f in self.card(d).get("f", []))

    def S(self, d, field, ints=True):
        return self.sig.get(d, field, ints)

    # ------------------------------------------------------------------ names: the exact tiers
    def prefetch(self, pairs) -> None:
        """ONE names query for every (name, kind) the query yields; tier handlers read from it."""
        pairs = sorted({(n, k) for n, k in pairs if n})
        got: dict = {}
        if pairs:
            self.names_queries += 1
            for i in range(0, len(pairs), 400):
                chunk = pairs[i:i + 400]
                vals = ",".join("(?,?)" for _ in chunk)
                for n, k, d, f in self.db.execute(f"SELECT name, kind, doc_id, flags FROM names WHERE (name, kind) IN (VALUES {vals})",
                                                  [x for p in chunk for x in p]):
                    got.setdefault((n, k), []).append((d, f))
        _prefetched.set((set(pairs), got))

    def names(self, name, kind):
        pre = _prefetched.get()
        if pre is not None and (name, kind) in pre[0]:
            return pre[1].get((name, kind), [])
        self.names_queries += 1
        return self.q("SELECT doc_id, flags FROM names WHERE name = ? AND kind = ?", name, kind)

    def names1(self, name, kind):
        return list(dict.fromkeys(d for d, _f in self.names(name, kind)))

    def titles_starting(self, prefix, kind, limit):
        """[(doc_id, title)] of the docs with a name of this kind that STARTS with prefix (facts.py's bare-title
        rule: "lassiter" -> the names "lassiterfilm", ...), or None when more than `limit` names do: the
        caller must not decide from a part of them. Two statements, whatever the count."""
        ids = [d for (d,) in self.q("SELECT doc_id FROM names WHERE name >= ? AND name < ? AND kind = ? LIMIT ?",
                                    prefix, prefix + "\U0010ffff", kind, limit + 1)]
        if len(ids) > limit:
            return None
        return list(self._local.registry.titles(sorted(set(ids)))) if ids else []

    # ------------------------------------------------------------------ sites and navigation
    def site(self, d):
        r = self.db.execute("SELECT home, description, domain, ut1 FROM sites WHERE doc_id = ?", (d,)).fetchone()
        return r or (None, None, None, None)

    def site_claims(self, host):
        """(the site has a title of its own, ((title, infobox kind, sitelinks, lead, short description, the host
        or its first label is one of the article's names), ...)) for a host: the articles whose official site is
        exactly this host's front page (search_api.site_name). Cached per host, shared, capped like the cards.
        A busy domain is the official site of thousands of articles, each a card to open to see whether it means
        the front page: nytimes.com 2,248 (1.5 s), myspace.com 1.1 s, apple.com 543 (0.3 s) on the first lookup
        (loaded workstation, 2026-10-09). Past SITE_CLAIMS_MAX articles only those the address is a name of are
        opened (apple.com: "Apple" is Apple Inc.), and the cards are read past the card cache so one site does not
        empty it."""
        cache = self._claims
        got = cache.get(host)
        if got is None:
            titled, rows = False, []
            for d in self.names1(host, N_DOMAIN)[:1]:
                titled = bool(self.reg(d)[3])
                dom = self.dict.get(self.site(d)[2]) or host
                aka = {a for k in {re.sub(r"[^a-z0-9]", "", host), re.sub(r"[^a-z0-9]", "", host.split(".")[0])}
                       if len(k) >= 2 for a in self.names1(k, N_ALIAS)}
                docs = self.names1(dom, N_OFFICIAL)
                for w in docs if len(docs) <= SITE_CLAIMS_MAX else [w for w in docs if w in aka]:
                    row = self.db.execute("SELECT c FROM cards WHERE doc_id = ?", (w,)).fetchone()
                    c = json.loads(zlib.decompress(row[0]) if isinstance(row[0], bytes) else row[0]) if row else {}
                    o = c.get("o")
                    if o and o[0] and self.s.front_host(o[0]) == host:
                        rows.append((self.title(w), self.dict.get(o[1]) if o[1] else None, self.S(w, "sitelinks"),
                                     (c.get("l") or "")[:400], c.get("d"), w in aka))
            got = (titled, tuple(rows))
            if len(cache) > CARDS_MAX:
                cache.clear()
            cache[host] = got
        return got

    def domain_doc(self, p):
        for d in self.names1(p["d"], N_DOMAIN):
            home, desc, _dom, _u = self.site(d)
            if home:
                _t, _k, url, title = self.reg(d)
                return [(url, title, desc, self.S(d, "tranco"))]
        return []

    def domain_nav(self, p):
        out = [(self.S(d, "nav_rank"),) for d in self.names1(p["d"], N_DOMAIN) if self.S(d, "nav_rank") is not None]
        return sorted(out)[:1]

    def nav(self, p):
        gen = re.compile(p["generic"])
        rows = []
        for k in p["keys"]:
            for d in self.names1(k, N_SITE_LABEL):
                _t, host, url, title = self.reg(d)
                rows.append((url, title, self.site(d)[1], self.S(d, "tranco"), 0, host, self.S(d, "cc_hc")))
        rows = list(dict.fromkeys(rows))
        rows.sort(key=lambda r: (not gen.search(r[5]), r[3] is None, r[3] or 0, r[6] is None, r[6] or 0))
        return [r[:6] for r in rows[:3]]

    def nav_sites(self, p):
        rows = {(self.reg(d)[1], self.S(d, "nav_rank")) for k in p["keys"] for d in self.names1(k, N_HOST)}
        return sorted(rows, key=lambda r: (r[1] is None, r[1] or 0))[:5]

    def host_nav(self, p):
        rows = []
        for k in p["keys"]:
            for d in self.names1(k, N_HOST_NAV):
                _t, url, _u, title = self.reg(d)
                desc = (self.db.execute("SELECT description FROM links WHERE doc_id = ?", (d,)).fetchone() or (None,))[0]
                rows.append((url, title, desc, self.S(d, "nav_rank")))
        return sorted(rows, key=lambda r: (r[3] is None, r[3] or 0))[:2]

    def self_named(self, p):
        sub = p["like"].strip("%")
        rows = []
        for d in self.names1(sub, N_SELF):
            _t, host, url, title = self.reg(d)
            rows.append((url, title, self.site(d)[1], host, self.S(d, "tranco")))
        rows.sort(key=lambda r: (r[4] is None, r[4] or 0))
        return rows[:30]

    # ------------------------------------------------------------------ wiki
    def wiki_exact_site(self, p):
        nq = p["nq"]
        rows = []
        for d in self.names1(nq, N_ALIAS):
            t = self.title(d)
            c = self.card(d)
            o = c.get("o") or [None, None, None]
            url, box = o[0], self.dict.get(o[1]) if o[1] else None
            if (t or "").startswith(".") or re.match(r"^https?://(www\.)?(nic|registry)\.", url or ""):
                continue
            dem = self.S(d, "demand", False) if self.item_kind(d) == "wiki" else None
            rows.append((t, c.get("l"), url, box, self.S(d, "site_rank"), 0, pg_lower(v0.SqliteLookups._NONALNUM.sub("", t or "")) == nq,
                         dem if dem is not None else -1.0))
        rows = list(dict.fromkeys(rows))
        # Postgres leaves equal (own title, site rank) in arbitrary order; here the more searched
        # article wins the tie ("get out": the film, not "Get Out!!").
        # Two titles can be the query once punctuation is dropped: "cnn" is "CNN" and "CNN+", and CNN+ (a streaming
        # service closed in 2022, plus.cnn.com) led cnn.com (2026-10-09). The title with nothing dropped is the one
        # typed: fewest characters beyond the key first, before the rank.
        rows.sort(key=lambda r: (not r[6], title_extra(r[0], nq) if r[6] else 0, r[4] is None, r[4] or 0, -r[7]))
        return [r[:7] for r in rows[:3]]

    def wiki_demand(self, p):
        out = []
        for t in p["t"]:
            ds = [self.S(d, "demand", False) for d in self.names1(t, N_TITLE) if self.item_kind(d) == "wiki"]
            ds = [x for x in ds if x is not None]
            if ds:
                out.append((t, v0.r32(max(ds))))
        return out

    def item_kind(self, d):
        r = self.db.execute("SELECT kind FROM suggest_items WHERE doc_id = ?", (d,)).fetchone()
        return self.dict.get(r[0]) if r else None

    def facts(self, p):
        want = {self.sid.get(f) for f in p["facts"]}
        rows = []
        for k in p["keys"]:
            for d in self.names1(k, N_ALIAS):
                c = self.card(d)
                t = self.title(d)
                dem = self.S(d, "demand", False) if self.item_kind(d) == "wiki" else None
                for f in c.get("f", []):
                    if f[0] in want:
                        rows.append((t, self.dict[f[0]], f[1], self.dict.get(f[2]) if f[2] else None, f[3], f[4],
                                     v0.r32(dem) if dem is not None else 0, c.get("t", 0)))
        rows = list(dict.fromkeys(rows))
        rows.sort(key=lambda r: (-(r[6] or 0), r[7]))
        return [r[:7] for r in rows[:40]]

    def related(self, p):
        out = []
        for d in self.names1(p["t"], N_TITLE)[:1]:
            for r in self.card(d).get("r", [])[:p["n"]]:
                out.append((self.title(r), self.card(r).get("l")))
        return out

    def account_row(self, d):
        r = self.db.execute("SELECT vord, hk, sk FROM accounts WHERE doc_id = ?", (d,)).fetchone()
        if not r:
            return None
        vord, hk, sk = r
        self.db
        platform, handle, url, entity = self._local.registry.account(d)
        e = self.db.execute("SELECT name, description FROM entities WHERE id = ?", (entity,)).fetchone() if entity is not None else None
        name, desc = e if e else (self.reg(d)[3], None)
        qid = str(entity) if entity is not None else None
        return {"platform": platform, "url": url, "name": name, "desc": desc,
                "value": handle, "sitelinks": self.S(d, "sitelinks"), "vord": vord, "hk": hk, "sk": sk, "qid": qid}

    def profiles(self, p):
        want = set(p["p"])
        rows = []
        for d in self.names1(p["t"], N_TITLE)[:1]:
            for a in self.card(d).get("p", []):
                r = self.account_row(a)
                if r and r["platform"] in want:
                    rows.append((r["platform"], r["url"], r["name"], r["desc"], r["value"], r["sitelinks"]))
        return sorted(rows, key=lambda r: r[0])

    def account(self, p):
        key, keys, plats = p["key"], p["keys"], set(p["p"])
        match: dict = {}
        for n in set(keys) | {key}:
            for d, f in self.names(n, N_ACCOUNT):
                bits = f & 15
                ok = (n in keys and bits & (F_KEY | F_WKEY)) or (n == key and bits & (F_HKEY | F_SKEY))
                if ok:
                    match.setdefault(d, set()).add((n, bits))
        cands = []
        for d, ms in match.items():
            r = self.account_row(d)
            if not r or r["platform"] not in plats:
                continue
            exact = {b for n, b in ms if n == key}
            via = 0
            for b in exact:
                via |= b
            hk_eq = None if not r["hk"] else bool(via & F_HKEY)
            # short_only = skey = key AND key NOT IN (key, hkey, wkey); NULL when there is no skey
            if not r["sk"]:
                short = None if not (via & (F_KEY | F_HKEY | F_WKEY)) else False
            else:
                short = bool(via & F_SKEY) and not (via & (F_KEY | F_HKEY | F_WKEY))
            cands.append((r, hk_eq, short))
        best = {}
        for r, hk_eq, short in sorted(cands, key=lambda x: (x[0]["qid"] or "", 0 if x[1] is None else (1 if x[1] else 2),
                                                             len(x[0]["value"] or ""))):
            best.setdefault(r["qid"], (r, short))
        so = lambda v: 2 if v is None else (1 if v else 0)
        rows = sorted(best.values(), key=lambda x: (so(x[1]), x[0]["sitelinks"] is not None, -(x[0]["sitelinks"] or 0)))
        return [(r["platform"], r["url"], r["name"], r["desc"], r["value"], r["sitelinks"]) for r, _s in rows[:2]]

    def link_accounts(self, key, platform, n):
        rows = []
        for d, f in self.names(key, N_ACCOUNT):
            if not f & (F_KEY | F_WKEY):
                continue
            r = self.account_row(d)
            if not r or r["platform"] != platform:
                continue
            rows.append(r)
        rows = list({r["url"] + "\t" + (r["qid"] or ""): r for r in rows}.values())
        if platform == "site":
            rows = [r for r in rows if (r["sitelinks"] or 0) >= self.s.LINK_SITE_MIN_SITELINKS]   # search_api's floor
            rows.sort(key=lambda r: (-(r["sitelinks"] or 0), r["vord"] or 0))
            return [(r["url"], r["name"], r["desc"], r["sitelinks"]) for r in rows[:n]]
        rows.sort(key=lambda r: (r["sitelinks"] is not None, -(r["sitelinks"] or 0)))
        return [(r["url"], r["name"], r["desc"]) for r in rows[:n]]

    def org(self, p):
        k = p["k"]
        hosts = {d: self.S(d, "nav_rank") for d in self.names1(k, N_HOST)}
        doms = {}
        for d, rk in hosts.items():
            dom = self.site(d)[2]
            if dom:
                doms.setdefault(self.dict[dom], []).append(rk)
        rows = []
        for w in self.names1(k, N_ALIAS):
            o = self.card(w).get("o")
            if o and o[2]:
                sd = self.reg(o[2])[1]
                for rk in doms.get(sd, []):
                    t = self.title(w)
                    rows.append((rk, t, re.sub(r"[^a-z0-9]", "", pg_lower(t or "")) != k, self.s.front_host(o[0]) is None,
                                 -(self.S(w, "demand", False) or 0.0), title_extra(t, k)))
        # Several articles can have their official site on the named domain, all with that domain's rank: "nike" is
        # Nike, Inc. (nike.com) and Nike+iPod (nike.com/nike-app), and the first one read won, so the card, the
        # article slot and the accounts went to Nike+iPod (2026-10-09). The one whose official site is the
        # article named exactly the query first ("dell" is Dell, not the better-searched Dell Technologies; "samsung"
        # the group, not Samsung Electronics), then the one whose official site is the front page, then the most
        # searched, then by title so the answer does not depend on read order.
        # ... and of two titles that are the query once punctuation is dropped ("CNN", "CNN+"), the one with nothing
        # dropped (wiki_exact_site orders them the same way, so the article slot and the site agree).
        rows.sort(key=lambda r: (r[0] is None, r[0] or 0, r[2], r[5] if not r[2] else 0, r[3], r[4], r[1] or ""))
        return [r[:2] for r in rows[:1]]

    def org_by_site(self, p):
        k, top = p["k"], p["top"]
        rows = []
        for d in self.names1(k, N_HOST):
            rk = self.S(d, "nav_rank")
            dom = self.site(d)[2]
            if rk is None or rk > top or not dom:
                continue
            for w in self.names1(self.dict[dom], N_OFFICIAL):
                t = self.title(w) or ""
                if re.sub(r"[^a-z0-9]", "", pg_lower(t)).startswith(k):
                    rows.append((rk, t))
        rows = list(dict.fromkeys(rows))
        rows.sort(key=lambda r: (r[0], len(r[1])))
        return rows[:5]

    # ------------------------------------------------------------------ suggestions
    def items_rows(self, docs_pos):
        out = []
        for d, ps in docs_pos:
            r = self.db.execute("SELECT kind, name, url, descr, wiki FROM suggest_items WHERE doc_id = ?", (d,)).fetchone()
            if not r:
                continue
            kind, name, url, descr, wiki = r
            t, key, rurl, title = self.reg(d)
            kind = self.dict.get(kind)
            out.append((d, kind, name, url or rurl, key if t == SITE else None,
                        title if t == WIKI else (self.title(wiki) if wiki else None),
                        v0.r32(self.S(d, "pop", False)), 0.0, v0.r32(self.S(d, "demand", False)) or 0.0, descr, ps))
        return out

    def suggest_top2(self, p):
        row = self.db.execute("SELECT docs, pos FROM suggest_top WHERE prefix = ?", (p["p"],)).fetchone()
        return self.items_rows(list(zip(json.loads(row[0]), json.loads(row[1])))) if row else []

    def suggest_scan2(self, p):
        pre = p["like"][:-1]
        grp: dict = {}
        for d, ps in self.db.execute("SELECT doc_id, pos FROM suggest_keys WHERE key >= ? AND key < ? || char(1114111)", (pre, pre)):
            grp[d] = min(ps, grp.get(d, 9))
        by_kind: dict = {}
        for d, ps in grp.items():
            sc = self.S(d, "score", False)
            by_kind.setdefault(self.item_kind(d), []).append(((sc if sc is not None else -1e9) - ps, d, ps))
        keep = []
        for _k, v in by_kind.items():
            v.sort(key=lambda x: -x[0])
            keep += [(d, ps) for _s, d, ps in v[:12]]
        return self.items_rows(keep)

    def phrases(self, p):
        r = self.db.execute("SELECT phrases FROM suggest_phrases WHERE prefix = ?", (p["p"],)).fetchone()
        if not r:
            return []
        ph = json.loads(r[0])
        # the build kept only phrases searched >= PHRASE_MIN_SEARCHES; the counts stay out of the file
        return [(ph, [self.s.PHRASE_MIN_SEARCHES] * len(ph))]

    def famous(self, p):
        out = []
        for d, ps in self.db.execute("SELECT doc_id, pos FROM suggest_keys WHERE key = ? AND pos IN (0, 1)", (p["k"],)):
            r = self.items_rows([(d, ps)])
            if r and r[0][1] in ("wiki", "site", "youtube"):
                x = r[0]
                out.append((x[1], x[2], x[5], x[8] or 0.0, ps))
        out.sort(key=lambda r: -(r[3] or 0))
        return out[:40]

    def product(self, p):
        best: dict = {}
        for (d,) in self.db.execute("SELECT doc_id FROM suggest_keys WHERE key = ? AND pos = 1", (p["k"],)):
            if self.item_kind(d) != "wiki":
                continue
            o = self.card(d).get("o")
            if not o or not o[0]:
                continue
            name = self.db.execute("SELECT name FROM suggest_items WHERE doc_id = ?", (d,)).fetchone()[0]
            k = (self.title(d), name, o[0])
            best[k] = max(best.get(k, 0.0), v0.r32(self.S(d, "demand", False)) or 0.0)
        rows = [k + (v,) for k, v in best.items()]
        rows.sort(key=lambda r: -r[-1])
        return rows[:60]

    def team(self, p, with_box):
        boxes = set(p.get("boxes") or ["infobox nfl team"])
        nfl, nba = "National Football League", "National Basketball Association"
        best: dict = {}
        for d, ps in self.db.execute("SELECT doc_id, pos FROM suggest_keys WHERE key = ? AND pos IN (0, 1)", (p["k"],)):
            if self.item_kind(d) != "wiki":
                continue
            c = self.card(d)
            o = c.get("o")
            box = self.dict.get(o[1]) if o and o[1] else None
            if box is None:
                continue
            league = {f[1] for f in c.get("f", []) if self.dict.get(f[0]) == "league"}
            if not (box in boxes or (box in ("infobox gridiron football team", "infobox american football team") and nfl in league)
                    or (box == "infobox basketball club" and nba in league)):
                continue
            name = self.db.execute("SELECT name FROM suggest_items WHERE doc_id = ?", (d,)).fetchone()[0]
            dem = v0.r32(self.S(d, "demand", False)) or 0.0
            k = (self.title(d), name, box) if with_box else (self.title(d), name)
            best[k] = max(best.get(k, 0.0), dem)
        rows = [k + (v,) for k, v in best.items()]
        rows.sort(key=lambda r: -r[-1])
        return rows[:5]

    def typo(self, p):
        s, t = self.s, p["t"]
        A = trigrams(t)
        q = self.tv.Query.fuzzy_term_query(self.tv_schema, "key", t, distance=s.max_edits(t), transposition_cost_one=True)
        sims = []
        for _sc, addr in self.tv_searcher.search(q, 2000).hits:
            key = self.tv_searcher.doc(addr)["key"][0]
            if key == t:
                continue
            B = trigrams(key)
            sm = sim_counts(len(A & B), len(A), len(B))
            if sm >= s.TYPO_SIM:
                sims.append((sm, key))
        sims.sort(key=lambda x: (-x[0], x[1]))
        out = []
        for sm, key in sims:
            for (d,) in self.db.execute("SELECT doc_id FROM suggest_keys WHERE key = ?", (key,)):
                nm = self.db.execute("SELECT name FROM suggest_items WHERE doc_id = ?", (d,)).fetchone()
                if nm:
                    out.append((nm[0], sm, v0.r32(self.S(d, "score", False)), key))
            if len(out) >= 100:
                break
        return out[:100]

    def near_names(self, p):
        """(key, score) for each of p["keys"] that a whole query can match exactly: an article's title or alias, a
        site's label or host. Accounts are left out: 12 M handles sit one letter from almost anything typed."""
        best: dict = {}
        keys = sorted(set(p["keys"]))
        for i in range(0, len(keys), 900):
            chunk = keys[i:i + 900]
            for n, d in self.db.execute(f"SELECT name, doc_id FROM names WHERE name IN ({','.join('?' * len(chunk))}) "
                                        f"AND kind IN ({N_ALIAS}, {N_SITE_LABEL}, {N_HOST}, {N_HOST_NAV}, {N_SELF})", chunk):
                best[n] = max(best.get(n, 0.0), v0.r32(self.S(d, "score", False)) or 0.0)
        return sorted(best.items())

    def is_name_itself(self, key) -> bool:
        """The typed text is already a name of the classes near_more() corrects to: an entity's name with an account
        of any kind (its own site may sit under the link tier's floor) or a place's, anywhere. Such a query is not a
        slip: "skørringe parish" is a parish, one letter from Skørpinge Parish (site-nav dev)."""
        if not key:
            return False
        if self.db.execute(f"SELECT 1 FROM names WHERE name = ? AND kind = {N_ACCOUNT} "
                           f"AND flags & {F_KEY | F_WKEY} LIMIT 1", (key,)).fetchone():
            return True
        return any(self.db.execute(f"SELECT 1 FROM {a}.places WHERE name_key = ? LIMIT 1", (key,)).fetchone() for a, _role in self.pfiles)

    def near_more(self, p):
        """(key, score) for each of p["keys"] that names what near_names() leaves out and an exact tier answers:
        p["links"] — the official site of a thing with no article, exactly when link_accounts() would show it (a key
        or wiki-key match, platform site, at or over the link tier's sitelinks floor). The other accounts stay out:
        handles sit one letter from almost anything typed. p["places"] — a place of exactly that name within p["r"] km of
        (p["lat"], p["lon"]), the place tier's reach. A site scores 1 + its sitelinks, a place under 1 and nearer
        first: a thing known to Wikidata before a shop. Nothing when p["typed"], the typed name's key, is itself such a
        name (is_name_itself)."""
        if self.is_name_itself(p.get("typed")):
            return []
        best: dict = {}
        keys = sorted(set(p["keys"]))
        for i in range(0, len(keys), 900):
            chunk = keys[i:i + 900]
            if p.get("links"):
                for (n,) in self.db.execute(f"SELECT DISTINCT name FROM names WHERE name IN ({qm(chunk)}) AND kind = {N_ACCOUNT} "
                                            f"AND flags & {F_KEY | F_WKEY}", chunk).fetchall():
                    for row in self.link_accounts(n, "site", 1):          # the link tier's own answer, floor included
                        best[n] = max(best.get(n, 0.0), 1.0 + (row[3] or 0))
            if p.get("places"):
                for a, _role in self.pfiles:
                    key_of = dict(self.db.execute(f"SELECT pid, name_key FROM {a}.places WHERE name_key IN ({qm(chunk)})", chunk))
                    for row in self._place_rows(a, list(key_of), p["lat"], p["lon"], p["r"]):
                        n = key_of[row[13]]
                        best[n] = max(best.get(n, 0.0), 1.0 / (2.0 + row[8]))
        return sorted(best.items())

    # ------------------------------------------------------------------ stocks, channels
    def stock(self, p):
        cls = p["cls"]
        rows = []
        for d in self.names1(p["k"], N_STOCK):
            r = self.db.execute("SELECT exchange, cik, site, ir, tord FROM stocks WHERE doc_id = ?", (d,)).fetchone()
            if r:
                _t, ticker, _u, title = self.reg(d)
                rows.append((ticker, self.dict.get(r[0]), title, r[1], r[2], r[3], self.S(d, "sitelinks"), r[4]))
        rows.sort(key=lambda r: (not r[0].endswith("-" + cls), r[1] == "OTC", -(r[6] or 0), "-" in r[0], len(r[0]), r[7]))
        return [r[:6] for r in rows[:1]]

    def stock_bare(self, p):
        for d in self.names1(p["k"].lower(), N_TICKER)[:1]:
            r = self.db.execute("SELECT exchange, cik, site, ir, word FROM stocks WHERE doc_id = ?", (d,)).fetchone()
            if r:
                _t, ticker, _u, title = self.reg(d)
                return [(ticker, self.dict.get(r[0]), title, r[1], r[2], r[3], bool(r[4]))]
        return []

    def yt_rows(self, key, flags_ok):
        best: dict = {}
        for d, f in self.names(key, N_YOUTUBE):
            if f & flags_ok:
                best[d] = best.get(d, 0) | f
        out = []
        for d, f in best.items():
            r = self.db.execute("SELECT handle, description FROM youtube WHERE doc_id = ?", (d,)).fetchone()
            if r:
                out.append((self.reg(d)[1], r[0], self.title(d), r[1], self.S(d, "followers", False), self.S(d, "sitelinks"), f))
        return out

    def yt_exact(self, p):
        rows = self.yt_rows(p["key"], 15)
        short = lambda f: bool(f & F_SKEY) and not (f & (F_KEY | F_HKEY | F_WKEY))
        rows.sort(key=lambda r: (short(r[6]), r[4] is None, -(r[4] or 0), r[5] is not None, -(r[5] or 0)))
        return [r[:4] + (int(r[4]) if r[4] is not None else None, r[5]) for r in rows[:2]]

    def yt_bare(self, p):
        rows = [r for r in self.yt_rows(p["key"], F_KEY | F_HKEY | F_WKEY) if (r[4] or 0) >= p["min"]]
        rows.sort(key=lambda r: -(r[4] or 0))
        return [r[:4] + (int(r[4]), r[5]) for r in rows[:1]]

    def yt_index(self):
        """An in-memory Tantivy index of every channel, built once: BM25 over what Postgres' search_text
        holds (name, handle, the article's title, description) with the engine's own en_pg analyzer.
        148k channels in rc3: a few seconds, once per process (YT_SEARCH_SQL had no release port and
        returned 503 for every channel-topic query under SEARCH_POSTGRES=off, rc3 acceptance arm C)."""
        if self._yt_ix is None:
            with self._pid_lock:
                if self._yt_ix is None:
                    tv, s_ = self.tv, self.s
                    name = s_.TANTIVY_ANALYZER
                    sb = tv.SchemaBuilder()
                    sb.add_unsigned_field("doc_id", stored=False, indexed=True, fast=True)
                    sb.add_text_field("body", stored=False, tokenizer_name=name)
                    ix = tv.Index(sb.build())
                    an = s_.TantivyWikiText.build_analyzer(tv, name)
                    ix.register_tokenizer(name, an)
                    w = ix.writer(heap_size=100_000_000, num_threads=1)
                    for d, handle, desc, wiki in self.db.execute("SELECT doc_id, handle, description, wiki FROM youtube").fetchall():
                        doc = tv.Document()
                        doc.add_unsigned("doc_id", d)
                        doc.add_text("body", " ".join(x for x in (self.title(d), handle, self.title(wiki) if wiki else None, desc) if x))
                        w.add_document(doc)
                    w.commit()
                    w.wait_merging_threads()
                    ix.reload()
                    self._yt_ix = (ix, an, ix.searcher())
        return self._yt_ix

    def yt_search(self, p):
        """YT_SEARCH_SQL: (channel_id, handle, name, description, followers, sitelinks, bm25) for the
        channels about a topic, best 30 by BM25."""
        tv = self.tv
        ix, an, searcher = self.yt_index()
        terms = list(dict.fromkeys(an.analyze(p["q"])))
        if not terms:
            return []
        hits = searcher.search(tv.Query.boolean_query(
            [(tv.Occur.Should, tv.Query.term_query(ix.schema, "body", t)) for t in terms]), 30).hits
        ids = searcher.fast_field_values("doc_id", [a for _s, a in hits]) if hits else []
        out = []
        for (score, _a), d in zip(hits, ids):
            d = int(d)
            r = self.db.execute("SELECT handle, description FROM youtube WHERE doc_id = ?", (d,)).fetchone()
            f = self.S(d, "followers", False)
            out.append((self.reg(d)[1], r[0], self.title(d), r[1], int(f) if f is not None else None,
                        self.S(d, "sitelinks"), float(score)))
        return out

    # ------------------------------------------------------------------ fuzzy fallbacks (FTS5 trigram, prefix filter)
    def df(self, vocab, g):
        self.db
        k = (vocab, g)
        cache = self._df
        n = cache.get(k)
        if n is None:
            r = self.db.execute(f"SELECT doc FROM temp.{vocab} WHERE term = ?", (g,)).fetchone()
            n = r[0] if r else 0
            if len(cache) > DF_MAX:
                cache.clear()
            cache[k] = n
        return n

    def candidates(self, fts, vocab, A, need, ranges=((None, None),)):
        """Rowids holding >= need of A's trigrams hold one of A's |A| - ceil(need) + 1 rarest:
        read only those doclists (the caller checks similarity exactly)."""
        src, tname = fts, fts.split(".")[-1]
        m = len(A) - math.ceil(need) + 1
        rare = sorted((self.df(vocab, g), g) for g in A)[:max(m, 0)]
        ids = set()
        for _n, g in rare:
            for lo, hi in ranges:
                if lo is None:
                    ids.update(i for (i,) in self.db.execute(f"SELECT rowid FROM {src} WHERE {tname} MATCH ?", (phrase(g),)))
                else:
                    ids.update(i for (i,) in self.db.execute(f"SELECT rowid FROM {src} WHERE {tname} MATCH ? AND rowid BETWEEN ? AND ?",
                                                             (phrase(g), lo, hi)))
        return ids

    def fuzzy_candidates(self, fts, vocab, A, need):
        """candidates() for a whole index without the rows few_candidates() rules out, in rowid order (titles
        equally similar then come out the same way every time: a set's order decided between them before)."""
        m = len(A) - math.ceil(need) + 1
        rare = sorted((self.df(vocab, g), g) for g in A)
        with self.s.stage("fuzzy_read"):
            return sorted(few_candidates(rare, m, lambda g: [i for (i,) in self.db.execute(
                f"SELECT rowid FROM {fts} WHERE {fts} MATCH ?", (phrase(g),))]))

    def fuzzy_site(self, p):
        key, th = p["key"], self.s.FUZZY_SIM
        A = trigrams(key)
        out = []
        for d in (self.fuzzy_candidates("label_trgm", "lt_vocab", A, th * len(A) - 1e-6) if A else ()):
            _t, host, url, title = self.reg(d)
            sm = similarity(host.split(".", 1)[0], key)
            if sm >= th:
                out.append((url, title, self.site(d)[1], sm))
        out.sort(key=lambda r: -r[3])
        return out[:3]

    def fuzzy_wiki(self, p):
        q, th = p["q"], self.s.FUZZY_SIM
        A = trigrams(q)
        best = []
        ids = self.fuzzy_candidates("wiki_trgm", "wt_vocab", A, th * len(A) - 1e-6) if A else []
        for d, t in self._local.registry.titles(ids):
            B = trigrams(t or "")
            sm = sim_counts(len(A & B), len(A), len(B))            # similarity(t, q), q's trigrams made once
            if sm >= th:
                best.append((sm, d, t))
        best.sort(key=lambda r: -r[0])
        return [(t, self.card(d).get("l"), sm) for sm, d, t in best[:2]]

    # ------------------------------------------------------------------ places (places-osm.sqlite)
    def _place_rows(self, a, pids, hlat, hlon, r):
        """The places of those pids within r km, [0] still the doc_id: _place_ids names the ones a caller keeps."""
        out, pids = [], list(pids)
        kinds = self.pdict[a]
        for i in range(0, len(pids), 30000):
            chunk = pids[i:i + 30000]
            for (pid, doc, name, kid, lat, lon, street, city, hours, web, phone, bl) in self.db.execute(
                    f"SELECT pid, doc_id, name, kind, lat, lon, street, city, hours, website, phone, brand_lower FROM {a}.places "
                    f"WHERE pid IN ({qm(chunk)})", chunk):
                dd = d_km(lat, lon, hlat, hlon)
                if dd <= r:
                    out.append((doc, name, kinds[kid], street, city, hours, web, phone, dd, lat, lon, bl, a, pid))
        return out

    def _place_ids(self, rows):
        """_place_rows' rows with the place's id (osm) for the doc_id, each remembered for hours_info. Only for the
        rows a caller keeps: the registry was read once for every candidate, 6,368 times for "new york times" near
        New York, where three are shown (2026-10-11)."""
        out = []
        for r in rows:
            osm = self.reg(r[0])[1]
            if len(self.place_doc) > 200000:
                self.place_doc.clear()
            self.place_doc[osm] = (r[0], r[12], r[13])
            out.append((osm,) + r[1:12])
        return out

    def places_kinds(self, p):
        lat, lon, e = p["lat"], p["lon"], 1e-4
        hint = (p.get("hint") or "").strip("%").lower()
        out = []
        for a, _role in self.pfiles:
            ids = []
            for k in p["kinds"]:
                kid = self.pkind[a].get(k)
                if kid is not None:
                    ids += [i for (i,) in self.db.execute(
                        f"SELECT id FROM {a}.places_geo WHERE lat0 <= ? AND lat1 >= ? AND lon0 <= ? AND lon1 >= ? AND k0 <= ? AND k1 >= ?",
                        (lat + p["dlat"] + e, lat - p["dlat"] - e, lon + p["dlon"] + e, lon - p["dlon"] - e, kid, kid))]
            rows = self._place_rows(a, ids, lat, lon, p["r"])
            if hint:        # PLACE_MATCH_KINDS' second arm: a restaurant with "pizz" in its name is a pizza place
                hids = []
                for k in p.get("hint_kinds") or []:
                    kid = self.pkind[a].get(k)
                    if kid is not None:
                        hids += [i for (i,) in self.db.execute(
                            f"SELECT id FROM {a}.places_geo WHERE lat0 <= ? AND lat1 >= ? AND lon0 <= ? AND lon1 >= ? AND k0 <= ? AND k1 >= ?",
                            (lat + p["dlat"] + e, lat - p["dlat"] - e, lon + p["dlon"] + e, lon - p["dlon"] - e, kid, kid))]
                seen = {r[0] for r in rows}
                rows += [r for r in self._place_rows(a, hids, lat, lon, p["r"]) if hint in pg_lower(r[1] or "") and r[0] not in seen]
            out += rows
        return [r[:11] for r in self._place_ids(sorted(out, key=lambda x: x[8])[:3])]

    def places_name(self, p):
        name, lat, lon = p["name"], p["lat"], p["lon"]
        A = trigrams(name)
        la0, la1 = int((lat - p["dlat"] + 90) // CELL), int((lat + p["dlat"] + 90) // CELL)
        lo0, lo1 = int((lon - p["dlon"] + 180) // CELL), int((lon + p["dlon"] + 180) // CELL)
        ranges = [((la * NLON + lo0) << SEQ_BITS, ((la * NLON + lo1 + 1) << SEQ_BITS) - 1) for la in range(la0, la1 + 1)]
        out = []
        for a, _role in self.pfiles:
            ids = self.candidates(f"{a}.place_names", f"pn_vocab_{a}", A, 0.6 * len(A) - 1e-6, ranges) if A else set()
            ids |= {i for (i,) in self.db.execute(f"SELECT pid FROM {a}.places WHERE brand_lower = ? AND brand_lower <> ''", (name,))}
            out += [r for r in self._place_rows(a, ids, lat, lon, p["r"])
                    if pg_lower(r[1] or "") == name or (r[11] and r[11] == name) or similarity(r[1] or "", name) >= 0.6]
        out.sort(key=lambda x: x[8])
        return [r[:11] for r in self._place_ids(out[:3])]

    def place_exact(self, p):
        keys = p["keys"]
        rows = []
        for a, _role in self.pfiles:
            ids = [i for (i,) in self.db.execute(f"SELECT pid FROM {a}.places WHERE name_key IN ({qm(keys)}) UNION "
                                                 f"SELECT pid FROM {a}.places WHERE brand_key IN ({qm(keys)}) AND brand_key <> ''", keys + keys)]
            rows += self._place_rows(a, ids, p["lat"], p["lon"], p["r"])
        rows = self._place_ids(sorted(rows, key=lambda x: x[8])[:40])
        return [(r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[9], r[10], r[8]) for r in rows]

    def place_named(self, keys) -> bool:
        """facts.py's known-name rule: is one of these keys the exact name, or the brand, of a place in the
        places files? Any place anywhere; the facts card has no home point."""
        for a, _role in self.pfiles:
            if self.db.execute(f"SELECT 1 FROM {a}.places WHERE name_key IN ({qm(keys)}) UNION ALL "
                               f"SELECT 1 FROM {a}.places WHERE brand_key IN ({qm(keys)}) AND brand_key <> '' LIMIT 1", keys + keys).fetchone():
                return True
        return False

    def town_circles(self, towns) -> list:
        """[(lat, lon, r_km), …] of the towns of those keys: where the places whose address names the town lie
        (meta rows `town:<key>`, tools/sqlite_lookups/towns.py). [] in a release built before them."""
        keys = ["town:" + t for t in towns]
        out = []
        for a, _role in self.pfiles:
            for (v,) in self.db.execute(f"SELECT value FROM {a}.meta WHERE key IN ({qm(keys)})", keys):
                out += [tuple(float(x) for x in c.split(",")[:3]) for c in v.split(";") if c]
        return out

    def place_town(self, p):
        """PLACE_TOWN_SQL: the places of that name or brand whose address names one of the towns, wherever they are.
        When none does: those with NO city in their address that lie where the town is (town_circles) — within
        p["near_km"] of the searcher unless p["far"] (the query named a state), since a circle is weaker evidence
        than an address. A place whose address names another town is never taken."""
        keys, towns = p["keys"], set(p["towns"])
        rows, bare = [], {}
        for a, _role in self.pfiles:
            ids = []
            for i, city, lat, lon in self.db.execute(
                    f"SELECT pid, city, lat, lon FROM {a}.places WHERE name_key IN ({qm(keys)}) UNION "
                    f"SELECT pid, city, lat, lon FROM {a}.places WHERE brand_key IN ({qm(keys)}) AND brand_key <> ''", keys + keys):
                if not city:
                    bare.setdefault(a, []).append((i, lat, lon))
                elif v0.city_key(city) in towns:
                    ids.append(i)
            rows += self._place_rows(a, ids, p["lat"], p["lon"], math.inf)
        if not rows and bare and p.get("near_km"):
            circles = self.town_circles(p["towns"])
            if not p.get("far"):
                circles = [c for c in circles if d_km(c[0], c[1], p["lat"], p["lon"]) <= p["near_km"] + c[2]]
            for a, cand in bare.items() if circles else ():
                ids = [i for i, lat, lon in cand if any(d_km(lat, lon, c[0], c[1]) <= c[2] for c in circles)]
                rows += self._place_rows(a, ids, p["lat"], p["lon"], math.inf)
        rows = self._place_ids(sorted(rows, key=lambda x: x[8])[:40])
        return [(r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[9], r[10], r[8]) for r in rows]

    def hours_info(self, p):
        """HOURS_INFO_SQL: (osm, hours_checked, edited, web hours, crawled, url) per place id the engine just saw."""
        out = []
        for osm in p["ids"]:
            seen = self.place_doc.get(osm)
            if seen is None:
                continue
            # by pid, the table's key, in the file the place came from: by doc_id (no index) this read the table
            # from its start for every place listed — 530 ms for three in New York, 46 in Houston (2026-10-11)
            d, a, pid = seen
            chk, ed = self.db.execute(f"SELECT hours_checked, edited FROM {a}.places WHERE pid = ?", (pid,)).fetchone() or (None, None)
            w = self.db.execute("SELECT hours, crawled, url FROM h.hours_web WHERE doc_id = ?", (d,)).fetchone() if self.has_hw else None
            out.append((osm, chk, ed, (w[0] if w else ""), (w[1] if w else ""), (w[2] if w else "")))
        return out

    def chain_hours(self, p):
        """CHAIN_HOURS_SQL: the hours the businesses' own pages publish (hours-web.sqlite) for places named
        like the chain (name_key in p["keys"]), up to 400; none without that file."""
        if not self.has_hw:
            return []
        # kept across searches: the release never changes under a running engine, and this read cost 17-36 ms in
        # each places file for every chain in a nearby list (2026-10-11)
        key = tuple(p["keys"])
        out = self._chain_hours.get(key)
        if out is None:
            out = []
            for a, _role in self.pfiles:
                out += self.db.execute(f"SELECT w.hours FROM {a}.places p JOIN h.hours_web w ON w.doc_id = p.doc_id "
                                       f"WHERE p.name_key IN ({qm(p['keys'])}) AND w.hours <> '' LIMIT 400", p["keys"]).fetchall()
            if len(self._chain_hours) >= CHAIN_HOURS_KEEP:
                self._chain_hours.clear()
            out = self._chain_hours[key] = out[:400]
        return list(out)

    # ------------------------------------------------------------------ the card, in one read
    def card_parts(self, title: str, related_n: int):
        """(description, related (title, lead) rows, picture row, person) for the card over title."""
        ds = self.names1(title, N_TITLE)[:1]
        if not ds:
            return None, [], None, False
        d = ds[0]
        c = self.card(d)
        rel = [(self.title(r), self.card(r).get("l")) for r in c.get("r", [])[:related_n]]
        pic = (self.db.execute("SELECT img, w, h, author, licence, licence_url, file FROM pic.pictures "
                               "WHERE doc_id = ? AND img IS NOT NULL", (d,)).fetchone() if self.has_pics else None)
        return c.get("d"), rel, pic, self.has_fact(d, "born")
