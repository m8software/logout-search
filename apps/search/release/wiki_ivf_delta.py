#!/usr/bin/env python3
"""Meaning-search side of the Wikipedia refresh: embed the changed articles, merge their 1-bit codes into the IVF folder.

    wiki_ivf_delta.py texts CHANGED.tsv.gz OUT_TEXTS.tsv.gz
        one `page_id TAB title. lead` row per new/changed article (the text the live codes were made from: buildbox's
        wiki_texts.tsv.gz is `title. lead[:600]`), for tools/embed-full.py nomic TEXTS OUTDIR on a GPU box
    wiki_ivf_delta.py merge PREV_IVF OUT_IVF RELEASE --vectors DIR [DIR ...]
                      --changed CHANGED.tsv.gz --deleted deleted.tsv [--no-drift-check]
        DIR holds embed-full.py's nomic-NNNN.ids.npy (page_ids) + nomic-NNNN.vecs.npy (float16, N x 768). The code is
        bit = component >= 0, MSB first (ivf_build.ENCODER). Ids are Wikipedia PAGE ids (sem_ivf.py). Removed = deleted +
        re-embedded articles; added = re-embedded articles. The centres do not move (update_common.ivf_merge); the drift
        guard (update_common.ivf_drift) refuses a merge that no longer fits, and then ivf_build.py must re-cluster.
Every changed row that is not an alias-only row must have a vector: a missing one aborts.
"""
from __future__ import annotations

import argparse
import glob
import gzip
import json
import os
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "importer"))
import update_common as uc  # noqa: E402
import wiki_text_delta as T  # noqa: E402


def log(msg: str) -> None:
    print(f"[wiki-ivf-delta] {msg}", flush=True)


LEAD_CHARS = 600           # measured 2026-11-15 against buildbox's wiki_texts.tsv.gz: 2,534 of 2,954 sampled texts equal `title. lead[:600]`
                           # exactly, the other 420 differ only by a trailing space, which the tokenizer ignores


def embed_text(title: str, lead: str) -> str:
    """The text the live codes were made from: `title. ` + the first 600 characters of the lead (tabs and newlines to spaces)."""
    return (title + ". " + lead[:LEAD_CHARS]).replace("\t", " ").replace("\n", " ").replace("\r", " ")


def changed_rows(path: str):
    """(op, page_id, title, lead) of every row that needs a fresh vector (alias-only rows keep theirs)."""
    import wikidelta as D
    with gzip.open(path, "rt", encoding="utf-8", newline="") as f:
        for line in f:
            p = line.rstrip("\n").split("\t")
            if p[0] != "alias_only":
                yield p[0], int(p[1]), D.unescape(p[3]) or "", D.unescape(p[5]) or ""


def cmd_texts(a) -> None:
    """--split N writes OUT.0 .. OUT.N-1 (round robin by row, so every file has the same mix of long and short leads)."""
    n = 0
    k = max(1, a.split)
    outs = [gzip.open(a.out if k == 1 else f"{a.out}.{i}", "wt", encoding="utf-8") for i in range(k)]
    for _op, pid, title, lead in changed_rows(a.changed):
        outs[n % k].write(f"{pid}\t{embed_text(title, lead)}\n")
        n += 1
    for f in outs:
        f.close()
    log(f"{n} texts -> {a.out}" + (f".0..{k - 1}" if k > 1 else ""))


def load_codes(vec_dirs) -> dict[int, np.ndarray]:
    """Codes from one or several embed-full.py output folders (rows split across machines)."""
    out: dict[int, np.ndarray] = {}
    for d in ([vec_dirs] if isinstance(vec_dirs, str) else vec_dirs):
        # embed-full.py shards (nomic-NNNN: float16 vectors) and wiki_embed_pod.py shards (shard-NNNN: packed bits already)
        for ip in sorted(glob.glob(os.path.join(d, "nomic-*.ids.npy")) + glob.glob(os.path.join(d, "shard-*.ids.npy"))):
            if os.path.basename(ip).startswith("shard-"):
                vp = ip.replace(".ids.npy", ".bits.npy")
                if not os.path.exists(vp):
                    continue
                ids, codes = np.load(ip), np.load(vp)
            else:
                vp = ip.replace(".ids.npy", ".vecs.npy")
                if not os.path.exists(vp):
                    continue                                # a shard without its vecs file is not complete (embed-full.py writes it last)
                ids, vecs = np.load(ip), np.load(vp)
                codes = np.packbits(vecs >= 0, axis=1)      # MSB first, 768 -> 96 bytes
            for i, c in zip(ids.tolist(), codes):
                if int(i) in out and not (out[int(i)] == c).all():
                    sys.exit(f"page {int(i)} was embedded twice with different bits (two devices disagree): keep one device")
                out[int(i)] = c
    return out


