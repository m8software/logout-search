#!/usr/bin/env python3
"""The `facts` open eval set: template x entity, answer = the value AND the article, known by construction.

    tools/facts-evalset.py FACTS.sqlite REGISTRY.sqlite SIGNALS.bin [--out tools/evalsets/open/facts.tsv] [--per 12]

Built like the other open sets (open-evalsets.py / head-brand-evalset.py): no Jev, no LLM, no browsing history.
Every row is one template of the table in facts.sqlite filled with one article's title (the bare title, no
"(film)" qualifier) whose Wikidata value for that property is on file; the expected answer is "<title>|<value>"
(the value as the build stored it: ISO date, decimal amount + unit, item label). Entities are the head of each
property by Wikipedia pageviews (the release's signals.bin, CC0), three strata by views rank (1-100, 101-1000,
1001+ of that property's articles); dev/test by entity with the md5 seed 402 (open-evalsets.split_of), the same
split key as every other set (the normalised Wikipedia title). A title that is another article's title or whose
bare form is shared is not scored (ambiguous-facts.tsv).

Licence: the values are Wikidata (CC0); the titles English Wikipedia (CC BY-SA 4.0): one file, both named in
the README. tools/facts-eval.py scores it (ANSWERED: card present, value matches, article matches).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import struct
import sys
from collections import Counter, defaultdict
from pathlib import Path

SEED = "402"
TYPE_SHIFT = 28
_TOKEN = re.compile(r"[a-z0-9]+")
COLS = ("query", "category", "expected", "set", "stratum", "split", "entity")
# templates that are not a question about one entity (the shared-name "where is X" shapes) are not sampled
SKIP_PROPS = {"P625", "P131", "P17", "P106", "P39", "P1830", "P1056", "P166", "P1346", "P155", "P156", "P179", "P1454", "P414", "P306", "P449"}


def norm_key(s: str) -> str:
    return "".join(_TOKEN.findall(s.lower()))


def h(*parts) -> str:
    return hashlib.md5((SEED + ":" + ":".join(map(str, parts))).encode()).hexdigest()


def split_of(entity: str) -> str:
    return "test" if int(h("split", entity)[:8], 16) % 2 else "dev"


def pageviews(signals: str):
    with open(signals, "rb") as f:
        head = f.read(12)
        n = struct.unpack("<I", head[8:12])[0]
        hd = json.loads(f.read(n))
        fld = next(x for x in hd["fields"] if x["name"] == "wiki.pageviews")
        f.seek(12 + n + fld["offset"])
        return memoryview(f.read(fld["count"] * 4)).cast("I")


def fill(pattern: str, title: str) -> str | None:
    """A template pattern -> one plain query: innermost groups first, an optional group dropped, otherwise its first
    alternative ("(?:who is the )?(?:ceo|chief executive(?: officer)?) of {x}" -> "ceo of {x}")."""
    if "\\d" in pattern:
        return None
    p = pattern
    while True:
        n = re.sub(r"\(\?:([^()]*)\)(\?)?", lambda m: "" if m.group(2) else m.group(1).split("|")[0], p)
        if n == p:
            break
        p = n
    p = p.replace("\\+", "+").replace("\\.", ".").replace(" ?", " ")
    if re.search(r"[()\[\]\\?*^$|]", p.replace("{x}", "")):
        return None
    return " ".join(p.replace("{x}", title.lower()).split())


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("facts")
    ap.add_argument("registry")
    ap.add_argument("signals")
    ap.add_argument("--out", default=str(Path(__file__).parent / "evalsets" / "open" / "facts.tsv"))
    ap.add_argument("--per", type=int, default=12, help="rows per (property, stratum)")
    a = ap.parse_args()
    pv = pageviews(a.signals)
    reg = sqlite3.connect(f"file:{a.registry}?mode=ro", uri=True)
    titles = dict(reg.execute(f"SELECT doc_id, title FROM registry WHERE doc_id >> {TYPE_SHIFT} = 1 AND title IS NOT NULL"))
    by_key = Counter(norm_key(re.sub(r"\s*\(.*\)$", "", t)) for t in titles.values())
    full_keys = Counter(norm_key(t) for t in titles.values())
    db = sqlite3.connect(f"file:{a.facts}?mode=ro", uri=True)
    props = {pid: (prop, name, kind) for pid, prop, name, kind in db.execute("SELECT pid, prop, name, kind FROM props")}
    pats = defaultdict(list)
    for pid, pat in db.execute("SELECT pid, pattern FROM templates ORDER BY pid, ord"):
        pats[pid].append(pat)
    rows, amb = [], []
    for pid, (prop, name, kind) in sorted(props.items()):
        if prop in SKIP_PROPS:
            continue
        docs = [d for (d,) in db.execute("SELECT DISTINCT doc_id FROM facts WHERE pid = ? AND ord = 0", (pid,))]
        docs.sort(key=lambda d: -int(pv[d & ((1 << TYPE_SHIFT) - 1)]))
        strata = {"views-rank:1-100": docs[:100], "views-rank:101-1000": docs[100:1000], "views-rank:1001+": docs[1000:20000]}
        fills = [f for f in (fill(p, "{x}") for p in pats[pid]) if f and f != "{x}"]
        if not fills:
            continue
        for st, ds in strata.items():
            picked = 0
            for d in sorted(ds, key=lambda d: h("take", prop, d)):
                t = titles.get(d)
                if not t:
                    continue
                bare = re.sub(r"\s*\(.*\)$", "", t)
                ent = norm_key(t)
                if len(norm_key(bare)) < 3 or len(bare.split()) > 6 or by_key[norm_key(bare)] > 1 or full_keys[norm_key(bare)] > 1 and bare != t:
                    amb.append((bare.lower(), by_key[norm_key(bare)]))
                    continue
                v, unit, prec, at = db.execute("SELECT v, unit, prec, at FROM facts WHERE doc_id = ? AND pid = ? AND ord = 0", (d, pid)).fetchone()
                if unit and unit.startswith("Q"):
                    continue
                tmpl = fills[int(h("tmpl", prop, d)[:4], 16) % len(fills)]
                q = tmpl.replace("{x}", bare.lower())
                exp = f"{t}|{v}" + (f" {unit}" if unit else "")
                rows.append({"query": q, "category": "fact", "expected": exp, "set": "facts",
                             "stratum": f"prop:{prop},{st},kind:{kind}", "entity": ent, "split": split_of(ent)})
                picked += 1
                if picked >= a.per:
                    break
    seen = set()
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    kept = []
    for r in rows:
        if r["query"] in seen:
            continue
        seen.add(r["query"])
        kept.append(r)
    with open(out, "w", encoding="utf-8") as f:
        f.write("\t".join(COLS) + "\n")
        for r in sorted(kept, key=lambda r: (r["split"], r["stratum"], r["query"])):
            f.write("\t".join(str(r[c]) for c in COLS) + "\n")
    with open(out.parent / "ambiguous-facts.tsv", "w", encoding="utf-8") as f:
        f.write("query\tset\tentities\n")
        for q, n in sorted(set(amb)):
            f.write(f"{q}\tfacts\t{n}\n")
    sp = Counter(r["split"] for r in kept)
    print(f"facts {len(kept):>6}  dev {sp['dev']:>5}  test {sp['test']:>5}  properties {len({r['stratum'].split(',')[0] for r in kept})}  ambiguous {len(set(amb))}", file=sys.stderr)


if __name__ == "__main__":
    main()
