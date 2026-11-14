"""Loads the seed corpus for the local search engine.

    search_import.py migrate              apply schema.sql (idempotent)
    search_import.py blocklists           mark ad/tracker/malware domains ineligible
    search_import.py history FILE         host<TAB>visits<TAB>last_visit_unix from the browser
    search_import.py ccgraph --release R  CC web-graph link authority + subdomains
    search_import.py cchostindex --crawls C1,C2   CC host-index website evidence
    search_import.py ccmap --crawl C      record every CC index file's SURT range
    search_import.py ccscan --crawl C --subset S [--only-missing] [--limit N] [--shard i/n] [--reset]   scan
    search_import.py ccfetch [--crawl C] [--workers N] [--limit N]   fetch + parse captures
    search_import.py ccfetch --refetch-stubs [--gov-only] [--dry-run]  stub captures read again from their sites
    search_import.py ccrobots [--workers N] [--limit N] [--rate R]   read stored captures again for their robots statements (G3 D4c)
    search_import.py wiki --file F --dump D [--procs N] [--limit N]   English Wikipedia
    search_import.py wiki-load --articles A --redirects R --dump D   load files parsed elsewhere (buildbox)
    search_import.py wiki-sites --file DUMP [--procs N]     official website per article -> wiki_sites
    search_import.py stackexchange [--sites S …]              Stack Exchange Q&A -> se_docs
    search_import.py se-authors [--sites S …]                 who asked / answered each se_docs question -> se_authors
    search_import.py open-pages --source S [--file F]        answer pages from open dumps -> page_docs
    search_import.py places                                  OpenStreetMap named places -> places
    search_import.py subreddits --file F                     subreddit directory -> subreddits
    search_import.py youtube                                 YouTube channels from Wikidata -> youtube_channels
    search_import.py wayback [--per-site S] [--limit N]      homepages from the Wayback Machine -> docs
    search_import.py d2q-load --file F                                   model-written queries per site (JSON) -> d2q_docs
    search_import.py anchors-load --file F [--meta M] [--inlinks I]      anchor text from jobs/cc-anchors.sh -> anchors, anchor_docs,
                                                             site_meta
    search_import.py query-list [--qac F] [--typos N]         the synthetic query list (legal sources) -> query_list
    search_import.py clickstream [--month 2026-08]            Wikipedia articles reached from search engines -> wiki_search_refs,
                                                             and the articles readers open next -> wiki_next
    search_import.py entities [--categories a,b]              named-entity lists from Wikidata -> entities
    search_import.py facts [--facts a,b]                      answer-box facts from Wikidata -> wiki_facts
    search_import.py wdc-places --file F [--zips Z]          businesses from jobs/wdc-places.sh -> places
    search_import.py pageviews [--month YYYY-MM]             Wikipedia monthly views -> wiki_pop.pageviews
    search_import.py wiki-pop                                Wikidata sitelinks per article -> wiki_pop
    search_import.py wiki-links [--platforms a,b]            every account + official site in Wikidata -> wiki_links
    search_import.py gutenberg-catalog [--file F]            books Project Gutenberg holds, by title and author -> wiki_links
    search_import.py card-images meta [--top N] [--lead-dump F]   card pictures: lead/Wikidata image + Commons licence -> card-images.sqlite src
    search_import.py card-images fetch [--limit N]           card pictures: thumbnails -> card-images.sqlite (2 at a time, stops on 403/429)
    search_import.py card-images todo [--limit N] > F.tsv.gz   card pictures: the next N to download on AWS (jobs/card-images.sh)
    search_import.py card-images load --file F               card pictures: the AWS job's results -> card-images.sqlite
    search_import.py card-images seed                        card pictures: ONE-SHOT bridge, Postgres card_images -> the file
    search_import.py quality                                 junk homepages/pages -> error 'junk: …'
    search_import.py wikidata-sites                          + official sites only in Wikidata (after wiki-sites)
    search_import.py kinds-wikidata                          what kind of site each official site is (Wikidata) -> kw_*
    search_import.py kinds-build --open [--import DIR]             site kinds, open sources only (ADR-065) -> site_kinds, kind_docs, kind_members
    search_import.py kinds-open                                  re-run the open filter over site_kinds_new
    search_import.py kinds-build [--import DIR] [--phrases F] [--members F]  (old, Jev) site kinds -> site_kinds, kind_docs, kind_members
    search_import.py junk-load [--quality F] [--ut1 F]        junk filter (Jev + UT1 unsafe) -> site_quality
    search_import.py kinds-vecs --file F                      kind-name embeddings (tools/embed-kinds.py) -> kind_vecs
    search_import.py vec-load --table T_vec --ids I --vecs V  GPU-made embeddings -> pgvector + HNSW
    search_import.py stats                what is loaded

Every subcommand runs `migrate` first, so a schema change needs no separate step.
Connection comes from the standard PG* environment variables.
"""

from __future__ import annotations

import argparse
import re
import os
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import psycopg
import tldextract

