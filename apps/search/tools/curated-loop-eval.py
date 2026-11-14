#!/usr/bin/env python3
"""curated-loop-eval.py BATCH_DIR SIGNAL.jsonl [--fit] [--seed 7]: does the engine's uncertainty predict where the
big models change the list? (nightly loop item A, 2026-11-09)

BATCH_DIR holds a finished curated batch as curated-nightly.sh leaves it: batch.json, cand.jsonl, cur.json, q.jsonl
(Qwen3-Reranker-4B per pair) and lw-out.jsonl (Qwen3-14B list). SIGNAL.jsonl is curated_signal.py run's output for a
sample of the same names. The outcome per name, from the batch alone:
  first_changed  the final list's first row is not the page's first row today (cur.json)
  cur_bad        1 - the 4B score of today's first row (the large model's doubt about what the page shows)
  moved          share of the final list's rows not among the pool's first len(rows) in the engine's merged order
Prints, on the test half (names split by a seeded shuffle; --fit first fits the logistic weights on the train half
and prints them for curated_signal.W): AUC for first_changed, Spearman with cur_bad and moved, and the share of all
first-row changes found in the top 20% / 40% by uncertainty, against popularity order (queue position) and chance.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import math
import random
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def load_mod(path: Path, name: str):
    s = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(s)
    sys.modules[name] = m
    s.loader.exec_module(m)
    return m


def auc(pos: list[float], neg: list[float]) -> float:
    allv = sorted([(v, 1) for v in pos] + [(v, 0) for v in neg])
    rank, i, rs = 0.0, 0, 0.0
    while i < len(allv):
        j = i
        while j < len(allv) and allv[j][0] == allv[i][0]:
            j += 1
        for k in range(i, j):
            if allv[k][1]:
                rs += (i + j + 1) / 2
        i = j
    n1, n0 = len(pos), len(neg)
    return (rs - n1 * (n1 + 1) / 2) / (n1 * n0) if n1 and n0 else float("nan")


def spearman(a: list[float], b: list[float]) -> float:
    def rk(x):
        o = sorted(range(len(x)), key=lambda i: x[i])
        r = [0.0] * len(x)
        i = 0
        while i < len(o):
            j = i
            while j < len(o) and x[o[j]] == x[o[i]]:
                j += 1
            for k in range(i, j):
                r[o[k]] = (i + j - 1) / 2
            i = j
        return r
    ra, rb = rk(a), rk(b)
    ma, mb = sum(ra) / len(ra), sum(rb) / len(rb)
    cov = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
    va, vb = sum((x - ma) ** 2 for x in ra), sum((y - mb) ** 2 for y in rb)
    return cov / math.sqrt(va * vb) if va and vb else float("nan")


def outcomes(bdir: Path) -> dict[str, dict]:
    B = load_mod(HERE / "curated-build.py", "curated_build")
    Q = B.load(str(bdir / "q.jsonl"))
    cur = {x["i"]: x.get("cur") for x in json.load(open(bdir / "cur.json"))}
    W = {o["i"]: o["rows"] for o in map(json.loads, open(bdir / "lw-out.jsonl"))}

    class A:                                            # curated-build's defaults
        tw, max, per_site, per_wiki = 0.3, 8, 2, 4
    out = {}
    for line in open(bdir / "cand.jsonl"):
        x = json.loads(line)
        if x["i"] not in W or not x.get("cand"):
            continue
        rows = B.build_whole(x, Q, W[x["i"]], A)
        if not rows:
            continue
        c = cur.get(x["i"]) or []
        c1 = B.norm(c[0]["u"]) if c else None
        by = {B.norm(r["u"]): k for k, r in enumerate(x["cand"])}
        qs = {B.norm(r["u"]): Q.get(f"{x['i']}-{k}") for k, r in enumerate(x["cand"])}
        n = len(rows)
        top = {B.norm(r["u"]) for r in x["cand"][:n]}
        out[" ".join(x["q"].lower().split())] = {
            "i": x["i"], "first_changed": float(B.norm(rows[0]["url"]) != c1),
            "cur_bad": 1.0 - (qs.get(c1) if qs.get(c1) is not None else 0.0) if c1 else 1.0,
            "moved": sum(1 for r in rows if B.norm(r["url"]) not in top) / n}
    return out


def fit_logistic(X: list[list[float]], y: list[float], steps: int = 4000, rate: float = 0.05) -> list[float]:
    """Plain batch gradient descent on standardised-free features (they are already on small scales)."""
    w = [0.0] * len(X[0])
    for _ in range(steps):
        g = [0.0] * len(w)
        for xi, yi in zip(X, y):
            p = 1 / (1 + math.exp(-sum(a * b for a, b in zip(w, xi))))
            for j, v in enumerate(xi):
                g[j] += (p - yi) * v
        w = [a - rate * gj / len(X) for a, gj in zip(w, g)]
    return w


def feats(f: dict) -> list[float]:
    ces = f["ces"]
    top = ces[0] if ces else -3.0
    margin = (ces[0] - ces[1]) if len(ces) > 1 else 0.0
    return [1.0, max(-6.0, min(6.0, top)), min(margin, 6.0), min(f["named"], 9), min(f["pool"], 60)]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("batch"); ap.add_argument("signal")
    ap.add_argument("--fit", action="store_true"); ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()
    S = load_mod(HERE.parent / "twin" / "curated_signal.py", "curated_signal")
    O = outcomes(Path(a.batch))
    sig = {" ".join(o["q"].lower().split()): o for o in map(json.loads, open(a.signal))}
    names = sorted(k for k in sig if k in O)
    random.Random(a.seed).shuffle(names)
    half = len(names) // 2
    train, test = names[:half], names[half:]
    print(f"{len(O)} finished names, {len(sig)} with a signal, {len(names)} in both; train {len(train)} / test {len(test)}")
    if a.fit:
        w = fit_logistic([feats(sig[k]) for k in train], [O[k]["first_changed"] for k in train])
        print("fitted W =", dict(zip(("b", "top", "margin", "named", "pool"), (round(v, 3) for v in w))))
        S.W.update(dict(zip(("b", "top", "margin", "named", "pool"), w)))
    unc = {k: S.uncertainty(sig[k]) for k in names}
    pos = {k: float(i) for i, k in enumerate(sorted(O, key=lambda k: O[k]["i"]))}      # queue position (popularity order)
    for label, part in (("train", train), ("TEST", test)):
        y = [O[k]["first_changed"] for k in part]
        p = [unc[k] for k in part]
        print(f"[{label}] first row changed in {sum(y) / len(y):.1%} of {len(y)} names")
        print(f"  AUC uncertainty -> first_changed {auc([u for u, v in zip(p, y) if v], [u for u, v in zip(p, y) if not v]):.3f}"
              f"   (queue position alone: {auc([pos[k] for k, v in zip(part, y) if v], [pos[k] for k, v in zip(part, y) if not v]):.3f})")
        print(f"  Spearman with cur_bad {spearman(p, [O[k]['cur_bad'] for k in part]):+.3f}, with moved {spearman(p, [O[k]['moved'] for k in part]):+.3f}")
        order = sorted(part, key=lambda k: -unc[k])
        tot = sum(y)
        for frac in (0.1, 0.2, 0.4):
            top = order[:int(len(order) * frac)]
            got = sum(O[k]["first_changed"] for k in top)
            mv = sum(O[k]["moved"] for k in top) / len(top)
            allmv = sum(O[k]["moved"] for k in part) / len(part)
            print(f"  top {frac:.0%} by uncertainty: {got / tot:.1%} of the changed first rows (chance {frac:.0%}), "
                  f"first row changed in {got / len(top):.1%} of them, moved {mv:.2f} vs {allmv:.2f} overall")
    return 0


if __name__ == "__main__":
    sys.exit(main())
