#!/usr/bin/env python3
"""How far can the meaning-search folder be MERGED month after month (centres fixed) before it must be re-clustered?

    ivf_drift_eval.py CODES.npy IDS.npy WORKDIR --scenario newest|random|fast [--months 1,3,6,12] [--lists 2600] [--threads 14]

Measurement tool for docs/internal/SEARCH-UPDATE-PACKAGE.md section 5 (dev only, needs faiss). CODES.npy = N x 96 uint8 1-bit codes,
IDS.npy = their page_ids, ascending (the real wiki_bit dump parsed by the caller; or any clustered set).

Every month the current set changes (rows leave, rows arrive), and two folders are made of it:
  merged  apps/search/release/ivf_build.py's monthly path (delta_build, drift check off): last month's merged folder + the delta,
          the centres of month 0 never move
  fresh   the full build (ivf_build.full_build: k-means on the current set)
and both are searched with the engine's own sem_ivf.IvfIndex at 200 probes. Recall@10 is "of the exact 10 nearest by Hamming distance,
how many did the IVF return": an answer counts when its distance is <= the 10th exact distance (ties are fair). Queries are codes of
1,000 rows held out of every index: 500 drawn from the whole set, 500 from the newest 30% of page_ids.

Scenarios: newest = the base set is the oldest 70% of page_ids, every month adds the next newest slice (a real shift: new
articles are about new things) and removes some rows at random; random = the same sizes but base and arrivals are random rows
(no shift); fast = newest with 10% arriving per month (--base-share 0.4 makes it the worst case: a k-means that saw only the oldest 40%). One JSON line per month to WORKDIR/results-SCENARIO.jsonl.
"""
import argparse
import json
import os
import shutil
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.join(HERE, "..", "release"), os.path.join(HERE, "..", "serve", "api")]
import ivf_build as ib  # noqa: E402
import update_common as uc  # noqa: E402
from sem_ivf import IvfIndex  # noqa: E402


