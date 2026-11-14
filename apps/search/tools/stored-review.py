#!/usr/bin/env python3
"""Review the stored searches (2026-11-04): flag likely defects in each stored result list, count them by source,
and draw the human review sample the warmed-names report judges.

Input: the report's two files, names.json ([query, tier, source] per name) and hits.json ({"rows": [...]} aligned,
each row the first 8 results {t title, u url, s source list, c snippet}, card {t, u}, n results). Only titles, URLs,
snippets and the card are read: what a person sees.

    stored-review.py counts NAMES HITS                 a table of flags x source (stdout)
    stored-review.py flags  NAMES HITS > flags.json    per name: [flag, ...] and the flagged result positions
    stored-review.py sample NAMES HITS --per 20 --controls 200 [--seed N] > sample.json

Flags (precision checked by hand on 12-row samples, 2026-11-04: ~7 in 10 for the first two, ~9 in 10 the rest):
  gov_offtopic   a .gov/.edu/.mil result whose title and host share no word with the query (slot filler)
  no_overlap     multi-word query; a top-3 result shares no word with the query or the card in title or snippet
  dup_site       one site twice: its root and a language root (/en), or a cdn./static. host of it
  host_3plus     one site 3 or more times in the first 8
  old_season     a "YYYY-YY ... season" article from before 2024 in the top 3
  adult_lookalike  an adult-looking host for a query that is not about adult content
  adult_query    the query names adult content (reviewed separately:)
  no_card        no card, for sources where one is expected (not shopping or weather)
  short          7 results or fewer
"""
from __future__ import annotations

import argparse
import collections
import json
import random
import re
import sys
from urllib.parse import urlsplit

STOP = set("the a an of and in on for to de la le el los las der die das von van du des et y s".split())
ADULT = re.compile(r"xnxx|xvideos|xhamster|pornhub|porn|xxx|\bsex|hentai|onlyfans|redtube|youporn|brazzers|chaturbate|livecams", re.I)
CDN = re.compile(r"^(cdn|static|assets|img|images|media)\d*\.", re.I)
LANG_ROOT = re.compile(r"^/(?:[a-z]{2}(?:[-_][a-z]{2})?)?/?$", re.I)
SEASON = re.compile(r"\b(1[89]\d\d|20\d\d)[–-](\d{2,4})\b.*\bseason\b", re.I)
NO_CARD_OK = {"amazonqac", "weather"}
LABELS = {
    "gov_offtopic": "Off-topic .gov/.edu page", "no_overlap": "Top-3 result shares no word with the search",
    "dup_site": "Same site twice (/en, cdn.)", "host_3plus": "One site 3+ times", "old_season": "Old season article in top 3",
    "adult_lookalike": "Adult look-alike host", "adult_query": "Adult search", "no_card": "No card",
    "short": "7 results or fewer",
}


def toks(s: str) -> set[str]:
    return {t for t in re.findall(r"[\w']+", (s or "").lower()) if t not in STOP}


def host(u: str) -> str:
    h = (urlsplit(u or "").hostname or "").lower()
    return h[4:] if h.startswith("www.") else h


def site(h: str) -> str:
    """A crude registrable domain: example.co.uk, example.com."""
    p = h.split(".")
    if len(p) >= 3 and p[-2] in ("co", "com", "org", "ac", "gov", "net", "edu") and len(p[-1]) == 2:
        return ".".join(p[-3:])
    return ".".join(p[-2:]) if len(p) >= 2 else h


