#!/usr/bin/env python3
"""How-to demand list from the anchor scan's page titles. Stdlib only (numpy only with --vecs).

    demand.py run --scan ~/search-eval/cc-anchors-2026-39 --out ~/search-eval/howto-demand/<name> --split-half
    demand.py compare A/topics.tsv B/topics.tsv [--top 500] [--allowed]
    demand.py inspect --scan DIR                       rows per finished shard, nothing written

Input: the scan (importer/anchors.py howto_of) writes, per finished shard,
out/shard-NN/howto-titles.tsv.gz — `title TAB registered domain`, the title lower-cased and cut to start
at its "how to / how do i …", one row per (title, site) per WAT file. This tool READS that directory and
never writes into it; a shard is read only when state/done/shard-NN exists and its rc file says 0.

Pipeline:
  1. title -> phrase: cut at the first site separator (" - ", " | ", ": ", "? "), topics.howto_phrase.
  2. phrase -> key (topics.key, the same function the box will run on a query) -> signature: the key's
     stems as a sorted SET with weak modifiers dropped ("best", "own", "homemade", a year). Two titles are
     one topic when their signatures are equal. This is the lexical clustering; it cannot chain.
     topics.cluster's Jaccard rule is NOT used here (measured: single linkage merges "make a cake" with
     every kind of cake). --vecs adds embedding neighbours (tools/embed.py output) as a second merge.
  3. A topic needs 2..9 stems and, after the first (the verb), one that is not vague: "fix it", "get
     started", "find us" are what is left of "<subject>: how to fix it" — the scan keeps a title from
     "how" on, so the subject is gone.
  4. Demand = distinct registered domains over the topic's titles.
  5. categories.Rules on every member title: deny if any member is denied, then health, else the
     canonical member's verdict. Only `allow` is ever generated.
  6. Any topic that is an excluded eval query (evalsets/open/exclude.tsv) is dropped; a bake-off topic is
     kept and flagged (it was used in prompt work, so it must stay out of a how-to held-out set).

Output (--out, outside the repo): topics.tsv (every topic at or above --min-sites, ranked), allowed.tsv,
summary.json, sample-top200.tsv; with --split-half also half-a/, half-b/ and the comparison in summary.json.
Per-shard work is cached under --work and reused while the shard file and this code are unchanged.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import heapq
import itertools
import json
import math
import os
import random
import re
import sys
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import categories  # noqa: E402
import topics  # noqa: E402

VERSION = "3"                      # bump when the per-shard rows change meaning (invalidates the cache)
REPO = os.path.abspath(os.path.join(HERE, "..", "..", "..", ".."))
DEFAULT_EXCLUDE = os.path.join(REPO, "apps", "search", "tools", "evalsets", "open", "exclude.tsv")
DEFAULT_BAKEOFF = os.path.join(HERE, "topics-bakeoff.tsv")
MIN_STEMS, MAX_STEMS = 2, 9

# The first thing that ends the how-to phrase in a page title: a site suffix or a subtitle.
_CUT = re.compile(r"\s+[-–—|·•»~/]+\s+|\s*[|»•]\s*|\s*::\s*|:\s+|[?!]\s+\S|\.\s+\S|\s+[-–—]$")
# Words that add nothing to the task: dropped from the signature only (the key keeps them).
WEAK_WORDS = ("own new best perfect perfectly good great really just actually successfully effectively "
              "efficiently homemade diy yourself myself absolutely exactly finally always ever simply "
              "free freely online now today").split()
# Words that cannot be the object of a task on their own: "how to fix it", "how to find us".
VAGUE_WORDS = ("one ones us here there yourself yourselves myself itself themselves him her his our we he she "
               "something anything everything thing things stuff more most much many less right rid back "
               "started way ways well better best good great new own like not no dont without about how "
               "get got make made use used using need needs know go going want wants take takes do doing done "
               "be being have having also just really first next all every each other another same different "
               "difference now today again too").split()
# A phrase whose object (second word) or last word is one of these lost its subject when the scan cut the
# title at "how": "measure and improve it", "use it safely", "make it work".
DANGLING = frozenset("it them this that these those they".split())
WEAK = frozenset(topics.stem(w) for w in WEAK_WORDS)
VAGUE = frozenset(topics.stem(w) for w in VAGUE_WORDS)
_YEAR = re.compile(r"^(19|20)\d\d$")


def clean_title(title: str) -> str | None:
    """A scan row's title ('how to tie a tie (with pictures) - wikihow') -> the how-to phrase ('tie a
    tie'), or None when nothing is left. Cuts at the FIRST separator, so 'x - blog - site' loses both."""
    t = topics.strip_accents(title or "").replace("’", "'").strip()
    m = _CUT.search(t)
    if m:
        t = t[:m.start()]
    return topics.howto_phrase(t)


def signature(key: str) -> str:
    """The clustering identity of a key: its stems as a sorted set, weak modifiers and years dropped —
    unless that leaves fewer than two stems, then nothing is dropped."""
    st = key.split()
    strong = {s for s in st if s not in WEAK and not _YEAR.match(s)}
    if len(strong) < 2:
        strong = set(st)
    return " ".join(sorted(strong))


