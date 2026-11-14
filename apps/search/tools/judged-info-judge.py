#!/usr/bin/env python3
"""Judge the engine's top-5 on the judged informational set once with Clef 27B and freeze the verdicts.

    tools/judged-info-judge.py [--engine http://127.0.0.1:18484] [--build ID] [--top 5] [--dry-run]
                               [--paid-cap-neurons N] [--verdicts PATH] [--yardstick-out PATH] [--report PATH]

For every query of evalsets/open/judged-info.tsv: ask the engine (/search?k=5; answers cached by engine build under
~/.local/share/logout-search/sq/cache/<build>/judged-info.jsonl, the build from sq-engine.sh's sidecar for the port),
then judge each (query, result) pair with search-ranker's RELEVANT question through utility/lib/clef.py — the
shared ledger, the content cache (~/.cache/logout/clef-judged-info.clef.json: a re-run is free), parallel 4 so a
budget stop overshoots by at most 4 calls. What Clef sees is search-answer-eval.state_of: query, title, URL and
the result's kind. NEVER a snippet or page text (ADR-059). Clef 27B is the judge and labels no training data
(the judge/labeller rule, evalsets/open/README.md).

--dry-run asks the engine, counts the pairs not yet in the cache and prints the neurons they would cost against the
ledger, and calls Clef for nothing. --paid-cap-neurons N is the named run's cap (the owner's go-ahead is in the
issue); without it the day's free stop applies.

Writes judged-info.verdicts.tsv (query key, template, split, rank, result id, url, kind, score, model, judged date,
build — no title, no text; the result id is sha256(url)[:16]), a per-template table (--report, markdown) with
P(top-1 right) = the mean Clef probability of the first result, top-1 >= 0.7, any of the top 5 >= 0.7, and
--yardstick-out: the top-5 lists in utility/ce-train/yardstick_eval.py's results shape (local file, not committed:
it carries the engine's content field). Nothing here runs on a box.
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import importlib.util
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
REPO = TOOLS.parents[2]
OPEN = TOOLS / "evalsets" / "open"
SQ_DIR = Path(os.environ.get("SQ_DIR", Path.home() / ".local/share/logout-search/sq"))
SET = "judged-info"
KEEP = ("url", "title", "source", "score", "lr", "ce", "content")
NEURONS_PER_CALL = 5.9                       # measured: 659 answer-eval calls = 3,865 neurons (2026-10-12)
RIGHT = 0.7                                  # search-answer-eval's "the top result is right" threshold
VERDICT_COLS = ("key", "template", "split", "rank", "result_id", "url", "kind", "score", "model", "judged", "build")

sys.path.insert(0, str(REPO / "utility" / "lib"))
import clef  # noqa: E402


def log(msg: str) -> None:
    print(f"[judged-info] {msg}", file=sys.stderr, flush=True)


def load_tool(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


def norm_query(q: str) -> str:
    """sq_common.norm_query: lower-case, single-spaced — the exclusion list's form=query key."""
    return " ".join(q.lower().split())


def load_set(path: Path) -> list[dict]:
    rows = []
    with open(path, encoding="utf-8") as f:
        cols = f.readline().rstrip("\n").split("\t")
        if cols[:4] != ["query", "category", "expected", "set"]:
            sys.exit(f"{path}: not an eval set (header {cols[:4]})")
        for line in f:
            if line.strip():
                r = dict(zip(cols, line.rstrip("\n").split("\t")))
                r["template"] = next((s[9:] for s in r.get("stratum", "").split(",") if s.startswith("template:")), "")
                rows.append(r)
    return rows


def engine_build(url: str, override: str | None) -> str | None:
    """The build hash results are cached under: --build, else sq-engine.sh's sidecar for the engine's port."""
    if override:
        return override
    port = urllib.parse.urlsplit(url).port
    p = SQ_DIR / "engines" / f"{port}.json"
    if port and p.exists():
        try:
            return json.loads(p.read_text())["build"]
        except (json.JSONDecodeError, KeyError):
            return None
    return None


def ask_one(api: str, q: str, k: int) -> dict:
    try:
        with urllib.request.urlopen(api + f"/search?k={k}&q=" + urllib.parse.quote(q), timeout=120) as r:
            d = json.load(r)
    except Exception as e:
        return {"q": q, "error": type(e).__name__}
    res = [{c: x.get(c) for c in KEEP if x.get(c) is not None} for x in d.get("results", [])[:k]]
    return {"q": q, "results": res, "confident": bool(d.get("confident")), "intent": d.get("intent", "")}


