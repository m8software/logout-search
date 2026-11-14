#!/usr/bin/env python3
"""Candidate brand -> store pairs for the buy section's brand list, for a person to read.

    shop-brand-candidates.py --table apps/search/serve/api/shop_stores.json --wikidata DIR [--leads leads.tsv] > candidates.tsv

Nothing here edits the table: a pair becomes a pin only when a person copies it into "brands" (and that is what
"checked" in an entry means). Two sources, strongest first:

  wikidata  DIR holds the files tools/product-table/fetch_orgs.py writes (CC0): brands.tsv (item, label, aliases,
            site, parent, owner, maker) and parent-sites.tsv / orgs.tsv (item, label, site...). A brand whose
            owner (P127) or parent (P749) is a business that IS one of our stores -- same website as the store's
            domain -- is a store brand of that store: rule "P127" / "P749".
  lead      leads.tsv: title <TAB> the article's opening. A sentence that says the brand is sold exclusively at a store,
            or is a store's house / private-label brand, names that store: rule "lead-exclusive" / "lead-house".
            Weaker (wording varies): read before using. Records, films and game editions are left out (MEDIA below).

Columns of the output: brand, aliases, store, rule, evidence, note. note says "already in the list" or, for a brand the
list sends to another store, "CONFLICT <store>".
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit


def plain(value: str) -> str:
    """A Wikidata label as QLever writes it in these files (Husky@en, or "Husky"@en) -> Husky."""
    m = re.fullmatch(r'"?(.*?)"?@[A-Za-z][A-Za-z-]*', value.strip())
    return m.group(1) if m else value.strip().strip('"')


def norm(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", str(text).lower().replace("'", "").replace("’", "")).split())


def tsv(path: Path):
    if not path.exists():
        return
    with path.open(encoding="utf-8", newline="") as f:
        for row in csv.reader(f, delimiter="\t"):
            if row:
                yield row


def host(site: str) -> str:
    return (urlsplit(site.strip().strip('"<>')).hostname or "").removeprefix("www.")


def stores_by_domain(table: dict) -> dict[str, dict]:
    """domain -> store, from the store's own site line (sub) and its search link; only stores that are on."""
    out = {}
    for s in table.get("stores", []):
        if not s.get("on"):
            continue
        for d in (urlsplit(s["url"]).hostname or "", s.get("sub", "")):
            d = d.removeprefix("www.")
            if re.fullmatch(r"[a-z0-9-]+(\.[a-z0-9-]+)+", d):
                out[d] = s
    return out


def from_wikidata(d: Path, by_dom: dict[str, dict]):
    sites: dict[str, str] = {}                              # item -> website host, from the parents' and organisations' files
    for name in ("parent-sites.tsv", "orgs.tsv"):
        for r in tsv(d / name):                              # parent-sites: item label site; orgs: item label aliases site ...
            col = 2 if name == "parent-sites.tsv" else 3
            if len(r) > col:
                sites.setdefault(r[0], host(r[col]))
    for r in tsv(d / "brands.tsv"):
        if len(r) < 6:
            continue
        item, label, aliases, _site, parent, owner = r[:6]
        label, aliases = plain(label), "|".join(plain(a) for a in aliases.split("|") if a)
        for rule, who in (("P127", owner), ("P749", parent)):
            store = by_dom.get(sites.get(who, ""))
            if who and store:
                yield label, [a for a in aliases.split("|") if a], store["id"], rule, f"{item} {rule} {who} ({sites[who]})"


LEAD_RULES = [
    ("lead-exclusive", re.compile(r"\b(?:sold|available|distributed)\s+(?:exclusively|only)\s+(?:at|through|by|in)\s+(?:[Tt]he\s+)?([A-Z][\w'&.-]*(?: [A-Z&][\w'&.-]*){0,3})")),
    ("lead-exclusive", re.compile(r"\bexclusive(?:ly)?\s+(?:brand\s+)?(?:at|of|to)\s+(?:[Tt]he\s+)?([A-Z][\w'&.-]*(?: [A-Z&][\w'&.-]*){0,3})")),
    ("lead-house", re.compile(r"\b(?:house|private[- ]label|store)\s+brand\s+(?:of|for|at|sold\s+(?:at|by))\s+(?:[Tt]he\s+)?([A-Z][\w'&.-]*(?: [A-Z&][\w'&.-]*){0,3})")),
]


# An opening that is about a record, a film or a game edition ("a bonus track exclusive to Target") is not a brand: the
# 2026-11-13 run over all of Wikipedia gave 77 pairs, about 60 of them albums, EPs and DVD editions. A brand's opening
# says what the product is; a release's says what it was released as.
MEDIA = re.compile(r"\b(album|ep|single|song|soundtrack|dvd|cd|blu-?ray|video game|film|episode|seasons?|tour|track|tracks|"
                   r"edition|compilation|greatest hits|studio recording|live recording|box set|mixtape|toyline|volume)\b", re.I)
MEDIA_TITLE = re.compile(r"\((album|ep|song|single|soundtrack|video|film|band|toyline|video game)\)|^List of |\bseason \d+\b", re.I)
PRODUCT = re.compile(r"\b(brand|clothing|clothes|apparel|line of|collection|furniture|tools?|toys?|cosmetics|beverages?|snacks?|"
                     r"appliances?|jeans|footwear|shoes|private[- ]label|housewares|bedding|mattress|cookware|accessories)\b", re.I)


def product_brand(title: str, lead: str) -> bool:
    return not MEDIA_TITLE.search(title) and not MEDIA.search(lead[:300]) and bool(PRODUCT.search(lead[:300]))


def from_leads(path: Path, table: dict):
    names = {norm(s["name"]): s for s in table.get("stores", []) if s.get("on")}
    for r in tsv(path):
        if len(r) < 2:
            continue
        title, lead = r[0], r[1]
        if not product_brand(title, lead):
            continue
        for rule, rx in LEAD_RULES:
            for m in rx.finditer(lead):
                store = names.get(norm(m.group(1).rstrip(".")))
                if store:
                    yield re.sub(r"\s*\([^)]*\)$", "", title), [], store["id"], rule, lead[max(0, m.start() - 40):m.end() + 20].replace("\t", " ")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--table", required=True)
    ap.add_argument("--wikidata", type=Path)
    ap.add_argument("--leads", type=Path)
    a = ap.parse_args()
    table = json.load(open(a.table, encoding="utf-8"))
    have: dict[str, set[str]] = {}
    for e in table.get("brands", []):
        for f in [e["brand"], *e.get("aka", [])]:
            have.setdefault(norm(f), set()).update(e["stores"])
    rows = []
    if a.wikidata:
        rows += from_wikidata(a.wikidata, stores_by_domain(table))
    if a.leads:
        rows += from_leads(a.leads, table)
    seen = set()
    w = csv.writer(sys.stdout, delimiter="\t", lineterminator="\n")
    w.writerow(["brand", "aliases", "store", "rule", "evidence", "note"])
    for brand, aka, store, rule, ev in sorted(rows, key=lambda x: (norm(x[0]), x[2], x[3])):
        key = (norm(brand), store, rule)
        if key in seen or not norm(brand):
            continue
        seen.add(key)
        known = have.get(norm(brand))
        note = "" if known is None else ("already in the list" if store in known else "CONFLICT " + ",".join(sorted(known)))
        w.writerow([brand, "|".join(aka), store, rule, ev, note])
    return 0


if __name__ == "__main__":
    sys.exit(main())
