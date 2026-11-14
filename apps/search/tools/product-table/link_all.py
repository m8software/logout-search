#!/usr/bin/env python3
"""Link site-less rows of a products SQLite to official sites through Wikidata (see link_makers.py for the rules), with a sector guard.

    link_all.py DB --wd2 DIR --out links-final.tsv [--apply]

For every distinct (source, maker, brand) of a row with no maker_site it collects the rule hits (A, B1, B2, C), fetches the Wikidata
classes and industries of the matched items (QLever, CC0, cached in DIR/sectors.tsv) and keeps the first hit whose sector fits the
row's source: food for fdc and off:food, drugs for ndc, appliances/electronics for es, any consumer sector for the other off:*.
A hit whose matched item is a record label, a university, a bank, a game studio and so on is therefore dropped, which is what a
bare name match gets wrong. Source wd is not linked (its makers are Wikidata items already; a name match adds nothing but errors).
--apply writes the sites into the DB (maker_site only; no other column or table changes).
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from fetch_wikidata import PFX, get  # noqa: E402
from link_makers import Resolver  # noqa: E402

SECTORS = {
    "food": r"food|beverage|drink|snack|candy|confection|chocolate|bakery|baking|dairy|cheese|cereal|restaurant|grocer|supermarket|meat|brew|wine|"
            r"coffee|\btea\b|juice|soft drink|ice cream|pasta|noodle|sauce|fast food|spice|sugar|bread|biscuit|cookie|cracker|seafood|"
            r"fruit|vegetable|cannery|poultry|pet food|spirits|distill|breakfast|cake|dessert|soup|frozen|chewing gum|creamery|bottler",
    "drug": r"pharma|drug|medic|health|biotech|cosmetic|personal care|chemist|supplement|vitamin|consumer goods|hygiene|retail|store|"
            r"supermarket|chain|wholesale|laborator|generic|life science",
    "es": r"manufactur|electronic|appliance|machinery|electrical|hvac|heating|lighting|computer|technology|tool|engineering|industrial|"
          r"equipment|brand|consumer goods|furniture|ventilat|air condition|refrigerat|water heat|telecommunication|semiconductor|software|"
          r"hardware|automotive|vehicle|company|business",
}
# Kept after the hand-read precision samples (links-sample*.tsv): rule A only, for fdc and ndc. B1 76%, B2 50%, C 86%, and A on
# es 64%, were under the 95% bar and are not applied.
RULES = {"A"}
SOURCES = {"fdc", "ndc"}
SRC_SECTOR = {"fdc": "food", "off:food": "food", "ndc": "drug", "es": "es", "off:beauty": "drug", "off:pet food": "food", "off:products": "es"}


def sector_ok(src: str, labels: set[str]) -> bool:
    pat = SECTORS[SRC_SECTOR[src]]
    return any(re.search(pat, l.lower()) for l in labels)


def fetch_sectors(qids: set[str], cache: Path) -> dict[str, set[str]]:
    have: dict[str, set[str]] = defaultdict(set)
    known: set[str] = set()
    if cache.exists():
        for line in cache.read_text(encoding="utf-8").split("\n"):
            p = line.split("\t")
            if len(p) == 2:
                have[p[0]].add(p[1])
                known.add(p[0])
            elif len(p) == 1 and p[0].startswith("Q"):
                known.add(p[0])
    todo = sorted(q for q in qids if q not in known)
    with open(cache, "a", encoding="utf-8") as out:
        for i in range(0, len(todo), 300):
            batch = todo[i:i + 300]
            q = (PFX + "SELECT ?item ?l WHERE { VALUES ?item { " + " ".join("wd:" + x for x in batch) + " } "
                 "{ ?item wdt:P31 ?c } UNION { ?item wdt:P452 ?c } "
                 "?c rdfs:label ?l FILTER(LANG(?l) = \"en\") }")
            got = {}
            for line in get(q):
                p = line.split("\t")
                if len(p) == 2 and "entity/Q" in p[0]:
                    qid = p[0].strip("<>").rsplit("/", 1)[1]
                    lab = p[1].strip().strip('"').rsplit('"@', 1)[0].strip('"')
                    got.setdefault(qid, set()).add(lab)
            for x in batch:
                for lab in got.get(x, ()):
                    out.write(f"{x}\t{lab}\n")
                    have[x].add(lab)
                if x not in got:
                    out.write(f"{x}\n")
            out.flush()
            time.sleep(3)
    return have


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("db"), ap.add_argument("--wd2", required=True), ap.add_argument("--out", required=True)
    ap.add_argument("--apply", action="store_true"), ap.add_argument("--sources", help="comma list overriding SOURCES")
    a = ap.parse_args()
    global SOURCES
    if a.sources:
        SOURCES = set(a.sources.split(","))
    db = sqlite3.connect(a.db)
    res = Resolver(a.wd2)
    groups: dict[tuple, list] = {}
    for src, maker, name, ids in db.execute("SELECT source, maker, name, ids FROM products WHERE maker_site='' AND source<>'wd' AND source<>'epa'"):
        if src == "fdc":
            brand = ids[6:].split(" gtin:")[0] if ids.startswith("brand:") else ""
        elif src == "ndc":
            brand = name
        else:
            brand = maker
        g = groups.setdefault((src, maker, brand), [name, 0])
        g[1] += 1
    print(f"{len(groups):,} distinct (source, maker, brand) without a site", flush=True)
    cands = {}
    qids = set()
    for k in groups:
        hits = res.candidates(k[1], k[2])
        if hits:
            cands[k] = hits
            for h in hits:
                qids.update(h[4])
    print(f"{len(cands):,} with a name hit; {len(qids):,} Wikidata items to classify", flush=True)
    sect = fetch_sectors(qids, Path(a.wd2) / "sectors.tsv")
    final = {}
    for k, hits in cands.items():
        for h in hits:
            labels = set().union(*(sect.get(q, set()) for q in h[4]))
            if sector_ok(k[0], labels) and h[1] in RULES and k[0] in SOURCES:
                h = (re.match(r"https?://[^/]+", h[0]).group(0) + "/",) + tuple(h[1:])     # the maker's front page, not a path
                final[k] = h
                break
    with open(a.out, "w", encoding="utf-8") as f:
        f.write("source\tmaker\tbrand\tsite_url\twikidata_id\trule\tmatched_label\texample_name\tn_rows\n")
        for k, h in sorted(final.items()):
            f.write("\t".join([k[0], k[1], k[2], h[0], h[2], h[1], h[3], groups[k][0].replace("\t", " "), str(groups[k][1])]) + "\n")
    rows = sum(groups[k][1] for k in final)
    print(f"kept {len(final):,} links covering {rows:,} rows", flush=True)
    if a.apply:
        cur = db.cursor()
        for k, h in final.items():
            src, maker, brand = k
            if src == "fdc":
                cur.execute("UPDATE products SET maker_site=? WHERE source='fdc' AND maker=? AND maker_site='' AND "
                            "(CASE WHEN ids LIKE 'brand:%' THEN substr(ids, 7, instr(ids || ' gtin:', ' gtin:') - 7) ELSE '' END)=?", (h[0], maker, brand))
            elif src == "ndc":
                cur.execute("UPDATE products SET maker_site=? WHERE source='ndc' AND maker=? AND name=? AND maker_site=''", (h[0], maker, brand))
            else:
                cur.execute("UPDATE products SET maker_site=? WHERE source=? AND maker=? AND maker_site=''", (h[0], src, maker))
        db.commit()


if __name__ == "__main__":
    main()
