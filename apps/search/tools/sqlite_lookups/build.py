"""The lookup builder (release format v1, docs/internal/SEARCH-RELEASE-FORMAT.md) — T4.

  python build.py OUT_DIR [--registry REG.sqlite] [--release-id ID] [--stage STAGE.sqlite | --own-rank own_rank.tsv] [--previous PREV_LOOKUP_DIR]

The single builder the release export (T5) calls for the lookup part. From the serving
Postgres (read-only, every SELECT through guard_sql) it writes:
  OUT_DIR/lookup.sqlite       exact lookups + the display data the registry does not hold, keyed
                              by doc_id: names (every exact-match tier in one table), cards (one
                              record per article), accounts, entities, sites, links, youtube,
                              stocks, suggestions, FTS5 trigram indexes for the fuzzy fallbacks
  OUT_DIR/places-osm.sqlite   OpenStreetMap places only (ODbL, own file; no WDC rows, G3 D3)
  OUT_DIR/text/typo/          Tantivy fuzzy index of the suggestion keys (typo correction)
and, until T5's registry.sqlite / signals.bin exist, STAND-INS in the same scheme:
  OUT_DIR/registry.sqlite     doc(doc_id, type, key, url, title) — one row per thing
  OUT_DIR/signals.bin(.json)  one fixed-width record per doc_id: every popularity number
With --registry, doc_ids come from T5's registry; a (type, key) it lacks aborts the build.

doc_id scheme (proposal, mirrored here until registry.sqlite lands):
  type 1 wiki    key = page_id             type 5 stock   key = ticker
       2 site    key = host                 6 place   key = osm ("node/123")
       3 account key = platform TAB handle TAB qid   7 link  key = url
       4 youtube key = channel_id
  dense 1..N in (type, key) order — wiki keys numerically, so articles keep page_id order — and
  signals.bin is a plain array by doc_id.

Release guards (refuse, never warn; mirror release/export.py check_export_sql): no refused
table (subreddits, build-only tables, the Jev-derived kinds tier and d2q, ADR-065), no
history_visits / hist, the required filters (no subreddit suggestions, no WDC places, UT1 rows
only of site_quality). Output guard: no popularity / history / Jev column, no kinds table,
secure_delete=ON while building, VACUUM at the end of the build, freelist_count = 0,
application_id + user_version, meta(release_id).
Popularity never enters lookup.sqlite: ranks, sitelinks, followers, demand, suggestion weights
go to signals.bin; clickstream counts and phrase counts only fix an order or a cut at build time.
"""
import argparse, json, os, re, shutil, sqlite3, struct, sys, time
import towns
import trgm
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "release"))
import pages_store  # noqa: E402  (SEARCH_PAGES_STORE: the inner pages as files, no Postgres)

FORMAT_VERSION = 1
APP_ID = 0x6C6F7331                      # "los1": T5's application_id for every SQLite file of a release
LEAD_CHARS = int(os.environ.get("LEAD_CHARS", 1000))     # cards never show more
PHRASE_MIN = float(os.environ.get("SEARCH_PHRASE_MIN", "200"))   # search_api PHRASE_MIN_SEARCHES
RELATED_N = 4                                                    # search_api RELATED_N
CELL, NLON, SEQ_BITS = 0.5, 720, 20
# T5's numbering (release_v1.py: doc_id = type << 28 | seq (meta.type_shift); 3 = page, unused here)
WIKI, SITE, PLACE, ACCOUNT, YOUTUBE, STOCK, LINK = 1, 2, 4, 5, 6, 7, 8
# names.kind: every exact-match tier in one table
N_TITLE, N_ALIAS, N_SITE_LABEL, N_HOST, N_DOMAIN, N_HOST_NAV, N_ACCOUNT, N_YOUTUBE, N_STOCK, N_OFFICIAL, N_SELF, N_TICKER = range(1, 13)
F_KEY, F_HKEY, F_WKEY, F_SKEY = 1, 2, 4, 8          # ACCOUNT / YOUTUBE flags: which key matched
SELF_NAMED_PREFIX = ("play", "visit", "go", "get", "my", "the", "shop", "try", "join", "ask", "discover",
                     "explore", "experience", "official", "we", "i", "e", "hello", "meet", "see", "enjoy")
SELF_NAMED_SUFFIX = ("online", "hq", "app", "official", "usa", "us", "inc", "co", "group", "live", "tv",
                     "nj", "ny", "home", "site", "web", "world", "now")

FORBIDDEN_TABLES = {"site_meta", "site_inlinks", "kw_country", "wiki_pop", "subreddits",
                    "kind_docs", "kind_members", "kind_keys", "kind_vecs", "kind_site_vec", "site_kinds",
                    "kinds_phrase_keep", "kinds_phrase_keys", "kinds_member_jev", "d2q_docs"}
REQUIRED_FILTERS = {"suggest_items": "kind <> 'subreddit'", "suggest_keys": "kind <> 'subreddit'",
                    "places": "osm NOT LIKE 'wdc:%'", "site_quality": "source IN ('ut1', 'jev+ut1')"}
FORBIDDEN_COLUMNS = {"*": {"history_visits", "hist"},
                     "site_quality": {"useful", "parked", "spam", "unsafe", "junk", "source"}}
OUT_FORBIDDEN_COLS = {"unsafe", "history_visits", "hist", "tranco_rank", "own_rank", "cc_hc_pos", "sitelinks", "followers", "subscribers",
                      "demand", "pop", "score", "clicks", "counts", "rank", "jev", "votes", "junk", "source", "useful"}


def guard_sql(q: str) -> str:
    """release/export.py check_export_sql(), per statement."""
    low = re.sub(r"'[^']*'", "''", q.lower())
    bad = FORBIDDEN_TABLES & set(re.findall(r"[a-z0-9_]+", low))
    if bad:
        sys.exit(f"G3/ADR-065: lookup SQL names refused table(s) {sorted(bad)} — refusing")
    tables = set(re.findall(r"\b(?:from|join)\s+([a-z0-9_]+)", low))
    for t in tables & REQUIRED_FILTERS.keys():
        if REQUIRED_FILTERS[t] not in q:
            sys.exit(f"G3: lookup export of {t} lacks the filter {REQUIRED_FILTERS[t]!r} — refusing")
    sel = low.split(" from ")[0]
    cols = set(FORBIDDEN_COLUMNS["*"]).union(*(FORBIDDEN_COLUMNS.get(t, set()) for t in tables))
    for col in cols:
        if re.search(rf"(?<![.\w]){col}\b", sel):
            sys.exit(f"ADR-065/privacy: lookup SQL selects column {col!r} — refusing")
    return q


def guard_output(path: str) -> dict:
    c = sqlite3.connect(path)
    tabs = [r[0] for r in c.execute("SELECT name FROM sqlite_schema WHERE type = 'table'")]
    bad_t = sorted(t for t in tabs if t in FORBIDDEN_TABLES or t.startswith("kind") or t == "site_quality")
    bad_c = sorted(f"{t}.{col[1]}" for t in tabs for col in c.execute(f"PRAGMA table_info('{t}')")
                   if col[1] in OUT_FORBIDDEN_COLS)
    free = c.execute("PRAGMA freelist_count").fetchone()[0]
    appid = c.execute("PRAGMA application_id").fetchone()[0]
    uv = c.execute("PRAGMA user_version").fetchone()[0]
    sd = c.execute("SELECT value FROM meta WHERE key = 'built_release'").fetchone()
    c.close()
    if bad_t or bad_c or free or appid != APP_ID or uv != FORMAT_VERSION or not sd:
        sys.exit(f"guard: {path}: tables {bad_t} columns {bad_c} freelist {free} application_id {appid:#x} "
                 f"user_version {uv} meta {sd} — refusing")
    return {"tables": len(tabs), "freelist": free}


PREV = None        # --previous: the previous release's lookup folder (stable ids); None = today's dense numbering
SEQ_MASK = (1 << SEQ_BITS) - 1


def carry_order(keys, prev, what):
    """Stable order-preserving ranks. `keys` = the distinct keys in their NEW order (Postgres en_US);
    `prev` = key -> rank carried from the previous release. A key present in `prev` keeps its rank (as long
    as the ranks still ascend with the new order); the keys between two anchors get fresh ranks strictly
    between them: the next integers when there is room, else evenly spaced fractions. No anchors at all
    gives 1..n, exactly dense_rank(). Returns key -> rank (int when integral)."""
    n = len(keys)
    known = [False] * n
    lo = None
    for i, k in enumerate(keys):
        t = prev.get(k)
        if t is not None and (lo is None or t > lo):
            known[i] = True
            lo = t
    out = {}
    i = 0
    last = None
    while i < n:
        if known[i]:
            last = out[keys[i]] = prev[keys[i]]
            i += 1
            continue
        j = i
        while j < n and not known[j]:
            j += 1
        m = j - i
        hi = prev[keys[j]] if j < n else None
        base = last if last is not None else 0
        if hi is None or hi - base > m:
            ts = [base + k for k in range(1, m + 1)]
        else:
            gap = hi - base
            ts = [base + gap * k / (m + 1) for k in range(1, m + 1)]
            prevt = base
            for t in ts:
                if not (prevt < t < hi):
                    sys.exit(f"--previous: no room left between {what} ranks {base} and {hi} for {m} new keys "
                             f"(float precision) — build this release without --previous to renumber")
                prevt = t
        for k, t in zip(keys[i:j], ts):
            out[k] = t
        last = ts[-1]
        i = j
    return out


def stable_rank_table(st, name, order_sql, prev_pairs, what):
    """TEMP table `name`(rank INTEGER PRIMARY KEY, t): new rank (the staged dense_rank) -> carried stable rank."""
    grp = {}                                   # keys that share a staged rank share a stable rank
    for r, k in st.execute(order_sql):
        grp.setdefault(r, []).append(k)
    ranks = sorted(grp)
    carried = {r: next((prev_pairs[k] for k in grp[r] if k in prev_pairs), None) for r in ranks}
    got = carry_order(ranks, {r: t for r, t in carried.items() if t is not None}, what)
    st.execute(f"DROP TABLE IF EXISTS temp.{name}")
    st.execute(f"CREATE TEMP TABLE {name} (rank INTEGER PRIMARY KEY, t)")
    st.executemany(f"INSERT INTO {name} VALUES (?, ?)", ((r, got[r]) for r in ranks))
    kept = sum(1 for r in ranks if carried[r] == got[r])
    log(f"stable {what} ranks: {len(ranks)} keys, {kept} carried, {len(ranks) - kept} new")


def prev_card_t(path):
    """doc_id -> cards.t of the previous release. 't' is the first key of the card JSON, so only the first
    bytes of each zlib blob are inflated."""
    import zlib
    c = sqlite3.connect(path)
    out = {}
    rx = re.compile(rb'^\{"t":(-?[0-9][0-9.eE+-]*)')
    for did, blob in c.execute("SELECT doc_id, c FROM cards"):
        m = rx.match(zlib.decompressobj().decompress(blob, 64))
        if m:
            v = m.group(1).decode()
            out[did] = float(v) if any(ch in v for ch in ".eE") else int(v)
    c.close()
    return out


T0 = time.time()


# account identity: platform TAB handle TAB qid (standalone; T5 may use platform TAB handle only)
AKEY = "l.platform || char(9) || l.value || char(9) || coalesce(l.qid, '')"
AKEY0 = "platform || char(9) || value || char(9) || coalesce(qid, '')"


def log(*a):
    print(f"[{time.time() - T0:7.1f}s]", *a, flush=True)


def f32(x):
    return None if x is None else struct.unpack("f", struct.pack("f", x))[0]


def norm_key(s: str) -> str:
    return "".join(re.findall(r"[a-z0-9]+", (s or "").lower()))


def js(v) -> str:
    return json.dumps(v, separators=(",", ":"), ensure_ascii=False)


def open_out(path: str, release_id: str, kind: str, extra: dict = None) -> sqlite3.Connection:
    if os.path.exists(path):
        os.remove(path)
    db = sqlite3.connect(path)
    # secure_delete: no deleted row survives in a free page; VACUUM at the end of the BUILD
    # the stamp is T5's (release/export.py stamp): application_id "los1", user_version = format,
    # meta(key, value) with the role and the release that BUILT the file; no timestamps
    db.executescript(f"PRAGMA page_size = 4096; PRAGMA journal_mode = OFF; PRAGMA synchronous = OFF; "
                     f"PRAGMA cache_size = -300000; PRAGMA temp_store = FILE; PRAGMA secure_delete = ON; "
                     f"PRAGMA application_id = {APP_ID}; PRAGMA user_version = {FORMAT_VERSION};"
                     f"CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT) WITHOUT ROWID;")
    rows = {"role": kind, "format": str(FORMAT_VERSION), "built_release": release_id}
    rows.update({k: str(v) for k, v in (extra or {}).items()})
    db.executemany("INSERT INTO meta VALUES (?, ?)", sorted(rows.items()))
    db.commit()
    return db


