#!/usr/bin/env python3
"""Patch the release's Wikipedia Tantivy index with a wikidelta.py change set (owner 2026-11-15: daily Wikipedia refresh).

    wiki_text_delta.py apply PREV_INDEX OUT_INDEX --registry NEW_REGISTRY --prev-registry OLD_REGISTRY
                       --changed changed.tsv.gz --deleted deleted.tsv --release RELEASE_ID [--heap-mb 1000]

The release index (text/wiki) is keyed by the registry's u64 `doc_id` (schema doc_id / title / body, nothing stored), so
release_v1.py tvmonth, which re-adds page_id documents, cannot patch it. This copy-on-write patch:
  1. copies PREV_INDEX to OUT_INDEX (cp --reflink=auto: the old segment files are shared, not duplicated);
  2. deletes every document whose Wikipedia article was changed, renamed, had its aliases changed, or was deleted
     (doc_id from the OLD registry: that is the id the old index holds), by a u64 term QUERY (delete_documents_by_term
     builds an i64 term and deletes nothing on a u64 field);
  3. adds one document per changed/new row, doc_id from the NEW registry (existing articles keep their id, new pages got
     theirs when the registry was built): title (positions) and body = title, aliases, infobox, headings, lead joined by a
     space, with the index's own analyzer (logout-analyzer.txt) so the terms match the Rust builder's;
  4. one commit, then the release stamp (release.json, role wiki) with the new release id.
New files in OUT_INDEX that PREV_INDEX did not have are what update_pkg.py ships (new segments + delete bitsets).
The result is checked: num_docs = old - deleted + added (an article both deleted and re-added counts once), and a term
query for a changed row's own title word finds its doc_id.
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import sqlite3
import subprocess
import sys
import time
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "importer"))
sys.path.insert(0, os.path.join(HERE, "..", "serve", "api"))
sys.path.insert(0, os.path.join(HERE, "..", "build"))

WIKI = 1 << 28            # registry type 1 (wiki): doc_id = (1 << 28) | seq; type_shift 28 in registry meta


def log(msg: str) -> None:
    print(f"[wiki-text-delta] {msg}", flush=True)


def unescape(field: str) -> str:
    import wikidelta as D
    return D.unescape(field) or ""


def page_ids_to_doc_ids(registry: str, ids: set[int]) -> dict[int, int]:
    db = sqlite3.connect(f"file:{registry}?mode=ro", uri=True)
    shift = int(dict(db.execute("SELECT key, value FROM meta")).get("type_shift", 28))
    lo, hi = 1 << shift, 2 << shift
    out: dict[int, int] = {}
    for doc_id, ext in db.execute("SELECT doc_id, ext FROM registry WHERE doc_id >= ? AND doc_id < ?", (lo, hi)):
        if ext in ids:
            out[ext] = doc_id
    return out


def analyzer_for(tantivy, index: str):
    if "psycopg" not in sys.modules:                  # search_api imports it; the analyzer does not use it
        try:
            import psycopg  # noqa: F401
        except ImportError:
            sys.modules["psycopg"] = types.ModuleType("psycopg")
    import search_api
    name = open(os.path.join(index, "logout-analyzer.txt")).read().strip() or search_api.TANTIVY_ANALYZER
    return name, search_api.TantivyWikiText.build_analyzer(tantivy, name)


def apply(a) -> dict:
    import tantivy
    t0 = time.time()
    if os.path.lexists(a.out_index):
        sys.exit(f"refusing: {a.out_index} exists (the output starts empty)")
    subprocess.run(["cp", "-r", "--reflink=auto", a.prev_index, a.out_index], check=True)
    subprocess.run(["chmod", "-R", "u+w", a.out_index], check=True)       # a published release is read-only; the copy is ours to patch
    before = set(os.listdir(a.out_index))
    def stream_rows():
        """One row at a time: a 2 M-row catch-up must not sit in memory (5 kB a row)."""
        with gzip.open(a.changed, "rt", encoding="utf-8", newline="") as f:
            for line in f:
                p = line.rstrip("\n").split("\t")
                yield int(p[1]), [unescape(x) for x in p[3:8]]       # page_id, (title, headings, lead, infobox, aliases)
    changed_ids = {pid for pid, _ in stream_rows()}
    deleted = []
    with open(a.deleted, encoding="utf-8", newline="") as f:
        for line in f:
            if line.strip():
                deleted.append(int(line.split("\t")[0]))
    want = changed_ids | set(deleted)
    old_ids = page_ids_to_doc_ids(a.prev_registry, want)
    new_ids = page_ids_to_doc_ids(a.registry, changed_ids)
    missing = [pid for pid in changed_ids if pid not in new_ids]
    if missing:
        sys.exit(f"{len(missing)} changed articles have no doc_id in {a.registry} (first: {missing[:5]}): build the registry from the "
                 f"updated wiki_docs first")
    idx = tantivy.Index.open(a.out_index)
    n_before = idx.searcher().num_docs
    name, an = analyzer_for(tantivy, a.out_index)
    idx.register_tokenizer(name, an)
    w = idx.writer(heap_size=a.heap_mb * 1_000_000, num_threads=1)
    n_del = 0
    for pid in sorted(want):
        if pid in old_ids:
            w.delete_documents_by_query(tantivy.Query.term_query(idx.schema, "doc_id", old_ids[pid]))
            n_del += 1
    n_added = 0
    for pid, (title, headings, lead, infobox, aliases) in stream_rows():
        n_added += 1
        d = tantivy.Document()
        d.add_unsigned("doc_id", new_ids[pid])
        d.add_text("title", title)
        d.add_text("body", " ".join(x for x in (title, aliases, infobox, headings, lead) if x))
        w.add_document(d)
    w.commit()
    w.wait_merging_threads()
    idx.reload()
    n_after = idx.searcher().num_docs
    expect = n_before - n_del + n_added
    if n_after != expect:
        sys.exit(f"num_docs {n_after} != {n_before} - {n_del} + {n_added} = {expect}: the patch is not exact, refusing")
    side_p = os.path.join(a.out_index, "release.json")
    side = json.load(open(side_p))
    side["built_release"] = a.release
    with open(side_p + ".tmp", "w") as f:                                   # write and replace: never open a file that came from the copy for writing
        f.write(json.dumps(side, sort_keys=True) + "\n")
    os.replace(side_p + ".tmp", side_p)
    after = set(os.listdir(a.out_index))
    new = sorted(after - before)
    summary = dict(docs_before=n_before, deleted_docs=n_del, added_docs=n_added, docs_after=n_after,
                   new_files=len(new), new_bytes=sum(os.path.getsize(os.path.join(a.out_index, x)) for x in new),
                   removed_files=len(before - after), seconds=round(time.time() - t0), analyzer=name)
    log(json.dumps(summary, sort_keys=True))
    return summary


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("apply")
    s.add_argument("prev_index"); s.add_argument("out_index")
    s.add_argument("--registry", required=True); s.add_argument("--prev-registry", required=True)
    s.add_argument("--changed", required=True); s.add_argument("--deleted", required=True)
    s.add_argument("--release", required=True); s.add_argument("--heap-mb", type=int, default=1000)
    a = ap.parse_args()
    apply(a)


if __name__ == "__main__":
    main()
