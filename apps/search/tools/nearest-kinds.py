#!/usr/bin/env python3
"""Nearest kind names for each homepage — candidate kind names for a later "is this a <kind>?" check.

    <domain TAB text lines> | docker run --rm -i -v KV:/kv.tsv:ro -v THIS:/t.py:ro \
        logout/search-api:<tag> python /t.py /kv.tsv [n] [min_sim]  > domain TAB key TAB sim

text = the homepage's title and description. Embeds with the image's bge-small (no query
instruction, like tools/embed-kinds.py) and compares with the kind-name vectors
(kind-vecs.tsv from embed-kinds.py). Runs in a search-api image: it has the model and numpy.
"""
import sys

import numpy as np

sys.path.insert(0, "/app")
import search_api  # noqa: E402

keys, vecs = [], []
for line in open(sys.argv[1], encoding="utf-8"):
    k, _, v = line.rstrip("\n").partition("\t")
    keys.append(k)
    vecs.append([float(x) for x in v.split(",")])
m = np.asarray(vecs, dtype=np.float32)
n = int(sys.argv[2]) if len(sys.argv) > 2 else 3
floor = float(sys.argv[3]) if len(sys.argv) > 3 else 0.55
emb = search_api._Embedder(search_api.MODEL_DIR)
for line in sys.stdin:
    dom, _, text = line.rstrip("\n").partition("\t")
    if not dom or not text:
        continue
    sims = m @ np.asarray(emb(text[:400], prefix=""), dtype=np.float32)
    for i in np.argsort(-sims)[:n]:
        if sims[i] >= floor:
            print(f"{dom}\t{keys[i]}\t{sims[i]:.3f}", flush=False)
