"""Common Crawl URL-index scan — our own per-domain, per-crawl statistics
and the best homepage capture for every site we hold.

The CC columnar index is 300 parquet files per subset per crawl, each a
contiguous SURT (reversed-host) range. Part numbers are NOT in SURT order, so
every file's range is read from its footer first (`map`). Files are then scanned
strictly one at a time with low DuckDB parallelism: the CDN answers 503 to wide
parallel range reads.
"""

from __future__ import annotations

import bisect
import gzip
import os
import re
import tempfile
import time

import duckdb
import psycopg

# How Common Crawl is read:
#   https — the public CDN, from anywhere; CC's per-IP limits apply (home).
#   s3    — s3://commoncrawl with the instance role, ONLY from inside us-east-1:
#           CC's own recommendation for in-region processing. Never used from
#           outside AWS (that would be dodging the CDN CC puts in front of us).
CC_ACCESS = os.environ.get("CC_ACCESS", "https")
if CC_ACCESS not in ("https", "s3"):
    raise SystemExit(f"CC_ACCESS must be https or s3, not {CC_ACCESS!r}")
CC_DATA = "s3://commoncrawl" if CC_ACCESS == "s3" else "https://data.commoncrawl.org"
SUBSETS = ("warc", "crawldiagnostics", "robotstxt")

# The downloaded catalogue columns are KEPT (decided 2026-09-23): each index file
# is fetched once into ARCHIVE/<crawl>/<subset>/<part>.parquet on the NAS and every
# query runs against that copy — a rescan, a new question (sites beyond Tranco,
# page-level) or a crash never re-downloads. ~57 GB per crawl for warc.
ARCHIVE = os.environ.get("CC_ARCHIVE", "/archive")   # "" = no local copy (disposable AWS instance)
ARCHIVE_COLUMNS = {
    "warc": ("url_host_registered_domain", "url_host_name", "url_path", "url_query", "url_protocol",
             "fetch_status", "content_mime_detected", "content_languages", "content_charset",
             "fetch_time", "warc_filename", "warc_record_offset", "warc_record_length"),
    "crawldiagnostics": ("url_host_registered_domain", "url_host_name", "url_path", "url_query",
                         "fetch_status", "fetch_redirect", "fetch_time"),
    "robotstxt": ("url_host_registered_domain", "url_host_name", "fetch_status", "fetch_time"),
}


def archived(d, path: str, subset: str) -> str:
    """Local parquet copy of one index file's needed columns; download only if absent.

    Written to a temp name and renamed, so an interrupted download never leaves a
    truncated file that a later run would trust.
    """
    if not ARCHIVE:
        # In-region and disposable: re-reading from S3 is free, a copy would outlive nothing.
        return f"{CC_DATA}/{path}"
    crawl = path.split("crawl=")[1].split("/")[0]
    local = os.path.join(ARCHIVE, crawl, subset, os.path.basename(path))
    if os.path.exists(local):
        return local
    os.makedirs(os.path.dirname(local), exist_ok=True)
    tmp = local + ".part"
    cols = ", ".join(ARCHIVE_COLUMNS[subset])
    d.execute(f"COPY (SELECT {cols} FROM read_parquet('{CC_DATA}/{path}')) "
              f"TO '{tmp}' (FORMAT parquet, COMPRESSION zstd)")
    os.replace(tmp, local)
    return local


def log(msg: str) -> None:
    print(f"[ccscan] {msg}", flush=True)


def surt_key(domain: str) -> str:
    """example.co.uk -> uk,co,example (the prefix of every SURT under it)."""
    return ",".join(reversed(domain.split(".")))


