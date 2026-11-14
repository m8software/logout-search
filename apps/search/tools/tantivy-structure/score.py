#!/usr/bin/env python3
"""Accuracy of a Tantivy wiki text stage, from the bench's per-query top-10 file.

    score.py mkeval SETS_DIR REGISTRY.sqlite OUTDIR   open sets (test split) -> OUTDIR/eval-{wiki-redirect,typo-wiki}.tsv
                                                      (query, expected title, page_id, doc_id) + eval-queries.txt
    score.py hits   OUT.tsv EVAL.tsv page|doc         hit@1 / hit@10 / n, one line of JSON
    score.py overlap PG.tsv OUT.tsv [RANKS.tsv]       top-10 overlap with Postgres BM25; RANKS (page_id, rank, pop,
                                                      doc_id) maps doc ids in OUT back to page ids

The text stage is only a candidate stage: hit@10 says whether the answer reaches the reranker.
"""
import csv
import json
import os
import sqlite3
import sys


def load(path):
    d = {}
    for line in open(path, encoding="utf-8"):
        q, _, ids = line.rstrip("\n").partition("\t")
        d[q] = [int(x) for x in ids.split(",") if x]
    return d


def mkeval(sets, registry, outdir):
    db = sqlite3.connect(f"file:{registry}?mode=ro", uri=True)
    title2 = {}
    for did, ext, title in db.execute("SELECT doc_id, ext, title FROM registry WHERE doc_id >> 24 = 1"):
        title2.setdefault(title, (ext, did))
    qs = []
    for name in ("wiki-redirect", "typo-wiki"):
        rows = []
        for r in csv.DictReader(open(os.path.join(sets, name + ".tsv"), encoding="utf-8"), delimiter="\t", quoting=csv.QUOTE_NONE):
            if r["split"] != "test" or r["expected"] not in title2:
                continue
            ext, did = title2[r["expected"]]
            rows.append((r["query"], r["expected"], ext, did))
        with open(os.path.join(outdir, f"eval-{name}.tsv"), "w", encoding="utf-8") as f:
            for row in rows:
                f.write("\t".join(map(str, row)) + "\n")
        qs += [r[0] for r in rows]
        print(name, len(rows), file=sys.stderr)
    with open(os.path.join(outdir, "eval-queries.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(dict.fromkeys(qs)) + "\n")


def hits(out, evalf, col):
    res = load(out)
    i = 2 if col == "page" else 3
    n = h1 = h10 = 0
    for line in open(evalf, encoding="utf-8"):
        p = line.rstrip("\n").split("\t")
        ids = res.get(p[0])
        if ids is None:
            continue
        n += 1
        want = int(p[i])
        h1 += bool(ids) and ids[0] == want
        h10 += want in ids[:10]
    print(json.dumps({"eval": os.path.basename(evalf), "n": n, "hit1": round(h1 / n, 3), "hit10": round(h10 / n, 3)}))


def overlap(pg, out, ranks=None):
    a, b = load(pg), load(out)
    if ranks:
        m = {}
        for line in open(ranks, encoding="utf-8"):
            p = line.rstrip("\n").split("\t")
            m[int(p[3])] = int(p[0])
        b = {q: [m.get(x, x) for x in ids] for q, ids in b.items()}
    qs = [q for q in a if q in b and a[q]]
    ov = [len(set(a[q][:10]) & set(b[q][:10])) / max(1, min(10, len(a[q]))) for q in qs]
    t1 = [bool(b[q]) and a[q][0] == b[q][0] for q in qs]
    print(json.dumps({"queries": len(qs), "top10_overlap": round(sum(ov) / len(ov), 3), "top1_agree": round(sum(t1) / len(t1), 3)}))


if __name__ == "__main__":
    c = sys.argv[1]
    if c == "mkeval":
        mkeval(*sys.argv[2:5])
    elif c == "hits":
        hits(*sys.argv[2:5])
    elif c == "overlap":
        overlap(*sys.argv[2:5])
    else:
        raise SystemExit(__doc__)
