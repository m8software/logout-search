#!/usr/bin/env python3
"""Release export PROTOTYPE (gate T5, docs/internal/SEARCH-HOME-ENGINE-PLAN.md, ADR-063).

Postgres build/serve database -> one data release in the shipped layout:

    <release>/text/wiki/            Tantivy (Wikipedia; the T1 parity recipe)
    <release>/text/sites/sites/     Tantivy (docs: one row per site's front page)
    <release>/text/sites/pages/     Tantivy (page_docs: pages inside the top sites)
    <release>/text/sites/anchors/   Tantivy (anchor_docs: what other sites call a site)
    <release>/vectors/raw/          raw vectors (T3 turns them into usearch indexes)
    <release>/lookup-*.sqlite       naive data-only copies of the lookup tables, one file per
                                    licence (T4 owns the design): wiki, sites, places-osm,
                                    phrases, unsafe-ut1, card-images
    <release>/manifest.json         files, sizes, sha256, release id, engine compatibility
    <release>/manifest.json.sig     ed25519 signature (TEST key only in the prototype)

Each stage is its own process and prints one JSON line (rows, seconds, bytes, peak RSS),
so a runner can time and size every artefact on its own.

    export.py wiki     OUT STOPFILE
    export.py sites    OUT STOPFILE
    export.py vectors  OUT            (wiki_bit from $PG_SEARCH_DSN, kind vectors from $PG_DSN)
    export.py profile  SQLFILE    (release-profile.sql, on the throwaway clone only)
    export.py guard               (refuses unless the source is release-profiled; every stage runs it)
    export.py lookup   REL        (one SQLite file per licence: LOOKUP_FILES / LICENCES)
    export.py catalog  OUT.json       (Postgres table/index sizes, for T4's reference)
    export.py removed  OUT.json       (what the rules keep out, with sizes)
    export.py manifest REL RELEASE_ID STOPFILE PRIVKEY.pem
    export.py verify   REL PUBKEY.pem
    export.py zstd     REL OUT.json   (compression test per artefact; nothing is written)

Ranking parity is T2's job; this file mirrors search_api.py's text expressions only so the
sizes are real. Rules it keeps: no readable page text (ADR-059: page bodies are indexed
as page_words(), never stored), no browsing history (history_visits / hist dropped),
no subreddits.
"""
import hashlib
import json
import os
import resource
import sqlite3
import struct
import sys
import time

try:
    import psycopg
except ImportError:       # a machine with only a staged file (buildbox): registry --from-stage needs no Postgres
    class _NoPsycopg:
        Error = Exception

        @staticmethod
        def connect(*a, **k):
            sys.exit("psycopg is not installed: this machine can only build from a staged file (--from-stage)")
    psycopg = _NoPsycopg()

PG_DSN = os.environ.get("PG_DSN", "host=release-proto-pg dbname=serve user=search")
PG_SEARCH_DSN = os.environ.get("PG_SEARCH_DSN", "host=release-proto-pg dbname=search user=search")
HEAP = int(os.environ.get("TANTIVY_HEAP", 2_000_000_000))
THREADS = int(os.environ.get("TANTIVY_THREADS", 3))
TANTIVY_VERSION = "0.26.2"
ANALYZER = "en_pg"
FORMAT_VERSION = 1          # bump when a schema below changes; the engine refuses other values
APPLICATION_ID = 0x6C6F7331  # "los1": PRAGMA application_id of every SQLite file of a release

# --------------------------------------------------------------------------- helpers


def peak_rss_mb() -> int:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024


def cgroup_peak_mb():
    """The container's memory peak (cgroup v2, includes page cache) or None."""
    for p in ("/sys/fs/cgroup/memory.peak", "/sys/fs/cgroup/memory/memory.max_usage_in_bytes"):
        try:
            return int(open(p).read().split()[0]) // 2**20
        except (OSError, ValueError):
            pass
    return None


def tree_bytes(p: str) -> int:
    if os.path.isfile(p):
        return os.path.getsize(p)
    return sum(os.path.getsize(os.path.join(d, f)) for d, _, fs in os.walk(p) for f in fs)


def report(stage: str, t0: float, **kw) -> None:
    kw.update(stage=stage, seconds=round(time.time() - t0, 1), peak_rss_mb=peak_rss_mb(),
              cgroup_peak_mb=cgroup_peak_mb())
    print(json.dumps(kw), flush=True)


# The clone has primarycache=metadata: index scans are one random disk read per row. Every export
# query reads sequentially and sorts (and never starts a scan mid-table, so the order is stable).
SEQ_ONLY = ("SET synchronize_seqscans = off; SET enable_indexscan = off; SET enable_bitmapscan = off; "
            "SET enable_indexonlyscan = off; SET work_mem = '256MB'")


def stamp(db, role: str, release: str, extra: dict = None) -> None:
    """Version stamp of a SQLite file (release format v1, rule 8): application_id, user_version =
    format, and a meta table with the role and the release that BUILT the file. No timestamps:
    the manifest carries the current release, so an unchanged file keeps its bytes."""
    db.execute(f"PRAGMA application_id = {APPLICATION_ID}")
    db.execute(f"PRAGMA user_version = {FORMAT_VERSION}")
    db.execute("DROP TABLE IF EXISTS meta")
    db.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT) WITHOUT ROWID")
    rows = {"role": role, "format": str(FORMAT_VERSION), "built_release": release}
    rows.update({k: str(v) for k, v in (extra or {}).items()})
    db.executemany("INSERT INTO meta VALUES (?, ?)", sorted(rows.items()))


def stream(dsn: str, sql: str, itersize: int = 20000):
    """Rows of sql through a server-side cursor (constant memory)."""
    with psycopg.connect(dsn) as c:
        c.execute(SEQ_ONLY)
        with c.cursor(name="x") as cur:
            cur.itersize = itersize
            cur.execute(sql)
            yield from cur


# --------------------------------------------------------------------------- Tantivy

def analyzer(stopfile: str):
    """Postgres 'english' parity (T1): simple tokenizer, lowercase, english.stop, snowball."""
    import tantivy
    words = [w.strip() for w in open(stopfile, encoding="utf-8") if w.strip()]
    return (tantivy.TextAnalyzerBuilder(tantivy.Tokenizer.simple())
            .filter(tantivy.Filter.lowercase())
            .filter(tantivy.Filter.custom_stopword(words))
            .filter(tantivy.Filter.stemmer("english"))
            .build())


