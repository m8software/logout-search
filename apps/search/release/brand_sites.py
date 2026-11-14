#!/usr/bin/env python3
"""brand-sites.tsv: a brand's or an operator's name -> its official site, as a file of the release.

    brand_sites.py qids NSI.json OUT.txt                 the Wikidata ids to ask for their P856
    brand_sites.py build NSI.json P856.tsv OUT.tsv       P856.tsv: QID, tab, official website (Wikidata, CC0)
    brand_sites.py check OUT.tsv                         the shape the engine reads; the problems, exit 1

Source: name-suggestion-index (https://github.com/osmlab/name-suggestion-index, dist/json/nsi.json, BSD-3-Clause) for the
names and their Wikidata ids, chains and utilities that people ask for by a name Wikidata may not hold; Wikidata P856
(CC0) for the site. NSI's OpenStreetMap presets, locations and other tags are not kept.

Output: sorted lines of key, tab, url, tab, name. The key is search_api.norm_key(name). Left out:
- names with anything outside ASCII: the engine's key drops those letters, so "ÖBB" would answer "bb";
- keys under 4 characters or over 40;
- a key two different items claim: the engine cannot tell them apart, and a guess here is shown as official.
A name's bracket is dropped as well ("Aurora Energy (Alaska)" also answers "aurora energy"), unless another item claims that.
An item with several official sites gives its shortest address (sorted, so the same input gives the same bytes).
"""
import json
import os
import re
import sys
from urllib.parse import urlsplit

KEY = re.compile(r"[a-z0-9]+")
BRACKET = re.compile(r"\s*\([^)]*\)\s*")
TREES = ("brands", "operators")
NAME_TAGS = ("brand", "operator", "name")
QID_TAGS = ("brand:wikidata", "operator:wikidata")
MIN_KEY, MAX_KEY = 4, 40
MIN_ROWS, MAX_ROWS = 1000, 500_000


def norm_key(s: str) -> str:
    return "".join(KEY.findall(s.lower()))


def items(path: str):
    """(qid, names) for each item of the two trees with a Wikidata id."""
    with open(path, encoding="utf-8") as f:
        nsi = json.load(f)["nsi"]
    for tree, v in nsi.items():
        if tree.split("/")[0] in TREES:
            for it in v.get("items", []):
                t = it.get("tags", {})
                qid = next((t[k] for k in QID_TAGS if t.get(k)), None)
                if qid:
                    yield qid, {x for x in [it.get("displayName")] + [t.get(k) for k in NAME_TAGS] if x}


def name_forms(name: str):
    yield name
    bare = BRACKET.sub(" ", name).strip()
    if bare and bare != name:
        yield bare


def build(nsi: str, p856: str, dst: str) -> int:
    sites: dict = {}
    with open(p856, encoding="utf-8") as f:
        for line in f:
            qid, url = line.rstrip("\n").split("\t")[:2]
            if url.startswith(("http://", "https://")) and urlsplit(url).hostname:
                sites.setdefault(qid, []).append(url)
    owners: dict = {}
    for qid, names in items(nsi):
        if qid not in sites:
            continue
        for full in names:
            for name in name_forms(full):
                if name.isascii() and MIN_KEY <= len(norm_key(name)) <= MAX_KEY:
                    owners.setdefault(norm_key(name), {}).setdefault(qid, name)
    rows = []
    for key, by in sorted(owners.items()):
        if len(by) == 1:
            qid, name = next(iter(by.items()))
            rows.append((key, min(sites[qid], key=lambda u: (len(u), u)), name))
    with open(dst, "w", encoding="utf-8", newline="\n") as f:
        f.write("".join("\t".join(r) + "\n" for r in rows))
    errs = check(dst)
    if errs:
        raise SystemExit(f"{dst}: " + "; ".join(errs))
    return len(rows)


def check(path: str) -> list:
    if not os.path.isfile(path):
        return ["no such file"]
    keys, errs = [], []
    with open(path, encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            p = line.rstrip("\n").split("\t")
            if len(p) != 3 or not p[0] or p[0] != norm_key(p[2]) and p[0] != norm_key(BRACKET.sub(" ", p[2])) or not p[1].startswith("http"):
                errs.append(f"line {n}: not key, url, name")
                break
            keys.append(p[0])
    if not MIN_ROWS <= len(keys) <= MAX_ROWS:
        errs.append(f"{len(keys)} rows, expected {MIN_ROWS} to {MAX_ROWS}")
    if keys != sorted(set(keys)):
        errs.append("keys not sorted and unique")
    return errs


def main(argv) -> int:
    if len(argv) == 4 and argv[1] == "qids":
        with open(argv[3], "w", encoding="utf-8", newline="\n") as f:
            f.write("".join(q + "\n" for q in sorted({q for q, _n in items(argv[2])})))
        return 0
    if len(argv) == 5 and argv[1] == "build":
        print(f"{argv[4]}: {build(argv[2], argv[3], argv[4])} names")
        return 0
    if len(argv) == 3 and argv[1] == "check":
        errs = check(argv[2])
        for e in errs:
            print(f"{argv[2]}: {e}", file=sys.stderr)
        return 1 if errs else 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
