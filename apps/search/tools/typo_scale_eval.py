#!/usr/bin/env python3
"""Size and speed of the typo index as a segment delta, at the real key count (3.18 M keys, lookups audit).

    typo_scale_eval.py keys  WIKI_DOCS.tsv.zst WORKDIR [--keys 3180000]
    typo_scale_eval.py chain WORKDIR [--months 7] [--add 0.02] [--delete 0.01]
    typo_scale_eval.py bench INDEX_DIR QUERIES.tsv [--queries 1500] [--label X]

There are no real suggest keys on the workstation, so the keys are the normalised Wikipedia titles (lower case, letters and digits
only: the form the lookups' keys and the eval sets' `entity` column have), a random 3.18 M of the distinct ones; the pool of the other titles
supplies the months' new keys. Real strings, real count; not the real key set.

keys    one pass over wiki_docs.tsv.zst -> WORKDIR/base.keys (sorted) and WORKDIR/pool.keys (the rest, shuffled)
chain   month 0 = typo_month.full_typo (one sorted segment); month m = typo_month.month(...) with delete x keys leaving and add x keys
        arriving (default 1% / 2% of the keys), each from a lookup.sqlite written for the month. Per month: mode, new bytes, index bytes,
        segments, seconds. Folders WORKDIR/m<k> are kept.
bench   the engine's typo query (lookups_release.py typo): a Tantivy fuzzy term query on `key`, distance 1 below 9 letters else 2,
        transposition = 1 edit, top 2000 hits, each hit's stored key read. Warm p50/p90 over the eval sets' typed queries.
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

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "release"))
import typo_month as tm  # noqa: E402
import update_common as uc  # noqa: E402

NON = re.compile(r"[^a-z0-9]")


def norm(s):
    return NON.sub("", s.lower())


def unesc(f):
    return re.sub(r"\\(.)", lambda m: {"t": "\t", "n": "\n", "r": "\r"}.get(m.group(1), m.group(1)), f) if "\\" in f else f


def cmd_keys(a):
    p = subprocess.Popen(["zstd", "-dc", a.docs], stdout=subprocess.PIPE)
    seen = set()
    for line in p.stdout:
        t = line.split(b"\t", 2)
        if len(t) < 3:
            continue
        k = norm(unesc(t[1].decode("utf-8", "replace")))
        if 3 <= len(k) <= 60 and k.isascii():
            seen.add(k)
    keys = sorted(seen)
    rnd = random.Random(1)
    rnd.shuffle(keys)
    base, pool = sorted(keys[:a.keys]), keys[a.keys:]
    os.makedirs(a.work, exist_ok=True)
    open(os.path.join(a.work, "base.keys"), "w").write("\n".join(base) + "\n")
    open(os.path.join(a.work, "pool.keys"), "w").write("\n".join(pool) + "\n")
    print(json.dumps({"distinct_titles": len(seen), "base": len(base), "pool": len(pool)}))


def mk_lookup(path, keys):
    if os.path.exists(path):
        os.remove(path)
    db = sqlite3.connect(path)
    db.execute("PRAGMA journal_mode = OFF")
    db.execute("CREATE TABLE suggest_keys (key TEXT, id INTEGER)")
    db.executemany("INSERT INTO suggest_keys VALUES (?, 1)", ((k,) for k in keys))
    db.execute("CREATE INDEX sk ON suggest_keys (key)")
    db.commit()
    db.close()


def tree(d):
    return sum(os.path.getsize(os.path.join(d, f)) for f in os.listdir(d))


def cmd_chain(a):
    rnd = random.Random(2)
    keys = set(open(os.path.join(a.work, "base.keys")).read().split())
    pool = open(os.path.join(a.work, "pool.keys")).read().split()
    n0 = len(keys)
    lk_prev = os.path.join(a.work, "lk0.sqlite")
    mk_lookup(lk_prev, sorted(keys))
    t = time.time()
    d0 = os.path.join(a.work, "m0")
    shutil.rmtree(d0, ignore_errors=True)
    r = tm.full_typo(sorted(keys), d0, "m0")
    print(json.dumps({"month": 0, "mode": "full", "keys": r["keys"], "index_bytes": tree(d0), "segments": 1, "seconds": round(time.time() - t)}), flush=True)
    prev = d0
    pi = 0
    for m in range(1, a.months + 1):
        gone = set(rnd.sample(sorted(keys), int(a.delete * n0)))
        nw = pool[pi:pi + int(a.add * n0)]
        pi += len(nw)
        keys = (keys - gone) | set(nw)
        lk = os.path.join(a.work, f"lk{m}.sqlite")
        mk_lookup(lk, sorted(keys))
        out = os.path.join(a.work, f"m{m}")
        shutil.rmtree(out, ignore_errors=True)
        t = time.time()
        res = tm.month(prev, lk_prev, lk, out, f"m{m}")
        res.update({"month": m, "index_bytes": tree(out), "segments": len(json.load(open(out + "/meta.json"))["segments"]),
                    "total_seconds": round(time.time() - t)})
        print(json.dumps(res, sort_keys=True), flush=True)
        os.remove(lk_prev)
        lk_prev, prev = lk, out


def cmd_bench(a):
    import numpy as np
    import tantivy
    qs = []
    for f in a.queries_f:
        qs += [l.split("\t")[0] for l in open(f).read().splitlines()[1:]]
    qs = list(dict.fromkeys(norm(q) for q in qs if len(norm(q)) >= 3))[:a.queries]
    idx = tantivy.Index.open(a.index)
    s, sch = idx.searcher(), idx.schema

    def run():
        ms = []
        nh = 0
        for t in qs:
            q = tantivy.Query.fuzzy_term_query(sch, "key", t, distance=1 if len(t) < 9 else 2, transposition_cost_one=True)
            t0 = time.perf_counter()
            hits = s.search(q, 2000).hits
            for _sc, addr in hits:
                s.doc(addr)["key"][0]
            ms.append((time.perf_counter() - t0) * 1e3)
            nh += len(hits)
        return ms, nh
    run()
    w1, nh = run()
    w2, _ = run()
    w = [min(x, y) for x, y in zip(w1, w2)]
    pc = lambda v, p: round(float(np.percentile(v, p)), 2)   # noqa: E731
    print(json.dumps({"label": a.label, "docs": s.num_docs, "segments": s.num_segments, "queries": len(qs), "hits_total": nh,
                      "warm_p50_ms": pc(w, 50), "warm_p90_ms": pc(w, 90), "warm_mean_ms": round(float(np.mean(w)), 2)}))


def main():
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("keys")
    p.add_argument("docs")
    p.add_argument("work")
    p.add_argument("--keys", type=int, default=3_180_000)
    p = sp.add_parser("chain")
    p.add_argument("work")
    p.add_argument("--months", type=int, default=7)
    p.add_argument("--add", type=float, default=0.02)
    p.add_argument("--delete", type=float, default=0.01)
    p = sp.add_parser("bench")
    p.add_argument("index")
    p.add_argument("queries_f", nargs="+")
    p.add_argument("--queries", type=int, default=1500)
    p.add_argument("--label", default="")
    a = ap.parse_args()
    {"keys": cmd_keys, "chain": cmd_chain, "bench": cmd_bench}[a.cmd](a)


if __name__ == "__main__":
    main()
