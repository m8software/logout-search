#!/usr/bin/env python3
"""wikipages-search.py CITED.sqlite RELEASE_SITES.txt MIX.tsv OUT.json: plain FTS over the cited-pages cut for the weak
kinds of mix-300 (how-to, people-info, site-section; up to 30 each, random with a fixed seed). Per search the top 5 rows
(bm25, title weighted 3x; AND of the words first, OR when AND gives fewer than 5), each flagged with whether its site
is in the release. A person reads the output and marks the searches whose top 5 holds a good answer."""
import json
import random
import re
import sqlite3
import sys

STOP = frozenset("the a an of and in on for to is are was how do does what who why when where can i my me you with at by "
                 "from it this that be or as".split())


def words(q):
    return [w for w in re.findall(r"[a-z0-9]+", q.lower()) if w not in STOP]


def main(db, sites, mix, out):
    release = set(open(sites).read().split())
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    rows = [l.rstrip("\n").split("\t") for l in open(mix, encoding="utf-8") if l.strip() and not l.startswith("#")]
    random.seed(7)
    res = []
    for kind in ("howto", "people-info", "site-section"):
        qs = [r[0] for r in rows if len(r) > 1 and r[1] == kind]
        random.shuffle(qs)
        for q in qs[:30]:
            w = words(q)
            top = []
            if w:
                for op in (" AND ", " OR "):
                    m = op.join(f'"{x}"' for x in w)
                    try:
                        top = con.execute("SELECT p.h, p.url, p.domain, p.title, p.sent, p.n FROM fts JOIN p ON p.id = fts.rowid "
                                          "WHERE fts MATCH ? ORDER BY bm25(fts, 3.0, 1.0) LIMIT 5", (m,)).fetchall()
                    except sqlite3.Error as e:
                        top = []
                    if len(top) >= 5:
                        break
            res.append({"kind": kind, "q": q, "op": op.strip() if w else "", "top": [
                {"url": ("http://" if h else "https://") + u, "domain": d, "title": t, "sent": s, "n": n,
                 "in_release": d in release} for h, u, d, t, s, n in top]})
    json.dump(res, open(out, "w"), ensure_ascii=False, indent=1)
    print(len(res), "searches")


if __name__ == "__main__":
    main(*sys.argv[1:5])
