#!/usr/bin/env python3
"""One installer version's intake of a candidate answers publish (run by compat_check.py, once per version).

    python3 -I compat_worker.py --src TREE --candidate DIR --work DIR --pubkey KEY --release-id ID [--sample N]

TREE is the source of that version (the repository files at its release tag). With ITS OWN code this does what an install of
that version does with the answers file:

  1. `install.py lists update` against the candidate folder (its manifest check, signature check, download of every part,
     decoding, re-keying to the install's release, drop in the store inbox);
  2. the engine's inbox intake: the version's own `store_inbox_once` function, taken out of its search_api.py by syntax tree
     (the engine itself needs a release and a gigabyte of libraries; this function needs only result_store.py) and run on a
     store of its own, which reads every part the way the engine does;
  3. a sample of the candidate's finished answers and hand-picked lists is read back from the store and compared with what the
     publish holds.

Prints one JSON object: {"ok", "version_tree", "lists", "intake": {"files", "refused", "bad", "numbers"}, "served": {"sampled",
"matched", "missing", "different"}, "problems": [...]}. Standard library only. Exit 0 when ok.
"""
from __future__ import annotations

import argparse
import ast
import json
import lzma
import os
import random
import re
import sqlite3
import subprocess
import sys
import tempfile
import zlib


def candidate_rows(candidate: str, sample: int, seed: int = 1) -> list:
    """[(table, key, expected)] sampled from the candidate's parts (the newest rows win: a delta replaces a base row)."""
    mpath = os.path.join(candidate, "lists", "manifest.json")
    m = json.load(open(mpath, encoding="utf-8"))
    rows = {}
    with tempfile.TemporaryDirectory(prefix="compat-parts-") as tmp:
        for part in m["parts"]:
            xz = os.path.join(candidate, "lists", "parts", part["name"] + ".sqlite.xz")
            if not os.path.isfile(xz):
                continue                                   # a part already published (upload removes local copies): not here to sample
            raw = os.path.join(tmp, part["name"] + ".sqlite")
            with lzma.open(xz, "rb") as fi, open(raw, "wb") as fo:
                for b in iter(lambda: fi.read(1 << 20), b""):
                    fo.write(b)
            db = sqlite3.connect(f"file:{raw}?mode=ro", uri=True)
            try:
                meta = dict(db.execute("SELECT k, v FROM lists_meta").fetchall())
                for t in ("curated", "finished"):
                    for key, body, at in db.execute(f"SELECT key, body, at FROM {t}"):
                        text = body if meta.get("body") == "plain" else zlib.decompress(body)
                        rows[(t, key)] = (json.loads(text), at)
            finally:
                db.close()
            os.remove(raw)
    keys = sorted(rows)
    random.Random(seed).shuffle(keys)
    return [(t, k, rows[(t, k)][0]) for t, k in keys[:sample]], len(rows)