def _duck() -> duckdb.DuckDBPyConnection:
    d = duckdb.connect()
    # A cap DuckDB spills under instead of being OOM-killed: its default (80 % of RAM)
    # plus search-postgres's 1 GB shared_buffers overran a 3.7 GB build instance.
    mem = os.environ.get("CC_DUCK_MEM", "")
    if mem:
        if not re.fullmatch(r"[0-9]+(MB|GB)", mem):
            raise SystemExit(f"CC_DUCK_MEM must look like 1500MB or 2GB, not {mem!r}")
        d.execute(f"SET memory_limit='{mem}'; SET preserve_insertion_order=false; "
                  "SET temp_directory='/tmp/duckdb-spill';")
    # CDN: 2 threads (DuckDB's default parallelism drew 503s). S3 in-region:
    # 4 — still one index file at a time, far below any S3 request limit.
    threads = 4 if CC_ACCESS == "s3" else 2
    # Retries: the CDN wants long patient backoff (10 x doubling from 3 s). S3 must not
    # get that — a SigV4-signed request retried for >15 min is refused with
    # RequestTimeTooSkewed (403), which killed the 2026-09-23 scan at robotstxt 230/300.
    # 6 x 1.5 from 2 s keeps one request's whole retry chain around a minute.
    retries, wait_ms, backoff = (6, 2000, 1.5) if CC_ACCESS == "s3" else (10, 3000, 2)
    d.execute(f"LOAD httpfs; SET threads={threads}; SET http_retries={retries}; "
              f"SET http_retry_wait_ms={wait_ms}; SET http_retry_backoff={backoff}; "
              "SET enable_http_metadata_cache=true;")
    if CC_ACCESS == "s3":
        # Authenticated with the instance role (AWS SDK default chain, incl. IMDSv2).
        d.execute("LOAD aws; CREATE OR REPLACE SECRET cc (TYPE s3, PROVIDER credential_chain, REGION 'us-east-1')")
    return d


def _rx_bytes() -> int:
    """Bytes received on this container's interfaces (its own network namespace)."""
    total = 0
    with open("/proc/net/dev") as f:
        for line in f.readlines()[2:]:
            name, data = line.split(":", 1)
            if name.strip() != "lo":
                total += int(data.split()[0])
    return total


def _paths(crawl: str, fetch) -> list[str]:
    if CC_ACCESS == "s3":
        d = _duck()
        rows = d.execute(f"SELECT column0 FROM read_csv('{CC_DATA}/crawl-data/{crawl}/cc-index-table.paths.gz', "
                         "header = false, columns = {'column0': 'VARCHAR'})").fetchall()
        return [r[0] for r in rows if r[0].endswith(".parquet")]
    raw = fetch(f"{CC_DATA}/crawl-data/{crawl}/cc-index-table.paths.gz")
    return [p for p in gzip.decompress(raw).decode().split() if p.endswith(".parquet")]


def cmd_map(conn: psycopg.Connection, crawl: str, subsets: list[str], fetch) -> None:
    """Record every index file's SURT range (footer reads only, a few KB each)."""
    known = {p for (p,) in conn.execute("SELECT path FROM cc_index_files WHERE crawl = %s", (crawl,))}
    todo = [p for p in _paths(crawl, fetch)
            if any(f"/subset={s}/" in p for s in subsets) and p not in known]
    log(f"{crawl}: {len(todo)} files to map ({len(known)} already mapped)")
    d = _duck()
    for i, p in enumerate(todo, 1):
        lo, hi = d.execute(
            f"SELECT min(stats_min_value), max(stats_max_value) FROM parquet_metadata('{CC_DATA}/{p}') "
            f"WHERE path_in_schema = 'url_surtkey'").fetchone()
        subset = p.split("/subset=")[1].split("/")[0]
        conn.execute("INSERT INTO cc_index_files (path, crawl, subset, surt_min, surt_max) "
                     "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (path) DO NOTHING", (p, crawl, subset, lo, hi))
        conn.commit()
        if i % 50 == 0 or i == len(todo):
            log(f"mapped {i}/{len(todo)}")


def files_holding(files: list[tuple[str, str, str]], missing: list[str]) -> list[tuple[str, str, str]]:
    """Index files (path, surt_min, surt_max) whose SURT range holds any of `missing`.

    `missing` is a SORTED list of domain SURT keys ("com,example"). A domain's
    SURTs run from key+")" (bare host) to key+",<sub>)" (subdomains), so it
    overlaps [lo, hi] when key+")" <= hi and key+",\uffff" >= lo. The search
    starts a little before lo's own host because a parent domain ("com,example")
    sorts just ahead of a file that begins at one of its subdomains.
    """
    keep = []
    for path, lo, hi in files:
        i = max(0, bisect.bisect_left(missing, lo.split(")")[0]) - 20)
        while i < len(missing) and missing[i] + ")" <= hi:
            if missing[i] + ",\uffff" >= lo:
                keep.append((path, lo, hi))
                break
            i += 1
    return keep


