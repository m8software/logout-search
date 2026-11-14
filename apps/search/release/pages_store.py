#!/usr/bin/env python3
"""The build's inner pages as files, not a Postgres table (SEARCH-POSTGRES-EXIT.md, move 2; D1: DuckDB over Parquet).

    pages_store.py from-pg  STORE                         BRIDGE (PG_DSN), the last read of page_docs: every row as
                                                          STORE/pg-page_docs.parquet, and the domains whose homepage
                                                          said noindex (docs_policy.PG_DOMAINS_NOINDEX) as
                                                          STORE/noindex-domains.txt
    pages_store.py add-tsv  STORE PAGES.tsv NAME FETCHED_AT   a crawl's pages (importer/linkcrawl.py pages.tsv, page_docs'
                                                          COPY columns) as STORE/NAME.parquet, every row fetched at
                                                          FETCHED_AT (ISO time: the crawl's own capture time is not kept)
    pages_store.py stats    STORE                         rows per part, distinct urls, what the latest-capture rule keeps
    pages_store.py pg-chars FILE                          BRIDGE (PG_DSN): Postgres's \\s, \\W and lower() tables as FILE
                                                          (release/pg-word-chars.txt, what page_words reads)
    pages_store.py check    STORE                         BRIDGE (PG_DSN): every page reader's rows from page_docs and from
                                                          the store, compared row by row (D1's equality check); exit 1 on
                                                          any difference. A store taken from that same page_docs only.

A STORE is a folder of Parquet parts with page_docs' columns. A url in several parts is the latest capture (fetched_at,
then crawl, as page_docs' readers order it), so a new crawl's capture of a page replaces the old one's; page_docs kept
the first row it was given (ON CONFLICT DO NOTHING). With SEARCH_PAGES_STORE=STORE the release's page readers read here
instead of page_docs: release_v1.py web-input's pages and the registry's page counts, the lookup stage's s_pages and
s_answer (tools/sqlite_lookups/build.py), pages_vec.py export. The noindex list is the docs table's at from-pg time:
docs is still in Postgres (move 4), and a re-export takes a newer one.
"""
from __future__ import annotations

import glob
import os
import re
import sys
import time

COLS = ["url", "domain", "crawl", "fetched_at", "http_status", "title", "description", "h1", "lang", "body", "error"]
TYPES = {"url": "VARCHAR", "domain": "VARCHAR", "crawl": "VARCHAR", "fetched_at": "TIMESTAMPTZ", "http_status": "INTEGER",
         "title": "VARCHAR", "description": "VARCHAR", "h1": "VARCHAR", "lang": "VARCHAR", "body": "VARCHAR", "error": "VARCHAR"}
TSV_COLS = ["domain", "crawl", "url", "http_status", "title", "description", "h1", "lang", "body", "error"]  # linkcrawl's
NOINDEX = "noindex-domains.txt"


def store_dir() -> str | None:
    return os.environ.get("SEARCH_PAGES_STORE") or None


def _duck():
    import duckdb
    c = duckdb.connect()
    c.execute("SET preserve_insertion_order = false")
    mem = os.environ.get("PAGES_DUCK_MEM")
    if mem:
        c.execute(f"SET memory_limit = '{mem}'")
    tmp = os.environ.get("TMPDIR")
    if tmp:
        c.execute(f"SET temp_directory = '{tmp}'")
    return c


def _parts(store: str) -> list[str]:
    parts = sorted(glob.glob(os.path.join(store, "*.parquet")))
    if not parts:
        sys.exit(f"pages store {store}: no *.parquet part")
    return parts


def latest_sql(store: str) -> str:
    """One row per url, the latest capture: what page_docs' DISTINCT ON (url) ... ORDER BY fetched_at DESC NULLS LAST,
    crawl gives, over every part."""
    files = ", ".join("'" + p.replace("'", "''") + "'" for p in _parts(store))
    return (f"(SELECT * FROM read_parquet([{files}], union_by_name = true) "
            f"QUALIFY row_number() OVER (PARTITION BY url ORDER BY fetched_at DESC NULLS LAST, crawl) = 1)")