def finish(db: sqlite3.Connection, path: str) -> None:
    db.commit()
    db.execute("ANALYZE")
    db.commit()
    db.execute("VACUUM")
    db.close()
    g = guard_output(path)
    c = sqlite3.connect(path)
    sizes = c.execute("SELECT name, sum(pgsize) FROM dbstat GROUP BY name ORDER BY 2 DESC").fetchall()
    c.close()
    log(f"{os.path.basename(path)}: {os.path.getsize(path) / 1e6:.0f} MB, guard ok {g}")
    for n, sz in sizes:
        print(f"  size {os.path.basename(path)} {n}\t{sz / 1e6:.1f} MB", flush=True)


# ------------------------------------------------------------------ stage: Postgres -> SQLite
STAGE = {
    "s_wiki": ("page_id INTEGER PRIMARY KEY, title TEXT, tord INTEGER, lead TEXT",
               f"SELECT page_id, title, dense_rank() OVER (ORDER BY title), left(lead, {LEAD_CHARS}) FROM wiki_docs ORDER BY page_id"),
    "s_alias": ("norm TEXT, page_id INTEGER", "SELECT norm, page_id FROM wiki_aliases WHERE norm IS NOT NULL"),
    "s_wsites": ("page_id INTEGER PRIMARY KEY, url TEXT, domain TEXT, box TEXT, own_rank INTEGER",
                 "SELECT page_id, url, domain, box, NULL FROM wiki_sites"),         # own_rank: stage_own() fills it from the file
    "s_facts": ("title TEXT, fact TEXT, value TEXT, unit TEXT, prec INTEGER, at TEXT",
                "SELECT title, fact, value, unit, precision, at FROM wiki_facts ORDER BY title COLLATE \"C\", fact, value, at"),
    "s_next": ("title TEXT, next TEXT, o INTEGER",
               "SELECT n.title, n.next_title, row_number() OVER (PARTITION BY n.title ORDER BY n.clicks DESC NULLS FIRST, n.next_title) "
               "FROM wiki_next n JOIN wiki_docs w ON w.title = n.next_title"),
    "s_links": ("platform TEXT, qid TEXT, value TEXT, url TEXT, name TEXT, description TEXT, wiki_title TEXT, sitelinks INTEGER, "
                "key TEXT, hkey TEXT, wkey TEXT, skey TEXT, vord INTEGER",
                "SELECT platform, qid, value, url, name, description, wiki_title, sitelinks, key, hkey, wkey, skey, "
                "dense_rank() OVER (ORDER BY value) FROM wiki_links ORDER BY qid COLLATE \"C\", platform, value, url"),
    "s_docs": ("domain TEXT PRIMARY KEY, url TEXT, title TEXT, description TEXT",
               "SELECT DISTINCT ON (domain) domain, url, title, description FROM docs WHERE error IS NULL "
               "ORDER BY domain, fetched_at DESC, crawl"),
    # the same capture's source and what it said to search engines (stage_capture_policy, G3 D4c)
    "s_docs_src": ("domain TEXT PRIMARY KEY, crawl TEXT, robots TEXT",
                   "SELECT DISTINCT ON (domain) domain, crawl, robots FROM docs WHERE error IS NULL "
                   "ORDER BY domain, fetched_at DESC, crawl"),
    # G3 D2: the site rank is our own. own_rank.tsv (importer/own_rank.py) is staged by stage_own(): s_own, the navigable
    # hosts s_nav (from the file, never from Postgres's Tranco-derived nav_sites) and every rank column below
    "s_serve": ("domain TEXT PRIMARY KEY, own_rank INTEGER, cc_hc INTEGER", "SELECT domain, NULL, cc_hc_pos FROM serve_sites"),
    "s_own": ("host TEXT PRIMARY KEY, domain TEXT, rank INTEGER, cc_pos INTEGER", None),
    "s_nav": ("host TEXT, key TEXT, rank INTEGER, domain TEXT", None),
    "s_hostnav": ("key TEXT, url TEXT, title TEXT, description TEXT, rank INTEGER",
                  "SELECT key, url, title, description, NULL FROM host_nav"),
    "s_yt": ("channel_id TEXT PRIMARY KEY, handle TEXT, name TEXT, description TEXT, wiki_title TEXT, followers INTEGER, "
             "sitelinks INTEGER, key TEXT, hkey TEXT, wkey TEXT, skey TEXT",
             "SELECT channel_id, handle, name, description, wiki_title, followers, sitelinks, key, hkey, wkey, skey FROM youtube_channels"),
    "s_stock": ("ticker TEXT PRIMARY KEY, exchange TEXT, name TEXT, cik INTEGER, qid TEXT, label TEXT, site TEXT, sitelinks INTEGER, "
                "ir TEXT, word INTEGER, tord INTEGER",
                "SELECT ticker, exchange, name, cik, qid, label, site, sitelinks, to_jsonb(t) ->> 'ir', "
                "coalesce((to_jsonb(t) ->> 'word')::boolean, false)::int, dense_rank() OVER (ORDER BY ticker) FROM stock_tickers t"),
    "s_stock_keys": ("key TEXT, ticker TEXT", "SELECT key, ticker FROM stock_keys"),
    "s_items": ("id INTEGER PRIMARY KEY, kind TEXT, name TEXT, url TEXT, domain TEXT, wiki TEXT, pop REAL, score REAL, demand REAL, descr TEXT",
                "SELECT id, kind, name, url, domain, wiki, pop, score, demand, descr FROM suggest_items WHERE kind <> 'subreddit'"),
    "s_keys": ("key TEXT, id INTEGER, pos INTEGER", "SELECT key, id, pos FROM suggest_keys WHERE kind <> 'subreddit'"),
    "s_top": ("prefix TEXT PRIMARY KEY, ids TEXT, pos TEXT", "SELECT prefix, ids, pos FROM suggest_top"),
    "s_phrases": ("prefix TEXT PRIMARY KEY, phrases TEXT, counts TEXT", "SELECT prefix, phrases, counts FROM suggest_phrases"),
    "s_places": ("osm TEXT PRIMARY KEY, name TEXT, kind TEXT, brand TEXT, lat REAL, lon REAL, street TEXT, city TEXT, hours TEXT, "
                 "website TEXT, phone TEXT, name_key TEXT, brand_key TEXT, brand_lower TEXT",
                 "SELECT osm, name, kind, brand, lat, lon, street, city, hours, website, phone, "
                 "regexp_replace(lower(name), '[^a-z0-9]', '', 'g'), "
                 "CASE WHEN brand <> '' THEN regexp_replace(lower(brand), '[^a-z0-9]', '', 'g') END, "
                 "CASE WHEN brand <> '' THEN lower(brand) END FROM places WHERE osm NOT LIKE 'wdc:%'"),
    "s_unsafe": ("domain TEXT PRIMARY KEY", "SELECT DISTINCT domain FROM site_quality WHERE source IN ('ut1', 'jev+ut1')"),
    # rc5 W2: what the suggestion tables above were built from (release/suggest_own.py writes it; absent when the build
    # database's own Tranco- and history-selected tables were staged)
    "s_sugmeta": ("key TEXT PRIMARY KEY, value TEXT", "SELECT key, value FROM suggest_meta"),
    # rc5 W5: inner pages, for the registry (url, title) only. Nothing else of a page is staged: its text reaches the
    # release as sorted words through release_v1.py web-input (ADR-059), its description not at all (G3 D4c).
    "s_pages": ("url TEXT PRIMARY KEY, domain TEXT, title TEXT",
                "SELECT DISTINCT ON (url) url, domain, title FROM page_docs WHERE error IS NULL ORDER BY url, fetched_at DESC NULLS LAST, crawl"),
    # the sites whose pages were picked as answer pages (importer/ccpages.py, cc_pages kind 'answer'); the domain only
    # and the sites whose pages came from an open dump (importer/openpages.py, page_docs crawl 'open:…')
    "s_answer": ("domain TEXT PRIMARY KEY", "SELECT domain FROM cc_pages WHERE kind = 'answer' "
                                            "UNION SELECT domain FROM page_docs WHERE crawl LIKE 'open:%'"),
    # Stack Exchange attribution (2026-10-22): who asked each question (role 1) and who wrote the answer its page's words
    # come from (role 2), keyed by the page's url (importer/stackexchange.py se-authors). The registry keeps the rows of
    # the pages it has (release_v1.py load_authors); a display name is CC BY-SA content, as the question's title is
    "s_se_authors": ("url TEXT, role INTEGER, user_id INTEGER, name TEXT",
                     "SELECT 'https://' || site || '/questions/' || qid, role, user_id, display_name FROM se_authors"),
}
PAGE_SITE_CUT = 25000      # pages ship only for sites inside our rank's popular head (search_api CONFIDENT_RANK_OWN): the page
#                            set was crawled for Tranco's top 10,000 domains, and Tranco must not choose what ships
ANSWER_SITE_CUT = 100000   #: an answer site's pages ship down to here. Its kind is evidence the rank alone lacks past the head:
#                            of the 715 ranked answer sites in rc6's kinds (2026-10-08) 151 are inside 25,000 and 293 inside
#                            100,000 (rtings.com is 87,362). Still our rank, and an unranked site ships no pages
# A United States government site: .gov, or a state's state.xx.us (importer/ccpages.py gov_domain, the same rule in SQL;
# a test pins the two). Its pages ship without a rank cut.
GOV_DOMAIN_SQL = "(domain LIKE '%.gov' OR domain GLOB 'state.[a-z][a-z].us')"
# With a pages store (SEARCH_PAGES_STORE) the page_docs half of these comes from its files; the rest, if any, is still
# read from Postgres: s_pages is all page_docs, s_answer is cc_pages' answer sites plus page_docs' open-dump sites.
STORE_SQL = {"s_pages": None, "s_answer": "SELECT domain FROM cc_pages WHERE kind = 'answer'"}
OPTIONAL_SOURCE = {"s_unsafe": "site_quality", "s_sugmeta": "suggest_meta", "s_pages": "page_docs", "s_answer": "cc_pages",
                   "s_se_authors": "se_authors"}       # staged empty when the database has no such table
REAL_COLS = {"s_items": (6, 7, 8)}       # Postgres float4: stored as the float32 it is


