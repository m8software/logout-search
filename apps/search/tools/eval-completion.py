#!/usr/bin/env python3
"""Score as-you-type completion against labelled real queries (step 0).

    tools/eval-completion.py LABELS.tsv [LABELS.tsv …] [--suggester baseline|api] [--details OUT.tsv]

Each site / wiki label (query <TAB> category <TAB> expected, the eval-queries.py format)
becomes a keystroke test: the query's first 3, 4, 5 … characters are asked of the
suggester, and we record whether the expected answer is in the top 3, and after how
many characters it is first.

Suggesters
  baseline  (--release DIR: from a release folder, no database and nothing leaves this machine;
            without it the LEGACY Postgres path over ssh, deleted at phase 7 of SEARCH-POSTGRES-EXIT.md)
            the instant-search mockup's quick lookup (2026-09-24): serve_sites whose
            domain starts with the prefix (spaces removed), Tranco <= 50k, best 3 by
            rank, plus the biggest 1M+ YouTube channel whose name starts with it.
            Every later step has to beat this.
  api       search-api's /instant endpoint: its "suggest" list, in order.
  tune      fetch every candidate once (/instant?all=1: kind, popularity, history,
            typed-in-full) and re-rank offline with search_api.rank_suggestions over a
            grid of kind weights — prints the default and the best weighting. 75 labels
            is a small set: prefer a round, plausible point near the best over the exact
            argmax, and put it in SEARCH_SUGGEST_WEIGHTS / SUGGEST_WEIGHTS.

Privacy: the label file holds real searches — keep it and --details OUT of git. The
prefixes travel on stdin (never a command line) to the serving box only.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import statistics
import subprocess
import sys
from urllib.parse import unquote, urlsplit

LENGTHS = (3, 4, 5, 6)
SHOW_MISSES = False
TOP = 3


def norm(q: str) -> str:
    """How the start page will send it: lowercase, spaces collapsed."""
    return " ".join(q.lower().split())


def prefixes(q: str, start: int = 3) -> list[str]:
    """"costco" -> ["cos", "cost", "costc", "costco"]; trailing-space prefixes are skipped
    (the box would send the same text as the prefix before)."""
    q = norm(q)
    return [q[:n] for n in range(start, len(q) + 1) if not q[:n].endswith(" ")]


WEIGHTS: dict[tuple[str, str], float] = {}


def load_labels(paths: list[str]) -> list[tuple[str, str, list[str]]]:
    """Unique (query, category, expected answers) for site and wiki labels, first file wins.
    An optional 4th column is the label's weight — the Jev-labelled set carries how
    sure Jev was, so a 0.4 label counts 0.4 in the rates, not a full vote (no threshold)."""
    seen: dict[tuple[str, str], list[str]] = {}
    for path in paths:
        for line in open(path, encoding="utf-8"):
            p = line.rstrip("\n").split("\t")
            if len(p) < 3 or p[1] not in ("site", "wiki") or p[2] in ("", "-"):
                continue
            key = (norm(p[0]), p[1])
            if len(key[0]) >= 3 and key not in seen:
                seen[key] = p[2].split("|")
                try:
                    WEIGHTS[key] = float(p[3]) if len(p) > 3 else 1.0
                except ValueError:
                    WEIGHTS[key] = 1.0
    return [(q, c, e) for (q, c), e in seen.items()]


def domain_of(url: str) -> str:
    h = (urlsplit(url).hostname or "").lower()
    return h[4:] if h.startswith("www.") else h


def hit(cat: str, expected: list[str], s: dict) -> bool:
    """A suggestion answers a site label when its host is (under) the expected domain,
    and a wiki label when it is that article — or carries it as its `wiki` title."""
    if cat == "site":
        h = domain_of(s.get("url", ""))
        return any(h == d or h.endswith("." + d) for d in expected)
    title = s.get("wiki") or ""
    if not title and "/wiki/" in s.get("url", "") and "wikipedia.org" in s.get("url", ""):
        title = unquote(s["url"].rsplit("/wiki/", 1)[-1]).replace("_", " ")
    return any(title.lower() == e.lower() for e in expected)


def score_query(cat: str, expected: list[str], q: str, sugg: dict[str, list[dict]]) -> dict:
    """Per query: top-3 hit at each of LENGTHS, and the first prefix length at which the
    answer is first / in the top 3 (None = never, even with the whole query typed)."""
    out = {"in3": {}, "first": None, "top3": None}
    for p in prefixes(q):
        got = sugg.get(p, [])
        n = len(p)
        in3 = any(hit(cat, expected, s) for s in got[:TOP])
        if n in LENGTHS:
            out["in3"][n] = in3
        if in3 and out["top3"] is None:
            out["top3"] = n
        if got and hit(cat, expected, got[0]) and out["first"] is None:
            out["first"] = n
    for n in LENGTHS:                                   # a query shorter than n: its full text counts
        if n not in out["in3"] and len(q) < n:
            out["in3"][n] = out["in3"].get(len(q), bool(out["top3"]))
    return out


def summarize(rows: list[tuple[str, str, list[str], dict]]) -> list[str]:
    lines = []
    for cat in ("site", "wiki", "all"):
        sel = [r for r in rows if cat == "all" or r[1] == cat]
        if not sel:
            continue
        n = len(sel)
        wsum = sum(WEIGHTS.get((r[0], r[1]), 1.0) for r in sel) or 1.0
        cells = []
        for L in LENGTHS:
            k = sum(1 for r in sel if r[3]["in3"].get(L))
            wk = sum(WEIGHTS.get((r[0], r[1]), 1.0) for r in sel if r[3]["in3"].get(L))
            cells.append(f"{k:>3}/{n} ({100 * wk / wsum:3.0f}%)")
        firsts = [r[3]["first"] for r in sel if r[3]["first"] is not None]
        never = n - len(firsts)
        med = f"{statistics.median(firsts):.0f}" if firsts else "-"
        lines.append(f"{cat:<5} {n:>4}  " + "  ".join(cells) + f"   {med:>6}   {never:>3}/{n}")
    return lines


BASELINE_SQL = r"""
CREATE TEMP TABLE p (q text);
COPY p FROM STDIN;
{prefixes}
\.
SELECT json_object_agg(p.q, coalesce(s.rows, '[]'::json)) FROM p
LEFT JOIN LATERAL (
  SELECT json_agg(x ORDER BY x.o) AS rows FROM (
    (SELECT 0 AS o, 'https://www.youtube.com/@' || coalesce(handle, channel_id) AS url, name AS title, 'youtube' AS source
       FROM youtube_channels WHERE key LIKE replace(p.q, ' ', '') || '%' AND followers >= 1000000
      ORDER BY followers DESC LIMIT 1)
    UNION ALL
    (SELECT 1 + row_number() OVER (ORDER BY s.tranco_rank), d.url, d.title, 'site'
       FROM serve_sites s JOIN docs d USING (domain)
      WHERE d.error IS NULL AND s.domain LIKE replace(p.q, ' ', '') || '%' AND s.tranco_rank <= 50000
      ORDER BY s.tranco_rank LIMIT 3)) x) s ON true;