def segments(path: str) -> int:
    return len(json.load(open(os.path.join(path, "meta.json"))).get("segments", []))


def build_index(path: str, stopfile: str, schema, rows, make_doc) -> dict:
    import tantivy
    os.makedirs(path, exist_ok=True)
    if os.listdir(path):
        sys.exit(f"{path} is not empty — remove it first (an index is never built on top of another)")
    idx = tantivy.Index(schema, path=path)
    idx.register_tokenizer(ANALYZER, analyzer(stopfile))
    w = idx.writer(heap_size=HEAP, num_threads=THREADS)
    n = 0
    for r in rows:
        w.add_document(make_doc(r))
        n += 1
    w.commit()
    w.wait_merging_threads()
    return {"rows": n, "bytes": tree_bytes(path), "segments": segments(path)}


# Field specs: the manifest copies them, so an engine can refuse an index it was not built for.
WIKI_FIELDS = {
    "page_id": "u64 stored indexed fast",
    "title": "text en_pg positions stored",
    "body": "text en_pg freq (title + aliases + infobox + headings + lead), not stored",
    "snippet": "bytes stored (lead[:300] utf-8)",
}
SITES_FIELDS = {
    "url": "string stored", "domain": "string indexed stored",
    "title": "text en_pg positions stored", "description": "bytes stored (<=300 chars)",
    "head": "text en_pg freq (domain words + title + h1) — docs_head_bm25",
    "body": "text en_pg freq (domain words + title + description + h1 + page_words(body)) — docs_bm25, not stored",
    "tranco_rank": "u64 fast (0 = none)", "cc_hc_pos": "u64 fast (0 = none)",
}
PAGES_FIELDS = {
    "url": "string stored", "domain": "string indexed fast stored",
    "title": "text en_pg positions stored", "description": "bytes stored (<=300 chars)",
    "body": "text en_pg freq (title + description + h1 + page_words(body)) — page_docs_bm25, not stored",
}
ANCHORS_FIELDS = {
    "domain": "string indexed stored", "sources": "u64 stored fast",
    "body": "text en_pg freq (anchor text) — anchor_docs_bm25, not stored",
}
# The shippable wiki index (tantivy-structure build): nothing displayable is stored, the registry's
# doc_id is the only way back to titles and snippets (release format rule 3).
WIKI_FIELDS_STORE_NOTHING = {
    "doc_id": "u64 fast indexed (registry doc_id: type 1 << 28 | seq)",
    "title": "text en_pg positions, not stored",
    "body": "text en_pg freq (title + aliases + infobox + headings + lead), not stored",
}
# Never stored in a shipped text index (ADR-059): the guard below checks every schema.
PAGE_TEXT_FIELDS = {"body"}


def check_no_page_text(name: str, fields: dict) -> None:
    for f in PAGE_TEXT_FIELDS & fields.keys():
        if "not stored" not in fields[f]:
            sys.exit(f"ADR-059: {name}.{f} would ship page text — refusing")


def cmd_wiki(out: str, stopfile: str) -> None:
    import tantivy
    t0 = time.time()
    check_source()
    sb = tantivy.SchemaBuilder()
    sb.add_integer_field("page_id", stored=True, indexed=True, fast=True)
    sb.add_text_field("title", stored=True, tokenizer_name=ANALYZER, index_option="position")
    sb.add_text_field("body", stored=False, tokenizer_name=ANALYZER, index_option="freq")
    sb.add_bytes_field("snippet", stored=True, indexed=False)
    check_no_page_text("wiki", WIKI_FIELDS)

    def doc(r):
        pid, title, aliases, infobox, headings, lead = r
        d = tantivy.Document()
        d.add_integer("page_id", pid)
        d.add_text("title", title or "")
        d.add_text("body", " ".join(x for x in (title, aliases, infobox, headings, lead) if x))
        d.add_bytes("snippet", (lead or "")[:300].encode("utf-8"))
        return d

    rows = stream(PG_DSN, "SELECT page_id, title, aliases, infobox, headings, lead FROM wiki_docs ORDER BY page_id")
    res = build_index(out, stopfile, sb.build(), rows, doc)
    report("wiki", t0, path=out, heap=HEAP, threads=THREADS, **res)


# search_api.py HEAD_EXPR / TEXT_FORMS, with page_words() over the page body (ADR-059).
PAGE_WORDS_SQL = """
CREATE OR REPLACE FUNCTION pg_temp.page_words(t text) RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
  SELECT coalesce(string_agg(w, ' ' ORDER BY w COLLATE "C"), '')
  FROM (SELECT lower(regexp_replace(x, '^\\W+|\\W+$', '', 'g')) AS w
          FROM regexp_split_to_table(t, '\\s+') AS x) s
  WHERE w <> ''
$$"""
def _block_words() -> list:
    """The adult/hate/drug word list of serve/phrases.sql (one source of truth, G3 D9)."""
    import re
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.environ.get("PHRASES_SQL") or next(
        p for p in (os.path.join(here, "..", "serve", "phrases.sql"), os.path.join(here, "phrases.sql"))
        if os.path.exists(p))
    m = re.search(r"ARRAY\[(.*?)\], '\|'", open(path, encoding="utf-8").read(), re.S)
    if not m:
        sys.exit(f"no word list found in {path} — refusing to export unfiltered anchor text")
    words = re.findall(r"'([^']*)'", m.group(1))
    if len(words) < 40:
        sys.exit(f"word list in {path} has only {len(words)} entries — refusing")
    return words


BLOCK_WORDS = _block_words()
BLOCK_RE = r"\m(" + "|".join(BLOCK_WORDS) + r")\M"           # Postgres ARE, as phrases.sql uses it
BLOCK_SQL = BLOCK_RE.replace("'", "''")
# Domains whose descriptions must not be shown (nosnippet / noarchive / TDM opt-out / removal on
# request, G3 D4c). The build database records none of those signals today (gap, see the audit);
# this file is the hook: one domain per line.
OPTOUT_FILE = os.environ.get("OPTOUT_FILE", "")


def optout_domains() -> frozenset:
    if not OPTOUT_FILE:
        return frozenset()
    return frozenset(l.strip().lower() for l in open(OPTOUT_FILE, encoding="utf-8") if l.strip() and not l.startswith("#"))


