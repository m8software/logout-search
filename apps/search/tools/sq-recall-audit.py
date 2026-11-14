#!/usr/bin/env python3
"""The candidate-recall audit: for every dev query of every open set, where the expected answer was
in the engine's candidate dump, at which step it was lost, and what was shown instead. No engine: it reads

    sq/candidates/<build>/<set>.jsonl   tools/sq-candidates.py's raw answers (one line per query, streamed)
    sq/<arm>/<set>.dev.tsv              tools/sq-eval.py's per-query rows (the served rank and first result)
    evalsets/open/<set>.tsv             the sets (DEV HALVES ONLY: a test row is counted, never read further)

    tools/sq-recall-audit.py [--build ID] [--arm baseline-rc8] [--sets a,b] [--release DIR] [--out DIR]
                             [--hard-negatives FILE | --no-hard-negatives] [--examples 5] [--open DIR]

Writes <out>/queries.tsv (one row per dev query: class, the lists that held the answer and its rank in each, its
rank at every step, the step that lost it, what beat it, the named cause), <out>/tables.md (every table below),
and the hard-negatives file (sq/hard-negatives/<build>.tsv, never in git). Prints tables.md.

The five classes (the served rank is sq-eval's, k=10 — the dump's final step is k=20 and is the path, not the verdict):
    first   the expected answer is the first result
    top5    in the first five, not first
    pool    in the candidate pool the rankers saw (the fused top-20, or an exact tier above it), outside the first five
    list    in one of the lists the fusion blends, but not in the fused top-20 nor in a tier
    never   in no list and no tier: never retrieved

The steps, in the engine's order (search_api._search): the lists (wiki, sites, anchors, pages, described, kind,
sem, ...) -> fused (their reciprocal-rank fusion) ; named (the exact tiers above the fusion, 870-1002) -> merged
(both, one order, after the hand rules) -> ranker (the learned model, first 20, skipped under a kept first result)
-> ce (the cross-encoder, 8 rows below 897) -> final (the endings and front-page passes).

--release DIR (optional) opens DIR/lookup.sqlite read-only (stdlib sqlite3, a few point lookups, no engine) to say
whether a site answer's domain is in the index at all, and whether it has a crawled front page.

Hard-negatives file (TSV, header row), one line per (query, wrong result), dev queries whose answer was retrieved
(class top5, pool or list) but not first:
    query  set  category  expected  class  answer_url  answer_rank  neg_rank  neg_url  neg_source  neg_score  neg_tier
answer_rank is the answer's rank in the dump's final step ("-" when it is not in the final 20); the negatives are
the results above it there, or the first HARD_NEG_MAX when it is absent. neg_tier is the score tier of the wrong
result: `nav`, `place` and `exact` rows are exact-name matches and may be a second right answer (a brand's other
ending, the entity's own article) — a trainer should drop or down-weight them and keep `under` and `fused`.
A query in evalsets/open/exclude.tsv (a test-half or judged query, as typed or as the engine's key) is never written.

Answer keys: a row's accepted alternatives (evalsets/open/accept/, every official site of its entity) count
as the answer at every step, as in sq-eval; SQ_KEYS=primary with --arm baseline-rc8 reproduces the 2026-10-13 audit.

DEV TOOLING: stdlib only, never imported by apps/search/serve.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import struct
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sq_common as C  # noqa: E402

eq = C.load_tool("eval_queries", C.TOOLS / "eval-queries.py")

CLASSES = ("first", "top5", "pool", "list", "never")
CLASS_LABEL = {"first": "first", "top5": "top-5, not first", "pool": "in the pool, outside top-5",
               "list": "in a list, not in fused top-20", "never": "never retrieved"}
STEPS = ("named", "fused", "fuzzy", "merged", "ranker", "ce", "final")
# search_api.py: NAV_SCORE 1000, the place-name tier 930-989, WIKI_EXACT_SCORE 900 (kind lift 904, product 897.5,
# accounts 899.x), CE_EXACT 897 = the floor the cross-encoder may not move, 870-896 demoted names / related
# articles / branches under the thing, the fusion below (one list ~49, two ~98)
PIN = 897.0
TIERS = (("nav", 990.0), ("place", 930.0), ("exact", PIN), ("under", 870.0))
HARD_NEG_MAX = 10
TEMPLATE_WORDS = (" official site", " official website", " website", " site")
OWNERS = ("retrieval", "fusion-cut", "tiers", "tier-order", "hand-rules", "fusion-order", "ranker", "ce", "final",
          "served")
OWNER_LABEL = {
    "retrieval": "never retrieved (no list, no tier)",
    "fusion-cut": "in a list, cut by the fusion (outside fused top-20)",
    "tiers": "first in the fusion, an exact tier put above it",
    "tier-order": "in an exact tier, another tier row above it",
    "hand-rules": "first in the tiers, moved down by the hand rules",
    "fusion-order": "in the fusion only, never first in it",
    "ranker": "first before the learned ranker, moved down by it",
    "ce": "first before the cross-encoder, moved down by it",
    "final": "first after the cross-encoder, moved by the endings / front-page passes",
    "served": "first in the dump's final step, not in the served list (k=10 vs k=20, or a run-to-run change)",
}


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def tier(score) -> str:
    """The score tier of a row: nav / place / exact (pinned, >= 897) / under / fused."""
    if not isinstance(score, (int, float)):
        return "-"
    for name, floor in TIERS:
        if score >= floor:
            return name
    return "fused"


def host_of(url: str) -> str:
    h = url.split("://", 1)[-1].split("/", 1)[0].split("?", 1)[0].lower()
    return h.rsplit("@", 1)[-1].split(":", 1)[0]


def name_of(q: str) -> str:
    """The name a query asks for, as the engine's key: the site-nav templates' own words dropped."""
    s = C.norm_query(q)
    for w in TEMPLATE_WORDS:
        if s.endswith(w):
            s = s[: -len(w)]
            break
    return C.norm_key(s)


def names_query(q: str, row: dict) -> bool:
    """The row is named like the query: its title (before the site's own suffix) or its host's label is the name."""
    nk = name_of(q)
    if not nk:
        return False
    title = (row.get("title") or "")
    for sep in (" — ", " - ", " | ", " – ", ": "):
        title = title.split(sep)[0]
    if C.norm_key(title) == nk:
        return True
    labels = [x for x in host_of(row.get("url", "")).split(".") if x != "www"]
    return bool(labels) and (C.norm_key(labels[0]) == nk or C.norm_key("".join(labels[:-1])) == nk)


def place_alias(expected: list[str], rows: list[dict], served_rank) -> str | None:
    """The URL of the listing twin sq-eval counted (same name within 150 m): the dump carries no coordinates, so it
    is taken from the final step — the place row with the expected name at the served rank, else the first one."""
    if not isinstance(served_rank, int):
        return None
    names = {eq.norm_key(e.split("@", 2)[2]) for e in expected if e.count("@") >= 2}
    same = [(i + 1, x) for i, x in enumerate(rows) if x.get("source") == "place"
            and eq.norm_key((x.get("title") or "").split(" — ")[0]) in names]
    if not same:
        return None
    return next((x["url"] for i, x in same if i == served_rank), same[0][1]["url"])


def first_hit(cat: str, exp: list[str], rows: list[dict], alias: str | None = None, accept: list[str] | None = None) -> int | None:
    """The rank of the first right answer: the expected one, a place's listing twin, or an accepted
    alternative — the entity's own official site."""
    for i, x in enumerate(rows):
        if (alias and x.get("url") == alias) or eq.hit(cat, exp, x) or (accept and eq.accept_hit(cat, accept, x)):
            return i + 1
    return None


def find_row(url: str, rows: list[dict]) -> dict | None:
    return next((x for x in rows if x.get("url") == url), None)


def audit_row(r: dict, d: dict | None, served_rank, served_top: str = "") -> dict:
    """One dev query against its dumped answer. Pure, for tests. served_rank: int, or None when the answer was not in
    the served ten; served_top: the URL sq-eval saw first."""
    cat, exp = r["category"], ([] if r["expected"] == "-" else r["expected"].split("|"))
    o: dict = {"set": r.get("set", ""), "query": r["query"], "category": cat, "expected": r["expected"],
               "stratum": r.get("stratum", ""), "entity": r.get("entity", ""), "served": served_rank}
    if d is None:
        o["class"] = "not-dumped"
        return o
    step_rows = {s: d[s].get("rows", []) for s in STEPS if d.get(s)}
    final = step_rows.get("final", [])
    alias = None
    if cat == "place" and isinstance(served_rank, int) and not (first_hit(cat, exp, final[:10]) == served_rank):
        alias = place_alias(exp, final, served_rank)
    o["alias"] = alias or ""
    o["corrected"] = d.get("corrected", "")
    lists = [(lst.get("name", ""), lst.get("rows", []), lst.get("n", 0)) for lst in d.get("lists", [])]
    o["asked"] = [n for n, _rows, _n in lists]
    o["list_n"] = {n: k for n, _rows, k in lists}
    o["fused_n"] = d.get("fused", {}).get("n", 0)
    acc = r.get("accept") or []
    o["accept"] = acc
    o["in_lists"] = [(n, k) for n, rows, _n in lists if (k := first_hit(cat, exp, rows, alias, acc))]
    rk = {s: first_hit(cat, exp, rows, alias, acc) for s, rows in step_rows.items()}
    o["ranks"] = rk
    o["ran"] = sorted(step_rows)
    o["sources"] = [n for n, _k in o["in_lists"]] + [s for s in ("named", "fuzzy") if rk.get(s)]
    in_pool = any(rk.get(s) for s in STEPS)
    dump_final = rk.get("final")
    if served_rank == 1:
        cls = "first"
    elif isinstance(served_rank, int) and served_rank <= 5:
        cls = "top5"
    elif in_pool or isinstance(served_rank, int):
        cls = "pool"
    elif o["in_lists"]:
        cls = "list"
    else:
        cls = "never"
    o["class"] = cls
    o["via"] = "tier" if rk.get("named") else "fusion" if rk.get("fused") else "-"
    # the answer's own row and score where the pool has one order
    ans = None
    for s in ("merged", "final", "named", "fused", "fuzzy"):
        if rk.get(s):
            ans = step_rows[s][rk[s] - 1]
            break
    if ans is None and o["in_lists"]:
        n0, k0 = o["in_lists"][0]
        ans = next(rows for n, rows, _n in lists if n == n0)[k0 - 1]
    o["answer_url"] = ans["url"] if ans else ""
    o["answer_tier"] = tier(ans.get("score")) if ans and any(rk.get(s) for s in ("merged", "final", "named")) else (
        "fused" if rk.get("fused") or rk.get("fuzzy") else "-")
    # the path: the answer's rank before the merge, then at each step that ran
    pre = "named" if rk.get("named") else "fused"
    path = [(pre, rk.get(pre))] + [(s, rk.get(s)) for s in ("merged", "ranker", "ce", "final") if s in step_rows]
    o["path"] = path
    o["moves"] = [(a, b, ra, rb) for (a, ra), (b, rb) in zip(path, path[1:])]
    owner = ""
    if cls == "never":
        owner = "retrieval"
    elif cls == "list":
        owner = "fusion-cut"
    elif cls != "first":
        for a, b, ra, rb in o["moves"]:
            if ra == 1 and rb != 1:
                owner = {"merged": "tiers" if a == "fused" else "hand-rules"}.get(b, b)
                break
        if not owner:
            if dump_final == 1:
                owner = "served"
            else:
                owner = "tier-order" if rk.get("named") else "fusion-order"
    o["owner"] = owner
    # what was shown first instead
    w = None
    if cls != "first":
        for s in ("final", "merged", "ce", "ranker", "named", "fused"):
            w = find_row(served_top, step_rows.get(s, [])) if served_top else None
            if w:
                break
        w = w or (final[0] if final else None)
    if w:
        m = find_row(w["url"], step_rows.get("merged", [])) or w
        flags = sorted(set(w.get("flags", [])) | set(m.get("flags", [])))
        wt = tier(m.get("score"))
        o["w_url"], o["w_source"], o["w_score"], o["w_tier"], o["w_flags"] = w["url"], w.get("source", ""), m.get("score"), wt, flags
        o["w_lists"] = [n for n, rows, _n in lists if find_row(w["url"], rows)] + (
            ["named"] if find_row(w["url"], step_rows.get("named", [])) else [])
        o["w_named"] = names_query(o["corrected"] or r["query"], w)
        src = {"wikipedia": "article", "site": "site", "place": "place"}.get(w.get("source", ""), w.get("source", "") or "row")
        if w.get("source") == "place":
            beat = "a place row"
        elif "kind" in flags and wt in ("exact", "nav", "place"):
            beat = "a kind lift (a site of the kind the words describe)"
        elif wt in ("nav", "place", "exact"):
            beat = f"a pinned {src} {'named like the query' if o['w_named'] else 'not named like the query'}"
        else:
            how = {"ranker": "the learned ranker's order", "ce": "the cross-encoder's order"}.get(owner, "the fusion's order")
            beat = f"an unpinned {src}, {how}"
        o["beat"] = beat
    return o


# ---------------------------------------------------------------------------------------------------------------------
# reading

def load_served(path: Path, rows: list[dict]) -> list[tuple]:
    """(rank or None, first URL) per set row, from sq-eval's per-query file: same order as the set, checked."""
    if not path.exists():
        return [(None, "")] * len(rows)
    got = []
    with open(path, encoding="utf-8") as f:
        cols = f.readline().rstrip("\n").split("\t")
        for line in f:
            p = dict(zip(cols, line.rstrip("\n").split("\t")))
            got.append((p.get("query", ""), int(p["rank"]) if p.get("rank", "-").isdigit() else None, p.get("top_url", "")))
    if len(got) == len(rows) and all(g[0] == r["query"] for g, r in zip(got, rows)):
        return [(g[1], g[2]) for g in got]
    by = {g[0]: (g[1], g[2]) for g in got}                # another order: by query (a repeated query takes the last row)
    return [by.get(r["query"], (None, "")) for r in rows]


def stream_dump(path: Path, want: set[str]):
    """(query, answer) for the wanted queries only, one line in memory at a time; the last answer of a query wins
    upstream (sq-candidates appends on a re-run), so the caller keeps what it needs per query."""
    with open(path, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            d = json.loads(line)
            if d.get("q") in want and "error" not in d:
                yield d["q"], d


class Index:
    """Is a site answer in the release at all? Read-only point lookups, stdlib only, no engine.
    lookup.sqlite names: kind 5 = a domain the release knows (sites.home = its front page was crawled), kind 10 =
    an article's official host, kind 7 = a name of a linked account or official site of a Wikidata item.
    signals.bin (LOSIGNL1): account.sitelinks, the number the link tier's floor reads (search_api.LINK_SITE_SQL:
    an official site of a thing with no English article is shown only with sitelinks >= 2)."""

    def __init__(self, release: str):
        root = Path(release).expanduser()
        self.db = sqlite3.connect(f"file:{root / 'lookup.sqlite'}?mode=ro&immutable=1", uri=True)
        self.sig = None
        p = root / "signals.bin"
        if p.exists():
            f = open(p, "rb")
            head = f.read(12)
            if head[:8] == b"LOSIGNL1":
                n = struct.unpack("<I", head[8:12])[0]
                h = json.loads(f.read(n))
                fd = next((x for x in h["fields"] if x["name"] == "account.sitelinks"), None)
                fmt = {"<u2": "<H", "<u4": "<I", "<i4": "<i", "<i2": "<h"}.get(fd["dtype"]) if fd else None
                if fmt:
                    self.sig = (f, 12 + n + fd["offset"], fd["count"], fmt, struct.calcsize(fmt), h.get("type_shift", 28))

    def domain(self, d: str) -> str:
        d = d.lower()
        cands = [d] + ([d[4:]] if d.startswith("www.") else [])
        best = "absent"
        for c in cands:
            for kind, home in self.db.execute(
                    "SELECT n.kind, s.home FROM names n LEFT JOIN sites s ON s.doc_id = n.doc_id WHERE n.name = ? AND n.kind IN (5, 10)", (c,)):
                if kind == 5 and home:
                    return "home"
                best = "known"
        return best

    def site(self, expected: str) -> str:
        """home when any accepted domain has a crawled front page; known when the release only knows the name."""
        got = [self.domain(d) for d in expected.split("|")]
        return "home" if "home" in got else "known" if "known" in got else "absent"

    def link_sitelinks(self, key: str) -> int | None:
        """The most sitelinks of any linked account or site the release files under this name key (flags: the key
        or the wiki key, as link_accounts reads them); None when the name has no such row or the file is not T5's."""
        if not self.sig or not key:
            return None
        f, base, count, fmt, size, shift = self.sig
        best = None
        for d, fl in self.db.execute("SELECT doc_id, flags FROM names WHERE name = ? AND kind = 7", (key,)):
            if not fl & 5:
                continue
            i = d & ((1 << shift) - 1)
            v = 0
            if i < count:
                f.seek(base + i * size)
                v = struct.unpack(fmt, f.read(size))[0]
            best = max(best or 0, v)
        return best


PARENT = {"typo-site": "site-nav", "typo-wiki": "wiki-redirect", "typo-place": "place"}
FOUND = ("first", "top5", "pool", "list")


def run(sets: list[str], dump: Path, arm: Path, index: Index | None = None) -> tuple[list[dict], dict]:
    recs, info, dev = [], {}, {}
    exp_domains: set[str] = set()
    seen_domains: set[str] = set()
    for s in sets:                                          # every set's dev rows first: a domain one set expects may
        allrows = C.load_set(s)                             # come back for another set's wording of the same thing
        rows = [r for r in allrows if r.get("split") == "dev"]
        info[s] = {"dev": len(rows), "test": sum(r.get("split") == "test" for r in allrows)}
        del allrows
        for r in rows:
            r["set"] = s
            if r["category"] in ("site", "click"):
                exp_domains.update(r["expected"].split("|"))
        dev[s] = rows
    for s in sets:
        rows = dev[s]
        served = load_served(arm / f"{s}.dev.tsv", rows)
        info[s]["served_rows"] = (arm / f"{s}.dev.tsv").exists()
        byq = defaultdict(list)
        for i, r in enumerate(rows):
            byq[r["query"]].append(i)
        out: list = [None] * len(rows)
        p = dump / f"{s}.jsonl"
        if p.exists():
            for q, d in stream_dump(p, set(byq)):
                for i in byq[q]:
                    out[i] = audit_row(rows[i], d, *served[i])
                for step in [lst.get("rows", []) for lst in d.get("lists", [])] + [d[x].get("rows", []) for x in STEPS if d.get(x)]:
                    for x in step:                      # which expected domains ever came back, for any wording
                        h = host_of(x.get("url", ""))
                        while h:
                            if h in exp_domains:
                                seen_domains.add(h)
                            h = h.partition(".")[2]
        for i, r in enumerate(rows):
            recs.append(out[i] or audit_row(r, None, *served[i]))
        log(f"  {s}: {len(rows)} dev rows ({info[s]['test']} test rows not read), {sum(o is None for o in out)} not in the dump")
    by_entity = defaultdict(list)
    for o in recs:
        if o.get("entity"):
            by_entity[o["entity"]].append(o)
    for o in recs:
        others = [x for x in by_entity.get(o.get("entity", ""), []) if x is not o]
        o["entity_rows"] = len(others)
        o["entity_found"] = sum(x["class"] in FOUND for x in others)
        base = next((x for x in others if x["set"] == PARENT.get(o["set"])), None)
        o["base_query"] = base["query"] if base else ""
        o["base_class"] = base["class"] if base else ""
        if o["category"] in ("site", "click"):
            o["seen_elsewhere"] = any(d in seen_domains for d in o["expected"].split("|"))
            if index is not None and o["class"] == "never":
                o["indexed"] = index.site(o["expected"])
                o["link_sl"] = index.link_sitelinks(name_of(o["base_query"] or o["query"]))
    for o in recs:
        o["cause"] = cause_of(o)
    return recs, info


# ---------------------------------------------------------------------------------------------------------------------
# the named causes

def cause_of(o: dict) -> str:
    """One named cause per query that is not first: what would have to change for the answer to lead."""
    cls = o["class"]
    if cls in ("first", "not-dumped"):
        return ""
    cat = o["category"]
    if cls == "never":
        if o["set"] in PARENT and o.get("base_class") in FOUND:
            return "N3 a typo the engine did not undo (the right spelling finds the answer)"
        if cat in ("site", "click"):
            if o.get("seen_elsewhere"):
                return "N5 the site is in the release, this wording does not reach it"
            if "article:no" in o.get("stratum", ""):
                return "N1 a link-only official site (no article, no crawled page) the link tier holds back"
            if "official" in o.get("w_flags", []) and o.get("w_tier") == "nav":
                return "N4 the engine led with another official site (the key's domain is not the one the release holds)"
            return "N2 the site never comes back (in no row of any dev query, under any wording; no crawled front page)"
        if cat == "place":
            return "N6 the place is not returned (another feature of the name is)"
        if cat == "wiki":
            return "N7 the article is not returned"
        return "N8 the account is not returned"
    if cls == "list":
        return "R9 in one list only, cut by the fusion (outside fused top-20)"
    ws, wt = o.get("w_source", ""), o.get("w_tier", "")
    beat = o.get("beat", "")
    if cat == "place":
        if ws == "place":
            return "R7 another place of the name first"
        if ws == "site" and wt == "place":
            return "R6 a site named like the query's first word lifted above the place"
        if ws == "wikipedia" and wt in ("nav", "exact"):
            return "R4 an exact-title article above the expected place"
        if wt in ("nav", "exact"):
            return "R2 a named site above the expected place"
    if beat.startswith("a kind lift"):
        return "R10 a kind lift first"
    if beat.startswith("a pinned"):
        if cat == "wiki":
            return "R1 a named site above the expected article" if ws != "wikipedia" else "R11 another exact-title article above the expected article"
        if ws == "wikipedia":
            return "R3 an exact-title article above the expected site"
        return "R5 another named site (or account) above the expected site"
    return {"ranker": "R8b unpinned winner: the learned ranker moved the answer down",
            "ce": "R8c unpinned winner: the cross-encoder moved the answer down"}.get(
        o.get("owner", ""), "R8a unpinned winner: the fusion never had the answer first")


# ---------------------------------------------------------------------------------------------------------------------
# tables

def pct(n: int, d: int) -> str:
    return f"{100 * n / d:.1f}" if d else "-"


def md_table(head: list[str], rows: list[list]) -> str:
    out = ["| " + " | ".join(head) + " |", "|" + "|".join("---" if i == 0 else "---:" for i in range(len(head))) + "|"]
    out += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return "\n".join(out)


def tables(recs: list[dict], info: dict, sets: list[str], n_examples: int = 5) -> str:
    live = [o for o in recs if o["class"] != "not-dumped"]
    by_set = defaultdict(list)
    for o in live:
        by_set[o["set"]].append(o)
    parts = []

    # 1. the five-way split
    rows = []
    for s in sets + ["all"]:
        xs = live if s == "all" else by_set[s]
        c = Counter(o["class"] for o in xs)
        n = len(xs)
        rows.append([s, n] + [f"{c[k]} ({pct(c[k], n)}%)" for k in CLASSES])
    parts.append("### T1. Where the expected answer was (dev halves)\n\n" + md_table(["set", "n"] + [CLASS_LABEL[k] for k in CLASSES], rows))
    nd = [o for o in recs if o["class"] == "not-dumped"]
    parts.append(f"Dev rows: {len(recs)}; not in the dump: {len(nd)}"
                 + (" (" + ", ".join(f"{o['set']}: {o['query']!r}" for o in nd[:10]) + ")" if nd else "")
                 + f". Test rows counted and not read: {sum(v['test'] for v in info.values())}.")

    # 1b. the pool class: through the fusion or through a tier; and the served/dump disagreement
    rows = []
    for s in sets + ["all"]:
        xs = live if s == "all" else by_set[s]
        pool = [o for o in xs if o["class"] in ("top5", "pool")]
        dis = sum((o["ranks"].get("final") == 1) != (o["served"] == 1) for o in xs)
        rows.append([s, len(pool), sum(o["via"] == "tier" for o in pool), sum(o["via"] == "fusion" for o in pool),
                     sum(o["via"] == "-" for o in pool), sum(bool(o.get("alias")) for o in xs), dis])
    parts.append("### T1b. Retrieved, not first: how the answer reached the pool\n\n" + md_table(
        ["set", "top-5 + pool", "through an exact tier", "through the fusion only", "served only", "place twins matched by name", "first in dump ≠ first served"], rows))

    # 2. the lists
    names = list(dict.fromkeys(n for o in live for n in o["asked"])) + ["named", "fuzzy"]
    for label, xs in [("all sets", live)] + [(s, by_set[s]) for s in sets]:
        rows = []
        n = len(xs)
        any_src = sum(bool(o["sources"]) for o in xs)
        for name in names:
            asked = n if name in ("named", "fuzzy") else sum(name in o["asked"] for o in xs)
            if name == "fuzzy":
                asked = sum("fuzzy" in o["ran"] for o in xs)
            has = [o for o in xs if name in o["sources"]]
            if not asked and not has:
                continue
            rank = {o["query"] + "\t" + o["expected"]: dict(o["in_lists"]).get(name) or o["ranks"].get(name) for o in has}
            r1 = sum(v == 1 for v in rank.values())
            r5 = sum(v <= 5 for v in rank.values())
            uniq = [o for o in has if len(o["sources"]) == 1]
            uniq_l = [o for o in has if name not in ("named", "fuzzy") and [x for x in o["sources"] if x not in ("named", "fuzzy")] == [name]]
            lens = sorted(o["list_n"].get(name, 0) for o in xs if name in o["asked"])
            med = lens[len(lens) // 2] if lens else "-"
            rows.append([name, asked, med, f"{len(has)} ({pct(len(has), n)}%)", r1, r5, len(uniq),
                         len(uniq_l) if name not in ("named", "fuzzy") else "-",
                         sum(o["class"] == "first" for o in uniq)])
        fn = sorted(o.get("fused_n", 0) for o in xs)
        parts.append(f"### T2. Source lists — {label} (n={n}, answer in any source: {any_src}, {pct(any_src, n)}%; "
                     f"fused rows per query: median {fn[len(fn) // 2] if fn else '-'})\n\n" + md_table(
            ["source", "ran for", "rows in it (median)", "answer in it (solo recall)", "at rank 1", "at rank ≤5", "only this source had it",
             "only this list (tiers aside)", "of the unique: served first"], rows))

    # 3. the steps
    trans = [("fused", "merged"), ("named", "merged"), ("merged", "ranker"), ("merged", "ce"), ("ranker", "ce"), ("ce", "final"),
             ("merged", "final"), ("ranker", "final")]
    tl = {("fused", "merged"): "fused → merged (exact tiers go above the fusion)", ("named", "merged"): "named → merged (hand rules)",
          ("merged", "ranker"): "merged → ranker", ("merged", "ce"): "merged → ce (ranker skipped)", ("ranker", "ce"): "ranker → ce",
          ("ce", "final"): "ce → final (endings, front pages)", ("merged", "final"): "merged → final (no ranker, no ce)",
          ("ranker", "final"): "ranker → final (no ce)"}
    rows = []
    for a, b in trans:
        mv = [(ra, rb) for o in live for (x, y, ra, rb) in o.get("moves", []) if (x, y) == (a, b) and (ra or rb)]
        if not mv:
            continue
        big = 99
        rows.append([tl[(a, b)], len(mv),
                     sum(ra == 1 and rb != 1 for ra, rb in mv), sum(ra != 1 and rb == 1 for ra, rb in mv),
                     sum((ra or big) <= 5 < (rb or big) for ra, rb in mv), sum((rb or big) <= 5 < (ra or big) for ra, rb in mv)])
    parts.append("### T3. Steps: answers each step moved (all dev queries with the answer on either side)\n\n" + md_table(
        ["step", "answers through it", "out of #1", "into #1", "out of top-5", "into top-5"], rows))
    rows = []
    c = {s: Counter(o["owner"] for o in by_set[s] if o["owner"]) for s in sets}
    tot = Counter(o["owner"] for o in live if o["owner"])
    for k in OWNERS:
        if tot[k]:
            rows.append([OWNER_LABEL[k], tot[k]] + [c[s][k] or "" for s in sets])
    parts.append("### T3b. The step that owns each answer that is not first\n\n" + md_table(["where it was lost", "all"] + sets, rows))

    # 4. what beat it
    lost = [o for o in live if o["class"] in ("top5", "pool", "list") and o.get("beat")]
    for title, key in (("T4a. What was first instead — row type and score tier of the winner vs the answer's tier",
                        lambda o: f"{o.get('w_source') or '-'} / {o.get('w_tier')} over an answer in {o.get('answer_tier')}"),
                       ("T4b. What was first instead — by kind", lambda o: o["beat"]),
                       ("T4c. Which sources held the winner", lambda o: "+".join(o.get("w_lists") or ["(none: tier 4 / pass)"]))):
        cc = Counter(key(o) for o in lost)
        per = defaultdict(Counter)
        for o in lost:
            per[key(o)][o["set"]] += 1
        rows = [[k, v, pct(v, len(lost))] + [per[k][s] or "" for s in sets] for k, v in cc.most_common(25)]
        parts.append(f"### {title} (retrieved, not first: n={len(lost)})\n\n" + md_table(["winner", "n", "%"] + sets, rows))
    fl = Counter(f for o in lost for f in o.get("w_flags", []))
    parts.append("Winner flags (a winner can carry several): " + ", ".join(f"{k} {v}" for k, v in fl.most_common()) + ".")

    # 5. the causes
    for title, classes in (("T5. Causes — never retrieved", ("never",)), ("T6. Causes — retrieved, not first", ("top5", "pool", "list"))):
        xs = [o for o in live if o["class"] in classes]
        cc = Counter(o["cause"] for o in xs)
        per = defaultdict(Counter)
        for o in xs:
            per[o["cause"]][o["set"]] += 1
        off = Counter(o["cause"] for o in xs if "official" in o.get("w_flags", []))
        rows = [[k, v, pct(v, len(xs)), off[k]] + [per[k][s] or "" for s in sets] for k, v in cc.most_common()]
        parts.append(f"### {title} (n={len(xs)})\n\n" + md_table(["cause", "n", "%", "first result is an official site"] + sets, rows))
        if classes == ("never",):
            ty = [o for o in xs if o["cause"].startswith("N3")]
            if ty:
                parts.append(f"Of the {len(ty)} typos not undone: {sum(not o.get('corrected') for o in ty)} were offered no respelling, "
                             f"{sum(bool(o.get('corrected')) for o in ty)} were respelled to another word. Typo queries whose right "
                             f"spelling does not find the answer either ({sum(o['set'] in PARENT and o.get('base_class') not in FOUND for o in xs)}) "
                             "are counted under the cause of the right spelling.")
        for k, _v in cc.most_common():
            ex = [o for o in xs if o["cause"] == k]
            ex.sort(key=lambda o: (sets.index(o["set"]) if o["set"] in sets else 99))
            step = max(1, len(ex) // n_examples)
            lines = []
            for o in ex[::step][:n_examples]:
                where = ", ".join(f"{n}#{r}" for n, r in o.get("in_lists", [])) or "no list"
                path = " → ".join(f"{s} {r or '-'}" for s, r in o.get("path", []))
                lines.append(f"- `{o['query']}` ({o['set']}) → `{o['expected'][:60]}`; lists: {where}; path: {path}; "
                             f"first instead: {o.get('w_url', '-')} ({o.get('w_tier', '-')})")
            parts.append(f"**{k}** — examples\n\n" + "\n".join(lines))

    # 6. index presence of never-retrieved site answers
    site_never = [o for o in live if o["class"] == "never" and o["category"] in ("site", "click")]
    if site_never:
        rows = []
        for s in sets:
            xs = [o for o in site_never if o["set"] == s]
            if not xs:
                continue
            un = [o for o in xs if not o.get("seen_elsewhere")]
            idx = Counter(o.get("indexed", "?") for o in un)
            rows.append([s, len(by_set[s]), len(xs), len(xs) - len(un), len(un), idx["absent"], idx["known"], idx["home"], idx["?"]])
        parts.append("### T7. Never-retrieved site answers: is the site in the release?\n\n" + md_table(
            ["set", "n", "never retrieved", "domain came back for another dev query", "domain in no row of any dev query",
             "… release: domain unknown", "… release: name known, no crawled front page", "… release: front page crawled",
             "… release not read"], rows))
        lk = [o for o in live if o["category"] in ("site", "click") and "article:no" in o.get("stratum", "") and o["class"] == "never"
              and not o.get("seen_elsewhere")]
        if lk:
            sl = Counter("not read" if "link_sl" not in o else "no name row" if o["link_sl"] is None else
                         "0 or 1 sitelinks" if o["link_sl"] < 2 else "2 or more sitelinks" for o in lk)
            zero = sum(o.get("link_sl") == 0 for o in lk)
            per = Counter(o["set"] for o in lk if o.get("link_sl") is not None and o["link_sl"] < 2)
            parts.append(f"Link-only sites (stratum article:no) never retrieved and in no row: {len(lk)}. The release's sitelinks "
                         "for the name (the link tier shows a site only from 2): " + ", ".join(f"{k} {v}" for k, v in sl.most_common())
                         + f" (no sitelinks recorded at all: {zero}; under the floor per set: " + ", ".join(f"{k} {v}" for k, v in per.items()) + ").")
    return "\n\n".join(parts) + "\n"


QUERY_COLS = ("set", "query", "category", "expected", "stratum", "entity", "class", "served", "via", "in_lists", "named", "fused",
              "fuzzy", "merged", "ranker", "ce", "final", "owner", "answer_tier", "answer_url", "w_url", "w_source", "w_score",
              "w_tier", "w_flags", "w_lists", "beat", "corrected", "base_query", "base_class", "indexed", "link_sl", "seen_elsewhere", "cause")


def query_line(o: dict) -> str:
    rk, ran = o.get("ranks", {}), o.get("ran", [])
    v = dict(o, served=o["served"] or "-", in_lists=",".join(f"{n}:{k}" for n, k in o.get("in_lists", [])),
             w_flags=",".join(o.get("w_flags", [])), w_lists=",".join(o.get("w_lists", [])),
             seen_elsewhere={True: 1, False: 0}.get(o.get("seen_elsewhere"), ""))
    for s in STEPS:
        v[s] = rk.get(s) or ("-" if s in ran else "")       # "-": the step ran without the answer; empty: it did not run
    return "\t".join(str(v.get(c, "") if v.get(c) is not None else "").replace("\t", " ") for c in QUERY_COLS)


# ---------------------------------------------------------------------------------------------------------------------
# hard negatives

HN_HEADER = "query\tset\tcategory\texpected\tclass\tanswer_url\tanswer_rank\tneg_rank\tneg_url\tneg_source\tneg_score\tneg_tier"


def hard_negatives(r: dict, o: dict, d: dict) -> list[str]:
    """The wrong results above a retrieved answer in the dump's final step. Pure, for tests."""
    if o["class"] not in ("top5", "pool", "list"):
        return []
    cat, exp = r["category"], r["expected"].split("|")
    final = d.get("final", {}).get("rows", [])
    k = o["ranks"].get("final")
    above = final[: k - 1] if k else final[:HARD_NEG_MAX]
    out = []
    for i, x in enumerate(above):
        if eq.hit(cat, exp, x) or x.get("url") == o.get("alias") or not x.get("url") or (
                o.get("accept") and eq.accept_hit(cat, o["accept"], x)):
            continue
        out.append("\t".join(map(str, (r["query"], o["set"], cat, r["expected"], o["class"], o["answer_url"], k or "-", i + 1,
                                       x["url"], x.get("source", ""), x.get("score", ""), tier(x.get("score"))))))
    return out[:HARD_NEG_MAX]


def excluded_keys(path: Path) -> set[str]:
    keys = set()
    if path.exists():
        with open(path, encoding="utf-8") as f:
            f.readline()
            for line in f:
                keys.add(line.split("\t", 1)[0])
    return keys


def write_hard_negatives(path: Path, sets: list[str], dump: Path, recs: list[dict]) -> tuple[int, int, int]:
    """A second streamed pass over the dump: (lines, queries, queries dropped by exclude.tsv)."""
    excl = excluded_keys(C.OPEN / "exclude.tsv")
    by = defaultdict(list)
    for o in recs:
        if o["class"] in ("top5", "pool", "list"):
            by[(o["set"], o["query"])].append(o)
    path.parent.mkdir(parents=True, exist_ok=True)
    n = nq = dropped = 0
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(HN_HEADER + "\n")
        for s in sets:
            want = {q for (st, q) in by if st == s}
            p = dump / f"{s}.jsonl"
            if not want or not p.exists():
                continue
            last = {}
            for q, d in stream_dump(p, want):
                lines = []
                for o in by[(s, q)]:
                    r = {"query": q, "category": o["category"], "expected": o["expected"]}
                    lines += hard_negatives(r, o, d)
                last[q] = lines                           # a re-asked query: its last answer, as the audit used
            for q, lines in last.items():
                if q in excl or C.norm_query(q) in excl or C.norm_key(q) in excl:
                    dropped += 1
                    continue
                if lines:
                    f.write("\n".join(lines) + "\n")
                    n += len(lines)
                    nq += 1
    os.replace(tmp, path)
    return n, nq, dropped


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--build", default="8f651b68abd0", help="the engine build whose candidate dump is read")
    ap.add_argument("--dump", help="the dump directory (default sq/candidates/<build>)")
    ap.add_argument("--arm", default=C.BASELINE, help="the sq-eval arm whose per-query rows give the served rank (name or directory)")
    ap.add_argument("--sets", default="", help="comma-separated subset (default: every open set)")
    ap.add_argument("--release", default="", help="a release folder: read lookup.sqlite to say whether a site answer is indexed")
    ap.add_argument("--out", help="output directory (default sq/recall-audit/<build>)")
    ap.add_argument("--hard-negatives", help="the hard-negatives file (default sq/hard-negatives/<build>.tsv)")
    ap.add_argument("--no-hard-negatives", action="store_true")
    ap.add_argument("--examples", type=int, default=5)
    ap.add_argument("--open", help="the sets directory (default tools/evalsets/open; the unit test's fixture)")
    a = ap.parse_args()
    if a.open:
        C.OPEN = Path(a.open)
    dump = Path(a.dump) if a.dump else C.SQ_DIR / "candidates" / a.build
    if not dump.is_dir():
        sys.exit(f"sq-recall-audit: no candidate dump at {dump}: build it with tools/sq-candidates.py (it needs an engine)")
    arm = Path(a.arm) if os.sep in a.arm else C.SQ_DIR / a.arm
    sets = [s.strip() for s in a.sets.split(",") if s.strip()] or C.all_sets()
    out = Path(a.out) if a.out else C.SQ_DIR / "recall-audit" / a.build
    out.mkdir(parents=True, exist_ok=True)
    index = Index(a.release) if a.release else None
    log(f"sq-recall-audit: dump {dump}, served ranks from {arm}, DEV halves only"
        + (f", index presence from {a.release}" if index else ""))
    recs, info = run(sets, dump, arm, index)
    with open(out / "queries.tsv", "w", encoding="utf-8") as f:
        f.write("\t".join(QUERY_COLS) + "\n")
        f.writelines(query_line(o) + "\n" for o in recs)
    md = tables(recs, info, sets, a.examples)
    if not a.no_hard_negatives:
        hn = Path(a.hard_negatives) if a.hard_negatives else C.SQ_DIR / "hard-negatives" / f"{a.build}.tsv"
        n, nq, dropped = write_hard_negatives(hn, sets, dump, recs)
        md += f"\nHard negatives: {n} rows for {nq} dev queries -> {hn} ({dropped} queries dropped: in exclude.tsv).\n"
    (out / "tables.md").write_text(md, encoding="utf-8")
    print(md)
    log(f"wrote {out / 'queries.tsv'} and {out / 'tables.md'}")


if __name__ == "__main__":
    main()