def noindex(store: str) -> set[str]:
    path = os.path.join(store, NOINDEX)
    if not os.path.exists(path):
        sys.exit(f"pages store {store}: no {NOINDEX} (pages_store.py from-pg writes it)")
    return {l.strip() for l in open(path, encoding="utf-8") if l.strip()}


def rows(store: str, cols: list[str], where: str = "error IS NULL", order: bool = True, n: int = 20000):
    """The latest capture of every url, COLS of it, WHERE, in url order (bytes, as COLLATE "C"): latest_sql's pick
    (fetched_at DESC NULLS LAST, crawl), made by merging the parts, each read in its own url order (_write_parquet
    sorts them). A window over every part holds every column of every row (the page bodies) and does not spill: it ran
    out of memory at 3.5 GiB on ~2.35M pages. One thread per part: a multi-threaded scan in insertion order
    buffers what a slow reader has not taken yet. ORDER is kept for the callers; the merge always gives url order."""
    import heapq
    import itertools

    def part(path: str):
        c = _duck()
        c.execute("SET preserve_insertion_order = true")
        c.execute("SET threads = 1")  # in order with more threads, rows wait in memory for a slow reader (OOM in web-input)
        p = path.replace("'", "''")
        cur = c.execute(f"SELECT url, epoch_us(fetched_at), crawl, coalesce({where}, false), {', '.join(cols)} "
                        f"FROM read_parquet('{p}')")
        prev = None
        while True:
            batch = cur.fetchmany(n)
            if not batch:
                return
            for r in batch:
                if prev is not None and r[0] < prev:
                    sys.exit(f"pages store: {path} is not in url order ({r[0]!r} after {prev!r})")
                prev = r[0]
                yield r

    for _url, g in itertools.groupby(heapq.merge(*(part(p) for p in _parts(store)), key=lambda r: r[0]),
                                     key=lambda r: r[0]):
        g = list(g)
        if len(g) > 1:
            g.sort(key=lambda r: (r[2] is None, r[2] or ""))           # crawl, nulls last
            g.sort(key=lambda r: (r[1] is not None, r[1]), reverse=True)  # fetched_at desc, nulls last (stable)
        if g[0][3]:
            yield tuple(g[0][4:])


def shipped_pages(store: str, cols: list[str]):
    """What a release may ship of the pages, release_v1.PAGE_SQL's rule: fetched, with words, and not on a site whose
    homepage said noindex."""
    out = noindex(store)
    di = cols.index("domain") if "domain" in cols else None
    want = cols if di is not None else cols + ["domain"]
    di = want.index("domain")
    for r in rows(store, want, "error IS NULL AND coalesce(body, '') <> ''"):
        if r[di] not in out:
            yield r if len(want) == len(cols) else r[:-1]


# Postgres's own character classes, not Python's: its \s, \W and lower() follow the database's libc locale, and
# Python's differ (no-break space, U+001F and U+0085 are space to Python only; ², ½ and combining marks are word
# characters to Python only; Python lowers İ to two characters and a final Σ to ς). The tables come from the build
# database itself (pg-chars below) and the check compares them with it before any row.
PG_CHARS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pg-word-chars.txt")
PG_CHARS_SQL = [
    ("S", "SELECT string_agg(cp::text, ',' ORDER BY cp) FROM {cps} WHERE chr(cp) ~ '\\s'"),
    ("W", "WITH g AS (SELECT cp, cp - row_number() OVER (ORDER BY cp) AS k FROM {cps} WHERE chr(cp) ~ '\\W') "
          "SELECT string_agg(lo || '-' || hi, ',' ORDER BY lo) FROM (SELECT min(cp) lo, max(cp) hi FROM g GROUP BY k) r"),
    ("L", "SELECT string_agg(cp || ':' || ascii(lower(chr(cp))), ',' ORDER BY cp) FROM {cps} "
          "WHERE lower(chr(cp)) <> chr(cp)"),
]
_CPS = "(SELECT cp FROM generate_series(1, 1114111) cp WHERE cp NOT BETWEEN 55296 AND 57343) c_"
_pgw = None