# The lists apps/adguard/AdGuardHome-template.yaml enables (minus the device-
# specific ones that never name a website), plus HaGeZi NSFW + gambling.
# Order is precedence: the first list that names a domain is the one recorded.
BLOCKLISTS = [
    ("hagezi-tif", "https://cdn.jsdelivr.net/gh/hagezi/dns-blocklists@latest/adblock/tif.txt"),
    ("hagezi-pro", "https://cdn.jsdelivr.net/gh/hagezi/dns-blocklists@latest/adblock/pro.txt"),
    ("hagezi-hoster", "https://cdn.jsdelivr.net/gh/hagezi/dns-blocklists@latest/adblock/hoster.txt"),
    # Adult and gambling sites are never indexed (2026-09-23: the Tranco top-1M is
    # full of both, and they surfaced in Loggedout search results).
    ("hagezi-nsfw", "https://cdn.jsdelivr.net/gh/hagezi/dns-blocklists@latest/adblock/nsfw.txt"),
    ("hagezi-gambling", "https://cdn.jsdelivr.net/gh/hagezi/dns-blocklists@latest/adblock/gambling.txt"),
    ("nocoin", "https://raw.githubusercontent.com/hoshsadiq/adblock-nocoin-list/master/hosts.txt"),
    ("windows-spy", "https://raw.githubusercontent.com/crazy-max/WindowsSpyBlocker/master/data/hosts/spy.txt"),
]

# `||example.com^` (adblock syntax) or `0.0.0.0 example.com` (hosts syntax).
_ADBLOCK = re.compile(r"^\|\|([a-z0-9.-]+)\^")
_HOSTS = re.compile(r"^(?:0\.0\.0\.0|127\.0\.0\.1)\s+([a-z0-9.-]+)")

# Offline: the bundled public-suffix snapshot, never a network fetch at runtime.
_extract = tldextract.TLDExtract(suffix_list_urls=())

CC_DATA = "https://data.commoncrawl.org"
CC_GRAPH_BASE = f"{CC_DATA}/projects/hyperlinkgraph"
CC_HOSTINDEX_PATHS = f"{CC_DATA}/projects/host-index-testing/v2.paths.gz"

SCHEMA = Path(__file__).with_name("schema.sql")
USER_AGENT = "logout-search-import/1 (personal search index)"


def log(msg: str) -> None:
    print(f"[search-import] {msg}", flush=True)


def fetch(url: str, timeout: int = 120) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def registrable(host: str) -> str:
    """example.co.uk for www.shop.example.co.uk; '' for IPs and bare suffixes."""
    return _extract(host).top_domain_under_public_suffix


def migrate(conn: psycopg.Connection) -> None:
    conn.execute(SCHEMA.read_text())
    conn.commit()


def parse_blocklist(text: str) -> set[str]:
    out = set()
    for line in text.splitlines():
        line = line.strip().lower()
        m = _ADBLOCK.match(line) or _HOSTS.match(line)
        if m:
            out.add(m.group(1).strip("."))
    return out


def cmd_blocklists(conn: psycopg.Connection) -> None:
    # Only an entry naming the registrable domain ITSELF blocks a site. A list
    # entry for ads.example.com says nothing about example.com's homepage, so
    # subdomain entries are not rolled up to their parent.
    marks: dict[str, str] = {}
    for name, url in BLOCKLISTS:
        try:
            entries = parse_blocklist(fetch(url).decode("utf-8", "replace"))
        except Exception as e:  # one dead list must not unblock everything else
            sys.exit(f"blocklist {name} ({url}) failed to download: {e} — nothing changed")
        new = sum(1 for d in entries if d not in marks)
        for d in entries:
            marks.setdefault(d, name)
        log(f"{name}: {len(entries):,} entries ({new:,} not already listed)")

    with conn.transaction():
        conn.execute("CREATE TEMP TABLE t_block (domain text PRIMARY KEY, list text) ON COMMIT DROP")
        with conn.cursor().copy("COPY t_block (domain, list) FROM STDIN") as cp:
            for d, name in marks.items():
                cp.write_row((d, name))
        conn.execute("UPDATE sites SET blocked_by = NULL WHERE blocked_by IS NOT NULL")
        cur = conn.execute(
            "UPDATE sites s SET blocked_by = b.list FROM t_block b WHERE b.domain = s.domain"
        )
        log(f"sites marked ineligible: {cur.rowcount:,}")
        # A newly blocked site must also leave the searchable content.
        gone = conn.execute(
            "DELETE FROM docs d USING sites s WHERE s.domain = d.domain AND s.blocked_by IS NOT NULL")
        log(f"parsed homepages removed for blocked sites: {gone.rowcount:,}")


def cmd_history(conn: psycopg.Connection, file: str) -> None:
    rows = []
    for line in Path(file).read_text().splitlines():
        parts = line.split("\t")
        if len(parts) != 3 or not parts[1].isdigit():
            continue
        host = parts[0].strip().lower().rstrip(".")
        domain = registrable(host)
        if not domain:
            continue
        last = datetime.fromtimestamp(int(parts[2]), tz=timezone.utc) if parts[2].isdigit() else None
        rows.append((host, domain, int(parts[1]), last))
    log(f"history hosts with a registrable domain: {len(rows):,}")

    with conn.transaction():
        conn.execute(
            "CREATE TEMP TABLE t_hist (host text, domain text, visits integer, last timestamptz) ON COMMIT DROP"
        )
        with conn.cursor().copy("COPY t_hist FROM STDIN") as cp:
            for r in rows:
                cp.write_row(r)
        conn.execute(
            "INSERT INTO hosts (host, domain, source, visits, last_visit) "
            "SELECT host, domain, 'history', visits, last FROM t_hist "
            "ON CONFLICT (host) DO UPDATE SET visits = EXCLUDED.visits, last_visit = EXCLUDED.last_visit"
        )
        # Sites you have visited still belong in the index.
        conn.execute(
            "INSERT INTO sites (domain) SELECT DISTINCT domain FROM t_hist ON CONFLICT (domain) DO NOTHING"
        )
        cur = conn.execute(
            "UPDATE sites s SET history_visits = h.v, history_last = h.l "
            "FROM (SELECT domain, sum(visits)::int AS v, max(last_visit) AS l "
            "      FROM hosts WHERE source = 'history' GROUP BY domain) h "
            "WHERE h.domain = s.domain"
        )
        log(f"sites with history: {cur.rowcount:,}")


