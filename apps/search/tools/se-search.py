#!/usr/bin/env python3
"""se-search.py SE.sqlite CUTX.sqlite QUERIES.tsv OUT.json [--kind howto]: plain FTS for each search over (a) the Stack
Exchange index alone, (b) cut X alone, (c) both (reciprocal-rank fusion of the two top-10 lists, k = 60).
bm25 with title weighted most; AND of the content words first, OR when AND gives fewer than 5 rows.
Queries: one per line, or query<TAB>kind (mix-300: only --kind rows are used). A person reads OUT.json."""
import argparse
import json
import re
import sqlite3
import sys

STOP = frozenset("the a an of and in on for to is are was how do does what who why when where can i my me you with at by "
                 "from it this that be or as get your".split())


def words(q):
    return [w for w in re.findall(r"[a-z0-9]+", q.lower()) if w not in STOP]


def fts(con, sql, q, n=10):
    w = words(q)
    for op in (" AND ", " OR "):
        if not w:
            return []
        try:
            rows = con.execute(sql, (op.join(f'"{x}"' for x in w), n)).fetchall()
        except sqlite3.Error:
            rows = []
        if len(rows) >= 5:
            return rows
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("se"); ap.add_argument("x"); ap.add_argument("queries"); ap.add_argument("out")
    ap.add_argument("--kind")
    a = ap.parse_args()
    se = sqlite3.connect(f"file:{a.se}?mode=ro", uri=True)
    x = sqlite3.connect(f"file:{a.x}?mode=ro", uri=True)
    qs = []
    for ln in open(a.queries, encoding="utf-8"):
        f = ln.rstrip("\n").split("\t")
        if not f[0] or f[0].startswith("#") or f[:2] == ["query", "category"]:
            continue
        if a.kind and (len(f) < 2 or f[1] != a.kind):
            continue
        qs.append(f[0])
    out = []
    for q in qs:
        s_rows = fts(se, "SELECT s.site, s.qid, s.title, s.score, s.views FROM fts JOIN s ON s.id = fts.rowid WHERE fts MATCH ? "
                         "ORDER BY bm25(fts, 6.0, 2.0, 1.0, 1.0) LIMIT ?", q)
        x_rows = fts(x, "SELECT p.h, p.url, p.title, p.sent FROM fts JOIN p ON p.id = fts.rowid WHERE fts MATCH ? "
                        "ORDER BY bm25(fts, 3.0, 1.0) LIMIT ?", q)
        se_l = [{"src": "se", "title": t, "url": f"https://{site}/questions/{qid}", "score": sc, "views": v}
                for site, qid, t, sc, v in s_rows]
        x_l = [{"src": "x", "title": t, "url": ("http://" if h else "https://") + u, "sent": s[:80]} for h, u, t, s in x_rows]
        fused = {}
        for lst in (se_l, x_l):
            for r, it in enumerate(lst):
                e = fused.setdefault(it["url"], [0.0, it])
                e[0] += 1.0 / (60 + r)
        both = [e[1] for e in sorted(fused.values(), key=lambda e: -e[0])][:5]
        out.append({"q": q, "se": se_l[:5], "x": x_l[:5], "both": both})
    json.dump(out, open(a.out, "w"), ensure_ascii=False, indent=1)
    print(len(out), "searches", file=sys.stderr)


if __name__ == "__main__":
    main()