def _files_to_scan(conn, crawl: str, subset: str, only_missing: bool) -> list[tuple[str, str, str]]:
    files = conn.execute(
        "SELECT path, surt_min, surt_max FROM cc_index_files "
        "WHERE crawl = %s AND subset = %s AND scanned_at IS NULL ORDER BY surt_min",
        (crawl, subset)).fetchall()
    if not only_missing:
        return files
    # Gap fill: only files whose range holds an eligible site with no capture yet.
    missing = sorted(surt_key(d) for (d,) in conn.execute(
        "SELECT s.domain FROM sites s WHERE s.blocked_by IS NULL "
        "AND NOT EXISTS (SELECT 1 FROM cc_captures c WHERE c.domain = s.domain)"))
    keep = files_holding(files, missing)
    log(f"gap fill: {len(missing):,} sites lack a capture; {len(keep)}/{len(files)} {crawl} files hold some")
    return keep


# The homepage that represents a domain lives on the bare host or www. — a root
# page on another subdomain (developers.facebook.com, tv.apple.com, a customer
# bucket on amazonaws.com) is a different site, and ranked above facebook.com's
# own pages in the first scan (2026-09-23). Among bare/www captures:
# root > one level down, https > http, bare > www; newest breaks ties.
# One level down, an English-first page beats any other (2026-10-08): a site whose "/" redirects by language has
# only such pages, and the newest was the homepage: thunderbird.net/tr/, firefox.com/es-CL/, nodejs.org/tr/.
_BARE = "(h = d OR h = 'www.' || d)"
# Fallback homepages (2026-09-24): 117,519 Tranco sites had HTML captures but no
# bare/www front page. A landing-like subdomain's front page (en.example.com) or an
# obvious English landing path is accepted — always below any bare/www capture.
_SUB_HOSTS = "^(en|us|en-us|home|web|site|www2|www3|main|portal|info|about|corp|global)\\."
_LANDING = "('/', '/en', '/en/', '/en-us', '/en-us/', '/us', '/us/', '/home', '/home/', '/index.html', '/index.htm')"

_QUALITY = f"""(CASE WHEN {_BARE} THEN 0 ELSE -30 END
             + CASE WHEN p = '/' THEN 20 ELSE 0 END
             + CASE WHEN l LIKE 'eng%' THEN 10 ELSE 0 END
             + CASE WHEN pr = 'https' THEN 5 ELSE 0 END
             + CASE WHEN h = d THEN 1 ELSE 0 END)"""


# Best homepage capture per domain from temp table `j` (one file's rows for our
# domains). t::VARCHAR — returning a TIMESTAMPTZ to Python makes DuckDB import
# pytz, which the image does not ship; Postgres parses the text on COPY.
CAPS_SQL = f"""SELECT d, {_QUALITY} qual,
               CASE WHEN NOT {_BARE} THEN 'subroot' WHEN p = '/' THEN 'root' ELSE 'shallow' END,
               h, p, pr, t::VARCHAR, l, wf, wo, wl
        FROM j WHERE s = 200 AND m = 'text/html' AND q IS NULL
          AND ((regexp_matches(p, '^/[^/]*/?$') AND {_BARE})
               OR (NOT {_BARE} AND regexp_matches(h, '{_SUB_HOSTS}') AND p IN {_LANDING}))
        QUALIFY row_number() OVER (PARTITION BY d ORDER BY qual DESC, t DESC) = 1"""


def _scan_warc(d, src):
    d.execute(f"""CREATE OR REPLACE TEMP TABLE j AS
        SELECT x.url_host_registered_domain d, x.url_host_name h, x.url_path p, x.url_query q,
               x.url_protocol pr, x.fetch_status s, x.content_mime_detected m,
               x.content_languages l, x.fetch_time t, x.warc_filename wf,
               x.warc_record_offset wo, x.warc_record_length wl
        FROM read_parquet('{src}') x SEMI JOIN ours o ON x.url_host_registered_domain = o.domain""")
    stats = d.execute("""SELECT d, count(*) FILTER (WHERE s = 200), count(*) FILTER (WHERE s = 200 AND m = 'text/html'),
                                count(DISTINCT h) FROM j GROUP BY d""").fetchall()
    caps = d.execute(CAPS_SQL).fetchall()
    d.execute("DROP TABLE j")
    return stats, caps


