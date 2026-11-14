#!/usr/bin/env python3
"""Add fetched government pages to a COPY of a release root (experiment). DEV ONLY — never ships.

    overlay.py ROOT PAGES.jsonl.gz --arm small|medium

ROOT must be a throwaway copy holding the marker file .gov-overlay (made by arms.sh): its registry.sqlite
gains one page row per new page (type "page": host origin + path, title) and its text/sites/pages index
one document per page with the text a release holds for a page (title + description + h1 + the page's
words, lower-cased and sorted: ADR-059). --arm small takes only the small budget's pages; medium takes all.
Run in the engine image (tantivy, search_api on the path). Nothing here is how a release is built: no rank
cut, no opt-out list, no licence file — it measures what the pages would do if they were there.
"""
import argparse
import gzip
import json
import os
import re
import sqlite3
import sys
from urllib.parse import urlsplit

import tantivy

import search_api

_EDGE = re.compile(r"^\W+|\W+$")


def page_words(text: str) -> str:
    """serve/box-profile.sql page_words(): each word lower-cased, edge punctuation stripped, sorted, counts kept."""
    return " ".join(sorted(w for w in (_EDGE.sub("", x).lower() for x in (text or "").split()) if w))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("root"); ap.add_argument("pages"); ap.add_argument("--arm", choices=("small", "medium"), required=True)
    a = ap.parse_args()
    if not os.path.exists(os.path.join(a.root, ".gov-overlay")):
        sys.exit(f"{a.root} has no .gov-overlay marker: this only writes to a throwaway copy")
    reg = sqlite3.connect(os.path.join(a.root, "registry.sqlite"))
    meta = dict(reg.execute("SELECT key, value FROM meta"))
    base = json.loads(meta["types"])["page"] << int(meta["type_shift"])
    top = base + (1 << int(meta["type_shift"]))
    nxt = (reg.execute("SELECT max(doc_id) FROM registry WHERE doc_id >= ? AND doc_id < ?", (base, top)).fetchone()[0] or base - 1) + 1
    hosts = {o: i for i, o in reg.execute("SELECT id, origin FROM hosts")}
    key = lambda origin, path: (urlsplit(origin).hostname.removeprefix("www."), path)
    have = {key(o, p) for o, p in reg.execute(
        "SELECT h.origin, r.path FROM registry r JOIN hosts h ON h.id = r.host_id WHERE r.doc_id >= ? AND r.doc_id < ?", (base, top))}
    n0 = len(have)
    path = os.path.join(a.root, "text", "sites", "pages")
    # Opened in place, not through search_api._TvIndex: that opens a folder without lock files through a cache
    # folder of symlinks (tv_open.py), and a writer there commits beside the index, not into it (2026-10-12).
    name = open(os.path.join(path, search_api.ANALYZER_FILE)).read().strip()
    index = tantivy.Index.open(path)
    index.register_tokenizer(name, search_api.TantivyWikiText.build_analyzer(tantivy, name))
    docs0 = index.searcher().num_docs
    w = index.writer(heap_size=256 << 20, num_threads=1)
    stats = {"read": 0, "failed": 0, "other arm": 0, "empty": 0, "already there": 0, "added": 0}
    with gzip.open(a.pages, "rt") as f:
        for line in f:
            p = json.loads(line)
            stats["read"] += 1
            if "error" in p or p.get("status") not in (200, "200", None):
                stats["failed"] += 1
                continue
            if a.arm == "small" and p["arm"] != "small":
                stats["other arm"] += 1
                continue
            title, text = (p.get("title") or "").strip(), p.get("text") or ""
            if not title or len(text) < 200:              # a page with no title or no text answers nothing
                stats["empty"] += 1
                continue
            u = urlsplit(p["url"])
            origin, upath = f"{u.scheme}://{u.netloc}", u.path
            if key(origin, upath) in have:
                stats["already there"] += 1
                continue
            have.add(key(origin, upath))
            if origin not in hosts:
                hosts[origin] = reg.execute("INSERT INTO hosts (origin) VALUES (?)", (origin,)).lastrowid
            reg.execute("INSERT INTO registry (doc_id, host_id, path, title) VALUES (?, ?, ?, ?)", (nxt, hosts[origin], upath, title[:300]))
            doc = tantivy.Document()
            doc.add_unsigned("doc_id", nxt)               # a keyword argument would make it signed: the field is u64
            doc.add_text("body", " ".join((title, p.get("description") or "", p.get("h1") or "", page_words(text))))
            w.add_document(doc)
            nxt += 1
            stats["added"] += 1
    w.commit()
    w.wait_merging_threads()
    reg.commit()
    index.reload()
    docs = tantivy.Index.open(path).searcher().num_docs
    if docs != docs0 + stats["added"]:
        sys.exit(f"the index holds {docs} documents, expected {docs0} + {stats['added']}")
    print(json.dumps({"arm": a.arm, "pages before": n0, **stats, "pages now": n0 + stats["added"], "index documents": docs}))


if __name__ == "__main__":
    main()
