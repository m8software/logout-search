#!/usr/bin/env python3
"""Per-set A/B table from two eval-queries.py --details files per set (the own site rank; a copy of
engine-ab/report.py with the two arms named on the command line).

    report.py DIR ARM_A ARM_B SET[=LABEL] ...      reads DIR/d-<SET>-<ARM_A>.tsv and DIR/d-<SET>-<ARM_B>.tsv

Per set: n, hit@1 / hit@5 for A and B, B-A with the 95% paired-bootstrap interval and the ADR-064
verdict (compare-evals.py's own boot() and passes()), and the number of queries whose rank or top
URL changed. Rows are paired by (query, set position) so a query repeated inside a set is not collapsed.
"""
import importlib.util
import os
import sys

here = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("ce", os.path.join(here, "..", "compare-evals.py"))
ce = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ce)

SCORED = ("site", "wiki", "click", "account", "place")


def load(path):
    rows = []
    with open(path, encoding="utf-8") as f:
        next(f)
        for line in f:
            p = line.rstrip("\n").split("\t")
            if p[1] not in SCORED:
                continue
            r = int(p[2]) if p[2] not in ("-", "") else 99
            rows.append((p[0], r, p[4]))
    return rows


def main():
    d, arm_a, arm_b, sets = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4:]
    print(f"| Set | n | {arm_a} hit@1 | {arm_b} hit@1 | {arm_b}-{arm_a} hit@1 [95% CI] | {arm_a} hit@5 | {arm_b} hit@5 | {arm_b}-{arm_a} hit@5 [95% CI] | verdict | changed (rank or top URL) | top-1 URL changed |")
    print("|---|---|---|---|---|---|---|---|---|---|---|")
    tot = {"a1": [], "b1": [], "a5": [], "b5": []}
    for s in sets:
        key, _, label = s.partition("=")
        a, b = load(f"{d}/d-{key}-{arm_a}.tsv"), load(f"{d}/d-{key}-{arm_b}.tsv")
        assert [x[0] for x in a] == [x[0] for x in b], f"{key}: query order differs"
        n = len(a)
        a1 = [float(x[1] == 1) for x in a]; b1 = [float(x[1] == 1) for x in b]
        a5 = [float(x[1] <= 5) for x in a]; b5 = [float(x[1] <= 5) for x in b]
        for k, v in (("a1", a1), ("b1", b1), ("a5", a5), ("b5", b5)):
            tot[k] += v
        cells, ok = [], True
        for A, B in ((a1, b1), (a5, b5)):
            diff = [y - x for x, y in zip(A, B)]
            lo, hi = ce.boot(diff)
            m = sum(diff) / n
            ok &= ce.passes(lo, hi, m)
            cells.append((sum(A) / n, sum(B) / n, m, lo, hi))
        chg = sum(1 for x, y in zip(a, b) if x[1] != y[1] or x[2] != y[2])
        urlchg = sum(1 for x, y in zip(a, b) if x[2] != y[2])
        (p1, q1, m1, l1, h1), (p5, q5, m5, l5, h5) = cells
        print(f"| {label or key} | {n} | {p1:.3f} | {q1:.3f} | {m1:+.3f} [{l1:+.3f}, {h1:+.3f}] | {p5:.3f} | {q5:.3f} | "
              f"{m5:+.3f} [{l5:+.3f}, {h5:+.3f}] | {'pass' if ok else 'FAIL'} | {chg} | {urlchg} |")
    n = len(tot["a1"])
    cells, ok = [], True
    for A, B in ((tot["a1"], tot["b1"]), (tot["a5"], tot["b5"])):
        diff = [y - x for x, y in zip(A, B)]
        lo, hi = ce.boot(diff)
        m = sum(diff) / n
        ok &= ce.passes(lo, hi, m)
        cells.append((sum(A) / n, sum(B) / n, m, lo, hi))
    (p1, q1, m1, l1, h1), (p5, q5, m5, l5, h5) = cells
    print(f"| **pooled** | {n} | {p1:.3f} | {q1:.3f} | {m1:+.3f} [{l1:+.3f}, {h1:+.3f}] | {p5:.3f} | {q5:.3f} | "
          f"{m5:+.3f} [{l5:+.3f}, {h5:+.3f}] | {'pass' if ok else 'FAIL'} | | |")


if __name__ == "__main__":
    main()
