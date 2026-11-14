#!/usr/bin/env python3
"""T3 spike (docs/internal/SEARCH-HOME-ENGINE-PLAN.md): can usearch replace pgvector for the
"sem" stage of the downloadable engine? Dev/bench tool only — never shipped from apps/.

Two vector spaces, each in its own directory:
  nomic  wiki_bit: nomic-embed-text-v1.5, 768 signs packed to 96 bytes, Hamming (PRIMARY)
  bge    wiki_vec: bge-small-en-v1.5, 384 x float16, cosine (the live default)

Runs inside a throwaway container (image: search-api + usearch) on testbox. Subcommands:

  export SPACE OUT_DIR --rows N   binary COPY of (id, b|v) on stdin -> ids.npy + bits_u8.npy | vec_f16.npy
  embed SPACE SEARCH_API_PY QUERIES OUT_DIR
                                  queries -> place_expand -> the query encoder lifted from search_api.py
                                  by AST (not copied) -> q.npy; writes the pgvector SQL files
  exact DIR                       brute-force top-10 (numpy) over every row -> exact_ids/exact_sim.npy
  build DIR KIND                  usearch HNSW (connectivity 16, expansion_add 64) -> idx_KIND.usearch
  bench DIR INDEX MODE TAG        single-threaded top-10 per query; MODE view (mmap) | load.
                                  The index file is evicted from the page cache before pass 1
  flat DIR TAG                    usearch exact (brute-force SIMD) scan over the mmap'd codes
  recall DIR TAG...               recall@5/@10 vs exact (tie-tolerant) and id overlap with pgvector
  delta DIR PCT SEED              a simulated month: changed + new vectors -> delta_PCT/ (+ delta index)
  exact_delta DIR PCT             brute-force truth after the month (main - tombstones + delta)
  inplace DIR PCT                 the other shape: remove + add in the main index, save, block diff
"""
import argparse
import ast
import json
import os
import resource
import sys
import time

import numpy as np

NEW_KEY_BASE = 10**12          # brand-new articles in a simulated month get keys above this


def peak_rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


# ---------------------------------------------------------------- the two spaces
class Space:
    def __init__(self, d):
        self.d = d
        self.bits = os.path.exists(os.path.join(d, "bits_u8.npy"))
        self.ids = np.load(os.path.join(d, "ids.npy"))
        self.data = np.load(os.path.join(d, "bits_u8.npy" if self.bits else "vec_f16.npy"), mmap_mode="r")
        self.q = np.load(os.path.join(d, "q.npy"))
        self.ndim = self.data.shape[1] * 8 if self.bits else self.data.shape[1]

    def index(self, kind):
        from usearch.index import Index
        if self.bits:
            return Index(ndim=self.ndim, metric="hamming", dtype="b1", connectivity=16, expansion_add=64)
        return Index(ndim=self.ndim, metric="cos", dtype=kind, connectivity=16, expansion_add=64)

    def rows(self, a, b=None):
        x = self.data[a] if b is None else self.data[a:b]
        return np.asarray(x) if self.bits else np.asarray(x, np.float32)


def sims(bits, q, blk):
    """Similarity of each query to each row: -Hamming for bit codes, cosine (unit vectors) otherwise."""
    if bits:
        qw = np.ascontiguousarray(q).view(np.uint64)            # (nq, 12)
        bw = np.ascontiguousarray(blk).view(np.uint64)          # (n, 12)
        out = np.empty((len(qw), len(bw)), np.float32)
        for i, w in enumerate(qw):
            out[i] = -np.bitwise_count(bw ^ w).sum(1, dtype=np.int32)
        return out
    return q @ np.asarray(blk, np.float32).T


