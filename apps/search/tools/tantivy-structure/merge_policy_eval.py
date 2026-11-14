#!/usr/bin/env python3
"""Tantivy merge policy for the monthly update package, measured on the real wiki index.

    merge_policy_eval.py prep   IN_DIR SAMPLE.tsv.zst [--stride 8]
    merge_policy_eval.py append BASE_INDEX WORK_DIR SAMPLE.tsv.zst REGISTRY.sqlite IN_DIR [--months 6] [--add 0.02] [--delete 0.01] [--snap 1,3,6]
    merge_policy_eval.py bench  INDEX_DIR IN_DIR QUERIES.tsv [--queries 500] [--label X]

IN_DIR holds english.stop (the engine's stop words). BASE_INDEX is never written to: every state is a directory of HARD LINKS to the
segment files (Tantivy never rewrites a segment file; it adds new files and rewrites meta.json, which is copied), so a state costs only
its new bytes.

prep    one pass over wiki_docs.tsv.zst: every STRIDE-th article, as the text source for the synthetic months (new documents have real
        text of real length; their doc_ids are new).
append  month m = delete `delete` x docs existing doc_ids (random, never twice), add `add` x docs NEW documents (doc_ids above the
        registry's maximum wiki doc_id, text from the sample), one commit, one writer thread (release_v1.py tvmonth's pattern);
        snapshots after the months in --snap are kept as WORK/m<k>. Prints new bytes and segment count per month.
bench   engine-shaped queries (apps/search/serve/api/search_api.py TantivyWikiText.search: the en_pg2 analyzer's terms, an OR of
        term queries on `body`, top 10) from an evalset's query column: warm p50/p90 (second pass) and a cold pass (index files
        evicted from the page cache with posix_fadvise DONTNEED first). Needs a quiet machine.
"""
import argparse
import json
import os
import random
import re
import shutil
import sqlite3
import subprocess
import sys
import time

import numpy as np
import tantivy

PG = r"[^\W_]+(?:[.@][^\W_]+)+|[^\W_]+"


def analyzer(stop_f):
    stop = [l.strip() for l in open(stop_f) if l.strip()]
    return (tantivy.TextAnalyzerBuilder(tantivy.Tokenizer.regex(PG)).filter(tantivy.Filter.lowercase())
            .filter(tantivy.Filter.custom_stopword(stop)).filter(tantivy.Filter.stemmer("english")).build())


def unesc(f):
    return re.sub(r"\\(.)", lambda m: {"t": "\t", "n": "\n", "r": "\r"}.get(m.group(1), m.group(1)), f) if "\\" in f else f


def light_copy(src, dst):
    """A state directory: hard links for the big (immutable) files, real copies of the small ones Tantivy rewrites."""
    os.makedirs(dst, exist_ok=True)
    for f in os.listdir(src):
        s, d = os.path.join(src, f), os.path.join(dst, f)
        if f in (".tantivy-meta.lock", ".tantivy-writer.lock"):
            continue
        if os.path.getsize(s) > (1 << 20) and not f.endswith("meta.json") and f != ".managed.json":
            os.link(s, d)
        else:
            shutil.copyfile(s, d)


def tree_bytes(d):
    return sum(os.path.getsize(os.path.join(d, f)) for f in os.listdir(d))


def seg_info(d):
    m = json.load(open(os.path.join(d, "meta.json")))
    return [(s["max_doc"], (s.get("deletes") or {}).get("num_deleted_docs", 0)) for s in m["segments"]]


def cmd_prep(a):
    p = subprocess.Popen(["zstd", "-dc", os.path.join(a.in_dir, "wiki_docs.tsv.zst")], stdout=subprocess.PIPE)
    out = subprocess.Popen(["zstd", "-q", "-3", "-T4", "-o", a.sample], stdin=subprocess.PIPE)
    n = kept = 0
    for line in p.stdout:
        if n % a.stride == 0:
            out.stdin.write(line)
            kept += 1
        n += 1
    out.stdin.close()
    out.wait()
    print(json.dumps({"lines": n, "kept": kept, "bytes": os.path.getsize(a.sample)}))


