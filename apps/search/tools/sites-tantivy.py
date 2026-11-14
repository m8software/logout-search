#!/usr/bin/env python3
"""Build the Tantivy indexes search_api's site, page and anchor lists read (SEARCH_SITES_BACKEND=tantivy).

    tools/sites-tantivy.py export-sql KIND [--readable-body]   the COPY statement for one export
    tools/sites-tantivy.py build KIND EXPORT.tsv[.zst] DIR      one index from that export

KIND: sites | pages | anchor_docs | anchors. SEARCH_SITES_INDEX holds one directory per KIND.

Page text (ADR-059): docs.body and page_docs.body enter the index only as page_words() —
each page's words lower-cased, edge punctuation stripped, sorted, counts kept — the same
SQL as serve/box-profile.sql, inlined here so the export runs read-only on a database that
still holds readable text. --readable-body exports the raw body instead: a parity
diagnostic for measuring the engine switch alone. An index built from it must never ship.

Each index mirrors the Postgres BM25 index it replaces, with the same analyzer as the wiki
list (search_api.TantivyWikiText.build_analyzer):
  sites        head = domain words + title + h1 (docs_head_bm25); body = domain words + title
               + description + h1 + page_words(body) (docs_bm25). Every docs row is indexed
               (the Postgres index has them all: same document counts and lengths); err=1
               rows are excluded at query time, as WHERE d.error IS NULL does.
  pages        body = title + description + h1 + page_words(body) (page_docs_bm25), err as above.
  anchor_docs  body = what other sites call the domain (anchor_docs_bm25), with the domain's
               docs row (url, title, description, popularity) copied in; nodoc=1 when the
               domain has no fetched page (ANCHORS_SQL's join drops it after the top 20).
  anchors      exact anchor text -> domain, sources (EXACT_ANCHOR_SQL), only domains with a
               fetched page; url/title/description come from the sites index.
Stored for display: url, title, description[:300] — all a result shows.
"""
import argparse
import importlib.util
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("wiki_tantivy", HERE / "wiki-tantivy.py")
wt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wt)          # copy_unescape, and search_api with a stand-in psycopg
search_api = wt.search_api
AN = search_api.TANTIVY_ANALYZER

# serve/box-profile.sql page_words(), inlined (no function is created on the source database)
PAGE_WORDS = ("(SELECT coalesce(string_agg(w, ' ' ORDER BY w COLLATE \"C\"), '') FROM "
              "(SELECT lower(regexp_replace(x, '^\\W+|\\W+$', '', 'g')) AS w "
              "FROM regexp_split_to_table({col}, '\\s+') AS x) pw_ WHERE w <> '')")
DOMAIN_WORDS = "regexp_replace(d.domain, '[.-]+', ' ', 'g')"

KINDS = ("sites", "pages", "anchor_docs", "anchors")


def export_sql(kind: str, readable_body: bool = False) -> str:
    body = "coalesce({col}, '')" if readable_body else PAGE_WORDS
    if kind == "sites":
        b = body.format(col="d.body")
        sel = (f"SELECT d.domain, d.url, coalesce(d.title, ''), left(coalesce(d.description, ''), 300), "
               f"(d.error IS NOT NULL)::int, "
               f"{DOMAIN_WORDS} || ' ' || coalesce(d.title, '') || ' ' || coalesce(d.h1, ''), "
               f"{DOMAIN_WORDS} || ' ' || coalesce(d.title, '') || ' ' || coalesce(d.description, '') || ' ' "
               f"|| coalesce(d.h1, '') || ' ' || {b}, "
               f"coalesce(s.tranco_rank, 0), coalesce(s.cc_hc_pos, 0) "
               f"FROM docs d LEFT JOIN serve_sites s ON s.domain = d.domain")
    elif kind == "pages":
        b = body.format(col="p.body")
        sel = (f"SELECT p.url, coalesce(p.title, ''), left(coalesce(p.description, ''), 300), "
               f"(p.error IS NOT NULL)::int, "
               f"coalesce(p.title, '') || ' ' || coalesce(p.description, '') || ' ' || coalesce(p.h1, '') "
               f"|| ' ' || {b} FROM page_docs p")
    elif kind == "anchor_docs":
        sel = ("SELECT a.domain, a.body, (d.domain IS NULL)::int, coalesce(d.url, ''), coalesce(d.title, ''), "
               "left(coalesce(d.description, ''), 300), coalesce(s.tranco_rank, 0), coalesce(s.cc_hc_pos, 0) "
               "FROM anchor_docs a LEFT JOIN docs d ON d.domain = a.domain AND d.error IS NULL "
               "LEFT JOIN serve_sites s ON s.domain = a.domain")
    elif kind == "anchors":
        sel = ("SELECT a.anchor, a.domain, a.sources FROM anchors a "
               "WHERE EXISTS (SELECT 1 FROM docs d WHERE d.domain = a.domain AND d.error IS NULL)")
    else:
        raise ValueError(kind)
    return f"COPY ({sel}) TO STDOUT;"


