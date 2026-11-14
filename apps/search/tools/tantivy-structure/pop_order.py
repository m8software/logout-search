#!/usr/bin/env python3
"""Popularity order for the Wikipedia Tantivy index (release format v1, rule 5).

    pop_order.py ranks  EXPORT.tsv.zst POP.tsv RANKS.tsv   page_id -> rank (0 = most popular)
    pop_order.py signals REGISTRY.sqlite SIGNALS.bin RANKS.tsv   the release's own numbers (below)
    pop_order.py prefix EXPORT.tsv.zst RANKS.tsv [--doc-id]      export lines prefixed with a %08d rank
                                                           (pipe to `LC_ALL=C sort`, then `cut -f2-`);
                                                           --doc-id also replaces page_id by the doc_id

`signals` ranks by the release's signals.bin (wiki.pageviews = last month's views, wiki.sitelinks =
language editions): pop = 0.5 * min(1, ln(max(sitelinks,1)) / ln 250) + 0.5 * min(1, ln(1+views) /
ln 500001), the suggester's article_pop. RANKS.tsv is `page_id, rank, pop, doc_id` (doc_id from the
registry, type 1: page_id = registry.ext).

Web indexes (sites, pages, anchors; web_input.py, tantivy-structure build --kind):
    pop_order.py hostranks REGISTRY SIGNALS OUT      host TAB rank  (a consistent registry + signals.bin pair)
    pop_order.py sitepop   REGISTRY HOSTRANKS OUT    doc_id TAB rank for every site and page of REGISTRY
    pop_order.py sprefix   ROWS|- RANKS              ROWS (doc_id first; .zst ok) prefixed with a %08d rank
`hostranks` rule: a site's key is its own rank (best of site.own_rank, site.nav_rank, lower is more popular;
a signals.bin from before G3 D2 has site.tranco_rank in place of site.own_rank); without one, 2^31 +
site.cc_hc_pos (Common Crawl's host rank, always after every ranked site);
a site with neither is unranked. `sitepop` puts sites in key order (unranked last), ties and unranked by
doc_id, ranks 0.. dense. A page takes its host's key (pages of one site stay together, in doc_id order);
an anchor document has its site's doc_id and so its site's rank. Every ordering is total: same
inputs, same order (rule 7). `sprefix` refuses a doc_id that RANKS lacks.

POP.tsv is `title<TAB>pop` (0..1). On testbox it comes from suggest_items (kind wiki/site, the
suggest builder's article_pop(sitelinks, pageviews)), because wiki_pop itself is not on the
serving DB. Articles without a pop value get 0. Order: pop descending, then page_id
ascending, so the order is total and the same on every run (rule 7: fixed input order).
"""
import io
import subprocess
import sys


def export_lines(path):
    z = subprocess.Popen(["zstd", "-dc", path], stdout=subprocess.PIPE)
    yield from io.TextIOWrapper(z.stdout, encoding="utf-8", errors="surrogateescape", newline="\n")
    if z.wait() != 0:
        raise SystemExit(f"zstd -dc {path} failed")


def ranks(export, popfile, out):
    pop = {}
    for line in open(popfile, encoding="utf-8"):
        t, _, v = line.rstrip("\n").rpartition("\t")
        pop[t] = float(v)
    ids = []
    hit = 0
    for line in export_lines(export):
        parts = line.split("\t", 2)
        if len(parts) < 3:
            continue
        pid = int(parts[0])
        # COPY text escapes in titles are rare; the pop keys are raw titles
        title = parts[1].replace("\\t", "\t").replace("\\\\", "\\")
        p = pop.get(title, 0.0)
        hit += p > 0
        ids.append((-p, pid))
    ids.sort()
    with open(out, "w", encoding="utf-8") as f:
        for r, (negp, pid) in enumerate(ids):
            f.write(f"{pid}\t{r}\t{-negp:.6f}\n")
    print(f"{len(ids)} articles ranked, {hit} with a pop value > 0", file=sys.stderr)


