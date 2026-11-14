#!/usr/bin/env python3
"""Curated answers for the warmed names (2026-11-05): the page's list chosen offline by large rerankers from the
engine's own candidates, written as a store file the engines take in through their inbox (result_store.curated).

    curated-build.py CANDIDATES.json OUT.sqlite --release proto-20261104-rc14 --qwen Q.jsonl [--bge B.jsonl]
                     [--first CUR.json] [--tq 0.5] [--tb 0.5] [--per-site 2] [--max 8] [--lists LISTS.json]

The whole run (2026-11-05, 19,515 stable warmed names; candidates on indexhost, the models on buildbox's 4070):
    1. candidates: /candidates?rows=50&text=1 merged pool per name, top 30; products also "buy X", "X brand" (15 each)
    2. curated-score.py PAIRS SCORES          Qwen3-Reranker-4B, P(yes) per pair (~32 pairs/s, batch 8)
    3. curated-mklw.py CAND SCORES CUR OUT    each name's current first row + the best 11 by Qwen
    4. curated-listwise.py IN OUT             Qwen3-14B (llama.cpp) sees the 12 together and picks, best first
    5. curated-build.py ... --whole OUT       this: the store file; engines take it in through their inbox

CANDIDATES: [{i, q, src, cand: [{u, t, c, s}]}] (the engine's merged pool per name, /candidates; pair ids
"p<i>-<k>" by position). Q / B: {"id", "p"} per pair from Qwen3-Reranker-4B and bge-reranker-v2-m3 on lr.
FIRST: [{i, cur: [{u, ...}]}] the page's list today: its first row stays first when Qwen gives it >= --tq
(the pilot: first row 72% -> 86% judged relevant, and fewer first rows change than letting the model pick).

A row below the first is kept only when Qwen gives it >= --tq and, with --bge, bge gives it >= --tb: two models
of different families must agree (one alone let "bug spray" -> flyspray.org through). At most --per-site rows of
one site, at most --max rows; the list may be one row long. Rows are stored as the page draws them (url, title,
content, source, score); nothing is fetched or invented.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
import zlib
from urllib.parse import urlsplit


def norm(u: str) -> str:
    return u.split("#")[0].rstrip("/").replace("://www.", "://")


def site(u: str) -> str:
    h = (urlsplit(u).hostname or "").lower().removeprefix("www.")
    p = h.split(".")
    return ".".join(p[-3:]) if len(p) >= 3 and p[-2] in ("co", "com", "org", "ac", "gov", "net", "edu") and len(p[-1]) == 2 \
        else ".".join(p[-2:])


def load(path):
    """{"<i>-<k>": p}: the pair ids' letter (p in the pilot, f in the full run) is dropped, so either run's scores fit."""
    return {(o := json.loads(line))["id"].lstrip("pf"): o["p"] for line in open(path, encoding="utf-8")} if path else {}


def load_candidates(path):
    """CANDIDATES as one JSON list (the pilot) or one name per line (the full run's full-cand.jsonl)."""
    with open(path, encoding="utf-8") as f:
        head = f.read(1); f.seek(0)
        return json.load(f) if head == "[" else [json.loads(line) for line in f if line.strip()]


def key(q: str) -> str:                       # search_api.curated_key
    return " ".join((q or "").lower().split())


def build(x: dict, Q: dict, B: dict, first: list | None, a) -> list[dict]:
    sc = []
    for k, r in enumerate(x.get("cand") or []):
        pid = f"{x['i']}-{k}"
        if pid in Q:
            sc.append((Q[pid], B.get(pid), r))
    sc.sort(key=lambda z: -z[0])
    if not sc:
        return []
    byu = {norm(r["u"]): (q, b, r) for q, b, r in sc}
    out, seen, per = [], set(), {}

    def add(q, r):
        out.append({"url": r["u"], "title": r["t"], "content": r.get("c", ""), "source": r.get("s", "site"),
                    "score": round(1000.0 - len(out), 1), "qwen": round(q, 4)})
        seen.add(norm(r["u"]))
        per[site(r["u"])] = per.get(site(r["u"]), 0) + 1

    e1 = norm(first[0]["u"]) if first else None
    agree = [(q, r) for q, b, r in sc if q >= a.tq and (not B or (b is not None and b >= a.tb))]
    if e1 and e1 in byu and byu[e1][0] >= a.tq:
        add(byu[e1][0], byu[e1][2])                  # the engine's first row, when the large model agrees
    elif agree:
        add(*agree[0])                               # else the best row both models agree on
    elif e1 and e1 in byu:
        add(byu[e1][0], byu[e1][2])                  # else the engine's own first ("bug spray": not flyspray.org)
    else:
        add(sc[0][0], sc[0][2])
    for q, b, r in sc:
        if len(out) >= a.max:
            break
        if norm(r["u"]) in seen or q < a.tq or (B and (b is None or b < a.tb)) or per.get(site(r["u"]), 0) >= cap(r["u"], a):
            continue
        add(q, r)
    return out


def cap(u: str, a) -> int:
    """Rows one site may hold: --per-site, but Wikipedia's articles are separate topics (2026-11-06: "spider-man"
    lost "in other media" and the 2017 series the list model had picked to a cap of 2), so --per-wiki for it."""
    return a.per_wiki if site(u).endswith("wikipedia.org") else a.per_site


def build_whole(x: dict, Q: dict, picks: list[str], a) -> list[dict]:
    """The whole-list pass's order (a strong model saw the top candidates together: it drops a different meaning
    of the words, "bug spray" -> flyspray.org), its first row kept, a later row only when Qwen gives it >= --tw.
    Pilot, 300 names, clef-flash judge: row 1 89%, rows 2-5 83%, 4.4 rows (today 72% / 26% / 5)."""
    by = {}
    for k, r in enumerate(x.get("cand") or []):
        by.setdefault(norm(r["u"]), (Q.get(f"{x['i']}-{k}", 0.0), r))
    out, per = [], {}
    for j, u in enumerate(picks):
        q, r = by.get(norm(u), (None, None))
        if r is None or (j and q < a.tw) or per.get(site(r["u"]), 0) >= cap(r["u"], a) or len(out) >= a.max:
            continue
        out.append({"url": r["u"], "title": r["t"], "content": r.get("c", ""), "source": r.get("s", "site"),
                    "score": round(1000.0 - len(out), 1), "qwen": round(q, 4)})
        per[site(r["u"])] = per.get(site(r["u"]), 0) + 1
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("candidates"); ap.add_argument("out")
    ap.add_argument("--release", required=True); ap.add_argument("--qwen", required=True); ap.add_argument("--bge")
    ap.add_argument("--first"); ap.add_argument("--tq", type=float, default=0.5); ap.add_argument("--tb", type=float, default=0.5)
    ap.add_argument("--per-site", type=int, default=2); ap.add_argument("--max", type=int, default=8)
    ap.add_argument("--per-wiki", type=int, default=4)
    ap.add_argument("--lists", help="also write {i: {q, src, rows: [url, ...]}} for judging")
    ap.add_argument("--whole", help="the whole-list pass (listwise.py: {i, rows: [url, ...]} per line): its order, "
                    "first row kept, a later row only with Qwen >= --tw (the pilot's best recipe)")
    ap.add_argument("--tw", type=float, default=0.3)
    ap.add_argument("--grades", help="curated_grade.py's file ({grades: {i: {url: grade}}}): a row graded wrong is dropped, and "
                    "a list left with no row graded exact or good is written EMPTY (the engine's own list was better for such "
                    "names: 2026-11-11, 300 of 2,151). A name or row without a grade is kept as built")
    ap.add_argument("--pulled", help="JSON list of names whose curated list was removed on review: such a name gets an "
                    "EMPTY list (the engine then answers it as it would without one), so a rebuild never brings a pulled list back")
    a = ap.parse_args()
    P = {key(q) for q in json.load(open(a.pulled, encoding="utf-8"))} if a.pulled else set()
    G = json.load(open(a.grades, encoding="utf-8")).get("grades", {}) if a.grades else {}
    graded_out = dropped = 0
    C = load_candidates(a.candidates)
    Q, B = load(a.qwen), load(a.bge)
    F = {x["i"]: x.get("cur") for x in json.load(open(a.first, encoding="utf-8"))} if a.first else {}
    db = sqlite3.connect(a.out)
    db.execute("CREATE TABLE IF NOT EXISTS curated (release TEXT NOT NULL, key TEXT NOT NULL, body BLOB NOT NULL, "
               "at REAL NOT NULL, PRIMARY KEY (release, key)) WITHOUT ROWID")
    # an empty rows table too: the inbox reads a file's rows before its curated rows, and today's engines refuse a
    # file without one (2026-11-06, indexhost: "no such table: rows")
    db.execute("CREATE TABLE IF NOT EXISTS rows (fp TEXT NOT NULL, kind TEXT NOT NULL, key TEXT NOT NULL, "
               "body BLOB NOT NULL, at REAL NOT NULL, PRIMARY KEY (fp, kind, key)) WITHOUT ROWID")
    now, lists, n, rows = time.time(), {}, 0, 0
    W = {o["i"]: o["rows"] for o in map(json.loads, open(a.whole, encoding="utf-8"))} if a.whole else {}
    for x in C:
        out = build_whole(x, Q, W[x["i"]], a) if x["i"] in W else build(x, Q, B, F.get(x["i"]), a)
        if not out:
            continue
        g = G.get(str(x["i"]))
        if g:
            kept = [r for r in out if g.get(r["url"]) != "wrong"]
            dropped += len(out) - len(kept)
            if not kept or (all(r["url"] in g for r in out) and not any(g.get(r["url"]) in ("exact", "good") for r in kept)):
                graded_out += 1
                db.execute("INSERT OR REPLACE INTO curated VALUES (?, ?, ?, ?)",
                           (a.release, key(x["q"]), zlib.compress(json.dumps({"rows": [], "graded_out": True}).encode(), 6), now))
                continue
            out = kept
        if key(x["q"]) in P:                 # removed on review: an empty list, newer than any list of it an engine holds
            db.execute("INSERT OR REPLACE INTO curated VALUES (?, ?, ?, ?)",
                       (a.release, key(x["q"]), zlib.compress(json.dumps({"rows": [], "pulled": True}).encode(), 6), now))
            continue
        body = {"rows": out, "model": ("qwen3-14b-listwise+" if x["i"] in W else "") + "qwen3-reranker-4b" + ("+bge-reranker-v2-m3" if B else ""), "tq": a.tq, "tb": a.tb}
        db.execute("INSERT OR REPLACE INTO curated VALUES (?, ?, ?, ?)",
                   (a.release, key(x["q"]), zlib.compress(json.dumps(body, ensure_ascii=False).encode(), 6), now))
        lists[x["i"]] = {"q": x["q"], "src": x.get("src"), "rows": [r["url"] for r in out]}
        n += 1; rows += len(out)
    db.commit(); db.close()
    if a.lists:
        json.dump(lists, open(a.lists, "w", encoding="utf-8"), ensure_ascii=False)
    if a.grades:
        print(f"grades: {graded_out} lists emptied (no row graded exact or good), {dropped} rows graded wrong dropped", file=sys.stderr)
    print(f"{n} curated lists, {rows / max(n, 1):.2f} rows each, release {a.release}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