def brute(bits, data, q, k, keep=None, chunk=250_000):
    nq = len(q)
    best_s = np.full((nq, k), -1e9, np.float32)
    best_p = np.full((nq, k), -1, np.int64)
    for s0 in range(0, len(data), chunk):
        sc = sims(bits, q, data[s0:s0 + chunk])
        if keep is not None:
            sc[:, ~keep[s0:s0 + chunk]] = -1e9
        part = np.argpartition(-sc, k, axis=1)[:, :k]
        ps = np.take_along_axis(sc, part, 1)
        cs = np.concatenate([best_s, ps], 1)
        cp = np.concatenate([best_p, part + s0], 1)
        o = np.argsort(-cs, 1, kind="stable")[:, :k]
        best_s, best_p = np.take_along_axis(cs, o, 1), np.take_along_axis(cp, o, 1)
    return best_p, best_s


# ---------------------------------------------------------------- export
def cmd_export(a):
    """Postgres binary COPY, fixed-size rows. bit(768): varbit_send = int32 bit length + 96 bytes,
    box 0 in the high bit of byte 0 (= np.packbits order). halfvec(384): int16 dim, int16 unused,
    384 float16. All big-endian."""
    head = [("nf", ">i2"), ("l1", ">i4"), ("id", ">i8"), ("l2", ">i4")]
    if a.space == "nomic":
        row = np.dtype(head + [("bitlen", ">i4"), ("x", "u1", (96,))])
        out_name, out_dtype, width = "bits_u8.npy", np.uint8, 96
    else:
        row = np.dtype(head + [("dim", ">i2"), ("unused", ">i2"), ("x", ">f2", (384,))])
        out_name, out_dtype, width = "vec_f16.npy", np.float16, 384
    f = sys.stdin.buffer
    hdr = f.read(19)
    assert hdr[:11] == b"PGCOPY\n\xff\r\n\0", hdr
    assert int.from_bytes(hdr[15:19], "big") == 0
    os.makedirs(a.out, exist_ok=True)
    n = a.rows
    ids = np.empty(n, np.int64)
    out = np.lib.format.open_memmap(os.path.join(a.out, out_name), "w+", out_dtype, (n, width))
    got, t0, pend = 0, time.time(), b""
    while True:
        buf = pend + f.read(row.itemsize * 65536)
        cut = len(buf) - len(buf) % row.itemsize
        body, pend = buf[:cut], buf[cut:]
        if body:
            r = np.frombuffer(body, row)
            assert (r["nf"] == 2).all() and (r["l1"] == 8).all() and (r["l2"] == row.itemsize - 18).all()
            ids[got:got + len(r)] = r["id"]
            out[got:got + len(r)] = r["x"].astype(out_dtype)
            got += len(r)
        if not body:
            assert pend == b"\xff\xff", pend[:20]              # COPY trailer
            break
    assert got == n, (got, n)
    out.flush()
    np.save(os.path.join(a.out, "ids.npy"), ids)
    for i in range(3):
        x = out[i]
        show = "".join(map(str, np.unpackbits(x)[:24])) if a.space == "nomic" else x[:4].tolist()
        log("row", i, ids[i], show)
    log(f"exported {got:,} rows in {time.time() - t0:.0f}s")


# ---------------------------------------------------------------- query embedding
def lift(path: str, names: set) -> dict:
    """Execute only the named top-level defs/assignments of search_api.py (no import side effects)."""
    tree = ast.parse(open(path, encoding="utf-8").read())
    keep = [n for n in tree.body
            if (isinstance(n, (ast.FunctionDef, ast.ClassDef)) and n.name in names)
            or (isinstance(n, ast.Assign) and any(getattr(t, "id", None) in names for t in n.targets))]
    ns = {"os": os}
    exec(compile(ast.Module(body=keep, type_ignores=[]), path, "exec"), ns)
    missing = names - set(ns)
    assert not missing, missing
    return ns


def read_queries(path):
    return [q for q in (line.strip() for line in open(path, encoding="utf-8")) if q and not q.startswith("#")]


