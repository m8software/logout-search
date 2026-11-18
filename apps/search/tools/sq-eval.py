#!/usr/bin/env python3
"""One-command eval for the search-quality epic: every open set against one engine, row for
row against the frozen baseline, with the acceptance rule's interval. Every arm measures with this and nothing else.

    tools/sq-eval.py --arm NAME [--engine URL | --release DIR] [--test] [--sets a,b] [--judged] [--force]
    tools/sq-eval.py --arm NAME --from-cache --build ID [--both] [--keys accept|primary]     # re-score, no engine
    tools/sq-eval.py --write-exclude evalsets/open/exclude.tsv [--build ID]

Dev halves by default. --test runs the TEST halves: once per arm, at the very end, and it says so loudly — a
second --test for the same arm is refused without --force (nobody tunes on test). --release DIR starts the engine
from that release copy with sq-engine.sh (podman, this workstation) and stops it after. One engine runs on
the workstation at a time (sq-engine.sh's lock): --release waits its turn, and an engine you started yourself
with `sq-engine.sh up` must be taken `down` as soon as you have measured.

Writes ~/.local/share/logout-search/sq/<arm>/:
  <set>.<split>.tsv   per-query rows in eval-queries.py --details shape + set, stratum (compare-evals.py labels pairs them)
  table.md, table.tsv the one table: n, hit@1, hit@5, and against --baseline (default baseline-rc8) the change in
                      points with its 95% paired-bootstrap interval and the verdict of compare-evals.py's rule
                      (pass = the interval includes 0 and the drop is at most 0.5 pt, or a significant gain)
  strata.tsv          hit@1 / hit@5 per (set, stratum) — head-brand's name:only and name:shared apart, as its README asks
  run.json            build, split, sets, how many answers were cached, when
  TEST-RUN.json       written by --test; its presence is what the refusal reads
Answers are cached by (engine build, set, query) under sq/cache/<build>/, so an arm that changed one list re-asks
only what moved: a new build hash re-asks everything, the same build nothing. The build comes from sq-engine.sh's
sidecar for the engine's port, or --build.

Answer keys: --keys accept (the default) counts a row's expected answer and its accepted alternatives
(evalsets/open/accept/: every official site of the row's entity, and a place's own website — a thing's own site
above its article or its map row is a right answer, owner 2026-10-13) and pairs against baseline-master-20261013
(today's master, dev and test halves; baseline-rc8-keys3 before the evening of 2026-10-13, kept);
--keys primary counts the expected answer alone and pairs against baseline-rc8, as every table before that date.
--from-cache scores what a build already answered (sq/cache/<build>/) without any engine: how baseline-rc8-keys2
and -keys3 were made from baseline-rc8's answers.

Judged sets (--judged): the held-out answer set and the informational set are judged by Clef through
utility/lib/clef.py — the shared ledger's judge() once feat/clef-free-budget is merged, the same call today. Titles,
URLs and query words only (ADR-059). --judge mock scores without any network call (tests). Nothing here runs on a box.
"""
from __future__ import annotations

import argparse
import atexit
import collections
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sq_common as C  # noqa: E402

REPO = C.TOOLS.parents[2]
eq = C.load_tool("eval_queries", C.TOOLS / "eval-queries.py")
ce = C.load_tool("compare_evals", C.TOOLS / "compare-evals.py")
fe = C.load_tool("facts_eval", C.TOOLS / "facts-eval.py")
KEEP = ("url", "title", "source", "score", "lat", "lon", "official", "popular", "lr", "ce", "corrected")
DETAILS_HEADER = "query\tcategory\trank\ttop_score\ttop_url\tconfident\tset\tstratum"
JUDGED_DEFAULT = Path.home() / ".local/share/logout-search/dbperf/judged-eval-1167.tsv"


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- asking the engine, cached by build
def ask_one(api: str, q: str, extra: str) -> dict:
    try:
        # "&instant=1" in --extra asks what the page asks (/instant?full=1: the shown list, after the card's mix and gate)
        path = "/instant?full=1" if "&instant=1" in extra else "/search?k=10"
        with urllib.request.urlopen(api + path + extra + "&q=" + urllib.parse.quote(q), timeout=120) as r:
            d = json.load(r)
    except Exception as e:
        return {"q": q, "error": type(e).__name__}
    res = [{k: x.get(k) for k in KEEP if x.get(k) is not None} for x in d.get("results", [])[:10]]
    o = {"q": q, "results": res, "confident": bool(d.get("confident")), "intent": d.get("intent", "")}
    if "&instant=1" in extra:   # what the page shows above the list: the answer box (facts) and the card's links (books)
        ans = d.get("answer") or {}
        if ans:
            o["answer"] = {k: ans.get(k) for k in ("url", "title", "answer", "value", "label") if ans.get(k) is not None}
        card = d.get("card") or {}
        links = [card.get("url")] + [l.get("url") for l in (card.get("links") or []) + (card.get("more_links") or [])]
        if any(links):
            o["card_links"] = [u for u in links if u]
    if d.get("corrected"):
        o["corrected"] = d["corrected"]
    return o


