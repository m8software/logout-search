#!/usr/bin/env python3
"""The typo index (text/typo, Tantivy fuzzy over the suggestion keys) for month N+1 as a SEGMENT DELTA on month N's.

    typo_month.py OLD_TYPO_DIR OLD_LOOKUP.sqlite NEW_LOOKUP.sqlite OUT_TYPO_DIR RELEASE_ID [--max-segments 6] [--max-deleted 0.25] [--max-changed 0.4]

build.py typo() writes the index whole every month: one sorted segment of every DISTINCT suggest_keys.key. Whole, a one-key change
re-ships ~32 bytes per key of 3.18 M keys (103 MB, lookups audit). This builds the next month the way the wiki index is built
(release_v1.py tvmonth): a COPY of N's index, one delete-by-term for every key that left, one new segment holding the keys that
arrived, one commit, then release_v1.tv_normalize. The old segment's files are untouched, so update_pkg ships only the new segment
and the delete bitset (the old segment is a `link`, content-addressed); the engine needs nothing new (Tantivy reads any number of
segments, and `num_docs` skips deleted ones).

RULES
- The old keys and the new keys are read from the two releases' lookup.sqlite (suggest_keys), never from the index.
- Tantivy merges on its own at the commit after an index has 8 segments of similar size, and the merged bytes differ from run to
  run (measured: same inputs twice, month 8 differs; months 1-7 are identical once meta.json's segment order is fixed,
  update_common.tv_sort_segments). So this tool never lets the index reach that: when the next month would take it
  past --max-segments (default 6, the hard limit is 8), or the deleted share of the documents is past --max-deleted, or more than --max-changed of the
  keys changed (the delta would be most of the file anyway), it REBUILDS instead: the same single sorted segment build.py typo()
  writes (`full_typo`), which ships whole (about 103 MB). Measured numbers: docs/internal/SEARCH-UPDATE-PACKAGE.md section 6.
- The result is reproducible: N is canonical (tv_normalize), the operations are in sorted key order on one writer thread, and
  tv_normalize renames each segment from the hash of its own files. Same inputs, same bytes.
"""
import argparse
import json
import os
import shutil
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import update_common as uc  # noqa: E402


def _rv():
    """release_v1 imports psycopg at module level; no database is used here."""
    import types
    try:
        import psycopg  # noqa: F401
    except ImportError:
        m = types.ModuleType("psycopg")
        m.Error = Exception
        sys.modules["psycopg"] = m
    import release_v1
    return release_v1


def key_diff(old_lookup: str, new_lookup: str):
    """(removed keys, added keys, keys in the new release): DISTINCT suggest_keys.key, sorted, as build.py typo() reads them."""
    db = sqlite3.connect(":memory:")
    db.execute("ATTACH ? AS o", (f"file:{old_lookup}?mode=ro",))
    db.execute("ATTACH ? AS n", (f"file:{new_lookup}?mode=ro",))
    rm = [r[0] for r in db.execute("SELECT key FROM (SELECT DISTINCT key FROM o.suggest_keys) WHERE key NOT IN (SELECT key FROM n.suggest_keys) ORDER BY key")]
    ad = [r[0] for r in db.execute("SELECT key FROM (SELECT DISTINCT key FROM n.suggest_keys) WHERE key NOT IN (SELECT key FROM o.suggest_keys) ORDER BY key")]
    total = db.execute("SELECT count(*) FROM (SELECT DISTINCT key FROM n.suggest_keys)").fetchone()[0]
    db.close()
    return rm, ad, total


def _schema():
    import tantivy
    sb = tantivy.SchemaBuilder()
    sb.add_text_field("key", stored=True, tokenizer_name="raw", index_option="basic")
    return sb.build()


def full_typo(keys, out: str, release: str) -> dict:
    """The whole index, exactly what build.py typo() writes (one thread, sorted keys, one segment), then tv_normalize."""
    import tantivy
    shutil.rmtree(out, ignore_errors=True)
    os.makedirs(out)
    idx = tantivy.Index(_schema(), path=out)
    w = idx.writer(heap_size=300_000_000, num_threads=1)
    n = 0
    for k in keys:
        w.add_document(tantivy.Document(key=k))
        n += 1
    w.commit()
    w.wait_merging_threads()
    del w, idx
    _rv().tv_normalize(out, release, "typo")
    return {"keys": n}


