#!/usr/bin/env python3
"""Is profile B as accurate as baseline A? The acceptance rule of docs/internal/SEARCH-BOX-PLAN.md.

    tools/compare-evals.py jev A.jsonl B.jsonl        search-ranker.py evaluate --out files
    tools/compare-evals.py labels A.tsv B.tsv          eval-queries.py --details files

Per query it pairs A with B and reports, for each metric, the mean of A, the mean of B,
B - A, and a 95% paired-bootstrap interval of B - A (10,000 resamples, fixed seed).
A cut PASSES a metric when the interval includes 0 and the drop is at most 0.5 points
(0.005 on a 0-1 scale). It also counts queries whose top-5 URLs are identical — the
test for a LOSSLESS cut is that this is every query, not that the metrics agree.

Jev runs are also broken out PER KIND (a cut can be neutral overall and wreck typos),
and with --demand DEMAND.tsv (tools/eval-demand.py) scored the way real searching is
distributed: each kind weighted by the real query mix (MIX, from 3 months of real
searches), each query inside its kind by how much people want its answer (FLOOR + pop).
The uniform eval set samples Wikipedia titles uniformly — mostly articles nobody looks
up — so without this a Wikipedia prune looks far worse than it is for real searches.
With --demand the verdict is the real-mix line's.
"""
import json
import math
import random
import sys

DROP_MAX = 0.005
N_BOOT = 10_000
FLOOR = 0.02            # weight of a query nobody measurably searches for (pop 0)

# Real query mix, from three months of one person's web searches
# (docs/internal/SEARCH-BOX-PLAN.md): find-a-site 22%, local places 17%, shopping 17%,
# how-to / comparisons / Reddit 12%, knowledge 10%, time-sensitive 6% (junk 17% left out).
# Each group's share is split over its eval kinds by their query counts.
MIX = {
    "site": (22, ("nav-brand", "nav-domain", "typo", "account", "youtube")),
    "local": (17, ("local", "local-brand")),
    "shop": (17, ("shop",)),
    "howto": (12, ("howto", "subreddit")),
    "knowledge": (10, ("wiki-topic", "topic", "question")),
    "fresh": (6, ("fresh",)),
}


def ndcg5(p: list[float]) -> float:
    """utility/search-ranker.py ndcg5: DCG@5 with Jev relevance as the gain, over the DCG of
    a perfect top five (all 1.0), so an empty list scores 0."""
    disc = [1 / math.log2(i + 2) for i in range(5)]
    return sum(g * d for g, d in zip(p[:5], disc)) / sum(disc)


def load_jev(path: str) -> dict:
    out = {}
    for line in open(path, encoding="utf-8"):
        if line.strip():
            o = json.loads(line)
            p = o.get("p") or []
            out[o["q"]] = {"at1": p[0] if p else 0.0, "best5": max(p[:5]) if p else 0.0, "ndcg5": ndcg5(p),
                           "_urls": tuple(r.get("url", "") for r in (o.get("results") or [])[:5]),
                           "_kind": o.get("kind", "")}
    return out


def load_labels(path: str) -> dict:
    out = {}
    with open(path, encoding="utf-8") as f:
        next(f)                                           # header
        for line in f:
            # six columns; open-data runs append set and stratum
            q, cat, rank, _top, url, _conf = line.rstrip("\n").split("\t")[:6]
            if cat not in ("site", "wiki", "click", "account", "place"):
                continue
            r = int(rank) if rank not in ("-", "") else 99
            out[q] = {"hit1": float(r == 1), "hit5": float(r <= 5), "_urls": (url,)}
    return out


def boot(diffs: list[float], seed: int = 7) -> tuple[float, float]:
    rnd = random.Random(seed)
    n = len(diffs)
    means = sorted(sum(diffs[rnd.randrange(n)] for _ in range(n)) / n for _ in range(N_BOOT))
    return means[int(0.025 * N_BOOT)], means[int(0.975 * N_BOOT) - 1]


def load_demand(path: str) -> dict:
    out = {}
    for line in open(path, encoding="utf-8"):
        parts = line.rstrip("\n").split("\t")
        if len(parts) >= 3:
            out[parts[0]] = float(parts[2])
    return out


def kind_weights(kinds: dict) -> dict:
    """{kind: query count} -> {kind: share of the real mix}; kinds outside MIX get 0."""
    w = {}
    for share, members in MIX.values():
        present = [k for k in members if kinds.get(k)]
        total = sum(kinds[k] for k in present)
        for k in present:
            w[k] = share * kinds[k] / total
    norm = sum(w.values())
    return {k: v / norm for k, v in w.items()}


def mix_mean(qs_by_kind: dict, kw: dict, qw: dict, val) -> float:
    """sum over kinds of kind share x demand-weighted mean inside the kind."""
    tot = 0.0
    for k, qs in qs_by_kind.items():
        if not qs or kw.get(k, 0) == 0:
            continue
        ws = sum(qw[q] for q in qs)
        tot += kw[k] * sum(qw[q] * val(q) for q in qs) / ws
    return tot