"""


def run_psql(jump: str, target: str, sql: str) -> str:
    # The SQL (with the prefixes) goes on stdin: nothing real-query-shaped on a command line.
    cmd = ["ssh", "-o", "BatchMode=yes", jump,
           f"ssh -o BatchMode=yes {target} 'sudo -n docker exec -i search-postgres psql -U search -d serve -Atq'"]
    return subprocess.run(cmd, input=sql, capture_output=True, text=True, check=True).stdout


def suggest_baseline_release(all_prefixes: list[str], release: str) -> dict[str, list[dict]]:
    """The same lookup over a release folder (names + sites + signals.bin), locally."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import release_src
    return release_src.open_release(release).suggest_baseline(all_prefixes)


def suggest_baseline(all_prefixes: list[str], jump: str, target: str) -> dict[str, list[dict]]:
    print("eval-completion: baseline over LEGACY Postgres (pass --release DIR); removed at phase 7", file=sys.stderr)
    body = "\n".join(p.replace("\\", "\\\\").replace("\t", " ") for p in all_prefixes)
    out = run_psql(jump, target, BASELINE_SQL.replace("{prefixes}", body))
    return json.loads(out[out.index("{"):])            # psql wraps long JSON over lines


REMOTE = r'''
import json, sys, urllib.parse, urllib.request
req = json.load(sys.stdin)
out = {}
for q in req["prefixes"]:
    try:
        d = json.load(urllib.request.urlopen(req["api"] + "/instant?results=0&q=" + urllib.parse.quote(q) + req["extra"], timeout=10))
        out[q] = d.get(req["field"], [])
    except Exception as e:
        out[q] = []
json.dump(out, sys.stdout)
'''