def is_topic(key: str) -> bool:
    st = key.split()
    return MIN_STEMS <= len(st) <= MAX_STEMS and any(s not in VAGUE for s in st[1:])


def title_row(title: str):
    """-> (signature, key, phrase) or a reason string: 'not-howto', 'vague'."""
    phrase = clean_title(title)
    if not phrase:
        return "not-howto"
    key = " ".join(topics.stems(phrase))
    w = topics._WORD.findall(phrase.lower().replace("'", ""))
    if not is_topic(key) or (w and (w[-1] in DANGLING or (len(w) > 1 and w[1] in DANGLING))):
        return "vague"
    return signature(key), key, phrase


# --- the scan directory (read only) -------------------------------------------------------------------

def done_shards(scan: str) -> list[tuple[str, str]]:
    """[(shard name, titles path)] for shards marked done whose rc is 0 and whose titles file exists."""
    done_dir = os.path.join(scan, "state", "done")
    out = []
    for name in sorted(os.listdir(done_dir)) if os.path.isdir(done_dir) else []:
        d = os.path.join(scan, "out", name)
        path = os.path.join(d, "howto-titles.tsv.gz")
        try:
            with open(os.path.join(d, "rc"), encoding="ascii") as f:
                rc = f.read().strip()
        except OSError:
            rc = "?"
        if rc == "0" and os.path.isfile(path):
            out.append((name, path))
    return out


def _code_stamp() -> str:
    """What a cached shard file depends on: topics.py and the title rules here — not the report code."""
    h = hashlib.sha1(repr((VERSION, _CUT.pattern, WEAK_WORDS, VAGUE_WORDS, sorted(DANGLING), MIN_STEMS, MAX_STEMS)).encode())
    with open(os.path.join(HERE, "topics.py"), "rb") as f:
        h.update(f.read())
    return h.hexdigest()[:16]


def shard_pairs(args) -> dict:
    """One shard's titles -> WORK/<shard>.pairs.tsv.gz: sorted unique (signature, site) rows with the key,
    phrase and raw title of the first title seen for that pair. Reused when nothing changed."""
    name, path, work, stamp = args
    out = os.path.join(work, f"{name}.pairs.tsv.gz")
    side = out + ".json"
    st = os.stat(path)
    ident = {"src": path, "size": st.st_size, "mtime_ns": st.st_mtime_ns, "code": stamp}
    if os.path.isfile(out) and os.path.isfile(side):
        try:
            with open(side, encoding="utf-8") as f:
                old = json.load(f)
            if old.get("ident") == ident:
                return dict(old["stats"], shard=name, out=out, cached=True)
        except (ValueError, KeyError, OSError):
            pass
    stats = Counter()
    rows: dict[tuple[str, str], tuple[str, str, str]] = {}
    with gzip.open(path, "rt", encoding="utf-8", errors="replace", newline="\n") as f:
        for line in f:
            parts = line.rstrip("\n").split("\t")
            stats["rows"] += 1
            if len(parts) != 2 or not parts[1]:
                stats["malformed"] += 1
                continue
            r = title_row(parts[0])
            if isinstance(r, str):
                stats[r] += 1
                continue
            sig, key, phrase = r
            if (sig, parts[1]) in rows:
                stats["repeat"] += 1
            else:
                rows[(sig, parts[1])] = (key, phrase, parts[0].replace("\t", " "))
    stats["pairs"] = len(rows)
    tmp = out + f".part{os.getpid()}"
    with gzip.open(tmp, "wt", encoding="utf-8", newline="\n", compresslevel=3) as g:
        for (sig, site) in sorted(rows):
            g.write("\t".join((sig, site) + rows[(sig, site)]) + "\n")
    os.replace(tmp, out)
    with open(side, "w", encoding="utf-8") as f:
        json.dump({"ident": ident, "stats": dict(stats)}, f)
    return dict(stats, shard=name, out=out, cached=False)


def _read_pairs(path: str):
    with gzip.open(path, "rt", encoding="utf-8", newline="\n") as f:
        for line in f:
            p = line.rstrip("\n").split("\t")
            if len(p) == 5:
                yield p


# --- topics -------------------------------------------------------------------------------------------

def _topic(sig: str, rows: list[list[str]]) -> dict:
    by_key: dict[str, set] = defaultdict(set)
    phrases: dict[str, Counter] = defaultdict(Counter)
    title_of: dict[str, str] = {}
    for _sig, site, key, phrase, title in rows:
        by_key[key].add(site)
        phrases[key][phrase.lower()] += 1
        if site not in title_of or title < title_of[site]:
            title_of[site] = title
    members = sorted(by_key, key=lambda k: (-len(by_key[k]), len(k), k))
    canon = members[0]
    phrase = sorted(phrases[canon].items(), key=lambda kv: (-kv[1], len(kv[0]), kv[0]))[0][0]
    return {"sig": sig, "key": canon, "topic": "how to " + phrase, "sites": set(title_of),
            "members": [(k, len(by_key[k]), sorted(phrases[k].items(), key=lambda kv: (-kv[1], len(kv[0]), kv[0]))[0][0])
                        for k in members],
            "titles": title_of}


