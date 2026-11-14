#!/usr/bin/env python3
"""The candidate dump: for every dev and test query of every open set, each list the engine's fusion
blended, the fused top-20 and every reordering after it, with scores — one TSV per set, built once per engine build.

    tools/sq-candidates.py [--engine URL] [--build ID] [--sets a,b] [--split dev|test|both] [--rows 20] [--text]

Asks the engine's /candidates (serve/api/sq_candidates.py: the same search with the trace on; ranking untouched).
Writes ~/.local/share/logout-search/sq/candidates/<build>/<set>.jsonl (the raw answers, one per query — a re-run
asks only queries not in it) and <set>.tsv:

    query  split  category  expected  stratum  step  rank  url  title  source  score  buildbox  ce  hit

step is list:<name> (wiki, sites, anchors, pages, sem, kind, described, ...: the fusion's inputs, in order), named
(the exact tiers above the fusion), fused (the RRF of the lists), merged (the whole pool, one order), ranker, ce
(the two reorderings) and final (what /search answers). hit = 1 when the row is the set's expected answer by
eval-queries.py's rule, or one of its accepted alternatives (SQ_KEYS=primary for the expected answer alone) (a place is matched by its OSM id here: the trace carries no coordinates).
--text asks /candidates?text=1 and appends three columns, title_full, content and ce_text: the title uncut (title
stays cut at 90 characters), the result's whole text, and the one string the cross-encoder is handed for that row
at serving (tab, CR and LF become a space in the TSV; the .jsonl keeps them). Give a text dump its own --out: in a
directory holding a dump made without --text every query is asked again and appended. Without --text nothing changes.
Read by the recall audit (which list finds the answer), hard negatives and the precompute.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sq_common as C  # noqa: E402

eq = C.load_tool("eval_queries", C.TOOLS / "eval-queries.py")
TSV_HEADER = "query\tsplit\tcategory\texpected\tstratum\tstep\trank\turl\ttitle\tsource\tscore\tlr\tce\thit"
TEXT_COLS = ("title_full", "content", "ce_text")          # --text: appended after hit


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def ask(api: str, q: str, rows: int, text: bool = False) -> dict:
    try:
        with urllib.request.urlopen(api + f"/candidates?rows={rows}{'&text=1' if text else ''}&q=" + urllib.parse.quote(q), timeout=180) as r:
            d = json.load(r)
        d["q"] = q
        if text and not d.get("text"):                 # an engine whose code has no text mode ignores text=1
            return {"q": q, "error": "NoTextMode"}
        return d
    except Exception as e:
        return {"q": q, "error": type(e).__name__}


def steps_of(d: dict) -> list[tuple[str, list[dict]]]:
    out = [("list:" + lst.get("name", ""), lst.get("rows", [])) for lst in d.get("lists", [])]
    for step in ("named", "fused", "merged", "ranker", "ce", "fuzzy", "final"):
        if d.get(step):
            out.append((step, d[step].get("rows", [])))
    return out


def flat(s) -> str:
    """A text field as one TSV cell: tab, CR and LF each become a space (the .jsonl keeps the field as answered)."""
    return str(s or "").replace("\t", " ").replace("\r", " ").replace("\n", " ")


def tsv_rows(r: dict, d: dict, text: bool = False) -> list[str]:
    """One TSV line per (step, rank) of one query's answer; with text, the three text columns after hit. Pure, for tests."""
    out = []
    for step, rows in steps_of(d):
        for i, x in enumerate(rows):
            hit = int(C.row_hit(eq, r, x))
            out.append("\t".join(map(str, (r["query"], r.get("split", ""), r["category"], r["expected"], r.get("stratum", ""),
                                           step, i + 1, x.get("url", ""), (x.get("title") or "").replace("\t", " "),
                                           x.get("source", ""), x.get("score", ""), x.get("lr", ""), x.get("ce", ""), hit)
                                      + (tuple(flat(x.get(c)) for c in TEXT_COLS) if text else ()))))
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--engine", default="http://127.0.0.1:18484")
    ap.add_argument("--build", help="engine build id (default: sq-engine.sh's sidecar for the engine's port)")
    ap.add_argument("--sets", default="", help="comma-separated subset (default: every open set)")
    ap.add_argument("--split", default="both", choices=("dev", "test", "both"))
    ap.add_argument("--rows", type=int, default=20, help="results kept per step (the fused top-20)")
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--limit", type=int, default=0, help="first N queries per set (smoke tests)")
    ap.add_argument("--text", action="store_true", help="also each row's full title, content and cross-encoder input (/candidates?text=1)")
    ap.add_argument("--out", help="output root (default sq/candidates/<build>; with --text, sq/candidates/<build>-text)")
    a = ap.parse_args()
    api = a.engine.rstrip("/")
    build = C.engine_build(api, a.build)
    if not build:
        sys.exit(f"sq-candidates: no build id for {api}: start it with sq-engine.sh up, or pass --build ID")
    out = Path(a.out) if a.out else C.SQ_DIR / "candidates" / (build + "-text" if a.text else build)
    out.mkdir(parents=True, exist_ok=True)
    sets = [s.strip() for s in a.sets.split(",") if s.strip()] or C.all_sets()
    t0 = time.time()
    for s in sets:
        rows = C.load_set(s, None if a.split == "both" else a.split, a.limit)
        raw = out / f"{s}.jsonl"
        have = {}
        if raw.exists():
            for line in open(raw, encoding="utf-8"):
                if line.strip():
                    o = json.loads(line)
                    if "error" not in o and (not a.text or o.get("text")):      # a dump made without --text has no text to give
                        have[o["q"]] = o
        todo = [q for q in dict.fromkeys(r["query"] for r in rows) if q not in have]
        if todo:
            log(f"  {s}: asking {len(todo)} queries ({len(rows) - len(todo)} already dumped)")
            with ThreadPoolExecutor(max(1, a.workers)) as ex, open(raw, "a", encoding="utf-8") as f:
                for d in ex.map(lambda q: ask(api, q, a.rows, a.text), todo):
                    if d.get("error") == "NoTextMode":
                        sys.exit(f"sq-candidates: the engine at {api} has no text mode: start it with API code that has /candidates?text=1")
                    if "error" in d:
                        log(f"  {s}: {d['q'][:40]!r} failed: {d['error']}")
                        continue
                    f.write(json.dumps(d) + "\n")
                    have[d["q"]] = d
        n = 0
        with open(out / f"{s}.tsv", "w", encoding="utf-8") as f:
            f.write(TSV_HEADER + ("\t" + "\t".join(TEXT_COLS) if a.text else "") + "\n")
            for r in rows:
                d = have.get(r["query"])
                if d:
                    lines = tsv_rows(r, d, a.text)
                    f.write("".join(line + "\n" for line in lines))
                    n += len(lines)
        log(f"  {s}: {len(rows)} queries, {n} candidate rows -> {out / (s + '.tsv')}")
    log(f"done in {time.time() - t0:.0f}s: {out}")
    print(out)


if __name__ == "__main__":
    main()