def _write_warc(conn, crawl, stats, caps):
    conn.execute("CREATE TEMP TABLE t_st (domain text, ok int, html int, hosts int) ON COMMIT DROP")
    with conn.cursor().copy("COPY t_st FROM STDIN") as cp:
        for r in stats:
            cp.write_row(r)
    conn.execute(
        "INSERT INTO cc_domain_crawl (domain, crawl, ok_200, html_200, hosts) "
        "SELECT domain, %s, ok, html, hosts FROM t_st ON CONFLICT (domain, crawl) DO UPDATE SET "
        "ok_200 = cc_domain_crawl.ok_200 + EXCLUDED.ok_200, html_200 = cc_domain_crawl.html_200 + EXCLUDED.html_200, "
        "hosts = cc_domain_crawl.hosts + EXCLUDED.hosts", (crawl,))
    conn.execute("CREATE TEMP TABLE t_cap (domain text, quality int, kind text, host text, path text, protocol text, "
                 "fetch_time timestamptz, languages text, wf text, wo bigint, wl int) ON COMMIT DROP")
    with conn.cursor().copy("COPY t_cap FROM STDIN") as cp:
        for r in caps:
            cp.write_row(r)
    # A domain straddling two files: keep whichever file's capture is better.
    conn.execute(
        "INSERT INTO cc_captures (domain, crawl, quality, kind, host, path, protocol, fetch_time, languages, "
        "warc_filename, warc_offset, warc_length) "
        "SELECT domain, %s, quality, kind, host, path, protocol, fetch_time, languages, wf, wo, wl FROM t_cap "
        "ON CONFLICT (domain, crawl) DO UPDATE SET quality = EXCLUDED.quality, kind = EXCLUDED.kind, "
        "host = EXCLUDED.host, path = EXCLUDED.path, protocol = EXCLUDED.protocol, fetch_time = EXCLUDED.fetch_time, "
        "languages = EXCLUDED.languages, warc_filename = EXCLUDED.warc_filename, "
        "warc_offset = EXCLUDED.warc_offset, warc_length = EXCLUDED.warc_length "
        "WHERE (EXCLUDED.quality, EXCLUDED.fetch_time) > (cc_captures.quality, cc_captures.fetch_time)", (crawl,))
    return len(caps)


def _scan_diag(d, src):
    return d.execute(f"""SELECT x.url_host_registered_domain,
            count(*) FILTER (WHERE x.fetch_status BETWEEN 300 AND 399),
            count(*) FILTER (WHERE x.fetch_status BETWEEN 400 AND 499),
            count(*) FILTER (WHERE x.fetch_status BETWEEN 500 AND 599)
        FROM read_parquet('{src}') x SEMI JOIN ours o ON x.url_host_registered_domain = o.domain
        GROUP BY 1""").fetchall()


def _write_diag(conn, crawl, rows):
    conn.execute("CREATE TEMP TABLE t_dg (domain text, r int, e4 int, e5 int) ON COMMIT DROP")
    with conn.cursor().copy("COPY t_dg FROM STDIN") as cp:
        for r in rows:
            cp.write_row(r)
    conn.execute(
        "INSERT INTO cc_domain_crawl (domain, crawl, redirects, err_4xx, err_5xx) "
        "SELECT domain, %s, r, e4, e5 FROM t_dg ON CONFLICT (domain, crawl) DO UPDATE SET "
        "redirects = cc_domain_crawl.redirects + EXCLUDED.redirects, "
        "err_4xx = cc_domain_crawl.err_4xx + EXCLUDED.err_4xx, err_5xx = cc_domain_crawl.err_5xx + EXCLUDED.err_5xx",
        (crawl,))
    return 0


def _scan_robots(d, src):
    return d.execute(f"""SELECT x.url_host_registered_domain, mode(x.fetch_status)
        FROM read_parquet('{src}') x SEMI JOIN ours o ON x.url_host_registered_domain = o.domain
        GROUP BY 1""").fetchall()


def _write_robots(conn, crawl, rows):
    conn.execute("CREATE TEMP TABLE t_rb (domain text, st int) ON COMMIT DROP")
    with conn.cursor().copy("COPY t_rb FROM STDIN") as cp:
        for r in rows:
            cp.write_row(r)
    conn.execute(
        "INSERT INTO cc_domain_crawl (domain, crawl, robots_status) SELECT domain, %s, st FROM t_rb "
        "ON CONFLICT (domain, crawl) DO UPDATE SET robots_status = EXCLUDED.robots_status", (crawl,))
    return 0


def shard_of(files: list, shard: str | None) -> list:
    """Keep every n-th file for worker i of n ("i/n", 1-based) — disjoint and complete,
    so two workers never scan (and sum) the same file."""
    if not shard:
        return files
    i, n = (int(x) for x in shard.split("/"))
    if not 1 <= i <= n:
        raise ValueError(f"shard {shard!r}: need 1 <= i <= n")
    return [f for k, f in enumerate(files) if k % n == i - 1]


