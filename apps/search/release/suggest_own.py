#!/usr/bin/env python3
"""Rebuild the suggestion tables of a release's database COPY from our own site rank (rc5 plan W2).

    suggest_own.py --own-rank own_rank.tsv [--blocked blocked.tsv] [--refs wiki_search_refs.tsv[.zst]]
                   [--entities entities.tsv[.zst]] [--suggest-sql ../serve/suggest.sql]      (PG* environment)

Found 2026-10-07: which sites can be suggested at all, their keys and the lists for 3- and 4-character prefixes were
the build database's: `tranco_rank <= 100000 OR history_visits > 0`, ordered by a score with a browser-history lift
(serve/suggest.sql). The release copied them verbatim, so Tranco and one browser's history still chose what a box
suggests, although no Tranco or history column ships.

This runs serve/suggest.sql UNCHANGED on the copy, with the tables it reads for the site rank replaced, for this
session only, by stand-ins made from own_rank.tsv (a TEMP table is found before the real one of the same name):

  sites      one row per own-rank host that is its own registrable domain; the column suggest.sql reads as
             `tranco_rank` holds OUR rank; history_visits is 0 for every row; blocked_by from blocked.tsv
  host_nav   the copy's rows whose host is in own_rank.tsv, with our rank of that host
  cc_domain_crawl   empty when the copy has none (suggest.sql's plumbing gate is then off; own_rank.py already
             leaves plumbing hosts out)

Run on a COPY only (buildbox): suggest.sql drops and recreates suggest_items / suggest_keys / suggest_top. Everything is
one transaction: a failure leaves the old tables.

suggest.sql also reads two tables a serving-profile copy does not carry; without them every article's demand is 0
and no description or alias key is built, so this refuses to run unless they are there or given as files:
  wiki_search_refs (title, searches)   Wikipedia clickstream, CC0
  entities                             Wikidata entity lists, CC0

It ends by writing suggest_meta (key, value), which the lookup builder copies into lookup.sqlite's meta and
`release_v1.py check --tranco-free` reads.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import sys

SITE_CUT = 100000          # suggest.sql's own cut ("<= 100000"), now on our rank; rank_pop and the demand curve reach 0 there
HERE = os.path.dirname(os.path.abspath(__file__))
EXTRA = {"wiki_search_refs": "CREATE TABLE wiki_search_refs (title text PRIMARY KEY, searches integer)",
         "entities": "CREATE TABLE entities (category text, qid text, name text, aliases text[], sitelinks integer, "
                     "extra text, descr text, wiki text, PRIMARY KEY (category, qid))"}


def log(msg: str) -> None:
    print(f"[suggest-own] {msg}", flush=True)


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for blk in iter(lambda: f.read(1 << 20), b""):
            h.update(blk)
    return h.hexdigest()


def read_own(path: str) -> list:
    """own_rank.tsv -> [(host, domain, rank)]; a malformed line aborts (release_v1.read_own_rank's rule)."""
    rows, seen = [], set()
    with open(path, encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            p = line.rstrip("\n").split("\t")
            if len(p) != 5 or not p[2].isdigit() or int(p[2]) < 1 or p[0] in seen:
                sys.exit(f"{path}: line {n} is not 'host domain own_rank cc_pos lists' (or repeats a host) — refusing")
            seen.add(p[0])
            rows.append((p[0], p[1], int(p[2])))
    if not rows:
        sys.exit(f"{path}: no rows — refusing")
    return rows


def read_blocked(path: str) -> list:
    out = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            d, _, lst = line.rstrip("\n").partition("\t")
            if d:
                out.setdefault(d, lst or "blocked")
    return sorted(out.items())


def has_table(conn, name: str) -> bool:
    return conn.execute("SELECT to_regclass(%s) IS NOT NULL", ("public." + name,)).fetchone()[0]


def load_extra(conn, name: str, path: str) -> int:
    """A COPY-text TSV (optionally .zst) -> the table, created here. Returns the row count."""
    conn.execute(EXTRA[name])
    src = subprocess.Popen(["zstd", "-dc", path], stdout=subprocess.PIPE).stdout if path.endswith(".zst") else open(path, "rb")
    with src, conn.cursor().copy(f"COPY {name} FROM STDIN") as cp:
        for blk in iter(lambda: src.read(1 << 20), b""):
            cp.write(blk)
    return conn.execute(f"SELECT count(*) FROM {name}").fetchone()[0]


def stand_ins(conn, own: list, blocked: list) -> None:
    conn.execute("CREATE TEMP TABLE sg_own (host text PRIMARY KEY, domain text, rank integer)")
    with conn.cursor().copy("COPY sg_own FROM STDIN") as cp:
        for r in own:
            cp.write_row(r)
    # `tranco_rank` is only the column name suggest.sql reads: the value is our rank
    conn.execute("CREATE TEMP TABLE sites (domain text PRIMARY KEY, tranco_rank integer, history_visits integer, blocked_by text)")
    conn.execute("INSERT INTO sites (domain, tranco_rank, history_visits) SELECT host, rank, 0 FROM sg_own WHERE host = domain")
    conn.execute("CREATE TEMP TABLE sg_blocked (domain text PRIMARY KEY, lst text)")
    with conn.cursor().copy("COPY sg_blocked FROM STDIN") as cp:
        for r in blocked:
            cp.write_row(r)
    conn.execute("INSERT INTO sites (domain, blocked_by) SELECT domain, lst FROM sg_blocked "
                 "ON CONFLICT (domain) DO UPDATE SET blocked_by = EXCLUDED.blocked_by")
    if has_table(conn, "host_nav"):
        conn.execute("CREATE TEMP TABLE host_nav AS SELECT h.key, h.url, h.title, h.description, o.rank "
                     "FROM public.host_nav h JOIN sg_own o ON o.host = lower(split_part(h.url, '/', 3))")
    if not has_table(conn, "cc_domain_crawl"):
        conn.execute("CREATE TEMP TABLE cc_domain_crawl (domain text, html_200 integer, redirects integer)")
    conn.execute("ANALYZE sites")


def verify(conn) -> dict:
    """What the result must satisfy; any violation aborts the transaction."""
    bad = conn.execute("SELECT count(*), min(i.domain) FROM suggest_items i LEFT JOIN sg_own o ON o.host = i.domain "
                       "WHERE i.kind = 'site' AND (o.rank IS NULL OR o.rank > %s)", (SITE_CUT,)).fetchone()
    if bad[0]:
        sys.exit(f"{bad[0]} site suggestions are outside our rank's top {SITE_CUT:,} (e.g. {bad[1]}) — refusing")
    svc = conn.execute("SELECT count(*), min(i.url) FROM suggest_items i LEFT JOIN sg_own o "
                       "ON o.host = lower(split_part(i.url, '/', 3)) WHERE i.kind = 'service' AND (o.rank IS NULL OR o.rank > %s)",
                       (SITE_CUT,)).fetchone()
    if svc[0]:
        sys.exit(f"{svc[0]} service suggestions are outside our rank's top {SITE_CUT:,} (e.g. {svc[1]}) — refusing")
    hist = conn.execute("SELECT count(*) FROM suggest_items WHERE hist <> 0").fetchone()[0]
    if hist:
        sys.exit(f"{hist} suggestions carry a browser-history lift — refusing")
    blk = conn.execute("SELECT count(*), min(i.domain) FROM suggest_items i JOIN sg_blocked b ON b.domain = i.domain "
                       "WHERE i.kind = 'site'").fetchone()
    if blk[0]:
        sys.exit(f"{blk[0]} site suggestions are on a block list (e.g. {blk[1]}) — refusing")
    return {k: n for k, n in conn.execute("SELECT kind, count(*) FROM suggest_items GROUP BY 1 ORDER BY 1")}


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--own-rank", required=True)
    ap.add_argument("--blocked", help="blocked.tsv (own_rank.py blocklist): domain TAB list")
    ap.add_argument("--refs", help="wiki_search_refs as COPY text (.zst allowed); loaded when the copy has no such table")
    ap.add_argument("--entities", help="entities as COPY text (.zst allowed); loaded when the copy has no such table")
    ap.add_argument("--suggest-sql", default=os.path.join(HERE, "..", "serve", "suggest.sql"))
    ap.add_argument("--allow-missing", action="store_true",
                    help="run without wiki_search_refs / entities (articles get demand 0, no descriptions, no alias keys): tests only")
    a = ap.parse_args(argv)
    import psycopg
    own = read_own(a.own_rank)
    blocked = read_blocked(a.blocked) if a.blocked else []
    sql = open(a.suggest_sql, encoding="utf-8").read()
    conn = psycopg.connect()
    conn.execute("SET statement_timeout = 0")
    before = ({k: n for k, n in conn.execute("SELECT kind, count(*) FROM suggest_items GROUP BY 1 ORDER BY 1")}
              if has_table(conn, "suggest_items") else {})
    loaded = {}
    for name, path in (("wiki_search_refs", a.refs), ("entities", a.entities)):
        if not has_table(conn, name) and path:
            loaded[name] = load_extra(conn, name, path)
            log(f"{name}: {loaded[name]:,} rows loaded from {path}")
        n = conn.execute(f"SELECT count(*) FROM {name}").fetchone()[0] if has_table(conn, name) else 0
        if not n and not a.allow_missing:
            sys.exit(f"{name}: not in this database and no file given (--{'refs' if name == 'wiki_search_refs' else 'entities'}): "
                     "articles would lose their demand, descriptions and alias keys — refusing")
        loaded[name] = n
    stand_ins(conn, own, blocked)
    log(f"stand-ins: {conn.execute('SELECT count(*) FROM sites WHERE tranco_rank IS NOT NULL').fetchone()[0]:,} ranked sites, "
        f"{len(blocked):,} blocked; running {os.path.basename(a.suggest_sql)}")
    conn.execute(sql)
    after = verify(conn)
    meta = {"rank_source": "own_rank", "own_rank_sha256": sha256_file(a.own_rank), "site_cut": str(SITE_CUT),
            "history": "none", "wiki_search_refs_rows": str(loaded["wiki_search_refs"]), "entities_rows": str(loaded["entities"])}
    conn.execute("DROP TABLE IF EXISTS public.suggest_meta")
    conn.execute("CREATE TABLE public.suggest_meta (key text PRIMARY KEY, value text)")
    conn.cursor().executemany("INSERT INTO public.suggest_meta VALUES (%s, %s)", sorted(meta.items()))
    conn.commit()
    log(f"items before {before}")
    log(f"items after  {after}")
    log(f"keys {conn.execute('SELECT count(*) FROM suggest_keys').fetchone()[0]:,}, "
        f"prefix lists {conn.execute('SELECT count(*) FROM suggest_top').fetchone()[0]:,}")
    log("SUGGEST-OWN-OK")


if __name__ == "__main__":
    main()
