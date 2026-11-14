#!/usr/bin/env python3
"""Plain FTS5 over products_fts, top 3: does a row carry the right product with the right maker, and that maker's official site?

    eval_fts.py DB[,DB2] --dev maker-plus-product.tsv --own own-queries.tsv

dev rows (gold = a host, split dev only): right = a top-3 row whose maker (or maker site) is the gold host's brand; the query's
words are all in name+maker+ids by construction of the AND match. own rows (gold = maker regex + product regex written before
any result was seen): right = a top-3 row whose maker matches `maker` and whose name matches `product`. site = that row has a
maker_site whose host matches the gold. mode strict = every word must match; relaxed = strict, else any-word OR (bm25).
"""
from __future__ import annotations

import csv
import re
import sqlite3
import sys
from collections import defaultdict
from urllib.parse import urlparse

STOP_LABELS = {"www", "com", "org", "net", "co", "uk", "us", "global", "shop", "store", "en", "de", "fr", "jp", "au", "ca", "io", "app", "gov", "edu"}


def labels(host: str) -> set[str]:
    return {p for p in re.split(r"[.\-]", host.lower()) if p and p not in STOP_LABELS}


def host_of(u: str) -> str:
    try:
        return urlparse(u if "//" in u else "//" + u).netloc.lower()
    except ValueError:
        return ""


def search(dbs, q: str, mode: str, k=3):
    """dbs: one connection or a list; rows of every file merged by bm25 (the files are separate licences, queried side by side)."""
    toks = re.findall(r"[a-z0-9]+", q.lower())
    if not toks:
        return []
    sql = ("SELECT p.name, p.maker, p.maker_site, p.category, p.source, bm25(products_fts, 5.0, 2.0, 1.0) s FROM products_fts f "
           "JOIN products p ON p.id=f.rowid WHERE products_fts MATCH ? ORDER BY s LIMIT ?")
    dbs = dbs if isinstance(dbs, list) else [dbs]

    def run(expr):
        rows = [r for d in dbs for r in d.execute(sql, (expr, k)).fetchall()]
        return [r[:5] for r in sorted(rows, key=lambda r: r[5])[:k]]
    rows = run(" AND ".join(f'"{t}"' for t in toks))
    if not rows and mode == "relaxed":
        rows = run(" OR ".join(f'"{t}"' for t in toks))
    return rows


def main():
    a = sys.argv[1:]
    db = [sqlite3.connect(x) for x in sys.argv[1].split(",")]
    dev = a[a.index("--dev") + 1]
    own = a[a.index("--own") + 1]
    for mode in ("strict", "relaxed"):
        for kind, path in (("dev (maker-plus-product, 122)", dev), ("own (65)", own)):
            tally = defaultdict(lambda: [0, 0, 0, 0])      # cat -> n, any row, right, right+site
            with open(path, encoding="utf-8") as fh:
                rd = list(csv.DictReader(fh, delimiter="\t"))
            for r in rd:
                if kind.startswith("dev"):
                    if r["split"] != "dev":
                        continue
                    cat = (re.search(r"cat:(\w+)", r["stratum"]) or [0, "other"])[1]
                    gold = {l for h in r["expected"].replace("|", ",").split(",") for l in labels(h.strip())}
                    ok = lambda row: bool(gold & (labels(host_of(row[2])) | {re.sub(r"[^a-z0-9]", "", row[1].lower())} | set(re.findall(r"[a-z0-9]+", row[1].lower())))) or any(g in re.sub(r"[^a-z0-9]", "", row[1].lower()) for g in gold if len(g) > 3)  # noqa: E731
                    okh = lambda row: bool(gold & labels(host_of(row[2])))  # noqa: E731
                else:
                    cat = r["category"]
                    mk, pr, hs = re.compile(r["maker"]), re.compile(r["product"]), re.compile(r["host"])
                    ok = lambda row: bool(mk.search(row[1].lower()) and pr.search(row[0].lower()))  # noqa: E731
                    okh = lambda row: bool(hs.search(host_of(row[2])))  # noqa: E731
                res = search(db, r["query"], mode)
                t = tally[cat]
                t[0] += 1
                t[1] += bool(res)
                good = [row for row in res if ok(row)]
                t[2] += bool(good)
                t[3] += any(okh(row) for row in good)
                r["_res"] = res
            print(f"\n### {kind}, {mode}\n| category | n | any row | right product+maker in top 3 | ... and row carries maker site |\n|---|---|---|---|---|")
            tot = [0, 0, 0, 0]
            for c, t in sorted(tally.items()):
                print(f"| {c} | {t[0]} | {t[1]} | {t[2]} | {t[3]} |")
                tot = [x + y for x, y in zip(tot, t)]
            print(f"| all | {tot[0]} | {tot[1]} | {tot[2]} | {tot[3]} |")
            if mode == "strict" and kind.startswith("own"):
                with open("/home/owner/data/jmaker/build/own-results.txt", "w") as o:
                    for r in rd:
                        o.write(f"{r['query']}\t{r['category']}\t{r['_res'][:3]}\n")


if __name__ == "__main__":
    main()
