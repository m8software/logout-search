#!/usr/bin/env python3
"""The PARTS of a synthetic release, laid out the way the real builders hand them over (for the assemble/install tests).

    search_assemble_parts.py PARTS_DIR RELEASE_DIR RELEASE_ID

RELEASE_DIR is a folder made by search_update_fixture.py (the real registry/lookup builders, a Tantivy wiki index, an IVF folder).
PARTS_DIR gets one name per part, hard-linked from it (nothing is copied but what has to differ): the SQLite files, signals.bin,
typo/ wiki/ ivf/, plus what the fixture does not build -- sites/ pages/ anchors/ (copies of the wiki index with their own roles),
a tiny stand-in for the nomic model (nomic/model.onnx + tokenizer.json: NOT a real model, the tests never run it) and english.stop.
Prints the `assemble.py build` arguments, one per line.
"""
import json
import os
import shutil
import sys

parts, rel, rid = sys.argv[1:4]
os.makedirs(parts, exist_ok=True)


def link(src, dst):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    try:
        os.link(src, dst)
    except OSError:
        shutil.copyfile(src, dst)


def link_tree(src, dst):
    for d, _, fs in os.walk(src):
        for f in fs:
            if not f.endswith(".lock"):
                link(os.path.join(d, f), os.path.join(dst, os.path.relpath(os.path.join(d, f), src)))


args = []
for k, f in (("registry", "registry.sqlite"), ("signals", "signals.bin"), ("lookup", "lookup.sqlite"), ("places-osm", "places-osm.sqlite"),
             ("places-overture", "places-overture.sqlite"), ("hours-web", "hours-web.sqlite"), ("pictures", "pictures.sqlite")):
    link(os.path.join(rel, f), os.path.join(parts, f))
    args += [f"--{k}", os.path.join(parts, f)]
for k in ("typo", "wiki"):
    link_tree(os.path.join(rel, "text", k), os.path.join(parts, k))
    for lock in (".tantivy-meta.lock", ".tantivy-writer.lock"):          # what a Tantivy open leaves behind: never part of a release
        open(os.path.join(parts, k, lock), "w").close()
    args += [f"--{k}", os.path.join(parts, k)]
link_tree(os.path.join(rel, "vectors", "ivf"), os.path.join(parts, "ivf"))
args += ["--ivf", os.path.join(parts, "ivf")]
for k in ("sites", "pages", "anchors"):          # same bytes as the wiki index, a sidecar of their own (the real ones: tantivy-structure)
    d = os.path.join(parts, k)
    shutil.rmtree(d, ignore_errors=True)
    link_tree(os.path.join(rel, "text", "wiki"), d)
    side = json.load(open(os.path.join(d, "release.json")))
    side["role"] = k
    os.unlink(os.path.join(d, "release.json"))
    open(os.path.join(d, "release.json"), "w").write(json.dumps(side, sort_keys=True) + "\n")
    args += [f"--{k}", d]
nm = os.path.join(parts, "nomic")
os.makedirs(nm, exist_ok=True)
import random  # noqa: E402
open(os.path.join(nm, "model.onnx"), "wb").write(random.Random(7).randbytes(4096))          # seeded (same bytes every run), incompressible: stored as is by --publish
open(os.path.join(nm, "tokenizer.json"), "w").write(json.dumps({"stand-in": "not the real tokenizer"}) + "\n" * 50)
args += ["--nomic-dir", nm]
rr = os.path.join(parts, "rerank")             # the real file names of rerank-turbo (model_int8.onnx + tokenizer.json), tiny stand-in bytes
os.makedirs(rr, exist_ok=True)
open(os.path.join(rr, "model_int8.onnx"), "wb").write(random.Random(11).randbytes(2048))
open(os.path.join(rr, "tokenizer.json"), "w").write(json.dumps({"stand-in": "not the real reranker tokenizer"}) + "\n" * 50)
args += ["--rerank-dir", rr]
km = os.path.join(parts, "kindsmodel")         # the file names of bge-small-en-v1.5; NOT passed by default (a release has it only with --kinds)
os.makedirs(km, exist_ok=True)
open(os.path.join(km, "model.onnx"), "wb").write(b"stand-in: not the real kinds model\n" * 40)
open(os.path.join(km, "tokenizer.json"), "w").write(json.dumps({"stand-in": "not the real bge tokenizer"}) + "\n" * 50)
rk = os.path.join(parts, "ranker.json")        # the shape of serve/api/ranker.json, stand-in weights
open(rk, "w").write(json.dumps({"features": ["a", "b"], "weights": [0.5, -0.5, 0.1], "trained_on": 7, "heldout_top1": {}}) + "\n")
args += ["--ranker", rk]
open(os.path.join(parts, "english.stop"), "w").write("the\nand\nof\n")
args += ["--stopwords", os.path.join(parts, "english.stop")]
# the real sidecars carry the sha256 of the stop list the index was built with (release_check compares it with english.stop);
# the typo index is built with no stop list (raw tokenizer) and its real sidecar has no such stamp: keep it that way here
import hashlib  # noqa: E402
stop_sha = hashlib.sha256(open(os.path.join(parts, "english.stop"), "rb").read()).hexdigest()
for k in ("wiki", "sites", "pages", "anchors"):
    p = os.path.join(parts, k, "release.json")
    side = json.load(open(p))
    side["stopwords_sha256"] = stop_sha
    os.unlink(p)                                    # a new inode: the fixture's own sidecar is a hard link and stays as it was
    open(p, "w").write(json.dumps(side, sort_keys=True) + "\n")
print("\n".join(args))
