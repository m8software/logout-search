#!/usr/bin/env python3
"""Rule 3 (store nothing) with the engine's binding, tantivy-py 0.26.2.

    tv_store.py build   EXPORT DIR STOPFILE [THREADS]           store-nothing schema, the Phase-1 code otherwise
    tv_store.py query   DIR STOPFILE QUERIES IDS OUT.tsv [REPS] IDS: stored:page_id | fast:doc_id | fast:page_id
    tv_store.py month   DIR STOPFILE EXPORT MOD REM TAG         a month on the store-nothing schema
    tv_store.py month-split HOT COLD STOPFILE EXPORT MOD REM TAG RANKS HOT_N   the same, routed to hot/cold
    tv_store.py same    A B                                     do two index dirs hold byte-identical files?

The build is dbt_tv2.py's (Phase 1) with one change: nothing displayable is stored. page_id
becomes doc_id, fast + indexed (indexed so a month can delete by term), not stored; title is
indexed with positions, not stored; the 300-char snippet is gone. It uses the Phase-1 row
parser (dbt_sqlite.rows) on purpose, so the only difference from v1 is the store.

query evicts the index files from the page cache (posix_fadvise DONTNEED) before each repeat,
so the first pass is cold under the container's memory cap; ids come back either from the
doc store (stored:page_id, what v1 and search_api do today) or from the fast field
(Searcher.fast_field_values, the only way once nothing is stored).
"""
import hashlib
import json
import os
import sys
import time

import tantivy

sys.path.insert(0, "/d")
from dbt_sqlite import rows  # noqa: E402  (the Phase-1 parser, kept on purpose)
from dbt_tv2 import ANALYZER, analyzer, dir_size, meta_segments, terms  # noqa: E402


def schema():
    sb = tantivy.SchemaBuilder()
    sb.add_integer_field("doc_id", stored=False, indexed=True, fast=True)
    sb.add_text_field("title", stored=False, tokenizer_name=ANALYZER, index_option="position")
    sb.add_text_field("body", stored=False, tokenizer_name=ANALYZER, index_option="freq")
    return sb.build()


def doc(r):
    pid, title, aliases, infobox, headings, lead = r
    d = tantivy.Document()
    d.add_integer("doc_id", pid)
    d.add_text("title", title or "")
    d.add_text("body", " ".join(x for x in (title, aliases, infobox, headings, lead) if x))
    return d


def build(export, path, stopfile, threads=2):
    os.makedirs(path, exist_ok=True)
    idx = tantivy.Index(schema(), path=path)
    idx.register_tokenizer(ANALYZER, analyzer(stopfile))
    w = idx.writer(heap_size=2_000_000_000, num_threads=threads)
    t0 = time.time()
    n = 0
    for r in rows(export):
        w.add_document(doc(r))
        n += 1
    w.commit()
    w.wait_merging_threads()
    print(json.dumps({"rows": n, "threads": threads, "build_s": round(time.time() - t0),
                      "index_gb": round(dir_size(path) / 2**30, 3), "segments": meta_segments(path)}))


def evict(path):
    n = 0
    for f in os.listdir(path):
        p = os.path.join(path, f)
        if os.path.isfile(p):
            fd = os.open(p, os.O_RDONLY)
            os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
            os.close(fd)
            n += os.path.getsize(p)
    return n


def pct(v, p):
    v = sorted(v)
    return round(v[min(len(v) - 1, int(len(v) * p))], 1)


