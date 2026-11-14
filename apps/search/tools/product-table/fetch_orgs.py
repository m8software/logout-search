#!/usr/bin/env python3
"""Wikidata organisations for maker -> official site linking (CC0), from QLever, paged, generic user agent.

    fetch_orgs.py OUTDIR

makers.tsv  : distinct targets of manufacturer P176 / brand P1716 / developer P178 that have an official website P856
orgs-*.tsv  : businesses (instance of business, subclass walk) with a website: label, aliases, site, parent P749, owner P127
brands.tsv  : brands (instance of brand, subclass walk) and P1716 targets: label, aliases, site (optional), parent, owner, the items that carry them
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

from fetch_wikidata import PFX, get

PAGE = 100000


def paged(out: Path, name: str, body: str, select: str, group: str) -> None:
    f = out / name
    if f.exists():
        return
    rows, page = [], 0
    while True:
        q = (PFX + f"SELECT {select} WHERE {{ {body} }} {group} ORDER BY ?item LIMIT {PAGE} OFFSET {page * PAGE}")
        got = [l for l in get(q) if l.strip()]
        rows += got
        print(name, page, len(got), flush=True)
        if len(got) < PAGE:
            break
        page += 1
        time.sleep(5)
    f.write_text("\n".join(rows) + "\n")


def main() -> None:
    out = Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    lab = "?item rdfs:label ?label FILTER(LANG(?label) = \"en\") "
    ali = "OPTIONAL { ?item skos:altLabel ?alias FILTER(LANG(?alias) = \"en\") } "
    paged(out, "makers.tsv",
          "{ ?x wdt:P176 ?item } UNION { ?x wdt:P1716 ?item } UNION { ?x wdt:P178 ?item } ?item wdt:P856 ?site . " + lab +
          "OPTIONAL { ?item wdt:P31 ?cls . ?cls rdfs:label ?cl FILTER(LANG(?cl) = \"en\") }",
          "?item ?label ?site (SAMPLE(?cl) AS ?klass)", "GROUP BY ?item ?label ?site")
    for i, (cls) in enumerate(["Q4830453"]):
        paged(out, "orgs.tsv", f"?item wdt:P31/wdt:P279* wd:{cls} . ?item wdt:P856 ?site . " + lab + ali +
              "OPTIONAL { ?item wdt:P749 ?par } OPTIONAL { ?item wdt:P127 ?own }",
              "?item ?label (GROUP_CONCAT(DISTINCT ?alias; separator=\"|\") AS ?aliases) ?site (SAMPLE(?par) AS ?parent) (SAMPLE(?own) AS ?owner)",
              "GROUP BY ?item ?label ?site")
    paged(out, "brands.tsv", "{ ?item wdt:P31/wdt:P279* wd:Q431289 } UNION { ?x wdt:P1716 ?item } " + lab + ali +
          "OPTIONAL { ?item wdt:P856 ?site } OPTIONAL { ?item wdt:P749 ?par } OPTIONAL { ?item wdt:P127 ?own } OPTIONAL { ?item wdt:P176 ?mkr }",
          "?item ?label (GROUP_CONCAT(DISTINCT ?alias; separator=\"|\") AS ?aliases) ?site (SAMPLE(?par) AS ?parent) (SAMPLE(?own) AS ?owner) (SAMPLE(?mkr) AS ?maker)",
          "GROUP BY ?item ?label ?site")
    # sites of the parents/owners named above, by item
    paged(out, "parent-sites.tsv", "{ ?x wdt:P749 ?item } UNION { ?x wdt:P127 ?item } ?item wdt:P856 ?site . " + lab,
          "?item ?label ?site", "GROUP BY ?item ?label ?site")


def nosite(out: Path) -> None:
    """Businesses with no website of their own whose parent P749 or owner P127 has one."""
    lab = "?item rdfs:label ?label FILTER(LANG(?label) = \"en\") "
    ali = "OPTIONAL { ?item skos:altLabel ?alias FILTER(LANG(?alias) = \"en\") } "
    paged(out, "orgs-nosite.tsv", "?item wdt:P31/wdt:P279* wd:Q4830453 . FILTER NOT EXISTS { ?item wdt:P856 ?own_site } "
          "{ ?item wdt:P749 ?par } UNION { ?item wdt:P127 ?par } ?par wdt:P856 ?site . " + lab + ali,
          "?item ?label (GROUP_CONCAT(DISTINCT ?alias; separator=\"|\") AS ?aliases) (SAMPLE(?par) AS ?parent) (SAMPLE(?site) AS ?psite)",
          "GROUP BY ?item ?label")


if __name__ == "__main__":
    main()
    nosite(Path(sys.argv[1]))