def pg_chars_lines(dsn: str) -> dict[str, str]:
    """The S (\\s), W (\\W, as ranges) and L (lower, code point to code point) lines from a Postgres."""
    import psycopg
    with psycopg.connect(dsn) as c:
        if c.execute("SELECT bool_and(length(lower(chr(cp))) = 1) FROM " + _CPS).fetchone()[0] is not True:
            raise SystemExit("this Postgres lowers a character to several: pg-word-chars.txt cannot say that")
        return {k: c.execute(q.format(cps=_CPS)).fetchone()[0] or "" for k, q in PG_CHARS_SQL}


def _read_pg_chars(path: str = PG_CHARS) -> dict[str, str]:
    out = {}
    for line in open(path, encoding="utf-8"):
        if line.strip() and not line.startswith("#"):
            k, v = line.rstrip("\n").split("|", 1)
            out[k] = v
    return out


def _pg_words():
    global _pgw
    if _pgw is None:
        t = _read_pg_chars()
        cls = lambda pairs: "".join(f"\\U{lo:08x}" + (f"-\\U{hi:08x}" if hi != lo else "") for lo, hi in pairs)
        ws = [(int(x), int(x)) for x in t["S"].split(",") if x]
        nw = [tuple(int(v) for v in r.split("-")) for r in t["W"].split(",") if r]
        low = {int(a): int(b) for a, b in (x.split(":") for x in t["L"].split(",") if x)}
        _pgw = (re.compile(f"[{cls(ws)}]+"), re.compile(f"\\A[{cls(nw)}]+|[{cls(nw)}]+\\Z"), low)
    return _pgw


def page_words(text: str) -> str:
    """release_v1._PAGE_WORDS in Python: the words split on space, stripped of their edge punctuation, lowered, sorted
    by code point (COLLATE "C" on UTF-8), joined by one space; empties dropped, repeats kept. Space, punctuation and
    lower case are the build database's (pg-word-chars.txt)."""
    ws_re, edge_re, low = _pg_words()
    ws = [edge_re.sub("", x).translate(low) for x in ws_re.split(text or "")]
    return " ".join(sorted(w for w in ws if w))


def _page_input(r) -> tuple:
    url, title, h1, desc, body = r
    return url, (title or "") + " " + (h1 or "") + " " + page_words((desc or "") + " " + (body or ""))