def cache_path(build: str, name: str, extra: str) -> Path:
    tag = "" if not extra else "." + re.sub(r"[^a-z0-9]+", "_", extra.lower()).strip("_")[:40]
    return C.SQ_DIR / "cache" / build / f"{name}{tag}.jsonl"


def answers(api: str, build: str | None, name: str, queries: list[str], extra: str, workers: int) -> tuple[dict, int]:
    """{query: answer} for every query, from the build's cache where it has one. -> (answers, how many were cached)"""
    have: dict = {}
    path = cache_path(build, name, extra) if build else None
    if path and path.exists():
        for line in open(path, encoding="utf-8"):
            if line.strip():
                o = json.loads(line)
                if "error" not in o:
                    have[o["q"]] = o
    todo = [q for q in dict.fromkeys(queries) if q not in have]
    cached = len(queries) - len(todo)
    if todo and api is None:
        sys.exit(f"sq-eval: --from-cache, and the cache of build {build} has no answer for {len(todo)} queries of {name} "
                 f"(first: {todo[0]!r}) — nothing was scored")
    if todo:
        log(f"  {name}: asking {len(todo)} queries ({cached} cached)")
        with ThreadPoolExecutor(max(1, workers)) as ex:
            got = list(ex.map(lambda q: ask_one(api, q, extra), todo))
        errs = [o for o in got if "error" in o]
        if errs:
            log(f"  {name}: {len(errs)} queries failed ({errs[0]['error']}) — not cached, scored as misses")
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                for o in got:
                    if "error" not in o:
                        f.write(json.dumps(o) + "\n")
        for o in got:
            have[o["q"]] = o
    return have, cached


# ---------------------------------------------------------------- scoring (eval-queries.py's hit rule)
def _book_hit(expected: str, url: str) -> bool:
    u = eq.norm_url(url or "")
    return any(e and (u == eq.norm_url(e) or u.startswith(eq.norm_url(e) + "/")) for e in expected.split("|"))


def _fact_hit(r: dict, o: dict) -> bool:
    """facts-eval.py's ANSWERED: the answer box names the row's article and its value matches."""
    title, _, value = r["expected"].partition("|")
    a = o.get("answer") or {}
    return (bool(a.get("answer")) and a.get("url") == "https://en.wikipedia.org/wiki/" + title.replace(" ", "_")
            and fe.value_matches(value, a))


def score_rows(rows: list[dict], got: dict) -> list[tuple]:
    """eval-queries.py --details rows + set, stratum: (q, cat, rank|'-', top_score, top_url, confident, set, stratum).
    A fact row is right when the answer box is (rank 1: the page shows it above the list); a books row matches the
    expected Gutenberg page by URL, in the list or among the card's links (the card is above the list: rank 1).
    Before 2026-11-07 both read 0.0 for every engine: hit() has no rule for them and /search has no answer box."""
    out = []
    for r in rows:
        o = got.get(r["query"]) or {"results": [], "confident": False}
        res = o.get("results") or []
        if r["category"] == "fact":
            rank = 1 if _fact_hit(r, o) else None
        elif r["category"] == "books":
            rank = 1 if any(_book_hit(r["expected"], u) for u in o.get("card_links") or []) else \
                next((i + 1 for i, x in enumerate(res) if _book_hit(r["expected"], x.get("url", ""))), None)
        else:
            rank = next((i + 1 for i, x in enumerate(res) if C.row_hit(eq, r, x)), None)
        top = res[0].get("score") or 0.0 if res else 0.0
        out.append((r["query"], r["category"], rank or "-", f"{float(top):.1f}", res[0]["url"] if res else "",
                    int(bool(o.get("confident"))), r["set"], r.get("stratum", "")))
    return out


