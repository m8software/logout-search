#!/usr/bin/env python3
"""T3b spike: low-memory meaning search over the nomic 1-bit codes (768 bits = 96 bytes/article).
Dev/bench tool only — never shipped from apps/. Reads the codes usearch_spike.py exported
(DIR/bits_u8.npy, ids.npy, q.npy, exact_*.npy) and writes results the same way, so
`usearch_spike.py recall` scores them.

  build DIR L [--sample N]       IVF: k-means on the bits (faiss, float) -> majority-bit centroids,
                                 every code to its nearest centroid by Hamming, codes rewritten
                                 grouped by list: DIR/ivfL/{centroids.npy, offsets.npy, codes.u8, ids.i32}
  delta DIR L PCT                a month (DIR/delta_PCT from usearch_spike.py delta) as a side file
                                 grouped by the same lists + tombstones -> DIR/ivfL/delta_PCT/
  ivf DIR L TAG [--probes ..] [--delta PCT] [--encoder API]
                                 search: nearest `probes` lists by Hamming, exact Hamming inside them
  flat DIR TAG [--threads 1,2] [--encoder API]
                                 compiled exact scan (numba) over the memory-mapped codes
  faissflat DIR TAG              faiss IndexBinaryFlat (codes copied into RAM)

Timings are per query, single query at a time. Pass 1 of each setting starts with the data file
evicted from the page cache (unmapped first, so the evict works); pass 2 is warm.
"""
import argparse
import json
import os
import re
import time

import numpy as np