SITES_SQL = """
SELECT d.url, d.domain, d.title, left(d.description, 300),
       regexp_replace(d.domain, '[.-]+', ' ', 'g') || ' ' || coalesce(d.title, '') || ' ' || coalesce(d.h1, ''),
       regexp_replace(d.domain, '[.-]+', ' ', 'g') || ' ' || coalesce(d.title, '') || ' ' ||
         coalesce(d.description, '') || ' ' || coalesce(d.h1, '') || ' ' || coalesce(pg_temp.page_words(d.body), ''),
       s.tranco_rank, s.cc_hc_pos
FROM docs d LEFT JOIN serve_sites s ON s.domain = d.domain
WHERE d.error IS NULL ORDER BY d.domain COLLATE "C", d.crawl COLLATE "C"
"""
PAGES_SQL = """
SELECT url, domain, title, left(description, 300),
       coalesce(title, '') || ' ' || coalesce(description, '') || ' ' || coalesce(h1, '') || ' ' ||
         coalesce(pg_temp.page_words(body), '')
FROM page_docs WHERE error IS NULL ORDER BY url COLLATE "C", crawl COLLATE "C"
"""
# Anchor text loses every word on the block list (slurs, porn spam: G3 D9).
ANCHORS_SQL = (f"SELECT domain, sources, body FROM (SELECT domain, sources, "
               f"regexp_replace(body, '{BLOCK_SQL}', ' ', 'gi') AS body FROM anchor_docs) a WHERE body ~ '\\w' "
               f"ORDER BY domain COLLATE \"C\"")


def stream_with_page_words(sql: str):
    with psycopg.connect(PG_DSN) as c:
        c.execute(SEQ_ONLY)
        c.execute(PAGE_WORDS_SQL)
        with c.cursor(name="x") as cur:
            cur.itersize = 5000
            cur.execute(sql)
            yield from cur


def cmd_sites(out: str, stopfile: str) -> None:
    import tantivy
    t_all = time.time()
    check_source()
    results = {}
    for name, fields in (("sites", SITES_FIELDS), ("pages", PAGES_FIELDS), ("anchors", ANCHORS_FIELDS)):
        check_no_page_text(name, fields)
    optout = optout_domains()

    # sites
    t0 = time.time()
    sb = tantivy.SchemaBuilder()
    sb.add_text_field("url", stored=True, tokenizer_name="raw", index_option="basic")
    sb.add_text_field("domain", stored=True, tokenizer_name="raw", index_option="basic")
    sb.add_text_field("title", stored=True, tokenizer_name=ANALYZER, index_option="position")
    sb.add_bytes_field("description", stored=True, indexed=False)
    sb.add_text_field("head", stored=False, tokenizer_name=ANALYZER, index_option="freq")
    sb.add_text_field("body", stored=False, tokenizer_name=ANALYZER, index_option="freq")
    sb.add_unsigned_field("tranco_rank", stored=False, indexed=False, fast=True)
    sb.add_unsigned_field("cc_hc_pos", stored=False, indexed=False, fast=True)

    def site_doc(r):
        url, domain, title, descr, head, body, tranco, cc = r
        d = tantivy.Document()
        d.add_text("url", url or "")
        d.add_text("domain", domain)
        d.add_text("title", title or "")
        d.add_bytes("description", ("" if domain in optout else descr or "").encode("utf-8"))
        d.add_text("head", head)
        d.add_text("body", body)
        d.add_unsigned("tranco_rank", tranco or 0)
        d.add_unsigned("cc_hc_pos", cc or 0)
        return d

    results["sites"] = build_index(os.path.join(out, "sites"), stopfile, sb.build(),
                                   stream_with_page_words(SITES_SQL), site_doc)
    results["sites"]["seconds"] = round(time.time() - t0, 1)

    # pages
    t0 = time.time()
    sb = tantivy.SchemaBuilder()
    sb.add_text_field("url", stored=True, tokenizer_name="raw", index_option="basic")
    sb.add_text_field("domain", stored=True, tokenizer_name="raw", index_option="basic", fast=True)
    sb.add_text_field("title", stored=True, tokenizer_name=ANALYZER, index_option="position")
    sb.add_bytes_field("description", stored=True, indexed=False)
    sb.add_text_field("body", stored=False, tokenizer_name=ANALYZER, index_option="freq")

    def page_doc(r):
        url, domain, title, descr, body = r
        d = tantivy.Document()
        d.add_text("url", url)
        d.add_text("domain", domain or "")
        d.add_text("title", title or "")
        d.add_bytes("description", ("" if (domain or "") in optout else descr or "").encode("utf-8"))
        d.add_text("body", body)
        return d

    results["pages"] = build_index(os.path.join(out, "pages"), stopfile, sb.build(),
                                   stream_with_page_words(PAGES_SQL), page_doc)
    results["pages"]["seconds"] = round(time.time() - t0, 1)

    # anchors
    t0 = time.time()
    sb = tantivy.SchemaBuilder()
    sb.add_text_field("domain", stored=True, tokenizer_name="raw", index_option="basic")
    sb.add_unsigned_field("sources", stored=True, indexed=False, fast=True)
    sb.add_text_field("body", stored=False, tokenizer_name=ANALYZER, index_option="freq")

    def anchor_doc(r):
        domain, sources, body = r
        d = tantivy.Document()
        d.add_text("domain", domain)
        d.add_unsigned("sources", sources or 0)
        d.add_text("body", body or "")
        return d

    results["anchors"] = build_index(os.path.join(out, "anchors"), stopfile, sb.build(),
                                     stream(PG_DSN, ANCHORS_SQL), anchor_doc)
    results["anchors"]["seconds"] = round(time.time() - t0, 1)
    report("sites", t_all, path=out, heap=HEAP, threads=THREADS, parts=results, bytes=tree_bytes(out),
           optout_domains=len(optout), block_words=len(BLOCK_WORDS))


# --------------------------------------------------------------------------- vectors (raw, for T3)

_rows_out: list = []


def yield_rows(buf: bytearray, final: bool = False) -> int:
    """Parse whole rows from a COPY binary buffer into _rows_out; return bytes consumed."""
    pos = 0
    if buf[:11] == b"PGCOPY\n\xff\r\n\x00":
        ext = struct.unpack_from(">i", buf, 15)[0]
        pos = 19 + ext
    n = len(buf)
    while pos + 2 <= n:
        nf = struct.unpack_from(">h", buf, pos)[0]
        if nf == -1:
            return n
        p = pos + 2
        cols = []
        ok = True
        for _ in range(nf):
            if p + 4 > n:
                ok = False
                break
            ln = struct.unpack_from(">i", buf, p)[0]
            p += 4
            if ln == -1:
                cols.append(None)
                continue
            if p + ln > n:
                ok = False
                break
            cols.append(bytes(buf[p:p + ln]))
            p += ln
        if not ok:
            break
        _rows_out.append(cols)
        pos = p
    if final and pos < n and struct.unpack_from(">h", buf, pos)[0] != -1:
        raise RuntimeError("COPY binary stream ended mid-row")
    return pos