def page_inputs(store: str, workers: int | None = None, batch: int = 20000):
    """web-input's page rows, (url, "title h1 page_words(description body)"), in url order. page_words is pure and
    per page, so WORKERS processes share it (PAGES_WORKERS, default: the cores, at most 10): rc12's 2.1M pages took
    66 min on one core. Batches keep memory flat; the next batch is read while the last one is worked on.
    forkserver, not fork: a forked worker would hold the caller's open pipes (web-input's zstd) and DuckDB's threads.
    A worker loads the caller's main script first; when it can't (python - reads it from stdin), Python 3.12's pool
    restarts the dying workers forever, so that caller gets one process."""
    import itertools
    import multiprocessing as mp
    n = workers or int(os.environ.get("PAGES_WORKERS") or min(10, os.cpu_count() or 1))
    main = getattr(sys.modules.get("__main__"), "__file__", None)
    if n > 1 and main and not os.path.exists(os.path.join(mp.process.ORIGINAL_DIR or "", main)):
        print(f"pages_store: worker processes can't load {main}; page words run in one process", file=sys.stderr)
        n = 1
    rows = shipped_pages(store, ["url", "title", "h1", "description", "body"])
    if n <= 1:
        yield from map(_page_input, rows)
        return
    with mp.get_context("forkserver").Pool(n) as pool:
        pending = None
        while True:
            chunk = list(itertools.islice(rows, batch))
            nxt = pool.map_async(_page_input, chunk, chunksize=max(1, len(chunk) // (4 * n))) if chunk else None
            if pending is not None:
                yield from pending.get()
            if nxt is None:
                return
            pending = nxt


def cmd_pg_chars(out: str) -> None:
    from_pg = pg_chars_lines(os.environ["PG_DSN"])
    with open(out + ".part", "w", encoding="utf-8") as f:
        f.write("# Postgres's \\s, \\W (code point ranges) and lower() (code point to code point), for page_words.\n"
                "# Made by: pages_store.py pg-chars FILE, against the build database (BRIDGE). Do not edit.\n")
        for k, _q in PG_CHARS_SQL:
            f.write(f"{k}|{from_pg[k]}\n")
    os.replace(out + ".part", out)
    print(f'{{"stage": "pg-chars", "space": {len(from_pg["S"].split(","))}, "lower": {len(from_pg["L"].split(","))}}}')


def _unescape(v: str) -> str | None:
    """A COPY text field: \\N is null; \\t \\n \\r \\\\ and the rest of COPY's escapes."""
    if v == "\\N":
        return None
    if "\\" not in v:
        return v
    out, i = [], 0
    esc = {"t": "\t", "n": "\n", "r": "\r", "\\": "\\", "b": "\b", "f": "\f", "v": "\v"}
    while i < len(v):
        ch = v[i]
        if ch == "\\" and i + 1 < len(v):
            out.append(esc.get(v[i + 1], v[i + 1]))
            i += 2
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _csv_field(v) -> str:
    return "\\N" if v is None else '"' + str(v).replace('"', '""') + '"'


def _write_parquet(records, out: str) -> int:
    """Rows (dicts with COLS) -> Parquet sorted by url, through a quoted CSV DuckDB reads in one pass (a null is the
    only unquoted field: \\N)."""
    tmp, n = out + ".part.csv", 0
    with open(tmp, "w", encoding="utf-8", newline="\n") as g:
        for r in records:
            g.write(",".join(_csv_field(r[k]) for k in COLS) + "\n")
            n += 1
    c = _duck()
    types = "{" + ", ".join(f"'{k}': '{TYPES[k]}'" for k in COLS) + "}"
    c.execute(f"COPY (SELECT * FROM read_csv('{tmp}', header = false, delim = ',', quote = '\"', escape = '\"', "
              f"nullstr = '\\N', allow_quoted_nulls = false, columns = {types}, strict_mode = true) ORDER BY url) "
              f"TO '{out}.tmp' (FORMAT parquet, COMPRESSION zstd)")
    os.replace(out + ".tmp", out)
    os.remove(tmp)
    return n


def cmd_add_tsv(store: str, tsv: str, name: str, fetched_at: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", name) or name.startswith("pg-"):
        sys.exit(f"add-tsv: part name {name!r}: letters, digits, _ . - only, and pg- is from-pg's")
    os.makedirs(store, exist_ok=True)
    out = os.path.join(store, name + ".parquet")
    if os.path.exists(out):
        sys.exit(f"add-tsv: {out} exists; a part is written once")
    t0 = time.time()

    def records():
        with open(tsv, encoding="utf-8", newline="\n") as f:
            for i, line in enumerate(f, 1):
                p = line.rstrip("\n").split("\t")
                if len(p) != len(TSV_COLS):
                    sys.exit(f"add-tsv: {tsv} line {i}: {len(p)} fields, want {len(TSV_COLS)}")
                r = dict(zip(TSV_COLS, (_unescape(x) for x in p)))
                r["fetched_at"] = fetched_at
                yield r
    n = _write_parquet(records(), out)
    print(f'{{"stage": "pages-store-add", "part": "{name}", "rows": {n}, "seconds": {time.time() - t0:.0f}}}', flush=True)


def cmd_from_pg(store: str) -> None:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "importer"))
    import export as ex
    import docs_policy
    import psycopg
    os.makedirs(store, exist_ok=True)
    out = os.path.join(store, "pg-page_docs.parquet")
    if os.path.exists(out):
        sys.exit(f"from-pg: {out} exists; move it aside to take a newer copy")
    t0 = time.time()
    with psycopg.connect(ex.PG_DSN) as pg:
        def records():
            with pg.cursor(name="pages_store") as cur:
                cur.itersize = 5000
                cur.execute(f"SELECT {', '.join(COLS)} FROM page_docs")
                for row in cur:
                    yield dict(zip(COLS, row))
        n = _write_parquet(records(), out)
        doms = [d for (d,) in pg.execute("SELECT DISTINCT domain FROM docs WHERE domain IN ("
                                         + docs_policy.PG_DOMAINS_NOINDEX + ") ORDER BY 1")]
    with open(os.path.join(store, NOINDEX + ".part"), "w", encoding="utf-8") as f:
        f.write("".join(d + "\n" for d in doms))
    os.replace(os.path.join(store, NOINDEX + ".part"), os.path.join(store, NOINDEX))
    print(f'{{"stage": "pages-store-from-pg", "rows": {n}, "noindex_domains": {len(doms)}, '
          f'"seconds": {time.time() - t0:.0f}}}', flush=True)


def cmd_stats(store: str) -> None:
    c = _duck()
    for p in _parts(store):
        n, u = c.execute(f"SELECT count(*), count(DISTINCT url) FROM read_parquet('{p}')").fetchone()
        print(f"{os.path.basename(p)}: {n} rows, {u} urls")
    n, ok, words = c.execute(f"SELECT count(*), count(*) FILTER (WHERE error IS NULL), "
                             f"count(*) FILTER (WHERE error IS NULL AND coalesce(body, '') <> '') FROM {latest_sql(store)}").fetchone()
    print(f"latest capture per url: {n} urls, {ok} fetched, {words} with words; noindex domains: {len(noindex(store))}")


def _compare(name: str, a, b, show: int = 3) -> bool:
    """Two row streams, both in their own order: equal row for row? Prints the count and the first differences."""
    import itertools
    n = diff = 0
    shown = []
    for x, y in itertools.zip_longest(a, b):
        n += 1
        x = tuple(x) if x is not None else None
        y = tuple(y) if y is not None else None
        if x != y:
            diff += 1
            if len(shown) < show:
                shown.append((n, x, y))
    print(f'{{"check": "{name}", "rows": {n}, "differ": {diff}}}', flush=True)
    for i, x, y in shown:
        print(f"  row {i}:\n    pg:    {str(x)[:300]}\n    store: {str(y)[:300]}", flush=True)
    return diff == 0


def cmd_check(store: str) -> int:
    here = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, here)
    sys.path.insert(0, os.path.join(here, "..", "importer"))
    sys.path.insert(0, os.path.join(here, "..", "tools", "sqlite_lookups"))
    import release_v1 as rv
    import build
    import pages_vec
    pg = lambda sql: rv.stream(rv.ex.PG_DSN, sql)
    if pg_chars_lines(rv.ex.PG_DSN) != _read_pg_chars():
        print(f"check: DIFFERENT — this Postgres's character classes are not {PG_CHARS}'s; remake it with pg-chars",
              flush=True)
        return 1
    print('{"check": "character classes", "differ": 0}', flush=True)
    ok = _compare("web-input pages", pg(rv.WEB_INPUT_SQL["pages"]), rv._store_page_input(store))
    ok &= _compare("web-input capture-rule count", pg(rv.PAGES_OUT_SQL), [rv._store_pages_out(store)])
    # the stage's s_pages: Postgres orders by its database collation, the store by bytes; compared as sets of rows
    ok &= _compare("stage s_pages", sorted(pg(build.STAGE["s_pages"][1])), rows(store, ["url", "domain", "title"]))
    ok &= _compare("stage s_answer open-dump sites", sorted({(d,) for (d,) in pg("SELECT domain FROM page_docs WHERE crawl LIKE 'open:%'")}),
                   sorted({(d,) for d, c in rows(store, ["domain", "crawl"], order=False) if (c or "").startswith("open:")}))
    T = pages_vec.TEXT_CHARS
    ok &= _compare("pages_vec export", pg(pages_vec._sql()),
                   ((u, ti or "", h or "", de or "", (bo or "")[:T]) for u, ti, h, de, bo in
                    shipped_pages(store, ["url", "title", "h1", "description", "body"])))
    print("check: " + ("EQUAL" if ok else "DIFFERENT"), flush=True)
    return 0 if ok else 1


def main(argv: list[str]) -> int:
    if len(argv) == 2 and argv[0] == "from-pg":
        cmd_from_pg(argv[1])
    elif len(argv) == 5 and argv[0] == "add-tsv":
        cmd_add_tsv(*argv[1:])
    elif len(argv) == 2 and argv[0] == "stats":
        cmd_stats(argv[1])
    elif len(argv) == 2 and argv[0] == "pg-chars":
        cmd_pg_chars(argv[1])
    elif len(argv) == 2 and argv[0] == "check":
        return cmd_check(argv[1])
    else:
        print(__doc__, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
