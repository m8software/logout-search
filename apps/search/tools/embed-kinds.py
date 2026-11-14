#!/usr/bin/env python3
"""Embed kind names for search-api's meaning match ("photo editor" ~ "image editor").

    <kind names, one per line> | docker exec -i search-api python - < embed-kinds.py   (see below)

Reads one kind name per line on stdin, writes `key <TAB> v1,v2,…` (bge-small-en-v1.5, CLS,
unit length, NO query instruction — both sides are short phrases) on stdout. Runs inside a
search-api container, which has the model and search_api._Embedder; the importer loads the
output with `search_import.py kinds-vecs --file F`.
"""
import sys

sys.path.insert(0, "/app")
import search_api  # noqa: E402

emb = search_api._Embedder(search_api.MODEL_DIR)
for line in sys.stdin:
    key = line.rstrip("\n")
    if key:
        v = emb(key, prefix="")
        print(key + "\t" + ",".join(f"{x:.5f}" for x in v))