def export_bits(dsn: str, sql: str, out_prefix: str, dims: int) -> dict:
    """id bigint + bit(dims) -> OUT.ids (little-endian u64) + OUT.bits (N x dims/8 bytes, packed MSB-first)."""
    nbytes = dims // 8
    n = 0
    with open(out_prefix + ".ids.u64", "wb") as fi, open(out_prefix + ".bits", "wb") as fb, \
            psycopg.connect(dsn) as c, c.cursor() as cur:
        with cur.copy(f"COPY ({sql}) TO STDOUT (FORMAT binary)") as cp:
            buf = bytearray()
            for chunk in cp:
                buf += chunk
                if len(buf) < 1 << 22:
                    continue
                n += _flush_bits(buf, fi, fb, nbytes, dims)
            n += _flush_bits(buf, fi, fb, nbytes, dims, final=True)
    return {"rows": n, "dims": dims, "bytes": os.path.getsize(out_prefix + ".ids.u64") + os.path.getsize(out_prefix + ".bits")}


def _flush_bits(buf, fi, fb, nbytes, dims, final=False) -> int:
    _rows_out.clear()
    pos = yield_rows(buf, final)
    del buf[:pos]
    ids, bits = bytearray(), bytearray()
    for cols in _rows_out:
        pid = struct.unpack(">q", cols[0])[0]
        b = cols[1]                                     # bit_send: int4 bit length + packed bytes
        if struct.unpack_from(">i", b, 0)[0] != dims or len(b) != 4 + nbytes:
            raise RuntimeError(f"id {pid}: expected bit({dims})")
        ids += struct.pack("<Q", pid)
        bits += b[4:]
    fi.write(ids)
    fb.write(bits)
    k = len(_rows_out)
    _rows_out.clear()
    return k


def export_halfvec(dsn: str, sql: str, out_prefix: str) -> dict:
    """key text + halfvec -> OUT.keys.txt (one per line) + OUT.f16 (N x dims little-endian float16)."""
    import numpy as np
    n, dims = 0, None
    with open(out_prefix + ".keys.txt", "w", encoding="utf-8") as fk, open(out_prefix + ".f16", "wb") as fv:
        for key, v in stream(dsn, sql):
            arr = np.array([float(x) for x in v.strip("[]{}").split(",")], dtype=np.float16)
            dims = dims or len(arr)
            fk.write(key.replace("\n", " ") + "\n")
            fv.write(arr.astype("<f2").tobytes())
            n += 1
    return {"rows": n, "dims": dims,
            "bytes": os.path.getsize(out_prefix + ".keys.txt") + os.path.getsize(out_prefix + ".f16")}


SEARCH_DB_TABLES = ["wiki_bit"]          # everything the export reads from PG_SEARCH_DSN


def cmd_vectors(out: str) -> None:
    t0 = time.time()
    check_source()
    check_history(PG_SEARCH_DSN, SEARCH_DB_TABLES)
    os.makedirs(out, exist_ok=True)
    parts = {}
    t = time.time()
    parts["wiki_bit"] = export_bits(PG_SEARCH_DSN, "SELECT id, bit_send(b) FROM wiki_bit ORDER BY id",
                                    os.path.join(out, "wiki_bit"), 768)
    parts["wiki_bit"].update(seconds=round(time.time() - t, 1), source="search.wiki_bit (nomic-embed-text-v1.5, 1 bit, 768 d)")
    # Spot check: 5 rows re-read as text must match the packed bytes.
    import random
    ids = open(os.path.join(out, "wiki_bit.ids.u64"), "rb").read()
    bits = open(os.path.join(out, "wiki_bit.bits"), "rb")
    total = len(ids) // 8
    checked = 0
    with psycopg.connect(PG_SEARCH_DSN) as c:
        for i in random.sample(range(total), 5):
            pid = struct.unpack_from("<Q", ids, i * 8)[0]
            txt = c.execute("SELECT b::text FROM wiki_bit WHERE id = %s", (pid,)).fetchone()[0]
            bits.seek(i * 96)
            mine = "".join(f"{x:08b}" for x in bits.read(96))
            if mine != txt:
                sys.exit(f"wiki_bit spot check failed at id {pid}")
            checked += 1
    parts["wiki_bit"]["spot_checked"] = checked
    # kind_vecs / kind_site_vec are NOT exported: the kinds tier is Jev-filtered (ADR-065).
    report("vectors", t0, path=out, parts=parts, bytes=tree_bytes(out))


# --------------------------------------------------------------------------- lookups (naive, for T4)