# Per-subset results, so a reset of one subset leaves the others' columns intact.
_RESET = {
    "warc": ("DELETE FROM cc_captures WHERE crawl = %(c)s",
             "UPDATE cc_domain_crawl SET ok_200 = 0, html_200 = 0, hosts = 0 WHERE crawl = %(c)s"),
    "crawldiagnostics": ("UPDATE cc_domain_crawl SET redirects = 0, err_4xx = 0, err_5xx = 0 WHERE crawl = %(c)s",),
    "robotstxt": ("UPDATE cc_domain_crawl SET robots_status = NULL WHERE crawl = %(c)s",),
}


def cmd_reset(conn: psycopg.Connection, crawl: str, subset: str) -> None:
    """Wipe one crawl+subset's results and done-markers before a clean rescan.

    Per-domain counts are SUMMED file by file, so rescanning files that already
    counted would double them. The archived parquet copies are kept — a rescan
    after a reset reads them instead of downloading again.
    """
    with conn.transaction():
        for sql in _RESET[subset]:
            conn.execute(sql, {"c": crawl})
        cur = conn.execute("UPDATE cc_index_files SET scanned_at = NULL, rx_bytes = NULL, seconds = NULL, "
                           "domains = NULL WHERE crawl = %s AND subset = %s AND scanned_at IS NOT NULL",
                           (crawl, subset))
    log(f"{crawl}/{subset}: results wiped, {cur.rowcount} files marked unscanned (archive kept)")


def _load_ours(conn, d) -> int:
    """All our domains into DuckDB in one bulk read (row-by-row inserts took ~3 min)."""
    with tempfile.NamedTemporaryFile("wb", suffix=".tsv", delete=False) as f:
        with conn.cursor().copy("COPY (SELECT domain FROM sites) TO STDOUT") as cp:
            for block in cp:
                f.write(bytes(block))
        name = f.name
    try:
        d.execute(f"CREATE TABLE ours AS SELECT * FROM read_csv('{name}', header = false, "
                  f"delim = '\t', quote = '', columns = {{'domain': 'VARCHAR'}})")
    finally:
        os.unlink(name)
    return d.execute("SELECT count(*) FROM ours").fetchone()[0]


def cmd_scan(conn: psycopg.Connection, crawl: str, subset: str, only_missing: bool, limit: int | None,
             shard: str | None = None) -> None:
    files = shard_of(_files_to_scan(conn, crawl, subset, only_missing), shard)
    if limit:
        files = files[:limit]
    if not files:
        log(f"{crawl}/{subset}: nothing to scan (run `ccmap` first, or all files are done)")
        return
    d = _duck()
    n_ours = _load_ours(conn, d)
    log(f"{crawl}/{subset}: scanning {len(files)} files for {n_ours:,} domains"
        + (f" (shard {shard})" if shard else "") + f", archive {ARCHIVE}")

    total_rx, total_s, total_caps, reused = 0, 0.0, 0, 0
    for i, (path, _lo, _hi) in enumerate(files, 1):
        rx0, t0 = _rx_bytes(), time.time()
        local = os.path.join(ARCHIVE, crawl, subset, os.path.basename(path))
        reused += os.path.exists(local)
        src = archived(d, path, subset)
        if subset == "warc":
            stats, caps = _scan_warc(d, src)
            seen = len(stats)
        elif subset == "crawldiagnostics":
            rows = _scan_diag(d, src)
            seen = len(rows)
        else:
            rows = _scan_robots(d, src)
            seen = len(rows)
        rx, secs = _rx_bytes() - rx0, time.time() - t0
        with conn.transaction():
            if subset == "warc":
                total_caps += _write_warc(conn, crawl, stats, caps)
            elif subset == "crawldiagnostics":
                _write_diag(conn, crawl, rows)
            else:
                _write_robots(conn, crawl, rows)
            conn.execute("UPDATE cc_index_files SET scanned_at = now(), rx_bytes = %s, seconds = %s, domains = %s "
                         "WHERE path = %s", (rx, secs, seen, path))
        total_rx += rx
        total_s += secs
        if i % 10 == 0 or i == len(files):
            log(f"{i}/{len(files)} files ({reused} from the archive), {total_rx / 1e9:.2f} GB downloaded, "
                f"{total_s / 60:.1f} min" + (f", {total_caps:,} captures" if subset == "warc" else ""))