def recall(folder, queries, exact_d, probes=200, k=10):
    ix = IvfIndex(folder)
    hit, ms = 0, []
    for q, ed in zip(queries, exact_d):
        t = time.perf_counter()
        res = ix.search(q, k=k, probes=probes)
        ms.append((time.perf_counter() - t) * 1e3)
        hit += sum(1 for _, d in res if d <= ed[k - 1])
    return hit / (k * len(queries)), float(np.percentile(ms, 50)), float(np.percentile(ms, 90))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("codes")
    ap.add_argument("ids")
    ap.add_argument("work")
    ap.add_argument("--scenario", choices=["newest", "random", "fast"], required=True)
    ap.add_argument("--months", default="1,3,6,12")
    ap.add_argument("--lists", type=int, default=2600)
    ap.add_argument("--threads", type=int, default=14)
    ap.add_argument("--probes", type=int, default=200)
    ap.add_argument("--base-share", type=float, default=0.7, help="share of the (page_id ordered) rows the month-0 k-means sees")
    a = ap.parse_args()
    import faiss
    faiss.omp_set_num_threads(a.threads)
    os.makedirs(a.work, exist_ok=True)
    codes = np.load(a.codes)
    ids = np.load(a.ids).astype(np.uint64)
    N = len(ids)
    rng = np.random.default_rng(7)
    q_all = rng.choice(N, 500, replace=False)
    newest = np.arange(int((1 - 0.3) * N), N)
    q_new = rng.choice(np.setdiff1d(newest, q_all), 500, replace=False)
    qrows = np.concatenate((q_all, q_new))
    queries = [codes[i] for i in qrows]
    usable = np.ones(N, bool)
    usable[qrows] = False
    fast = a.scenario == "fast"
    add_share = 0.10 if fast else 0.02
    rm_share = 0.02 if fast else 0.005
    if a.scenario == "random":
        perm = rng.permutation(np.flatnonzero(usable))
        order = perm
    else:
        order = np.flatnonzero(usable)                      # page_id ascending: the arrivals are the newest
    base_n = int(a.base_share * len(order))
    cur = np.zeros(N, bool)
    cur[order[:base_n]] = True
    step = int(add_share * len(order))
    months = sorted({int(x) for x in a.months.split(",")})
    out = os.path.join(a.work, f"results-{a.scenario}.jsonl")
    merged_dir = None
    rel = "m0"

    def exact(sel):
        ix = faiss.IndexBinaryFlat(768)
        ix.add(np.ascontiguousarray(codes[sel]))
        D, _ = ix.search(np.ascontiguousarray(np.stack(queries)), 10)
        return D

    def log(row):
        print(json.dumps(row), flush=True)
        with open(out, "a") as f:
            f.write(json.dumps(row) + "\n")

    t0 = time.time()
    sel = np.flatnonzero(cur)
    merged_dir = os.path.join(a.work, "merged-a")
    shutil.rmtree(merged_dir, ignore_errors=True)
    base = ib.full_build(ids[sel], codes[sel], merged_dir, "m0", lists=a.lists, threads=a.threads)
    D = exact(sel)
    r_all, p50, p90 = recall(merged_dir, queries[:500], D[:500], a.probes)
    r_new, _, _ = recall(merged_dir, queries[500:], D[500:], a.probes)
    log({"scenario": a.scenario, "month": 0, "rows": int(len(sel)), "kind": "base k-means", "recall_all": round(r_all, 4), "recall_new": round(r_new, 4),
         "p50_ms": round(p50, 1), "p90_ms": round(p90, 1), "stats": uc.ivf_stats(merged_dir), "train_s": base["train_s"]})
    nxt = base_n
    flip = "b"
    for m in range(1, max(months) + 1):
        arrivals = order[nxt:nxt + step]
        nxt += step
        have = np.flatnonzero(cur)
        gone = rng.choice(have, int(rm_share * len(order)), replace=False)
        cur[gone] = False
        cur[arrivals] = True
        if m not in months:
            # still merge every month (the folder is cumulative), measure only at the checkpoints
            pass
        sel = np.flatnonzero(cur)
        new_dir = os.path.join(a.work, f"merged-{flip}")
        shutil.rmtree(new_dir, ignore_errors=True)
        res = ib.delta_build(ids[sel], codes[sel], merged_dir, new_dir, f"m{m}", check_drift=False, threads=a.threads)
        shutil.rmtree(merged_dir)
        merged_dir, flip = new_dir, ("a" if flip == "b" else "b")
        if m not in months:
            continue
        D = exact(sel)
        r_all, p50, p90 = recall(merged_dir, queries[:500], D[:500], a.probes)
        r_new, _, _ = recall(merged_dir, queries[500:], D[500:], a.probes)
        row = {"scenario": a.scenario, "month": m, "rows": int(len(sel)), "kind": "merged", "added": res["added"], "removed": res["removed"],
               "recall_all": round(r_all, 4), "recall_new": round(r_new, 4), "p50_ms": round(p50, 1), "p90_ms": round(p90, 1),
               "stats": res["stats"], "drift": res["drift"]["metrics"]}
        log(row)
        fresh_dir = os.path.join(a.work, "fresh")
        shutil.rmtree(fresh_dir, ignore_errors=True)
        fr = ib.full_build(ids[sel], codes[sel], fresh_dir, f"m{m}", lists=a.lists, threads=a.threads)
        f_all, fp50, fp90 = recall(fresh_dir, queries[:500], D[:500], a.probes)
        f_new, _, _ = recall(fresh_dir, queries[500:], D[500:], a.probes)
        log({"scenario": a.scenario, "month": m, "rows": int(len(sel)), "kind": "fresh k-means", "recall_all": round(f_all, 4), "recall_new": round(f_new, 4),
             "p50_ms": round(fp50, 1), "p90_ms": round(fp90, 1), "stats": uc.ivf_stats(fresh_dir), "train_s": fr["train_s"]})
        shutil.rmtree(fresh_dir)
    print("done", round(time.time() - t0), "s", file=sys.stderr)


if __name__ == "__main__":
    main()
