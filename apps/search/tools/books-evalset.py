#!/usr/bin/env python3
"""Book eval sets for Project Gutenberg links (ADR-064/065): no Jev, no LLM, no judge for the exact-answer set.

    tools/books-evalset.py [--ssh indexhost] [--out tools/evalsets/open] [--per 120] [--judged-n 200]

Writes, next to the other open sets:

  books.tsv             "<title> gutenberg" -> the book's Gutenberg ebook page; "<author> gutenberg" -> the
                        author's shelf. Expected answers are Wikidata's own IDs (P2034 ebook, P1938 author), loaded
                        as wiki_links platforms `gutenberg` and `gutenberg-author`. The catalog match
                        (`gutenberg-catalog`) is NOT used: it would be checking itself.
  ambiguous-books.tsv  names that two entities share: not scored.
  judged-books-queries.tsv  ~200 queries people type ("read <title> online", "<title> full text") from fixed
                        templates over Wikidata-ID books and catalog titles, for utility/search-answer-eval.py --queries
                        with Clef as judge. Dev half is judged first; the test half stays unread until the end.

Sampling is deterministic (md5 of a fixed seed and the entity); the dev/test split is open-evalsets.split_of
by ENTITY (the normalised Wikipedia title when the thing has an article, else the Wikidata QID), the same key as every
other set. The data comes from the build database on indexhost (read-only), after `wiki-links --platforms
gutenberg,gutenberg-author` has run. Licence: CC0 (Wikidata).
"""
from __future__ import annotations

import argparse
import csv
import importlib.util
import io
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("open_evalsets", HERE / "open-evalsets.py")
OE = importlib.util.module_from_spec(spec)
sys.modules["open_evalsets"] = OE
spec.loader.exec_module(OE)

COLS = OE.COLS
BUCKETS = (("30+", 30), ("10-29", 10), ("1-9", 0))
JUDGED_TEMPLATES = ("read {} online", "{} full text", "{} free ebook", "{} online free", "{} pdf")


def psql_rows(ssh: str, sql: str) -> list[list[str]]:
    cmd = ["ssh", "-o", "BatchMode=yes", ssh,
           "sudo -n docker exec -i search-postgres psql -U search -d search -v ON_ERROR_STOP=1 -q "
           f"-c \"COPY ({sql.replace(chr(34), chr(92) + chr(34))}) TO STDOUT WITH (FORMAT csv)\""]
    out = subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=600).stdout
    return list(csv.reader(io.StringIO(out)))


def bucket(sl: int) -> str:
    return next(name for name, lo in BUCKETS if sl >= lo)


def fetch(ssh: str):
    """(rows of Wikidata gutenberg ids, catalog titles). Row: platform, qid, value, url, name, wiki_title, sitelinks."""
    wd = psql_rows(ssh, "SELECT platform, qid, value, url, name, coalesce(wiki_title, ''), sitelinks FROM wiki_links "
                        "WHERE platform IN ('gutenberg', 'gutenberg-author') AND name IS NOT NULL AND length(name) > 2")
    cat = psql_rows(ssh, "SELECT qid, name, coalesce(wiki_title, ''), sitelinks FROM wiki_links WHERE platform = 'gutenberg-catalog' "
                         "AND name IS NOT NULL AND length(name) > 2")
    return wd, cat


def entity_of(qid: str, wiki_title: str) -> str:
    return OE.norm_key(wiki_title) if wiki_title else qid


def build(wd, per: int):
    by_qid = defaultdict(lambda: {"urls": set(), "name": "", "kind": "", "wt": "", "sl": 0})
    for platform, qid, value, url, name, wt, sl in wd:
        e = by_qid[(platform, qid)]
        e["urls"].add(OE.norm_url(url))
        e["name"], e["kind"], e["wt"], e["sl"] = name, platform, wt, max(e["sl"], int(sl or 0))
    owners = defaultdict(set)                 # name key -> every entity (book or author) that answers to it
    for (platform, qid), e in by_qid.items():
        owners[OE.norm_key(e["name"])].add(qid)
    cands, ambiguous = [], []
    for (platform, qid), e in by_qid.items():
        name = e["name"]
        key = OE.norm_key(name)
        if len(key) < 3 or len(name.split()) > 5 or OE.NOT_A_NAME.match(name.lower()):
            continue
        if len(owners[key]) > 1:
            ambiguous.append((f"{name.lower()} gutenberg", "books", len(owners[key])))
            continue
        b = bucket(e["sl"])
        kind = "book" if platform == "gutenberg" else "author"
        cands.append((entity_of(qid, e["wt"]), name, f"{kind}|{b}", qid, "|".join(sorted(e["urls"])), kind, b))
    picked = OE.take(cands, per, key_of=lambda c: c[2])
    rows = [{"query": f"{name} gutenberg".lower(), "category": "books", "expected": exp, "set": "books",
             "stratum": f"kind:{kind},sitelinks:{b}", "entity": ent}
            for ent, name, _s, _q, exp, kind, b in picked]
    return rows, ambiguous


