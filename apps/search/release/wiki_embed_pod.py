#!/usr/bin/env python3
"""Embed Wikipedia texts into 1-bit nomic codes on a rented GPU (RunPod, via utility/judge-pod), with a built-in gate.

    wiki_embed_pod.py BASE_URL OUTDIR [--shard 200000] [--max-mean-diff 1.0] [--max-diff 12]

BASE_URL is a public folder (a temporary Hugging Face dataset repo, `.../resolve/main`) holding:
    texts.tsv.gz         `page_id TAB title. lead[:600]` rows (public Wikipedia text; wiki_ivf_delta.py texts)
    sample-texts.tsv.gz  the 2,954 texts of the recipe check measured on buildbox 2026-11-15 (buildbox's own wiki_texts rows)
    sample-codes.json    page_id -> the LIVE rc15 code of that article (96 bytes, hex)
The recipe is buildbox's, exactly (tools/embed-full.py nomic): nomic-ai/nomic-embed-text-v1.5, prefix `search_document: `, max
sequence 256 tokens, fp16, mean pooling, L2-normalised, bit = component >= 0, packed MSB first (768 -> 96 bytes).

GATE: the sample is embedded first and compared with the live codes; mean differing bits over the sample above
--max-mean-diff or any row above --max-diff (buildbox's 4070 measured mean 0.168, max 4, 2,513 of 2,954 identical) writes
GATE-FAILED and stops. Nothing is embedded for the full set on a card that disagrees with the live codes.
OUTDIR gets, per shard, shard-NNNN.ids.npy (int64 page ids) and shard-NNNN.bits.npy (uint8 N x 96), then manifest.json
(rows, sha256 per file) and DONE. The caller downloads, checks the manifest, and TERMINATES the pod.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import sys
import time
import urllib.request

import numpy as np

PREFIX = "search_document: "
MAXLEN = 256
MODEL = "nomic-ai/nomic-embed-text-v1.5"
REVISION = "e9b6763023c676ca8431644204f50c2b100d9aab"       # the snapshot in buildbox's cache (2026-11-15)


def log(msg: str) -> None:
    print(f"[wiki-embed-pod {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def fetch(base: str, name: str, dest: str) -> str:
    p = os.path.join(dest, name)
    if not os.path.exists(p):
        urllib.request.urlretrieve(f"{base}/{name}", p)
    return p


def rows(path: str):
    with gzip.open(path, "rt", encoding="utf-8", errors="replace") as f:
        for line in f:
            i, _, text = line.rstrip("\n").partition("\t")
            if i:
                yield int(i), text.replace("\\n", " ")


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 22):
            h.update(chunk)
    return h.hexdigest()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("base"); ap.add_argument("out")
    ap.add_argument("--shard", type=int, default=200_000)
    ap.add_argument("--max-mean-diff", type=float, default=1.0)
    ap.add_argument("--max-diff", type=int, default=12)
    a = ap.parse_args()
    import torch
    from sentence_transformers import SentenceTransformer
    os.makedirs(a.out, exist_ok=True)
    work = "/root/in"
    os.makedirs(work, exist_ok=True)
    log(f"torch {torch.__version__} on {torch.cuda.get_device_name(0)}")
    model = SentenceTransformer(MODEL, device="cuda", trust_remote_code=True, revision=REVISION)
    model.half()
    model.max_seq_length = MAXLEN

    def codes(texts):
        v = model.encode([PREFIX + t for t in texts], batch_size=128, normalize_embeddings=True, convert_to_numpy=True, show_progress_bar=False)
        return np.packbits(v.astype(np.float16) >= 0, axis=1)

    # ---- gate
    st = fetch(a.base, "sample-texts.tsv.gz", work)
    live = json.load(open(fetch(a.base, "sample-codes.json", work)))
    sid, stx = zip(*rows(st))
    t0 = time.time()
    sb = np.concatenate([codes(stx[i:i + 1024]) for i in range(0, len(stx), 1024)])
    diffs = []
    for i, b in zip(sid, sb):
        if str(i) in live:
            diffs.append(int(np.unpackbits(np.frombuffer(bytes.fromhex(live[str(i)]), np.uint8) ^ b).sum()))
    res = {"sample": len(diffs), "identical": sum(1 for d in diffs if d == 0), "mean_diff": round(float(np.mean(diffs)), 3) if diffs else None,
           "max_diff": max(diffs) if diffs else None, "docs_per_s": round(len(stx) / (time.time() - t0), 1)}
    log("gate: " + json.dumps(res))
    json.dump(res, open(os.path.join(a.out, "gate.json"), "w"))
    if not diffs or res["mean_diff"] > a.max_mean_diff or res["max_diff"] > a.max_diff:
        open(os.path.join(a.out, "GATE-FAILED"), "w").write(json.dumps(res) + "\n")
        log("GATE FAILED: this card/software does not reproduce the live codes; not embedding the full set")
        return
    # ---- full set
    texts = fetch(a.base, "texts.tsv.gz", work)
    manifest = {"model": MODEL, "revision": REVISION, "gate": res, "shards": [], "rows": 0}
    buf_i, buf_t, n_shard, t1 = [], [], 0, time.time()

    def flush():
        nonlocal n_shard
        if not buf_i:
            return
        b = np.concatenate([codes(buf_t[s:s + 20_000]) for s in range(0, len(buf_t), 20_000)])
        ip, bp = os.path.join(a.out, f"shard-{n_shard:04d}.ids.npy"), os.path.join(a.out, f"shard-{n_shard:04d}.bits.npy")
        np.save(ip, np.array(buf_i, np.int64)); np.save(bp, b)
        manifest["shards"].append({"n": len(buf_i), "ids_sha256": sha256(ip), "bits_sha256": sha256(bp), "name": f"shard-{n_shard:04d}"})
        manifest["rows"] += len(buf_i)
        log(f"shard {n_shard}: {len(buf_i):,} rows, {manifest['rows']:,} total, {manifest['rows'] / (time.time() - t1):.0f}/s")
        n_shard += 1
        buf_i.clear(); buf_t.clear()

    for i, t in rows(texts):
        buf_i.append(i); buf_t.append(t)
        if len(buf_i) >= a.shard:
            flush()
    flush()
    manifest["seconds"] = round(time.time() - t1)
    json.dump(manifest, open(os.path.join(a.out, "manifest.json"), "w"), indent=1)
    open(os.path.join(a.out, "DONE"), "w").write(f"rows={manifest['rows']}\n")
    log(f"done: {manifest['rows']:,} rows in {manifest['seconds']} s")


if __name__ == "__main__":
    main()