def stage_own(st, own_file: str) -> None:
    """own_rank.tsv -> s_own, the navigable hosts s_nav and every rank column of the stage (G3 D2). Nothing here reads
    Tranco or Postgres. A host_nav row (a subdomain service) is kept only if its host is in the file."""
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "importer"))
    import own_rank
    rows = []
    with open(own_file, encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            p = line.rstrip("\n").split("\t")
            if len(p) != 5 or not p[2].isdigit() or int(p[2]) < 1:
                sys.exit(f"{own_file}: line {n} is not 'host domain own_rank cc_pos lists' — refusing")
            rows.append((p[0], p[1], int(p[2]), None if p[3] in ("", "\\N") else int(p[3])))
    if not rows:
        sys.exit(f"{own_file}: no rows — refusing")
    st.executemany("INSERT INTO s_own VALUES (?,?,?,?)", rows)
    st.executemany("INSERT INTO s_nav VALUES (?,?,?,?)", ((h, k, r, d) for h, d, r, _c in rows for k in own_rank.nav_keys(h, d) if k))
    st.create_function("url_host", 1, lambda u: (u or "").split("://", 1)[-1].split("/", 1)[0].lower(), deterministic=True)
    st.create_function("official_key", 2, own_rank.official_key, deterministic=True)
    st.executescript("""
        UPDATE s_serve SET own_rank = (SELECT rank FROM s_own o WHERE o.host = s_serve.domain);
        UPDATE s_wsites SET own_rank = (SELECT rank FROM s_own o WHERE o.host = official_key(s_wsites.url, s_wsites.domain));
        DELETE FROM s_hostnav WHERE url_host(url) NOT IN (SELECT host FROM s_own);
        UPDATE s_hostnav SET rank = (SELECT rank FROM s_own o WHERE o.host = url_host(s_hostnav.url));""")
    st.commit()
    log(f"stage s_own: {len(rows)} hosts; s_nav {st.execute('SELECT count(*) FROM s_nav').fetchone()[0]}; "
        f"s_hostnav kept {st.execute('SELECT count(*) FROM s_hostnav').fetchone()[0]}")


ADULT_ENDINGS = (".xxx", ".porn", ".adult", ".sex")       # our own rule (rc5 W3): the ending says what the site is
SITE_HOSTS_SQL = """SELECT domain FROM s_docs UNION SELECT domain FROM s_serve UNION SELECT host FROM s_nav
    UNION SELECT domain FROM s_wsites WHERE domain IS NOT NULL UNION SELECT domain FROM s_items WHERE kind = 'site' AND domain IS NOT NULL"""


def blocked_host(host: str, blocked) -> bool:
    """A block list entry covers the name and everything under it (how the lists are written): ads.example.com is
    blocked by `example.com` and by `ads.example.com`, example.com is not blocked by `ads.example.com`."""
    p = (host or "").lower().split(".")
    return any(".".join(p[i:]) in blocked for i in range(len(p) - 1))


def stage_blocked(st, blocked_file: str) -> dict:
    """rc5 W3: the block lists applied to the stage itself (blocked.tsv, own_rank.py blocklist: domain TAB list). Until
    now the builders trusted the database to have dropped them, and an entry naming a host under a site removed
    nothing. Used for REMOVAL only: no list name, category or flag derived from these lists is written anywhere."""
    blocked = set()
    with open(blocked_file, encoding="utf-8") as f:
        for line in f:
            d = line.split("\t", 1)[0].strip().lower()
            if d:
                blocked.add(d)
    if not blocked:
        sys.exit(f"{blocked_file}: no rows — refusing")
    url_host = lambda u: (u or "").split("://", 1)[-1].split("/", 1)[0].lower()      # noqa: E731
    gone = {}
    for t, col, host_of in (("s_docs", "domain", str), ("s_serve", "domain", str), ("s_wsites", "domain", str),
                            ("s_hostnav", "url", url_host), ("s_own", "host", str), ("s_nav", "host", str), ("s_pages", "url", url_host)):
        hit = [(v,) for (v,) in st.execute(f"SELECT DISTINCT {col} FROM {t} WHERE {col} IS NOT NULL") if blocked_host(host_of(v), blocked)]
        st.executemany(f"DELETE FROM {t} WHERE {col} = ?", hit)
        gone[t] = len(hit)
    ids = [(i,) for i, d, u in st.execute("SELECT id, domain, url FROM s_items WHERE kind IN ('site', 'service')")
           if blocked_host(d or url_host(u), blocked)]
    st.executemany("DELETE FROM s_items WHERE id = ?", ids)
    st.executemany("DELETE FROM s_keys WHERE id = ?", ids)
    gone["s_items"] = len(ids)
    st.commit()
    log(f"block lists ({len(blocked)} names): removed {gone}")
    return gone


_URL_PARTS = re.compile(r"(https?://[^/?#]+)([^?#]*)(.*)", re.S)
# a capture one level down that IS the site's landing page: a language root (/en, /en-US, /fr_CA) or an index file
_LANDING = re.compile(r"/(?:[a-z]{2}(?:[-_][A-Za-z]{2,4})?|index\.[a-z]{3,5}|home|default\.[a-z]{3,4})/?$", re.I)


def front_page(url: str):
    """(the site's front-page address, whether the capture's title and description describe the SITE). The crawl takes a
    page one level down when a site has no front page (gog.com/en, realmadrid.com/en-US, weather.gov/aprfc): a site
    result must name the site, and a deep page's title must not stand for it (rc5 W1)."""
    m = _URL_PARTS.match(url or "")
    if not m:
        return url, True
    origin, path, rest = m.groups()
    if path in ("", "/") and not rest:
        return origin + "/", True
    return origin + "/", not rest and bool(_LANDING.fullmatch(path))


def stage_front_pages(st) -> dict:
    """Every staged homepage row gets its site's front-page address; a capture that was not a landing page loses its
    title and description (they describe the deep page; the reader then shows the host)."""
    fix = []
    for domain, url, in st.execute("SELECT domain, url FROM s_docs WHERE url IS NOT NULL"):
        front, landing = front_page(url)
        if front != url or not landing:
            fix.append((front, landing, domain))
    st.executemany("UPDATE s_docs SET url = ?1, title = CASE WHEN ?2 THEN title END, description = CASE WHEN ?2 THEN description END "
                   "WHERE domain = ?3", fix)
    st.commit()
    n = {"moved to the front page": len(fix), "of which lost title and description": sum(1 for _f, l, _d in fix if not l)}
    log(f"homepage addresses: {n}")
    return n


def stage_pages(st) -> int:
    """rc5 W5: keep an inner page only when its site is inside our rank's top PAGE_SITE_CUT, or is an answer site
    inside ANSWER_SITE_CUT, or is a United States government site (GOV_DOMAIN_SQL, the importer's
    ccpages.gov_domain rule — a state's whole government sits under one domain that no rank places, and the pages were
    chosen by agency with their own budget). Returns the pages kept."""
    n0 = st.execute("SELECT count(*) FROM s_pages").fetchone()[0]
    st.execute(f"DELETE FROM s_pages WHERE domain IS NULL OR (NOT ({GOV_DOMAIN_SQL}) AND domain NOT IN (SELECT host FROM s_own WHERE rank <= ?1 "
               "UNION SELECT o.host FROM s_own o JOIN s_answer a ON a.domain = o.host WHERE o.rank <= ?2))", (PAGE_SITE_CUT, ANSWER_SITE_CUT))
    st.commit()
    n, sites = st.execute("SELECT count(*), count(DISTINCT domain) FROM s_pages").fetchone()
    ng, sg = st.execute(f"SELECT count(*), count(DISTINCT domain) FROM s_pages WHERE {GOV_DOMAIN_SQL}").fetchone()
    log(f"pages: {n} of {n0} kept, on {sites} sites inside our rank's top {PAGE_SITE_CUT}; {ng} of them government pages on {sg} sites (no rank cut)")
    return n


def stage_unsafe(st, ut1_file: str) -> dict:
    """rc5 W3: the unsafe flag straight from UT1's file (importer/directories.py ut1 -> ut1-unsafe.tsv.gz: domain, url,
    category; CC BY-SA 4.0, attributed in the release's LICENCES and NOTICE), for EVERY site the release can return.
    Before, a site was flagged only if the build database's Tranco-derived site list knew it (importer/junk.py):
    4,055 of 1.9M sites in rc4. Same categories as importer/junk.py; a host is matched by its own name."""
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "importer"))
    import gzip
    import junk
    hosts = {h for (h,) in st.execute(SITE_HOSTS_SQL) if h}
    hit, rows = {}, 0
    with gzip.open(ut1_file, "rt", encoding="utf-8", errors="replace") as f:
        for line in f:
            p = line.rstrip("\n").split("\t")
            if len(p) >= 3 and p[2] in junk.UT1_JUNK and p[0]:
                rows += 1
                d = p[0].lower().removeprefix("www.")
                if d in hosts:
                    hit.setdefault(d, p[2])
    if not rows:
        sys.exit(f"{ut1_file}: no unsafe rows — refusing (an empty file would ship a release with no flag at all)")
    st.execute("DELETE FROM s_unsafe")
    st.executemany("INSERT INTO s_unsafe VALUES (?)", ((d,) for d in sorted(hit)))
    st.execute("INSERT OR REPLACE INTO s_sugmeta VALUES ('unsafe_source', ?)", (f"ut1 file sha256 {sha256_file(ut1_file)}",))
    st.commit()
    by = {}
    for c in hit.values():
        by[c] = by.get(c, 0) + 1
    log(f"unsafe: {len(hit)} of {len(hosts)} sites are on UT1's lists ({rows} rows read): {dict(sorted(by.items()))}")
    return by


def stage(pg, path: str, own_file: str, ut1_unsafe: str = None, blocked: str = None) -> sqlite3.Connection:
    have = {r[0] for r in pg.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")}
    for _t, (_c, sql) in STAGE.items():
        if sql:
            guard_sql(sql)
    if os.path.exists(path):
        os.remove(path)
    st = sqlite3.connect(path)
    st.executescript("PRAGMA journal_mode = OFF; PRAGMA synchronous = OFF; PRAGMA cache_size = -300000; PRAGMA temp_store = FILE;")
    store = pages_store.store_dir()
    if store:
        have = have | {"page_docs"}                    # the store stands in for the table
    for t, (cols, sql) in STAGE.items():
        st.execute(f"CREATE TABLE {t} ({cols})")
        if sql is None or OPTIONAL_SOURCE.get(t, "") not in have | {""}:
            continue
        n = len(cols.split(","))
        ins = f"INSERT OR IGNORE INTO {t} VALUES ({','.join('?' * n)})"
        rc = REAL_COLS.get(t, ())
        if store and t in STORE_SQL:                   # SEARCH_PAGES_STORE: page_docs from files (release/pages_store.py)
            sql = STORE_SQL[t]
            if t == "s_pages":
                st.executemany(ins, pages_store.rows(store, ["url", "domain", "title"]))
            else:
                st.executemany(ins, ((d,) for d, crawl in pages_store.rows(store, ["domain", "crawl"], order=False)
                                     if (crawl or "").startswith("open:")))
            if sql is None:
                st.commit()
                log(f"stage {t}: {st.execute(f'SELECT count(*) FROM {t}').fetchone()[0]} (pages store {store})")
                continue
        with pg.cursor(name=f"s{t}") as cur:
            cur.itersize = 20000
            cur.execute(sql)
            while True:
                b = cur.fetchmany(20000)
                if not b:
                    break
                if t in ("s_top", "s_phrases"):
                    b = [(r[0], js(r[1]), js(r[2])) for r in b]
                if rc:
                    b = [tuple(f32(v) if i in rc else v for i, v in enumerate(r)) for r in b]
                st.executemany(ins, b)
        st.commit()
        log(f"stage {t}: {st.execute(f'SELECT count(*) FROM {t}').fetchone()[0]}")
    stage_own(st, own_file)
    sm = dict(st.execute("SELECT key, value FROM s_sugmeta"))
    if sm and sm.get("own_rank_sha256") != sha256_file(own_file):
        sys.exit(f"the suggestion tables were built from another own-rank file (sha256 {sm.get('own_rank_sha256')}) than "
                 f"{own_file}: run release/suggest_own.py with this file first — refusing")
    log(f"suggestions: rank source {sm.get('rank_source', 'the build database (Tranco, browser history)')}")
    stage_front_pages(st)
    stage_pages(st)
    if blocked:
        stage_blocked(st, blocked)
    if ut1_unsafe:
        stage_unsafe(st, ut1_unsafe)
    st.executescript("""
        CREATE INDEX s_wiki_title ON s_wiki (title, page_id);
        CREATE INDEX s_facts_title ON s_facts (title);
        CREATE INDEX s_next_title ON s_next (title, o);
        CREATE INDEX s_links_wt ON s_links (wiki_title);
        CREATE INDEX s_items_wiki ON s_items (wiki);
        CREATE INDEX s_nav_host ON s_nav (host);
        CREATE INDEX s_hostnav_url ON s_hostnav (url);""")
    # G3 D4c opt-outs: those sites' shown description is blanked (release/export.py OPTOUT_FILE)
    of = os.environ.get("OPTOUT_FILE")
    if of and os.path.exists(of):
        dom = [l.strip().lower() for l in open(of) if l.strip() and not l.startswith("#")]
        st.executemany("UPDATE s_docs SET description = NULL WHERE domain = ?", [(d,) for d in dom])
        st.commit()
        log(f"opt-outs: {len(dom)} domains lose their description")
    log(f"capture policy: {stage_capture_policy(st)}")
    return st


def staged_policy(st) -> dict:
    """descr_policy as stage_capture_policy recorded it; nothing for a stage made before it existed (the publish gate
    then refuses the lookup file)."""
    if not st.execute("SELECT 1 FROM sqlite_master WHERE name = 's_policy'").fetchone():
        return {}
    return dict(st.execute("SELECT key, value FROM s_policy WHERE key = 'descr_policy'"))      # pages_capture_out is the registry's


def stage_capture_policy(st) -> dict:
    """What a site said about being shown, applied to the staged homepages (importer/docs_policy.py is the rule, G3 D4c):
    a Wayback capture and a noindex capture leave the stage whole (no title, no address, no description from them); a
    description is dropped for nosnippet / max-snippet:0 / a TDM reservation, cut to 160 characters otherwise, and
    dropped when the capture was never read for its robots statements — unless DESCR_UNCHECKED=keep, for a build that is
    not published. The subdomain-service descriptions (s_hostnav) carry no such record: dropped unless that is set, cut
    when it is. The outcome is stamped into lookup.sqlite's meta (descr_policy) and the publish gate reads it."""
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "importer"))
    import docs_policy as dp
    unchecked = os.environ.get("DESCR_UNCHECKED", "drop")
    if unchecked not in ("drop", "keep"):
        sys.exit(f"DESCR_UNCHECKED must be drop or keep, not {unchecked!r}")
    n = {"wayback captures dropped": 0, "noindex captures dropped": 0, "descriptions kept": 0, "cut": 0,
         "dropped: site said no": 0, "dropped: never checked": 0}
    gone, said_no, upd = [], [], []
    for domain, descr, crawl, robots in st.execute(
            "SELECT d.domain, d.description, s.crawl, s.robots FROM s_docs d LEFT JOIN s_docs_src s USING (domain)").fetchall():
        if not dp.capture_ok(crawl, robots):
            n["wayback captures dropped" if (crawl or "").startswith("wayback") else "noindex captures dropped"] += 1
            gone.append((domain,))
            if dp.said_noindex(robots):
                said_no.append((domain,))
            continue
        if not descr:
            continue
        shown = dp.shown_description(descr, crawl, robots, unchecked)
        if shown is None:
            n["dropped: never checked" if robots is None else "dropped: site said no"] += 1
        else:
            n["descriptions kept"] += 1
            n["cut"] += shown != " ".join(descr.split())
        if shown != descr:
            upd.append((shown, domain))
    st.executemany("DELETE FROM s_docs WHERE domain = ?", gone)
    # the inner pages of a site that SAID noindex on its homepage go too (audit 2026-10-22 G4); a Wayback-only homepage
    # keeps its pages: they were not read from the Archive (docs_policy.PG_DOMAINS_NOINDEX)
    st.execute("CREATE TEMP TABLE IF NOT EXISTS gone_docs (domain TEXT PRIMARY KEY)")
    st.execute("DELETE FROM gone_docs")
    st.executemany("INSERT OR IGNORE INTO gone_docs VALUES (?)", said_no)
    n["pages dropped: their site's homepage capture"] = st.execute(
        "DELETE FROM s_pages WHERE domain IN (SELECT domain FROM gone_docs)").rowcount
    st.executemany("UPDATE s_docs SET description = ? WHERE domain = ?", upd)
    if unchecked == "keep":
        rows = [(dp.cut(d, dp.DESCR_MAX), u) for u, d in st.execute("SELECT url, description FROM s_hostnav WHERE description <> ''").fetchall()]
        st.executemany("UPDATE s_hostnav SET description = ? WHERE url = ?", rows)
    else:
        n["service descriptions dropped"] = st.execute("UPDATE s_hostnav SET description = NULL WHERE description IS NOT NULL").rowcount
    policy = f"max={dp.DESCR_MAX};unchecked={unchecked};wayback=drop;noindex=drop"
    st.execute("CREATE TABLE IF NOT EXISTS s_policy (key TEXT PRIMARY KEY, value TEXT)")
    st.execute("INSERT OR REPLACE INTO s_policy VALUES ('descr_policy', ?)", (policy,))
    st.execute("INSERT OR REPLACE INTO s_policy VALUES ('pages_capture_out', ?)",
               (json.dumps({"pages": n["pages dropped: their site's homepage capture"]}),))
    st.commit()
    n["policy"] = policy
    return n