def cmd_append(a):
    t0 = time.time()
    reg = sqlite3.connect(f"file:{a.registry}?mode=ro", uri=True)
    ids = np.array([r[0] for r in reg.execute("SELECT doc_id FROM registry WHERE doc_id >> 28 = 1 ORDER BY doc_id")], np.int64)
    n_base = len(ids)
    next_id = int(ids.max()) + 1
    rnd = np.random.default_rng(11)
    perm = rnd.permutation(ids)
    n_add, n_del = int(a.add * n_base), int(a.delete * n_base)
    src = subprocess.Popen(["zstd", "-dc", a.sample], stdout=subprocess.PIPE)
    an = analyzer(os.path.join(a.in_dir, "english.stop"))
    cur = os.path.join(a.work, "cum")
    shutil.rmtree(a.work, ignore_errors=True)
    light_copy(a.base, cur)
    snaps = {int(x) for x in a.snap.split(",")}
    res = []
    for m in range(1, a.months + 1):
        idx = tantivy.Index.open(cur)
        idx.register_tokenizer("en_pg2", an)
        w = idx.writer(heap_size=1_000_000_000, num_threads=1)
        before = set(os.listdir(cur))
        for d in perm[(m - 1) * n_del: m * n_del]:
            # NOT delete_documents_by_term: with a Python int it builds an i64 term and silently deletes nothing on the real u64 doc_id
            w.delete_documents_by_query(tantivy.Query.term_query(idx.schema, "doc_id", int(d)))
        added = 0
        for line in src.stdout:
            f = line.rstrip(b"\n").decode("utf-8", "replace").split("\t")
            if len(f) < 6:
                continue
            title, al, ib, hd, lead = (unesc(x) for x in f[1:6])
            d = tantivy.Document()
            d.add_unsigned("doc_id", next_id)
            next_id += 1
            d.add_text("title", title or "")
            d.add_text("body", " ".join(x for x in (title, al, ib, hd, lead) if x))
            w.add_document(d)
            added += 1
            if added >= n_add:
                break
        w.commit()
        w.wait_merging_threads()
        del w, idx
        chk = tantivy.Index.open(cur)
        sr = chk.searcher()
        q = lambda i: sr.search(tantivy.Query.term_query(chk.schema, "doc_id", int(i)), 1).count   # noqa: E731
        gone_ok = all(q(d) == 0 for d in perm[(m - 1) * n_del:(m - 1) * n_del + 20])
        new_ok = all(q(i) == 1 for i in range(next_id - 20, next_id))
        del chk, sr
        new = sorted(set(os.listdir(cur)) - before)
        row = {"month": m, "deleted": n_del, "added": added, "new_files": len(new),
               "new_bytes": sum(os.path.getsize(os.path.join(cur, f)) for f in new),
               "deletes_work": gone_ok, "new_docs_found": new_ok, "segments": len(seg_info(cur)), "seg_docs": seg_info(cur), "seconds": round(time.time() - t0)}
        print(json.dumps(row), flush=True)
        res.append(row)
        if m in snaps:
            light_copy(cur, os.path.join(a.work, f"m{m}"))
    json.dump(res, open(os.path.join(a.work, "append.json"), "w"))


def evict(d):
    for f in os.listdir(d):
        fd = os.open(os.path.join(d, f), os.O_RDONLY)
        os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
        os.close(fd)


def cmd_bench(a):
    qs = [l.split("\t")[0] for l in open(a.queries_f).read().splitlines()[1:]]
    qs = list(dict.fromkeys(qs))
    random.Random(5).shuffle(qs)
    an = analyzer(os.path.join(a.in_dir, "english.stop"))
    qs = [q for q in qs if an.analyze(q)][:a.queries]
    evict(a.index)
    idx = tantivy.Index.open(a.index)
    idx.register_tokenizer("en_pg2", an)
    idx.reload()
    s, sch = idx.searcher(), idx.schema

    def run():
        ms, hits = [], []
        for q in qs:
            terms = list(dict.fromkeys(an.analyze(q)))
            query = tantivy.Query.boolean_query([(tantivy.Occur.Should, tantivy.Query.term_query(sch, "body", t)) for t in terms])
            t = time.perf_counter()
            r = s.search(query, 10)
            ms.append((time.perf_counter() - t) * 1e3)
            hits.append(len(r.hits))
        return ms, hits
    cold, _ = run()
    warm, h1 = run()
    warm2, _ = run()
    pc = lambda v, p: round(float(np.percentile(v, p)), 3)   # noqa: E731
    w = [min(x, y) for x, y in zip(warm, warm2)]
    print(json.dumps({"label": a.label, "index": a.index, "docs": s.num_docs, "segments": s.num_segments, "bytes": tree_bytes(a.index),
                      "queries": len(qs), "cold_p50": pc(cold, 50), "cold_p90": pc(cold, 90), "warm_p50": pc(w, 50), "warm_p90": pc(w, 90),
                      "warm_p99": pc(w, 99), "warm_mean": round(float(np.mean(w)), 3)}))


def main():
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("prep")
    p.add_argument("in_dir")
    p.add_argument("sample")
    p.add_argument("--stride", type=int, default=8)
    p = sp.add_parser("append")
    p.add_argument("base")
    p.add_argument("work")
    p.add_argument("sample")
    p.add_argument("registry")
    p.add_argument("in_dir")
    p.add_argument("--months", type=int, default=6)
    p.add_argument("--add", type=float, default=0.02)
    p.add_argument("--delete", type=float, default=0.01)
    p.add_argument("--snap", default="1,3,6")
    p = sp.add_parser("bench")
    p.add_argument("index")
    p.add_argument("in_dir")
    p.add_argument("queries_f")
    p.add_argument("--queries", type=int, default=500)
    p.add_argument("--label", default="")
    a = ap.parse_args()
    {"prep": cmd_prep, "append": cmd_append, "bench": cmd_bench}[a.cmd](a)


if __name__ == "__main__":
    main()
