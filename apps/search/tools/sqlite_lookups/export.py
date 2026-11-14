"""Build lookup.sqlite from the serving Postgres (T4, SEARCH-HOME-ENGINE-PLAN.md).

  python export.py OUT.sqlite [GROUPS] [--no-jev]
      GROUPS: comma list of places,suggest,wiki,nav,kinds,misc (default: all). Read-only on PGHOST.
      --no-jev: the shippable file (ADR-065): no kinds tier, only UT1's junk-filter rows.
      Also writes OUT.typo-keys/ (Tantivy index of the suggestion keys) beside OUT.

Everything search_api does as an exact / structured lookup (serve/api/lookups_sqlite.py reads it):
  places       places (+ name/brand keys), place_kinds, places_geo (R*Tree: lat, lon, kind),
               place_names (FTS5 trigram over pg_trgm-padded names, contentless)
  suggest      suggest_items, suggest_keys, suggest_top, suggest_phrases; typo keys -> Tantivy
  wiki         wiki_titles (page_id, title, tord), wiki_leads (lead[:300]), wiki_trgm (FTS5,
               fuzzy fallback), wiki_aliases, wiki_sites, wiki_facts, wiki_links (+ vord),
               wiki_next
  nav          nav_sites, host_nav, serve_sites, docs_meta (homepages: url/title/description)
               + label_trgm / label_sub (FTS5), site_junk (domain, source)
  kinds        kind_keys + kind_key_words, kind_members, kind_docs (no search_text), kind_vecs
               — ALL Jev-derived (ADR-065): left out by --no-jev
  misc         stock_keys, stock_tickers (+ tord), subreddits, youtube_channels (lookup columns)

Jev-derived (ADR-065), never in the shipped file: every kinds table; site_quality rows whose
source is "jev" (only "ut1" / "jev+ut1" — flagged by UT1's lists — are kept by --no-jev).
tord / vord = the row's rank in Postgres's en_US order, for ORDER BY on text.
Ids are stable from month to month: places.id = cell << 20 | seq (0.5-degree grid square,
seq in osm order at first build). Exports are ORDER BY'd so two builds of the same data are
the same file.
"""
import json, math, os, sqlite3, struct, sys, time
import psycopg
import trgm

ARGS = [a for a in sys.argv[1:] if not a.startswith("--")]
OUT = ARGS[0]
GROUPS = set((ARGS[1] if len(ARGS) > 1 else "places,suggest,wiki,nav,kinds,misc").split(","))
# ADR-065: the shipped file holds no Jev-derived data. --no-jev leaves out the whole kinds tier
# (kind_docs phrases were Jev-filtered; kind_keys / kind_members / kind_vecs derive from it)
# and keeps only the junk-filter rows UT1's lists flag (source ut1 or jev+ut1), not Jev's.
NO_JEV = "--no-jev" in sys.argv
if NO_JEV:
    GROUPS.discard("kinds")
TYPO_DIR = os.path.join(os.path.dirname(os.path.abspath(OUT)), os.path.basename(OUT).rsplit(".", 1)[0] + ".typo-keys")
T0 = time.time()


def log(*a):
    print(f"[{time.time() - T0:7.1f}s]", *a, flush=True)


pg = psycopg.connect()
pg.execute("SET statement_timeout = 0")
db = sqlite3.connect(OUT)
db.executescript("PRAGMA journal_mode = OFF; PRAGMA synchronous = OFF; PRAGMA cache_size = -400000; "
                 "PRAGMA temp_store = FILE; PRAGMA secure_delete = OFF;")


def pg_rows(sql, n=20000):
    with pg.cursor(name=f"x{time.monotonic_ns()}") as cur:
        cur.itersize = n
        cur.execute(sql)
        while True:
            batch = cur.fetchmany(n)
            if not batch:
                break
            yield batch


def f32(x):
    """A Postgres real (float4) as the double it really is: psycopg reads it as text ("0.7"),
    and a comparison in SQL (jev >= 0.7) must see 0.699999988 as Postgres does."""
    return None if x is None else struct.unpack("f", struct.pack("f", x))[0]


def f32_at(*cols):
    return lambda r: tuple(f32(v) if i in cols else v for i, v in enumerate(r))