def _meta(d: str):
    m = json.load(open(os.path.join(d, "meta.json")))
    segs = m.get("segments", [])
    docs = sum(s["max_doc"] for s in segs)
    dead = sum((s.get("deletes") or {}).get("num_deleted_docs", 0) for s in segs)
    return len(segs), docs, dead


def month(old_typo: str, old_lookup: str, new_lookup: str, out: str, release: str, max_segments: int = 6, max_deleted: float = 0.25,
          max_changed: float = 0.4) -> dict:
    import tantivy
    t0 = time.time()
    if os.path.exists(out):
        raise uc.UpdateError(f"{out} exists: an index is never built on top of another")
    rm, ad, total = key_diff(old_lookup, new_lookup)
    segs, docs, dead = _meta(old_typo)
    why = []
    if segs + 1 > max_segments:
        why.append(f"{segs} segments + this month's would pass --max-segments {max_segments}")
    if (dead + len(rm)) > max_deleted * max(1, docs + len(ad)):
        why.append(f"deleted share {(dead + len(rm)) / max(1, docs + len(ad)):.3f} would pass --max-deleted {max_deleted:g}")
    if len(rm) + len(ad) > max_changed * max(1, total):
        why.append(f"{len(rm) + len(ad)} of {total} keys changed (more than --max-changed {max_changed:g})")
    if why:
        sdb = sqlite3.connect(f"file:{new_lookup}?mode=ro", uri=True)
        res = full_typo((r[0] for r in sdb.execute("SELECT DISTINCT key FROM suggest_keys ORDER BY key")), out, release)
        sdb.close()
        return dict(res, mode="rebuild", why=why, removed=len(rm), added=len(ad), seconds=round(time.time() - t0, 1))
    old_sha = {uc.sha256_file(os.path.join(old_typo, f)) for f in os.listdir(old_typo)}      # a renamed segment is not new bytes
    shutil.copytree(old_typo, out)
    for f in (".tantivy-meta.lock", ".tantivy-writer.lock"):
        if os.path.exists(os.path.join(out, f)):
            os.remove(os.path.join(out, f))
    idx = tantivy.Index.open(out)
    w = idx.writer(heap_size=300_000_000, num_threads=1)
    for k in rm:
        w.delete_documents_by_term("key", k)
    for k in ad:
        w.add_document(tantivy.Document(key=k))
    w.commit()
    w.wait_merging_threads()
    del w, idx
    _rv().tv_normalize(out, release, "typo")
    uc.tv_sort_segments(out)                       # Tantivy's own segment order differs run to run from 3 segments up
    s2, d2, dead2 = _meta(out)
    if s2 > segs + 1:
        shutil.rmtree(out)
        raise uc.UpdateError(f"{out}: Tantivy merged on its own ({segs} -> {s2} segments); the bytes would not be reproducible - lower --max-segments")
    idx = tantivy.Index.open(out)
    live = idx.searcher().num_docs
    if live != total:
        shutil.rmtree(out)
        raise uc.UpdateError(f"{out}: {live} live keys after the delta, the new lookup.sqlite has {total}")
    return {"mode": "delta", "keys": total, "removed": len(rm), "added": len(ad), "segments": s2, "deleted_docs": dead2,
            "new_bytes": sum(os.path.getsize(os.path.join(out, f)) for f in os.listdir(out)
                             if uc.sha256_file(os.path.join(out, f)) not in old_sha),
            "seconds": round(time.time() - t0, 1)}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("old_typo")
    ap.add_argument("old_lookup")
    ap.add_argument("new_lookup")
    ap.add_argument("out")
    ap.add_argument("release")
    ap.add_argument("--max-segments", type=int, default=6)
    ap.add_argument("--max-deleted", type=float, default=0.25)
    ap.add_argument("--max-changed", type=float, default=0.4)
    a = ap.parse_args()
    try:
        print(json.dumps(month(a.old_typo, a.old_lookup, a.new_lookup, a.out, a.release, a.max_segments, a.max_deleted, a.max_changed), sort_keys=True))
    except uc.UpdateError as e:
        print(f"typo_month: REFUSED: {e}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
