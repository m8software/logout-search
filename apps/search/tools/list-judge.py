#!/usr/bin/env python3
"""list-judge.py: the quality of the WHOLE list under the card, judged row by row by Clef 27B (2026-11-07).

Owner: "what were looking for at this point is increasing the breadth of quality results, i think we pretty much nail
whats on the card". So the primary score of an engine arm is its page list (/instant?full=1, the rows the page shows
under the card, up to 8), every row judged relevant or not.

    list-judge.py fetch ENGINE ARM OUTDIR QUERIES.tsv [QUERIES2.tsv ...]   the page list per search -> OUTDIR/ARM.lists.jsonl
    list-judge.py judge OUTDIR ARM [ARM ...]                                 judge every row (Clef 27B, free tier), print the table
    list-judge.py export OUTDIR ARM [ARM ...]                                one JSON line per shown row with its cached verdict (no Clef call)

QUERIES: query<TAB>kind[<TAB>source] (evalsets/mix-300.tsv), or an open/judged set with a header (judged-info.tsv:
its rows count as kind "judged-info"). Clef sees the query, the row's title, URL and kind, nothing from the page
(ADR-059); answers are cached on that content, so a row two arms share is judged once and a new arm costs only its
new rows. FREE TIER ONLY: clef.judge stops at the day's allowance (exit 2); rerun after 00:00 UTC for the rest.

Per arm: relevant rows per search (mean; a row is relevant at P >= 0.5), share of searches with >= 5 relevant rows,
distinct sites in the 8 rows, Wikipedia rows per search, precision (relevant rows / shown rows) and not-relevant rows
shown per search, and the same by kind. LIST_JUDGE_SKIP_KINDS=judged-info leaves
those searches out (judged-info is ~1,000 searches, ~8,000 rows: days of the free allowance on its own).
"""
import collections
import json
import os
import sys
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
TOP = 8
EXTRA = os.environ.get("LIST_JUDGE_EXTRA", "")    # e.g. "&bl=JDL": an A/B flag on one engine (2026-11-07)


def read_queries(paths):
    out = []
    for p in paths:
        for line in open(p, encoding="utf-8"):
            f = line.rstrip("\n").split("\t")
            if not f[0] or f[0].startswith("#") or f[:2] == ["query", "category"]:
                continue
            kind = "judged-info" if Path(p).name.startswith("judged-info") else (f[1] if len(f) > 1 else "")
            out.append((f[0], kind))
    seen, uniq = set(), []
    for q, k in out:
        if q not in seen:
            seen.add(q); uniq.append((q, k))
    return uniq


def site(u: str) -> str:
    h = (urllib.parse.urlsplit(u).hostname or "").lower().removeprefix("www.")
    p = h.split(".")
    return ".".join(p[-3:]) if len(p) >= 3 and p[-2] in ("co", "com", "org", "ac", "gov", "net", "edu") and len(p[-1]) == 2 else ".".join(p[-2:])


def cmd_fetch(engine, arm, outdir, qfiles):
    qs = read_queries(qfiles)
    Path(outdir).mkdir(parents=True, exist_ok=True)

    def one(qk):
        q, k = qk
        try:
            with urllib.request.urlopen(f"{engine}/instant?full=1{EXTRA}&q=" + urllib.parse.quote(q), timeout=120) as r:
                d = json.load(r)
        except Exception as e:  # noqa: BLE001 - a failed search is an empty list, counted as such
            return {"q": q, "kind": k, "rows": [], "error": type(e).__name__}
        rows = [{"url": x.get("url", ""), "title": x.get("title", ""), "source": x.get("source", ""),
                 **{f: x[f] for f in ("score", "ce") if x.get(f) is not None}}      # the reranker score, for precision by score
                for x in (d.get("results") or [])[:TOP] if x.get("url")]
        return {"q": q, "kind": k, "card": bool(d.get("card")), "rows": rows}
    with ThreadPoolExecutor(3) as ex, open(Path(outdir) / f"{arm}.lists.jsonl", "w", encoding="utf-8") as f:
        for o in ex.map(one, qs):
            f.write(json.dumps(o, ensure_ascii=False) + "\n")
    print(f"{arm}: {len(qs)} searches fetched", file=sys.stderr)


