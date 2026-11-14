#!/usr/bin/env python3
"""Wikidata real-product rows (CC0) from QLever, for the product table. Small polite paged queries, generic user agent.

    fetch_wikidata.py OUTDIR

One TSV per root class (item, label, aliases, maker, maker_site, class, gtin, site, described_at). An item is kept when it is
an instance of (a subclass of) one of ROOTS and has a manufacturer P176, brand P1716 or developer P178. Ships, trains, organs,
supercomputers, gas stations and the like are not in ROOTS, so they never come in.
"""
from __future__ import annotations

import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ENDPOINT = "https://qlever.dev/api/wikidata"
UA = "research-script/1.0"
PFX = ("PREFIX wd: <http://www.wikidata.org/entity/> PREFIX wdt: <http://www.wikidata.org/prop/direct/> "
       "PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#> PREFIX skos: <http://www.w3.org/2004/02/skos/core#> ")
# name -> (root qid, walk subclasses?)
ROOTS = {
    "car_model": ("Q3231690", True), "motorcycle_model": ("Q23866334", True), "product_model": ("Q10929058", True),
    "smartphone_model": ("Q19723451", True), "camera_model": ("Q20741022", True), "lens_model": ("Q109672300", True),
    "tractor_model": ("Q55725952", True), "aircraft_model": ("Q15056995", True), "home_appliance": ("Q1501", True),
    "action_figure": ("Q343566", False), "beer_brand": ("Q15075508", False), "beer": ("Q44", False),
    "software": ("Q7397", True), "video_game": ("Q7889", True), "game_console": ("Q8076", True),
    "mobile_phone": ("Q17517", True), "laptop": ("Q3962", True), "tablet": ("Q155972", True), "television": ("Q289", False),
    "toy": ("Q11422", True), "watch_model": ("Q1394264", True), "bicycle_model": ("Q18543041", True),
    "consumer_electronics": ("Q1070", False),
}
PAGE = 40000


def query(qid: str, walk: bool, page: int) -> str:
    cls = f"?item wdt:P31/wdt:P279* wd:{qid} . " if walk else f"?item wdt:P31 wd:{qid} . "
    return (PFX + "SELECT ?item ?label (GROUP_CONCAT(DISTINCT ?alias; separator=\"|\") AS ?aliases) ?mk ?msite ?gtin ?site ?desc WHERE { "
            + cls + "{ ?item wdt:P176 ?maker } UNION { ?item wdt:P1716 ?maker } UNION { ?item wdt:P178 ?maker } "
            "?item rdfs:label ?label FILTER(LANG(?label) = \"en\") "
            "OPTIONAL { ?maker rdfs:label ?mk FILTER(LANG(?mk) = \"en\") } OPTIONAL { ?maker wdt:P856 ?msite } "
            "OPTIONAL { ?item skos:altLabel ?alias FILTER(LANG(?alias) = \"en\") } OPTIONAL { ?item wdt:P3962 ?gtin } "
            "OPTIONAL { ?item wdt:P856 ?site } OPTIONAL { ?item wdt:P973 ?desc } } "
            f"GROUP BY ?item ?label ?mk ?msite ?gtin ?site ?desc ORDER BY ?item LIMIT {PAGE} OFFSET {page * PAGE}")


def get(q: str) -> list[str]:
    data = urllib.parse.urlencode({"query": q}).encode()
    for attempt in range(5):
        try:
            req = urllib.request.Request(ENDPOINT, data=data, headers={"User-Agent": UA, "Accept": "text/tab-separated-values"})
            with urllib.request.urlopen(req, timeout=300) as r:
                return r.read().decode("utf-8").split("\n")[1:]
        except Exception as e:  # noqa: BLE001
            print(f"retry {attempt}: {e}", file=sys.stderr, flush=True)
            time.sleep(20 * (attempt + 1))
    raise SystemExit("QLever failed 5 times")


def main() -> None:
    out = Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    for name, (qid, walk) in ROOTS.items():
        f = out / f"{name}.tsv"
        if f.exists():
            continue
        rows, page = [], 0
        while True:
            got = [l for l in get(query(qid, walk, page)) if l.strip()]
            rows += got
            print(f"{name} page {page}: {len(got)}", flush=True)
            if len(got) < PAGE // 4 and page == 0 or len(got) == 0:
                break
            page += 1
            time.sleep(4)
            if len(got) < PAGE // 2:
                break
        f.write_text("\n".join(rows) + "\n")
        time.sleep(4)


if __name__ == "__main__":
    main()


# Items to drop even when they sit under a ROOT (a "product model" that is a railcar series, an organ, a ship class ...).
EXCLUDE = {"ship": "Q11446", "ship_class": "Q559026", "pipe_organ": "Q281460", "rolling_stock_class": "Q811704",
           "locomotive_class": "Q19832486", "supercomputer": "Q121117", "individual_aircraft": "Q11436", "railway_vehicle": "Q1075",
           "rail_vehicle": "Q18643213", "building": "Q41176", "weapon": "Q728", "streetlight": "Q503958", "ride": "Q2389789"}


def exclusions(out: Path) -> None:
    for name, qid in EXCLUDE.items():
        f = out / f"_exclude_{name}.tsv"
        if f.exists():
            continue
        q = (PFX + "SELECT DISTINCT ?item WHERE { ?item wdt:P31/wdt:P279* wd:%s . { ?item wdt:P176 ?m } UNION { ?item wdt:P1716 ?m } "
             "UNION { ?item wdt:P178 ?m } }" % qid)
        got = [l for l in get(q) if l.strip()]
        f.write_text("\n".join(got) + "\n")
        print(f"exclude {name}: {len(got)}", flush=True)
        time.sleep(4)
