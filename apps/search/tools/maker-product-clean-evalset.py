#!/usr/bin/env python3
"""maker-product-clean: the bare-product set (maker-product.tsv, a product asked by its bare word) with the rows a
fair reader would not defend taken out. The old file is not edited; this one sits beside it. No Tranco, no visitor
searches, no network, no engine: the old set, the release's own lookup.sqlite, facts.sqlite and signals.bin (the
Wikipedia pageviews, CC0), read-only.

    tools/maker-product-clean-evalset.py --release DIR [--old evalsets/open/maker-product.tsv] [--out evalsets/open]
                                         [--share 0.5]

What goes (each count is printed, and written to maker-product-clean.report.md):
  1. a gold site on web.archive.org (the product's own site is dead; the archive is not an answer);
  2. a discontinued product: the release's facts hold a dissolved (P576) or end-time (P582) date for its article;
  3. a row whose article the release does not hold under the query's tail key (nothing to measure dominance with);
  4. a row whose product is not the dominant meaning of its key. The key is the query without spaces and
     punctuation (the engine's key). Every Wikipedia article the release lists under that key, as an exact title
     (pos 0) or as the tail of a longer title (pos 1, "Nissan Altima" under "altima"), counts with its pageviews
     (signals.bin wiki.pageviews). A row stays when the products of its key hold at least --share (0.5) of
     the key's pageviews: "keep" / Google Keep stays, "world" / Lotte World goes, "mobile" (three makers, none of
     them the word's meaning) goes.
Then rows of one query merge into ONE row: expected = the union of their sites, "|"-joined (a result on any of them
is a hit), the accept sidecar the union of theirs. A merged row keeps its rows' split when they agree, else the
split of the query (open-evalsets.split_of). Unmerged rows keep their split, entity and stratum.

Output: maker-product-clean.tsv, accept/maker-product-clean.accept.tsv, maker-product-clean.report.md.
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
import re
import sqlite3
import struct
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SEED = "402"
TYPE_SHIFT = 28
COLS = ("query", "category", "expected", "set", "stratum", "split", "entity")
SET = "maker-product-clean"
nk = lambda s: re.sub(r"[^a-z0-9]", "", s.lower())      # noqa: E731  search_api.norm_key


def split_of(entity: str) -> str:
    """open-evalsets.split_of, verbatim."""
    return "test" if int(hashlib.md5(f"{SEED}:split:{entity}".encode()).hexdigest()[:8], 16) % 2 else "dev"


def pageviews(signals: Path):
    with open(signals, "rb") as f:
        head = f.read(12)
        n = struct.unpack("<I", head[8:12])[0]
        hd = json.loads(f.read(n))
        fld = next(x for x in hd["fields"] if x["name"] == "wiki.pageviews")
        f.seek(12 + n + fld["offset"])
        return memoryview(f.read(fld["count"] * 4)).cast("I")


def views_of(pv, doc: int) -> int:
    seq = doc & ((1 << TYPE_SHIFT) - 1)
    return int(pv[seq]) if seq < len(pv) else 0


def key_docs(lk: sqlite3.Connection, key: str) -> list[tuple[int, str, int]]:
    """[(doc_id, name, pos)] of the Wikipedia articles listed under the key as an exact title (0) or a tail (1)."""
    return [(d, n, p) for d, n, p in lk.execute(
        "SELECT DISTINCT i.doc_id, i.name, k.pos FROM suggest_keys k JOIN suggest_items i ON i.doc_id = k.doc_id "
        "WHERE k.key = ? AND k.pos IN (0, 1)", (key,)) if d >> TYPE_SHIFT == 1]


def article_of(lk, key: str, entity: str) -> int | None:
    for d, name, _p in sorted(key_docs(lk, key), key=lambda t: -t[2]):
        if nk(re.sub(r"\s*\(.*\)$", "", name)) == entity or nk(name) == entity:
            return d
    return None


def read_set(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        cols = f.readline().rstrip("\n").split("\t")
        return [dict(zip(cols, ln.rstrip("\n").split("\t"))) for ln in f if ln.strip()]


def read_accept(path: Path) -> dict:
    out: dict = {}
    if path.exists():
        with open(path, encoding="utf-8") as f:
            next(f)
            for ln in f:
                p = ln.rstrip("\n").split("\t")
                if len(p) >= 4:
                    out[(p[0], p[1])] = ([t for t in p[3].split("|") if t], p[2])
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--release", required=True, help="a release directory: lookup.sqlite, facts.sqlite, signals.bin")
    ap.add_argument("--old", default=str(HERE / "evalsets/open/maker-product.tsv"))
    ap.add_argument("--out", default=str(HERE / "evalsets/open"))
    ap.add_argument("--share", type=float, default=0.5)
    a = ap.parse_args()
    rel, out = Path(a.release), Path(a.out)
    lk = sqlite3.connect(f"file:{rel / 'lookup.sqlite'}?mode=ro&immutable=1", uri=True)
    fx = sqlite3.connect(f"file:{rel / 'facts.sqlite'}?mode=ro&immutable=1", uri=True)
    pv = pageviews(rel / "signals.bin")
    rows = read_set(Path(a.old))
    acc = read_accept(out / "accept/maker-product.accept.tsv")
    drop = collections.Counter()
    kept: list[dict] = []
    for r in rows:
        r["_acc"], r["_qid"] = acc.get((r["query"], r["entity"]), ([], ""))
        key = nk(r["query"])
        if "web.archive.org" in r["expected"]:
            drop["gold site is web.archive.org"] += 1
            continue
        doc = article_of(lk, key, r["entity"])
        if doc is None:
            drop["article not found under the query's key in the release"] += 1
            continue
        if fx.execute("SELECT 1 FROM facts WHERE doc_id = ? AND pid IN (95, 97) LIMIT 1", (doc,)).fetchone():
            drop["discontinued (dissolved / end-time date in the release's facts)"] += 1
            continue
        r["_doc"] = doc
        kept.append(r)
    by_q: dict[str, list[dict]] = collections.defaultdict(list)
    for r in kept:
        by_q[r["query"]].append(r)
    result, shares = [], []
    for q, grp in by_q.items():
        key = nk(q)
        cand = {d for d, _n, _p in key_docs(lk, key)}
        total = sum(views_of(pv, d) for d in cand) or 1
        mine = sum(views_of(pv, r["_doc"]) for r in grp)
        share = mine / total
        shares.append(share)
        if share < a.share:
            drop[f"not the dominant meaning of its key (products hold under {a.share:.0%} of the key's article views)"] += len(grp)
            continue
        if len(grp) == 1:
            r = grp[0]
            result.append(({c: r[c] for c in COLS}, r["_acc"], share, r["_qid"]))
            continue
        hosts = list(dict.fromkeys(h for r in grp for h in r["expected"].split("|")))
        covered = lambda t: any(t.split(":", 1)[1].split("/")[0] == h or t.split(":", 1)[1].split("/")[0].endswith("." + h) for h in hosts)  # noqa: E731
        toks = list(dict.fromkeys(t for r in grp for t in r["_acc"] if not covered(t)))
        qids = "|".join(dict.fromkeys(r["_qid"] for r in grp if r["_qid"]))
        splits = {r["split"] for r in grp}
        row = {c: grp[0][c] for c in COLS}
        row["expected"] = "|".join(hosts)
        row["entity"] = "+".join(sorted(r["entity"] for r in grp))
        row["split"] = splits.pop() if len(splits) == 1 else split_of(q)
        row["stratum"] = grp[0]["stratum"] + ",merged:%d" % len(grp)
        result.append((row, toks, share, qids))
        drop["(not dropped) rows merged into another row of the same query"] += len(grp) - 1
    first = {}
    for i, r in enumerate(rows):
        first.setdefault(r["query"], i)
    result.sort(key=lambda t: first[t[0]["query"]])
    for row, _t, share, _q in result:
        row["set"] = SET
    with open(out / f"{SET}.tsv", "w", encoding="utf-8") as f:
        f.write("\t".join(COLS) + "\n")
        for row, _t, _s, _q in result:
            f.write("\t".join(row[c] for c in COLS) + "\n")
    with open(out / f"accept/{SET}.accept.tsv", "w", encoding="utf-8") as f:
        f.write("query\tentity\tqid\taccept\n")
        for row, toks, _s, qid in result:
            if toks and qid:
                f.write(f"{row['query']}\t{row['entity']}\t{qid}\t{'|'.join(toks)}\n")
    n = collections.Counter(r[0]["split"] for r in result)
    rep = [f"# {SET}: built from {Path(a.old).name} ({len(rows)} rows) and release {rel.name}", "",
           f"rows out: {len(result)} (dev {n['dev']}, test {n['test']}); the old set: dev {sum(r['split'] == 'dev' for r in rows)}, "
           f"test {sum(r['split'] == 'test' for r in rows)}", "", "| what | rows |", "|---|---|"]
    rep += [f"| {k} | {v} |" for k, v in drop.most_common()]
    srt = sorted(shares)
    rep += ["", "Dominance share of each surviving query group (products' pageviews / all articles under the key):",
            f"- groups: {len(srt)}; at or over 0.3: {sum(s >= .3 for s in srt)}; 0.5: {sum(s >= .5 for s in srt)}; 0.7: {sum(s >= .7 for s in srt)}; 0.9: {sum(s >= .9 for s in srt)}"]
    (out / f"{SET}.report.md").write_text("\n".join(rep) + "\n")
    print("\n".join(rep))


if __name__ == "__main__":
    main()