def cmd_judge(outdir, arms):
    sys.path.insert(0, str(REPO / "utility" / "lib"))
    import clef  # noqa: E402
    import importlib.util
    spec = importlib.util.spec_from_file_location("sae", REPO / "utility" / "search-answer-eval.py")
    sae = importlib.util.module_from_spec(spec); spec.loader.exec_module(sae)
    skip = set(filter(None, os.environ.get("LIST_JUDGE_SKIP_KINDS", "").split(",")))   # e.g. judged-info: ~1,000 searches
    lists = {a: [o for o in map(json.loads, open(Path(outdir) / f"{a}.lists.jsonl", encoding="utf-8")) if o["kind"] not in skip]
             for a in arms}
    P, done, seen = {}, [], set()
    for a in arms:                       # arm by arm, so an arm is reported only once every row of it is judged
        pairs = []
        for o in lists[a]:
            for r in o["rows"]:
                k = (o["q"], r["url"], r["title"], r["source"])
                if k not in seen:
                    seen.add(k); pairs.append((o["q"], 0, r))      # position 0 for all: one verdict per (query, row)
        print(f"{a}: {len(pairs)} new rows to judge", file=sys.stderr)
        try:
            ans, use = clef.judge(pairs, sae.state_of, sae.sr.RELEVANT, "list-judge", progress=False)
        except SystemExit:
            print(f"stopped at the free allowance inside {a}: rerun after 00:00 UTC (judged rows are cached); "
                  f"complete arms: {' '.join(done) or 'none'}", file=sys.stderr)
            table(done, lists, P)
            raise
        P.update({(q, r["url"], r["title"], r["source"]): clef.noul(x, "answers") for (q, _p, r), x in zip(pairs, ans) if x is not None})
        print(f"{a}: {use.line(len(pairs))}", file=sys.stderr)
        done.append(a)
    table(done, lists, P)


def cmd_export(outdir, arms):
    """One JSON line per shown row: arm, q, kind, card, pos, url, title, source, score/ce when the list has them, and p, the
    cached Clef verdict (null = not judged). Reads the verdict cache only; asks Clef nothing."""
    sys.path.insert(0, str(REPO / "utility" / "lib"))
    import clef  # noqa: E402
    import importlib.util
    spec = importlib.util.spec_from_file_location("sae", REPO / "utility" / "search-answer-eval.py")
    sae = importlib.util.module_from_spec(spec); spec.loader.exec_module(sae)
    cache = clef.Cache("list-judge")
    for a in arms:
        for o in map(json.loads, open(Path(outdir) / f"{a}.lists.jsonl", encoding="utf-8")):
            for pos, r in enumerate(o["rows"]):
                ans = cache.get(clef.Cache.key(sae.state_of((o["q"], 0, r)), sae.sr.RELEVANT))
                print(json.dumps({"arm": a, "q": o["q"], "kind": o["kind"], "card": o.get("card"), "pos": pos + 1, **r,
                                  "p": clef.noul(ans, "answers") if ans is not None else None}, ensure_ascii=False))


def table(arms, lists, P):
    head = ("| arm | kind | searches | relevant rows / search | >= 5 relevant | distinct sites in 8 | Wikipedia rows / search "
            "| precision (relevant / shown) | not-relevant rows shown / search |")
    print(head); print("|" + "---|" * 9)
    for a in arms:
        by = collections.defaultdict(list)
        for o in lists[a]:
            rel = sum(1 for r in o["rows"] if P.get((o["q"], r["url"], r["title"], r["source"]), 0.0) >= 0.5)
            by["all"].append((rel, len({site(r["url"]) for r in o["rows"]}), sum(1 for r in o["rows"] if "wikipedia.org" in r["url"]),
                              len(o["rows"])))
            by[o["kind"]].append(by["all"][-1])
        for k in ["all"] + sorted(x for x in by if x != "all"):
            v = by[k]; n = len(v)
            print(f"| {a} | {k} | {n} | {sum(x[0] for x in v) / n:.2f} | {100 * sum(1 for x in v if x[0] >= 5) / n:.1f}% | "
                  f"{sum(x[1] for x in v) / n:.2f} | {sum(x[2] for x in v) / n:.2f} | "
                  f"{100 * sum(x[0] for x in v) / max(1, sum(x[3] for x in v)):.1f}% | {sum(x[3] - x[0] for x in v) / n:.2f} |")


if __name__ == "__main__":
    if len(sys.argv) >= 5 and sys.argv[1] == "fetch":
        cmd_fetch(sys.argv[2].rstrip("/"), sys.argv[3], sys.argv[4], sys.argv[5:])
    elif len(sys.argv) >= 4 and sys.argv[1] == "export":
        cmd_export(sys.argv[2], sys.argv[3:])
    elif len(sys.argv) >= 4 and sys.argv[1] == "judge":
        cmd_judge(sys.argv[2], sys.argv[3:])
    else:
        sys.exit(__doc__)