def signals(registry, sigfile, out):
    import array
    import json
    import math
    import sqlite3
    import struct
    with open(sigfile, "rb") as f:
        assert f.read(8) == b"LOSIGNL1"
        n = struct.unpack("<I", f.read(4))[0]
        head = json.loads(f.read(n))
        base = 12 + n
        arrs = {}
        for fld in head["fields"]:
            if fld["name"] in ("wiki.pageviews", "wiki.sitelinks"):
                a = array.array("I" if fld["dtype"] == "<u4" else "H")
                f.seek(base + fld["offset"])
                a.frombytes(f.read(fld["count"] * a.itemsize))
                arrs[fld["name"]] = a
    pv, sl = arrs["wiki.pageviews"], arrs["wiki.sitelinks"]
    db = sqlite3.connect(f"file:{registry}?mode=ro", uri=True)
    rows = []
    for did, ext in db.execute("SELECT doc_id, ext FROM registry WHERE doc_id >> 24 = 1"):
        seq = did & 0xFFFFFF
        pop = (0.5 * min(1.0, math.log(max(sl[seq], 1)) / math.log(250))
               + 0.5 * min(1.0, math.log(1 + pv[seq]) / math.log(500001)))
        rows.append((-round(pop, 6), did, ext))
    rows.sort()
    with open(out, "w", encoding="utf-8") as f:
        for r, (negp, did, ext) in enumerate(rows):
            f.write(f"{ext}\t{r}\t{-negp:.6f}\t{did}\n")
    print(f"{len(rows)} wiki documents ranked from signals.bin", file=sys.stderr)


def prefix(export, rankfile, with_doc_id=False):
    rank, docid = {}, {}
    for line in open(rankfile, encoding="utf-8"):
        parts = line.rstrip("\n").split("\t")
        rank[int(parts[0])] = int(parts[1])
        if with_doc_id:
            docid[int(parts[0])] = parts[3]
    out = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="surrogateescape", newline="\n")
    for line in export_lines(export):
        pid, _, rest = line.partition("\t")
        if pid.isdigit() and int(pid) in rank:
            p = int(pid)
            out.write(f"{rank[p]:08d}\t{docid[p] + chr(9) + rest if with_doc_id else line}")
    out.flush()


def hostranks(registry, sigfile, out):
    import array
    import json
    import sqlite3
    import struct
    want = ("site.own_rank", "site.nav_rank", "site.cc_hc_pos", "site.tranco_rank")   # tranco_rank: old files only
    with open(sigfile, "rb") as f:
        assert f.read(8) == b"LOSIGNL1"
        n = struct.unpack("<I", f.read(4))[0]
        head = json.loads(f.read(n))
        base = 12 + n
        arrs = {}
        for fld in head["fields"]:
            if fld["name"] in want:
                a = array.array("I")
                assert fld["dtype"] == "<u4"
                f.seek(base + fld["offset"])
                a.frombytes(f.read(fld["count"] * 4))
                arrs[fld["name"]] = a
    db = sqlite3.connect(f"file:{registry}?mode=ro", uri=True)
    shift = int(dict(db.execute("SELECT key, value FROM meta"))["type_shift"])
    if head.get("type_shift") != shift:
        raise SystemExit(f"signals.bin type_shift {head.get('type_shift')} is not the registry's {shift}: not a pair")
    tr, nv, cc = arrs.get("site.own_rank") or arrs.get("site.tranco_rank"), arrs.get("site.nav_rank"), arrs.get("site.cc_hc_pos")
    mask = (1 << shift) - 1
    best = {}
    # layout 3: a site's `path` is its host. The previous layout (no meta.layout) had one row per
    # crawled url, host in hosts.origin: the host takes its best row's key.
    for did, host in db.execute("SELECT r.doc_id, coalesce(substr(h.origin, instr(h.origin, '://') + 3), r.path) FROM registry r "
                                "LEFT JOIN hosts h ON h.id = r.host_id WHERE r.doc_id >> ? = 2 ORDER BY r.doc_id", (shift,)):
        seq = did & mask
        t = [v for v in (tr[seq] if tr and seq < len(tr) else 0, nv[seq] if nv and seq < len(nv) else 0) if v]
        c = cc[seq] if cc and seq < len(cc) else 0
        key = min(t) if t else (2 ** 31 + c if c else 0)
        if key and key < best.get(host, key + 1):
            best[host] = key
    with open(out, "w", encoding="utf-8") as o:
        for host in sorted(best):
            o.write(f"{host}\t{best[host]}\n")
    n = len(best)
    print(f"{n} hosts with a rank key", file=sys.stderr)