def intake_function(tree: str):
    """The version's own store_inbox_once, from its search_api.py (None when that version has no such function), and FIN_V."""
    path = os.path.join(tree, "apps", "search", "serve", "api", "search_api.py")
    src = open(path, encoding="utf-8").read()
    mod = ast.parse(src)
    fn = next((n for n in mod.body if isinstance(n, ast.FunctionDef) and n.name == "store_inbox_once"), None)
    fin = re.search(r"^FIN_V\s*=\s*(\d+)", src, re.M)
    return (ast.Module(body=[fn], type_ignores=[]) if fn else None), int(fin.group(1)) if fin else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--src", required=True)
    ap.add_argument("--candidate", required=True)
    ap.add_argument("--work", required=True)
    ap.add_argument("--pubkey", required=True)
    ap.add_argument("--release-id", required=True)
    ap.add_argument("--sample", type=int, default=50)
    a = ap.parse_args()
    tree = os.path.abspath(a.src)
    out = {"ok": False, "tree": os.path.basename(tree.rstrip("/")), "problems": []}
    problems = out["problems"]
    api, inst_dir, rel_dir = (os.path.join(tree, "apps", "search", p) for p in ("serve/api", "installer", "release"))
    for d in (api, inst_dir):
        if not os.path.isdir(d):
            problems.append(f"{d} is not in this version's source")
            print(json.dumps(out))
            return 1
    # 1. the version's own `lists update`
    base = os.path.join(a.work, "base")
    rid = a.release_id
    os.makedirs(os.path.join(base, "releases", rid), exist_ok=True)
    json.dump({"release": rid}, open(os.path.join(base, "releases", rid, "manifest.json"), "w"))
    sys.path[:0] = [inst_dir, rel_dir]
    import release_ptr
    release_ptr.set_pointer(base, "current", rid)
    # lists_build.upload removes the parts it sent, so the folder of a nightly delta holds the manifest (naming the base) and
    # only the new part. The parts that are not here were checked when they were published: they count as already taken, and
    # the install takes the ones that are here, as an install that is one day behind does
    m = json.load(open(os.path.join(a.candidate, "lists", "manifest.json"), encoding="utf-8"))
    absent = [p["name"] for p in m["parts"] if not os.path.isfile(os.path.join(a.candidate, "lists", "parts", p["name"] + ".sqlite.xz"))]
    out["parts_not_here"] = absent
    if absent:
        os.makedirs(os.path.join(base, "store"), exist_ok=True)
        json.dump({"base": m["base"], "release": rid, "applied": absent, "n": 0}, open(os.path.join(base, "store", "lists.json"), "w"))
    r = subprocess.run([sys.executable, "-I", os.path.join(inst_dir, "install.py"), "lists", "update", "--dest", base, "--source",
                        a.candidate, "--pubkey", a.pubkey], capture_output=True, text=True, cwd=a.work, timeout=1800)
    lists = {}
    try:
        lists = json.loads(r.stdout.strip().splitlines()[-1]) if r.stdout.strip() else {}
    except ValueError:
        pass
    out["lists"] = {k: lists.get(k) for k in ("lists", "version", "why", "parts_fetched", "rows") if k in lists}
    if r.returncode or lists.get("lists") not in ("fetched", "up to date"):
        problems.append(f"`lists update` of this version: exit {r.returncode}, {lists.get('lists')!r} "
                        f"{lists.get('why') or (r.stderr or '').strip()[-300:]}")
        print(json.dumps(out))
        return 1
    # 2. the engine's own intake
    tree_fn, fin_v = intake_function(tree)
    if tree_fn is None:
        problems.append("search_api.py of this version has no store_inbox_once: the intake cannot be run")
        print(json.dumps(out))
        return 1
    sys.path.insert(0, api)
    import result_store
    store_path = os.path.join(base, "store", "results.sqlite")
    st = result_store.ResultStore(store_path, "fp-compat")
    log = []
    ns = {"os": os, "result_store": lambda: st, "STORE_PATH": store_path, "CURATED_RELEASE": rid, "FIN_V": fin_v,
          "print": lambda *x, **k: log.append(" ".join(str(i) for i in x)), "list": list, "sorted": sorted, "dict": dict, "any": any,
          "Exception": Exception, "OSError": OSError}
    exec(compile(tree_fn, "store_inbox_once", "exec"), ns)  # noqa: S102 - this version's own function, from its own source
    inbox = os.path.join(base, "store", "inbox")
    files = len([f for f in os.listdir(inbox) if f.endswith(".sqlite")])
    done = ns["store_inbox_once"]()
    bad = [f for f in os.listdir(inbox) if f.endswith(".bad")]
    refused = sum(v for n in done for k, v in n.items() if k.endswith("refused") and isinstance(v, int)) + len(bad)
    refused += len([ln for ln in log if "refused (" in ln])
    out["intake"] = {"files": files, "taken_in": len(done), "refused": refused, "bad": len(bad), "numbers": done[:3]}
    if refused or len(done) != files:
        problems.append(f"intake: {files} part(s), {len(done)} taken in, {refused} refused " + "; ".join(log)[:300])
    # 3. served back
    sample, total = candidate_rows(a.candidate, a.sample)
    got = {"sampled": len(sample), "of": total, "matched": 0, "missing": 0, "different": 0}
    for table, key, expected in sample:
        value = st.get_finished(rid, key, 1e12) if table == "finished" else st.get_curated(rid, key)
        if value is None:
            got["missing"] += 1
        elif value != expected:
            got["different"] += 1
        else:
            got["matched"] += 1
    out["served"] = got
    if not sample:
        problems.append("the candidate holds no answers to sample")
    if got["missing"] or got["different"]:
        problems.append(f"served: {got['missing']} missing and {got['different']} different of {got['sampled']} sampled")
    out["ok"] = not problems
    print(json.dumps(out))
    return 0 if out["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
