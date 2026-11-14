#!/usr/bin/env python3
"""Tables for the Tantivy structure audit.

    analyze.py bench BENCH.jsonl            median (min-max) of p50/p90 per variant x memory cap
    analyze.py alpha DUMP.jsonl PG.tsv [RANKS.tsv]   hot/cold skip rule: for each alpha, how often cold is
                                            skipped and the top-10 overlap with Postgres, tuned on
                                            the even-numbered queries, reported on the odd ones

The skip rule: search cold unless hot returned 10 hits and hot's 10th score >= alpha * bound,
bound = the best score any cold document can reach for the query's terms. alpha = 1 is exact.
"""
import json
import statistics
import sys


def bench(path):
    rows = []
    for line in open(path):
        try:
            rows.append(json.loads(line))
        except ValueError:
            print("UNPARSEABLE (failed run):", line[:90].strip())
    groups = {}
    for r in rows:
        if r["rc"] != 0 or not r["r"] or r["lines"] < 346:
            print("BAD RUN", {k: r[k] for k in ("tag", "variant", "mem", "round", "rc", "lines")})
            continue
        groups.setdefault((r["tag"], r["variant"], r["mem"]), []).append(r)
    print("| harness | variant | cap | runs | load1 | first p50 | first p90 | warm p50 | warm p90 | cold searched |")
    print("|---|---|---|---|---|---|---|---|---|---|")
    for (tag, v, mem), rs in sorted(groups.items(), key=lambda x: (x[0][0], x[0][2], x[0][1])):
        def cell(key):
            if tag != "py":
                vals = [r["r"]["runs"][0]["passes"][key[0]][key[1]] for r in rs]
            else:
                vals = [r["r"]["runs"][0][key[0]]["with_ids_" + key[1]] for r in rs]
            return f"{statistics.median(vals):.1f} ({min(vals):.1f}-{max(vals):.1f})"
        loads = [r["load1"] for r in rs]
        cs = rs[0]["r"].get("cold_searched", "") if tag != "py" else ""
        print(f"| {tag} | {v} | {mem} | {len(rs)} | {min(loads):.0f}-{max(loads):.0f} | {cell(('first', 'p50_ms'))} | "
              f"{cell(('first', 'p90_ms'))} | {cell(('warm', 'p50_ms'))} | {cell(('warm', 'p90_ms'))} | {cs} |")


def load_pg(path):
    d = {}
    for line in open(path, encoding="utf-8"):
        q, _, ids = line.rstrip("\n").partition("\t")
        d[q] = [int(x) for x in ids.split(",") if x]
    return d


def alpha(dump, pgfile, ranks=None):
    pg = load_pg(pgfile)
    recs = [json.loads(line) for line in open(dump)]
    if ranks:  # the dump holds doc ids; Postgres' reference holds page ids
        m = {}
        for line in open(ranks, encoding="utf-8"):
            p = line.rstrip("\n").split("\t")
            m[int(p[3])] = int(p[0])
        for r in recs:
            for k in ("hot", "cold"):
                r[k] = [(sc, m.get(i, i)) for sc, i in r[k]]
    recs = [r for r in recs if pg.get(r["q"])]

    def result(r, a):
        hot = [(s, i) for s, i in r["hot"]]
        skip = a is not None and len(hot) == 10 and hot[9][0] >= a * r["bound"]
        if skip:
            return [i for _, i in hot], True
        allh = sorted(hot + [(s, i) for s, i in r["cold"]], key=lambda x: (-x[0], x[1]))[:10]
        return [i for _, i in allh], False

    def score(sub, a):
        ov, sk, t1 = [], 0, 0
        for r in sub:
            ids, s = result(r, a)
            ref = pg[r["q"]]
            ov.append(len(set(ref[:10]) & set(ids[:10])) / max(1, min(10, len(ref))))
            t1 += bool(ids) and ids[0] == ref[0]
            sk += s
        return sum(ov) / len(ov), sk / len(sub), t1 / len(sub)

    even, odd = recs[0::2], recs[1::2]
    print(f"queries: {len(recs)} (tune on {len(even)} even, report on {len(odd)} odd)")
    print("| alpha | tune: overlap | tune: cold skipped | test: overlap | test: #1 agree | test: cold skipped |")
    print("|---|---|---|---|---|---|")
    for a in [None, 1.0, 0.9, 0.8, 0.7, 0.6, 0.5, 0.4, 0.3, 0.2, 0.0]:
        to, ts, _ = score(even, a)
        so, ss, s1 = score(odd, a)
        print(f"| {'never skip' if a is None else a} | {to:.3f} | {ts:.0%} | {so:.3f} | {s1:.3f} | {ss:.0%} |")
    # how far the exact bound sits above hot's 10th score
    ratios = sorted(r["hot"][9][0] / r["bound"] for r in recs if len(r["hot"]) == 10 and r["bound"] > 0)
    print("hot 10th / cold bound: p10 %.2f p50 %.2f p90 %.2f max %.2f (n=%d)" % (
        ratios[len(ratios) // 10], ratios[len(ratios) // 2], ratios[int(len(ratios) * .9)], ratios[-1], len(ratios)))


if __name__ == "__main__":
    if sys.argv[1] == "bench":
        bench(sys.argv[2])
    elif sys.argv[1] == "alpha":
        alpha(sys.argv[2], sys.argv[3], sys.argv[4] if len(sys.argv) > 4 else None)
    else:
        raise SystemExit(__doc__)