# ------------------------------------------------------------------ build
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--registry", help="T5's registry.sqlite (else a stand-in is written)")
    ap.add_argument("--release-id", default="t4-" + time.strftime("%Y%m%d"))
    ap.add_argument("--stage", help="reuse this staging file (skip Postgres)")
    ap.add_argument("--own-rank", help="own_rank.tsv (importer/own_rank.py compute): the site rank and the navigable hosts (G3 D2); "
                                       "needed whenever the stage is built from Postgres")
    ap.add_argument("--ut1-unsafe", help="ut1-unsafe.tsv.gz: flag every site of the release that UT1 lists, instead of the "
                                         "database's Tranco-filtered site_quality rows (staging only)")
    ap.add_argument("--blocked", help="blocked.tsv (own_rank.py blocklist): removed from the stage, hosts under a blocked name too (staging only)")
    ap.add_argument("--places-dir", help="the dated places release (utility/places/release.py: manifest.json + places-osm / "
                                         "places-overture / hours_web TSVs); without it places come from the staged Postgres rows (OSM only)")
    ap.add_argument("--previous", help="the PREVIOUS release's lookup folder (lookup.sqlite + places-*.sqlite): ids stay stable "
                                       "across releases (places.pid, dict ids, cards.t, accounts.vord, stocks.tord). Needs --registry, "
                                       "whose doc_ids must be the previous release's. Without it every id is renumbered densely as before")
    a = ap.parse_args()
    global PREV
    if a.previous:
        if not a.registry:
            sys.exit("--previous needs --registry: ids carry over by doc_id, and the stand-in registry renumbers doc_ids every build — refusing")
        PREV = os.path.abspath(a.previous)
        for f in ("lookup.sqlite",):
            if not os.path.exists(os.path.join(PREV, f)):
                sys.exit(f"--previous {PREV}: {f} is missing — refusing")
    os.makedirs(a.out, exist_ok=True)
    stp = a.stage or os.path.join(a.out, "stage.sqlite")
    if a.stage and os.path.exists(a.stage):
        if a.ut1_unsafe or a.blocked:
            sys.exit("--ut1-unsafe and --blocked change the stage, which the registry was already built from: pass them "
                     "when the stage is made — refusing")
        st = sqlite3.connect(a.stage)
        st.execute("PRAGMA cache_size = -300000")
    else:
        if not a.own_rank:
            sys.exit("staging from Postgres needs --own-rank own_rank.tsv: Tranco's nav_sites and ranks are no longer read (G3 D2) — refusing")
        import psycopg
        pg = psycopg.connect()
        pg.execute("SET statement_timeout = 0")
        st = stage(pg, stp, a.own_rank, a.ut1_unsafe, a.blocked)
        pg.close()

    PLACE_SRC = "s_places"
    if a.places_dir:
        meta_places = load_places(st, a.places_dir)
        PLACE_SRC = "(SELECT osm, name FROM p_osm UNION ALL SELECT osm, name FROM p_ov)"
    # ---- things (type, key, url, title), first writer of url/title wins
    st.executescript(f"""
        DROP TABLE IF EXISTS things;
        CREATE TABLE things (type INTEGER, key TEXT, url TEXT, title TEXT, PRIMARY KEY (type, key)) WITHOUT ROWID;
        INSERT OR IGNORE INTO things SELECT {WIKI}, CAST(page_id AS TEXT), 'https://en.wikipedia.org/wiki/' || replace(title, ' ', '_'), title FROM s_wiki;
        INSERT OR IGNORE INTO things SELECT {SITE}, domain, url, title FROM s_docs;
        INSERT OR IGNORE INTO things SELECT {SITE}, domain, 'https://' || domain || '/', NULL FROM s_serve;
        INSERT OR IGNORE INTO things SELECT {SITE}, host, 'https://' || host || '/', NULL FROM s_nav;
        INSERT OR IGNORE INTO things SELECT {SITE}, domain, 'https://' || domain || '/', NULL FROM s_wsites WHERE domain IS NOT NULL;
        INSERT OR IGNORE INTO things SELECT {LINK}, url, url, title FROM s_hostnav;
        INSERT OR IGNORE INTO things SELECT {ACCOUNT}, {AKEY0}, url, name FROM s_links;
        INSERT OR IGNORE INTO things SELECT {YOUTUBE}, channel_id, NULL, name FROM s_yt;
        INSERT OR IGNORE INTO things SELECT {STOCK}, ticker, NULL, coalesce(label, name) FROM s_stock;
        INSERT OR IGNORE INTO things SELECT {PLACE}, osm, NULL, name FROM {PLACE_SRC};
        INSERT OR IGNORE INTO things SELECT {SITE}, domain, url, NULL FROM s_items WHERE kind = 'site' AND domain IS NOT NULL;
        INSERT OR IGNORE INTO things SELECT {SITE}, domain, 'https://' || domain || '/', NULL FROM s_unsafe;
        INSERT OR IGNORE INTO things SELECT {LINK}, url, url, name FROM s_items i
            WHERE NOT (kind = 'site' AND domain IS NOT NULL) AND NOT (kind = 'wiki' AND EXISTS (SELECT 1 FROM s_wiki w WHERE w.title = i.wiki));
    """)
    st.commit()
    if a.registry:
        use_registry(st, a.registry)
        # the (type, key) index BEFORE the check below: without it the NOT EXISTS scans doc once per
        # thing (21M x 21M on real data: 1.5 h and still running, 2026-10-03)
        st.executescript("CREATE UNIQUE INDEX IF NOT EXISTS doc_tk ON doc (type, key);")
        miss = st.execute("SELECT t.type, count(*), min(t.key) FROM things t WHERE NOT EXISTS "
                          "(SELECT 1 FROM doc d WHERE d.type = t.type AND d.key = t.key) GROUP BY t.type").fetchall()
        if miss:
            sys.exit(f"registry {a.registry} lacks documents (type, count, example key): {miss} — refusing")
    else:
        st.executescript(f"DROP TABLE IF EXISTS doc; CREATE TABLE doc AS SELECT row_number() OVER (ORDER BY type, CASE WHEN type IN ({WIKI}) THEN CAST(key AS INTEGER) END, key) AS doc_id, type, key, url, title FROM things;")
    # CROSS JOIN doc below = "doc is the inner table": without it the planner once looped
    # over 6.8M wiki docs scanning s_wiki for each (4 h, never finished)
    st.executescript("""CREATE UNIQUE INDEX IF NOT EXISTS doc_tk ON doc (type, key); ANALYZE;""")
    st.commit()
    ndoc = st.execute("SELECT max(doc_id) FROM doc").fetchone()[0]
    log(f"things: {st.execute('SELECT count(*) FROM things').fetchone()[0]}, doc_id max {ndoc}")
    # helper maps as tables
    st.executescript(f"""
        DROP TABLE IF EXISTS wdoc; CREATE TABLE wdoc AS SELECT w.page_id, w.title, d.doc_id FROM s_wiki w CROSS JOIN doc d ON d.type = {WIKI} AND d.key = CAST(w.page_id AS TEXT);
        CREATE INDEX wdoc_title ON wdoc (title, page_id); CREATE UNIQUE INDEX wdoc_pid ON wdoc (page_id);
        DROP TABLE IF EXISTS tdoc; CREATE TABLE tdoc AS SELECT title, min(page_id) AS page_id FROM s_wiki WHERE title IS NOT NULL GROUP BY title;
        CREATE UNIQUE INDEX tdoc_t ON tdoc (title);
        DROP TABLE IF EXISTS idoc; CREATE TABLE idoc (id INTEGER PRIMARY KEY, doc_id INTEGER);
        INSERT OR IGNORE INTO idoc SELECT i.id, w.doc_id FROM s_items i JOIN tdoc t ON t.title = i.wiki JOIN wdoc w ON w.page_id = t.page_id
            WHERE i.kind = 'wiki';
        INSERT OR IGNORE INTO idoc SELECT i.id, d.doc_id FROM s_items i CROSS JOIN doc d ON d.type = {SITE} AND d.key = i.domain
            WHERE i.kind = 'site' AND i.domain IS NOT NULL;
        INSERT OR IGNORE INTO idoc SELECT i.id, d.doc_id FROM s_items i CROSS JOIN doc d ON d.type = {LINK} AND d.key = i.url
            WHERE i.id NOT IN (SELECT id FROM idoc);
        CREATE INDEX idoc_doc ON idoc (doc_id);
        ANALYZE;
    """)
    st.commit()
    log(f"items mapped to docs: {st.execute('SELECT count(*) FROM idoc').fetchone()[0]} of {st.execute('SELECT count(*) FROM s_items').fetchone()[0]}; "
        f"docs with >1 item: {st.execute('SELECT count(*) FROM (SELECT doc_id FROM idoc GROUP BY doc_id HAVING count(*) > 1)').fetchone()[0]}")

    if not a.registry:
        rp = os.path.join(a.out, "registry.sqlite")
        if os.path.exists(rp):
            os.remove(rp)
        st.execute(f"ATTACH '{rp}' AS rg")
        st.executescript("""CREATE TABLE rg.doc (doc_id INTEGER PRIMARY KEY, type INTEGER, key TEXT, url TEXT, title TEXT);
            INSERT INTO rg.doc SELECT doc_id, type, key, url, title FROM doc ORDER BY doc_id;
            CREATE INDEX rg.doc_title ON doc (title) WHERE type = 1;""")
        st.commit(); st.execute("DETACH rg")
        log(f"registry STAND-IN -> {rp} ({os.path.getsize(rp) / 1e6:.0f} MB)")
        write_signals(st, a.out, ndoc, a.release_id)

    lookup(st, a.out, a.release_id)
    if a.places_dir:
        places_release(st, a.out, a.release_id, meta_places)
    else:
        places(st, a.out, a.release_id)
    typo(a.out, a.release_id)
    log("BUILD-OK")


