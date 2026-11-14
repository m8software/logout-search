#!/usr/bin/env python3
"""Old layout vs release layout on the same rows: size and top-10 agreement (needs tantivy-py 0.26.2).

    web_compare.py KIND NEW_INDEX ROWS.tsv.zst REGISTRY QUERIES.txt STOPFILE WORKDIR [--analyzer en_pg2] [--ranks RANKS.tsv]

Builds KIND's index the way export.py's cmd_sites builds it today (stored url/domain/title, raw-
tokenised url and domain, fast ranks, rows in domain order, tantivy-py's own writer) from the same
ROWS (web_input.py's columns), then runs every query against both on the BM25 field(s) the engine
searches (sites: head and body; pages and anchors: body) and prints one JSON line:
sizes, mean overlap@10 (as sets of documents), the share of queries whose documents scoring ABOVE the
10th score are the same in both, top-1 agreement (same document; same top score), and
(with --ranks, the popularity ranks the new index was built in) the mean popularity rank of the
top 10 on queries where documents tie at the 10th score. NEW_INDEX returns registry doc_ids; the old one returns its stored
ident; both are compared as idents (the registry maps doc_id back).
"""
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys

import tantivy

kind, new_dir, rows_f, registry, queries_f, stop_f, work = sys.argv[1:8]
an_name = sys.argv[sys.argv.index("--analyzer") + 1] if "--analyzer" in sys.argv else "en_pg"
PG = r"[^\W_]+(?:[.@][^\W_]+)+|[^\W_]+"
stop = [l.strip() for l in open(stop_f) if l.strip()]


def analyzer():
    tok = tantivy.Tokenizer.regex(PG) if an_name == "en_pg2" else tantivy.Tokenizer.simple()
    return (tantivy.TextAnalyzerBuilder(tok).filter(tantivy.Filter.lowercase())
            .filter(tantivy.Filter.custom_stopword(stop)).filter(tantivy.Filter.stemmer("english")).build())


def unesc(f):
    return re.sub(r"\\(.)", lambda m: {"t": "\t", "n": "\n", "r": "\r"}.get(m.group(1), m.group(1)), f) if "\\" in f else f


db = sqlite3.connect(f"file:{registry}?mode=ro", uri=True)
shift = int(dict(db.execute("SELECT key, value FROM meta"))["type_shift"])
typ = 3 if kind == "pages" else 2
ident_of, title_of = {}, {}
for d, ident, title in db.execute(
        "SELECT r.doc_id, coalesce(h.origin || r.path, r.alt, r.path), r.title FROM registry r LEFT JOIN hosts h ON h.id = r.host_id "
        "WHERE r.doc_id >> ? = ?", (shift, typ)):
    ident_of[d] = ident
    title_of[ident] = title or ""
if typ == 2:                                   # layout 3 sites: ident is `path`
    ident_of = dict(db.execute("SELECT doc_id, path FROM registry WHERE doc_id >> ? = 2", (shift,)))

# ---- old layout, built from the same rows
old = os.path.join(work, f"old-{kind}")
shutil.rmtree(old, ignore_errors=True)
os.makedirs(old)
sb = tantivy.SchemaBuilder()
if kind == "sites":
    sb.add_text_field("url", stored=True, tokenizer_name="raw", index_option="basic")
    sb.add_text_field("domain", stored=True, tokenizer_name="raw", index_option="basic")
    sb.add_text_field("title", stored=True, tokenizer_name=an_name, index_option="position")
    sb.add_text_field("head", stored=False, tokenizer_name=an_name, index_option="freq")
    sb.add_text_field("body", stored=False, tokenizer_name=an_name, index_option="freq")
    sb.add_unsigned_field("tranco_rank", stored=False, indexed=False, fast=True)
    sb.add_unsigned_field("cc_hc_pos", stored=False, indexed=False, fast=True)
elif kind == "pages":
    sb.add_text_field("url", stored=True, tokenizer_name="raw", index_option="basic")
    sb.add_text_field("domain", stored=True, tokenizer_name="raw", index_option="basic", fast=True)
    sb.add_text_field("title", stored=True, tokenizer_name=an_name, index_option="position")
    sb.add_text_field("body", stored=False, tokenizer_name=an_name, index_option="freq")
else:
    sb.add_text_field("domain", stored=True, tokenizer_name="raw", index_option="basic")
    sb.add_unsigned_field("sources", stored=True, indexed=False, fast=True)
    sb.add_text_field("body", stored=False, tokenizer_name=an_name, index_option="freq")
