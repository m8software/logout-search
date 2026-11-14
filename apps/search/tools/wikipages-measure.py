#!/usr/bin/env python3
"""wikipages-measure.py PAGES.sqlite RELEASE_SITES.txt OUT.json: bytes per column and row counts for the candidate cuts
of the cited-pages table (2026-11-10). One scan, no index. Bytes are the SHIPPED row: url + title + one sentence
capped at 200 characters + domain (UTF-8), before any index. Run niced, in a container with 2 CPUs / 4 GB.

Cuts (rows, bytes): all; cited by 2+ / 3+; has a sentence; has sentence and title; top N per site (N = 3, 10, 30; counts
exact, bytes = rows x the mean row of the pool); on / off the sites of the release (lookup.sqlite sites.domain).
"""
import collections
import json
import sqlite3
import sys
import time

SENT_CAP = 200


def main(db_path, sites_path, out):
    t0 = time.time()
    release = set(open(sites_path, encoding="utf-8").read().split())
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    cur = con.execute("SELECT url, domain, title, n_articles, n_sentences, contexts FROM pages")
    # key: (n bucket 1/2/3, has sentence, has title, in release) -> [rows, url, domain, title, contexts_json, sent<=cap, section]
    agg = collections.defaultdict(lambda: [0, 0, 0, 0, 0, 0, 0])
    dom = {}                       # domain -> [rows, rows with sentence, rows n>=2 with sentence and title]
    n = 0
    for url, d, title, na, ns, ctx in cur:
        has_s = 0
        s_len = sec_len = 0
        if ns:
            try:
                best = json.loads(ctx)
            except ValueError:
                best = []
            if best:
                has_s = 1
                s_len = len(best[0][2][:SENT_CAP].encode("utf-8"))
                sec_len = len(best[0][1].encode("utf-8"))
        has_t = 1 if title else 0
        b = agg[(1 if na < 2 else 2 if na < 3 else 3, has_s, has_t, 1 if d in release else 0)]
        b[0] += 1
        b[1] += len(url.encode("utf-8"))
        b[2] += len(d.encode("utf-8"))
        b[3] += len(title.encode("utf-8"))
        b[4] += len(ctx.encode("utf-8"))
        b[5] += s_len
        b[6] += sec_len
        e = dom.get(d)
        if e is None:
            e = dom[d] = [0, 0, 0]
        e[0] += 1
        e[1] += has_s
        e[2] += 1 if (na >= 2 and has_s and has_t) else 0
        n += 1
        if n % 5_000_000 == 0:
            print(f"{n:,} rows, {(time.time() - t0) / 60:.1f} min", file=sys.stderr, flush=True)
    res = {"rows": n, "minutes": round((time.time() - t0) / 60, 1),
           "agg": [{"nb": k[0], "sent": k[1], "title": k[2], "release": k[3], "rows": v[0], "url": v[1], "domain": v[2],
                    "title_b": v[3], "contexts_b": v[4], "sent200_b": v[5], "section_b": v[6]} for k, v in sorted(agg.items())],
           "domains": len(dom),
           "topn": {str(N): {"all": sum(min(N, e[0]) for e in dom.values()),
                             "with_sentence": sum(min(N, e[1]) for e in dom.values()),
                             "n2_sent_title": sum(min(N, e[2]) for e in dom.values())} for N in (3, 10, 30)}}
    json.dump(res, open(out, "w"), indent=1)
    print(f"done {n:,} rows in {res['minutes']} min", file=sys.stderr)


if __name__ == "__main__":
    main(*sys.argv[1:4])