# T5's registry.sqlite (release_v1.py + the extension agreed with its owner, 2026-10-03):
# registry(doc_id, ext, host_id, path, title, alt), hosts(id, origin, domain), platforms(id, name,
# prefix, suffix); doc_id = type << type_shift | seq (28; read from registry meta) with the same type numbers as ours. A document's identity
# is rebuilt from its own columns (what apps/search/release/registry_idents.py idmap() does):
#   wiki   str(ext)                       site    path (the HOST; hosts is not used)
#   place  path (osm id)                  account platforms.name TAB path TAB 'Q' || ext
#   youtube path (channel id)             stock   path (ticker)
#   link   coalesce(hosts.origin || path, alt)           (3 page: not used here)
# url: wiki = wikipedia + title; site = coalesce(alt, 'https://' || path || '/'); link as its ident.
def use_registry(st, path):
    st.execute(f"ATTACH '{path}' AS r")
    tabs = {x[0] for x in st.execute("SELECT name FROM r.sqlite_master WHERE type = 'table'")}
    if "registry" not in tabs:
        sys.exit(f"registry {path}: no registry table (tables {sorted(tabs)}) — refusing")
    meta = dict(st.execute("SELECT key, value FROM r.meta")) if "meta" in tabs else {}
    shift = int(meta.get("type_shift", 28))
    if int(meta.get("layout", 3)) < 3 or shift != 28:
        sys.exit(f"registry {path}: layout {meta.get('layout')} / type_shift {meta.get('type_shift')} — want layout 3, shift 28 — refusing")
    st.executescript(f"""
        DROP TABLE IF EXISTS doc;
        CREATE TABLE doc (doc_id INTEGER, type INTEGER, key TEXT, url TEXT, title TEXT);
        INSERT INTO doc SELECT r.doc_id, r.doc_id >> {shift} AS type,
               CASE r.doc_id >> {shift} WHEN {WIKI} THEN CAST(r.ext AS TEXT) WHEN {SITE} THEN r.path
                    WHEN {ACCOUNT} THEN p.name || char(9) || r.path || char(9) || 'Q' || r.ext
                    WHEN {LINK} THEN coalesce(h.origin || r.path, r.alt) ELSE r.path END AS key,
               CASE r.doc_id >> {shift} WHEN {WIKI} THEN 'https://en.wikipedia.org/wiki/' || replace(r.title, ' ', '_')
                    WHEN {SITE} THEN coalesce(r.alt, 'https://' || r.path || '/')
                    WHEN {LINK} THEN coalesce(h.origin || r.path, r.alt) END AS url, r.title
            FROM r.registry r LEFT JOIN r.hosts h ON h.id = r.host_id AND r.doc_id >> {shift} = {LINK}
            LEFT JOIN r.platforms p ON p.id = r.host_id AND r.doc_id >> {shift} = {ACCOUNT}
            WHERE r.doc_id >> {shift} IN ({WIKI}, {SITE}, {PLACE}, {ACCOUNT}, {YOUTUBE}, {STOCK}, {LINK})
              AND r.doc_id NOT IN (SELECT doc_id FROM r.tombstones);
    """)
    log(f"registry {path}: {st.execute('SELECT count(*) FROM doc').fetchone()[0]} documents")


def write_signals(st, out, ndoc, release_id):
    """STAND-IN for T5's signals.bin: one fixed-width record per doc_id (index 0 unused). Its "tranco" field is the
    reader's name for the site rank (lookups_release.T5_OWN); the value is own_rank (G3 D2)."""
    import numpy as np
    dt = np.dtype([("tranco", "<i4"), ("nav_rank", "<i4"), ("cc_hc", "<i4"), ("site_rank", "<i4"), ("sitelinks", "<i4"),
                   ("followers", "<f8"), ("demand", "<f4"), ("pop", "<f4"), ("score", "<f4")])
    arr = np.zeros(ndoc + 1, dtype=dt)
    for f in ("tranco", "nav_rank", "cc_hc", "site_rank", "sitelinks"):
        arr[f] = -1
    for f in ("followers", "demand", "pop", "score"):
        arr[f] = np.nan
    q = {
        "tranco,cc_hc": f"SELECT d.doc_id, s.own_rank, s.cc_hc FROM s_serve s CROSS JOIN doc d ON d.type = {SITE} AND d.key = s.domain",
        "nav_rank": f"SELECT d.doc_id, min(n.rank) FROM s_nav n CROSS JOIN doc d ON d.type = {SITE} AND d.key = n.host GROUP BY d.doc_id",
        "nav_rank ": f"SELECT d.doc_id, min(h.rank) FROM s_hostnav h CROSS JOIN doc d ON d.type = {LINK} AND d.key = h.url GROUP BY d.doc_id",
        "site_rank": "SELECT w.doc_id, s.own_rank FROM s_wsites s JOIN wdoc w USING (page_id)",
        "sitelinks": f"SELECT d.doc_id, max(l.sitelinks) FROM s_links l CROSS JOIN doc d ON d.type = {ACCOUNT} AND "
                     f"d.key = {AKEY} GROUP BY d.doc_id",
        "sitelinks,followers": f"SELECT d.doc_id, y.sitelinks, y.followers FROM s_yt y CROSS JOIN doc d ON d.type = {YOUTUBE} AND d.key = y.channel_id",
        "sitelinks ": f"SELECT d.doc_id, s.sitelinks FROM s_stock s CROSS JOIN doc d ON d.type = {STOCK} AND d.key = s.ticker",
        "pop,score,demand": "SELECT x.doc_id, i.pop, i.score, i.demand FROM s_items i JOIN idoc x USING (id) ORDER BY i.id DESC",
    }
    for fields, sql in q.items():
        fs = [f.strip() for f in fields.split(",")]
        for r in st.execute(sql):
            for f, v in zip(fs, r[1:]):
                if v is not None:
                    arr[r[0]][f] = v
    arr.tofile(os.path.join(out, "signals.bin"))
    json.dump({"format_version": FORMAT_VERSION, "release_id": release_id, "records": len(arr),
               "dtype": [(n, dt.fields[n][0].str) for n in dt.names], "null": "-1 for ints, NaN for floats",
               "stand_in": True}, open(os.path.join(out, "signals.json"), "w"), indent=1)
    log(f"signals STAND-IN: {len(arr)} x {dt.itemsize} B = {arr.nbytes / 1e6:.0f} MB")