def _text_rows(path: str):
    with gzip.open(path, "rt", encoding="utf-8", errors="replace") as f:
        for line in f:
            i, _, text = line.rstrip("\n").partition("\t")
            if i:
                yield int(i), text


def cmd_reuse(a) -> None:
    """Vectors made for an earlier text file are kept for every article whose text is the same in the new one (rc16: the
    catch-up was embedded while the daily runs still went). OUT_VECS gets shard-reuse.{ids,bits}.npy with the kept codes;
    TODO gets the rows that still need the embedder (their shards go into OUT_VECS next to it: the ids do not overlap)."""
    import hashlib
    h = lambda t: hashlib.blake2b(t.encode("utf-8", "replace"), digest_size=8).digest()   # noqa: E731
    old = {pid: h(text) for pid, text in _text_rows(a.old_texts)}
    codes = load_codes(a.old_vecs)
    ids, keep, todo = [], [], 0
    os.makedirs(a.out_vecs, exist_ok=True)
    with gzip.open(a.todo, "wt", encoding="utf-8") as out:
        for pid, text in _text_rows(a.new_texts):
            if old.get(pid) == h(text) and pid in codes:
                ids.append(pid); keep.append(codes[pid])
            else:
                out.write(f"{pid}\t{text}\n"); todo += 1
    np.save(os.path.join(a.out_vecs, "shard-reuse.ids.npy"), np.array(ids, np.int64))
    np.save(os.path.join(a.out_vecs, "shard-reuse.bits.npy"), np.stack(keep) if keep else np.zeros((0, uc.DIM_BYTES), np.uint8))
    log(json.dumps({"stage": "reuse", "kept": len(ids), "todo": todo}))


def cmd_merge(a) -> None:
    t0 = time.time()
    page_ids = {pid for _o, pid, _t, _l in changed_rows(a.changed)}      # ids only: the leads of a 2 M-row set stay out of memory
    deleted = [int(l.split("\t")[0]) for l in open(a.deleted, encoding="utf-8") if l.strip()]
    codes = load_codes(a.vectors)
    missing = [pid for pid in page_ids if pid not in codes]
    if missing:
        sys.exit(f"{len(missing)} re-embedded articles have no vector (first {missing[:5]}): the embedding run is not complete")
    # the folder's ids are Wikipedia PAGE ids (sem_ivf.py: ids.i32 = page_id of each row), not registry doc ids
    removed = np.array(sorted(page_ids | set(deleted)), np.int64)
    order = sorted(page_ids)
    added = np.array(order, np.int64)
    new_codes = np.stack([codes[p] for p in order]) if order else np.zeros((0, uc.DIM_BYTES), np.uint8)
    info = uc.ivf_merge(a.prev_ivf, a.out_ivf, removed, added, new_codes, a.release)
    pm = json.load(open(os.path.join(a.prev_ivf, "ivf.json"), encoding="utf-8"))
    stats = uc.ivf_stats(a.out_ivf)
    merges = int(json.load(open(os.path.join(a.out_ivf, "ivf.json")))["merges"])
    base = pm.get("trained") or uc.ivf_stats(a.prev_ivf)
    drift = uc.ivf_drift(stats, base, merges, dict(uc.IVF_LIMITS, merges=a.merges_limit))
    res = {"stage": "wiki-ivf-delta", "removed": info["removed"], "added": info["added"], "rows": info["rows"], "drift": drift,
           "seconds": round(time.time() - t0, 1)}
    log(json.dumps(res, sort_keys=True))
    if drift["rebuild"] and not a.no_drift_check:
        import shutil
        shutil.rmtree(a.out_ivf)
        sys.exit(f"the merged folder drifted past its limits ({drift['reasons']}): re-cluster with ivf_build.py --previous --force-rebuild")


_POP = np.array([bin(i).count("1") for i in range(256)], np.uint8)