def page_ranks(rows: list[dict], got: dict) -> list[tuple]:
    """Page-level gold (maker + product set, 2026-11-10): a set file may carry an optional `page` column, the address of
    the one page that answers the row (`|`-joined when several do; scheme, www., case and a trailing slash do not
    count). -> (query, rank|'-') for the rows that have one: the first result AT that address or under it. The site-level
    score of the row (score_rows) is untouched; this one is reported beside it and reads page@1 / page@5."""
    out = []
    for r in rows:
        pages = [eq.norm_url(p) for p in (r.get("page") or "").split("|") if p.strip()]
        if not pages:
            continue
        res = (got.get(r["query"]) or {}).get("results") or []
        rank = next((i + 1 for i, x in enumerate(res)
                     if any(eq.norm_url(x.get("url", "")) == p or eq.norm_url(x.get("url", "")).startswith(p + "/") for p in pages)), None)
        out.append((r["query"], rank or "-"))
    return out


def write_details(path: Path, details: list[tuple]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(DETAILS_HEADER + "\n")
        for d in details:
            f.write("\t".join(map(str, d)) + "\n")


def hits(details: list[tuple]) -> dict:
    """{key: {"hit1", "hit5"}} per row. The key is the query; a query a set asks twice with two expected answers
    (maker-product has 15) gets "#2", "#3" on its repeats, so no row is dropped and rows pair in file order.
    (compare-evals.py load_labels() keys by the query alone and keeps the last.)"""
    out, seen = {}, collections.Counter()
    for q, _cat, rank, *_ in details:
        seen[q] += 1
        r = int(rank) if rank != "-" else 99
        out[q if seen[q] == 1 else f"{q}\t#{seen[q]}"] = {"hit1": float(r == 1), "hit5": float(r <= 5)}
    return out


def read_details(path: Path) -> list[tuple]:
    with open(path, encoding="utf-8") as f:
        next(f)
        return [tuple(line.rstrip("\n").split("\t")) for line in f if line.strip()]


def compare(a: dict, b: dict, metric: str) -> dict | None:
    """compare-evals.py's rule on the paired queries: means, B - A in points, the 95% interval, the verdict."""
    qs = sorted(set(a) & set(b))
    if not qs:
        return None
    d = [b[q][metric] - a[q][metric] for q in qs]
    lo, hi = ce.boot(d)
    delta = sum(d) / len(d)
    return {"n": len(qs), "a": sum(a[q][metric] for q in qs) / len(qs), "b": sum(b[q][metric] for q in qs) / len(qs),
            "delta": delta, "lo": lo, "hi": hi, "pass": ce.passes(lo, hi, delta)}


def strata_table(details: list[tuple]) -> list[tuple]:
    groups: dict = collections.OrderedDict()
    for q, _cat, rank, _t, _u, _c, st, strat in details:
        r = int(rank) if rank != "-" else 99
        for facet in [""] + [x for x in strat.split(",") if x]:
            groups.setdefault((st, facet), []).append(r)
    return [(st, facet or "(all)", len(rs), sum(r == 1 for r in rs) / len(rs), sum(r <= 5 for r in rs) / len(rs))
            for (st, facet), rs in groups.items()]


# ---------------------------------------------------------------- the table
def fmt_ci(c: dict | None) -> str:
    return "" if c is None else f"[{100 * c['lo']:+.1f}, {100 * c['hi']:+.1f}]"


def render(arm: str, split: str, build: str | None, baseline: str, rows: list[dict]) -> tuple[str, str]:
    """(markdown, tsv). rows: {set, n, hit1, hit5, c1, c5} with c1/c5 from compare() or None."""
    head = ["set", "n", "hit@1", "hit@5", f"{baseline} hit@1", "Δ hit@1 pt", "95% CI", "Δ hit@5 pt", "verdict"]
    lines = [f"## sq-eval — arm `{arm}`, {split} halves, build `{build or '?'}`, keys `{C.KEYS}`, vs `{baseline}`", "",
             "| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    tsv = ["\t".join(head)]
    for r in rows:
        c1, c5 = r["c1"], r["c5"]
        verdict = r["na"] if r.get("na") else "no baseline" if c1 is None else ("pass" if c1["pass"] and (c5 is None or c5["pass"]) else "FAIL")
        pct = lambda v: "n/a" if v != v else f"{100 * v:.1f}"   # noqa: E731
        cells = [r["set"], str(r["n"]), pct(r["hit1"]), pct(r["hit5"]),
                 "" if c1 is None else f"{100 * c1['a']:.1f}",
                 "" if c1 is None else f"{100 * c1['delta']:+.1f}", fmt_ci(c1),
                 "" if c5 is None else f"{100 * c5['delta']:+.1f}", verdict]
        lines.append("| " + " | ".join(cells) + " |")
        tsv.append("\t".join(cells))
    lines.append("")
    lines.append("Verdict: compare-evals.py's rule per set on hit@1 and hit@5 — pass when the 95% paired-bootstrap "
                 "interval of the change includes 0 and the drop is at most 0.5 pt, or the gain is significant. "
                 "`all` pools every scored query of the run.")
    return "\n".join(lines) + "\n", "\n".join(tsv) + "\n"


# ---------------------------------------------------------------- judged sets (plumbing; Clef through utility/lib/clef.py)
def judged_queries(path: Path, kinds: set[str] | None) -> dict:
    out = {}
    for line in open(path, encoding="utf-8"):
        parts = line.rstrip("\n").split("\t")
        if len(parts) >= 2 and parts[0] and not parts[0].startswith("#"):
            if kinds is None or parts[1] in kinds:
                out[parts[0]] = parts[1]
    return out


def collect_instant(api: str, queries: list[str], extra: str, workers: int) -> list[dict]:
    """What the searcher sees (/instant full) and, for an empty list, what the index would have shown
    (search-answer-eval.py's collection, over HTTP to the local engine)."""
    def get(path):
        with urllib.request.urlopen(api + path, timeout=120) as r:
            return json.load(r)

    def one(q):
        try:
            d = get("/instant?full=1" + extra + "&q=" + urllib.parse.quote(q))
            o = {"q": q, "results": d.get("results", [])[:5], "news": (d.get("news") or [])[:3],
                 "weather_place": (d.get("weather") or {}).get("place") or "", "games": d.get("games") or [],
                 "espn": d.get("espn") or None, "no_game": d.get("no_game") or None}
            if not o["results"]:
                o["local"] = get("/search?k=5&quiet=0&q=" + urllib.parse.quote(q)).get("results", [])[:5]
            return o
        except Exception as e:
            return {"q": q, "results": [], "error": type(e).__name__}
    with ThreadPoolExecutor(max(1, workers)) as ex:
        return list(ex.map(one, queries))


def judge_mock(pairs, state_fn, _questions, _cache_name, **_kw):
    """The judge for tests: a result is right when every query word is in its title or URL. No network."""
    out = []
    for p in pairs:
        s = state_fn(p)
        text = json.dumps(s.get("result") or s.get("headline") or "").lower()
        words = re.findall(r"[a-z0-9]+", s["query"].lower())
        out.append({"answers": {"noul": 1.0 if words and all(w in text for w in words) else 0.0},
                    "good": {"noul": 1.0 if words and all(w in text for w in words) else 0.0}})
    return out, None


def judged_run(api: str, name: str, qfile: Path, kinds: set[str] | None, judge: str, extra: str, workers: int,
               out_dir: Path) -> tuple[list[tuple], dict]:
    """One judged set -> (details rows with ANSWERED as the hit, per-outcome counts). The judge sees query, title,
    URL and the result's kind — never a snippet (ADR-059)."""
    sae = C.load_tool("search_answer_eval", REPO / "utility" / "search-answer-eval.py")   # outcomes, the news question, box rule
    kind = judged_queries(qfile, kinds)
    got = collect_instant(api, list(kind), extra, workers)
    if judge == "mock":
        judge_fn = judge_mock
    else:
        sys.path.insert(0, str(REPO / "utility" / "lib"))
        import clef                                                       # the shared judge (ledger-aware once merged)
        judge_fn = clef.judge
    state = sae.state_of

    def judged(field, top):
        pairs = [(o["q"], pos, r) for o in got for pos, r in enumerate((o.get(field) or [])[:top])]
        ans, usage = judge_fn(pairs, state, sae.sr.RELEVANT, f"sq-{name}-{field}", progress=False)
        rel: dict = collections.defaultdict(dict)
        for (q, pos, _r), a in zip(pairs, ans):
            rel[q][pos] = float((a.get("answers") or {}).get("noul") or 0.0) if judge == "mock" else sae.clef.noul(a, "answers")
        if usage is not None:
            log(f"  judged {name}/{field}: {usage.line(len(pairs))}")
        return rel
    shown, local = judged("results", 1), judged("local", 5)
    npairs = list(dict.fromkeys((o["q"], o["news"][0]["title"], o["news"][0].get("source", "")) for o in got if o.get("news")))
    nans, _u = judge_fn(npairs, lambda p: {"query": p[0], "headline": p[1], "outlet": p[2]}, sae.NEWS_Q, f"sq-{name}-news", progress=False)
    good = {p: (float((x.get("good") or {}).get("noul") or 0.0) if judge == "mock" else sae.clef.noul(x, "good")) for p, x in zip(npairs, nans)}
    details, per = [], collections.Counter()
    out_dir.mkdir(parents=True, exist_ok=True)
    with open(out_dir / f"judged.{name}.jsonl", "w", encoding="utf-8") as f:
        for o in got:
            q = o["q"]
            top = shown[q].get(0, 0.0) if o["results"] else None
            news = o.get("news") or []
            ntop = good.get((q, news[0]["title"], news[0].get("source", "")), 0.0) if news else 0.0
            if o["results"] and o["results"][0].get("stock"):
                oc = "card"
            elif top is not None and top >= 0.7:
                oc = "results"
            elif ntop >= 0.7:
                oc = "news"
            elif sae.box_answers(o):
                oc = "box"
            elif o.get("espn"):
                oc = "browser"
            elif top is None:
                oc = "handoff-bad" if max(local[q].values(), default=0.0) >= 0.7 else "handoff-ok"
            else:
                oc = "miss"
            per[oc] += 1
            answered = oc in ("results", "news", "box", "card")
            f.write(json.dumps(dict(o, kind=kind[q], outcome=oc, top=top, news_top=ntop)) + "\n")
            details.append((q, "judged", 1 if answered else "-", f"{(top or 0.0):.2f}",
                            o["results"][0].get("url", "") if o["results"] else "", 0, f"judged:{name}", f"kind:{kind[q]},outcome:{oc}"))
    return details, dict(per)


# ---------------------------------------------------------------- exclude.tsv
def write_exclude(path: Path, build: str | None) -> int:
    """Every test-half query key of every open set, as typed (lower-case, one space) and as the engine's key
    (letters and digits), plus the engine's corrected spelling where the build's cache has one. Rows for the judged
    sets are appended by their issues with source=judged-<name>."""
    rows = set()
    for s in C.all_sets():
        corrected: dict = {}
        if build and cache_path(build, s, "").exists():
            for line in open(cache_path(build, s, ""), encoding="utf-8"):
                o = json.loads(line) if line.strip() else {}
                if o.get("corrected"):
                    corrected[o["q"]] = o["corrected"]
        for r in C.load_set(s, "test"):
            q = r["query"]
            rows.add((C.norm_query(q), "query", s, "test", "open"))
            rows.add((C.norm_key(q), "norm", s, "test", "open"))
            if q in corrected:
                rows.add((C.norm_query(corrected[q]), "query", s, "test", f"corrected@{build}"))
                rows.add((C.norm_key(corrected[q]), "norm", s, "test", f"corrected@{build}"))
    rows = {r for r in rows if r[0]}
    with open(path, "w", encoding="utf-8") as f:
        f.write("# Queries no head list, training pool or precompute may contain (read by).\n"
                "# key = the query lower-cased and single-spaced (form=query) or as the engine's exact-tier key, letters and\n"
                "# digits only (form=norm, search_api.norm_key); corrected@<build> rows are the engine's respelling of a typo set.\n"
                "# Every TEST-half query of every open set is here (tests/unit/test-search-sq-eval.sh checks that).\n"
                "# Judged sets: the held-out answer set v2 and the informational set append their queries\n"
                "# below with source=judged-<name>, split=test — same two forms per query. Regenerate the open rows with\n"
                "#   tools/sq-eval.py --write-exclude evalsets/open/exclude.tsv [--build ID]   (judged rows are kept)\n"
                "key\tform\tset\tsplit\tsource\n")
        for r in sorted(rows):
            f.write("\t".join(r) + "\n")
    return len(rows)


def read_exclude_judged(path: Path) -> list[str]:
    """The judged rows already in exclude.tsv, kept across a regeneration."""
    if not path.exists():
        return []
    keep = []
    for line in open(path, encoding="utf-8"):
        if line.startswith("#") or line.startswith("key\t"):
            continue
        parts = line.rstrip("\n").split("\t")
        if len(parts) == 5 and parts[4].startswith("judged"):
            keep.append(line)
    return keep


# ---------------------------------------------------------------- engine lifecycle (--release)
def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_release(release: str, arm: str, cap4: bool) -> tuple[str, str, str]:
    """-> (url, build, container name suffix) from sq-engine.sh up."""
    port = free_port()
    name = f"eval-{re.sub(r'[^A-Za-z0-9_.-]', '-', arm)}"
    cmd = [str(C.TOOLS / "sq-engine.sh"), "up", release, "--port", str(port), "--name", name] + (["--cap4"] if cap4 else [])
    log(f"starting the engine from {release} ({' '.join(cmd)})")
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode:
        sys.exit(f"sq-engine.sh up failed: {r.stderr.strip()[-600:]}")
    return f"http://127.0.0.1:{port}", r.stdout.strip().splitlines()[-1], name


def stop_release(name: str) -> None:
    subprocess.run([str(C.TOOLS / "sq-engine.sh"), "down", "--name", name], capture_output=True, text=True)


# ---------------------------------------------------------------- main
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--arm", help="name of this run; results go to sq/<arm>/")
    ap.add_argument("--engine", default="http://127.0.0.1:18484", help="a running engine (sq-engine.sh up)")
    ap.add_argument("--release", help="start the engine from this release copy instead (and stop it after)")
    ap.add_argument("--cap4", action="store_true", help="with --release: the 4 GB / 4-core box budget")
    ap.add_argument("--keep", action="store_true", help="with --release: leave the engine running")
    ap.add_argument("--build", help="engine build id for the cache when the engine has no sq-engine.sh sidecar")
    ap.add_argument("--no-cache", action="store_true", help="ask every query and cache nothing")
    ap.add_argument("--test", action="store_true", help="the TEST halves: once per arm, at the end")
    ap.add_argument("--both", action="store_true", help="dev and test halves (the baseline freeze only; implies --test)")
    ap.add_argument("--force", action="store_true", help="allow a second --test run of the same arm")
    ap.add_argument("--sets", default="", help="comma-separated subset of the open sets (default: all)")
    ap.add_argument("--baseline", help="the arm to pair against (default: the frozen baseline scored on the same keys — "
                    "baseline-master-20261013, or baseline-rc8 with --keys primary)")
    ap.add_argument("--keys", choices=("accept", "primary"), default=C.KEYS,
                    help="answer keys: accept = expected + accepted alternatives (the default); primary = expected only")
    ap.add_argument("--from-cache", action="store_true", help="no engine: score the answers --build already cached")
    ap.add_argument("--extra", default="", help="query-string switches for an A/B, e.g. '&ce=0'; '&instant=1' scores the page's list (/instant full)")
    ap.add_argument("--workers", type=int, default=3, help="queries in flight (the engine has 4 cores)")
    ap.add_argument("--limit", type=int, default=0, help="first N queries per set (smoke tests)")
    ap.add_argument("--judged", action="store_true", help="also the judged sets (Clef via utility/lib/clef.py)")
    ap.add_argument("--judged-queries", default=str(JUDGED_DEFAULT), help="TSV query<TAB>kind for the judged set (NAME=PATH, repeatable with commas)")
    ap.add_argument("--judged-kinds", default="", help="only these kinds of the judged file, comma-separated")
    ap.add_argument("--judge", default="clef", choices=("clef", "mock"), help="mock: no network (tests)")
    ap.add_argument("--write-exclude", metavar="PATH", help="write exclude.tsv (every test-half key) and exit")
    a = ap.parse_args()

    if a.write_exclude:
        path = Path(a.write_exclude)
        kept = read_exclude_judged(path)
        n = write_exclude(path, a.build)
        if kept:
            with open(path, "a", encoding="utf-8") as f:
                f.writelines(kept)
        log(f"wrote {n} open rows (+{len(kept)} judged rows kept) to {path}")
        return
    if not a.arm:
        ap.error("--arm NAME is required")
    a.baseline = a.baseline or C.set_keys(a.keys)
    C.set_keys(a.keys)
    if a.from_cache and (not a.build or a.release or a.no_cache or a.judged):
        ap.error("--from-cache needs --build ID and takes no --release, --no-cache or --judged")
    if a.both:
        a.test = True
    out_dir = C.SQ_DIR / a.arm
    marker = out_dir / "TEST-RUN.json"
    if a.test:
        if marker.exists() and not a.force:
            prev = json.loads(marker.read_text())
            sys.exit(f"sq-eval: arm {a.arm!r} already ran the TEST halves on {prev.get('when')} (build {prev.get('build')}). "
                     f"One --test per arm; --force overrides and is logged.")
        log("=" * 78 + f"\n*** TEST HALVES *** arm {a.arm!r}: this is the one test run of this arm" +
            (" (--force: a repeat, logged)" if a.force else "") + "\n" + "=" * 78)

    started_name = None
    api, build = a.engine.rstrip("/"), None
    if a.release:
        api, build, started_name = start_release(a.release, a.arm, a.cap4)
        if not a.keep:
            atexit.register(stop_release, started_name)      # also on a failure: the engine lock must not stay held
    build = None if a.no_cache else (a.build or build or C.engine_build(api, None))
    if not a.no_cache and not build:
        sys.exit(f"sq-eval: no build id for {api}: start it with sq-engine.sh up (its sidecar names the build), "
                 "or pass --build ID, or --no-cache")
    if a.from_cache:
        api = None
    else:
        try:
            with urllib.request.urlopen(api + "/readyz", timeout=30) as r:
                if r.status != 200:
                    sys.exit(f"sq-eval: {api}/readyz answered {r.status}")
        except Exception as e:
            sys.exit(f"sq-eval: engine at {api} not reachable: {type(e).__name__}: {e}")

    sets = [s.strip() for s in a.sets.split(",") if s.strip()] or C.all_sets()
    splits = ["dev", "test"] if a.both else (["test"] if a.test else ["dev"])
    out_dir.mkdir(parents=True, exist_ok=True)
    if a.test:
        marker.write_text(json.dumps({"when": time.strftime("%Y-%m-%dT%H:%M:%S"), "build": build, "sets": sets,
                                      "forced": a.force, "splits": splits}, indent=1))
    t0 = time.time()
    table, pooled_a, pooled_b, n_cached, n_asked, strata = [], {}, {}, 0, 0, []
    for split in splits:
        for s in sets:
            rows = C.load_set(s, split, a.limit)
            if not rows:
                continue
            cat = rows[0]["category"]
            # facts and books are asked as the page asks (their answer is the answer box / the card, not the list)
            ext = a.extra if cat not in ("fact", "books") or "&instant=1" in a.extra else a.extra + "&instant=1"
            got, cached = answers(api, build, s, [r["query"] for r in rows], ext, a.workers)
            n_cached += cached
            n_asked += len(rows) - cached
            details = score_rows(rows, got)
            write_details(out_dir / f"{s}.{split}.tsv", details)
            pg = page_ranks(rows, got)
            if pg:
                with open(out_dir / f"{s}.{split}.page.tsv", "w", encoding="utf-8") as f:
                    f.write("query\tpage_rank\n" + "".join(f"{q}\t{rk}\n" for q, rk in pg))
                p1 = sum(rk == 1 for _q, rk in pg) / len(pg)
                p5 = sum(rk != "-" and rk <= 5 for _q, rk in pg) / len(pg)
                table.append({"set": f"{s} (page gold)", "n": len(pg), "hit1": p1, "hit5": p5, "c1": None, "c5": None})
                log(f"  {s} page gold: n={len(pg)} page@1={100 * p1:.1f} page@5={100 * p5:.1f}")
            strata += strata_table(details)
            b = hits(details)
            base_path = C.SQ_DIR / a.baseline / f"{s}.{split}.tsv"
            base = hits(read_details(base_path)) if base_path.exists() and a.baseline != a.arm else None
            c1 = compare(base, b, "hit1") if base else None
            c5 = compare(base, b, "hit5") if base else None
            if base:
                for q in set(base) & set(b):
                    pooled_a[f"{s}\t{split}\t{q}"], pooled_b[f"{s}\t{split}\t{q}"] = base[q], b[q]
            label = s if len(splits) == 1 else f"{s} ({split})"
            if cat == "place" and not any(x.get("source") == "place" for o in got.values() for x in (o.get("results") or [])):
                # the place sets were built around testbox's home point and /search takes no location: an engine started
                # without SEARCH_HOME=40.592,-105.871 returns no place rows at all, which is "not measured", not 0
                table.append({"set": label, "n": len(details), "hit1": float("nan"), "hit5": float("nan"), "c1": None, "c5": None,
                              "na": "n/a: no place rows (start the engine with SEARCH_HOME=40.592,-105.871, testbox's home)"})
                log(f"  {label}: {table[-1]['na']}")
                continue
            table.append({"set": label, "n": len(details), "hit1": sum(x["hit1"] for x in b.values()) / len(b),
                          "hit5": sum(x["hit5"] for x in b.values()) / len(b), "c1": c1, "c5": c5})
            log(f"  {label}: n={len(details)} hit@1={100 * table[-1]['hit1']:.1f} hit@5={100 * table[-1]['hit5']:.1f}"
                + (f"  Δ@1={100 * c1['delta']:+.1f} {fmt_ci(c1)} {'pass' if c1['pass'] else 'FAIL'}" if c1 else "  (no baseline)"))
    if a.judged:
        kinds = set(a.judged_kinds.split(",")) if a.judged_kinds else None
        for spec in a.judged_queries.split(","):
            name, _, p = spec.partition("=")
            if not p:
                name, p = Path(name).stem, name
            if not Path(p).exists():
                log(f"  judged set {name}: {p} is not here — skipped (put it there)")
                continue
            details, per = judged_run(api, name, Path(p), kinds, a.judge, a.extra, a.workers, out_dir)
            write_details(out_dir / f"judged.{name}.tsv", details)
            b = hits(details)
            base_path = C.SQ_DIR / a.baseline / f"judged.{name}.tsv"
            base = hits(read_details(base_path)) if base_path.exists() and a.baseline != a.arm else None
            # for a judged set hit@1 is ANSWERED (results + news + box + card); hit@5 means nothing and is left out
            c1 = compare(base, b, "hit1") if base else None
            table.append({"set": f"judged:{name}", "n": len(details), "hit1": sum(x["hit1"] for x in b.values()) / max(1, len(b)),
                          "hit5": float("nan"), "c1": c1, "c5": None})
            log(f"  judged:{name}: n={len(details)} ANSWERED={100 * table[-1]['hit1']:.1f} outcomes={per}")
    if pooled_a:
        c1, c5 = compare(pooled_a, pooled_b, "hit1"), compare(pooled_a, pooled_b, "hit5")
        table.append({"set": "all", "n": c1["n"], "hit1": c1["b"], "hit5": c5["b"], "c1": c1, "c5": c5})
    md, tsv = render(a.arm, "+".join(splits), build, a.baseline, table)
    (out_dir / "table.md").write_text(md)
    (out_dir / "table.tsv").write_text(tsv)
    with open(out_dir / "strata.tsv", "w", encoding="utf-8") as f:
        f.write("set\tstratum\tn\thit@1\thit@5\n")
        for st, facet, n, h1, h5 in strata:
            f.write(f"{st}\t{facet}\t{n}\t{100 * h1:.1f}\t{100 * h5:.1f}\n")
    (out_dir / "run.json").write_text(json.dumps({
        "arm": a.arm, "engine": api or "from-cache", "build": build, "keys": a.keys, "splits": splits, "sets": sets, "baseline": a.baseline, "extra": a.extra,
        "test": a.test, "forced": a.force, "cached": n_cached, "asked": n_asked, "seconds": round(time.time() - t0),
        "when": time.strftime("%Y-%m-%dT%H:%M:%S")}, indent=1))
    print(md, end="")
    if a.test:
        log(f"*** TEST HALVES were scored for arm {a.arm!r} — recorded in {marker}")
    log(f"written: {out_dir}")


if __name__ == "__main__":
    main()