# Every lookup file: table -> SELECT. One file per licence (G3 D5); the tag each file gets
# in the manifest is LICENCES below. Never selected: browsing history (history_visits, hist),
# anything Jev produced or filtered (ADR-065: the whole kinds tier, site_quality's Jev rows
# and probabilities, d2q_docs), WDC listings or anything derived only from them (D3), the
# build-only tables (D10). Data only, no secondary indexes: T4 designs those.
LEAD_CHARS = int(os.environ.get("LEAD_CHARS", 1000))     # cards never show more
LOOKUP_FILES = {
    # OpenStreetMap only: ODbL, its own file and attribution (D6). WDC rows never leave (D3).
    "lookup-places-osm.sqlite": {
        "places": "SELECT osm, name, kind, brand, lat, lon, street, city, hours, website, phone "
                  "FROM places WHERE osm NOT LIKE 'wdc:%'",
    },
    # Wikipedia (CC BY-SA 4.0) + Wikidata / clickstream (CC0) + SEC tickers (public domain).
    "lookup-wiki.sqlite": {
        "wiki_aliases": "SELECT norm, page_id FROM wiki_aliases",
        "wiki_sites": "SELECT page_id, url, domain, box, tranco_rank FROM wiki_sites",
        "wiki_links": "SELECT platform, qid, value, url, name, description, wiki_title, sitelinks, key, hkey, wkey, skey FROM wiki_links",
        "wiki_facts": "SELECT title, fact, value, unit, precision, at FROM wiki_facts",
        "wiki_next": "SELECT title, next_title, clicks FROM wiki_next",
        # the full title stays; the lead is cut for cards (the text index keeps aliases/infobox)
        "wiki_lead": f"SELECT page_id, title, left(lead, {LEAD_CHARS}) AS lead FROM wiki_docs",
        "youtube_channels": "SELECT channel_id, handle, name, description, wiki_title, followers, sitelinks, key, hkey, wkey, skey FROM youtube_channels",
        "stock_tickers": "SELECT ticker, exchange, name, cik, qid, label, site, sitelinks, ir, word FROM stock_tickers",
        "stock_keys": "SELECT key, ticker FROM stock_keys",
    },
    # Card pictures: pictures.sqlite, keyed by doc_id (release_v1.py pictures).
    # AmazonQAC phrase completions: CDLA-Permissive-2.0.
    "lookup-phrases.sqlite": {
        "suggest_phrases": "SELECT prefix, phrases, counts FROM suggest_phrases",
    },
    # UT1 unsafe lists only (CC BY-SA 4.0): the junk filter without Jev's verdicts (ADR-065).
    "lookup-unsafe-ut1.sqlite": {
        "site_unsafe": "SELECT domain, category FROM site_quality WHERE source IN ('ut1', 'jev+ut1')",
    },
    # Our own layer (CC0: rankings, suggest tables, navigation) over third-party facts: site
    # titles and descriptions (D4), Tranco / Common Crawl ranks (D2 open), anchor phrases (D9).
    "lookup-sites.sqlite": {
        # subreddit suggestions never ship: 11,085 items in serve on 2026-10-01
        "suggest_items": "SELECT id, kind, name, url, domain, wiki, pop, score, demand, descr FROM suggest_items "
                         "WHERE kind <> 'subreddit'",
        "suggest_keys": "SELECT key, id, kind, score, pos FROM suggest_keys WHERE kind <> 'subreddit'",
        "suggest_top": "SELECT prefix, ids, pos FROM suggest_top",
        "nav_sites": "SELECT host, key, rank, domain FROM nav_sites",
        "host_nav": "SELECT key, url, title, description, rank FROM host_nav",
        "serve_sites": "SELECT domain, tranco_rank, cc_hc_pos FROM serve_sites",
        "anchors": f"SELECT domain, anchor, sources FROM anchors WHERE anchor !~* '{BLOCK_SQL}'",
        # what site and anchor hits display (no page text); opt-out domains lose the description
        "site_display": "SELECT domain, url, title, description FROM docs WHERE error IS NULL",
    },
}
LICENCES = {
    "lookup-places-osm.sqlite": "ODbL-1.0 (OpenStreetMap contributors)",
    "lookup-wiki.sqlite": "CC-BY-SA-4.0 (Wikipedia) + CC0-1.0 (Wikidata, clickstream) + public domain (SEC)",
    "pictures.sqlite": "per row: PD / CC0 / CC BY / CC BY-SA (Wikimedia Commons; licence column)",
    "registry.sqlite": "CC-BY-SA-4.0 (Wikipedia titles) + CC0-1.0 (Wikidata, SEC tickers, M8 layer) + third-party site titles/URLs (G3 D4); place rows carry only the OSM id (names stay in the ODbL file)",
    "signals.bin": "CC0 (Wikimedia pageviews/sitelinks, M8 layer) + Common Crawl web-graph rank (CC ToU) in site.own_rank/cc_hc_pos and the ranks derived from them, see its header",
    "lookup-phrases.sqlite": "CDLA-Permissive-2.0 (AmazonQAC)",
    "lookup-unsafe-ut1.sqlite": "CC-BY-SA-4.0 (UT1, Universite Toulouse Capitole)",
    "kinds.sqlite": "CC-BY-SA-4.0 (combined kind-label set: Wikidata CC0, Wikipedia categories/lists CC BY-SA, UT1 CC BY-SA, DMOZ CC BY 3.0, "
                    "Homebrew BSD-2, public-apis MIT, awesome lists per list, Flathub/Debian facts); no Jev, no Tranco, no page text (D2, release_v1.py kinds)",
    "lookup-sites.sqlite": "CC0-1.0 (M8 Software's own layer); third-party titles/descriptions and Tranco/Common Crawl ranks inside (G3 D2/D4)",
    "text/wiki": "CC-BY-SA-4.0 (Wikipedia)",
    "text/sites": "CC0-1.0 (M8 Software's own layer); third-party titles/descriptions and Common Crawl anchor phrases inside (G3 D4/D9)",
    "vectors/raw/wiki_bit": "CC-BY-SA-4.0 (derived from Wikipedia text)",
    "english.stop": "PostgreSQL",
}
# Never named by any export SQL: build-only tables (D10), subreddits, and everything
# Jev produced or filtered (ADR-065) — the old kinds tier is built from Jev-kept phrases. The OPEN kinds tier ships as
# kinds.sqlite from kinds_build.py (D2): it is read from the judged open-build dump, never from these Postgres tables.
FORBIDDEN_TABLES = {"site_meta", "site_inlinks", "kw_country", "wiki_pop", "subreddits",
                    "kind_docs", "kind_members", "kind_keys", "kind_vecs", "kind_site_vec", "site_kinds",
                    "kinds_phrase_keep", "kinds_phrase_keys", "kinds_member_jev", "d2q_docs"}
# Tables that mix in rows that must not ship, and the filter their export SQL must carry.
REQUIRED_FILTERS = {"suggest_items": "kind <> 'subreddit'", "suggest_keys": "kind <> 'subreddit'",
                    "places": "osm NOT LIKE 'wdc:%'",
                    "site_quality": "source IN ('ut1', 'jev+ut1')",
                    "anchors": BLOCK_SQL, "anchor_docs": BLOCK_SQL}
# Columns never selected, not even banded: browsing history anywhere, Jev scores (ADR-065).
FORBIDDEN_COLUMNS = {"*": {"history_visits", "hist"},
                     "site_quality": {"useful", "parked", "spam", "unsafe", "junk", "source"}}