def sitepop(registry, hostfile, out):
    import sqlite3
    from urllib.parse import urlsplit
    key = {}
    for line in open(hostfile, encoding="utf-8"):
        h, _, k = line.rstrip("\n").partition("\t")
        key[h] = int(k)
    db = sqlite3.connect(f"file:{registry}?mode=ro", uri=True)
    shift = int(dict(db.execute("SELECT key, value FROM meta"))["type_shift"])
    none = 2 ** 62
    rows = []
    for did, host in db.execute("SELECT doc_id, path FROM registry WHERE doc_id >> ? = 2", (shift,)):
        rows.append((key.get(host, none), did))
    for did, origin, alt in db.execute("SELECT r.doc_id, h.origin, r.alt FROM registry r LEFT JOIN hosts h ON h.id = r.host_id "
                                       "WHERE r.doc_id >> ? = 3", (shift,)):
        host = origin.split("://", 1)[-1] if origin else (urlsplit(alt or "").hostname or "")
        rows.append((key.get(host, none), did))
    sites = sorted(r for r in rows if r[1] >> shift == 2)
    pages = sorted(r for r in rows if r[1] >> shift == 3)
    with open(out, "w", encoding="utf-8") as o:
        for rank, (_k, did) in enumerate(sites):
            o.write(f"{did}\t{rank}\n")
        for rank, (_k, did) in enumerate(pages):
            o.write(f"{did}\t{rank}\n")
    print(f"{len(sites)} sites, {len(pages)} pages ranked ({sum(1 for r in rows if r[0] == none)} without a key)", file=sys.stderr)


def sprefix(rows, rankfile):
    rank = {}
    for line in open(rankfile, encoding="utf-8"):
        d, _, r = line.rstrip("\n").partition("\t")
        rank[d] = int(r)
    out = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="surrogateescape", newline="\n")
    if rows == "-":
        src = io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8", errors="surrogateescape", newline="\n")
    elif rows.endswith(".zst"):
        src = export_lines(rows)
    else:
        src = open(rows, encoding="utf-8", errors="surrogateescape", newline="\n")
    for line in src:
        d = line.partition("\t")[0]
        if d not in rank:
            raise SystemExit(f"doc_id {d!r} has no rank in {rankfile} — refusing")
        out.write(f"{rank[d]:08d}\t{line}")
    out.flush()


if __name__ == "__main__":
    if sys.argv[1] == "hostranks":
        hostranks(*sys.argv[2:5])
    elif sys.argv[1] == "sitepop":
        sitepop(*sys.argv[2:5])
    elif sys.argv[1] == "sprefix":
        sprefix(*sys.argv[2:4])
    elif sys.argv[1] == "ranks":
        ranks(sys.argv[2], sys.argv[3], sys.argv[4])
    elif sys.argv[1] == "signals":
        signals(sys.argv[2], sys.argv[3], sys.argv[4])
    elif sys.argv[1] == "prefix":
        prefix(sys.argv[2], sys.argv[3], "--doc-id" in sys.argv)
    else:
        raise SystemExit(__doc__)