def answers(api: str, build: str, queries: list[str], k: int, workers: int) -> dict:
    """{query: answer} from the build's cache, asking the engine only for what is not there."""
    path = SQ_DIR / "cache" / build / f"{SET}.jsonl"
    have: dict = {}
    if path.exists():
        for line in open(path, encoding="utf-8"):
            if line.strip():
                o = json.loads(line)
                if "error" not in o:
                    have[o["q"]] = o
    todo = [q for q in dict.fromkeys(queries) if q not in have]
    if todo:
        log(f"asking the engine {len(todo)} queries ({len(queries) - len(todo)} cached under build {build})")
        with ThreadPoolExecutor(max(1, workers)) as ex:
            got = list(ex.map(lambda q: ask_one(api, q, k), todo))
        errs = [o for o in got if "error" in o]
        if errs:
            log(f"{len(errs)} queries failed ({errs[0]['error']}): not cached, no verdicts for them")
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            for o in got:
                if "error" not in o:
                    f.write(json.dumps(o) + "\n")
                    have[o["q"]] = o
    return have


def result_id(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]


def summarise(rows: list[dict], verdict: dict, got: dict, top: int) -> list[dict]:
    """Per template (and all): n, with results, P(top-1 right) mean, top-1 >= 0.7, any top-5 >= 0.7, mean max."""
    per: dict[str, list] = collections.defaultdict(list)
    total: collections.Counter = collections.Counter()
    for r in rows:
        o = got.get(r["query"])
        if not o:
            continue
        scores = [verdict.get((r["query"], x.get("url", "")), None) for x in o["results"][:top]]
        for key in (r["template"], "all"):
            total[key] += 1
        if any(s is None for s in scores):
            continue                                        # a query with an unjudged result is not scored at all
        for key in (r["template"], "all"):
            per[key].append(scores)
    out = []
    for key in sorted(total, key=lambda k: (k == "all", k)):
        lists = per[key]
        with_res = [s for s in lists if s]
        n, m = len(lists), len(with_res)
        out.append({"template": key, "in_set": total[key], "n": n, "with_results": m,
                    "p_top1": sum(s[0] for s in with_res) / n if n else 0.0,
                    "top1_right": sum(s[0] >= RIGHT for s in with_res) / n if n else 0.0,
                    "any5_right": sum(max(s) >= RIGHT for s in with_res) / n if n else 0.0,
                    "mean_max": sum(max(s) for s in with_res) / n if n else 0.0,
                    "pairs": sum(len(s) for s in lists)})
    return out