def detect(query: str, source: str, row: dict) -> tuple[list[str], dict[str, list[int]]]:
    """(flags, {flag: [result positions]}) for one stored search."""
    res = row.get("r") or []
    q = toks(query)
    card = row.get("card") or {}
    ctoks = toks(card.get("t", ""))
    out: dict[str, list[int]] = {}

    def add(f, pos=None):
        out.setdefault(f, [])
        if pos is not None and pos not in out[f]:
            out[f].append(pos)

    hosts = [host(x.get("u", "")) for x in res]
    sites = [site(h) for h in hosts]
    for j, x in enumerate(res[:8]):
        h = hosts[j]
        if h.endswith((".gov", ".edu", ".mil", ".gov.uk")) and not (toks(x.get("t")) & q) \
                and not any(t in h for t in q if len(t) > 3):
            add("gov_offtopic", j)
    if len(q) >= 2:
        for j, x in enumerate(res[:3]):
            if not ((q | ctoks) & toks(x.get("t", "") + " " + x.get("c", ""))):
                add("no_overlap", j)
    roots: set[str] = set()                      # sites whose home page (or a language root of it) is already listed
    for j, (h, s) in enumerate(zip(hosts, sites)):
        if not s or s == "wikipedia.org":
            continue
        if CDN.match(h):                         # a CDN or asset host serving pages is never a result of its own
            add("dup_site", j)
        elif LANG_ROOT.match(urlsplit(res[j].get("u", "")).path or "/"):
            if s in roots:
                add("dup_site", j)
            roots.add(s)
    c = collections.Counter(s for s in sites if s and s != "wikipedia.org")
    for s, k in c.items():
        if k >= 3:
            for j, t in enumerate(sites):
                if t == s:
                    add("host_3plus", j)
    for j, x in enumerate(res[:3]):
        m = SEASON.search(x.get("t", ""))
        if m and int(m.group(1)) < 2024:
            add("old_season", j)
    if ADULT.search(query):
        add("adult_query")
    else:
        for j, h in enumerate(hosts):
            if ADULT.search(h):
                add("adult_lookalike", j)
    if not card.get("t") and source not in NO_CARD_OK:
        add("no_card")
    if (row.get("n") or 0) <= 7:
        add("short")
    return sorted(out, key=list(LABELS).index), out


def load(names_path: str, hits_path: str):
    names = json.load(open(names_path, encoding="utf-8"))
    rows = json.load(open(hits_path, encoding="utf-8"))["rows"]
    if len(names) != len(rows):
        sys.exit(f"names ({len(names)}) and hits ({len(rows)}) do not line up")
    return names, rows


def all_flags(names, rows):
    return [detect(n[0], n[2], r) if not r.get("err") else (["error"], {}) for n, r in zip(names, rows)]


def cmd_counts(a) -> None:
    names, rows = load(a.names, a.hits)
    fl = all_flags(names, rows)
    src = collections.Counter(n[2] for n in names)
    by = collections.defaultdict(collections.Counter)
    for n, (f, _) in zip(names, fl):
        for x in f:
            by[x][n[2]] += 1
    print(f"{len(rows)} stored searches")
    for f in [k for k in LABELS if k in by] + [k for k in by if k not in LABELS]:
        tot = sum(by[f].values())
        top = ", ".join(f"{s} {v} ({100 * v / src[s]:.0f}%)" for s, v in by[f].most_common(4))
        print(f"{f:16s} {tot:6d} {100 * tot / len(rows):5.1f}%   {top}")


def cmd_flags(a) -> None:
    names, rows = load(a.names, a.hits)
    json.dump([{"f": f, "p": p} for f, p in all_flags(names, rows)], sys.stdout, separators=(",", ":"))


def cmd_sample(a) -> None:
    """Up to --per names per (flag, source) cell, plus --controls with no flag but no_card/short; each name once,
    in its first cell. The order mixes cells so a partial review is still a fair look across them."""
    names, rows = load(a.names, a.hits)
    fl = all_flags(names, rows)
    rnd = random.Random(a.seed)
    cells = collections.defaultdict(list)
    clean = []
    for i, (n, (f, _)) in enumerate(zip(names, fl)):
        real = [x for x in f if x not in ("no_card", "short", "error")]
        if not real:
            clean.append(i)
        for x in real:
            cells[(x, n[2])].append(i)
    picked, out = set(), []
    for key in sorted(cells):
        pool = [i for i in cells[key] if i not in picked]
        rnd.shuffle(pool)
        for i in pool[:a.per]:
            picked.add(i)
            out.append({"i": i, "cell": f"{key[0]}:{key[1]}"})
    rnd.shuffle(clean)
    out += [{"i": i, "cell": "control"} for i in clean[:a.controls]]
    rnd.shuffle(out)
    json.dump({"seed": a.seed, "per": a.per, "n": len(out), "items": out}, sys.stdout, separators=(",", ":"))
    print(f"sample: {len(out)} names from {len(cells)} cells + {min(a.controls, len(clean))} controls", file=sys.stderr)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for c in ("counts", "flags", "sample"):
        p = sub.add_parser(c)
        p.add_argument("names")
        p.add_argument("hits")
        if c == "sample":
            p.add_argument("--per", type=int, default=20)
            p.add_argument("--controls", type=int, default=200)
            p.add_argument("--seed", type=int, default=20261104)
    a = ap.parse_args()
    {"counts": cmd_counts, "flags": cmd_flags, "sample": cmd_sample}[a.cmd](a)


if __name__ == "__main__":
    main()