def _stream_lines(url: str):
    """Yield decoded lines of a (possibly huge) gzip file over one sequential GET."""
    import gzip
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=120) as r, gzip.GzipFile(fileobj=r) as gz:
        for raw in gz:
            yield raw.decode("utf-8", "replace").rstrip("\n")


def cmd_ccgraph(conn: psycopg.Connection, release: str, hosts_per_domain: int) -> None:
    """Link authority for our sites + their subdomains from a CC web graph release.

    Both rank files are streamed and filtered against the domains we hold, so
    the multi-GB originals are never stored. Host ranks are sorted best-first,
    which is what makes the per-domain cap a plain counter.
    """
    base = f"{CC_GRAPH_BASE}/{release}"
    ours = {d for (d,) in conn.execute("SELECT domain FROM sites")}
    log(f"{len(ours):,} domains to match against {release}")

    # --- domain ranks: #harmonicc_pos #harmonicc_val #pr_pos #pr_val #host_rev #n_hosts
    drows, seen = [], 0
    for line in _stream_lines(f"{base}/domain/{release}-domain-ranks.txt.gz"):
        if line.startswith("#"):
            continue
        seen += 1
        f = line.split("\t")
        if len(f) < 6:
            continue
        dom = ".".join(reversed(f[4].split(".")))
        if dom in ours:
            drows.append((dom, int(f[0]), float(f[1]), int(f[2]), float(f[3]), int(f[5])))
        if seen % 20_000_000 == 0:
            log(f"domain ranks: {seen:,} read, {len(drows):,} matched")
    log(f"domain ranks: {seen:,} read, {len(drows):,} of our domains ranked")

    # --- host ranks: subdomains of our domains, top-N per domain by harmonic rank.
    # The site itself (bare / www.) is covered by the domain rank above.
    hrows, per, seen = [], {}, 0
    for line in _stream_lines(f"{base}/host/{release}-host-ranks.txt.gz"):
        if line.startswith("#"):
            continue
        seen += 1
        if seen % 50_000_000 == 0:  # before any `continue`: most lines are not ours
            log(f"host ranks: {seen:,} read, {len(hrows):,} subdomains kept")
        f = line.split("\t")
        if len(f) < 5:
            continue
        labels = f[4].split(".")[::-1]              # rev host -> normal label order
        dom = None
        for k in range(2, len(labels) + 1):         # longest registrable match wins
            cand = ".".join(labels[-k:])
            if cand in ours:
                dom = cand
        if dom is None:
            continue
        host = ".".join(labels)
        if host == dom or host == "www." + dom:
            continue
        n = per.get(dom, 0)
        if n >= hosts_per_domain:
            continue
        per[dom] = n + 1
        hrows.append((host, dom, int(f[0]), int(f[2])))
    log(f"host ranks: {seen:,} read, {len(hrows):,} subdomains kept across {len(per):,} domains")

    with conn.transaction():
        conn.execute("CREATE TEMP TABLE t_dr (domain text, hp int, hv real, pp int, pv float8, nh int) ON COMMIT DROP")
        with conn.cursor().copy("COPY t_dr FROM STDIN") as cp:
            for r in drows:
                cp.write_row(r)
        conn.execute("UPDATE sites SET cc_graph = NULL, cc_hc_pos = NULL, cc_hc_val = NULL, "
                     "cc_pr_pos = NULL, cc_pr_val = NULL, cc_n_hosts = NULL WHERE cc_graph IS NOT NULL")
        conn.execute(
            "UPDATE sites s SET cc_graph = %s, cc_hc_pos = t.hp, cc_hc_val = t.hv, cc_pr_pos = t.pp, "
            "cc_pr_val = t.pv, cc_n_hosts = t.nh FROM t_dr t WHERE t.domain = s.domain", (release,))
        conn.execute("CREATE TEMP TABLE t_hr (host text, domain text, hp int, pp int) ON COMMIT DROP")
        with conn.cursor().copy("COPY t_hr FROM STDIN") as cp:
            for r in hrows:
                cp.write_row(r)
        conn.execute("DELETE FROM hosts WHERE source = 'cc-webgraph'")
        conn.execute(
            "INSERT INTO hosts (host, domain, source, cc_hc_pos, cc_pr_pos) "
            "SELECT host, domain, 'cc-webgraph', hp, pp FROM t_hr "
            "ON CONFLICT (host) DO UPDATE SET cc_hc_pos = EXCLUDED.cc_hc_pos, cc_pr_pos = EXCLUDED.cc_pr_pos")
    log("web graph ranks loaded")