def check_sql_one(q: str, allow_tables: tuple = ()) -> None:
    """One statement against the release rules: no refused table (except `allow_tables`, which a
    caller names for one statement, with its reason), every required filter present, no forbidden
    column selected (in any form: no bands of Jev scores either, ADR-065)."""
    import re
    low = re.sub(r"'[^']*'", "''", q.lower())
    bad = (FORBIDDEN_TABLES - set(allow_tables)) & set(re.findall(r"[a-z0-9_]+", low))
    if bad:
        sys.exit(f"G3: export SQL names build-only table(s) {sorted(bad)} — refusing")
    tables = set(re.findall(r"\b(?:from|join)\s+([a-z0-9_]+)", low))
    for t in tables & REQUIRED_FILTERS.keys():
        if REQUIRED_FILTERS[t] not in q:
            sys.exit(f"G3: export of {t} lacks the filter {REQUIRED_FILTERS[t]!r} — refusing")
    sel = low.split(" from ")[0]
    cols = set(FORBIDDEN_COLUMNS["*"]).union(*(FORBIDDEN_COLUMNS.get(t, set()) for t in tables))
    for col in cols:
        if re.search(rf"(?<![.\w]){col}\b", sel):
            sys.exit(f"ADR-065/privacy: export SQL selects column {col!r} — refusing")


def check_export_sql() -> None:
    """Refuse any export SQL of this file that breaks the release rules (see check_sql_one)."""
    for q in [q for f in LOOKUP_FILES.values() for q in f.values()] + [SITES_SQL, PAGES_SQL, ANCHORS_SQL]:
        check_sql_one(q)


def check_source(dsn: str = None) -> dict:
    """The source database must be release-profiled (release-profile.sql) before any export:
    no readable page text (serve/no-page-text-check.sql = 0, ADR-059) and no browsing history
    (history_visits = 0 in every table that has the column). Refuses otherwise."""
    check_export_sql()
    dsn = dsn or PG_DSN
    here = os.path.dirname(os.path.abspath(__file__))
    check_sql = os.environ.get("NO_PAGE_TEXT_CHECK") or next(
        p for p in (os.path.join(here, "..", "serve", "no-page-text-check.sql"),
                    os.path.join(here, "no-page-text-check.sql")) if os.path.exists(p))
    q = "\n".join(l for l in open(check_sql, encoding="utf-8") if not l.lstrip().startswith("--")).strip().rstrip(";")
    with psycopg.connect(dsn) as c:
        try:
            readable = c.execute(q).fetchone()[0]
        except psycopg.Error as e:
            sys.exit(f"ADR-059: no-page-text-check failed to run ({type(e).__name__}: {e}) — "
                     "page_words() missing, release-profile.sql never ran; refusing")
        if readable:
            sys.exit(f"ADR-059: {readable} sampled page bodies are readable text — refusing")
        hist = {}
        for (t,) in c.execute("SELECT table_name FROM information_schema.columns WHERE table_schema = 'public' "
                              "AND column_name = 'history_visits'").fetchall():
            n = c.execute(f'SELECT count(*) FROM "{t}" WHERE history_visits <> 0').fetchone()[0]
            hist[t] = n
        if any(hist.values()):
            sys.exit(f"privacy: history_visits is non-zero in {hist} — refusing")
    return {"page_text_readable": 0, "history_visits_nonzero": hist}


def cmd_profile(sqlfile: str) -> None:
    """Run release-profile.sql on the THROWAWAY clone database (never on the live one)."""
    t0 = time.time()
    if "release-proto-pg" not in PG_DSN and os.environ.get("RELEASE_PROFILE_OK") != "1":
        sys.exit(f"profile only runs against the throwaway clone (release-proto-pg), not {PG_DSN!r}")
    with psycopg.connect(PG_DSN, autocommit=True) as c:
        c.execute(open(sqlfile, encoding="utf-8").read())
    report("profile", t0, sql=os.path.basename(sqlfile))


def check_history(dsn: str, tables: list) -> dict:
    """For a second source database (search, for wiki_bit): every exported table that has a
    history_visits column must hold only zeros. The page-text check does not apply there
    (no page text is read from it)."""
    with psycopg.connect(dsn) as c:
        have = {t for (t,) in c.execute(
            "SELECT table_name FROM information_schema.columns WHERE table_schema = 'public' "
            "AND column_name = 'history_visits' AND table_name = ANY(%s)", (list(tables),)).fetchall()}
        hist = {t: c.execute(f'SELECT count(*) FROM "{t}" WHERE history_visits <> 0').fetchone()[0] for t in have}
    if any(hist.values()):
        sys.exit(f"privacy: history_visits is non-zero in {hist} — refusing")
    return {"tables": list(tables), "with_history_column": hist}


def cmd_guard() -> None:
    t0 = time.time()
    res = check_source()
    res["search_db"] = check_history(PG_SEARCH_DSN, SEARCH_DB_TABLES)
    report("guard", t0, **res)


def _sqlite_value(v):
    if v is None or isinstance(v, (int, float, str, bytes)):
        return v
    if isinstance(v, (list, tuple, dict)):
        return json.dumps(v, default=str, ensure_ascii=False)
    if isinstance(v, memoryview):
        return bytes(v)
    if isinstance(v, bool):
        return int(v)
    return str(v)


def cmd_lookup(rel: str) -> None:
    t0 = time.time()
    check_source()
    files = {}
    optout = optout_domains()
    only = set(filter(None, os.environ.get("LOOKUP_ONLY", "").split(",")))
    for fname, tables in LOOKUP_FILES.items():
        if only and fname not in only:
            continue
        out = os.path.join(rel, fname)
        os.makedirs(os.path.dirname(out), exist_ok=True)
        if os.path.exists(out):
            os.remove(out)
        db = sqlite3.connect(out)
        db.execute("PRAGMA journal_mode = OFF")
        db.execute("PRAGMA synchronous = OFF")
        db.execute("PRAGMA page_size = 4096")
        db.execute("PRAGMA cache_size = -500000")     # the clone caches no file data (primarycache=metadata)
        parts = {}
        for table, sql in tables.items():
            t = time.time()
            before = os.path.getsize(out)
            n = 0
            with psycopg.connect(PG_DSN) as c:
                c.execute(SEQ_ONLY)
                with c.cursor(name="x") as cur:
                    cur.itersize = 20000
                    # a total order over the whole row: two builds of the same data = the same file
                    cur.execute(f"SELECT * FROM ({sql}) q ORDER BY q")
                    names = [d.name for d in cur.description]
                    db.execute(f"CREATE TABLE {table} ({', '.join(names)})")
                    ins = f"INSERT INTO {table} VALUES ({', '.join('?' * len(names))})"
                    batch = []
                    for r in cur:
                        if table == "site_display" and r[0] in optout:
                            r = (r[0], r[1], r[2], None)
                        batch.append(tuple(_sqlite_value(v) for v in r))
                        if len(batch) >= 20000:
                            db.executemany(ins, batch)
                            n += len(batch)
                            batch.clear()
                    if batch:
                        db.executemany(ins, batch)
                        n += len(batch)
            db.commit()
            # file growth per table: close to exact for a data-only file written table by table
            parts[table] = {"rows": n, "seconds": round(time.time() - t, 1),
                            "bytes_added": os.path.getsize(out) - before}
        stamp(db, fname.rsplit(".", 1)[0], os.environ.get("RELEASE_ID") or os.path.basename(os.path.normpath(rel)),
              {"licence": LICENCES[fname]})
        db.commit()
        db.close()
        files[fname] = {"bytes": os.path.getsize(out), "tables": parts}
    report("lookup", t0, path=rel, files=files, bytes=sum(v["bytes"] for v in files.values()),
           lead_chars=LEAD_CHARS, optout_domains=len(optout))