def lookup(st, out, release_id):
    path = os.path.join(out, "lookup.sqlite")
    # rc5 W2: where the suggestion set came from, for `release_v1.py check --tranco-free` (a stage made before W2 has no table)
    sm = (dict(st.execute("SELECT key, value FROM s_sugmeta"))
          if st.execute("SELECT 1 FROM sqlite_master WHERE name = 's_sugmeta'").fetchone() else {})
    db = open_out(path, release_id, "lookup", {**{"suggest_" + k: sm[k] for k in ("rank_source", "own_rank_sha256", "site_cut", "history") if k in sm},
                                             **({"unsafe_source": sm["unsafe_source"]} if "unsafe_source" in sm else {}),
                                             **staged_policy(st)})
    db.close()
    import zlib
    st.create_function("adult_ending", 1, lambda h: 1 if (h or "").endswith(ADULT_ENDINGS) else 0, deterministic=True)
    st.create_function("zc", 1, lambda t: None if t is None else zlib.compress(t.encode("utf-8"), 6), deterministic=True)
    st.execute(f"ATTACH '{path}' AS L")
    st.executescript(f"""
        PRAGMA L.secure_delete = ON; PRAGMA L.journal_mode = OFF; PRAGMA L.synchronous = OFF;
        -- one dictionary for repeated strings (platforms, boxes, fact names, units, kinds, exchanges, domains)
        CREATE TEMP TABLE ds (s TEXT PRIMARY KEY) WITHOUT ROWID;
        INSERT OR IGNORE INTO ds SELECT platform FROM s_links WHERE platform IS NOT NULL;      -- fact/profile code compares names
        INSERT OR IGNORE INTO ds SELECT box FROM s_wsites WHERE box IS NOT NULL;
        INSERT OR IGNORE INTO ds SELECT fact FROM s_facts WHERE fact IS NOT NULL;
        INSERT OR IGNORE INTO ds SELECT unit FROM s_facts WHERE unit IS NOT NULL;
        INSERT OR IGNORE INTO ds SELECT kind FROM s_items WHERE kind IS NOT NULL;
        INSERT OR IGNORE INTO ds SELECT exchange FROM s_stock WHERE exchange IS NOT NULL;
        INSERT OR IGNORE INTO ds SELECT domain FROM s_nav WHERE domain IS NOT NULL;
        CREATE TABLE L.dict (id INTEGER PRIMARY KEY, s TEXT UNIQUE);
    """)
    T_EXPR, T_JOIN, VORD_EXPR, VORD_JOIN, TORD_EXPR, TORD_JOIN = "s.tord", "", "l.vord", "", "s.tord", ""
    if PREV:
        st.execute("ATTACH ? AS V", (os.path.join(PREV, "lookup.sqlite"),))
        # previous ids stay (a string that left keeps its row, so its id is never handed out again); new strings are appended
        st.execute("INSERT INTO L.dict (id, s) SELECT id, s FROM V.dict ORDER BY id")
        st.execute("INSERT INTO L.dict (s) SELECT s FROM ds WHERE s NOT IN (SELECT s FROM L.dict) ORDER BY s")
        # cards.t / accounts.vord / stocks.tord are ORDER keys (the reader sorts on them), so they stay in the new
        # en_US order but keep the previous number for every unchanged key
        pt = prev_card_t(os.path.join(PREV, "lookup.sqlite"))
        ptitle = {}
        for name, did in st.execute(f"SELECT name, doc_id FROM V.names WHERE kind = {N_TITLE}"):
            if did in pt:
                ptitle[name] = pt[did]
        stable_rank_table(st, "rk_wiki", "SELECT DISTINCT tord, title FROM s_wiki WHERE title IS NOT NULL ORDER BY tord", ptitle, "card title")
        T_EXPR, T_JOIN = "coalesce(rk.t, s.tord)", "LEFT JOIN rk_wiki rk ON rk.rank = s.tord"
        pv = dict(st.execute("SELECT doc_id, vord FROM V.accounts WHERE vord IS NOT NULL"))
        pvals = {}
        for did, val in st.execute(f"SELECT d.doc_id, l.value FROM s_links l CROSS JOIN doc d ON d.type = {ACCOUNT} AND d.key = {AKEY}"):
            if did in pv and val is not None:
                pvals[val] = pv[did]
        stable_rank_table(st, "rk_vord", "SELECT DISTINCT vord, value FROM s_links WHERE value IS NOT NULL ORDER BY vord", pvals, "account value")
        VORD_EXPR, VORD_JOIN = "coalesce(rv.t, l.vord)", "LEFT JOIN rk_vord rv ON rv.rank = l.vord"
        ps = dict(st.execute("SELECT doc_id, tord FROM V.stocks WHERE tord IS NOT NULL"))
        ptick = {}
        for did, tk in st.execute(f"SELECT d.doc_id, s.ticker FROM s_stock s CROSS JOIN doc d ON d.type = {STOCK} AND d.key = s.ticker"):
            if did in ps:
                ptick[tk] = ps[did]
        stable_rank_table(st, "rk_tord", "SELECT DISTINCT tord, ticker FROM s_stock ORDER BY tord", ptick, "stock ticker")
        TORD_EXPR, TORD_JOIN = "coalesce(rt.t, s.tord)", "LEFT JOIN rk_tord rt ON rt.rank = s.tord"
        del pt, ptitle, pv, pvals, ps, ptick
    else:
        st.execute("INSERT INTO L.dict (s) SELECT s FROM ds ORDER BY s")
    st.commit()
    log("dict built")
    # ---- cards: one record per article
    st.executescript(f"""
        CREATE TEMP TABLE c_facts AS SELECT title, json_group_array(json_array(fd.id, f.value, ud.id, f.prec, f.at)) AS j
            FROM (SELECT * FROM s_facts ORDER BY title, rowid) f JOIN L.dict fd ON fd.s = f.fact LEFT JOIN L.dict ud ON ud.s = f.unit GROUP BY title;
        CREATE INDEX temp.c_facts_t ON c_facts (title);
        CREATE TEMP TABLE c_rel AS SELECT n.title, json_group_array(w.doc_id) AS j
            FROM (SELECT title, next, o FROM s_next WHERE o <= {RELATED_N} ORDER BY title, o) n
            JOIN tdoc t ON t.title = n.next JOIN wdoc w ON w.page_id = t.page_id GROUP BY n.title;
        CREATE INDEX temp.c_rel_t ON c_rel (title);
        CREATE TEMP TABLE c_descr AS SELECT wiki, descr FROM (SELECT wiki, descr, row_number() OVER
            (PARTITION BY wiki ORDER BY demand DESC NULLS LAST, id) rn FROM s_items WHERE wiki IS NOT NULL AND descr IS NOT NULL) WHERE rn = 1;
        CREATE INDEX temp.c_descr_w ON c_descr (wiki);
        CREATE TEMP TABLE c_prof AS SELECT wiki_title, json_group_array(doc_id) AS j FROM (
            SELECT l.wiki_title, d.doc_id, l.platform FROM (SELECT *, row_number() OVER (PARTITION BY wiki_title, platform
                ORDER BY length(value), vord) rn FROM s_links WHERE wiki_title IS NOT NULL) l
            CROSS JOIN doc d ON d.type = {ACCOUNT} AND d.key = {AKEY}
            WHERE l.rn = 1 ORDER BY l.wiki_title, l.platform) GROUP BY wiki_title;
        CREATE INDEX temp.c_prof_t ON c_prof (wiki_title);
        -- c: the card as zlib-compressed JSON (the leads are 80% of the bytes and compress about 2:1)
        CREATE TABLE L.cards (doc_id INTEGER PRIMARY KEY, c BLOB);
        INSERT INTO L.cards SELECT w.doc_id, zc(json_patch('{{}}', json_object(
                't', {T_EXPR}, 'l', s.lead, 'd', cd.descr,
                'o', CASE WHEN ws.page_id IS NOT NULL THEN json_array(ws.url, bd.id, sd.doc_id) END,
                'f', CASE WHEN own THEN json(cf.j) END, 'r', CASE WHEN own THEN json(cr.j) END,
                'p', CASE WHEN own THEN json(cp.j) END)))
            FROM (SELECT w.*, (SELECT page_id FROM tdoc t WHERE t.title = w.title) = w.page_id AS own FROM wdoc w) w
            JOIN s_wiki s ON s.page_id = w.page_id {T_JOIN}
            LEFT JOIN c_descr cd ON cd.wiki = w.title
            LEFT JOIN s_wsites ws ON ws.page_id = w.page_id LEFT JOIN L.dict bd ON bd.s = ws.box
            LEFT JOIN doc sd ON sd.type = {SITE} AND sd.key = ws.domain
            LEFT JOIN c_facts cf ON cf.title = w.title LEFT JOIN c_rel cr ON cr.title = w.title
            LEFT JOIN c_prof cp ON cp.wiki_title = w.title
            ORDER BY w.page_id;
    """)
    st.commit()
    log(f"cards: {st.execute('SELECT count(*) FROM L.cards').fetchone()[0]}")
    # ---- accounts: the registry holds platform, handle, url and the Q number (store once); here only what
    # it lacks: the article an account belongs to, its en_US order, and whether it has an hkey / skey
    # (ACCOUNT_SQL's NULL ordering). entities (Q number -> name, description) is the display name.
    st.executescript(f"""
        CREATE TABLE L.entities (id INTEGER PRIMARY KEY, name TEXT, description TEXT);
        INSERT OR IGNORE INTO L.entities SELECT CAST(substr(qid, 2) AS INTEGER), name, description FROM s_links
            WHERE qid GLOB 'Q[0-9]*' ORDER BY qid;
        CREATE TABLE L.accounts (doc_id INTEGER PRIMARY KEY, wiki INTEGER, vord INTEGER, hk INTEGER, sk INTEGER);
        INSERT OR IGNORE INTO L.accounts SELECT d.doc_id,
                (SELECT w.doc_id FROM tdoc t JOIN wdoc w USING (page_id) WHERE t.title = l.wiki_title), {VORD_EXPR},
                CASE WHEN l.hkey IS NOT NULL THEN 1 END, CASE WHEN l.skey IS NOT NULL THEN 1 END
            FROM s_links l {VORD_JOIN} CROSS JOIN doc d ON d.type = {ACCOUNT} AND d.key = {AKEY} ORDER BY d.doc_id;
        CREATE INDEX L.accounts_wiki ON accounts (wiki) WHERE wiki IS NOT NULL;
    """)
    st.commit()
    log(f"accounts: {st.execute('SELECT count(*) FROM L.accounts').fetchone()[0]}")
    # ---- sites, links, youtube, stocks
    st.executescript(f"""
        -- home: a crawled homepage exists (the registry's url/title are its own); domain: nav_sites' registrable domain
        -- ut1: hidden unless the query names the site. 1 = on UT1's unsafe lists (CC BY-SA 4.0), 2 = our own rule, an adult ending
        CREATE TABLE L.sites (doc_id INTEGER PRIMARY KEY, home INTEGER, description TEXT, domain INTEGER, ut1 INTEGER);
        INSERT INTO L.sites SELECT * FROM (SELECT d.doc_id, CASE WHEN h.domain IS NOT NULL THEN 1 END AS home, h.description,
                (SELECT dd.id FROM s_nav n JOIN L.dict dd ON dd.s = n.domain WHERE n.host = d.key LIMIT 1) AS dom,
                CASE WHEN d.key IN (SELECT domain FROM s_unsafe) THEN 1 WHEN adult_ending(d.key) THEN 2 END AS u
            FROM doc d LEFT JOIN s_docs h ON h.domain = d.key WHERE d.type = {SITE})
            WHERE home IS NOT NULL OR dom IS NOT NULL OR u IS NOT NULL ORDER BY doc_id;
        CREATE TABLE L.links (doc_id INTEGER PRIMARY KEY, description TEXT);
        INSERT OR IGNORE INTO L.links SELECT d.doc_id, h.description FROM s_hostnav h CROSS JOIN doc d ON d.type = {LINK} AND d.key = h.url ORDER BY d.doc_id;
        -- the channel id is the registry's ident, the name its title
        CREATE TABLE L.youtube (doc_id INTEGER PRIMARY KEY, handle TEXT, description TEXT, wiki INTEGER);
        INSERT INTO L.youtube SELECT d.doc_id, y.handle, y.description,
                (SELECT w.doc_id FROM tdoc t JOIN wdoc w USING (page_id) WHERE t.title = y.wiki_title)
            FROM s_yt y CROSS JOIN doc d ON d.type = {YOUTUBE} AND d.key = y.channel_id ORDER BY d.doc_id;
        -- the ticker is the registry's ident, coalesce(label, name) its title
        CREATE TABLE L.stocks (doc_id INTEGER PRIMARY KEY, exchange INTEGER, cik INTEGER, qid TEXT, site TEXT, ir TEXT, word INTEGER, tord INTEGER);
        INSERT INTO L.stocks SELECT d.doc_id, xd.id, s.cik, s.qid, s.site, s.ir, s.word, {TORD_EXPR}
            FROM s_stock s {TORD_JOIN} CROSS JOIN doc d ON d.type = {STOCK} AND d.key = s.ticker LEFT JOIN L.dict xd ON xd.s = s.exchange ORDER BY d.doc_id;
        -- suggestions: items keyed by doc_id (their weights live in signals.bin)
        -- wiki: the article a non-article item names (a site's own article), as a doc_id
        CREATE TABLE L.suggest_items (doc_id INTEGER PRIMARY KEY, kind INTEGER, name TEXT, url TEXT, descr TEXT, wiki INTEGER);
        INSERT OR IGNORE INTO L.suggest_items SELECT x.doc_id, kd.id, i.name, CASE WHEN i.url = d.url THEN NULL ELSE i.url END, i.descr,
                CASE WHEN i.kind <> 'wiki' AND i.wiki IS NOT NULL THEN (SELECT w.doc_id FROM tdoc t JOIN wdoc w USING (page_id) WHERE t.title = i.wiki) END
            FROM s_items i JOIN idoc x USING (id) JOIN doc d ON d.doc_id = x.doc_id JOIN L.dict kd ON kd.s = i.kind ORDER BY x.doc_id, i.id;
        CREATE TABLE L.suggest_keys (key TEXT, doc_id INTEGER, pos INTEGER, PRIMARY KEY (key, doc_id, pos)) WITHOUT ROWID;
        INSERT OR IGNORE INTO L.suggest_keys SELECT k.key, x.doc_id, k.pos FROM s_keys k JOIN idoc x USING (id) ORDER BY k.key, x.doc_id, k.pos;
    """)
    st.commit()
    # suggest_top: item ids -> doc_ids; phrases: cut at PHRASE_MIN searches, counts dropped
    imap = dict(st.execute("SELECT id, doc_id FROM idoc"))
    st.execute("CREATE TABLE L.suggest_top (prefix TEXT PRIMARY KEY, docs TEXT, pos TEXT) WITHOUT ROWID")
    top = []
    for prefix, ids, pos in st.execute("SELECT prefix, ids, pos FROM s_top ORDER BY prefix"):
        z = [(imap[i], p) for i, p in zip(json.loads(ids) or [], json.loads(pos) or []) if i in imap]
        top.append((prefix, js([x for x, _ in z]), js([y for _, y in z])))
    st.executemany("INSERT INTO L.suggest_top VALUES (?,?,?)", top)
    del imap
    st.execute("CREATE TABLE L.suggest_phrases (prefix TEXT PRIMARY KEY, phrases TEXT) WITHOUT ROWID")
    ph = []
    for prefix, phrases, counts in st.execute("SELECT prefix, phrases, counts FROM s_phrases ORDER BY prefix"):
        keep = [p for p, c in zip(json.loads(phrases) or [], json.loads(counts) or []) if (c or 0) >= PHRASE_MIN]
        if keep:
            ph.append((prefix, js(keep)))
    st.executemany("INSERT INTO L.suggest_phrases VALUES (?,?)", ph)
    st.commit()
    log(f"suggest: {len(top)} top lists, {len(ph)} phrase lists (>= {PHRASE_MIN:g} searches)")
    # ---- names: every exact-match tier, one table
    st.executescript(f"""
        CREATE TEMP TABLE nm (name TEXT, kind INTEGER, doc_id INTEGER, flags INTEGER);
        INSERT INTO nm SELECT title, {N_TITLE}, doc_id, 0 FROM wdoc WHERE title IS NOT NULL;
        INSERT INTO nm SELECT a.norm, {N_ALIAS}, w.doc_id, 0 FROM s_alias a JOIN wdoc w USING (page_id);
        INSERT INTO nm SELECT n.key, {N_HOST}, d.doc_id, 0 FROM s_nav n CROSS JOIN doc d ON d.type = {SITE} AND d.key = n.host;
        INSERT INTO nm SELECT key, {N_DOMAIN}, doc_id, 0 FROM doc WHERE type = {SITE};
        INSERT INTO nm SELECT substr(h.domain, 1, instr(h.domain || '.', '.') - 1), {N_SITE_LABEL}, d.doc_id, 0
            FROM s_docs h CROSS JOIN doc d ON d.type = {SITE} AND d.key = h.domain;
        INSERT INTO nm SELECT h.key, {N_HOST_NAV}, d.doc_id, 0 FROM s_hostnav h CROSS JOIN doc d ON d.type = {LINK} AND d.key = h.url;
        INSERT INTO nm SELECT ws.domain, {N_OFFICIAL}, w.doc_id, 0 FROM s_wsites ws JOIN wdoc w USING (page_id) WHERE ws.domain IS NOT NULL;
        INSERT INTO nm SELECT k.key, {N_STOCK}, d.doc_id, 0 FROM s_stock_keys k CROSS JOIN doc d ON d.type = {STOCK} AND d.key = k.ticker;
        INSERT INTO nm SELECT lower(ticker), {N_TICKER}, d.doc_id, 0 FROM s_stock s CROSS JOIN doc d ON d.type = {STOCK} AND d.key = s.ticker;
        CREATE TEMP TABLE ad AS SELECT l.*, d.doc_id FROM s_links l CROSS JOIN doc d ON d.type = {ACCOUNT}
            AND d.key = {AKEY};
        INSERT INTO nm SELECT key, {N_ACCOUNT}, doc_id, {F_KEY} FROM ad WHERE key IS NOT NULL;
        INSERT INTO nm SELECT hkey, {N_ACCOUNT}, doc_id, {F_HKEY} FROM ad WHERE hkey IS NOT NULL;
        INSERT INTO nm SELECT wkey, {N_ACCOUNT}, doc_id, {F_WKEY} FROM ad WHERE wkey IS NOT NULL;
        INSERT INTO nm SELECT skey, {N_ACCOUNT}, doc_id, {F_SKEY} FROM ad WHERE skey IS NOT NULL;
        CREATE TEMP TABLE yd AS SELECT y.*, d.doc_id FROM s_yt y CROSS JOIN doc d ON d.type = {YOUTUBE} AND d.key = y.channel_id;
        INSERT INTO nm SELECT key, {N_YOUTUBE}, doc_id, {F_KEY} FROM yd WHERE key IS NOT NULL;
        INSERT INTO nm SELECT hkey, {N_YOUTUBE}, doc_id, {F_HKEY} FROM yd WHERE hkey IS NOT NULL;
        INSERT INTO nm SELECT wkey, {N_YOUTUBE}, doc_id, {F_WKEY} FROM yd WHERE wkey IS NOT NULL;
        INSERT INTO nm SELECT skey, {N_YOUTUBE}, doc_id, {F_SKEY} FROM yd WHERE skey IS NOT NULL;
    """)
    # self-named sites: a homepage label = a known prefix + the name, or the name + a known suffix
    selfn = []
    for dom, did in st.execute(f"SELECT h.domain, d.doc_id FROM s_docs h CROSS JOIN doc d ON d.type = {SITE} AND d.key = h.domain"):
        ln = norm_key(dom.split(".", 1)[0])
        for p in SELF_NAMED_PREFIX:
            if ln.startswith(p) and len(ln) > len(p):
                selfn.append((ln[len(p):], N_SELF, did, 0))
        for s in SELF_NAMED_SUFFIX:
            if ln.endswith(s) and len(ln) > len(s):
                selfn.append((ln[:-len(s)], N_SELF, did, 0))
    st.executemany("INSERT INTO nm VALUES (?,?,?,?)", selfn)
    st.executescript("""
        CREATE TABLE L.names (name TEXT, kind INTEGER, doc_id INTEGER, flags INTEGER, PRIMARY KEY (name, kind, doc_id, flags)) WITHOUT ROWID;
        INSERT OR IGNORE INTO L.names SELECT name, kind, doc_id, flags FROM nm WHERE name IS NOT NULL AND name <> '' ORDER BY name, kind, doc_id, flags;
        DROP TABLE nm; DROP TABLE ad; DROP TABLE yd;
    """)
    st.commit()
    log(f"names: {st.execute('SELECT count(*) FROM L.names').fetchone()[0]} "
        f"({', '.join(f'{k}={n}' for k, n in st.execute('SELECT kind, count(*) FROM L.names GROUP BY kind'))})")
    # ---- FTS5 trigram: fuzzy wiki titles, fuzzy site labels (rowid = doc_id)
    st.executescript("""
        CREATE VIRTUAL TABLE L.wiki_trgm USING fts5(t, content='', contentless_delete=1, detail='none', tokenize='trigram');
        CREATE VIRTUAL TABLE L.label_trgm USING fts5(t, content='', contentless_delete=1, detail='none', tokenize='trigram');""")
    cur = st.cursor()
    for sql, fts in ((f"SELECT w.doc_id, w.title FROM tdoc t JOIN wdoc w USING (page_id)", "wiki_trgm"),
                     (f"SELECT d.doc_id, substr(h.domain, 1, instr(h.domain || '.', '.') - 1) FROM s_docs h CROSS JOIN doc d ON d.type = {SITE} AND d.key = h.domain", "label_trgm")):
        rows_ = st.execute(sql).fetchall()
        cur.executemany(f"INSERT INTO L.{fts} (rowid, t) VALUES (?, ?)", ((i, trgm.padded(t)) for i, t in rows_ if t))
        st.execute(f"INSERT INTO L.{fts} ({fts}) VALUES ('optimize')")
        st.commit()
    log("FTS5 trigram indexes built")
    st.commit()
    if PREV:
        st.execute("DETACH V")
    st.execute("DETACH L")
    db = sqlite3.connect(path)
    db.execute("PRAGMA secure_delete = ON")
    finish(db, path)