def cmd_cchostindex(conn: psycopg.Connection, crawls: list[str]) -> None:
    """Website evidence: successful page fetches CC logged per registrable domain.

    The host index is one parquet row per host per crawl; only the two columns
    needed are read. Its newest crawl (as of 2026-09) is CC-MAIN-2025-18, so this
    is historical evidence — 2026 evidence comes from the URL-index scan.
    """
    import duckdb
    import gzip as _gz
    paths = _gz.decompress(fetch(CC_HOSTINDEX_PATHS)).decode().split()
    files = [f"{CC_DATA}/{p}" for p in paths if any(f"crawl={c}/" in p for c in crawls)]
    if not files:
        sys.exit(f"no host-index files for crawls {crawls}")
    log(f"host index: {len(files)} files for {', '.join(crawls)}")

    duck = duckdb.connect()
    duck.execute("LOAD httpfs; SET threads=2; SET http_retries=10; "
                 "SET http_retry_wait_ms=3000; SET http_retry_backoff=2;")
    ours = [(d,) for (d,) in conn.execute("SELECT domain FROM sites")]
    duck.execute("CREATE TABLE ours (domain VARCHAR)")
    duck.executemany("INSERT INTO ours VALUES (?)", ours)
    agg: dict[str, list[int]] = {}
    for i, f in enumerate(files, 1):
        # One file at a time: the CDN answers 503 to wide parallel range reads.
        for dom, f200, r200 in duck.execute(
            f"SELECT h.url_host_registered_domain, sum(coalesce(h.fetch_200,0)), sum(coalesce(h.robots_200,0)) "
            f"FROM read_parquet('{f}') h JOIN ours o ON o.domain = h.url_host_registered_domain GROUP BY 1"
        ).fetchall():
            a = agg.setdefault(dom, [0, 0])
            a[0] += int(f200)
            a[1] += int(r200)
        if i % 10 == 0 or i == len(files):
            log(f"host index: {i}/{len(files)} files, {len(agg):,} of our domains seen")

    label = ",".join(crawls)
    with conn.transaction():
        conn.execute("CREATE TEMP TABLE t_hi (domain text, f200 bigint, r200 int) ON COMMIT DROP")
        with conn.cursor().copy("COPY t_hi FROM STDIN") as cp:
            for d, (f200, r200) in agg.items():
                cp.write_row((d, f200, r200))
        # Measured-and-absent is 0, not NULL: CC looked at these crawls and logged no fetch.
        conn.execute("UPDATE sites SET cc_fetch_200 = 0, cc_robots_200 = 0, cc_hostindex_crawls = %s", (label,))
        conn.execute("UPDATE sites s SET cc_fetch_200 = t.f200, cc_robots_200 = t.r200 FROM t_hi t WHERE t.domain = s.domain")
    log(f"host index evidence loaded for {len(agg):,} domains")


def cmd_search(conn: psycopg.Connection, q: str, k: int) -> None:
    """Slice-test search (step 3): BM25 over docs, then a popularity blend.

    BM25 alone ranks a 3-word page that repeats the query above the site you
    meant; the blend adds the CC link rank. Weights are
    placeholders until the eval set fits them.
    """
    rows = conn.execute(
        """WITH hits AS (
               SELECT d.domain, d.title, d.description,
                      -(d.search_text <@> to_bm25query(%s, 'docs_bm25')) AS bm25
               FROM docs d WHERE d.error IS NULL
               ORDER BY d.search_text <@> to_bm25query(%s, 'docs_bm25') LIMIT 100)
           SELECT h.domain, h.title, h.description, h.bm25, s.cc_hc_pos,
                  h.bm25 * (1 + 0.10 * greatest(0, 18 - ln(coalesce(s.cc_hc_pos, 100000000)))) AS score
           FROM hits h JOIN sites s USING (domain)
           ORDER BY score DESC LIMIT %s""",
        (q, q, k)).fetchall()
    print(f"\n=== {q}")
    if not rows:
        print("  sites:     (no results)")
    for dom, title, desc, bm25, hc, score in rows:
        print(f"  site {score:6.2f}  bm25 {bm25:5.2f}  link {hc or '-':>9}  {dom:28}  {(title or '')[:50]}")
    if conn.execute("SELECT to_regclass('wiki_docs') IS NOT NULL").fetchone()[0]:
        for title, bm25, lead in conn.execute(
                "SELECT title, -(search_text <@> to_bm25query(%s, 'wiki_docs_bm25')), left(lead, 70) FROM wiki_docs "
                "ORDER BY search_text <@> to_bm25query(%s, 'wiki_docs_bm25') LIMIT 3", (q, q)).fetchall():
            print(f"  wiki        bm25 {bm25:5.2f}  {title[:40]:40}  {lead}")


