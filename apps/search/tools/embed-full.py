#!/usr/bin/env python3
"""Embed every Wikipedia article with one model on a GPU, in resumable shards.

    embed-full.py MODEL TEXTS.tsv.gz OUTDIR [--shard 500000] [--batch 128]

MODEL  nomic | arctic   (see embed-compare.py; 768-d, Apache-2.0)
TEXTS  id<TAB>text rows, the text the live wiki_vec was embedded from (buildbox: wiki_texts.tsv.gz)
OUTDIR MODEL-NNNN.ids.npy (int64) + MODEL-NNNN.vecs.npy (float16, N x 768, L2-normalised), one pair per shard

A shard whose two files already exist is skipped, so a crash, a stopped container or a GPU taken by another
job costs at most one shard. Documents get the model's document prefix and are cut at 256 tokens, as
tools/embed.py did for bge-small. Shard order follows the input file, so shard n always holds the same rows.
"""
import argparse
import gzip
import os
import sys
import time

import numpy as np
import torch
from sentence_transformers import SentenceTransformer

MODELS = {
    "nomic": dict(name="nomic-ai/nomic-embed-text-v1.5", dprefix="search_document: ", trust=True),
    "arctic": dict(name="Snowflake/snowflake-arctic-embed-m-v1.5", dprefix="", trust=False),
}
MAXLEN = 256


def rows(path: str):
    with gzip.open(path, "rt", encoding="utf-8", errors="replace") as f:
        for line in f:
            i, _, text = line.rstrip("\n").partition("\t")
            if i:
                yield int(i), text.replace("\\n", " ")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("model"); ap.add_argument("texts"); ap.add_argument("outdir")
    ap.add_argument("--shard", type=int, default=500_000)
    ap.add_argument("--batch", type=int, default=128)
    a = ap.parse_args()
    m = MODELS[a.model]
    os.makedirs(a.outdir, exist_ok=True)
    model = SentenceTransformer(m["name"], device="cuda", trust_remote_code=m["trust"])
    model.half()
    model.max_seq_length = MAXLEN
    print(f"{a.model} on {torch.cuda.get_device_name(0)}", flush=True)
    buf_i: list[int] = []
    buf_t: list[str] = []
    n_shard, done_rows, t0 = 0, 0, time.time()

    def path(n: int, kind: str) -> str:
        return os.path.join(a.outdir, f"{a.model}-{n:04d}.{kind}.npy")

    def flush() -> None:
        nonlocal n_shard, done_rows
        if not buf_i:
            return
        if os.path.exists(path(n_shard, "vecs")) and os.path.exists(path(n_shard, "ids")):
            print(f"shard {n_shard}: present, skipped", flush=True)
        else:
            t1 = time.time()
            vecs = []
            for s in range(0, len(buf_t), 20_000):
                v = model.encode([m["dprefix"] + t for t in buf_t[s:s + 20_000]], batch_size=a.batch,
                                 normalize_embeddings=True, convert_to_numpy=True, show_progress_bar=False)
                vecs.append(v.astype(np.float16))
            np.save(path(n_shard, "ids") + ".tmp.npy", np.array(buf_i, dtype=np.int64))
            np.save(path(n_shard, "vecs") + ".tmp.npy", np.concatenate(vecs))
            os.replace(path(n_shard, "ids") + ".tmp.npy", path(n_shard, "ids"))
            os.replace(path(n_shard, "vecs") + ".tmp.npy", path(n_shard, "vecs"))   # vecs last: its presence = shard complete
            print(f"shard {n_shard}: {len(buf_i):,} in {(time.time() - t1) / 60:.1f} min ({len(buf_i) / (time.time() - t1):.0f}/s); "
                  f"{done_rows + len(buf_i):,} rows, {(time.time() - t0) / 3600:.2f} h", flush=True)
        done_rows += len(buf_i)
        n_shard += 1
        buf_i.clear()
        buf_t.clear()

    for i, text in rows(a.texts):
        buf_i.append(i)
        buf_t.append(text)
        if len(buf_i) >= a.shard:
            flush()
    flush()
    print(f"done: {done_rows:,} rows in {n_shard} shards, {(time.time() - t0) / 3600:.2f} h", flush=True)


if __name__ == "__main__":
    sys.exit(main())