def places(st, out, release_id):
    """No places release given: the OSM rows staged from Postgres (WDC and ov: rows left out), one file."""
    st.executescript("""
        DROP TABLE IF EXISTS p_osm;
        CREATE TABLE p_osm AS SELECT osm, name, kind, brand, lat, lon, street, city, hours, website, phone,
            NULL AS hours_checked, NULL AS edited, name_key, brand_key, brand_lower FROM s_places WHERE osm NOT LIKE 'ov:%';
    """)
    write_places(st, out, release_id, "places-osm", "p_osm", {"licence": "ODbL-1.0 (© OpenStreetMap contributors)"})


# ------------------------------------------------------------------ places release (one file per licence)
PLACE_COLS = ("osm", "name", "kind", "brand", "lat", "lon", "street", "city", "hours", "website", "phone", "hours_checked", "edited")
HW_COLS = ("osm", "hours", "crawled", "url", "name")
# click / ad tracking parameters: a place's website is a fact, the tracking is not
TRACKING = re.compile(r"^(utm_.*|fbclid|gclid|dclid|msclkid|yclid|mc_cid|mc_eid|igshid|_ga|_gl|gbraid|wbraid|ref_src)$", re.I)
PLACE_ID = re.compile(r"^(node|way|relation)/\d+$|^ov:[0-9a-f-]{8,}$")


def clean_url(u: str) -> str:
    if not u or "?" not in u:
        return u
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
    sp = urlsplit(u)
    q = [(k, v) for k, v in parse_qsl(sp.query, keep_blank_values=True) if not TRACKING.match(k)]
    return urlunsplit((sp.scheme, sp.netloc, sp.path, urlencode(q), sp.fragment))


def read_tsv(path, ncols):
    import subprocess
    p = subprocess.Popen(["zstd", "-dc", path], stdout=subprocess.PIPE)
    n = 0
    for raw in p.stdout:
        n += 1
        f = raw.decode("utf-8").rstrip("\n").split("\t")
        if len(f) != ncols:
            sys.exit(f"{path}: line {n} has {len(f)} columns, want {ncols} — refusing")
        yield f
    if p.wait():
        sys.exit(f"zstd -dc {path} failed")


def sha256_file(path):
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def load_places(st, d):
    """Verify the places release against its manifest (sha256, row counts, licence tag, column list),
    refuse any wdc: id, and stage p_osm / p_ov (13 columns) and p_hw (5 columns). Tracking parameters
    are dropped from websites and hours urls. Returns the manifest."""
    man = json.load(open(os.path.join(d, "manifest.json")))
    if man["columns"]["places"] != list(PLACE_COLS) or man["columns"]["hours_web"] != list(HW_COLS):
        sys.exit(f"{d}/manifest.json: columns {man['columns']} differ from what build.py reads — refusing")
    files = {"p_osm": "places-osm.tsv.zst", "p_ov": "places-overture.tsv.zst", "p_hw": "hours_web.tsv.zst"}
    for t, fn in files.items():
        m = man["files"].get(fn)
        if not m or not m.get("licence"):
            sys.exit(f"{d}/manifest.json: {fn} missing or without a licence — refusing")
        got = sha256_file(os.path.join(d, fn))
        if got != m["sha256"]:
            sys.exit(f"{fn}: sha256 {got} does not match the manifest's {m['sha256']} — refusing")
    log(f"places release {man['release']}: sha256 of the three files match the manifest")
    for t in ("p_osm", "p_ov", "p_hw"):
        st.execute(f"DROP TABLE IF EXISTS {t}")
    for t in ("p_osm", "p_ov"):
        st.execute(f"CREATE TABLE {t} (osm TEXT PRIMARY KEY, name TEXT, kind TEXT, brand TEXT, lat REAL, lon REAL, street TEXT, city TEXT, "
                   "hours TEXT, website TEXT, phone TEXT, hours_checked TEXT, edited TEXT, name_key TEXT, brand_key TEXT, brand_lower TEXT)")
    st.execute("CREATE TABLE p_hw (osm TEXT PRIMARY KEY, hours TEXT, crawled TEXT, url TEXT, name TEXT)")
    for t, fn in files.items():
        n, batch = 0, []
        want = man["files"][fn]["rows"]
        for f in read_tsv(os.path.join(d, fn), 13 if t != "p_hw" else 5):
            if f[0].startswith("wdc:"):
                sys.exit(f"{fn}: a wdc: id ({f[0]}) — Web Data Commons is research-only (G3 D3) — refusing")
            if not PLACE_ID.match(f[0]):
                sys.exit(f"{fn}: unknown place id {f[0]!r} — refusing")
            if (t == "p_ov") != f[0].startswith("ov:") and t != "p_hw":
                sys.exit(f"{fn}: id {f[0]!r} is in the wrong licence file — refusing")
            n += 1
            if t == "p_hw":
                batch.append((f[0], f[1], f[2], clean_url(f[3]), f[4]))
            else:
                osm, name, kind, brand, lat, lon, street, city, hours, web, phone, hc, ed = f
                nk = "".join(ch for ch in trgm.pg_lower(name) if ch.isascii() and ch.isalnum())
                bk = "".join(ch for ch in trgm.pg_lower(brand) if ch.isascii() and ch.isalnum()) if brand else None
                batch.append((osm, name, kind, brand, float(lat) if lat else None, float(lon) if lon else None, street, city, hours,
                              clean_url(web), phone, hc or None, ed or None, nk, bk, trgm.pg_lower(brand) if brand else None))
            if len(batch) >= 50000:
                st.executemany(f"INSERT OR IGNORE INTO {t} VALUES ({','.join('?' * len(batch[0]))})", batch)
                batch.clear()
        if batch:
            st.executemany(f"INSERT OR IGNORE INTO {t} VALUES ({','.join('?' * len(batch[0]))})", batch)
        st.commit()
        if n != want:
            sys.exit(f"{fn}: {n} rows, the manifest says {want} — refusing")
        log(f"{t}: {n} rows from {fn}")
    return man


