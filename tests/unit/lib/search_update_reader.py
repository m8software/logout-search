#!/usr/bin/env python3
"""Open a release folder with the engine's own readers and print what they answer (deterministic), for the update-package tests.

    search_update_reader.py RELEASE_DIR

lookups_release.ReleaseLookups (through search_api, as test-search-release-lookups.sh does) and sem_ivf.IvfIndex. Anything the engine
refuses raises, so a non-zero exit means "this folder is not a release the engine opens".
"""
import importlib.util
import json
import os
import sqlite3
import sys
import types

import numpy as np

root = os.path.abspath(sys.argv[1])
repo = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
api = os.path.join(repo, "apps", "search", "serve", "api")
os.environ["SEARCH_LOOKUP_BACKEND"] = "release"
os.environ["SEARCH_RELEASE_DIR"] = root
psycopg = types.ModuleType("psycopg")
psycopg.Error = Exception
sys.modules["psycopg"] = psycopg
sys.path.insert(0, api)
spec = importlib.util.spec_from_file_location("search_api", api + "/search_api.py")
m = importlib.util.module_from_spec(spec)
sys.modules["search_api"] = m
spec.loader.exec_module(m)
lk = m.lookups()
print("opened", lk.name, lk.release_id)
reg = sqlite3.connect(f"file:{root}/registry.sqlite?mode=ro", uri=True)
X = lambda sql, p: list(lk.execute(sql, p))  # noqa: E731
# a few wiki titles from the registry: the exact-name path of the engine, and the places by kind
titles = [r[0] for r in reg.execute("SELECT title FROM registry WHERE doc_id >> 28 = 1 ORDER BY doc_id LIMIT 3")]
titles += [r[0] for r in reg.execute("SELECT title FROM registry WHERE doc_id >> 28 = 1 ORDER BY doc_id DESC LIMIT 3")]
for t in titles:
    nq = "".join(c for c in t.lower() if c.isalnum())
    lk.prefetch(m.exact_names(nq, m.nav_keys(nq), None))
    print("wiki", t, [(r[0], r[2]) for r in X(m.WIKI_EXACT_SITE_SQL, {"nq": nq})][:2])
p = {"lat": 40.683, "lon": -105.716, "dlat": 3.0, "dlon": 3.0, "r": 400}
print("shops", sorted(r[1] for r in X(m.PLACES_SQL.format(match=m.PLACE_MATCH_KINDS), dict(p, kinds=["shop=wholesale"], hint="", hint_kinds=[])))[:8])
t0 = "".join(c for c in titles[0].lower() if c.isalnum())
print("typo", [(r[0], r[3]) for r in X(m.TYPO_SQL, {"t": t0[:-1] + ("x" if t0[-1] != "x" else "y")})][:1])
db = sqlite3.connect(f"file:{root}/hours-web.sqlite?mode=ro", uri=True)
print("hours_web rows", db.execute("SELECT count(*), sum(doc_id) FROM hours_web").fetchone())
sys.path.insert(0, api)
import sem_ivf  # noqa: E402
ix = sem_ivf.IvfIndex(f"{root}/vectors/ivf", lk.release_id)
ids = np.array(ix.ids)
rng = np.random.default_rng(5)
q = np.packbits(rng.integers(0, 2, 768, dtype=np.uint8))
codes = np.array(ix.codes).view(np.uint8).reshape(-1, 96)
d = np.bitwise_count(codes ^ q).sum(1)
best = sorted(zip(d.tolist(), range(len(d))))[:5]
got = ix.search(q, 5, probes=len(ix.cent))                    # every list = exact
print("ivf rows", len(ix), "exact-search ok", [i for i, _ in got] == [int(ids[r]) for _, r in best] or sorted(x for _, x in got) == sorted(x for x, _ in best))
print("ivf sorted ids", int(ids.sum()))
