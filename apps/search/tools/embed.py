#!/usr/bin/env python3
"""Embed id<TAB>text rows on a GPU box (buildbox's RTX 4070) for the semantic tier.

    embed.py IN.tsv.gz OUT_PREFIX [--batch 512]
      -> OUT_PREFIX.ids.npy (int64) + OUT_PREFIX.vecs.npy (float16, N x 384, L2-normalised)

Model: BAAI/bge-small-en-v1.5 — small enough for the query side to run on CPU inside
search-api (same model, same normalisation), and English-only like the index.
Documents are embedded WITHOUT bge's query instruction; queries get it (search-api).
"""
import argparse
import gzip
import sys
import time

import numpy as np
import torch
from sentence_transformers import SentenceTransformer

MODEL = "BAAI/bge-small-en-v1.5"


def rows(path: str):
    with gzip.open(path, "rt", encoding="utf-8", errors="replace") as f:
        for line in f:
            i, _, text = line.rstrip("\n").partition("\t")
            if i:
                yield int(i), text.replace("\\n", " ")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("inp")
    ap.add_argument("out")
    ap.add_argument("--batch", type=int, default=512)
    a = ap.parse_args()
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    model = SentenceTransformer(MODEL, device=dev)
    if dev == "cuda":
        model.half()
    model.max_seq_length = 256
    ids, vecs, buf_i, buf_t = [], [], [], []
    t0 = time.time()

    def flush():
        v = model.encode(buf_t, batch_size=a.batch, normalize_embeddings=True, convert_to_numpy=True,
                         show_progress_bar=False)
        vecs.append(v.astype(np.float16))
        ids.extend(buf_i)
        buf_i.clear()
        buf_t.clear()

    for i, text in rows(a.inp):
        buf_i.append(i)
        buf_t.append(text)
        if len(buf_t) >= 20_000:
            flush()
            if len(ids) % 500_000 < 20_000:
                print(f"{len(ids):,} embedded, {len(ids) / (time.time() - t0):.0f}/s", flush=True)
    if buf_t:
        flush()
    np.save(a.out + ".ids.npy", np.array(ids, dtype=np.int64))
    np.save(a.out + ".vecs.npy", np.concatenate(vecs) if vecs else np.zeros((0, 384), np.float16))
    print(f"done: {len(ids):,} in {(time.time() - t0) / 60:.1f} min on {dev}", flush=True)


if __name__ == "__main__":
    sys.exit(main())