def copy(table, create, sql, ncol, conv=None, ignore=False):
    db.execute(f"DROP TABLE IF EXISTS {table}")
    db.execute(create)
    ins = f"INSERT {'OR IGNORE ' if ignore else ''}INTO {table} VALUES ({','.join('?' * ncol)})"
    n = 0
    for batch in pg_rows(sql):
        if conv:
            batch = [conv(r) for r in batch]
        db.executemany(ins, batch)
        n += len(batch)
    db.commit()
    got = db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    log(f"{table}: {n} rows exported, {got} stored")
    return n


def index(*stmts):
    for s in stmts:
        db.execute(s)
    db.commit()


def tables_present():
    return {r[0] for r in pg.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")}


HAS = tables_present()

# ---------------------------------------------------------------- places
CELL = 0.5
NLON = int(360 / CELL)


def cell_of(lat, lon):
    return int((lat + 90) // CELL) * NLON + int((lon + 180) // CELL)


if "places" in GROUPS:
    db.executescript("""
      DROP TABLE IF EXISTS places_stage; DROP TABLE IF EXISTS places; DROP TABLE IF EXISTS place_kinds;
      DROP TABLE IF EXISTS places_geo; DROP TABLE IF EXISTS place_names;
      CREATE TABLE places_stage (cell INTEGER, osm TEXT, name TEXT, kind TEXT, brand TEXT, lat REAL, lon REAL,
        street TEXT, city TEXT, hours TEXT, website TEXT, phone TEXT, name_key TEXT, brand_key TEXT, brand_lower TEXT);
    """)
    n = 0
    for batch in pg_rows("""SELECT osm, name, kind, brand, lat, lon, street, city, hours, website, phone,
            regexp_replace(lower(name), '[^a-z0-9]', '', 'g'),
            CASE WHEN brand <> '' THEN regexp_replace(lower(brand), '[^a-z0-9]', '', 'g') END,
            CASE WHEN brand <> '' THEN lower(brand) END
            FROM places ORDER BY osm"""):
        db.executemany("INSERT INTO places_stage VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       [(cell_of(r[4], r[5]) if r[4] is not None else -1, *r) for r in batch])
        n += len(batch)
    log(f"places_stage: {n}")
    db.executescript("""
      CREATE TABLE place_kinds (id INTEGER PRIMARY KEY, kind TEXT UNIQUE);
      INSERT INTO place_kinds (kind) SELECT DISTINCT coalesce(kind, '') FROM places_stage ORDER BY 1;
      CREATE TABLE places (id INTEGER PRIMARY KEY, osm TEXT NOT NULL, name TEXT, kind_id INTEGER, brand TEXT,
        lat REAL, lon REAL, street TEXT, city TEXT, hours TEXT, website TEXT, phone TEXT,
        name_key TEXT, brand_key TEXT, brand_lower TEXT);
      INSERT INTO places
        SELECT (s.cell << 20) | (row_number() OVER (PARTITION BY s.cell ORDER BY s.osm) - 1),
               s.osm, s.name, k.id, s.brand, s.lat, s.lon, s.street, s.city, s.hours, s.website, s.phone,
               s.name_key, s.brand_key, s.brand_lower
        FROM places_stage s JOIN place_kinds k ON k.kind = coalesce(s.kind, '') ORDER BY 1;
      DROP TABLE places_stage;
    """)
    log("places: ids assigned")
    index("CREATE INDEX places_osm ON places (osm)",
          "CREATE INDEX places_name_key ON places (name_key)",
          "CREATE INDEX places_brand_key ON places (brand_key) WHERE brand_key <> ''",
          "CREATE INDEX places_brand_lower ON places (brand_lower) WHERE brand_lower <> ''")
    db.executescript("""
      CREATE VIRTUAL TABLE places_geo USING rtree(id, lat0, lat1, lon0, lon1, k0, k1);
      INSERT INTO places_geo SELECT id, lat, lat, lon, lon, kind_id, kind_id FROM places WHERE lat IS NOT NULL;
      CREATE VIRTUAL TABLE place_names USING fts5(t, content='', contentless_delete=1, detail='none', tokenize='trigram');
    """)
    log("places_geo built")
    batch = []
    for pid, name in db.execute("SELECT id, name FROM places WHERE name <> '' ORDER BY id").fetchall():
        batch.append((pid, trgm.padded(name)))
    db.executemany("INSERT INTO place_names (rowid, t) VALUES (?, ?)", batch)
    db.execute("INSERT INTO place_names (place_names) VALUES ('optimize')")
    db.commit()
    log(f"place_names: {len(batch)}")

# ---------------------------------------------------------------- suggest

if "suggest" in GROUPS:
    copy("suggest_items", "CREATE TABLE suggest_items (id INTEGER PRIMARY KEY, kind TEXT, name TEXT, url TEXT, "
         "domain TEXT, wiki TEXT, pop REAL, hist REAL, score REAL, demand REAL, descr TEXT)",
         "SELECT id, kind, name, url, domain, wiki, pop, hist, score, demand, descr FROM suggest_items ORDER BY id", 11,
         f32_at(6, 7, 8, 9))
    index("CREATE INDEX suggest_items_wiki ON suggest_items (wiki) WHERE wiki IS NOT NULL")
    copy("suggest_keys", "CREATE TABLE suggest_keys (key TEXT, id INTEGER, pos INTEGER, kind TEXT, score REAL, "
         "PRIMARY KEY (key, id, pos)) WITHOUT ROWID",
         "SELECT key, id, pos, kind, score FROM suggest_keys ORDER BY key COLLATE \"C\", id, pos", 5, f32_at(4), ignore=True)
    js = lambda a: json.dumps(a, separators=(",", ":")) if a is not None else None
    packed = [0, 0]

    def top(r):
        packed[0] += len(js(r[1]) or "") + len(js(r[2]) or "")
        packed[1] += 4 * len(r[1] or []) + 2 * len(r[2] or [])
        return (r[0], js(r[1]), js(r[2]))
    copy("suggest_top", "CREATE TABLE suggest_top (prefix TEXT PRIMARY KEY, ids TEXT, pos TEXT) WITHOUT ROWID",
         "SELECT prefix, ids, pos FROM suggest_top ORDER BY prefix COLLATE \"C\"", 3, top)
    log(f"suggest_top arrays: JSON {packed[0] / 1e6:.1f} MB vs packed int32/int16 {packed[1] / 1e6:.1f} MB")
    copy("suggest_phrases", "CREATE TABLE suggest_phrases (prefix TEXT PRIMARY KEY, phrases TEXT, counts TEXT) WITHOUT ROWID",
         "SELECT prefix, phrases, counts FROM suggest_phrases ORDER BY prefix COLLATE \"C\"", 3,
         lambda r: (r[0], js(r[1]), js(r[2])))
    # typo correction: a Tantivy index of the distinct keys (fuzzy term queries), beside the file
    import shutil
    import tantivy
    shutil.rmtree(TYPO_DIR, ignore_errors=True)
    os.makedirs(TYPO_DIR)
    sb = tantivy.SchemaBuilder()
    sb.add_text_field("key", stored=True, tokenizer_name="raw", index_option="basic")
    tix = tantivy.Index(sb.build(), path=TYPO_DIR)
    tw = tix.writer(heap_size=300_000_000, num_threads=1)
    n = 0
    for (key,) in db.execute("SELECT DISTINCT key FROM suggest_keys WHERE key IS NOT NULL ORDER BY key"):
        tw.add_document(tantivy.Document(key=key))
        n += 1
    tw.commit()
    tw.wait_merging_threads()
    size = sum(os.path.getsize(os.path.join(TYPO_DIR, f)) for f in os.listdir(TYPO_DIR))
    log(f"typo keys: {n} distinct keys -> Tantivy {TYPO_DIR} ({size / 1e6:.1f} MB)")

# ---------------------------------------------------------------- wiki
if "wiki" in GROUPS:
    # tord: the title's rank in Postgres's en_US order (SQLite compares bytes), for ORDER BY title
    copy("wiki_titles", "CREATE TABLE wiki_titles (page_id INTEGER PRIMARY KEY, title TEXT, tord INTEGER)",
         "SELECT page_id, title, dense_rank() OVER (ORDER BY title) FROM wiki_docs ORDER BY page_id", 3)
    index("CREATE INDEX wiki_titles_title ON wiki_titles (title)")
    # the lead as search_api shows it (_wiki: lead[:300]); in the shipped engine the Tantivy
    # store's snippet is the same text, and one of the two owns it
    copy("wiki_leads", "CREATE TABLE wiki_leads (page_id INTEGER PRIMARY KEY, lead TEXT)",
         "SELECT page_id, left(lead, 300) FROM wiki_docs WHERE lead IS NOT NULL ORDER BY page_id", 2)
    # the tier-4 fuzzy title fallback (FUZZY_WIKI_SQL): pg_trgm-padded titles, rowid = page_id
    db.executescript("DROP TABLE IF EXISTS wiki_trgm; CREATE VIRTUAL TABLE wiki_trgm USING fts5(t, content='', "
                     "contentless_delete=1, detail='none', tokenize='trigram');")
    cur = db.execute("SELECT page_id, title FROM wiki_titles WHERE title IS NOT NULL ORDER BY page_id")
    while True:
        batch = cur.fetchmany(100000)
        if not batch:
            break
        db.executemany("INSERT INTO wiki_trgm (rowid, t) VALUES (?, ?)", [(i, trgm.padded(t)) for i, t in batch])
    db.execute("INSERT INTO wiki_trgm (wiki_trgm) VALUES ('optimize')")
    db.commit()
    log("wiki_trgm built")
    copy("wiki_aliases", "CREATE TABLE wiki_aliases (norm TEXT, page_id INTEGER, PRIMARY KEY (norm, page_id)) WITHOUT ROWID",
         "SELECT norm, page_id FROM wiki_aliases WHERE norm IS NOT NULL ORDER BY norm COLLATE \"C\", page_id", 2, ignore=True)
    copy("wiki_sites", "CREATE TABLE wiki_sites (page_id INTEGER PRIMARY KEY, url TEXT, domain TEXT, box TEXT, "
         "tranco_rank INTEGER, history_visits INTEGER)",
         "SELECT page_id, url, domain, box, tranco_rank, history_visits FROM wiki_sites ORDER BY page_id", 6)
    index("CREATE INDEX wiki_sites_domain ON wiki_sites (domain)")
    copy("wiki_facts", "CREATE TABLE wiki_facts (title TEXT, fact TEXT, value TEXT, unit TEXT, precision INTEGER, at TEXT)",
         "SELECT title, fact, value, unit, precision, at FROM wiki_facts ORDER BY title COLLATE \"C\", fact, value, at", 6)
    index("CREATE INDEX wiki_facts_title ON wiki_facts (title, fact)")
    # vord: the value's rank in Postgres's en_US order — PROFILES_SQL picks the shortest handle,
    # then the first in that order (MahindraRise vs mahindrarise)
    copy("wiki_links", "CREATE TABLE wiki_links (platform TEXT, qid TEXT, value TEXT, url TEXT, name TEXT, description TEXT, "
         "wiki_title TEXT, sitelinks INTEGER, key TEXT, hkey TEXT, wkey TEXT, skey TEXT, vord INTEGER)",
         "SELECT platform, qid, value, url, name, description, wiki_title, sitelinks, key, hkey, wkey, skey, "
         "dense_rank() OVER (ORDER BY value) FROM wiki_links ORDER BY qid COLLATE \"C\", platform, value, url", 13)
    index("CREATE INDEX wiki_links_key ON wiki_links (platform, key) WHERE key IS NOT NULL",
          "CREATE INDEX wiki_links_hkey ON wiki_links (platform, hkey) WHERE hkey IS NOT NULL",
          "CREATE INDEX wiki_links_wkey ON wiki_links (platform, wkey) WHERE wkey IS NOT NULL",
          "CREATE INDEX wiki_links_skey ON wiki_links (platform, skey) WHERE skey IS NOT NULL",
          "CREATE INDEX wiki_links_title ON wiki_links (wiki_title) WHERE wiki_title IS NOT NULL")
    copy("wiki_next", "CREATE TABLE wiki_next (title TEXT, next_title TEXT, clicks INTEGER)",
         "SELECT title, next_title, clicks FROM wiki_next ORDER BY title COLLATE \"C\", next_title COLLATE \"C\"", 3)
    index("CREATE INDEX wiki_next_title ON wiki_next (title)")

# ---------------------------------------------------------------- nav
if "nav" in GROUPS:
    copy("nav_sites", "CREATE TABLE nav_sites (host TEXT, key TEXT, rank INTEGER, domain TEXT)",
         "SELECT host, key, rank, domain FROM nav_sites ORDER BY key COLLATE \"C\", rank, host COLLATE \"C\"", 4)
    index("CREATE INDEX nav_sites_key ON nav_sites (key, rank)", "CREATE INDEX nav_sites_host ON nav_sites (host)")
    copy("host_nav", "CREATE TABLE host_nav (key TEXT, url TEXT, title TEXT, description TEXT, rank INTEGER)",
         "SELECT key, url, title, description, rank FROM host_nav ORDER BY key COLLATE \"C\", rank, url COLLATE \"C\"", 5)
    index("CREATE INDEX host_nav_key ON host_nav (key)")
    copy("serve_sites", "CREATE TABLE serve_sites (domain TEXT PRIMARY KEY, tranco_rank INTEGER, cc_hc_pos INTEGER, "
         "history_visits INTEGER) WITHOUT ROWID",
         "SELECT domain, tranco_rank, cc_hc_pos, history_visits FROM serve_sites ORDER BY domain COLLATE \"C\"", 4)
    # rid: an INTEGER PRIMARY KEY, so the FTS5 rowids below survive VACUUM
    copy("docs_meta", "CREATE TABLE docs_meta (rid INTEGER PRIMARY KEY, domain TEXT, url TEXT, title TEXT, description TEXT, "
         "label TEXT, fetched_at TEXT)",
         "SELECT row_number() OVER (ORDER BY domain COLLATE \"C\", crawl COLLATE \"C\"), domain, url, title, description, "
         "split_part(domain, '.', 1), fetched_at::text FROM docs WHERE error IS NULL ORDER BY 1", 7)
    index("CREATE INDEX docs_meta_label ON docs_meta (label)", "CREATE INDEX docs_meta_domain ON docs_meta (domain)")
    # FUZZY_SITE_SQL (pg_trgm on the label) and SELF_NAMED_SQL (label LIKE '%x%')
    db.executescript("""DROP TABLE IF EXISTS label_trgm; DROP TABLE IF EXISTS label_sub;
      CREATE VIRTUAL TABLE label_trgm USING fts5(t, content='', contentless_delete=1, detail='none', tokenize='trigram');
      CREATE VIRTUAL TABLE label_sub USING fts5(t, content='', contentless_delete=1, tokenize='trigram');""")
    lab = db.execute("SELECT rid, label FROM docs_meta").fetchall()
    db.executemany("INSERT INTO label_trgm (rowid, t) VALUES (?, ?)", [(i, trgm.padded(l)) for i, l in lab])
    db.executemany("INSERT INTO label_sub (rowid, t) VALUES (?, ?)", lab)
    db.commit()
    log(f"label_trgm, label_sub: {len(lab)} labels")
    if "site_quality" in HAS:
        # source: jev | ut1 | jev+ut1 (importer/junk.py). Jev's verdicts never ship (ADR-065).
        copy("site_junk", "CREATE TABLE site_junk (domain TEXT PRIMARY KEY, source TEXT) WITHOUT ROWID",
             "SELECT domain, source FROM site_quality WHERE junk" + (" AND source IN ('ut1', 'jev+ut1')" if NO_JEV else "")
             + " ORDER BY domain COLLATE \"C\"", 2)

# ---------------------------------------------------------------- kinds
if "kinds" in GROUPS:
    copy("kind_keys", "CREATE TABLE kind_keys (key TEXT PRIMARY KEY, n INTEGER, nwords INTEGER) WITHOUT ROWID",
         "SELECT key, n, coalesce(array_length(words, 1), 0) FROM kind_keys ORDER BY key COLLATE \"C\"", 3)
    copy("kind_key_words", "CREATE TABLE kind_key_words (word TEXT, key TEXT, PRIMARY KEY (word, key)) WITHOUT ROWID",
         "SELECT DISTINCT unnest(words), key FROM kind_keys ORDER BY 1, 2", 2, ignore=True)
    copy("kind_members", "CREATE TABLE kind_members (key TEXT, domain TEXT, votes INTEGER, jev REAL, lead REAL)",
         "SELECT key, domain, votes, jev, lead FROM kind_members ORDER BY key COLLATE \"C\", domain COLLATE \"C\"", 5,
         f32_at(3, 4))
    index("CREATE INDEX kind_members_key ON kind_members (key)")
    copy("kind_docs", "CREATE TABLE kind_docs (domain TEXT PRIMARY KEY, url TEXT, title TEXT, descr TEXT, tranco_rank INTEGER, "
         "cc_hc_pos INTEGER, sitelinks INTEGER, country TEXT) WITHOUT ROWID",
         "SELECT domain, url, title, descr, tranco_rank, cc_hc_pos, sitelinks, country FROM kind_docs ORDER BY domain COLLATE \"C\"", 8)
    copy("kind_vecs", "CREATE TABLE kind_vecs (key TEXT PRIMARY KEY, v BLOB) WITHOUT ROWID",
         "SELECT key, v FROM kind_vecs ORDER BY key COLLATE \"C\"", 2,
         lambda r: (r[0], struct.pack(f"{len(r[1])}f", *r[1])))

# ---------------------------------------------------------------- misc
if "misc" in GROUPS:
    copy("stock_keys", "CREATE TABLE stock_keys (key TEXT, ticker TEXT)",
         "SELECT key, ticker FROM stock_keys ORDER BY key COLLATE \"C\", ticker COLLATE \"C\"", 2)
    index("CREATE INDEX stock_keys_key ON stock_keys (key)")
    copy("stock_tickers", "CREATE TABLE stock_tickers (ticker TEXT PRIMARY KEY, exchange TEXT, name TEXT, cik INTEGER, "
         "qid TEXT, label TEXT, site TEXT, sitelinks INTEGER, ir TEXT, word INTEGER, tord INTEGER) WITHOUT ROWID",
         "SELECT ticker, exchange, name, cik, qid, label, site, sitelinks, to_jsonb(t) ->> 'ir', "
         "coalesce((to_jsonb(t) ->> 'word')::boolean, false)::int, dense_rank() OVER (ORDER BY ticker) "
         "FROM stock_tickers t ORDER BY ticker COLLATE \"C\"", 11)
    copy("subreddits", "CREATE TABLE subreddits (key TEXT PRIMARY KEY, name TEXT, title TEXT, description TEXT, "
         "subscribers INTEGER) WITHOUT ROWID",
         "SELECT key, name, title, description, subscribers FROM subreddits ORDER BY key COLLATE \"C\"", 5)
    copy("youtube_channels", "CREATE TABLE youtube_channels (channel_id TEXT, handle TEXT, name TEXT, description TEXT, "
         "wiki_title TEXT, followers INTEGER, sitelinks INTEGER, key TEXT, hkey TEXT, wkey TEXT, skey TEXT)",
         "SELECT channel_id, handle, name, description, wiki_title, followers, sitelinks, key, hkey, wkey, skey "
         "FROM youtube_channels ORDER BY channel_id COLLATE \"C\"", 11)
    index("CREATE INDEX yt_key ON youtube_channels (key)", "CREATE INDEX yt_hkey ON youtube_channels (hkey)",
          "CREATE INDEX yt_wkey ON youtube_channels (wkey)", "CREATE INDEX yt_skey ON youtube_channels (skey)")
    if "card_images" not in HAS:
        log("card_images: NOT on this database — skipped (UNVERIFIED)")

db.execute("ANALYZE")
db.commit()
log("ANALYZE done; VACUUM")
db.execute("VACUUM")
db.close()
log(f"done: {os.path.getsize(OUT) / 1e6:.0f} MB")
db = sqlite3.connect(OUT)
sizes = db.execute("SELECT name, sum(pgsize) FROM dbstat GROUP BY name ORDER BY 2 DESC").fetchall()
for name, sz in sizes:
    print(f"  size {name}\t{sz / 1e6:.1f} MB")
