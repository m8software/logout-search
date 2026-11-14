#!/usr/bin/env python3
"""Build the Tantivy index search_api's Wikipedia text list reads (SEARCH_WIKI_BACKEND=tantivy).

    tools/wiki-tantivy.py build EXPORT.tsv[.zst] DIR [--heap-mb 2000] [--threads 3]

EXPORT is `COPY (SELECT page_id, title, aliases, infobox, headings, lead FROM wiki_docs) TO
STDOUT` (text format), optionally zstd-compressed. The index mirrors the Postgres expression
index of WIKI_SQL: one `body` field = title, aliases, infobox, headings, lead, analysed the way
to_bm25query analyses (search_api.TantivyWikiText.build_analyzer). Stored: page_id, title and
snippet = lead[:300] (all the result shows). Phase 1 numbers (testbox, 2026-10-01): 6,790,408
articles, 3.2 GB, top-10 overlap with Postgres 0.946. A build-side job: it needs ~6 GB of RAM.
"""
import argparse
import io
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "serve" / "api"))
if "psycopg" not in sys.modules:                  # search_api imports it; not used here
    import types
    sys.modules["psycopg"] = types.ModuleType("psycopg")
import search_api  # noqa: E402

_COPY_ESC = re.compile(r"\\(.)")
_COPY_MAP = {"t": "\t", "n": "\n", "r": "\r", "b": "\b", "f": "\f", "v": "\v", "\\": "\\"}


def copy_unescape(field: str) -> str:
    """One COPY text-format field: \\N is NULL (''), backslash escapes decoded in one pass."""
    if field == "\\N":
        return ""
    return _COPY_ESC.sub(lambda m: _COPY_MAP.get(m.group(1), m.group(1)), field)


def rows(path: str):
    if path.endswith(".zst"):
        z = subprocess.Popen(["zstd", "-dc", path], stdout=subprocess.PIPE)
        f = io.TextIOWrapper(z.stdout, encoding="utf-8", errors="replace")
    else:
        f = open(path, encoding="utf-8", errors="replace")
    for line in f:
        parts = line.rstrip("\n").split("\t")
        if len(parts) == 6:
            yield (int(parts[0]), *(copy_unescape(p) for p in parts[1:]))


def schema(tantivy):
    sb = tantivy.SchemaBuilder()
    sb.add_integer_field("page_id", stored=True, indexed=True, fast=True)
    sb.add_text_field("title", stored=True, tokenizer_name=search_api.TANTIVY_ANALYZER, index_option="position")
    sb.add_text_field("body", stored=False, tokenizer_name=search_api.TANTIVY_ANALYZER, index_option="freq")
    sb.add_bytes_field("snippet", stored=True, indexed=False)
    return sb.build()


def document(tantivy, pid, title, aliases, infobox, headings, lead):
    d = tantivy.Document()
    d.add_integer("page_id", pid)
    d.add_text("title", title)
    d.add_text("body", " ".join(x for x in (title, aliases, infobox, headings, lead) if x))
    d.add_bytes("snippet", lead[:300].encode("utf-8"))
    return d


def build(export: str, path: str, heap_mb: int = 2000, threads: int = 3) -> int:
    import tantivy
    os.makedirs(path, exist_ok=True)
    idx = tantivy.Index(schema(tantivy), path=path)
    idx.register_tokenizer(search_api.TANTIVY_ANALYZER, search_api.TantivyWikiText.build_analyzer(tantivy))
    w = idx.writer(heap_size=heap_mb * 1_000_000, num_threads=threads)
    n = 0
    for r in rows(export):
        w.add_document(document(tantivy, *r))
        n += 1
    w.commit()
    w.wait_merging_threads()
    return n


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("export")
    b.add_argument("dir")
    b.add_argument("--heap-mb", type=int, default=2000)
    b.add_argument("--threads", type=int, default=3)
    a = ap.parse_args()
    t0 = time.time()
    n = build(a.export, a.dir, a.heap_mb, a.threads)
    print(f"{n} articles indexed into {a.dir} in {time.time() - t0:.0f} s")


if __name__ == "__main__":
    main()