def suggest_api(all_prefixes: list[str], host: str, api: str, candidates: bool = False) -> dict[str, list[dict]]:
    b64 = base64.b64encode(REMOTE.encode()).decode()
    cmd = ["ssh", "-o", "BatchMode=yes", host,
           "docker exec -i searxng-a /usr/local/searxng/.venv/bin/python -c "
           f"\"import base64; exec(base64.b64decode('{b64}'))\""]
    if host == "direct":            # --direct: --api is reachable from here (a tunnel to a throwaway engine); same code, run locally
        cmd = [sys.executable, "-c", REMOTE]
    req = {"api": api, "prefixes": all_prefixes, "extra": "&all=1" if candidates else "",
           "field": "candidates" if candidates else "suggest"}
    r = subprocess.run(cmd, input=json.dumps(req),
                       capture_output=True, text=True, check=True)
    return json.loads(r.stdout)


def objective(rows) -> tuple:
    """Top-3 hits summed over 3/4/5 characters, then answers that ever reach first place."""
    return (sum(sum(1 for n in LENGTHS if r[3]["in3"].get(n)) for r in rows),
            sum(1 for r in rows if r[3]["first"] is not None))


def tune(labels, cands: dict[str, list[dict]]) -> None:
    import itertools
    import os
    import sys
    import types
    sys.modules.setdefault("psycopg", types.ModuleType("psycopg"))
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "serve", "api"))
    import search_api as sa

    v2 = any("demand" in c for cs in cands.values() for c in cs)

    def row(c):
        r = (c["id"], c["source"], c["title"], c["url"], c.get("domain"), c.get("wiki"), c["pop"], c["hist"])
        return r + (c.get("demand") or 0.0, c.get("descr"), c.get("pos") or 0) if v2 else r

    def run(w):
        sugg = {}
        for p, cs in cands.items():
            sugg[p] = sa.rank_suggestions([row(c) for c in cs], {c["id"] for c in cs if c["exact"]}, w=w)
        return [(q, c, e, score_query(c, e, q, sugg)) for q, c, e in labels]

    results = []
    if v2:          # demand-ranked candidates: log10(searches) + these nudges
        base = dict(sa.SUGGEST_WEIGHTS2)
        grid = {"k_site": (0.3, 0.8, 1.3, 1.8, 2.3), "k_youtube": (-0.5, 0.0), "exact": (0.3, 0.8),
                "later": (0.5, 1.0, 1.5), "alias": (0.5, 1.0), "foreign": (0.5, 1.0)}
    else:
        base = dict(sa.SUGGEST_WEIGHTS)
        grid = {"wiki": (0.2, 0.3, 0.4), "youtube": (0.6, 0.85), "hist": (0.5,),
                "exact": (0.2, 0.35), "exact_wiki": (0.0, 0.1), "foreign": (0.0, 0.1, 0.2, 0.3)}
    for combo in itertools.product(*grid.values()):
        w = dict(base, **dict(zip(grid, combo)))
        results.append((objective(run(w)), combo, w))
    results.sort(key=lambda x: x[0], reverse=True)
    print("default weights:", {k: base[k] for k in grid}, "->", objective(run(base)))
    print("best 8 of", len(results), "(top-3 hits over " + "/".join(map(str, LENGTHS)) + " chars, answers ever first):")
    for obj, combo, w in results[:8]:
        print("  ", obj, dict(zip(grid, combo)))
    print("\nthe best weighting in the usual table:")
    for line in summarize(run(results[0][2])):
        print(line)
    if SHOW_MISSES:
        w = results[0][2]
        print("\nsite misses at 4 characters (what took the top 3):")
        for q, c, e in labels:
            p = prefixes(q)[1] if len(prefixes(q)) > 1 else prefixes(q)[0]
            cs = cands.get(p, [])
            top = sa.rank_suggestions([row(x) for x in cs], {x["id"] for x in cs if x["exact"]}, w=w)
            if c == "site" and not any(hit(c, e, t) for t in top[:TOP]):
                have = [x for x in cs if hit(c, e, {"url": x["url"], "wiki": x.get("wiki")})]
                print(f"  {p!r:8} want {e[0]:<22} {'(candidate, pop %.2f)' % have[0]['pop'] if have else '(not a candidate)':<24} "
                      + " | ".join(f"{t['title'][:20]}({t['source'][0]} {t['score']:.2f})" for t in top[:3]))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("labels", nargs="+")
    ap.add_argument("--suggester", choices=("baseline", "api", "tune"), default="baseline")
    ap.add_argument("--host", default="user@jump-host", help="SearXNG host (api) / jump host (baseline)")
    ap.add_argument("--target", default="user@index-host", help="serving box behind the jump (baseline)")
    ap.add_argument("--api", default="http://index-host:8090")
    ap.add_argument("--release", default=os.environ.get("SEARCH_RELEASE_DIR", ""),
                    help="baseline: read this release folder instead of Postgres (default $SEARCH_RELEASE_DIR)")
    ap.add_argument("--direct", action="store_true", help="api / tune: ask --api from this machine (a tunnel to a throwaway engine), "
                                                          "not from inside the SearXNG network over ssh")
    ap.add_argument("--details", help="per-query TSV (real queries: keep out of git)")
    ap.add_argument("--cache", help="tune: candidates JSON, fetched once and reused — holds the prefixes of every label file given, real ones included: keep it local, out of git")
    ap.add_argument("--show-misses", action="store_true", help="tune: list site misses at 4 chars (real queries — terminal only)")
    a = ap.parse_args()

    if a.direct:
        a.host = "direct"
    labels = load_labels(a.labels)
    allp = sorted({p for q, _, _ in labels for p in prefixes(q)})
    global SHOW_MISSES
    SHOW_MISSES = a.show_misses
    if a.suggester == "tune":
        cands = None
        if a.cache and os.path.exists(a.cache):
            cands = json.load(open(a.cache))
        if cands is None or any(p not in cands for p in allp):
            cands = suggest_api(allp, a.host, a.api, candidates=True)
            if a.cache:
                json.dump(cands, open(a.cache, "w"))
        tune(labels, cands)
        return
    sugg = ((suggest_baseline_release(allp, a.release) if a.release else suggest_baseline(allp, a.host, a.target))
            if a.suggester == "baseline"
            else suggest_api(allp, a.host, a.api))
    rows = [(q, c, e, score_query(c, e, q, sugg)) for q, c, e in labels]

    print(f"suggester: {a.suggester} | {len(labels)} labelled queries, {len(allp)} prefixes asked\n")
    print("Is the answer in the top 3 after typing N characters? (count of labelled queries, % of that row)")
    print("'first at' = median characters typed before the answer is the FIRST suggestion;")
    print("'never' = queries whose answer is never first, even with the whole query typed.\n")
    print(f"{'kind':<5} {'n':>4}  " + "  ".join(f"{str(n) + ' chars':>13}" for n in LENGTHS) + "   first at   never")
    for line in summarize(rows):
        print(line)
    if a.details:
        with open(a.details, "w", encoding="utf-8") as f:
            f.write("query\tkind\texpected\t" + "\t".join(f"in3@{n}" for n in LENGTHS) + "\tfirst_at\ttop3_at\n")
            for q, c, e, r in rows:
                f.write("\t".join([q, c, "|".join(e)] + [str(int(bool(r["in3"].get(n)))) for n in LENGTHS]
                                  + [str(r["first"] or "-"), str(r["top3"] or "-")]) + "\n")


if __name__ == "__main__":
    main()