def write_places(st, out, release_id, role, table, extra):
    """One licence, one file: places + R*Tree + FTS5 trigram names. Same schema for OSM and Overture."""
    path = os.path.join(out, f"{role}.sqlite")
    db = open_out(path, release_id, role, extra)
    db.close()
    st.execute(f"ATTACH '{path}' AS P")
    CELLX = f"CAST((lat + 90) / {CELL} AS INTEGER) * {NLON} + CAST((lon + 180) / {CELL} AS INTEGER)"
    if PREV:
        # stable ids: a place keeps its pid while its cell is unchanged (the cell is the pid's high bits and the
        # reader's R*Tree ranges); a new place takes the next sequence number of its cell, past every number the cell
        # has EVER used (pid_hwm, carried release to release), so a retired pid is never handed out again
        pp = os.path.join(PREV, f"{role}.sqlite")
        if not os.path.exists(pp):
            sys.exit(f"--previous {PREV}: {role}.sqlite is missing — refusing")
        st.execute("ATTACH ? AS V", (pp,))
        st.executescript(f"""
            DROP TABLE IF EXISTS temp.pp; DROP TABLE IF EXISTS temp.hw; DROP TABLE IF EXISTS temp.pc;
            DROP TABLE IF EXISTS temp.pm_old; DROP TABLE IF EXISTS temp.pm_new; DROP TABLE IF EXISTS temp.pmap;
            CREATE TEMP TABLE pp AS SELECT doc_id, pid FROM V.places;
            CREATE INDEX temp.pp_d ON pp (doc_id);
            CREATE TEMP TABLE hw AS SELECT cell, max(nxt) AS nxt FROM (
                SELECT pid >> {SEQ_BITS} AS cell, (pid & {SEQ_MASK}) + 1 AS nxt FROM pp
                UNION ALL SELECT CAST(substr(key, 9) AS INTEGER), CAST(value AS INTEGER) FROM V.meta WHERE key GLOB 'pid_hwm:*') GROUP BY cell;
            CREATE UNIQUE INDEX temp.hw_c ON hw (cell);
            CREATE TEMP TABLE pc AS SELECT p.osm, d.doc_id, c.cell FROM {table} p
                JOIN (SELECT osm, {CELLX} AS cell FROM {table}) c USING (osm)
                CROSS JOIN doc d ON d.type = {PLACE} AND d.key = p.osm WHERE p.lat IS NOT NULL;
            CREATE TEMP TABLE pm_old AS SELECT c.osm, c.doc_id, pp.pid FROM pc c JOIN pp ON pp.doc_id = c.doc_id AND (pp.pid >> {SEQ_BITS}) = c.cell;
            CREATE UNIQUE INDEX temp.pm_old_d ON pm_old (doc_id);
            CREATE TEMP TABLE pm_new AS SELECT n.osm, n.doc_id, n.cell, coalesce(hw.nxt, 0) + n.rn - 1 AS seq FROM (
                SELECT c.osm, c.doc_id, c.cell, row_number() OVER (PARTITION BY c.cell ORDER BY c.osm) AS rn
                FROM pc c WHERE c.doc_id NOT IN (SELECT doc_id FROM pm_old)) n LEFT JOIN hw ON hw.cell = n.cell;
        """)
        over = st.execute(f"SELECT cell, max(seq) FROM pm_new WHERE seq > {SEQ_MASK} GROUP BY cell LIMIT 5").fetchall()
        if over:
            sys.exit(f"{role}: cell(s) {over} (cell, seq) would overflow SEQ_BITS={SEQ_BITS} ({SEQ_MASK + 1} places per cell) — "
                     f"raise SEQ_BITS in build.py AND lookups_release.py, or build without --previous")
        st.executescript(f"""
            CREATE TEMP TABLE pmap AS SELECT osm, pid FROM pm_old UNION ALL SELECT osm, (cell << {SEQ_BITS}) | seq FROM pm_new;
            CREATE UNIQUE INDEX temp.pmap_o ON pmap (osm);
        """)
        log(f"{role}: {st.execute('SELECT count(*) FROM pm_old').fetchone()[0]} pids carried, "
            f"{st.execute('SELECT count(*) FROM pm_new').fetchone()[0]} new")
    st.executescript(f"""
        PRAGMA P.secure_delete = ON; PRAGMA P.journal_mode = OFF; PRAGMA P.synchronous = OFF;
        CREATE TABLE P.dict (id INTEGER PRIMARY KEY, s TEXT UNIQUE);
        {"INSERT INTO P.dict (id, s) SELECT id, s FROM V.dict ORDER BY id;" if PREV else ""}
        INSERT INTO P.dict (s) SELECT DISTINCT coalesce(kind, '') FROM {table}
            {"WHERE coalesce(kind, '') NOT IN (SELECT s FROM P.dict)" if PREV else ""} ORDER BY 1;
        -- hours_checked / edited: YYYY-MM-DD of the newest check tag / the element's last edit (OSM only)
        CREATE TABLE P.places (pid INTEGER PRIMARY KEY, doc_id INTEGER, name TEXT, kind INTEGER, brand TEXT, lat REAL, lon REAL,
            street TEXT, city TEXT, hours TEXT, website TEXT, phone TEXT, hours_checked TEXT, edited TEXT,
            name_key TEXT, brand_key TEXT, brand_lower TEXT);
        INSERT INTO P.places SELECT {"pm.pid" if PREV else f"(c.cell << {SEQ_BITS}) | (row_number() OVER (PARTITION BY c.cell ORDER BY p.osm) - 1)"},
                d.doc_id, p.name, kd.id, p.brand, p.lat, p.lon, p.street, p.city, p.hours, p.website, p.phone, p.hours_checked, p.edited,
                p.name_key, p.brand_key, p.brand_lower
            FROM {table} p {"JOIN pmap pm ON pm.osm = p.osm" if PREV else f"JOIN (SELECT osm, {CELLX} AS cell FROM {table}) c USING (osm)"}
            CROSS JOIN doc d ON d.type = {PLACE} AND d.key = p.osm JOIN P.dict kd ON kd.s = coalesce(p.kind, '')
            WHERE p.lat IS NOT NULL ORDER BY 1;
        CREATE INDEX P.places_name_key ON places (name_key);
        CREATE INDEX P.places_brand_key ON places (brand_key) WHERE brand_key <> '';
        CREATE INDEX P.places_brand_lower ON places (brand_lower) WHERE brand_lower <> '';
        CREATE VIRTUAL TABLE P.places_geo USING rtree(id, lat0, lat1, lon0, lon1, k0, k1);
        INSERT INTO P.places_geo SELECT pid, lat, lat, lon, lon, kind, kind FROM P.places;
        CREATE VIRTUAL TABLE P.place_names USING fts5(t, content='', contentless_delete=1, detail='none', tokenize='trigram');
    """)
    rows_ = st.execute("SELECT pid, name FROM P.places WHERE name <> '' ORDER BY pid").fetchall()
    st.executemany("INSERT INTO P.place_names (rowid, t) VALUES (?, ?)", ((i, trgm.padded(n)) for i, n in rows_))
    st.execute("INSERT INTO P.place_names (place_names) VALUES ('optimize')")
    n_towns = towns.write(st, "P")        # where each town is, from this file's own addresses: meta rows, no new table
    if PREV:
        st.executescript(f"""
            DROP TABLE IF EXISTS temp.curmax;
            CREATE TEMP TABLE curmax AS SELECT pid >> {SEQ_BITS} AS cell, max(pid & {SEQ_MASK}) + 1 AS nxt FROM P.places GROUP BY 1;
            -- only a cell whose top sequence number was retired needs a row: elsewhere max + 1 says it already. meta, not a table,
            -- so the file's schema is the same as a release built without --previous (a schema change ships the file whole)
            INSERT INTO P.meta SELECT 'pid_hwm:' || h.cell, CAST(h.nxt AS TEXT) FROM (
                SELECT cell, max(nxt) AS nxt FROM (SELECT cell, nxt FROM hw UNION ALL SELECT cell, nxt FROM curmax) GROUP BY cell) h
                LEFT JOIN curmax c ON c.cell = h.cell WHERE h.nxt > coalesce(c.nxt, 0) ORDER BY 1;
        """)
    st.commit()
    log(f"{role}: {st.execute('SELECT count(*) FROM P.places').fetchone()[0]} places, {len(rows_)} named, {n_towns} towns")
    if PREV:
        st.execute("DETACH V")
    st.execute("DETACH P")
    db = sqlite3.connect(path)
    db.execute("PRAGMA secure_delete = ON")
    finish(db, path)


# places-overture.sqlite's own meta.licence = its manifest tag (release/assemble.py LICENCES, which refuses a file whose meta
# says otherwise; a test pins the two strings). The places release's own tag says CDLA only: the gap-fill is chosen against
# OpenStreetMap, so ODbL too (owner, 2026-10-09; audit 2026-10-22 G6)
OVERTURE_LICENCE = ("ODbL-1.0 (rows selected against OpenStreetMap) + CDLA-Permissive-2.0 (Overture Maps Foundation; "
                    "Foursquare records Apache-2.0, AllThePlaces records CC0-1.0) + CC0-1.0 (chain branches from All the Places)")
# Overture's source dataset names -> the provider names its attribution page asks for ("Data from [provider name]").
# None: Overture's own datasets (overture, overture-signals), credited as Overture Maps Foundation on its own line
OVERTURE_PROVIDER_NAMES = {"meta": "Meta", "msft": "Microsoft", "microsoft": "Microsoft", "foursquare": "Foursquare",
                           "alltheplaces": "All the Places", "all the places": "All the Places", "pinmeto": "PinMeTo",
                           "krick": "Krick", "renvoi": "Renvoi", "overture": None, "overture-signals": None}


def provider_names(datasets) -> list:
    """Overture dataset names -> the sorted provider names to credit (Overture's own datasets dropped, duplicates merged)."""
    names = {OVERTURE_PROVIDER_NAMES.get(k.lower(), k) for k in datasets}
    return sorted(n for n in names if n)


def overture_providers(src: dict):
    """The providers whose records are in places-overture, from the places release's sources: Overture's datasets as
    release.py overture counted them on the rows it kept (overture.json "providers"), plus All the Places when its
    branches were added. None when the places release did not record them (built before 2026-10-22)."""
    got = (src.get("overture") or {}).get("providers")
    if not got:
        return None
    return provider_names([k for k, n in got.items() if n] + (["All the Places"] if src.get("atp") else []))


def places_release(st, out, release_id, man):
    f = man["files"]
    src = man.get("sources", {})
    write_places(st, out, release_id, "places-osm", "p_osm",
                 {"licence": f["places-osm.tsv.zst"]["licence"], "attribution": "© OpenStreetMap contributors (ODbL 1.0)",
                  "source": json.dumps(src.get("osm"), sort_keys=True), "places_release": man["release"]})
    prov = overture_providers(src)
    write_places(st, out, release_id, "places-overture", "p_ov",
                 {"licence": OVERTURE_LICENCE,
                  "attribution": "© OpenStreetMap contributors (ODbL 1.0: rows selected against OpenStreetMap); "
                  "© Overture Maps Foundation (CDLA-Permissive-2.0); "
                  + (f"data from {', '.join(prov)}" if prov else "providers not recorded by places release " + man["release"])
                  + ("; Foursquare records: Copyright 2024 Foursquare Labs, Inc. All rights reserved. (Apache-2.0)"
                     if prov is None or "Foursquare" in prov else "")
                  + ("; chain branches: All the Places (CC0 1.0)" if src.get("atp") else ""),
                  **({"providers": json.dumps(prov)} if prov else {}),
                  "source": json.dumps(src.get("overture"), sort_keys=True), "places_release": man["release"]})
    # hours-web.sqlite: the businesses' own published hours, kept apart (facts only, source + date + page)
    path = os.path.join(out, "hours-web.sqlite")
    db = open_out(path, release_id, "hours-web",
                  {"licence": f["hours_web.tsv.zst"]["licence"], "source": json.dumps(src.get("hours"), sort_keys=True),
                   "places_release": man["release"]})
    db.close()
    st.execute(f"ATTACH '{path}' AS H")
    st.executescript(f"""
        PRAGMA H.secure_delete = ON; PRAGMA H.journal_mode = OFF; PRAGMA H.synchronous = OFF;
        CREATE TABLE H.hours_web (doc_id INTEGER PRIMARY KEY, hours TEXT, crawled TEXT, url TEXT, name TEXT) WITHOUT ROWID;
        INSERT INTO H.hours_web SELECT d.doc_id, w.hours, w.crawled, w.url, w.name FROM p_hw w
            CROSS JOIN doc d ON d.type = {PLACE} AND d.key = w.osm ORDER BY d.doc_id;
    """)
    st.commit()
    miss = st.execute("SELECT count(*) FROM p_hw w WHERE NOT EXISTS (SELECT 1 FROM doc d WHERE d.type = %d AND d.key = w.osm)" % PLACE).fetchone()[0]
    log(f"hours-web: {st.execute('SELECT count(*) FROM H.hours_web').fetchone()[0]} places with hours ({miss} not in the registry)")
    st.execute("DETACH H")
    db = sqlite3.connect(path)
    db.execute("PRAGMA secure_delete = ON")
    finish(db, path)


def typo(out, release_id):
    import tantivy
    td = os.path.join(out, "text", "typo")
    shutil.rmtree(td, ignore_errors=True)
    os.makedirs(td)
    sb = tantivy.SchemaBuilder()
    sb.add_text_field("key", stored=True, tokenizer_name="raw", index_option="basic")
    tix = tantivy.Index(sb.build(), path=td)
    tw = tix.writer(heap_size=300_000_000, num_threads=1)      # one thread, fixed order: same input, same files
    n = 0
    for (key,) in sqlite3.connect(os.path.join(out, "lookup.sqlite")).execute("SELECT DISTINCT key FROM suggest_keys ORDER BY key"):
        tw.add_document(tantivy.Document(key=key))
        n += 1
    tw.commit()
    tw.wait_merging_threads()
    # T5's Tantivy stamp (rule 8; release/release_v1.py tvnormalize rewrites it and normalises the segment ids)
    import tantivy as _tv
    json.dump({"format": FORMAT_VERSION, "built_release": release_id, "role": "typo", "tantivy": getattr(_tv, "__version__", "0.26.2"),
               "analyzer": "raw", "keys": n}, open(os.path.join(td, "release.json"), "w"), sort_keys=True)
    open(os.path.join(td, "release.json"), "a").write("\n")
    log(f"typo index: {n} keys, {sum(os.path.getsize(os.path.join(td, f)) for f in os.listdir(td)) / 1e6:.0f} MB")


if __name__ == "__main__":
    main()
