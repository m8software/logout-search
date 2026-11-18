"""T4 whole-engine A/B: two arms' answers (run.py) on the hand / click label sets, agreement,
speed.

  python3 compare.py search A.json B.json LABELS.tsv...     hit@1 / hit@5 per set, B - A with the
                                                          95% paired bootstrap, the acceptance rule;
                                                          agreement on every query; wall time
  python3 compare.py instant A.json B.json                suggestions / phrases / card / place agreement

Acceptance rule (SEARCH-BOX-PLAN, ADR-064): per set, the CI of B - A includes 0 and the point
drop is at most 0.5 pt (or the CI is above 0). hit() is eval-queries.py's.
"""
import json, random, statistics, sys
from urllib.parse import unquote, urlsplit

N_BOOT, DROP_MAX = 10000, 0.005


def domain_of(url):
    return (urlsplit(url).hostname or "").lower()


def hit(cat, expected, r):
    if cat in ("site", "click"):
        h = domain_of(r.get("url", ""))
        return any(h == d or h.endswith("." + d) for d in expected)
    if cat == "wiki":
        if r.get("source") != "wikipedia":
            return False
        title = unquote(r["url"].rsplit("/wiki/", 1)[-1]).replace("_", " ")
        return any(title.lower() == e.lower() for e in expected)
    return False


def boot(d, seed=7):
    rnd = random.Random(seed)
    n = len(d)
    m = sorted(sum(d[rnd.randrange(n)] for _ in range(n)) / n for _ in range(N_BOOT))
    return m[int(0.025 * N_BOOT)], m[int(0.975 * N_BOOT) - 1]


def pct(v, p):
    v = sorted(v)
    return v[min(len(v) - 1, int(p * len(v)))] if v else float("nan")


def load(path):
    return {r["q"]: r for r in json.load(open(path))}


def urls(r):
    return [x["url"] for x in r["d"].get("results", [])]


def search(a_path, b_path, labels):
    A, B = load(a_path), load(b_path)
    print("| Set | Metric | n | A | B | B − A | CI low | CI high | Rule |")
    print("|---|---|---|---|---|---|---|---|---|")
    for lf in labels:
        rows = []
        for line in open(lf, encoding="utf-8"):
            q, cat, exp = line.rstrip("\n").split("\t")[:3]
            if cat in ("site", "click", "wiki") and exp != "-" and q in A and q in B:
                rows.append((q, cat, exp.split("|")))
        for k in (1, 5):
            ha = [float(any(hit(c, e, r) for r in A[q]["d"].get("results", [])[:k])) for q, c, e in rows]
            hb = [float(any(hit(c, e, r) for r in B[q]["d"].get("results", [])[:k])) for q, c, e in rows]
            d = [y - x for x, y in zip(ha, hb)]
            lo, hi = boot(d)
            delta = statistics.mean(d)
            ok = (lo <= 0 <= hi and delta >= -DROP_MAX) or lo > 0
            name = lf.rsplit("/", 1)[-1].rsplit(".", 1)[0]
            print(f"| {name} | hit@{k} | {len(rows)} | {statistics.mean(ha):.3f} | {statistics.mean(hb):.3f} | {delta:+.3f} | "
                  f"{lo:+.3f} | {hi:+.3f} | {'pass' if ok else 'FAIL'} |")
    qs = [q for q in A if q in B]
    ea = sum(1 for q in qs if "error" in A[q]["d"]); eb = sum(1 for q in qs if "error" in B[q]["d"])
    same_list = sum(1 for q in qs if urls(A[q]) == urls(B[q]))
    same1 = [q for q in qs if urls(A[q])[:1] == urls(B[q])[:1]]
    ov = [len(set(urls(A[q])) & set(urls(B[q]))) / max(len(urls(A[q])), len(urls(B[q])), 1) for q in qs]
    print(f"\nagreement on {len(qs)} queries: identical top-10 lists {same_list} ({100 * same_list / len(qs):.1f}%), "
          f"same #1 {len(same1)} ({100 * len(same1) / len(qs):.1f}%), mean top-10 overlap {statistics.mean(ov):.3f}; "
          f"errors A {ea}, B {eb}")
    for nm, X in (("A", A), ("B", B)):
        ms = [X[q]["ms"] for q in qs]
        print(f"wall ms {nm}: p50 {pct(ms, .5):.0f} p90 {pct(ms, .9):.0f} mean {statistics.mean(ms):.0f}")
    diff = [q for q in qs if urls(A[q])[:1] != urls(B[q])[:1]]
    print("\n#1 differs (first 25):")
    for q in diff[:25]:
        print(f"  {q[:50]!r}: {urls(A[q])[:1]} -> {urls(B[q])[:1]}")


def instant(a_path, b_path):
    A, B = load(a_path), load(b_path)
    qs = [q for q in A if q in B]
    print("| Part | n (either arm has it) | identical | differ |")
    print("|---|---|---|---|")
    for part in ("suggest", "phrases", "card", "place", "kinds"):
        n = same = 0
        for q in qs:
            x, y = A[q]["d"].get(part), B[q]["d"].get(part)
            if not x and not y:
                continue
            n += 1
            same += json.dumps(x, sort_keys=True) == json.dumps(y, sort_keys=True)
        print(f"| {part} | {n} | {same} | {n - same} |")
    for nm, X in (("A", A), ("B", B)):
        ms = [X[q]["ms"] for q in qs]
        print(f"wall ms {nm}: p50 {pct(ms, .5):.0f} p90 {pct(ms, .9):.0f}")
    shown = 0
    for q in qs:
        for part in ("suggest", "card", "place"):
            x, y = A[q]["d"].get(part), B[q]["d"].get(part)
            if (x or y) and json.dumps(x, sort_keys=True) != json.dumps(y, sort_keys=True) and shown < 15:
                shown += 1
                print(f"  {part} {q[:40]!r}: {json.dumps(x)[:160]} || {json.dumps(y)[:160]}")


if __name__ == "__main__":
    if sys.argv[1] == "search":
        search(sys.argv[2], sys.argv[3], sys.argv[4:])
    else:
        instant(sys.argv[2], sys.argv[3])