def schema(tantivy, kind: str, analyzer: str = AN):
    sb = tantivy.SchemaBuilder()
    text = dict(stored=False, tokenizer_name=analyzer, index_option="freq")
    if kind == "sites":
        sb.add_text_field("domain", stored=True, tokenizer_name="raw")
        sb.add_text_field("head", **text)
        sb.add_text_field("body", **text)
    elif kind == "pages":
        sb.add_text_field("body", **text)
    elif kind == "anchor_docs":
        sb.add_text_field("domain", stored=True, tokenizer_name="raw")
        sb.add_text_field("body", **text)
    elif kind == "anchors":
        sb.add_text_field("anchor", stored=False, tokenizer_name="raw")
        sb.add_text_field("domain", stored=True, tokenizer_name="raw")
        sb.add_integer_field("sources", stored=True, fast=True)
        return sb.build()
    if kind in ("sites", "pages"):
        sb.add_integer_field("err", stored=False, indexed=True)
    if kind == "anchor_docs":
        sb.add_integer_field("nodoc", stored=True)
    for f in ("url", "title", "desc"):               # display only: stored, not searched
        sb.add_bytes_field(f, stored=True, indexed=False)
    if kind in ("sites", "anchor_docs"):
        sb.add_integer_field("tranco", stored=True)
        sb.add_integer_field("cchc", stored=True)
    return sb.build()


def document(tantivy, kind: str, f: list[str]):
    d = tantivy.Document()
    if kind == "sites":
        domain, url, title, desc, err, head, body, tranco, cchc = f
        d.add_text("domain", domain); d.add_text("head", head); d.add_text("body", body)
        d.add_integer("err", int(err))
    elif kind == "pages":
        url, title, desc, err, body = f
        d.add_text("body", body); d.add_integer("err", int(err))
    elif kind == "anchor_docs":
        domain, body, nodoc, url, title, desc, tranco, cchc = f
        d.add_text("domain", domain); d.add_text("body", body); d.add_integer("nodoc", int(nodoc))
    else:
        anchor, domain, sources = f
        d.add_text("anchor", anchor); d.add_text("domain", domain); d.add_integer("sources", int(sources))
        return d
    for name, v in (("url", url), ("title", title), ("desc", desc)):
        d.add_bytes(name, v.encode("utf-8"))
    if kind in ("sites", "anchor_docs"):
        d.add_integer("tranco", int(tranco)); d.add_integer("cchc", int(cchc))
    return d


WIDTH = {"sites": 9, "pages": 5, "anchor_docs": 8, "anchors": 3}


def rows(path: str, kind: str):
    import io
    import subprocess
    if path.endswith(".zst"):
        z = subprocess.Popen(["zstd", "-dc", path], stdout=subprocess.PIPE)
        fh = io.TextIOWrapper(z.stdout, encoding="utf-8", errors="replace")
    else:
        fh = open(path, encoding="utf-8", errors="replace")
    for line in fh:
        parts = line.rstrip("\n").split("\t")
        if len(parts) == WIDTH[kind]:
            yield [wt.copy_unescape(p) for p in parts]


def build(kind: str, export: str, path: str, heap_mb: int = 1000, threads: int = 2, analyzer: str = AN) -> int:
    import os
    import tantivy
    os.makedirs(path, exist_ok=True)
    idx = tantivy.Index(schema(tantivy, kind, analyzer), path=path)
    idx.register_tokenizer(analyzer, search_api.TantivyWikiText.build_analyzer(tantivy, analyzer))
    w = idx.writer(heap_size=heap_mb * 1_000_000, num_threads=threads)
    n = 0
    for f in rows(export, kind):
        w.add_document(document(tantivy, kind, f))
        n += 1
    w.commit()
    w.wait_merging_threads()
    with open(os.path.join(path, search_api.ANALYZER_FILE), "w") as f:   # search_api reads it
        f.write(analyzer + "\n")
    return n


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("export-sql")
    e.add_argument("kind", choices=KINDS)
    e.add_argument("--readable-body", action="store_true", help="raw page text: a parity diagnostic, never ships")
    b = sub.add_parser("build")
    b.add_argument("kind", choices=KINDS)
    b.add_argument("export")
    b.add_argument("dir")
    b.add_argument("--heap-mb", type=int, default=1000)
    b.add_argument("--threads", type=int, default=2)
    b.add_argument("--analyzer", choices=search_api.TANTIVY_ANALYZERS, default=AN,
                   help="en_pg: simple tokenizer; en_pg2: keeps dotted/@ tokens whole like Postgres")
    a = ap.parse_args()
    if a.cmd == "export-sql":
        print(export_sql(a.kind, a.readable_body))
        return
    t0 = time.time()
    n = build(a.kind, a.export, a.dir, a.heap_mb, a.threads, a.analyzer)
    print(f"{a.kind}: {n} rows indexed into {a.dir} in {time.time() - t0:.0f} s")


if __name__ == "__main__":
    main()
