#!/usr/bin/env python3
"""Build the release's meaning-search folder (vectors/ivf<L>/) from the exported 1-bit codes.

    ivf_build.py RAW_PREFIX OUT_DIR RELEASE_ID [--lists 2600] [--sample 262144] [--iters 20] [--threads 1]
                 [--previous PREV_IVF_DIR [--force-rebuild] [--no-drift-check]]

RAW_PREFIX is what export.py vectors wrote (RAW_PREFIX.bits = N x 96 bytes, RAW_PREFIX.ids.u64).

FULL BUILD (no --previous). Same recipe as the T3b spike (apps/search/tools/bitivf_spike.py build), whose recall/speed numbers
chose it: k-means (faiss, float) on a fixed sample of the bits, majority bit per centre, every code to its nearest centre by
Hamming distance, codes rewritten grouped by list. Build-only: faiss is not in the engine image. One faiss thread and a fixed
seed, so the same codes give the same lists (rule 7 of docs/internal/SEARCH-RELEASE-FORMAT.md); the check is a second build +
sha256. ivf.json also records `trained`: ivf_stats() of the folder as built, the yardstick for drift.

MONTHLY BUILD (--previous PREV_IVF_DIR = last release's folder). The centres are KEPT. The export is compared with PREV's
(ids, codes): a removed id leaves, a new or changed id is assigned to its nearest existing centre, and the result is written by
update_common.ivf_merge, so the update package carries only the delta (update_pkg.py: op ivf-merge). No faiss needed. The merged
folder is measured against PREV's `trained` stats; when a limit in update_common.IVF_LIMITS is passed (code-to-centre distance,
biggest list, rows, merges since the last k-means) the centres no longer fit and the folder is RE-CLUSTERED by the full build
instead (the centres move, so update_pkg ships the folder whole). --force-rebuild re-clusters unconditionally; --no-drift-check
merges whatever the drift. Prints one JSON line with `mode`: merge | rebuild, and why.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import resource
import shutil
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import update_common as uc  # noqa: E402

FORMAT = uc.IVF_FORMAT
DIM_BYTES = uc.DIM_BYTES
CHUNK = 500_000
ENCODER = "nomic-embed-text-v1.5, mean-pooled, unit length, bit = component >= 0, MSB first"


def sha256(path: str) -> str:
    return uc.sha256_file(path)


def load_raw(raw_prefix: str):
    ids = np.fromfile(raw_prefix + ".ids.u64", np.uint64)
    n = len(ids)
    bits = np.memmap(raw_prefix + ".bits", np.uint8, "r", shape=(n, DIM_BYTES))
    if n and ids.max() >= 2**31:
        raise SystemExit(f"page_id {int(ids.max())} does not fit ids.i32")
    return ids, bits


def full_build(ids, bits, out: str, release: str, lists: int = 2600, sample: int = 262_144, iters: int = 20, threads: int = 1) -> dict:
    import faiss
    faiss.omp_set_num_threads(threads)
    t0 = time.time()
    n = len(ids)
    rng = np.random.default_rng(1)
    samp = np.sort(rng.choice(n, min(sample, n), replace=False))
    x = np.unpackbits(np.asarray(bits[samp]), axis=1).astype(np.float32)
    km = faiss.Kmeans(DIM_BYTES * 8, lists, niter=iters, seed=1, verbose=False, max_points_per_centroid=10**9)
    km.train(x)
    del x
    cent = np.packbits((km.centroids >= 0.5).astype(np.uint8), axis=1)      # majority bit per list
    train_s = time.time() - t0

    t1 = time.time()
    q = faiss.IndexBinaryFlat(DIM_BYTES * 8)
    q.add(cent)
    assign = np.empty(n, np.int32)
    for s in range(0, n, CHUNK):
        _, idx = q.search(np.ascontiguousarray(bits[s:s + CHUNK]), 1)
        assign[s:s + CHUNK] = idx[:, 0]
    assign_s = time.time() - t1

    order = np.argsort(assign, kind="stable")      # within a list: export order (page_id order)
    counts = np.bincount(assign, minlength=lists)
    offs = np.zeros(lists + 1, np.int64)
    offs[1:] = np.cumsum(counts)

    os.makedirs(out, exist_ok=True)
    cent.tofile(os.path.join(out, "centroids.u8"))
    offs.tofile(os.path.join(out, "offsets.i64"))
    ids[order].astype(np.int32).tofile(os.path.join(out, "ids.i32"))
    mm = np.memmap(os.path.join(out, "codes.u8"), np.uint8, "w+", shape=(n, DIM_BYTES))
    for s in range(0, n, CHUNK):
        mm[s:s + CHUNK] = bits[order[s:s + CHUNK]]
    mm.flush()
    del mm

    meta = {"format": FORMAT, "release": release, "rows": int(n), "lists": lists, "dim_bits": DIM_BYTES * 8, "encoder": ENCODER,
            "recipe": {"sample": int(len(samp)), "iters": iters, "seed": 1, "faiss_threads": threads}}
    with open(os.path.join(out, "ivf.json"), "w", encoding="utf-8") as f:       # first without `trained`: ivf_stats reads the folder
        json.dump(meta, f, indent=1, sort_keys=True)
        f.write("\n")
    meta["trained"] = dict(uc.ivf_stats(out), release=release)
    with open(os.path.join(out, "ivf.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=1, sort_keys=True)
        f.write("\n")
    return dict(meta, stage="ivf", mode="full", train_s=round(train_s), assign_s=round(assign_s),
                list_min=int(counts.min()), list_p50=int(np.median(counts)), list_max=int(counts.max()), empty=int((counts == 0).sum()))


def delta_build(ids, bits, prev: str, out: str, release: str, check_drift: bool = True, force: bool = False, **full_kw) -> dict:
    """Last month's folder + the delta to this export, centres fixed; re-cluster when the drift check (or force) says so."""
    t0 = time.time()
    pm = json.load(open(os.path.join(prev, "ivf.json"), encoding="utf-8"))
    if pm.get("format") != FORMAT:
        raise SystemExit(f"{prev}/ivf.json: format {pm.get('format')!r}, this build merges {FORMAT!r}")
    why = []
    if force:
        why.append("--force-rebuild")
    else:
        poffs = np.fromfile(os.path.join(prev, "offsets.i64"), np.int64)
        pn = int(poffs[-1])
        pids = np.fromfile(os.path.join(prev, "ids.i32"), np.int32)
        pcodes = np.memmap(os.path.join(prev, "codes.u8"), np.uint8, "r", shape=(pn, DIM_BYTES))
        removed, added, codes = uc.ivf_diff(pids, pcodes, ids.astype(np.int64), bits)
        del pcodes
        info = uc.ivf_merge(prev, out, removed, added, codes, release)
        stats = uc.ivf_stats(out)
        merges = int(json.load(open(os.path.join(out, "ivf.json")))["merges"])
        base = pm.get("trained") or uc.ivf_stats(prev)      # a folder without `trained` (older build) is its own yardstick
        drift = uc.ivf_drift(stats, base, merges)
        if drift["rebuild"] and check_drift:
            why = drift["reasons"]
        else:
            return {"stage": "ivf", "mode": "merge", "release": release, "from": pm.get("release"), "removed": info["removed"], "added": info["added"],
                    "rows": info["rows"], "stats": stats, "drift": drift, "drift_check": check_drift, "seconds": round(time.time() - t0, 1)}
        shutil.rmtree(out)
    res = full_build(ids, bits, out, release, lists=int(pm.get("lists", 2600)), **full_kw)
    res["mode"], res["rebuild_because"] = "rebuild", why
    mp = os.path.join(out, "ivf.json")
    meta = json.load(open(mp, encoding="utf-8"))
    meta["rebuilt_because"] = why                 # update_pkg quotes it when it ships the folder whole
    with open(mp, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=1, sort_keys=True)
        f.write("\n")
    return res


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("raw_prefix")
    p.add_argument("out")
    p.add_argument("release")
    p.add_argument("--lists", type=int, default=2600)
    p.add_argument("--sample", type=int, default=262_144)
    p.add_argument("--iters", type=int, default=20)
    p.add_argument("--threads", type=int, default=1, help="faiss threads; 1 is the reproducible setting (rule 7)")
    p.add_argument("--previous", help="last release's vectors/ivf folder: keep its centres, merge the delta")
    p.add_argument("--force-rebuild", action="store_true")
    p.add_argument("--no-drift-check", action="store_true")
    a = p.parse_args()
    t0 = time.time()
    ids, bits = load_raw(a.raw_prefix)
    kw = {"sample": a.sample, "iters": a.iters, "threads": a.threads}
    if a.previous:
        res = delta_build(ids, bits, a.previous, a.out, a.release, not a.no_drift_check, a.force_rebuild, **kw)
    else:
        res = full_build(ids, bits, a.out, a.release, lists=a.lists, **kw)
    res["bytes"] = {f: os.path.getsize(os.path.join(a.out, f)) for f in sorted(os.listdir(a.out))}
    res["sha256"] = {f: sha256(os.path.join(a.out, f)) for f in ("centroids.u8", "offsets.i64", "ids.i32", "codes.u8")}
    res["seconds"] = round(time.time() - t0, 1)
    res["peak_rss_mb"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024
    print(json.dumps(res), flush=True)


if __name__ == "__main__":
    main()