def cmd_embed(a):
    common = {"_PLACE_ABBR", "_STATE_END", "place_expand"}
    if a.space == "nomic":
        ns = lift(a.api, common | {"NOMIC_DIR", "NOMIC_PREFIX", "NOMIC_TOKENS", "nomic_pool", "bit_code", "_NomicOV"})
        # Same class the live service uses; the CPU device (no iGPU in a throwaway container).
        # 12-nomic-1bit.md: the IR on CPU matches fp32 at cosine 1.0000 / 99.98% of boxes.
        enc = ns["_NomicOV"](ns["NOMIC_DIR"], "CPU")
        f = lambda t: enc(t)                                     # noqa: E731
    else:
        ns = lift(a.api, common | {"MODEL_DIR", "QUERY_PREFIX", "_Embedder"})
        enc = ns["_Embedder"](ns["MODEL_DIR"])
        f = lambda t: enc(t)                                     # noqa: E731
    qs = read_queries(a.queries)
    f("warm up")
    lits, qv, times = [], [], []
    for q in qs:
        wq = ns["place_expand"](q)
        t = time.perf_counter()
        e = f(wq)
        times.append((time.perf_counter() - t) * 1000)
        if a.space == "nomic":
            code = ns["bit_code"](e)                             # what _sem_rows sends to wiki_bit
            lits.append(f"'{code}'::bit(768)")
            qv.append(np.packbits(np.frombuffer(code.encode(), np.uint8) - 48))
        else:
            lit = "[" + ",".join(f"{x:.5f}" for x in e) + "]"    # what _sem_rows sends to wiki_vec
            lits.append(f"'{lit}'::halfvec")
            qv.append(np.array([float(x) for x in lit[1:-1].split(",")], np.float32))
    np.save(os.path.join(a.out, "q.npy"), np.array(qv))
    with open(os.path.join(a.out, "queries.tsv"), "w", encoding="utf-8") as fo:
        fo.write("\n".join(qs) + "\n")
    if a.space == "nomic":
        setup, tbl, col, op = f"SET ivfflat.probes = {a.probes};", "wiki_bit", "b", "<~>"
        served = ("SELECT w.title, w.lead, 1 - b.d / 768.0 AS sim FROM (SELECT id, (b <~> {l}) AS d FROM wiki_bit "
                  "ORDER BY b <~> {l} LIMIT 5) b JOIN wiki_docs w ON w.page_id = b.id ORDER BY b.d;")
    else:
        setup, tbl, col, op = "SET hnsw.ef_search = 40;", "wiki_vec", "v", "<=>"
        served = ("SELECT w.title, w.lead, 1 - (v.v <=> {l}) AS sim FROM wiki_vec v JOIN wiki_docs w "
                  "ON w.page_id = v.id ORDER BY v.v <=> {l} LIMIT 5;")
    idq = f"SELECT id FROM {tbl} ORDER BY {col} {op} {{l}} LIMIT 10"
    with open(os.path.join(a.out, "pg_ids.sql"), "w") as fo:
        fo.write(setup + "\n")
        for i, lit in enumerate(lits):
            fo.write(f"SELECT {i} || chr(9) || coalesce(string_agg(id::text, ',' ORDER BY rk), '') FROM "
                     f"(SELECT id, row_number() OVER () rk FROM ({idq.format(l=lit)}) s) z;\n")
    for name, sql in (("pg_time_served.sql", served), ("pg_time_ids.sql", idq + ";")):
        with open(os.path.join(a.out, name), "w") as fo:
            fo.write(setup + "\n\\o /dev/null\n\\timing on\n")
            for lit in lits:
                fo.write(sql.format(l=lit) + "\n")
    t = np.array(times)
    log(f"{a.space}: embedded {len(qs)} queries; encoder ms p50 {np.percentile(t, 50):.1f} p90 {np.percentile(t, 90):.1f}")


# ---------------------------------------------------------------- exact truth
def cmd_exact(a):
    s = Space(a.dir)
    t = time.time()
    p, sc = brute(s.bits, s.data, s.q, 10)
    np.save(os.path.join(a.dir, "exact_ids.npy"), s.ids[p])
    np.save(os.path.join(a.dir, "exact_sim.npy"), sc)
    log(f"exact top-10 for {len(s.q)} queries over {len(s.ids):,} in {time.time() - t:.0f}s")


