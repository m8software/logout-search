#!/usr/bin/env python3
"""What the dev engine answered, how fast, and where it came back empty.

Reads the engine's query log (native_host.log_query, one JSON line per answered query) on
stdin or from FILES, restricted to the last DAYS days:

    dev-engine.sh stats [DAYS]            # fetches the log from indexhost and runs this
    query-stats.py [--days N] [FILE...]

Kinds: suggest = the as-you-type list (no results); results = the list with results;
full = the whole result page (the page prefetches it after a pause, so a full is "somebody
stopped typing", not necessarily an Enter). Times are wall ms inside the engine.
"""
import argparse
import collections
import datetime as dt
import json
import sys


def pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(len(xs) * p))] if xs else 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=float, default=7)
    ap.add_argument("files", nargs="*")
    a = ap.parse_args()
    lines = []
    for f in a.files or ["-"]:
        lines += (sys.stdin if f == "-" else open(f, encoding="utf-8")).read().splitlines()
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=a.days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    recs = []
    for ln in lines:
        try:
            r = json.loads(ln)
        except ValueError:
            continue
        if r.get("t", "") >= since:
            recs.append(r)
    if not recs:
        print(f"no queries logged in the last {a.days:g} days")
        return
    print(f"{len(recs)} queries since {since} (UTC), {len({r['q'].lower() for r in recs})} distinct\n")

    by = collections.defaultdict(list)
    for r in recs:
        by[r["kind"]].append(r)
    print("HOW LONG — engine time per answer, ms")
    print(f"  {'kind':<9}{'answers':>8}{'median':>8}{'p95':>8}{'max':>8}{'>1s':>6}")
    for k in ("suggest", "results", "full"):
        xs = [r["ms"] for r in by.get(k, [])]
        if xs:
            print(f"  {k:<9}{len(xs):>8}{pct(xs, .5):>8}{pct(xs, .95):>8}{max(xs):>8}{sum(x > 1000 for x in xs):>6}")
    print("  (suggest = as-you-type list, results = list with results, full = whole result page)\n")

    print("SLOWEST — the 10 slowest answers, with the stage that took longest")
    for r in sorted(recs, key=lambda r: -r["ms"])[:10]:
        st = r.get("stages") or {}
        top = max(st.items(), key=lambda kv: kv[1]) if st else None
        print(f"  {r['ms']:>6} ms  {r['kind']:<8} {r['q'][:50]!r}" + (f"  mostly {top[0]} ({top[1]} ms)" if top else ""))
    print()

    full = [r for r in recs if r["kind"] in ("full", "results") and "err" not in r]
    empty = collections.Counter(r["q"].lower() for r in full if not r.get("n") and not r.get("card") and not r.get("answer"))
    print(f"NOT WORKING — asked for results, got nothing ({sum(empty.values())} answers, {len(empty)} distinct)")
    for q, n in empty.most_common(15):
        print(f"  {n:>4}x  {q}")
    weak = collections.Counter(r["q"].lower() for r in full if r.get("n") and not r.get("conf"))
    print(f"\nUNSURE — results came back but the engine wasn't confident ({sum(weak.values())} answers, {len(weak)} distinct)")
    for q, n in weak.most_common(15):
        print(f"  {n:>4}x  {q}")
    sug_empty = collections.Counter(r["q"].lower() for r in by.get("suggest", []) if not r.get("sug") and not r.get("phr"))
    print(f"\nNO SUGGESTIONS — as-you-type list was empty ({sum(sug_empty.values())} answers)")
    for q, n in sug_empty.most_common(10):
        print(f"  {n:>4}x  {q}")
    errs = collections.Counter((r["err"], r["q"].lower()) for r in recs if "err" in r)
    if errs:
        print(f"\nERRORS ({sum(errs.values())})")
        for (e, q), n in errs.most_common(10):
            print(f"  {n:>4}x  {e}  {q}")

    print("\nWORKING — most-asked full searches, what came first")
    top = collections.Counter(r["q"].lower() for r in by.get("full", []))
    first = {r["q"].lower(): r for r in by.get("full", [])}
    for q, n in top.most_common(15):
        r = first[q]
        print(f"  {n:>4}x  {q:<30} {r.get('n', 0):>2} results, {r.get('intent') or '-':<6} → {(r.get('top') or '-')[:60]}")

    print("\nPER DAY / SOURCE")
    days = collections.Counter(r["t"][:10] for r in recs)
    src = collections.Counter(r.get("src") or "-" for r in recs)
    print("  " + "  ".join(f"{d}: {n}" for d, n in sorted(days.items())))
    print("  " + "  ".join(f"{s}: {n}" for s, n in src.most_common()))


if __name__ == "__main__":
    main()