ix = tantivy.Index(sb.build(), path=old)
ix.register_tokenizer(an_name, analyzer())
w = ix.writer(heap_size=400_000_000, num_threads=1)
z = subprocess.Popen(["zstd", "-dc", rows_f], stdout=subprocess.PIPE)
rows = sorted((unesc(l.split("\t", 1)[0]), l.rstrip("\n").split("\t")[1:]) for l in (b.decode() for b in z.stdout))
for ident, cols in rows:
    cols = [unesc(c) for c in cols]
    d = tantivy.Document()
    if kind == "sites":
        d.add_text("url", f"https://{ident}/"); d.add_text("domain", ident); d.add_text("title", title_of.get(ident, ""))
        d.add_text("head", cols[0]); d.add_text("body", cols[1]); d.add_unsigned("tranco_rank", 0); d.add_unsigned("cc_hc_pos", 0)
    elif kind == "pages":
        d.add_text("url", ident); d.add_text("domain", ident.split("/")[2] if "//" in ident else ""); d.add_text("title", title_of.get(ident, ""))
        d.add_text("body", cols[0])
    else:
        d.add_text("domain", ident); d.add_unsigned("sources", 1); d.add_text("body", cols[0])
    w.add_document(d)
w.commit(); w.wait_merging_threads()


def tree(p):
    return sum(os.path.getsize(os.path.join(p, f)) for f in os.listdir(p))


new_ix = tantivy.Index.open(new_dir)
new_ix.register_tokenizer(an_name, analyzer())
old_ix = tantivy.Index.open(old)
old_ix.register_tokenizer(an_name, analyzer())
az = analyzer()
fields = ["head", "body"] if kind == "sites" else ["body"]
rank = {}
if "--ranks" in sys.argv:
    for l in open(sys.argv[sys.argv.index("--ranks") + 1]):
        a, _, b = l.rstrip("\n").partition("\t")
        rank[int(a)] = int(b)
ident_rank = {ident_of[d]: rank.get(d, 10 ** 9) for d in ident_of}
old_key = "url" if kind == "pages" else "domain"


def top(ix, field, terms, is_new):
    q = tantivy.Query.boolean_query([(tantivy.Occur.Should, tantivy.Query.term_query(ix.schema, field, t)) for t in terms])
    s = ix.searcher()
    hs = s.search(q, 10).hits
    if is_new:
        ids = s.fast_field_values("doc_id", [a for _x, a in hs]) if hs else []
        return [(ident_of[d], sc, d) for (sc, _a), d in zip(hs, ids)]
    return [(s.doc(a)[old_key][0], sc, None) for sc, a in hs]


seen, qs = set(), []
for l in open(queries_f, encoding="utf-8"):
    q = l.split("\t")[0].strip()
    if q and q not in seen and az.analyze(q):
        seen.add(q)
        qs.append(q)
n = 0
ov = strict_same = top1 = top1_score = 0
tie_q = 0
rk_new = rk_old = 0.0
EPS = 1e-4
for field in fields:
    for q in qs:
        terms = list(dict.fromkeys(az.analyze(q)))
        o, nw = top(old_ix, field, terms, False), top(new_ix, field, terms, True)
        if not o and not nw:
            continue
        n += 1
        so, sn = {x[0] for x in o}, {x[0] for x in nw}
        ov += len(so & sn) / max(len(so), len(sn))
        floor = min([x[1] for x in o] + [x[1] for x in nw]) if len(o) == 10 and len(nw) == 10 else -1
        above_o = {x[0] for x in o if x[1] > floor + EPS}
        above_n = {x[0] for x in nw if x[1] > floor + EPS}
        strict_same += above_o == above_n
        if o and nw:
            top1 += o[0][0] == nw[0][0]
            top1_score += abs(o[0][1] - nw[0][1]) < EPS
        if rank and len(o) == 10 and len(nw) == 10 and sum(1 for x in nw if abs(x[1] - floor) <= EPS) > 1:
            tie_q += 1
            rk_new += sum(ident_rank[x[0]] for x in nw) / 10
            rk_old += sum(ident_rank.get(x[0], 10 ** 9) for x in o) / 10
print(json.dumps({"kind": kind, "queries_x_fields": n, "new_bytes": tree(new_dir), "old_bytes": tree(old),
                  "old_stored_only_bytes": sum(os.path.getsize(os.path.join(old, f)) for f in os.listdir(old) if f.endswith(".store")),
                  "mean_overlap_at_10": round(ov / n, 4), "queries_same_above_10th_score": round(strict_same / n, 4),
                  "top1_same_doc": round(top1 / n, 4), "top1_same_score": round(top1_score / n, 4), "tie_queries": tie_q,
                  "mean_pop_rank_top10_new": round(rk_new / tie_q) if tie_q else None,
                  "mean_pop_rank_top10_old": round(rk_old / tie_q) if tie_q else None}))
