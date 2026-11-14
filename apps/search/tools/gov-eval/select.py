#!/usr/bin/env python3
"""Government pages a budget would fetch, from ccgov-scan's candidates file. DEV ONLY.

    select.py CANDIDATES.tsv OUT.tsv [--small 10,500] [--medium 20,1000]

OUT: the medium budget's rows with the arm in front ("small" rows are in both arms), small first, so
one fetch in file order serves both and the small arm can be measured while the rest is still read.
Run where importer/ccpages.py is importable (the importer image: /app).
"""
import argparse
import collections
import sys

import ccpages


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("cands"); ap.add_argument("out")
    ap.add_argument("--small", default="10,500"); ap.add_argument("--medium", default="20,1000")
    a = ap.parse_args()
    rows = [l.rstrip("\n").split("\t") for l in open(a.cands)]
    su, sd = map(int, a.small.split(","))
    mu, md = map(int, a.medium.split(","))
    small = ccpages.gov_cut(rows, su, sd)
    in_small = {r[4] for r in small}
    medium = [r for r in ccpages.gov_cut(rows, mu, md) if r[4] not in in_small]
    with open(a.out, "w") as f:
        for arm, part in (("small", small), ("medium", medium)):
            for r in part:
                f.write("\t".join([arm, *r]) + "\n")
    n = collections.Counter()
    for arm, part in (("small", small), ("medium", small + medium)):
        n[arm] = len(part)
        print(f"{arm}: {len(part):,} pages on {len({r[0] for r in part}):,} sites, "
              f"{sum(int(r[3]) >= 3 for r in part):,} with a service word", file=sys.stderr)


if __name__ == "__main__":
    main()