def mix_boot(qs_by_kind: dict, kw: dict, qw: dict, diff, seed: int = 7) -> tuple[float, float]:
    """95% interval of the real-mix difference, resampling queries within each kind."""
    rnd = random.Random(seed)
    kinds = [k for k, qs in qs_by_kind.items() if qs and kw.get(k, 0) > 0]
    vals = {k: [(qw[q], diff(q)) for q in qs_by_kind[k]] for k in kinds}
    means = []
    for _ in range(N_BOOT):
        tot = 0.0
        for k in kinds:
            v = vals[k]; n = len(v)
            sw = sd = 0.0
            for _ in range(n):
                w, d = v[rnd.randrange(n)]
                sw += w; sd += w * d
            tot += kw[k] * sd / sw
        means.append(tot)
    means.sort()
    return means[int(0.025 * N_BOOT)], means[int(0.975 * N_BOOT) - 1]


def passes(lo: float, hi: float, delta: float) -> bool:
    return lo <= 0 <= hi and delta >= -DROP_MAX or lo > 0


def per_kind(a: dict, b: dict, qs: list) -> None:
    kinds = sorted({a[q]["_kind"] for q in qs})
    print(f"\nper kind — right at 1 and nDCG@5, B - A with the 95% interval (FAIL = a real loss)")
    print(f"{'kind':<12} {'n':>4}  {'at1 A':>6} {'at1 B':>6} {'at1 B-A [95% CI]':>24}  {'nDCG5 B-A [95% CI]':>24}  verdict")
    for k in kinds:
        ks = [q for q in qs if a[q]["_kind"] == k]
        cells, ok = [], True
        for m in ("at1", "ndcg5"):
            d = [b[q][m] - a[q][m] for q in ks]
            lo, hi = boot(d)
            delta = sum(d) / len(d)
            ok &= passes(lo, hi, delta)
            cells.append(f"{delta:+.3f} [{lo:+.3f}, {hi:+.3f}]")
        ma = sum(a[q]["at1"] for q in ks) / len(ks); mb = sum(b[q]["at1"] for q in ks) / len(ks)
        print(f"{k:<12} {len(ks):>4}  {ma:6.3f} {mb:6.3f} {cells[0]:>24}  {cells[1]:>24}  {'pass' if ok else 'FAIL'}")


def real_mix(a: dict, b: dict, qs: list, demand: dict) -> bool:
    by = {}
    for q in qs:
        by.setdefault(a[q]["_kind"], []).append(q)
    kw = kind_weights({k: len(v) for k, v in by.items()})
    qw = {q: FLOOR + demand.get(q, 0.0) for q in qs}
    print(f"\nreal-mix, demand-weighted (kind shares: " +
          ", ".join(f"{k} {v:.0%}" for k, v in sorted(kw.items(), key=lambda x: -x[1])) + ")")
    print(f"{'metric':<7} {'A':>7} {'B':>7} {'B-A':>8}  {'95% CI of B-A':>19}  verdict")
    ok_all = True
    for m in ("at1", "best5", "ndcg5"):
        ma = mix_mean(by, kw, qw, lambda q: a[q][m]); mb = mix_mean(by, kw, qw, lambda q: b[q][m])
        lo, hi = mix_boot(by, kw, qw, lambda q: b[q][m] - a[q][m])
        ok = passes(lo, hi, mb - ma)
        ok_all &= ok
        print(f"{m:<7} {ma:7.3f} {mb:7.3f} {mb - ma:+8.3f}  [{lo:+.3f}, {hi:+.3f}]  {'pass' if ok else 'FAIL'}")
    return ok_all


def main() -> None:
    args = sys.argv[1:]
    demand = None
    if "--demand" in args:
        i = args.index("--demand")
        demand = load_demand(args[i + 1])
        del args[i:i + 2]
    if len(args) != 3 or args[0] not in ("jev", "labels"):
        sys.exit(__doc__)
    load = load_jev if args[0] == "jev" else load_labels
    a, b = load(args[1]), load(args[2])
    qs = sorted(set(a) & set(b))
    if not qs:
        sys.exit("no queries in common")
    same = sum(1 for q in qs if a[q]["_urls"] == b[q]["_urls"])
    print(f"{len(qs)} queries in both ({len(a)} in A, {len(b)} in B); identical results: {same} ({same / len(qs):.1%})")
    print(f"{'metric':<7} {'A':>7} {'B':>7} {'B-A':>8}  {'95% CI of B-A':>19}  verdict")
    ok_all = True
    for m in [k for k in a[qs[0]] if not k.startswith("_")]:
        d = [b[q][m] - a[q][m] for q in qs]
        ma, mb = sum(a[q][m] for q in qs) / len(qs), sum(b[q][m] for q in qs) / len(qs)
        lo, hi = boot(d)
        ok = passes(lo, hi, mb - ma)
        ok_all &= ok
        print(f"{m:<7} {ma:7.3f} {mb:7.3f} {mb - ma:+8.3f}  [{lo:+.3f}, {hi:+.3f}]  {'pass' if ok else 'FAIL'}")
    print(f"(all queries, uniform: {'pass' if ok_all else 'FAIL'})")
    if args[0] == "jev":
        per_kind(a, b, qs)
        if demand is not None:
            ok_all = real_mix(a, b, qs, demand)
    label = "real-mix line" if demand is not None and args[0] == "jev" else "uniform line"
    print("\nPASS" if ok_all else "\nFAIL", f"— the acceptance rule on the {label} "
          "(CI includes 0 and drop <= 0.5 pt, or a significant gain)")


if __name__ == "__main__":
    main()