W = 12          # 768 bits = 12 x uint64


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def mem():
    s = open("/proc/self/status").read()
    return {k: int(re.search(k + r":\s+(\d+)", s).group(1)) // 1024 for k in ("VmRSS", "VmHWM", "RssAnon", "RssFile")}


def evict(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
    finally:
        os.close(fd)


def summary(times, **kw):
    t = np.array(times)
    return dict(kw, n=len(t), p50=round(float(np.percentile(t, 50)), 2), p90=round(float(np.percentile(t, 90)), 2),
                p99=round(float(np.percentile(t, 99)), 2), mean=round(float(t.mean()), 2))


# ---------------------------------------------------------------- numba kernels
def kernels():
    import numba
    from numba import njit, prange

    @njit(inline="always")
    def pc(x):                                  # SWAR popcount; LLVM emits popcnt on this CPU
        x = x - ((x >> np.uint64(1)) & np.uint64(0x5555555555555555))
        x = (x & np.uint64(0x3333333333333333)) + ((x >> np.uint64(2)) & np.uint64(0x3333333333333333))
        x = (x + (x >> np.uint64(4))) & np.uint64(0x0F0F0F0F0F0F0F0F)
        return (x * np.uint64(0x0101010101010101)) >> np.uint64(56)

    @njit(inline="always")
    def ham(a, i, q):
        d = 0
        for w in range(12):
            d += pc(a[i, w] ^ q[w])
        return d

    @njit
    def push(bd, bp, d, p):                     # keep the k smallest (bd sorted ascending)
        k = bd.shape[0]
        if d >= bd[k - 1]:
            return
        j = k - 1
        while j > 0 and bd[j - 1] > d:
            bd[j] = bd[j - 1]
            bp[j] = bp[j - 1]
            j -= 1
        bd[j] = d
        bp[j] = p

    @njit
    def nearest_lists(cent, q, n):
        d = np.empty(cent.shape[0], np.int64)
        for i in range(cent.shape[0]):
            d[i] = ham(cent, i, q)
        return np.argsort(d, kind="mergesort")[:n]

    @njit
    def ivf_search(codes, offs, lists, q, k, tomb, scodes, soffs):
        """Exact Hamming inside the chosen lists. Positions >= 0 are main rows, < 0 are side rows
        (-1 - j). tomb: a uint8 bitmap over main rows (empty = none)."""
        bd = np.full(k, 1 << 30, np.int64)
        bp = np.full(k, -(1 << 62), np.int64)
        scanned = 0
        has_t = tomb.shape[0] > 0
        has_s = soffs.shape[0] > 0
        for li in lists:
            a, b = offs[li], offs[li + 1]
            scanned += b - a
            if has_t:
                for i in range(a, b):
                    if (tomb[i >> 3] >> (i & 7)) & 1:
                        continue
                    push(bd, bp, ham(codes, i, q), i)
            else:                               # the hot loop without the tombstone branch
                for i in range(a, b):
                    push(bd, bp, ham(codes, i, q), i)
            if has_s:
                a, b = soffs[li], soffs[li + 1]
                scanned += b - a
                for j in range(a, b):
                    push(bd, bp, ham(scodes, j, q), -1 - j)
        return bd, bp, scanned

    @njit(parallel=True)
    def ivf_search_par(codes, offs, lists, q, k, nt):
        """Parallel form (no delta): the chosen lists are dealt round-robin to nt threads, each keeps
        its own top k, merged at the end."""
        bds = np.full((nt, k), 1 << 30, np.int64)
        bps = np.full((nt, k), -(1 << 62), np.int64)
        sc = np.zeros(nt, np.int64)
        for t in prange(nt):
            bd = bds[t]
            bp = bps[t]
            for j in range(t, lists.shape[0], nt):
                li = lists[j]
                a, b = offs[li], offs[li + 1]
                sc[t] += b - a
                for i in range(a, b):
                    push(bd, bp, ham(codes, i, q), i)
        fd = bds.ravel()
        fp = bps.ravel()
        o = np.argsort(fd, kind="mergesort")[:k]
        return fd[o], fp[o], sc.sum()

    @njit(parallel=True)
    def flat_search(codes, q, k, nt):
        n = codes.shape[0]
        step = (n + nt - 1) // nt
        bds = np.full((nt, k), 1 << 30, np.int64)
        bps = np.full((nt, k), -1, np.int64)
        for t in prange(nt):
            bd = bds[t]
            bp = bps[t]
            for i in range(t * step, min(n, (t + 1) * step)):
                push(bd, bp, ham(codes, i, q), i)
        fd = bds.ravel()
        fp = bps.ravel()
        o = np.argsort(fd, kind="mergesort")[:k]
        return fd[o], fp[o]

    return numba, nearest_lists, ivf_search, flat_search, ivf_search_par


# ---------------------------------------------------------------- build
def cmd_build(a):
    import faiss
    faiss.omp_set_num_threads(2)
    d = a.dir
    bits = np.load(os.path.join(d, "bits_u8.npy"), mmap_mode="r")
    ids = np.load(os.path.join(d, "ids.npy"))
    n = len(ids)
    rng = np.random.default_rng(1)
    t = time.time()
    samp = np.sort(rng.choice(n, min(a.sample, n), replace=False))
    x = np.unpackbits(np.asarray(bits[samp]), axis=1).astype(np.float32)
    km = faiss.Kmeans(768, a.lists, niter=a.iters, seed=1, verbose=False, max_points_per_centroid=10**9)
    km.train(x)
    del x
    cent = np.packbits((km.centroids >= 0.5).astype(np.uint8), axis=1)          # majority bit per list
    train_s = time.time() - t
    t = time.time()
    q = faiss.IndexBinaryFlat(768)
    q.add(cent)
    assign = np.empty(n, np.int32)
    for s0 in range(0, n, 500_000):
        _, I = q.search(np.ascontiguousarray(bits[s0:s0 + 500_000]), 1)
        assign[s0:s0 + 500_000] = I[:, 0]
    assign_s = time.time() - t
    order = np.argsort(assign, kind="stable")
    counts = np.bincount(assign, minlength=a.lists)
    offs = np.zeros(a.lists + 1, np.int64)
    offs[1:] = np.cumsum(counts)
    out = os.path.join(d, f"ivf{a.lists}")
    os.makedirs(out, exist_ok=True)
    np.save(os.path.join(out, "centroids.npy"), cent)
    np.save(os.path.join(out, "offsets.npy"), offs)
    assert ids.max() < 2**31
    ids[order].astype(np.int32).tofile(os.path.join(out, "ids.i32"))
    mm = np.memmap(os.path.join(out, "codes.u8"), np.uint8, "w+", shape=(n, 96))
    for s0 in range(0, n, 500_000):
        mm[s0:s0 + 500_000] = bits[order[s0:s0 + 500_000]]
    mm.flush()
    info = {"lists": a.lists, "sample": int(len(samp)), "iters": a.iters, "train_s": round(train_s),
            "assign_s": round(assign_s), "list_min": int(counts.min()), "list_p50": int(np.median(counts)),
            "list_max": int(counts.max()), "empty": int((counts == 0).sum()),
            "bytes": int(n * 96 + n * 4 + cent.nbytes + offs.nbytes), "peak_mb": mem()["VmHWM"]}
    log(json.dumps(info))
    json.dump(info, open(os.path.join(out, "build.json"), "w"))


def cmd_delta(a):
    import faiss
    d = a.dir
    out = os.path.join(d, f"ivf{a.lists}")
    src = os.path.join(d, f"delta_{a.pct}")
    keys = np.load(os.path.join(src, "keys.npy"))
    data = np.load(os.path.join(src, "data.npy"))
    tomb = np.load(os.path.join(src, "tombstones.npy"))
    cent = np.load(os.path.join(out, "centroids.npy"))
    q = faiss.IndexBinaryFlat(768)
    q.add(cent)
    _, I = q.search(np.ascontiguousarray(data), 1)
    assign = I[:, 0]
    order = np.argsort(assign, kind="stable")
    soffs = np.zeros(len(cent) + 1, np.int32)
    soffs[1:] = np.cumsum(np.bincount(assign, minlength=len(cent)))
    od = os.path.join(out, f"delta_{a.pct}")
    os.makedirs(od, exist_ok=True)
    data[order].tofile(os.path.join(od, "codes.u8"))
    keys[order].astype(np.int64).tofile(os.path.join(od, "ids.i64"))
    np.save(os.path.join(od, "offsets.npy"), soffs)
    np.diff(np.sort(tomb), prepend=0).astype(np.uint32).tofile(os.path.join(od, "tombstones.u32"))
    info = {"pct": a.pct, "rows": int(len(keys)), "tombstones": int(len(tomb)),
            "files": {f: os.path.getsize(os.path.join(od, f)) for f in ("codes.u8", "ids.i64", "offsets.npy", "tombstones.u32")}}
    log(json.dumps(info))
    json.dump(info, open(os.path.join(od, "info.json"), "w"))


# ---------------------------------------------------------------- search
def encoder(api_path):
    """The live query encoder (lifted from search_api.py by usearch_spike.lift), CPU device."""
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from usearch_spike import lift
    ns = lift(api_path, {"_PLACE_ABBR", "_STATE_END", "place_expand", "NOMIC_DIR", "NOMIC_PREFIX", "NOMIC_TOKENS",
                         "nomic_pool", "bit_code", "_NomicOV"})
    enc = ns["_NomicOV"](ns["NOMIC_DIR"], "CPU")

    def code(q):
        c = ns["bit_code"](enc(ns["place_expand"](q)))
        return np.packbits(np.frombuffer(c.encode(), np.uint8) - 48).view(np.uint64)
    code("warm up")
    return code


def query_source(a, d):
    """Either the precomputed query codes, or (with --encoder) text -> code in this process."""
    if a.encoder:
        qs = [line.rstrip("\n") for line in open(os.path.join(d, "queries.tsv"), encoding="utf-8") if line.strip()]
        return qs, encoder(a.encoder)
    q = np.load(os.path.join(d, "q.npy")).view(np.uint64)
    return list(q), None


def cmd_ivf(a):
    numba, nearest_lists, ivf_search, _, ivf_search_par = kernels()
    nt = a.threads
    if nt > 1:
        assert not a.delta, "the parallel kernel has no delta path"
        numba.set_num_threads(nt)
    d = a.dir
    out = os.path.join(d, f"ivf{a.lists}")
    cent = np.load(os.path.join(out, "centroids.npy")).view(np.uint64)
    offs = np.load(os.path.join(out, "offsets.npy"))
    n = int(offs[-1])
    cpath, ipath = os.path.join(out, "codes.u8"), os.path.join(out, "ids.i32")
    tomb = np.zeros(0, np.uint8)
    scodes = np.zeros((0, W), np.uint64)
    soffs = np.zeros(0, np.int64)
    sids = np.zeros(0, np.int64)
    if a.delta:
        od = os.path.join(out, f"delta_{a.delta}")
        scodes = np.fromfile(os.path.join(od, "codes.u8"), np.uint8).reshape(-1, 96).view(np.uint64)
        sids = np.fromfile(os.path.join(od, "ids.i64"), np.int64)
        soffs = np.load(os.path.join(od, "offsets.npy")).astype(np.int64)
        tids = np.cumsum(np.fromfile(os.path.join(od, "tombstones.u32"), np.uint32).astype(np.int64))
        mids = np.fromfile(ipath, np.int32)                       # once, at apply time: ids -> main rows
        pos = np.flatnonzero(np.isin(mids, tids))
        del mids
        bitmap = np.zeros((n + 7) // 8, np.uint8)
        np.bitwise_or.at(bitmap, pos >> 3, (1 << (pos & 7)).astype(np.uint8))
        tomb = bitmap
    qsrc, code = query_source(a, d)
    # compile before any timing
    z = np.zeros((1, W), np.uint64)
    ivf_search(z, np.array([0, 1]), np.array([0]), z[0], 10, tomb[:0], scodes[:0], soffs[:0])
    nearest_lists(cent, cent[0], 4)
    if nt > 1:
        ivf_search_par(z, np.array([0, 1]), np.array([0]), z[0], 10, nt)
    res = {"threads": nt, "lists": a.lists, "delta": a.delta, "encoder": bool(a.encoder), "runs": [], "mem_start": mem()}
    for p in [int(x) for x in a.probes.split(",")]:
        for ps in (1, 2):
            if ps == 1:
                evict(cpath)
                evict(ipath)
            codes = np.asarray(np.memmap(cpath, np.uint8, "r", shape=(n, 96))).view(np.uint64)   # plain array for numba
            idsm = np.memmap(ipath, np.int32, "r", shape=(n,))
            times, etimes, scanned, out_ids = [], [], [], []
            for qi in qsrc:
                if code is not None:
                    t = time.perf_counter()
                    qv = code(qi)
                    etimes.append((time.perf_counter() - t) * 1000)
                else:
                    qv = qi
                t = time.perf_counter()
                lists = nearest_lists(cent, qv, p)
                if nt > 1:
                    bd, bp, sc = ivf_search_par(codes, offs, lists, qv, 10, nt)
                else:
                    bd, bp, sc = ivf_search(codes, offs, lists, qv, 10, tomb, scodes, soffs)
                keys = [int(idsm[x]) if x >= 0 else int(sids[-1 - x]) for x in bp if x != -(1 << 62)]
                times.append((time.perf_counter() - t) * 1000)
                scanned.append(sc)
                out_ids.append(keys + [-1] * (10 - len(keys)))
            del codes, idsm
            sc = np.array(scanned)
            run = summary(times, probes=p, threads=nt, **{"pass": ps}, scanned_rows_mean=int(sc.mean()),
                          read_mb_mean=round(float(sc.mean()) * 96 / 1e6, 1), share=round(float(sc.mean()) / n, 4))
            if etimes:
                run["encoder_p50"] = round(float(np.percentile(etimes, 50)), 1)
                run["encoder_p90"] = round(float(np.percentile(etimes, 90)), 1)
            run.update(mem())
            res["runs"].append(run)
            np.save(os.path.join(d, f"res_{a.tag}_p{p}_s{ps}.npy"), np.array(out_ids, np.int64))
            log(json.dumps(run))
    json.dump(res, open(os.path.join(d, f"bench_{a.tag}.json"), "w"))


def cmd_flat(a):
    numba, _, _, flat_search, _ = kernels()
    d = a.dir
    path = os.path.join(d, "bits_u8.npy")
    ids = np.load(os.path.join(d, "ids.npy"), mmap_mode="r")
    qsrc, code = query_source(a, d)
    res = {"encoder": bool(a.encoder), "runs": [], "mem_start": mem()}
    for nt in [int(x) for x in a.threads.split(",")]:
        numba.set_num_threads(nt)
        c0 = np.asarray(np.load(path, mmap_mode="r")).view(np.uint64)
        flat_search(c0[:1000], c0[0], 10, nt)                    # compile
        del c0
        for ps in (1, 2):
            if ps == 1:
                evict(path)
            codes = np.asarray(np.load(path, mmap_mode="r")).view(np.uint64)
            times, etimes, out_ids = [], [], []
            for qi in qsrc[:a.limit]:
                if code is not None:
                    t = time.perf_counter()
                    qv = code(qi)
                    etimes.append((time.perf_counter() - t) * 1000)
                else:
                    qv = qi
                t = time.perf_counter()
                bd, bp = flat_search(codes, qv, 10, nt)
                times.append((time.perf_counter() - t) * 1000)
                out_ids.append([int(ids[x]) for x in bp])
            del codes
            run = summary(times, threads=nt, **{"pass": ps}, read_mb=round(len(ids) * 96 / 1e6, 1))
            if etimes:
                run["encoder_p50"] = round(float(np.percentile(etimes, 50)), 1)
            run.update(mem())
            res["runs"].append(run)
            np.save(os.path.join(d, f"res_{a.tag}_t{nt}_s{ps}.npy"), np.array(out_ids, np.int64))
            log(json.dumps(run))
    json.dump(res, open(os.path.join(d, f"bench_{a.tag}.json"), "w"))


def cmd_faissflat(a):
    import faiss
    d = a.dir
    ids = np.load(os.path.join(d, "ids.npy"), mmap_mode="r")
    q = np.load(os.path.join(d, "q.npy"))
    t = time.time()
    idx = faiss.IndexBinaryFlat(768)
    bits = np.load(os.path.join(d, "bits_u8.npy"), mmap_mode="r")
    for s0 in range(0, len(ids), 500_000):
        idx.add(np.ascontiguousarray(bits[s0:s0 + 500_000]))
    log(f"faiss flat: {idx.ntotal:,} codes in RAM in {time.time() - t:.0f}s; {mem()}")
    res = {"runs": []}
    for nt in [int(x) for x in a.threads.split(",")]:
        faiss.omp_set_num_threads(nt)
        for ps in (1, 2):
            times, out_ids = [], []
            for qv in q[:a.limit]:
                t = time.perf_counter()
                _, I = idx.search(qv[None], 10)
                times.append((time.perf_counter() - t) * 1000)
                out_ids.append([int(ids[x]) for x in I[0]])
            run = summary(times, threads=nt, **{"pass": ps})
            run.update(mem())
            res["runs"].append(run)
            np.save(os.path.join(d, f"res_{a.tag}_t{nt}_s{ps}.npy"), np.array(out_ids, np.int64))
            log(json.dumps(run))
    json.dump(res, open(os.path.join(d, f"bench_{a.tag}.json"), "w"))


def main():
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("build"); p.add_argument("dir"); p.add_argument("lists", type=int)
    p.add_argument("--sample", type=int, default=262_144); p.add_argument("--iters", type=int, default=20)
    p = sp.add_parser("delta"); p.add_argument("dir"); p.add_argument("lists", type=int); p.add_argument("pct", type=int)
    p = sp.add_parser("ivf"); p.add_argument("dir"); p.add_argument("lists", type=int); p.add_argument("tag")
    p.add_argument("--probes", default="25,50,100,200,400"); p.add_argument("--delta", type=int)
    p.add_argument("--threads", type=int, default=1)
    p.add_argument("--encoder")
    p = sp.add_parser("flat"); p.add_argument("dir"); p.add_argument("tag"); p.add_argument("--threads", default="1,2")
    p.add_argument("--encoder"); p.add_argument("--limit", type=int)
    p = sp.add_parser("faissflat"); p.add_argument("dir"); p.add_argument("tag"); p.add_argument("--threads", default="1,2")
    p.add_argument("--limit", type=int)
    a = ap.parse_args()
    globals()["cmd_" + a.cmd](a)


if __name__ == "__main__":
    main()