def cmd_removed(out: str) -> None:
    """What the rules keep out of the release, with sizes (for the audit; nothing is written to
    the release). Postgres sizes: pg_column_size sums for columns, table + indexes for tables."""
    t0 = time.time()
    res = {}
    with psycopg.connect(PG_DSN) as c:
        def one(sql):
            return c.execute(sql).fetchone()
        tables = {}
        for t in ("kind_docs", "kind_members", "kind_keys", "kind_vecs", "kind_site_vec", "site_quality", "d2q_docs",
                  "site_meta", "site_inlinks", "kw_country", "subreddits"):
            r = one(f"SELECT pg_table_size('{t}'), pg_indexes_size('{t}'), (SELECT count(*) FROM {t})") \
                if one(f"SELECT to_regclass('{t}') IS NOT NULL")[0] else None
            tables[t] = {"table_bytes": r[0], "index_bytes": r[1], "rows": r[2]} if r else None
        res["tables_refused"] = tables
        cols = {}
        for t, col in (("kind_members", "jev"), ("kind_members", "lead"), ("site_quality", "useful"),
                       ("site_quality", "parked"), ("site_quality", "spam"), ("site_quality", "unsafe"),
                       ("site_quality", "junk")):
            r = one(f"SELECT count({col}), coalesce(sum(pg_column_size({col})), 0) FROM {t}")
            cols[f"{t}.{col}"] = {"non_null": r[0], "bytes": r[1]}
        res["jev_columns"] = cols
        r = one("SELECT count(*) FILTER (WHERE source IN ('ut1', 'jev+ut1')), count(*) FILTER (WHERE source = 'jev'), "
                "count(*) FILTER (WHERE source = 'jev+ut1'), count(*) FROM site_quality")
        res["site_quality_rows"] = {"ut1_kept": r[0], "jev_only_dropped": r[1], "jev_plus_ut1_kept_as_ut1": r[2], "all": r[3]}
        r = one("SELECT count(*), coalesce(sum(pg_column_size(places.*)), 0) FROM places WHERE osm LIKE 'wdc:%'")
        res["wdc_places"] = {"rows": r[0], "row_bytes": r[1]}
        r = one("SELECT count(*), coalesce(sum(pg_column_size(suggest_items.*)), 0) FROM suggest_items WHERE kind = 'subreddit'")
        res["subreddit_suggest_items"] = {"rows": r[0], "row_bytes": r[1]}
        r = one(f"SELECT count(*), count(*) FILTER (WHERE anchor ~* '{BLOCK_SQL}') FROM anchors")
        res["anchors_block_list"] = {"rows": r[0], "dropped": r[1]}
        r = one(f"SELECT count(*), count(*) FILTER (WHERE body ~* '{BLOCK_SQL}'), "
                f"coalesce(sum(length(body) - length(regexp_replace(body, '{BLOCK_SQL}', '', 'gi'))), 0) FROM anchor_docs")
        res["anchor_docs_block_list"] = {"docs": r[0], "docs_with_blocked_words": r[1], "chars_removed": r[2]}
        r = one(f"SELECT count(*), coalesce(sum(octet_length(lead)), 0), "
                f"coalesce(sum(octet_length(left(lead, {LEAD_CHARS}))), 0) FROM wiki_docs")
        res["wiki_lead"] = {"rows": r[0], "lead_bytes_full": r[1], f"lead_bytes_cut_{LEAD_CHARS}": r[2]}
    json.dump(res, open(out, "w"), indent=1)
    report("removed", t0, path=out, **res)


def cmd_catalog(out: str) -> None:
    """Postgres sizes (heap+toast, indexes) of every table the lookups and indexes read."""
    t0 = time.time()
    res = {}
    for dsn, db in ((PG_DSN, "serve"), (PG_SEARCH_DSN, "search")):
        with psycopg.connect(dsn) as c:
            rows = c.execute("""
                SELECT c.relname, pg_table_size(c.oid), pg_indexes_size(c.oid), c.reltuples::bigint
                FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = 'public' AND c.relkind = 'r'""").fetchall()
        res[db] = {r[0]: {"table_bytes": r[1], "index_bytes": r[2], "rows_est": r[3]} for r in rows}
    json.dump(res, open(out, "w"), indent=1, sort_keys=True)
    report("catalog", t0, path=out)


# --------------------------------------------------------------------------- manifest

def sha256_file(p: str) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def licence_of(path: str) -> str:
    """Per-file licence tag (G3 decisions, docs/legal/SEARCH-DATA-LICENSES.md)."""
    for prefix in sorted(LICENCES, key=len, reverse=True):
        if path == prefix or path.startswith(prefix + "/") or path.startswith(prefix + "."):
            return LICENCES[prefix]
    sys.exit(f"no licence tag for {path} — refusing to sign a manifest with an untagged file")


def _wiki_fields(rel: str) -> dict:
    """The schema text/wiki was built with, from its release.json sidecar (`schema=store-nothing`)."""
    try:
        side = json.load(open(os.path.join(rel, "text", "wiki", "release.json")))
    except (OSError, ValueError):
        return WIKI_FIELDS
    return WIKI_FIELDS_STORE_NOTHING if side.get("schema") == "store-nothing" else WIKI_FIELDS


def _registry_types() -> dict:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from registry_idents import TYPES
    return TYPES


