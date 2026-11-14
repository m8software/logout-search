#!/usr/bin/env python3
"""curated-prefilter-eval.py BATCH_DIR Q06.jsonl [--target 0.99]: how many pairs can Qwen3-Reranker-0.6B drop before the
4B model scores them? (nightly loop item D, 2026-11-09)

BATCH_DIR is a finished curated batch (cand.jsonl, q.jsonl = the 4B's P(yes) per pair, lw-out.jsonl = Qwen3-14B's list
per name). Q06.jsonl is the 0.6B's score for the pairs of some of its names (curated-score.py with the 0.6B model).
Only names present in Q06 are used. A pair SURVIVES the prefilter when its 0.6B score >= T or it is among the best K by
0.6B score for its name (so a name never loses all of its candidates). Reported per (T, K): the share of pairs dropped,
and the recall of (a) the rows of the FINAL list (what ships: curated-build.build_whole on the real 4B scores) and
(b) the 4B's own top-11 (what the list model is shown). The 4B+14B pipeline is the truth here, not a human: a row
the 0.6B drops cannot reach the list.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def load_mod(path: Path, name: str):
    s = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(s)
    sys.modules[name] = m
    s.loader.exec_module(m)
    return m


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("batch"); ap.add_argument("q06"); ap.add_argument("--target", type=float, default=0.99)
    a = ap.parse_args()
    B = load_mod(HERE / "curated-build.py", "curated_build")
    bd = Path(a.batch)
    Q4 = B.load(str(bd / "q.jsonl"))
    Q6 = B.load(a.q06)
    W = {o["i"]: o["rows"] for o in map(json.loads, open(bd / "lw-out.jsonl"))}

    class A:
        tw, max, per_site, per_wiki = 0.3, 8, 2, 4
    names = []                                   # per name: [(s06, s4, norm url)], final urls, 4B top-11 urls
    for line in open(bd / "cand.jsonl"):
        x = json.loads(line)
        if x["i"] not in W or not x.get("cand"):
            continue
        ps = [(Q6.get(f"{x['i']}-{k}"), Q4.get(f"{x['i']}-{k}"), B.norm(r["u"])) for k, r in enumerate(x["cand"])]
        if any(p[0] is None or p[1] is None for p in ps):
            continue
        fin = {B.norm(r["url"]) for r in B.build_whole(x, Q4, W[x["i"]], A)}
        top4 = {u for _, _, u in sorted(ps, key=lambda p: -p[1])[:11]}
        names.append((ps, fin, top4))
    npairs = sum(len(n[0]) for n in names)
    nfin = sum(len(n[1]) for n in names)
    ntop = sum(len(n[2]) for n in names)
    print(f"{len(names)} names, {npairs} pairs ({npairs / len(names):.1f} per name), {nfin} final rows, {ntop} 4B top-11 rows")
    print(f"{'T':>6} {'K':>3} {'dropped':>8} {'final recall':>13} {'top-11 recall':>14} {'names losing a final row':>25}")
    best = None
    for K in (0, 3, 5, 8):
        for T in (0.002, 0.005, 0.01, 0.02, 0.05, 0.1, 0.2):
            kept = fin_hit = top_hit = lost = 0
            for ps, fin, top4 in names:
                order = sorted(range(len(ps)), key=lambda j: -ps[j][0])
                keep = {j for j in order[:K]} | {j for j, p in enumerate(ps) if p[0] >= T}
                ku = {ps[j][2] for j in keep}
                kept += len(keep)
                f = len(fin & ku)
                fin_hit += f
                top_hit += len(top4 & ku)
                lost += f < len(fin)
            drop, rf, rt = 1 - kept / npairs, fin_hit / nfin, top_hit / ntop
            print(f"{T:>6} {K:>3} {drop:>8.1%} {rf:>13.2%} {rt:>14.2%} {lost / len(names):>25.1%}")
            if rf >= a.target and (best is None or drop > best[0]):
                best = (drop, T, K, rf, rt)
    if best:
        print(f"best at final recall >= {a.target:.0%}: drop {best[0]:.1%} of pairs with T={best[1]}, K={best[2]} "
              f"(final recall {best[3]:.2%}, top-11 recall {best[4]:.2%})")
    else:
        print(f"no (T, K) reaches final recall {a.target:.0%}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
