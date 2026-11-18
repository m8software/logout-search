#!/usr/bin/env python3
"""Kinds A/B (ADR-064): ask one search-api for every eval query, save the top 10.

    tools/kinds-open-eval.py collect --api http://127.0.0.1:PORT --arm NAME [--extra '&off=...'] --out DIR
    tools/kinds-open-eval.py report DIR --arms off,old,new

Sets (all read from DIR/sets/): hand46.tsv (query, site, gold domains — tools/eval-queries.py
format), controls.txt + topics.txt (the article is right), descriptive.txt (258, unlabelled),
q349.txt. Scoring uses tools/eval-queries.py's hit() — no model.
  hand46        #1 / top-5 gold-domain hits
  controls      the article kinds-off puts first is still first / still in the top 5
  descriptive   lift coverage; #1 agreement and top-5 overlap between arms (diagnostic only)
  q349          top-10 overlap and #1 agreement between arms
Failed requests are retried once at the end; whatever still fails is counted per arm.
"""
import argparse
import importlib.util
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("eq", HERE / "eval-queries.py")
eq = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(eq)


def lines(p: Path) -> list[str]:
    return [x.strip() for x in p.read_text().splitlines() if x.strip() and not x.startswith("#")]


def sets(d: Path) -> dict[str, list[str]]:
    s = d / "sets"
    return {"hand46": [x.split("\t")[0] for x in lines(s / "hand46.tsv")],
            "controls": lines(s / "controls.txt"), "topics": lines(s / "topics.txt"),
            "descriptive": lines(s / "descriptive.txt"), "q349": lines(s / "q349.txt"),
            "labelled": [x.split("\t")[0] for x in lines(s / "labelled.tsv")] if (s / "labelled.tsv").exists() else []}


def ask(api: str, q: str, extra: str) -> dict:
    url = api + "/search?k=10" + extra + "&q=" + urllib.parse.quote(q)
    d = json.load(urllib.request.urlopen(url, timeout=30))
    return {"results": [{k: r.get(k) for k in ("url", "source", "kind", "kind_top", "score")}
                        for r in d.get("results", [])[:10]]}


def collect(a) -> None:
    d = Path(a.out)
    qs = sorted({q for v in sets(d).values() for q in v})
    out, failed = {}, []
    t0 = time.time()
    for i, q in enumerate(qs):
        try:
            out[q] = ask(a.api, q, a.extra)
        except Exception as e:
            failed.append(q)
            print(f"fail {q!r}: {type(e).__name__}", file=sys.stderr)
        if i % 100 == 0:
            print(f"{a.arm}: {i}/{len(qs)} {time.time() - t0:.0f}s", file=sys.stderr)
    still = []
    for q in failed:
        try:
            out[q] = ask(a.api, q, a.extra)
        except Exception:
            still.append(q)
            out[q] = {"error": True, "results": []}
    (d / f"{a.arm}.json").write_text(json.dumps(out))
    print(f"{a.arm}: {len(qs)} queries, {len(failed)} failed once, {len(still)} failed twice, "
          f"{time.time() - t0:.0f}s", file=sys.stderr)


def dom(r: dict) -> str:
    return eq.domain_of(r.get("url") or "")


def urls(res: dict, n: int) -> list[str]:
    return [(r.get("url") or "").rstrip("/").lower() for r in res.get("results", [])[:n]]