def query(path, stopfile, qfile, ids, out, reps=3):
    how, field = ids.split(":")
    an = analyzer(stopfile)
    qs = [line.strip() for line in open(qfile, encoding="utf-8") if line.strip() and not line.startswith("#")]
    runs = []
    res_out = {}
    for rep in range(int(reps)):
        ev = evict(path)
        t = time.perf_counter()
        idx = tantivy.Index.open(path)
        idx.register_tokenizer(ANALYZER, an)
        idx.reload()
        s = idx.searcher()
        sc = idx.schema
        open_ms = (time.perf_counter() - t) * 1000
        r = {"repeat": rep, "evicted_mb": ev >> 20, "open_ms": round(open_ms, 1)}
        for name in ("first", "warm"):
            tot = []
            srch = []
            for q in qs:
                ts = terms(an, q)
                if not ts:
                    continue
                t = time.perf_counter()
                parts = [(tantivy.Occur.Should, tantivy.Query.term_query(sc, "body", x)) for x in ts]
                res = s.search(tantivy.Query.boolean_query(parts), 10)
                t1 = time.perf_counter()
                addrs = [a for _, a in res.hits]
                if how == "stored":
                    got = [s.doc(a)[field][0] for a in addrs]
                else:
                    got = s.fast_field_values(field, addrs)
                tot.append((time.perf_counter() - t) * 1000)
                srch.append((t1 - t) * 1000)
                res_out[q] = got
            r[name] = {"n": len(tot), "search_p50_ms": pct(srch, .5), "search_p90_ms": pct(srch, .9),
                       "with_ids_p50_ms": pct(tot, .5), "with_ids_p90_ms": pct(tot, .9)}
        runs.append(r)
        del s, idx
    with open(out, "w", encoding="utf-8") as f:
        for q, got in res_out.items():
            f.write(q + "\t" + ",".join(str(x) for x in got if x is not None) + "\n")

    def med(name, key):
        v = sorted(x[name][key] for x in runs)
        return {"median": v[len(v) // 2], "min": v[0], "max": v[-1]}
    print(json.dumps({"index": path, "ids": ids, "repeats": len(runs), "queries": runs[0]["first"]["n"],
                      "first_with_ids_p50": med("first", "with_ids_p50_ms"), "first_with_ids_p90": med("first", "with_ids_p90_ms"),
                      "warm_with_ids_p50": med("warm", "with_ids_p50_ms"), "warm_with_ids_p90": med("warm", "with_ids_p90_ms"),
                      "runs": runs, "index_gb": round(dir_size(path) / 2**30, 3), "segments": meta_segments(path)}))


def id_type(path):
    m = json.load(open(os.path.join(path, "meta.json")))
    return next(f["type"] for f in m["schema"] if f["name"] == "doc_id")


def month_doc(r, tag, unsigned):
    r = list(r)
    r[5] = (r[5] or "") + " " + tag
    d = doc(r)
    if unsigned:  # an index built by the Rust tool: doc_id is u64
        pid, title, aliases, infobox, headings, lead = r
        d = tantivy.Document()
        d.add_unsigned("doc_id", pid)
        d.add_text("title", title or "")
        d.add_text("body", " ".join(x for x in (title, aliases, infobox, headings, lead) if x))
    return d


def month(path, stopfile, export, mod, rem, tag, cold=None, ranks=None, hot_n=0):
    """Delete + re-add every article with page_id % mod == rem (with a marker word).
    With COLD and RANKS: articles ranked < hot_n go to the hot index, the rest to cold."""
    paths = [path] + ([cold] if cold else [])
    writers = []
    for p in paths:
        idx = tantivy.Index.open(p)
        idx.register_tokenizer(ANALYZER, analyzer(stopfile))
        writers.append((idx.writer(heap_size=500_000_000, num_threads=2), id_type(p) == "u64", idx.schema))
    hot = None
    if cold:
        hot = set()
        for line in open(ranks, encoding="utf-8"):
            pid, rk, _ = line.split("\t")
            if int(rk) < hot_n:
                hot.add(int(pid))
    t0 = time.time()
    n = [0] * len(paths)
    for r in rows(export):
        if r[0] % mod != rem:
            continue
        k = 0 if hot is None or r[0] in hot else 1
        w, unsigned, schema = writers[k]
        # by_term builds an i64 term and deletes nothing on a u64 doc_id; a term query takes the field's type
        w.delete_documents_by_query(tantivy.Query.term_query(schema, "doc_id", r[0]))
        w.add_document(month_doc(r, tag, unsigned))
        n[k] += 1
    for w, _, _ in writers:
        w.commit()
        w.wait_merging_threads()
    print(json.dumps({"rows": n, "apply_s": round(time.time() - t0, 1),
                      "index_gb": [round(dir_size(p) / 2**30, 3) for p in paths],
                      "segments": [meta_segments(p) for p in paths]}))


def same(a, b):
    def h(p):
        d = hashlib.sha256()
        with open(p, "rb") as f:
            for blk in iter(lambda: f.read(1 << 20), b""):
                d.update(blk)
        return d.hexdigest()
    fa = {f: h(os.path.join(a, f)) for f in sorted(os.listdir(a)) if not f.endswith(".lock")}
    fb = {f: h(os.path.join(b, f)) for f in sorted(os.listdir(b)) if not f.endswith(".lock")}
    by_content_a = {v: k for k, v in fa.items()}
    by_content_b = {v: k for k, v in fb.items()}
    rep = {"files_a": len(fa), "files_b": len(fb),
           "same_name_same_bytes": sorted(f for f in fa if fb.get(f) == fa[f]),
           "same_bytes_other_name": sorted(f"{by_content_a[c]} = {by_content_b[c]}" for c in by_content_a
                                           if c in by_content_b and by_content_a[c] != by_content_b[c]),
           "only_in_a": sorted(f for f in fa if fa[f] not in by_content_b),
           "only_in_b": sorted(f for f in fb if fb[f] not in by_content_a)}
    rep["identical"] = fa == fb
    print(json.dumps(rep, indent=1))


if __name__ == "__main__":
    c = sys.argv[1]
    if c == "build":
        build(sys.argv[2], sys.argv[3], sys.argv[4], int(sys.argv[5]) if len(sys.argv) > 5 else 2)
    elif c == "query":
        query(*sys.argv[2:])
    elif c == "month":
        month(sys.argv[2], sys.argv[3], sys.argv[4], int(sys.argv[5]), int(sys.argv[6]), sys.argv[7])
    elif c == "month-split":
        month(sys.argv[2], sys.argv[4], sys.argv[5], int(sys.argv[6]), int(sys.argv[7]), sys.argv[8],
              cold=sys.argv[3], ranks=sys.argv[9], hot_n=int(sys.argv[10]))
    elif c == "same":
        same(sys.argv[2], sys.argv[3])
    else:
        raise SystemExit(__doc__)