# ---------------------------------------------------------------- build
def cmd_build(a):
    import usearch
    s = Space(a.dir)
    idx = s.index(a.kind)
    t = time.time()
    step = 200_000
    for s0 in range(0, len(s.ids), step):
        idx.add(s.ids[s0:s0 + step], s.rows(s0, s0 + step), threads=a.threads)
        if (s0 // step) % 5 == 0:
            log(f"{a.kind}: {min(s0 + step, len(s.ids)):,} added, {time.time() - t:.0f}s, rss {peak_rss_mb():.0f} MB")
    build_s = time.time() - t
    path = os.path.join(a.dir, f"idx_{a.kind}.usearch")
    t = time.time()
    idx.save(path)
    info = {"kind": a.kind, "n": len(idx), "build_s": round(build_s), "save_s": round(time.time() - t),
            "bytes": os.path.getsize(path), "peak_rss_mb": round(peak_rss_mb()), "usearch": usearch.__version__,
            "hardware": str(idx.hardware_acceleration), "levels": idx.nlevels, "threads": a.threads}
    log(json.dumps(info))
    with open(os.path.join(a.dir, f"build_{a.kind}.json"), "w") as f:
        json.dump(info, f)


# ---------------------------------------------------------------- bench
def evict(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
    finally:
        os.close(fd)


def summary(times, **kw):
    tt = np.array(times)
    return dict(kw, p50=round(float(np.percentile(tt, 50)), 2), p90=round(float(np.percentile(tt, 90)), 2),
                p99=round(float(np.percentile(tt, 99)), 2), mean=round(float(tt.mean()), 2), rss_mb=round(peak_rss_mb()))


def cmd_bench(a):
    from usearch.index import Index
    q = np.load(os.path.join(a.dir, "q.npy"))[:a.limit]       # tight caps thrash: a prefix of the set
    evict(a.index)
    t = time.time()
    idx = Index.restore(a.index, view=(a.mode == "view"))
    open_ms = (time.time() - t) * 1000
    delta = tomb = None
    if a.delta:
        delta = Index.restore(a.delta, view=False)
        tomb = np.load(a.tomb)
    log(f"opened {a.index} mode={a.mode} in {open_ms:.0f} ms; hw={idx.hardware_acceleration}; "
        f"rss {peak_rss_mb():.0f} MB" + (f"; delta {len(delta):,}, tombstones {len(tomb):,}" if delta else ""))
    res = {"open_ms": round(open_ms), "mode": a.mode, "index": a.index, "runs": []}
    over = 10                                   # extra main results fetched to absorb tombstoned ids
    for exp in [int(x) for x in a.exp.split(",")]:
        idx.expansion_search = exp
        if delta is not None:
            delta.expansion_search = exp
        for ps in range(1, a.passes + 1):
            if ps == 1 and a.evict_each:
                evict(a.index)
            times, out, short = [], [], 0
            for qv in q:
                t = time.perf_counter()
                if delta is None:
                    keys = idx.search(qv, 10, threads=1).keys[:10]
                else:
                    m = idx.search(qv, 10 + over, threads=1)
                    pos = np.minimum(np.searchsorted(tomb, m.keys), len(tomb) - 1)   # tomb is sorted
                    keep = tomb[pos] != m.keys
                    mk, md = m.keys[keep], m.distances[keep]
                    dm = delta.search(qv, 10, threads=1)
                    ak = np.concatenate([mk, dm.keys])
                    ad = np.concatenate([md, dm.distances])
                    keys = ak[np.argsort(ad, kind="stable")][:10]
                    short += len(mk) < 10
                times.append((time.perf_counter() - t) * 1000)
                out.append(list(map(int, keys)) + [-1] * (10 - len(keys)))
            run = summary(times, exp=exp, **{"pass": ps}, short_main=short)
            res["runs"].append(run)
            np.save(os.path.join(a.dir, f"res_{a.tag}_e{exp}_p{ps}.npy"), np.array(out, np.int64))
            log(json.dumps(run))
    with open(os.path.join(a.dir, f"bench_{a.tag}.json"), "w") as f:
        json.dump(res, f)


def cmd_flat(a):
    """No graph at all: usearch's exact SIMD scan over the mmap'd codes, one query at a time."""
    from usearch.index import search, MetricKind
    s = Space(a.dir)
    path = os.path.join(a.dir, "bits_u8.npy" if s.bits else "vec_f16.npy")
    evict(path)
    data = np.load(path, mmap_mode="r")
    metric = MetricKind.Hamming if s.bits else MetricKind.Cos
    res = {"runs": []}
    for ps in range(1, a.passes + 1):
        times, out = [], []
        for qv in s.q:
            t = time.perf_counter()
            m = search(data, qv, 10, metric, exact=True, threads=a.threads)
            times.append((time.perf_counter() - t) * 1000)
            out.append(list(map(int, s.ids[m.keys[:10]])))
        run = summary(times, **{"pass": ps}, threads=a.threads)
        res["runs"].append(run)
        np.save(os.path.join(a.dir, f"res_{a.tag}_p{ps}.npy"), np.array(out, np.int64))
        log(json.dumps(run))
    with open(os.path.join(a.dir, f"bench_{a.tag}.json"), "w") as f:
        json.dump(res, f)


# ---------------------------------------------------------------- recall
def load_pg(path, n):
    out = np.full((n, 10), -1, np.int64)
    for line in open(path):
        i, _, rest = line.rstrip("\n").partition("\t")
        ks = [int(x) for x in rest.split(",") if x]
        out[int(i), :len(ks)] = ks
    return out


def overlap(found, ref, k):
    return float(np.mean([len((set(found[i, :k]) & set(ref[i, :k])) - {-1}) / k for i in range(len(found))]))


def cmd_recall(a):
    s = Space(a.dir)
    ex = np.load(a.exact_ids or os.path.join(a.dir, "exact_ids.npy"))
    exs = np.load(a.exact_sim or os.path.join(a.dir, "exact_sim.npy"))
    order = np.argsort(s.ids)
    sid = s.ids[order]
    extra = {}
    if a.delta_dir:
        dk = np.load(os.path.join(a.delta_dir, "keys.npy"))
        dv = np.load(os.path.join(a.delta_dir, "data.npy"))
        extra = dict(zip(map(int, dk), dv))

    def simfn(i, keys):
        out = []
        for k in map(int, keys):
            if k in extra:
                row = extra[k]
            else:
                p = np.searchsorted(sid, k)
                if not (p < len(sid) and sid[p] == k):
                    out.append(-1e9)
                    continue
                row = s.data[order[p]]
            out.append(float(sims(s.bits, s.q[i:i + 1], np.asarray(row)[None])[0, 0]))
        return np.array(out)

    def tolerant(found, k):                            # found may be a prefix of the query set
        """A hit counts if its true similarity reaches the k-th exact one (ties, duplicate rows)."""
        hits = 0
        for i in range(len(found)):
            hits += int((simfn(i, found[i, :k]) >= exs[i, k - 1] - 1e-4).sum())
        return hits / (len(found) * k)

    pgp = os.path.join(a.dir, "pg_ids.tsv")
    pg = load_pg(pgp, len(s.q)) if os.path.exists(pgp) and not a.delta_dir else None
    tags = (["pgvector"] if pg is not None else []) + list(a.tags)
    rows = []
    for tag in tags:
        found = pg if tag == "pgvector" else np.load(os.path.join(a.dir, f"res_{tag}.npy"))
        r = {"tag": tag, "n": len(found), "exact@10": round(tolerant(found, 10), 4), "exact@5": round(tolerant(found, 5), 4),
             "exact@1": round(tolerant(found, 1), 4), "ids_exact@10": round(overlap(found, ex[:len(found)], 10), 4)}
        if pg is not None and tag != "pgvector":
            r["pg@10"] = round(overlap(found, pg[:len(found)], 10), 4)
            r["pg@5"] = round(overlap(found, pg[:len(found)], 5), 4)
        rows.append(r)
        print(json.dumps(r), flush=True)
    with open(os.path.join(a.delta_dir or a.dir, f"recall_{a.name}.json"), "w") as f:
        json.dump(rows, f)


# ---------------------------------------------------------------- delta (a simulated month)
def cmd_delta(a):
    """PCT% of articles change and PCT/10 % brand-new ones appear. A changed vector is 0.8 of its old
    self + 0.2 of another article (bits: 20% of the boxes taken from the other article); a new one is
    half of two articles, under a new key. The month ships as a separate small index of every
    changed/new vector plus tombstones for the replaced main-index ids."""
    s = Space(a.dir)
    rng = np.random.default_rng(a.seed)
    n = len(s.ids)
    nch, nnew = int(n * a.pct / 100), int(n * a.pct / 1000)
    ch = np.sort(rng.choice(n, nch, replace=False))
    oth = np.sort(rng.integers(0, n, nch))
    na, nb = np.sort(rng.integers(0, n, nnew)), np.sort(rng.integers(0, n, nnew))
    if s.bits:
        def mix(x, y, share, step=50_000):          # chunked: a 10% month OOM-killed at 4 GB unchunked
            out = []
            for i in range(0, len(x), step):
                bx = np.unpackbits(s.rows(x[i:i + step]), axis=1)
                by = np.unpackbits(s.rows(y[i:i + step]), axis=1)
                take = rng.random(bx.shape, np.float32) < share
                out.append(np.packbits(np.where(take, by, bx), axis=1))
            return np.concatenate(out)
        data = np.concatenate([mix(ch, oth, 0.2), mix(na, nb, 0.5)])
    else:
        v = np.concatenate([0.8 * s.rows(ch) + 0.2 * s.rows(oth), 0.5 * s.rows(na) + 0.5 * s.rows(nb)])
        data = (v / np.linalg.norm(v, axis=1, keepdims=True)).astype(np.float16)
    keys = np.concatenate([s.ids[ch], NEW_KEY_BASE + np.arange(nnew)])
    out = os.path.join(a.dir, f"delta_{a.pct}")
    os.makedirs(out, exist_ok=True)
    np.save(os.path.join(out, "keys.npy"), keys)
    np.save(os.path.join(out, "data.npy"), data)
    tomb = np.sort(s.ids[ch])
    np.save(os.path.join(out, "tombstones.npy"), tomb)
    np.diff(tomb, prepend=0).astype(np.uint32).tofile(os.path.join(out, "tombstones.u32"))  # as shipped: gaps
    t = time.time()
    idx = s.index(a.kind)
    idx.add(keys, data if s.bits else data.astype(np.float32), threads=a.threads)
    build_s = time.time() - t
    p = os.path.join(out, f"delta_{a.kind}.usearch")
    idx.save(p)
    info = {"pct": a.pct, "changed": nch, "new": nnew, "delta_vectors": int(len(keys)), "build_s": round(build_s, 1),
            "delta_bytes": os.path.getsize(p), "tomb_raw_bytes": os.path.getsize(os.path.join(out, "tombstones.u32"))}
    log(json.dumps(info))
    with open(os.path.join(out, f"info_{a.kind}.json"), "w") as f:
        json.dump(info, f)


def cmd_exact_delta(a):
    s = Space(a.dir)
    out = os.path.join(a.dir, f"delta_{a.pct}")
    tomb = np.load(os.path.join(out, "tombstones.npy"))
    p, sc = brute(s.bits, s.data, s.q, 10, ~np.isin(s.ids, tomb))
    dk = np.load(os.path.join(out, "keys.npy"))
    p2, s2 = brute(s.bits, np.load(os.path.join(out, "data.npy")), s.q, 10)
    allk, alls = np.concatenate([s.ids[p], dk[p2]], 1), np.concatenate([sc, s2], 1)
    o = np.argsort(-alls, 1, kind="stable")[:, :10]
    np.save(os.path.join(out, "exact_ids.npy"), np.take_along_axis(allk, o, 1))
    np.save(os.path.join(out, "exact_sim.npy"), np.take_along_axis(alls, o, 1))
    log(f"exact after month {a.pct}%: done")


def cmd_inplace(a):
    """The other update shape: one index file edited in place (remove + add), then saved."""
    from usearch.index import Index
    s = Space(a.dir)
    out = os.path.join(a.dir, f"delta_{a.pct}")
    src = os.path.join(a.dir, f"idx_{a.kind}.usearch")
    t = time.time()
    idx = Index.restore(src, view=False)
    load_s = time.time() - t
    tomb = np.load(os.path.join(out, "tombstones.npy"))
    keys = np.load(os.path.join(out, "keys.npy"))
    data = np.load(os.path.join(out, "data.npy"))
    t = time.time()
    removed = idx.remove(tomb)
    rm_s = time.time() - t
    t = time.time()
    idx.add(keys, data if s.bits else data.astype(np.float32), threads=a.threads)
    add_s = time.time() - t
    dst = os.path.join(out, f"inplace_{a.kind}.usearch")
    idx.save(dst)
    info = {"pct": a.pct, "load_s": round(load_s), "removed": int(np.sum(removed)), "remove_s": round(rm_s, 1),
            "add_s": round(add_s, 1), "size": len(idx), "bytes_before": os.path.getsize(src),
            "bytes_after": os.path.getsize(dst), "peak_rss_mb": round(peak_rss_mb())}
    for bs in (4096, 65536):                      # fixed-offset block diff (a naive block updater)
        diff = 0
        with open(src, "rb") as f1, open(dst, "rb") as f2:
            while True:
                b1, b2 = f1.read(bs), f2.read(bs)
                if not b1 and not b2:
                    break
                diff += b1 != b2
        info[f"changed_bytes_{bs}"] = diff * bs
    log(json.dumps(info))
    with open(os.path.join(out, f"inplace_{a.kind}.json"), "w") as f:
        json.dump(info, f)


def main():
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("export"); p.add_argument("space", choices=["nomic", "bge"]); p.add_argument("out")
    p.add_argument("--rows", type=int, required=True)
    p = sp.add_parser("embed"); p.add_argument("space", choices=["nomic", "bge"]); p.add_argument("api")
    p.add_argument("queries"); p.add_argument("out"); p.add_argument("--probes", type=int, default=100)
    p = sp.add_parser("exact"); p.add_argument("dir")
    p = sp.add_parser("build"); p.add_argument("dir"); p.add_argument("kind"); p.add_argument("--threads", type=int, default=2)
    p = sp.add_parser("bench"); p.add_argument("dir"); p.add_argument("index"); p.add_argument("mode", choices=["view", "load"])
    p.add_argument("tag"); p.add_argument("--exp", default="40,64,128"); p.add_argument("--passes", type=int, default=2)
    p.add_argument("--evict-each", action="store_true"); p.add_argument("--delta"); p.add_argument("--tomb")
    p.add_argument("--limit", type=int)
    p = sp.add_parser("flat"); p.add_argument("dir"); p.add_argument("tag"); p.add_argument("--passes", type=int, default=2)
    p.add_argument("--threads", type=int, default=1)
    p = sp.add_parser("recall"); p.add_argument("dir"); p.add_argument("name"); p.add_argument("tags", nargs="*")
    p.add_argument("--exact-ids"); p.add_argument("--exact-sim"); p.add_argument("--delta-dir")
    p = sp.add_parser("delta"); p.add_argument("dir"); p.add_argument("pct", type=int); p.add_argument("seed", type=int)
    p.add_argument("--kind", default="i8"); p.add_argument("--threads", type=int, default=2)
    p = sp.add_parser("exact_delta"); p.add_argument("dir"); p.add_argument("pct", type=int)
    p = sp.add_parser("inplace"); p.add_argument("dir"); p.add_argument("pct", type=int)
    p.add_argument("--kind", default="i8"); p.add_argument("--threads", type=int, default=2)
    a = ap.parse_args()
    globals()["cmd_" + a.cmd](a)


if __name__ == "__main__":
    main()