def judged_queries(wd, cat, n: int):
    """Fixed templates over book titles (Wikidata-ID books first, then catalog matches), one template per title chosen by
    hash, n queries in all, spread over dev and test by the entity split."""
    titles = {}
    for platform, qid, _v, _u, name, wt, sl in wd:
        if platform == "gutenberg":
            titles.setdefault(qid, (name, wt, "wikidata"))
    for qid, name, wt, sl in cat:
        titles.setdefault(qid, (name, wt, "catalog"))
    ranked = sorted(titles.items(), key=lambda kv: OE.h("judged-books", kv[0]))
    out = []
    for qid, (name, wt, src) in ranked:
        if len(name.split()) > 5 or OE.NOT_A_NAME.match(name.lower()):
            continue
        t = JUDGED_TEMPLATES[int(OE.h("tpl", qid)[:4], 16) % len(JUDGED_TEMPLATES)]
        ent = entity_of(qid, wt)
        out.append({"query": t.format(name.lower()), "title": name, "source": src, "entity": ent, "split": OE.split_of(ent)})
        if len(out) >= n:
            break
    return out


def write_exclude(path: Path, books, judged) -> int:
    """Add the TEST-half queries to exclude.tsv, as typed and as the engine's key: books.tsv rows as set `books` (source
    open) into the open block, the judged book queries as `judged-books` (source judged-books) into the judged block
    below it. Both blocks stay sorted by (key, form, set, split, source), as sq-eval.py --write-exclude writes them;
    rerunning replaces these rows."""
    head, rows = [], []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("#") or line.startswith("key\t"):
            head.append(line)
        elif line:
            rows.append(tuple(line.split("\t")))
    rows = [r for r in rows if r[2] not in ("books", "judged-books")]
    new_open, new_judged = set(), set()
    for lst, st, src, dest in ((books, "books", "open", new_open), (judged, "judged-books", "judged-books", new_judged)):
        for r in lst:
            if r["split"] == "test":
                q = " ".join(r["query"].lower().split())
                dest.add((q, "query", st, "test", src))
                dest.add((OE.norm_key(r["query"]), "norm", st, "test", src))
    block_open = sorted([r for r in rows if not r[4].startswith("judged")] + list(new_open))
    block_judged = sorted([r for r in rows if r[4].startswith("judged")] + list(new_judged))
    path.write_text("\n".join(head + ["\t".join(r) for r in block_open + block_judged]) + "\n", encoding="utf-8")
    return len(new_open) + len(new_judged)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ssh", default="indexhost")
    ap.add_argument("--out", default=str(HERE / "evalsets" / "open"))
    ap.add_argument("--per", type=int, default=120, help="queries per (kind, sitelinks bucket)")
    ap.add_argument("--judged-n", type=int, default=200)
    ap.add_argument("--no-exclude", action="store_true", help="do not touch exclude.tsv")
    a = ap.parse_args()
    wd, cat = fetch(a.ssh)
    print(f"Wikidata gutenberg rows: {len(wd):,}; catalog rows: {len(cat):,}", file=sys.stderr)
    rows, amb = build(wd, a.per)
    for r in rows:
        r["split"] = OE.split_of(r["entity"])
    out = Path(a.out)
    with open(out / "books.tsv", "w", encoding="utf-8") as f:
        f.write("\t".join(COLS) + "\n")
        for r in sorted(rows, key=lambda r: (r["split"], r["stratum"], r["query"])):
            f.write("\t".join(str(r[c]) for c in COLS) + "\n")
    with open(out / "ambiguous-books.tsv", "w", encoding="utf-8") as f:
        f.write("query\tset\tentities\n")
        for q, s_, n in sorted(set(amb)):
            f.write(f"{q}\t{s_}\t{n}\n")
    jq = judged_queries(wd, cat, a.judged_n)
    with open(out / "judged-books-queries.tsv", "w", encoding="utf-8") as f:
        f.write("query\ttitle\tsource\tsplit\tentity\n")
        for r in sorted(jq, key=lambda r: (r["split"], r["query"])):
            f.write("\t".join(str(r[c]) for c in ("query", "title", "source", "split", "entity")) + "\n")
    n_ex = 0 if a.no_exclude else write_exclude(out / "exclude.tsv", rows, jq)
    sp = {s: sum(1 for r in rows if r["split"] == s) for s in ("dev", "test")}
    print(f"books {len(rows)}  dev {sp['dev']}  test {sp['test']}  ambiguous {len(set(amb))}  "
          f"judged queries {len(jq)} (dev {sum(1 for r in jq if r['split'] == 'dev')})  exclude rows added {n_ex}  CC0 (Wikidata)",
          file=sys.stderr)


if __name__ == "__main__":
    main()