def report(a) -> None:
    d = Path(a.dir)
    S = sets(d)
    arms = a.arms.split(",")
    R = {arm: json.loads((d / f"{arm}.json").read_text()) for arm in arms}
    gold = {x.split("\t")[0]: x.split("\t")[2].split("|") for x in lines(d / "sets" / "hand46.tsv")}
    print("## hand46 (n=46, gold domains; higher is better)\n")
    print("| arm | #1 right | top-5 right | errors |\n|---|---|---|---|")
    per = {}
    for arm in arms:
        h1 = h5 = err = 0
        for q in S["hand46"]:
            res = R[arm].get(q, {"error": True, "results": []})
            err += bool(res.get("error"))
            rs = res["results"]
            r1 = bool(rs) and eq.hit("site", gold[q], rs[0])
            r5 = any(eq.hit("site", gold[q], r) for r in rs[:5])
            per.setdefault(q, {})[arm] = (r1, r5, dom(rs[0]) if rs else "")
            h1 += r1
            h5 += r5
        print(f"| {arm} | {h1}/46 | {h5}/46 | {err} |")
    if len(arms) >= 2:
        for x in arms[1:]:
            w = sum(per[q][x][0] and not per[q][arms[0]][0] for q in S["hand46"])
            lo = sum(per[q][arms[0]][0] and not per[q][x][0] for q in S["hand46"])
            print(f"\n{x} vs {arms[0]} at #1: {w} won, {lo} lost (paired, same 46 queries)")
        if "old" in arms:
            for x in arms:
                if x != "old":
                    w = sum(per[q][x][0] and not per[q]["old"][0] for q in S["hand46"])
                    lo = sum(per[q]["old"][0] and not per[q][x][0] for q in S["hand46"])
                    print(f"{x} vs old at #1: {w} won, {lo} lost")
        print("\nPer-query #1 changes vs the first arm (query: arm=top domain [right?]):\n")
        for q in S["hand46"]:
            v = per[q]
            if len({v[arm][0] for arm in arms}) > 1 or len({v[arm][2] for arm in arms}) > 1:
                print("- " + q + ": " + ", ".join(f"{arm}={v[arm][2]}{' ✓' if v[arm][0] else ''}" for arm in arms))
    if S["labelled"]:
        lab = [x.split("\t") for x in lines(d / "sets" / "labelled.tsv")]
        print(f"\n## hand-labelled v2+v3 site/wiki queries (n={len(lab)}; real searches; higher is better)\n")
        print("| arm | site #1 | site top-5 | wiki #1 | wiki top-5 | errors |\n|---|---|---|---|---|---|")
        for arm in arms:
            c = {"site": [0, 0, 0], "wiki": [0, 0, 0]}
            err = 0
            for q, cat, exp in lab:
                res = R[arm].get(q, {"error": True, "results": []})
                err += bool(res.get("error"))
                rs, e = res["results"], exp.split("|")
                c[cat][0] += 1
                c[cat][1] += bool(rs) and eq.hit(cat, e, rs[0])
                c[cat][2] += any(eq.hit(cat, e, r) for r in rs[:5])
            print(f"| {arm} | {c['site'][1]}/{c['site'][0]} | {c['site'][2]}/{c['site'][0]} | "
                  f"{c['wiki'][1]}/{c['wiki'][0]} | {c['wiki'][2]}/{c['wiki'][0]} | {err} |")
        print("\nPer-query #1 changes on the labelled set (arm=top domain, ✓ = right):\n")
        for q, cat, exp in lab:
            tops = {arm: (R[arm].get(q, {"results": []})["results"] or [{}])[0] for arm in arms}
            if len({(t.get("url") or "") for t in tops.values()}) > 1:
                print("- " + q + ": " + ", ".join(
                    f"{arm}={dom(t) or '-'}{' ✓' if t and eq.hit(cat, exp.split('|'), t) else ''}" for arm, t in tops.items()))
    base = arms[0]
    for name in ("controls", "topics"):
        print(f"\n## {name} (n={len(S[name])}; the article {base} puts first must stay first / in the top 5)\n")
        print("| arm | same #1 as " + base + " | " + base + "'s #1 article still in top 5 | errors |\n|---|---|---|---|")
        for arm in arms:
            same = kept = err = 0
            for q in S[name]:
                b, r = R[base].get(q, {"results": []}), R[arm].get(q, {"error": True, "results": []})
                err += bool(r.get("error"))
                bu, ru = urls(b, 1), urls(r, 5)
                same += bool(bu) and bool(ru) and bu[0] == ru[0]
                kept += bool(bu) and bu[0] in ru
            print(f"| {arm} | {same}/{len(S[name])} | {kept}/{len(S[name])} | {err} |")
    print(f"\n## descriptive (n={len(S['descriptive'])}, unlabelled; diagnostics, not a quality score)\n")
    print("| arm | kind lifted to #1 | any kind result in top 5 | errors |\n|---|---|---|---|")
    for arm in arms:
        lift = anyk = err = 0
        for q in S["descriptive"]:
            r = R[arm].get(q, {"error": True, "results": []})
            err += bool(r.get("error"))
            lift += any(x.get("kind_top") for x in r["results"][:1]) or (bool(r["results"]) and bool(r["results"][0].get("kind")))
            anyk += any(x.get("kind") for x in r["results"][:5])
        print(f"| {arm} | {lift} | {anyk} | {err} |")
    other = set(S["descriptive"]) | set(S["controls"])
    S["q349-only"] = [q for q in S["q349"] if q not in other]      # the 349 file holds all 258 + 40
    for name, n in (("descriptive", 5), ("q349", 10), ("q349-only", 10), ("hand46", 5), ("labelled", 10)):
        if not S[name]:
            continue
        print(f"\n## agreement on {name} (n={len(S[name])}; top-{n} overlap = shared URLs / {n}, mean)\n")
        print(f"| pair | same #1 | top-{n} overlap |\n|---|---|---|")
        for i in range(len(arms)):
            for j in range(i + 1, len(arms)):
                x, y = arms[i], arms[j]
                s1 = ov = 0
                for q in S[name]:
                    ux, uy = urls(R[x].get(q, {"results": []}), n), urls(R[y].get(q, {"results": []}), n)
                    s1 += bool(ux) and bool(uy) and ux[0] == uy[0]
                    ov += len(set(ux) & set(uy)) / n
                print(f"| {x} vs {y} | {s1}/{len(S[name])} | {ov / len(S[name]):.3f} |")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    c = sp.add_parser("collect")
    c.add_argument("--api", required=True)
    c.add_argument("--arm", required=True)
    c.add_argument("--extra", default="")
    c.add_argument("--out", required=True)
    r = sp.add_parser("report")
    r.add_argument("dir")
    r.add_argument("--arms", default="off,old,new")
    a = ap.parse_args()
    collect(a) if a.cmd == "collect" else report(a)


if __name__ == "__main__":
    main()