def cmd_stats(conn: psycopg.Connection) -> None:
    q = {
        "sites": "SELECT count(*) FROM sites",
        "ranked": "SELECT count(*) FROM sites WHERE tranco_rank IS NOT NULL",
        "blocked": "SELECT count(*) FROM sites WHERE blocked_by IS NOT NULL",
        "eligible ranked": "SELECT count(*) FROM sites WHERE tranco_rank IS NOT NULL AND blocked_by IS NULL",
        "in history": "SELECT count(*) FROM sites WHERE history_visits > 0",
        "history-only (unranked)": "SELECT count(*) FROM sites WHERE history_visits > 0 AND tranco_rank IS NULL",
        "hosts": "SELECT count(*) FROM hosts",
        "with CC link rank": "SELECT count(*) FROM sites WHERE cc_hc_pos IS NOT NULL",
        "CC fetched pages (hostidx)": "SELECT count(*) FROM sites WHERE cc_fetch_200 > 0",
        "CC: no pages (hostidx)": "SELECT count(*) FROM sites WHERE cc_fetch_200 = 0",
        "with a CC homepage capture": "SELECT count(DISTINCT domain) FROM cc_captures",
    }
    for label, sql in q.items():
        print(f"{label:>24}: {conn.execute(sql).fetchone()[0]:,}")
    for name, n in conn.execute(
        "SELECT blocked_by, count(*) FROM sites WHERE blocked_by IS NOT NULL GROUP BY 1 ORDER BY 2 DESC"
    ):
        print(f"{'blocked by ' + name:>24}: {n:,}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate")
    sub.add_parser("blocklists")
    h = sub.add_parser("history")
    h.add_argument("file")
    g = sub.add_parser("ccgraph")
    g.add_argument("--release", required=True, help="e.g. cc-main-2026-jun-jul-aug")
    g.add_argument("--hosts-per-domain", type=int, default=50)
    hi = sub.add_parser("cchostindex")
    hi.add_argument("--crawls", required=True, help="comma-separated, e.g. CC-MAIN-2025-13,CC-MAIN-2025-18")
    m = sub.add_parser("ccmap")
    m.add_argument("--crawl", required=True)
    m.add_argument("--subsets", default="warc,crawldiagnostics,robotstxt")
    sc = sub.add_parser("ccscan")
    sc.add_argument("--crawl", required=True)
    sc.add_argument("--subset", default="warc", choices=["warc", "crawldiagnostics", "robotstxt"])
    sc.add_argument("--only-missing", action="store_true", help="gap fill: only files holding sites with no capture yet")
    sc.add_argument("--limit", type=int)
    sc.add_argument("--shard", help="i/n — this worker takes every n-th file (1-based)")
    sc.add_argument("--reset", action="store_true",
                    help="wipe this crawl+subset's results and done-markers first (archive kept)")
    cr = sub.add_parser("ccrobots")
    cr.add_argument("--workers", type=int, default=4)
    cr.add_argument("--limit", type=int)
    cr.add_argument("--rate", type=float, default=8.0, help="max requests/s to CC (their guidance: < 10)")
    cf = sub.add_parser("ccfetch")
    cf.add_argument("--crawl")
    cf.add_argument("--workers", type=int, default=4)
    cf.add_argument("--limit", type=int)
    cf.add_argument("--rate", type=float, default=8.0, help="max requests/s to CC (their guidance: < 10)")
    cf.add_argument("--pages", action="store_true", help="fetch cc_pages (ccpages) into page_docs, not homepages")
    cf.add_argument("--procs", type=int, default=1, help="processes (parsing is CPU-bound; one per core)")
    cf.add_argument("--refetch-stubs", action="store_true",
                    help="page_docs rows whose capture was a stub (bot wall, shell, redirect, under 200 chars): read them "
                         "again from their sites; a stub both ways becomes an error row")
    cf.add_argument("--gov-only", action="store_true", help="with --refetch-stubs: government sites only")
    cf.add_argument("--dry-run", action="store_true", help="with --refetch-stubs: count and list, change nothing")
    cf.add_argument("--retry-errors", action="store_true", help="with --refetch-stubs: rows an earlier pass left as stub errors too")
    cp_ = sub.add_parser("ccpages", help="pages inside the top-N sites from one crawl's URL index -> cc_pages")
    cp_.add_argument("--crawl", required=True)
    cp_.add_argument("--top", type=int, default=10000)
    cp_.add_argument("--per-domain", type=int, default=15)
    cp_.add_argument("--hosts", type=int, default=0, help="also the front page of the top-N tranco_hosts")
    cp_.add_argument("--answer-per-domain", type=int, default=0,
                     help="also up to N answer pages inside each answer site (ccpages.ANSWER_KINDS); 0 = none")
    cp_.add_argument("--answer-groups", help="comma list of answer groups to take (recipe,howto,review,...); default all")
    cp_.add_argument("--gov-per-unit", type=int, default=0,
                     help="also up to N pages for each agency (host + first path level) of a government site; 0 = none")
    cp_.add_argument("--gov-per-domain", type=int, default=0, help="at most N government pages a site; 0 = no cap")
    gs = sub.add_parser("ccgov-scan", help="government page candidates of one crawl -> a file; writes nothing to the database")
    gs.add_argument("--crawl", required=True)
    gs.add_argument("--per-unit", type=int, default=200)
    gs.add_argument("--out", required=True)
    gs.add_argument("--roots", action="store_true", help="also each site's front page (score 99), for linkcrawl.py")
    se = sub.add_parser("search")
    se.add_argument("queries", nargs="+")
    se.add_argument("-k", type=int, default=8)
    wk = sub.add_parser("wiki")
    wk.add_argument("--file", required=True)
    wk.add_argument("--dump", required=True, help="e.g. enwiki-20260901")
    wk.add_argument("--procs", type=int, default=4)
    wk.add_argument("--limit", type=int)
    sr = sub.add_parser("subreddits", help="subreddit directory from jobs/reddit-subs.sh -> subreddits")
    sr.add_argument("--file", required=True)
    wb = sub.add_parser("wayback", help="homepages from the Wayback Machine for the sites table (run on AWS)")
    wb.add_argument("--per-site", type=float, default=5.0)
    wb.add_argument("--limit", type=int)
    al = sub.add_parser("anchors-load", help="anchor text from jobs/cc-anchors.sh -> anchors, anchor_docs")
    al.add_argument("--file", required=True)
    al.add_argument("--meta", help="site_meta.tsv.gz from the same job -> site_meta")
    al.add_argument("--inlinks", help="inlinks.tsv.gz from the same job (or anchors.py merge) -> site_inlinks")
    dq = sub.add_parser("d2q-load", help="model-written queries per site, {domain: [query, ...]} JSON -> d2q_docs")
    dq.add_argument("--file", required=True)
    ql = sub.add_parser("query-list", help="synthetic query list from entities, clickstream, AmazonQAC -> query_list")
    ql.add_argument("--qac", help="amazonqac-terms.tsv.gz from jobs/amazonqac-terms.sh")
    ql.add_argument("--typos", type=int, default=50_000)
    ql.add_argument("--qac-min", type=int, default=20, help="keep AmazonQAC terms searched at least this often")
    cs = sub.add_parser("clickstream", help="readers reaching each Wikipedia article from a search engine (CC0) -> wiki_search_refs")
    cs.add_argument("--month", default="2026-08")
    en = sub.add_parser("entities", help="companies, athletes, foods, chains, celebrities, games, countries, states, "
                                          "vehicles, musicians, news orgs from Wikidata -> entities")
    en.add_argument("--categories", help="comma-separated subset (default: all)")
    fa = sub.add_parser("facts", help="birth/death dates, height, spouse, founding, population … from Wikidata -> wiki_facts")
    fa.add_argument("--facts", help="comma-separated subset (default: all)")
    wp = sub.add_parser("wdc-places", help="businesses from jobs/wdc-places.sh -> places (OSM duplicates skipped)")
    wp.add_argument("--file", required=True)
    wp.add_argument("--zips", help="Census Gazetteer ZCTA file: address-only businesses go to their ZIP's centre")
    pv = sub.add_parser("pageviews", help="English Wikipedia monthly views from the public dump -> wiki_pop.pageviews")
    pv.add_argument("--month", default="2026-08")
    sub.add_parser("wiki-pop", help="how many Wikipedias have each article (Wikidata sitelinks) -> wiki_pop")
    wl_ = sub.add_parser("wiki-links", help="accounts (X, Instagram, IMDb …) + official sites from Wikidata -> wiki_links")
    wl_.add_argument("--platforms", help="comma-separated subset, default all")
    gc = sub.add_parser("gutenberg-catalog", help="Project Gutenberg's catalog matched to book articles by title and author -> wiki_links")
    gc.add_argument("--file", help="pg_catalog.csv already downloaded (default: fetch the offline catalog feed)")
    ci = sub.add_parser("card-images", help="card pictures from Wikidata/Commons, free licences only -> card-images.sqlite")
    ci.add_argument("phase", choices=("meta", "fetch", "todo", "load", "reorder", "seed"))
    ci.add_argument("--db", help="the working file (default $CARD_IMAGES_DB or /card/card-images.sqlite)")
    ci.add_argument("--top", type=int, default=500_000, help="meta: the most-read N articles (default 500000)")
    ci.add_argument("--limit", type=int, default=1_000, help="fetch/todo: articles this run (default 1000)")
    ci.add_argument("--file", help="load: results from card_images.py download (under /import)")
    ci.add_argument("--lead-dump", help="meta: enwiki-*-page_props.sql.gz for each article's free lead image")
    ci.add_argument("--redo", action="store_true", help="todo: re-fetch pictures stored smaller than the box, and ones a smaller cap refused")
    ci.add_argument("--replace", action="store_true", help="load: a picture at least as large replaces the stored one")
    sub.add_parser("youtube", help="YouTube channels from Wikidata (P2397) -> youtube_channels")
    sub.add_parser("quality", help="mark parked/placeholder/error/non-English homepages and pages as junk")
    sub.add_parser("kinds-wikidata", help="instance of / use / industry / genre of every official site -> kw_*")
    kb = sub.add_parser("kinds-build", help="what kind of site each site is, all sources -> site_kinds, kind_docs")
    kb.add_argument("--import", dest="import_dir", default="/import")
    kb.add_argument("--phrases", help="REFUSED: Jev-judged kind phrases (ADR-065, tool deleted) — use --open")
    kb.add_argument("--members", help="REFUSED: Jev verdicts per (kind, site) (ADR-065, tool deleted) — use --open")
    kb.add_argument("--leaders", help="REFUSED: Jev 'first choice' verdicts (ADR-065, tool deleted) — use --open")
    kb.add_argument("--open", dest="open_build", action="store_true",
                    help="the open build (ADR-065): rules over open sources, no Jev files, no Tranco")
    sub.add_parser("kinds-roots", help="which broad Wikidata trees each kind class is under (P279*) -> kw_class_roots")
    ko = sub.add_parser("kinds-open", help="re-run only the open filter/members/lead step over an existing site_kinds_new")
    ko.add_argument("--judge", nargs="*", default=[], help="optional judge output files (utility/search-kinds-judge.py), any model")
    jl = sub.add_parser("junk-load", help="junk filter: Jev homepage verdicts + UT1 unsafe lists -> site_quality")
    jl.add_argument("--quality", help="private site_quality.sql (utility/search-site-quality.py)")
    jl.add_argument("--ut1", help="kinds-unsafe/ut1-unsafe.tsv.gz (site-directories2.sh)")
    sub.add_parser("kinds-country", help="country of each official site's owner (Wikidata P17) -> kw_country")
    stl = sub.add_parser("stocks-load", help="stock/ticker searches: SEC ticker list + Wikidata names -> stock_tickers, stock_keys")
    stl.add_argument("--mappings", required=True, help="sec-cik-mapper mappings/stocks/mappings.csv")
    stl.add_argument("--ir", help="irlinks.py ir.tsv: investor-relations pages found in Common Crawl")
    stl.add_argument("--funds", help="sec-cik-mapper mappings/mutual_funds/mappings.csv (ETFs, mutual funds)")
    stl.add_argument("--words", help="a dictionary (SCOWL american-english): tickers that are words are marked")
    ksv = sub.add_parser("kinds-sitevecs", help="site vectors (title + kinds + description) -> kind_site_vec")
    ksv.add_argument("--file", required=True)
    kv = sub.add_parser("kinds-vecs", help="kind-name embeddings from tools/embed-kinds.py -> kind_vecs")
    kv.add_argument("--file", required=True)
    sub.add_parser("wikidata-sites", help="official websites only in Wikidata (run after wiki-sites)")
    vl = sub.add_parser("vec-load", help="embeddings from tools/embed.py -> <name>_vec + HNSW")
    vl.add_argument("--table", required=True)
    vl.add_argument("--ids", required=True)
    vl.add_argument("--vecs", required=True)
    pl = sub.add_parser("places", help="named places from OpenStreetMap extracts -> places (wdc rows kept)")
    pl.add_argument("--extracts", help="comma-separated Geofabrik paths, the box's own region and those "
                                       "next to it; default: $OSM_EXTRACT")
    sx = sub.add_parser("stackexchange", help="Stack Exchange Q&A (curated English sites) -> se_docs")
    sx.add_argument("--sites", nargs="*", help="default: stackexchange.SITES")
    sa = sub.add_parser("se-authors", help="Stack Exchange attribution: who asked and who answered each se_docs question "
                                           "(Posts.xml + Users.xml of the same archives) -> se_authors")
    sa.add_argument("--sites", nargs="*", help="default: every site in se_docs (a subset replaces the table with those sites)")
    op = sub.add_parser("open-pages", help="answer pages from open dumps (Stack Exchange, Wikibooks, Wikivoyage, "
                                           "MedlinePlus) -> page_docs")
    op.add_argument("--source", required=True, help="stackexchange | wikibooks | wikivoyage | medlineplus")
    op.add_argument("--file", help="the wiki's pages-articles.xml.bz2; a MedlinePlus topic XML (default: NLM's newest)")
    op.add_argument("--min-score", type=int, default=3, help="stackexchange: a question's least score")
    op.add_argument("--per-site", type=int, default=20000, help="stackexchange: a site's most questions, best first")
    op.add_argument("--procs", type=int, default=4)
    op.add_argument("--dump", help="the name the rows carry (default: read from the source or the file name)")
    ws = sub.add_parser("wiki-sites", help="official website per article, from the same dump")
    ws.add_argument("--file", required=True)
    ws.add_argument("--procs", type=int, default=4)
    wl = sub.add_parser("wiki-load")
    wl.add_argument("--articles", required=True)
    wl.add_argument("--redirects", required=True)
    wl.add_argument("--dump", required=True)
    sub.add_parser("stats")
    a = p.parse_args()

    # autocommit: a plain read must not leave a transaction open. ccgraph streams
    # for ~30 min after its first SELECT, and the idle-in-transaction lock it held
    # on `sites` blocked every other job's migrate (ALTER TABLE) until it finished.
    # Writes still group into explicit conn.transaction() blocks.
    with psycopg.connect(autocommit=True) as conn:
        migrate(conn)
        if a.cmd == "blocklists":
            cmd_blocklists(conn)
        elif a.cmd == "history":
            cmd_history(conn, a.file)
        elif a.cmd == "ccgraph":
            cmd_ccgraph(conn, a.release, a.hosts_per_domain)
        elif a.cmd == "cchostindex":
            cmd_cchostindex(conn, a.crawls.split(","))
        elif a.cmd == "ccmap":
            import ccscan
            ccscan.cmd_map(conn, a.crawl, a.subsets.split(","), fetch)
        elif a.cmd == "ccscan":
            import ccscan
            if a.reset:
                ccscan.cmd_reset(conn, a.crawl, a.subset)
            ccscan.cmd_scan(conn, a.crawl, a.subset, a.only_missing, a.limit, a.shard)
        elif a.cmd == "ccrobots":
            import ccfetch
            ccfetch.cmd_robots(conn, a.workers, a.limit, a.rate)
        elif a.cmd == "ccfetch" and a.refetch_stubs:
            import ccfetch
            ccfetch.cmd_refetch_stubs(conn, a.workers, a.limit, a.gov_only, a.dry_run, a.retry_errors)
        elif a.cmd == "ccfetch":
            import ccfetch
            ccfetch.cmd_fetch(conn, a.crawl, a.workers, a.limit, a.rate, a.pages, a.procs)
        elif a.cmd == "ccpages":
            import ccpages
            ccpages.cmd_pages(conn, a.crawl, a.top, a.per_domain, a.hosts, a.answer_per_domain,
                              set(a.answer_groups.split(",")) if a.answer_groups else None,
                              a.gov_per_unit, a.gov_per_domain)
        elif a.cmd == "ccgov-scan":
            import ccpages
            ccpages.cmd_gov_scan(conn, a.crawl, a.per_unit, a.out, a.roots)
        elif a.cmd == "search":
            for q in a.queries:
                cmd_search(conn, q, a.k)
        elif a.cmd == "wiki":
            import wikiparse
            wikiparse.cmd_wiki(conn, a.file, a.dump, a.procs, a.limit)
        elif a.cmd == "subreddits":
            import subreddits
            subreddits.cmd_subreddits(conn, a.file)
        elif a.cmd == "wayback":
            import wayback
            wayback.cmd_wayback(conn, a.per_site, a.limit)
        elif a.cmd == "anchors-load":
            import anchors
            anchors.cmd_load(conn, a.file)
            if a.meta:
                anchors.cmd_load_meta(conn, a.meta)
            if a.inlinks:
                anchors.cmd_load_inlinks(conn, a.inlinks)
        elif a.cmd == "d2q-load":
            import d2q
            d2q.cmd_load(conn, a.file)
        elif a.cmd == "query-list":
            import querylist
            querylist.cmd_query_list(conn, a.qac, a.typos, a.qac_min)
        elif a.cmd == "clickstream":
            import clickstream
            clickstream.cmd_clickstream(conn, a.month)
        elif a.cmd == "facts":
            import facts
            facts.cmd_facts(conn, a.facts.split(",") if a.facts else None)
        elif a.cmd == "entities":
            import entities
            entities.cmd_entities(conn, a.categories.split(",") if a.categories else None)
        elif a.cmd == "wdc-places":
            # 2026-10-02 (owner): Web Data Commons listings are research-only (licensing
            # inventory G3) and left the places table; Overture's open places replace them
            # (utility/places/overture_import.py). The loader stays for research databases.
            if not os.environ.get("SEARCH_WDC_RESEARCH"):
                sys.exit("wdc-places: Web Data Commons listings are research-only and no longer go into a "
                         "serving database (2026-10-02). Set SEARCH_WDC_RESEARCH=1 for a research copy.")
            import wdc
            wdc.cmd_load_places(conn, a.file, a.zips)
        elif a.cmd == "pageviews":
            import pageviews
            pageviews.cmd_pageviews(conn, a.month)
        elif a.cmd == "wiki-pop":
            import wikipop
            wikipop.cmd_wiki_pop(conn)
        elif a.cmd == "wiki-links":
            import wikilinks
            wikilinks.cmd_wiki_links(conn, a.platforms.split(",") if a.platforms else None)
        elif a.cmd == "gutenberg-catalog":
            import gutenberg
            gutenberg.cmd_gutenberg_catalog(conn, a.file)
        elif a.cmd == "card-images":
            import card_images
            db = card_images.open_db(a.db)            # ADR-063: the working file; `conn` only for the bridges
            if a.phase == "meta":
                card_images.cmd_meta(db, conn, a.top, a.lead_dump)
            elif a.phase == "seed":
                card_images.cmd_seed(db, conn)
            elif a.phase == "fetch":
                card_images.cmd_fetch(db, a.limit)
            elif a.phase == "reorder":
                card_images.cmd_reorder(db)
            elif a.phase == "todo":
                card_images.cmd_todo(db, a.limit, a.redo)
            elif not a.file:
                p.error("card-images load needs --file")
            else:
                card_images.cmd_load(db, a.file, a.replace)
        elif a.cmd == "youtube":
            import youtube
            youtube.cmd_youtube(conn)
        elif a.cmd == "quality":
            import quality
            quality.cmd_quality(conn)
        elif a.cmd == "kinds-wikidata":
            import kinds
            kinds.cmd_kinds_wikidata(conn)
        elif a.cmd == "kinds-build":
            import kinds
            kinds.cmd_kinds_build(conn, a.import_dir, a.phrases, a.members, a.leaders, a.open_build)
        elif a.cmd == "kinds-roots":
            import kinds
            kinds.cmd_kinds_roots(conn)
        elif a.cmd == "kinds-open":
            import kinds_open
            kinds_open.cmd_kinds_open(conn, a.judge)
        elif a.cmd == "junk-load":
            import junk
            junk.cmd_junk_load(conn, a.quality, a.ut1)
        elif a.cmd == "kinds-country":
            import kinds
            kinds.cmd_kinds_country(conn)
        elif a.cmd == "stocks-load":
            import stocks
            stocks.cmd_stocks_load(conn, a.mappings, a.ir, a.funds, a.words)
        elif a.cmd == "kinds-sitevecs":
            import kinds
            kinds.cmd_kinds_sitevecs(conn, a.file)
        elif a.cmd == "kinds-vecs":
            import kinds
            kinds.cmd_kinds_vecs(conn, a.file)
        elif a.cmd == "wikidata-sites":
            import wikidata_sites
            wikidata_sites.cmd_wikidata_sites(conn)
        elif a.cmd == "vec-load":
            import vectors
            vectors.cmd_vec_load(conn, a.table, a.ids, a.vecs)
        elif a.cmd == "places":
            import osmplaces
            osmplaces.cmd_places(conn, a.extracts)
        elif a.cmd == "stackexchange":
            import stackexchange
            stackexchange.cmd_stackexchange(conn, a.sites)
        elif a.cmd == "se-authors":
            import stackexchange
            stackexchange.cmd_se_authors(conn, a.sites)
        elif a.cmd == "open-pages":
            import openpages
            openpages.cmd_open_pages(conn, a.source, a.file, a.min_score, a.per_site, a.procs, a.dump)
        elif a.cmd == "wiki-sites":
            import wikisites
            wikisites.cmd_wiki_sites(conn, a.file, a.procs)
        elif a.cmd == "wiki-load":
            import wikiparse
            wikiparse.load_from_files(conn, a.articles, a.redirects, a.dump)
        elif a.cmd == "stats":
            cmd_stats(conn)


if __name__ == "__main__":
    main()