def render(summary: list[dict], build: str, when: str, usage_line: str, ledger_line: str, engine: str) -> str:
    lines = [f"## judged-info — build `{build}`, judged {when}, Clef 27B (`@cf/cloudflare/clef`), top-5 titles+URLs only",
             "", f"Engine: {engine}. Judge spend: {usage_line}. Ledger: {ledger_line}.", "",
             "| template | in set | n judged | with results | P(top-1 right) | top-1 ≥ 0.7 | any of top-5 ≥ 0.7 | mean max | pairs judged |",
             "|---|---|---|---|---|---|---|---|---|"]
    for s in summary:
        if not s["n"]:
            lines.append(f"| {s['template']} | {s['in_set']} | 0 | — | — | — | — | — | 0 |")
            continue
        lines.append(f"| {s['template']} | {s['in_set']} | {s['n']} | {s['with_results']} | {s['p_top1']:.3f} | {100 * s['top1_right']:.1f}% "
                     f"| {100 * s['any5_right']:.1f}% | {s['mean_max']:.3f} | {s['pairs']} |")
    lines += ["", "n judged = queries whose every top-5 result has a verdict; a query with an unjudged result is left out, "
              "so a row with n judged below in set is PARTIAL and `all` is not the whole set's number.",
              "", "P(top-1 right) is the mean Clef probability of the first result (0 when the list is empty); "
              "the two percentage columns count a query when a result scores at least 0.7 (search-answer-eval's threshold)."]
    return "\n".join(lines) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--engine", default="http://127.0.0.1:18484")
    ap.add_argument("--build", help="engine build id (default: sq-engine.sh's sidecar for the engine's port)")
    ap.add_argument("--set", default=str(OPEN / f"{SET}.tsv"))
    ap.add_argument("--top", type=int, default=5)
    ap.add_argument("--workers", type=int, default=3, help="engine queries in flight")
    ap.add_argument("--parallel", type=int, default=4, help="Clef requests in flight (the budget overshoot bound)")
    ap.add_argument("--paid-cap-neurons", type=float, help="the named run's cap on the day's total (owner's go-ahead in the issue)")
    ap.add_argument("--dry-run", action="store_true", help="count the pairs to judge and their cost; call Clef for nothing")
    ap.add_argument("--cached-only", action="store_true",
                    help="write the verdicts and the table from what the cache already holds; call Clef for nothing "
                         "(after a budget stop or a refusal: queries with an unjudged result are left out of the table)")
    ap.add_argument("--verdicts", default=str(OPEN / f"{SET}.verdicts.tsv"))
    ap.add_argument("--report", default=str(OPEN / f"{SET}.report.md"), help="the per-template table; '-' for stdout only")
    ap.add_argument("--yardstick-out", help="write the top-5 lists for yardstick_eval.py --info judged-info (local file)")
    ap.add_argument("--limit", type=int, default=0, help="first N queries (smoke test)")
    a = ap.parse_args()
    api = a.engine.rstrip("/")
    build = engine_build(api, a.build)
    if not build:
        sys.exit(f"judged-info-judge: no build id for {api}: start it with sq-engine.sh up, or pass --build ID")
    rows = load_set(Path(a.set))
    if a.limit:
        rows = rows[: a.limit]
    got = answers(api, build, [r["query"] for r in rows], a.top, a.workers)
    pairs = [(r["query"], pos, x) for r in rows for pos, x in enumerate((got.get(r["query"]) or {"results": []})["results"][: a.top])]
    log(f"{len(rows)} queries, {sum(1 for r in rows if (got.get(r['query']) or {}).get('results'))} with results, {len(pairs)} pairs")

    sae = load_tool("search_answer_eval", REPO / "utility" / "search-answer-eval.py")     # state_of, sr.RELEVANT
    ledger = clef.Ledger(paid_cap=a.paid_cap_neurons)
    cache = clef.Cache(SET)
    todo = sum(1 for p in pairs if cache.get(clef.Cache.key(sae.state_of(p), sae.sr.RELEVANT)) is None)
    est = todo * NEURONS_PER_CALL
    log(f"{len(pairs) - todo} pairs already judged (cache {cache.path}); {todo} to send ≈ {est:,.0f} neurons; "
        f"ledger: {ledger.line()}")
    if a.dry_run:
        print(f"dry run: {todo} calls ≈ {est:,.0f} neurons (${est * clef.USD_PER_NEURON:.2f} if all paid); "
              f"{'fits' if est <= ledger.left() else 'DOES NOT FIT'} the {ledger.left():,.0f} left under the day's stop")
        return
    if est > ledger.left() + a.parallel * NEURONS_PER_CALL:
        log(f"WARNING: the estimate exceeds what is left; judge() will stop at the day's stop and exit 2 — a rerun after "
            f"00:00 UTC (or a higher cap with a go-ahead) finishes from the cache")
    if a.cached_only:
        ans = [cache.get(clef.Cache.key(sae.state_of(p), sae.sr.RELEVANT)) for p in pairs]
        usage = clef.Usage()
        usage.cached = sum(1 for x in ans if x is not None)
        log(f"--cached-only: {usage.cached} of {len(pairs)} pairs have a verdict; the other {len(pairs) - usage.cached} are left out")
    else:
        ans, usage = clef.judge(pairs, sae.state_of, sae.sr.RELEVANT, SET, parallel=a.parallel, progress=True, ledger=ledger)
    when = time.strftime("%Y-%m-%d", time.gmtime())              # the UTC day, the ledger's
    verdict: dict = {}
    model = f"@cf/cloudflare/{clef.MODEL}"
    by_q = {r["query"]: r for r in rows}
    out_rows = []
    for (q, pos, x), an in zip(pairs, ans):
        if an is None:
            continue
        s = clef.noul(an, "answers")
        url = x.get("url", "")
        verdict[(q, url)] = s
        r = by_q[q]
        out_rows.append((norm_query(q), r["template"], r.get("split", ""), str(pos + 1), result_id(url), url,
                         x.get("source", ""), f"{s:.4f}", model, when, build))
    Path(a.verdicts).parent.mkdir(parents=True, exist_ok=True)
    with open(a.verdicts, "w", encoding="utf-8") as f:
        f.write("# Clef 27B verdicts on the engine's top-5 for evalsets/open/judged-info.tsv: query key, result id\n"
                "# (sha256 of the url, 16 hex), url, kind and P(this result is what the searcher wanted). No title, no page\n"
                "# text. Frozen: a result is re-judged only when it changes (the judge's content cache keys on query+title+url).\n")
        f.write("\t".join(VERDICT_COLS) + "\n")
        for row in sorted(out_rows):
            f.write("\t".join(row) + "\n")
    summary = summarise(rows, verdict, got, a.top)
    spend = (f"{usage.cached} of {len(pairs)} pairs from the cache, no request sent" if a.cached_only else usage.line(len(pairs)))
    md = render(summary, build, when, spend, ledger.line(), api)
    print(md, end="")
    if a.report != "-":
        Path(a.report).write_text(md, encoding="utf-8")
        log(f"report -> {a.report}")
    log(f"{len(out_rows)} verdicts -> {a.verdicts}")
    if a.yardstick_out:
        with open(a.yardstick_out, "w", encoding="utf-8") as f:
            for r in rows:
                o = got.get(r["query"])
                if o:
                    f.write(json.dumps({"q": r["query"], "template": r["template"], "split": r.get("split", ""),
                                        "results": o["results"][: a.top]}) + "\n")
        log(f"yardstick results -> {a.yardstick_out}")


if __name__ == "__main__":
    main()