def _signals_fields(rel: str):
    """The field list of signals.bin, from its own header (documented in the manifest)."""
    p = os.path.join(rel, "signals.bin")
    if not os.path.exists(p):
        return None
    with open(p, "rb") as f:
        f.read(8)
        n = struct.unpack("<I", f.read(4))[0]
        h = json.loads(f.read(n))
    return [{k: x[k] for k in ("name", "type", "dtype", "count", "source", "licence", "note")} for x in h["fields"]]


def cmd_manifest(rel: str, release_id: str, stopfile: str, keyfile: str) -> None:
    from cryptography.hazmat.primitives import serialization
    t0 = time.time()
    files = []
    for d, _, fs in os.walk(rel):
        for f in sorted(fs):
            p = os.path.join(d, f)
            r = os.path.relpath(p, rel)
            if r in ("manifest.json", "manifest.json.sig") or r.startswith("_") or f.endswith(".lock"):
                continue
            files.append({"path": r, "bytes": os.path.getsize(p), "sha256": sha256_file(p), "licence": licence_of(r)})
    files.sort(key=lambda x: x["path"])
    import tantivy  # noqa: F401  (the version below is the one the indexes were written with)
    m = {
        "format": FORMAT_VERSION,
        "release": release_id,
        "prototype": True,
        "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "engine": {
            "tantivy": TANTIVY_VERSION,
            "analyzer": {"name": ANALYZER, "tokenizer": "simple", "filters": ["lowercase", "custom_stopword", "stemmer:english"],
                         "stopwords": {"file": "english.stop (Postgres)", "sha256": sha256_file(stopfile),
                                       "words": sum(1 for w in open(stopfile, encoding="utf-8") if w.strip())}},
            "text": {"wiki": _wiki_fields(rel), "sites/sites": SITES_FIELDS, "sites/pages": PAGES_FIELDS,
                     "sites/anchors": ANCHORS_FIELDS},
            "vectors": {"format": "raw (T3 replaces with usearch)",
                        "wiki_bit": "ids.u64 little-endian + bits N x 96 bytes (bit(768), MSB first); "
                                    "query model nomic-embed-text-v1.5, prefix 'search_query: ', mean pooling, sign bits"},
            "lookup": {"format": "sqlite3, data only, no secondary indexes (T4 replaces)",
                       "files": {f: sorted(t) for f, t in LOOKUP_FILES.items()}, "wiki_lead_chars": LEAD_CHARS},
            "signals": _signals_fields(rel),
            "registry": {"doc_id": "type << 28 | seq", "types": _registry_types(), "layout": "registry_idents.LAYOUT"},
            "stamps": {"sqlite_application_id": APPLICATION_ID, "sqlite_user_version": FORMAT_VERSION,
                       "sqlite_meta": "role, format, built_release", "tantivy_sidecar": "release.json",
                       "signals_header": "LOSIGNL1 + JSON (format, built_release, fields)"},
            "refused": {"tables": sorted(FORBIDDEN_TABLES), "columns": {k: sorted(v) for k, v in FORBIDDEN_COLUMNS.items()},
                        "rules": ["ADR-059 page text", "ADR-065 Jev", " subreddits", "G3 D3 WDC", "G3 D10 build-only",
                                  "G3 D9 anchor block list"]},
        },
        "files": files,
        "total_bytes": sum(f["bytes"] for f in files),
    }
    body = json.dumps(m, indent=1, sort_keys=True).encode()
    open(os.path.join(rel, "manifest.json"), "wb").write(body)
    key = serialization.load_pem_private_key(open(keyfile, "rb").read(), password=None)
    open(os.path.join(rel, "manifest.json.sig"), "wb").write(key.sign(body))
    report("manifest", t0, files=len(files), total_bytes=m["total_bytes"])


def cmd_verify(rel: str, pubfile: str) -> None:
    """Signature, then every file's size and sha256 — what the updater (T6) will do."""
    from cryptography.hazmat.primitives import serialization
    t0 = time.time()
    body = open(os.path.join(rel, "manifest.json"), "rb").read()
    pub = serialization.load_pem_public_key(open(pubfile, "rb").read())
    pub.verify(open(os.path.join(rel, "manifest.json.sig"), "rb").read(), body)   # raises if bad
    m = json.loads(body)
    bad = [f["path"] for f in m["files"]
           if os.path.getsize(os.path.join(rel, f["path"])) != f["bytes"]
           or sha256_file(os.path.join(rel, f["path"])) != f["sha256"]]
    # a tampered manifest must fail too
    try:
        pub.verify(open(os.path.join(rel, "manifest.json.sig"), "rb").read(), body + b" ")
        tamper = "ACCEPTED (bad)"
    except Exception:
        tamper = "rejected"
    report("verify", t0, signature="ok", files=len(m["files"]), mismatched=bad, tampered_manifest=tamper)
    if bad:
        sys.exit(1)


def cmd_zstd(rel: str, out: str) -> None:
    """Compressed size per artefact at zstd level 3 (all) and 19 (raw vectors + SQLite)."""
    import zstandard
    t0 = time.time()
    groups: dict = {}
    for d, _, fs in os.walk(rel):
        for f in fs:
            r = os.path.relpath(os.path.join(d, f), rel)
            if r.startswith("manifest.json"):
                continue
            g = "/".join(r.split("/")[:2]) if r.startswith("text/") else r.split("/")[0]
            groups.setdefault(g, []).append(os.path.join(d, f))
    res = {}
    for g, paths in groups.items():
        levels = (3,) if g.startswith("text/") else (3, 19)
        res[g] = {"bytes": sum(os.path.getsize(p) for p in paths), "files": len(paths)}
        for lv in levels:
            t = time.time()
            cctx = zstandard.ZstdCompressor(level=lv, threads=4)
            total = 0
            for p in paths:
                with open(p, "rb") as f:
                    for chunk in cctx.read_to_iter(f, read_size=1 << 20, write_size=1 << 20):
                        total += len(chunk)
            res[g][f"zstd{lv}_bytes"] = total
            res[g][f"zstd{lv}_seconds"] = round(time.time() - t, 1)
    json.dump(res, open(out, "w"), indent=1)
    report("zstd", t0, groups=res)


if __name__ == "__main__":
    cmd, args = sys.argv[1], sys.argv[2:]
    {"profile": cmd_profile, "guard": cmd_guard, "wiki": cmd_wiki, "sites": cmd_sites, "vectors": cmd_vectors, "lookup": cmd_lookup,
     "catalog": cmd_catalog, "removed": cmd_removed, "manifest": cmd_manifest, "verify": cmd_verify, "zstd": cmd_zstd}[cmd](*args)