def ivf_recall(folder: str, queries: int, k: int, probes: int, seed: int = 1) -> dict:
    """recall@k of the IVF probe search against the exact Hamming top-k over the same folder, for a fixed sample of its own
    codes as queries (a code is its own nearest neighbour, so it is dropped from both lists). The same seed picks the same
    positions, so two folders of the same release are compared on comparable queries."""
    meta = json.load(open(os.path.join(folder, "ivf.json"), encoding="utf-8"))
    offs = np.fromfile(os.path.join(folder, "offsets.i64"), np.int64)
    n = int(offs[-1])
    cent = np.fromfile(os.path.join(folder, "centroids.u8"), np.uint8).reshape(-1, uc.DIM_BYTES)
    ids = np.fromfile(os.path.join(folder, "ids.i32"), np.int32)
    codes = np.memmap(os.path.join(folder, "codes.u8"), np.uint8, "r", shape=(n, uc.DIM_BYTES))
    rng = np.random.default_rng(seed)
    q_rows = rng.choice(n, min(queries, n), replace=False)
    hits = tot = 0
    for qr in q_rows:
        q = np.asarray(codes[qr])
        exact = np.empty(n, np.uint16)
        for s0 in range(0, n, 1_000_000):
            exact[s0:s0 + 1_000_000] = _POP[np.bitwise_xor(codes[s0:s0 + 1_000_000], q)].sum(1, dtype=np.uint16)
        exact[qr] = 65535
        truth = set(ids[np.argsort(exact, kind="stable")[:k]].tolist())
        order = np.argsort(_POP[np.bitwise_xor(cent, q)].sum(1, dtype=np.uint16), kind="stable")[:probes]
        cand = np.concatenate([np.arange(offs[l], offs[l + 1]) for l in order])
        cand = cand[cand != qr]
        d = _POP[np.bitwise_xor(codes[cand], q)].sum(1, dtype=np.uint16)
        got = set(ids[cand[np.argsort(d, kind="stable")[:k]]].tolist())
        hits += len(truth & got)
        tot += len(truth)
    return {"recall": round(hits / max(tot, 1), 4), "queries": int(len(q_rows)), "k": k, "probes": probes, "rows": n, "merges": int(meta.get("merges", 0))}


def cmd_recall(a) -> None:
    cur = ivf_recall(a.folder, a.queries, a.k, a.probes)
    out = {"folder": cur}
    if a.fresh:
        fresh = ivf_recall(a.fresh, a.queries, a.k, a.probes)
        out["fresh"] = fresh
        out["drop"] = round(fresh["recall"] - cur["recall"], 4)
        out["ok"] = out["drop"] <= a.max_drop
    else:
        out["ok"] = cur["recall"] >= a.min_recall
    print(json.dumps(out, sort_keys=True), flush=True)
    if not out["ok"]:
        sys.exit("the merged folder lost recall: force a re-cluster now (ivf_build.py --previous --force-rebuild)")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("texts"); t.add_argument("changed"); t.add_argument("out"); t.add_argument("--split", type=int, default=1)
    ru = sub.add_parser("reuse", help="keep earlier vectors for articles whose text did not change; list the rest for the embedder")
    ru.add_argument("old_texts"); ru.add_argument("old_vecs"); ru.add_argument("new_texts"); ru.add_argument("out_vecs"); ru.add_argument("todo")
    m = sub.add_parser("merge")
    m.add_argument("prev_ivf"); m.add_argument("out_ivf"); m.add_argument("release")
    m.add_argument("--vectors", required=True, nargs="+", help="one or more embed-full.py output folders"); m.add_argument("--changed", required=True); m.add_argument("--deleted", required=True)
    m.add_argument("--no-drift-check", action="store_true")
    m.add_argument("--merges-limit", type=int, default=31,
                   help="merges since the last k-means before a re-cluster is demanded: 31 for the daily cadence (owner 2026-11-15; a monthly scheduled re-cluster keeps merges below it; the drift guard and `recall` are the real gates)")
    r = sub.add_parser("recall", help="recall@k of the IVF search vs exact, after each merge (vs a fresh re-cluster with --fresh)")
    r.add_argument("folder"); r.add_argument("--fresh"); r.add_argument("--queries", type=int, default=200)
    r.add_argument("--k", type=int, default=10); r.add_argument("--probes", type=int, default=200)
    r.add_argument("--max-drop", type=float, default=0.01, help="largest recall loss against --fresh")
    r.add_argument("--min-recall", type=float, default=0.90, help="floor when there is no --fresh folder")
    a = ap.parse_args()
    {"texts": cmd_texts, "reuse": cmd_reuse, "merge": cmd_merge, "recall": cmd_recall}[a.cmd](a)


if __name__ == "__main__":
    main()