def merge(pair_files: list[str], min_sites: int = 2):
    """Streams the sorted per-shard files together -> (topics at or above min_sites, counters)."""
    stats = Counter()
    out = []
    stream = heapq.merge(*(_read_pairs(p) for p in pair_files), key=lambda r: (r[0], r[1]))
    for sig, group in itertools.groupby(stream, key=lambda r: r[0]):
        rows = list(group)
        n = len({r[1] for r in rows})
        stats["signatures"] += 1
        stats["pairs"] += n
        for floor in (2, 3, 5, 10, 20):
            if n >= floor:
                stats[f"topics_sites>={floor}"] += 1
        if n >= min_sites:
            out.append(_topic(sig, rows))
    return out, stats


def merge_pairs(tops: list[dict], pairs) -> list[dict]:
    """Union topics joined by (i, j) index pairs (embedding neighbours): sites are re-counted over the union."""
    parent = list(range(len(tops)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    for i, j in pairs:
        if i < len(tops) and j < len(tops):
            ri, rj = find(i), find(j)
            if ri != rj:
                parent[max(ri, rj)] = min(ri, rj)
    groups: dict[int, list[int]] = defaultdict(list)
    for i in range(len(tops)):
        groups[find(i)].append(i)
    out = []
    for idx in groups.values():
        idx.sort(key=lambda i: (-len(tops[i]["sites"]), tops[i]["key"]))
        head = dict(tops[idx[0]])
        if len(idx) > 1:
            head["sites"] = set().union(*(tops[i]["sites"] for i in idx))
            head["members"] = [m for i in idx for m in tops[i]["members"]]
            titles = {}
            for i in reversed(idx):
                titles.update(tops[i]["titles"])
            head["titles"] = titles
        out.append(head)
    return out


class Excluded:
    """evalsets/open/exclude.tsv: a topic is excluded when a member phrase, with or without 'how to', is an
    excluded query (form=query), or its letters-and-digits form is (form=norm), or an excluded query has
    the same topic key."""

    def __init__(self, path: str | None):
        self.queries, self.norms, self.keys = set(), set(), set()
        self.rows = 0
        if not path:
            return
        with open(path, encoding="utf-8", newline="") as f:
            for row in csv.reader(f, delimiter="\t", quoting=csv.QUOTE_NONE):
                if not row or row[0].startswith("#") or len(row) < 2 or row[:2] == ["key", "form"]:
                    continue
                self.rows += 1
                if row[1] == "norm":
                    self.norms.add(row[0])
                else:
                    q = " ".join(row[0].lower().split())
                    self.queries.add(q)
                    k = topics.key(q)
                    if is_topic(k):
                        self.keys.add(k)

    @staticmethod
    def norm(text: str) -> str:
        return re.sub(r"[^a-z0-9]", "", topics.strip_accents(text).lower())

    def hit(self, top: dict) -> str | None:
        for key, _n, phrase in top["members"]:
            if key in self.keys:
                return f"key:{key}"
            for q in (phrase, "how to " + phrase):
                if q in self.queries:
                    return f"query:{q}"
                if self.norm(q) in self.norms:
                    return f"norm:{self.norm(q)}"
        return None


def read_bakeoff(path: str | None) -> dict[str, str]:
    """signature -> bake-off topic."""
    out = {}
    if path and os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                if line.strip() and not line.startswith("#"):
                    t = line.split("\t")[0].strip()
                    out[signature(topics.key(t))] = t
    return out


class FastRules:
    """categories.Rules applied to a topic KEY (already stemmed), with the rules indexed by their first
    stem: the same verdict precedence and file order as Rules.classify, without re-keying the text or
    scanning 800 patterns per topic."""

    def __init__(self, rules: categories.Rules):
        self.index: dict[str, dict[str, list]] = {v: defaultdict(list) for v in categories.VERDICTS}
        for verdict in categories.VERDICTS:
            for n, (_v, category, pat, raw) in enumerate(rules.by_verdict[verdict]):
                self.index[verdict][pat[0]].append((n, category, pat, raw))
        self.cache: dict[str, tuple[str, str, str]] = {}

    def classify_key(self, key: str) -> tuple[str, str, str]:
        hit = self.cache.get(key)
        if hit is None:
            hit = self.cache[key] = self._classify(key.split())
        return hit

    def _classify(self, st: list[str]) -> tuple[str, str, str]:
        present = set(st)
        for verdict in categories.VERDICTS:
            idx = self.index[verdict]
            best = None
            for s in present:
                for n, category, pat, raw in idx.get(s, ()):
                    if (best is None or n < best[0]) and categories.Rules._contains(st, pat):
                        best = (n, category, raw)
            if best is not None:
                return (categories.RETURNED.get(verdict, verdict), best[1], best[2])
        return ("unknown", "", "")


def classify(top: dict, rules) -> tuple[str, str, str]:
    """Deny wins over health wins over the canonical member's verdict, across every member key."""
    if not isinstance(rules, FastRules):
        rules = FastRules(rules)
    seen = [rules.classify_key(key) for key, _n, _phrase in top["members"]]
    for verdict in ("deny", "health"):
        for v in seen:
            if v[0] == verdict:
                return v
    return seen[0]


def build(pair_files: list[str], rules, excluded: Excluded, bakeoff: dict[str, str],
          min_sites: int = 2, vec_pairs=None) -> tuple[list[dict], dict]:
    if not isinstance(rules, FastRules):
        rules = FastRules(rules)
    tops, stats = merge(pair_files, min_sites)
    kept, dropped = [], []
    for t in tops:
        why = excluded.hit(t)
        if why:
            dropped.append((t["topic"], len(t["sites"]), why))
        else:
            kept.append(t)
    if vec_pairs is not None:                       # ids = row numbers of a previous run's keys.tsv
        kept.sort(key=lambda t: t["sig"])
        if any(i >= len(kept) or j >= len(kept) for i, j in vec_pairs):
            raise SystemExit("--vecs was made from a different run's keys.tsv (an id is past the last topic)")
        before = len(kept)
        kept = merge_pairs(kept, vec_pairs)
        stats["merged_by_embedding"] = before - len(kept)
    for t in kept:
        t["verdict"], t["category"], t["rule"] = classify(t, rules)
        t["bakeoff"] = any(signature(k) in bakeoff for k, _n, _p in t["members"])
    kept.sort(key=lambda t: (-len(t["sites"]), t["key"]))
    n_allowed = 0
    for n, t in enumerate(kept, 1):
        t["rank"] = n
        if t["verdict"] == "allow":
            n_allowed += 1
            t["allowed_rank"] = n_allowed
    stats["topics"] = len(kept)
    stats["excluded_dropped"] = len(dropped)
    by_v = Counter(t["verdict"] for t in kept)
    by_c = Counter((t["verdict"], t["category"]) for t in kept)
    summary = {"counts": dict(stats), "verdicts": dict(by_v),
               "categories": {f"{v}/{c or '-'}": n for (v, c), n in sorted(by_c.items(), key=lambda kv: -kv[1])},
               "allowed_by_floor": {str(f): sum(1 for t in kept if t["verdict"] == "allow" and len(t["sites"]) >= f)
                                    for f in (2, 3, 5, 10, 20, 50)},
               "topics_by_floor": {str(f): sum(1 for t in kept if len(t["sites"]) >= f) for f in (2, 3, 5, 10, 20, 50)},
               "excluded": [{"topic": a, "sites": b, "why": c} for a, b, c in sorted(dropped, key=lambda d: -d[1])]}
    return kept, summary


COLUMNS = ("rank", "allowed_rank", "topic", "sites", "verdict", "category", "rule", "key", "members", "bakeoff",
           "example_titles", "example_sites")


def row_of(t: dict, titles: bool = True) -> list[str]:
    sites = sorted(t["sites"])
    ex = [t["titles"][s] for s in sites[:3]] if titles else [p for _k, _n, p in t["members"][:3]]
    return [str(t["rank"]), str(t.get("allowed_rank", "")), t["topic"], str(len(sites)), t["verdict"], t["category"],
            t["rule"], t["key"], "|".join(f"{k}:{n}" for k, n, _p in t["members"][:8]), "1" if t["bakeoff"] else "",
            " || ".join(ex), " ".join(sites[:5])]


def write_tsv(path: str, tops: list[dict], titles: bool = True, header: str = "") -> None:
    tmp = path + ".part"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        if header:
            f.write(header)
        f.write("\t".join(COLUMNS) + "\n")
        for t in tops:
            f.write("\t".join(c.replace("\t", " ").replace("\n", " ") for c in row_of(t, titles)) + "\n")
    os.replace(tmp, path)


# --- stability ----------------------------------------------------------------------------------------

def _spearman(xs: list[float], ys: list[float]) -> float:
    def ranks(v):
        order = sorted(range(len(v)), key=lambda i: v[i])
        r = [0.0] * len(v)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and v[order[j + 1]] == v[order[i]]:
                j += 1
            for k in range(i, j + 1):
                r[order[k]] = (i + j) / 2 + 1
            i = j + 1
        return r
    if len(xs) < 3:
        return float("nan")
    rx, ry = ranks(xs), ranks(ys)
    mx, my = sum(rx) / len(rx), sum(ry) / len(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den = (sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry)) ** 0.5
    return num / den if den else float("nan")


def compare(a: list[tuple[str, int]], b: list[tuple[str, int]], tops=(50, 100, 200, 500)) -> dict:
    """Two ranked lists of (signature, sites), best first -> overlap of the top N and the rank correlation
    of site counts over the union of the two top-N sets (a topic missing from one side counts 0 there)."""
    da, db = dict(a), dict(b)
    out = {"a_topics": len(a), "b_topics": len(b)}
    for n in tops:
        ta, tb = [s for s, _ in a[:n]], [s for s, _ in b[:n]]
        if len(ta) < n or len(tb) < n:
            continue
        union = sorted(set(ta) | set(tb))
        out[f"top{n}"] = {"overlap": round(len(set(ta) & set(tb)) / n, 3),
                          "a_top_seen_in_b_at_all": round(sum(1 for s in ta if s in db) / n, 3),
                          "spearman_sites_union": round(_spearman([da.get(s, 0) for s in union], [db.get(s, 0) for s in union]), 3),
                          "nth_sites_a": a[n - 1][1], "nth_sites_b": b[n - 1][1]}
    return out


def _ranked(tops: list[dict], allowed: bool) -> list[tuple[str, int]]:
    return [(t["sig"], len(t["sites"])) for t in tops if not allowed or t["verdict"] == "allow"]


def read_ranked(path: str, allowed: bool) -> list[tuple[str, int]]:
    out = []
    with open(path, encoding="utf-8", newline="") as f:
        rows = csv.reader((line for line in f if not line.startswith("#")), delimiter="\t", quoting=csv.QUOTE_NONE)
        head = next(rows)
        ix = {c: n for n, c in enumerate(head)}
        for r in rows:
            if not allowed or r[ix["verdict"]] == "allow":
                out.append((signature(r[ix["key"]]), int(r[ix["sites"]])))
    return out


def growth(pair_files: list[str], rules, excluded, bakeoff, seed: int = 483, orders: int = 3) -> list[dict]:
    """Topics above each floor as shards are added, averaged over a few shard orders: the curve the
    full-crawl estimate is read from."""
    n = len(pair_files)
    ks = sorted({k for k in (1, 2, 3, 4, 6, 8, 12, 18, 24, 30, 36) if k <= n} | {n})
    rng = random.Random(seed)
    acc: dict[int, list[dict]] = defaultdict(list)
    for o in range(orders if n > 1 else 1):
        order = list(pair_files)
        if o:
            rng.shuffle(order)
        for k in ks:
            if k == n and acc[k]:
                continue
            tops, summ = build(order[:k], rules, excluded, bakeoff, 2)
            acc[k].append({"allowed": summ["allowed_by_floor"], "topics": summ["topics_by_floor"],
                           "top100_allowed_mean_sites": round(sum(s for _g, s in _ranked(tops, True)[:100]) / 100, 2)})
    out = []
    for k in ks:
        runs = acc[k]
        row = {"shards": k, "orders": len(runs)}
        for what in ("allowed", "topics"):
            for f in ("2", "3", "5", "10", "20"):
                row[f"{what}>={f}"] = round(sum(r[what][f] for r in runs) / len(runs))
        row["top100_allowed_mean_sites"] = round(sum(r["top100_allowed_mean_sites"] for r in runs) / len(runs), 2)
        out.append(row)
    return out


def extrapolate(curve: list[dict], col: str, to: int = 36) -> dict | None:
    """Power law N(k) = a * k^b through the last two points of the curve, and through the first and last —
    two readings, because the exponent falls as shards are added (a site seen in one shard is seen again)."""
    pts = [(r["shards"], r[col]) for r in curve if r[col] > 0]
    if len(pts) < 2:
        return None
    def fit(p, q):
        b = math.log(q[1] / p[1]) / math.log(q[0] / p[0])
        return {"exponent": round(b, 3), "at": to, "estimate": round(q[1] * (to / q[0]) ** b)}
    out = {"last_two": fit(pts[-2], pts[-1]), "first_last": fit(pts[0], pts[-1]), "have": pts[-1][1], "shards": pts[-1][0]}
    if len(pts) >= 4:          # how wrong the same method was inside the data: predict the last point without it
        mid = len(pts) // 2
        b = math.log(pts[mid][1] / pts[mid - 1][1]) / math.log(pts[mid][0] / pts[mid - 1][0])
        pred = pts[mid][1] * (pts[-1][0] / pts[mid][0]) ** b
        out["backtest"] = {"from_shards": [pts[mid - 1][0], pts[mid][0]], "predicted": round(pred), "actual": pts[-1][1],
                           "ratio": round(pred / pts[-1][1], 3)}
    return out


# --- CLI ----------------------------------------------------------------------------------------------

def _prepare(scan: str, work: str, only: list[str] | None, jobs: int):
    shards = done_shards(scan)
    if only:
        shards = [s for s in shards if s[0] in only or s[0].replace("shard-", "") in only]
    if not shards:
        raise SystemExit(f"no finished shard with a titles file under {scan} (state/done + rc 0)")
    real_scan, real_work = os.path.realpath(scan), os.path.realpath(work)
    if real_work == real_scan or real_work.startswith(real_scan + os.sep):
        raise SystemExit("--work/--out must not be inside the scan directory (it belongs to the running job)")
    os.makedirs(work, exist_ok=True)
    stamp = _code_stamp()
    jobs_in = [(name, path, work, stamp) for name, path in shards]
    if jobs > 1 and len(jobs_in) > 1:
        with ProcessPoolExecutor(max_workers=min(jobs, len(jobs_in))) as ex:
            res = list(ex.map(shard_pairs, jobs_in))
    else:
        res = [shard_pairs(j) for j in jobs_in]
    return res


def _write_run(out: str, tops: list[dict], summary: dict, note: str) -> None:
    os.makedirs(out, exist_ok=True)
    head = f"# {note}\n"
    write_tsv(os.path.join(out, "topics.tsv"), tops, True, head)
    write_tsv(os.path.join(out, "allowed.tsv"), [t for t in tops if t["verdict"] == "allow"], True, head)
    write_tsv(os.path.join(out, "sample-top200.tsv"), tops[:200], False,
              head + "# top 200 of topics.tsv; example_titles here are the cleaned phrases, not page titles\n")
    if "merged_by_embedding" not in summary["counts"]:
        with open(os.path.join(out, "keys.tsv"), "w", encoding="utf-8") as f:   # row number = id for tools/embed.py
            for t in sorted(tops, key=lambda t: t["sig"]):
                f.write(t["topic"] + "\n")
    with open(os.path.join(out, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=1, sort_keys=True)
        f.write("\n")


def _md_rows(tops: list[dict], cols=("rank", "topic", "sites", "verdict", "category", "rule", "members")) -> list[str]:
    out = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for t in tops:
        cell = {"rank": t["rank"], "topic": t["topic"], "sites": len(t["sites"]), "verdict": t["verdict"],
                "category": t["category"] or "-", "rule": t["rule"] or "-",
                "members": ", ".join(f"{p} ({n})" for _k, n, p in t["members"][:4])}
        out.append("| " + " | ".join(str(cell[c]).replace("|", "/") for c in cols) + " |")
    return out


def render_md(tops: list[dict], S: dict, seed: int = 483) -> str:
    """The checked-in summary: every number and sample comes from this run; prose lives in the README."""
    rng = random.Random(seed)
    c, n = S["counts"], len(tops)
    allowed = [t for t in tops if t["verdict"] == "allow"]
    denied = [t for t in tops if t["verdict"] in ("deny", "health")]
    unknown = [t for t in tops if t["verdict"] == "unknown"]
    per = S["per_shard"]
    tot = lambda k: sum(p.get(k, 0) for p in per)
    L = [f"# How-to demand list — {'PARTIAL, ' if len(S['shards']) < S['shards_of'] else ''}{len(S['shards'])} of {S['shards_of']} shards", "",
         "Generated by `demand.py run --publish` — do not edit; rerun instead. Prose and caveats: `../README.md` §2d.",
         f"Shards: {', '.join(S['shards'])}. Clustering: {S['clustering']}. Demand = distinct registered domains whose page title asks the topic.", "",
         "## Counts", "", "| | |", "|---|---|",
         f"| title rows in (title, site per WAT file) | {S['titles_in']:,} |",
         f"| no how-to phrase left after cleaning | {tot('not-howto'):,} |",
         f"| vague (subject lost, one stem, or over {MAX_STEMS} stems) | {tot('vague'):,} |",
         f"| same (topic, site) seen again inside a shard | {tot('repeat'):,} |",
         f"| distinct (topic, site) pairs over all shards | {c['pairs']:,} |",
         f"| topic signatures (clusters), any site count | {c['signatures']:,} |",
         f"| **topics with >= {S['min_sites']} distinct sites** | **{n + c['excluded_dropped']:,}** |",
         f"| dropped: an excluded eval query ({S['exclude_rows']:,} exclusion rows read) | {c['excluded_dropped']} |",
         f"| **topics out** | **{n:,}** |", "",
         "## Verdicts", "", "| verdict / category | topics | share |", "|---|---|---|"]
    for v in ("allow", "deny", "health", "unknown"):
        L.append(f"| **{v}** | {S['verdicts'].get(v, 0):,} | {S['verdicts'].get(v, 0) / max(n, 1):.1%} |")
    for k, v in S["categories"].items():
        L.append(f"| {k} | {v:,} | {v / max(n, 1):.1%} |")
    L += ["", "## Topics above a distinct-site floor", "", "| floor | all topics | allowed |", "|---|---|---|"]
    for f in ("2", "3", "5", "10", "20", "50"):
        L.append(f"| >= {f} | {S['topics_by_floor'][f]:,} | {S['allowed_by_floor'][f]:,} |")
    if "split_half" in S:
        sh = S["split_half"]
        L += ["", "## Stability: one half of the shards against the other", "",
              f"Half A = {', '.join(sh['a_shards'])}; half B = {', '.join(sh['b_shards'])}; each half run through the whole pipeline on its own.",
              "Overlap = share of A's top N that is in B's top N. Spearman = rank correlation of site counts over the union of the two top-N sets.", "",
              "| list | top N | overlap | A's top N seen in B at all | Spearman | sites at rank N (A / B) |", "|---|---|---|---|---|---|"]
        for name in ("allowed", "all"):
            for k in ("top50", "top100", "top200", "top500"):
                if k in sh[name]:
                    r = sh[name][k]
                    L.append(f"| {name} | {k[3:]} | {r['overlap']:.0%} | {r['a_top_seen_in_b_at_all']:.0%} | {r['spearman_sites_union']:.2f} | {r['nth_sites_a']} / {r['nth_sites_b']} |")
    if "growth" in S:
        L += ["", "## Growth as shards are added, and the 36-shard reading", "",
              "Mean over shard orders.", "",
              "| shards | topics >=2 | allowed >=2 | allowed >=3 | allowed >=5 | allowed >=10 | mean sites, top 100 allowed |", "|---|---|---|---|---|---|---|"]
        for g in S["growth"]:
            L.append(f"| {g['shards']} | {g['topics>=2']:,} | {g['allowed>=2']:,} | {g['allowed>=3']:,} | {g['allowed>=5']:,} | {g['allowed>=10']:,} | {g['top100_allowed_mean_sites']} |")
        L += ["", "Power-law extrapolation N(k) = a·k^b to 36 shards, two readings (the last two points; the first and the last).",
              "The exponent is still falling as shards are added, so both are upper readings — see the README.", "",
              "Backtest = the same last-two-points method applied inside the data: the final point predicted from two earlier ones, as predicted / actual.", "",
              "| allowed topics at floor | have | last two points: exponent, estimate | first and last: exponent, estimate | backtest |", "|---|---|---|---|---|"]
        for col, e in S["extrapolation_36"].items():
            if e:
                bt = e.get("backtest")
                bts = f"{bt['predicted']:,} / {bt['actual']:,} = {bt['ratio']} (from shards {bt['from_shards'][0]} and {bt['from_shards'][1]})" if bt else "-"
                L.append(f"| {col[7:]} | {e['have']:,} | {e['last_two']['exponent']}, {e['last_two']['estimate']:,} | {e['first_last']['exponent']}, {e['first_last']['estimate']:,} | {bts} |")
    b = S["bakeoff"]
    L += ["", "## Eval exclusions and the bake-off topics", "",
          f"Dropped as excluded eval queries: {c['excluded_dropped']}." + ("" if not S["excluded"] else " " + "; ".join(f"{e['topic']} ({e['sites']} sites, {e['why']})" for e in S["excluded"][:40])),
          "", f"Bake-off topics ({b['topics']}): {len(b['in_list'])} are in the list and flagged `bakeoff=1` (keep them out of a how-to held-out set); "
          f"{len(b['not_in_list'])} are not in it yet; {len(b['excluded'])} are excluded eval queries.",
          "Not in the list: " + "; ".join(b["not_in_list"]) + "."]
    L += ["", "## Top 50 allowed", ""] + _md_rows(allowed[:50])
    mid = allowed[len(allowed) // 5: (len(allowed) * 3) // 5]
    L += ["", f"## 30 random mid-rank allowed (allowed ranks {len(allowed) // 5 + 1:,}–{(len(allowed) * 3) // 5:,}, seed {seed})", ""]
    L += _md_rows(sorted(rng.sample(mid, min(30, len(mid))), key=lambda t: t["rank"]))
    L += ["", "## 30 denied or health-routed: the top 10 and 20 at random", ""]
    L += _md_rows(denied[:10] + sorted(rng.sample(denied[10:], min(20, max(0, len(denied) - 10))), key=lambda t: t["rank"]))
    L += ["", "## Top 30 unknown (no rule, or outside the allowed categories — never generated)", ""] + _md_rows(unknown[:30])
    return "\n".join(L) + "\n"


def cmd_run(a) -> int:
    scan = os.path.expanduser(a.scan)
    out = os.path.expanduser(a.out)
    work = os.path.expanduser(a.work) if a.work else os.path.join(os.path.dirname(out.rstrip("/")) or ".", "work")
    real_scan, real_out = os.path.realpath(scan), os.path.realpath(out)
    if real_out == real_scan or real_out.startswith(real_scan + os.sep):
        raise SystemExit("--out must not be inside the scan directory")
    res = _prepare(scan, work, a.shards.split(",") if a.shards else None, a.jobs)
    files = [r["out"] for r in res]
    rules = FastRules(categories.Rules.load(a.rules))
    excluded = Excluded(None if a.exclude == "none" else a.exclude)
    bakeoff = read_bakeoff(a.bakeoff)
    vec_pairs = None
    if a.vecs:
        _sim, vec_pairs = topics._cos_sim(a.vecs, a.cos)
    tops, summary = build(files, rules, excluded, bakeoff, a.min_sites, vec_pairs)
    titles_in = sum(r["rows"] for r in res)
    summary.update({"shards": [r["shard"] for r in res], "shards_of": 36, "titles_in": titles_in,
                    "per_shard": [{k: r.get(k, 0) for k in ("shard", "rows", "malformed", "not-howto", "vague", "repeat", "pairs")}
                                  for r in res],
                    "clustering": "signature (lexical)" + (f" + embedding cos>={a.cos}" if a.vecs else ""),
                    "min_sites": a.min_sites, "exclude_rows": excluded.rows,
                    "bakeoff": {"topics": len(bakeoff),
                                "in_list": sorted(bakeoff[signature(k)] for t in tops for k, _n, _p in t["members"]
                                                  if signature(k) in bakeoff),
                                "excluded": [bt for sig, bt in sorted(bakeoff.items())
                                             if excluded.hit({"members": [(topics.key(bt), 0, topics.howto_phrase(bt) or bt)]})]}})
    seen = set(summary["bakeoff"]["in_list"])
    summary["bakeoff"]["in_list"] = sorted(seen)
    summary["bakeoff"]["not_in_list"] = sorted(set(bakeoff.values()) - seen)
    note = f"how-to demand, PARTIAL: {len(res)} of 36 shards ({', '.join(r['shard'] for r in res)}); sites = distinct registered domains"
    if a.split_half and len(files) >= 2:
        halves = (files[0::2], files[1::2])
        runs = []
        for name, fs in zip(("half-a", "half-b"), halves):
            ht, hs = build(fs, rules, excluded, bakeoff, a.min_sites)
            hs["shards"] = [os.path.basename(f).split(".")[0] for f in fs]
            _write_run(os.path.join(out, name), ht, hs, f"how-to demand, split half {name}: {', '.join(hs['shards'])}")
            runs.append(ht)
        summary["split_half"] = {"a_shards": [os.path.basename(f).split(".")[0] for f in halves[0]],
                                 "b_shards": [os.path.basename(f).split(".")[0] for f in halves[1]],
                                 "allowed": compare(_ranked(runs[0], True), _ranked(runs[1], True)),
                                 "all": compare(_ranked(runs[0], False), _ranked(runs[1], False))}
    if a.growth:
        curve = growth(files, rules, excluded, bakeoff)
        summary["growth"] = curve
        summary["extrapolation_36"] = {c: extrapolate(curve, c) for c in ("allowed>=2", "allowed>=3", "allowed>=5", "allowed>=10")}
    _write_run(out, tops, summary, note)
    md = render_md(tops, summary)
    with open(os.path.join(out, "DEMAND.md"), "w", encoding="utf-8") as f:
        f.write(md)
    if a.publish:
        os.makedirs(a.publish, exist_ok=True)
        with open(os.path.join(a.publish, "DEMAND.md"), "w", encoding="utf-8") as f:
            f.write(md)
        with open(os.path.join(out, "sample-top200.tsv"), encoding="utf-8") as src, \
                open(os.path.join(a.publish, "sample-top200.tsv"), "w", encoding="utf-8") as dst:
            dst.write(src.read())
    c = summary["counts"]
    print(f"shards {len(res)}/36  titles in {titles_in:,}  signatures {c['signatures']:,}  topics (>= {a.min_sites} sites) {c['topics']:,}"
          f"  allowed {summary['verdicts'].get('allow', 0):,}  excluded-dropped {c['excluded_dropped']}  -> {out}")
    return 0


def cmd_inspect(a) -> int:
    for name, path in done_shards(os.path.expanduser(a.scan)):
        rows = sites = 0
        seen = set()
        with gzip.open(path, "rt", encoding="utf-8", errors="replace") as f:
            for line in f:
                rows += 1
                seen.add(line.rstrip("\n").rpartition("\t")[2])
        sites = len(seen)
        print(f"{name}\t{rows} rows\t{sites} distinct sites\t{os.path.getsize(path)} bytes")
    return 0


def cmd_compare(a) -> int:
    print(json.dumps(compare(read_ranked(a.a, a.allowed), read_ranked(a.b, a.allowed), (50, 100, 200, a.top)), indent=1))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--scan", required=True, help="the anchor scan's directory (read only)")
    r.add_argument("--out", required=True, help="results directory, outside the repo and outside --scan")
    r.add_argument("--work", help="per-shard cache (default: <out>/../work)")
    r.add_argument("--shards", help="comma list (02,04 or shard-02) — default every finished shard")
    r.add_argument("--min-sites", type=int, default=2)
    r.add_argument("--jobs", type=int, default=4)
    r.add_argument("--rules", default=categories.DEFAULT_TSV)
    r.add_argument("--exclude", default=DEFAULT_EXCLUDE, help="evalsets/open/exclude.tsv, or 'none'")
    r.add_argument("--bakeoff", default=DEFAULT_BAKEOFF)
    r.add_argument("--split-half", action="store_true", help="also run alternate shards as two halves and compare")
    r.add_argument("--growth", action="store_true", help="topics above each floor as shards are added + the 36-shard estimate")
    r.add_argument("--publish", help="also write DEMAND.md and sample-top200.tsv here (the checked-in copy: howto/demand)")
    r.add_argument("--vecs", help="tools/embed.py output prefix over a previous run's keys.tsv (needs numpy)")
    r.add_argument("--cos", type=float, default=0.90)
    r.set_defaults(fn=cmd_run)
    i = sub.add_parser("inspect")
    i.add_argument("--scan", required=True)
    i.set_defaults(fn=cmd_inspect)
    c = sub.add_parser("compare")
    c.add_argument("a")
    c.add_argument("b")
    c.add_argument("--top", type=int, default=500)
    c.add_argument("--allowed", action="store_true")
    c.set_defaults(fn=cmd_compare)
    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
