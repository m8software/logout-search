#!/usr/bin/env python3
"""Regression check on the open answer-known sets: ask one search-api, score hit@1/@5.

    tools/kinds-open-sets.py --api URL --arm NAME --out DIR SET.tsv [SET.tsv ...]
    tools/kinds-open-sets.py --report DIR --arms off,old,new2

SET.tsv: tools/evalsets/open format (query, category, expected, set, stratum, split, entity).
Per-arm answers go to DIR/open-<arm>.json; --report prints one table per set plus paired
win/loss counts against the first arm.
"""
import argparse
import importlib.util
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
_s = importlib.util.spec_from_file_location("eq", HERE / "eval-queries.py")
eq = importlib.util.module_from_spec(_s)
_s.loader.exec_module(eq)


def rows(p):
    out = []
    for i, line in enumerate(Path(p).read_text().splitlines()):
        f = line.split("\t")
        if i and len(f) >= 4:
            out.append((f[0], f[1], f[2].split("|"), f[3]))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--api")
    ap.add_argument("--arm")
    ap.add_argument("--extra", default="")
    ap.add_argument("--out")
    ap.add_argument("--report")
    ap.add_argument("--arms", default="off,old,new2")
    ap.add_argument("sets", nargs="*")
    a = ap.parse_args()
    if a.report:
        d = Path(a.report)
        arms = a.arms.split(",")
        R = {x: json.loads((d / f"open-{x}.json").read_text()) for x in arms}
        sets = sorted({r[3] for v in R.values() for r in v["rows"]})
        print("| set | n | " + " | ".join(f"{x} #1 / top-5" for x in arms) + " |\n|---|---|" + "---|" * len(arms))
        for s in sets:
            cells, n = [], 0
            for x in arms:
                rr = [r for r in R[x]["rows"] if r[3] == s]
                n = len(rr)
                cells.append(f"{sum(r[4] for r in rr)} / {sum(r[5] for r in rr)}")
            print(f"| {s} | {n} | " + " | ".join(cells) + " |")
        b = {(r[0], r[3]): r for r in R[arms[0]]["rows"]}
        for x in arms[1:]:
            w = sum(r[4] and not b[(r[0], r[3])][4] for r in R[x]["rows"])
            l = sum(b[(r[0], r[3])][4] and not r[4] for r in R[x]["rows"])
            print(f"\n{x} vs {arms[0]} at #1: {w} won, {l} lost; errors {R[x]['errors']}")
        return
    out, errs = [], 0
    for p in a.sets:
        for q, cat, exp, s in rows(p):
            try:
                d = json.load(urllib.request.urlopen(a.api + "/search?k=10" + a.extra + "&q=" + urllib.parse.quote(q), timeout=30))
                rs = d.get("results", [])
            except Exception:
                errs += 1
                rs = []
            out.append([q, cat, exp, s, int(bool(rs) and eq.hit(cat, exp, rs[0])),
                        int(any(eq.hit(cat, exp, r) for r in rs[:5]))])
    Path(a.out, f"open-{a.arm}.json").write_text(json.dumps({"rows": out, "errors": errs}))
    print(f"{a.arm}: {len(out)} queries, {errs} errors", file=sys.stderr)


if __name__ == "__main__":
    main()
