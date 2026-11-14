#!/usr/bin/env python3
"""Own-title eval set (rc5 plan W4): a person, a band, an organisation or a place asked by the exact
title of its article. Which leads, the official site or the article?

    tools/own-title-evalset.py [--out tools/evalsets/open] [--per 300] [--cache ~/.cache/logout]

Owner rule, 2026-10-07 (final): the SITE leads for everything, the article sits directly under it (on the page the
article is the card). A place was first planned as the exception and is kept as its own stratum to watch. No
existing set asks this: wiki-redirect asks by aliases, and "italy" and "cadiz" are left out elsewhere as ambiguous.

A row is an English article whose Wikidata item has an official website (P856) that is a front page, and whose
title has no "(...)" (so the title is the plain name). The kind comes from Wikidata's instance-of, through
subclass-of, NOT from the infobox type the engine reads, so the set and the engine do not share their evidence:

  person  human (Q5)                                   expected: the site          category site
  band    musical group (Q215380)                      expected: the site          category site
  org     an ORG_TYPES type of importer/wikidata_sites.py, and none of the above   category site
  place   country, human settlement, administrative or political territorial entity
                                                       expected: the site          category site

Per kind the --per most-linked items (Wikidata sitelinks: no pageview fetch needed). Wikidata (CC0) through QLever;
article titles are English Wikipedia's (CC BY-SA 4.0). Split and columns are open-evalsets.py's.
Output: own-title.tsv (query, category, expected, set, stratum, split, entity). hit@5 shows whether the other
one of the pair is close behind.
"""
from __future__ import annotations

import argparse
import importlib.util
import sys
import urllib.parse
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("head_brand", HERE / "head-brand-evalset.py")
hb = importlib.util.module_from_spec(_spec)
sys.modules["head_brand"] = hb
_spec.loader.exec_module(hb)

PLACE_TYPES = "Q6256 Q486972 Q56061 Q1048835 Q15642541".split()
KINDS = ("person", "band", "org", "place")
QUERY = """PREFIX wdt: <http://www.wikidata.org/prop/direct/>
PREFIX wd: <http://www.wikidata.org/entity/>
PREFIX schema: <http://schema.org/>
PREFIX wikibase: <http://wikiba.se/ontology#>
SELECT ?article ?site (MAX(?sl) AS ?links) (MAX(?p) AS ?person) (MAX(?b) AS ?band) (MAX(?g) AS ?place) (MAX(?o) AS ?org) WHERE {
  ?item wdt:P856 ?site ; wikibase:sitelinks ?sl .
  ?article schema:about ?item ; schema:isPartOf <https://en.wikipedia.org/> .
  OPTIONAL { ?item wdt:P31 wd:Q5 . BIND(1 AS ?p) }
  OPTIONAL { ?item wdt:P31/wdt:P279* wd:Q215380 . BIND(1 AS ?b) }
  OPTIONAL { ?item wdt:P31/wdt:P279* ?x . VALUES ?x { %s } BIND(1 AS ?g) }
  OPTIONAL { ?item wdt:P31/wdt:P279* ?y . VALUES ?y { %s } BIND(1 AS ?o) }
} GROUP BY ?article ?site HAVING (MAX(?sl) >= 40)""" % (" ".join("wd:" + q for q in PLACE_TYPES), " ".join("wd:" + q for q in hb.ORG_TYPES))


def kind_of(person: bool, band: bool, place: bool, org: bool) -> str | None:
    """One kind per item, or None when the item is two things at once (a city-state, a one-person band)."""
    if person + band + place > 1:
        return None
    if person:
        return "person"
    if band:
        return "band"
    if place:
        return "place"
    return "org" if org else None


def rows_of(lines) -> dict[str, list[tuple[str, str, int]]]:
    """QLever TSV -> {kind: [(title, hosts "|"-joined, sitelinks)]}, most linked first. Titles with "(...)" and
    items with no front-page site are left out; a query two items share is dropped from every kind."""
    per: dict[str, dict] = {}
    for line in lines:
        f = line.rstrip("\n").split("\t")
        if len(f) < 7 or f[0].startswith("?") or "/wiki/" not in f[0]:
            continue
        title = urllib.parse.unquote(f[0].strip("<>").rsplit("/wiki/", 1)[-1]).replace("_", " ")
        e = per.setdefault(title, {"sl": 0, "hosts": set(), "flags": [False] * 4})
        try:
            e["sl"] = max(e["sl"], int(f[2].strip('"').split("^^")[0].strip('"') or 0))
        except ValueError:
            pass
        for i in range(4):
            e["flags"][i] = e["flags"][i] or hb._flag(f[3 + i])
        host = hb.front_host(f[1].strip("<>"))
        if host:
            e["hosts"].add(host)
    seen = defaultdict(int)
    for title in per:
        seen[hb.norm_key(title)] += 1
    out: dict[str, list] = {k: [] for k in KINDS}
    for title, e in per.items():
        kind = kind_of(*e["flags"])
        if kind is None or not e["hosts"] or "(" in title or len(hb.norm_key(title)) < 3 or seen[hb.norm_key(title)] > 1:
            continue
        out[kind].append((title, "|".join(sorted(e["hosts"], key=lambda h: h.encode("utf-8"))), e["sl"]))
    for k in out:
        out[k].sort(key=lambda r: (-r[2], r[0].encode("utf-8")))
    return out


def build(by_kind: dict, per: int) -> list[dict]:
    res = []
    for kind in KINDS:
        for i, (title, hosts, _sl) in enumerate(by_kind[kind][:per]):
            ent = hb.norm_key(title)
            res.append({"query": title.lower(), "category": "site", "expected": hosts,
                        "set": "own-title", "stratum": f"kind:{kind},links-rank:{'1-100' if i < 100 else '101+'}",
                        "split": hb.split_of(ent), "entity": ent})
    return res


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", default=str(HERE / "evalsets" / "open"))
    ap.add_argument("--per", type=int, default=300, help="rows per kind (the most linked)")
    ap.add_argument("--cache", default=str(Path.home() / ".cache" / "logout"))
    a = ap.parse_args()
    by_kind = rows_of(hb.qlever(QUERY, Path(a.cache) / "own-title-candidates.tsv"))
    hb.log("candidates: " + ", ".join(f"{k} {len(v):,}" for k, v in by_kind.items()))
    res = build(by_kind, a.per)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    with open(out / "own-title.tsv", "w", encoding="utf-8") as f:
        f.write("\t".join(hb.COLS) + "\n")
        for r in res:
            f.write("\t".join(str(r[c]) for c in hb.COLS) + "\n")
    n = defaultdict(int)
    for r in res:
        n[r["split"], r["stratum"].split(",")[0]] += 1
    hb.log(f"wrote {len(res):,} rows: " + ", ".join(f"{s} {k} {c}" for (s, k), c in sorted(n.items())))


if __name__ == "__main__":
    main()
