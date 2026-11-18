#!/usr/bin/env python3
"""kinds.sqlite: the open-signals kinds tier as one release file (own licence; owner decision D2, 2026-10-04).

    release_v1.py kinds REGISTRY OUT.sqlite RELEASE_ID DUMP.sql.gz [--vecs kind-vecs.tsv] [--embed MODEL_DIR]

Input is the open-tier tables as a pg_dump plain-format text dump (kinds_judge, kind_members,
kind_docs, kinds_open_rows; Postgres is not needed to read it). No legacy column may be in it: this
module reads no column named jev, builds none, and `release_v1.sqlguard_file` refuses the file if one
appears (kinds_guard below).

What ships, and why that and nothing else (the question is "which queries does search_api's kinds tier
ask", answered in serve/api/kinds_release.py):
    kind_keys(id, key, nwords, n)           the kind names; n = member sites (KIND_MIN_MEMBERS is applied at query time)
    kind_key_words(word, kid)               containment match: every query word in a name's words (KIND_KEYS_SQL)
    kind_members(kid, doc_id, votes, conf, lead)
                                            the open evidence per (kind, site); conf/lead from importer/kinds_open.py with
                                            the Clef judge hook already applied (the dump's rows are the judged build)
    kind_docs(doc_id, kinds, sources, sitelinks, pageviews, country)
                                            one row per site; `kinds` is the label string (Wikidata CC0, Wikipedia CC BY-SA,
                                            DMOZ CC BY 3.0, UT1 CC BY-SA, ...), the FTS5 index below searches it
    kind_docs_fts                           FTS5 (porter) over kind_docs.kinds, rowid = doc_id: the fused "kinds BM25" list
    kind_vecs(kid, v)                       float16 bge-small-en-v1.5 vectors of the kind names with >= 2 liftable sites
The key of a site is its REGISTRY doc_id (type 2), as anchor-phrases.sqlite: url, title and description are NOT
here (kind_docs.title / descr hold crawled page text: ADR-059), they come from the registry and lookup.sqlite
at serve time. A domain the registry lacks is dropped here and counted. cc_hc_pos is not here either: signals.bin
carries it. tranco_rank is not here (D2: Tranco does not ship; the column was always NULL in the open build).
Not shipped: kinds_judge (its verdicts are already folded into conf/lead; no serving query reads it) and
kinds_open_rows (a 9.7M-row build intermediate). Row counts of the dump are checked against EXPECTED.
"""
import gzip
import json
import math
import os
import re
import sqlite3
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import registry_idents as ri  # noqa: E402

FORMAT_VERSION, APPLICATION_ID = 1, 0x6C6F7331           # = export.py; not imported: that module wants psycopg's stub
SITE = ri.TYPES["site"]
# kind_docs: the brief said 498,691; the judged dump holds 498,312 (self-consistent: every kind_members domain has its
# kind_docs row, so the 379 difference touches no member; cause not found - the dump is the only copy on this workstation).
EXPECTED = {"kinds_judge": 351_891, "kind_members": 1_475_137, "kind_docs": 498_312}
LIFT_CONF = 0.7           # = importer/kinds_open.py LIFT_CONF and search_api KIND_LIFT_CONF (test-search-kinds-release.sh checks)
LIFT_MIN_SITES = 2        # a kind name is embedded when this many sites are liftable (tools/embed-kinds.py's rule)
VEC_MODEL, VEC_DIM = "bge-small-en-v1.5", 384
LICENCE = ("CC BY-SA 4.0 (combined label set: Wikidata CC0, Wikipedia categories/lists CC BY-SA 4.0, UT1 CC BY-SA 4.0, "
           "DMOZ CC BY 3.0, Homebrew BSD-2, public-apis MIT, awesome lists per list, Flathub/Debian facts)")
SOURCE = "open kinds build of 2026-10-04: site labels from the sources named in the licence"
DOC_COLS = ("domain", "url", "title", "descr", "kinds", "sources", "tranco_rank", "cc_hc_pos", "sitelinks", "pageviews",
            "inlinks", "country", "search_text")
TABLE_COLS = {"kind_docs": DOC_COLS, "kind_members": ("key", "domain", "votes", "conf", "lead"),
              "kinds_judge": ("stage", "key", "domain", "p"), "kinds_open_rows": ("key", "domain", "family", "full_row", "lead_row")}

_COPY = re.compile(r"^COPY public\.(\w+) \(([^)]*)\) FROM stdin;$")
_ESC = re.compile(r"\\(.)")
_MAP = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", "v": "\v", "\\": "\\"}
_OCT = re.compile(r"\\([0-7]{1,3})")


def unescape(v: str):
    """One COPY text field: \\N is NULL; \\t \\n \\r \\b \\f \\v \\\\ and \\ooo are the usual escapes."""
    if v == "\\N":
        return None
    if "\\" not in v:
        return v
    v = _OCT.sub(lambda m: chr(int(m.group(1), 8)), v)
    return _ESC.sub(lambda m: _MAP.get(m.group(1), m.group(1)), v)


def f32(x) -> float:
    """A Postgres `real` as the double the engine's arithmetic sees (real >= numeric compares as double)."""
    return None if x is None else struct.unpack("f", struct.pack("f", float(x)))[0]


def parse_dump(path: str):
    """-> (docs, members, counts). docs: domain -> (kinds, sources, sitelinks, pageviews, country); members: list of
    (key, domain, votes, conf, lead); counts: rows per table of the dump (kinds_open_rows is only counted, its fields
    are never decoded; kinds_judge is counted and shape-checked)."""
    with open(path, "rb") as probe:   # by content, not by name: the build pipeline mounts inputs as /in/NAME
        op = gzip.open if probe.read(2) == b"\x1f\x8b" else open
    docs, members, counts, table, cols = {}, [], {}, None, None
    with op(path, "rt", encoding="utf-8", newline="\n") as f:
        for line in f:
            if table is None:
                m = _COPY.match(line.rstrip("\n"))
                if m:
                    table, cols = m.group(1), tuple(c.strip() for c in m.group(2).split(","))
                    if table in TABLE_COLS and cols != TABLE_COLS[table]:
                        sys.exit(f"kinds: {table} has columns {cols}, this builder reads {TABLE_COLS[table]}")
                    counts[table] = 0
                continue
            if line == "\\.\n":
                table = None
                continue
            counts[table] += 1
            if table == "kinds_open_rows":
                continue
            p = line.rstrip("\n").split("\t")
            if len(p) != len(cols):
                sys.exit(f"kinds: {table} line {counts[table]} has {len(p)} fields, want {len(cols)}")
            if table == "kind_docs":
                r = [unescape(x) for x in p]
                if r[0] in docs:
                    sys.exit(f"kinds: kind_docs domain {r[0]!r} twice (the table has a unique index on domain)")
                docs[r[0]] = (r[4], int(r[5]) if r[5] is not None else None, int(r[8]) if r[8] is not None else None,
                              int(r[9]) if r[9] is not None else None, r[11])
            elif table == "kind_members":
                r = [unescape(x) for x in p]
                members.append((r[0], r[1], f32(r[2]), f32(r[3]), f32(r[4])))
            elif table == "kinds_judge":
                unescape(p[3])
    return docs, members, counts


def check_counts(counts: dict, expect: dict) -> None:
    bad = [f"{t}: {counts.get(t)} rows, expected {n}" for t, n in expect.items() if counts.get(t) != n]
    if bad:
        sys.exit("kinds: the dump is not the judged open build - " + "; ".join(bad))


def read_vecs(path: str) -> dict:
    """tools/embed-kinds.py output: `key TAB v1,v2,...` -> {key: tuple of floats}."""
    out = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            k, _t, v = line.rstrip("\n").partition("\t")
            if k:
                out[k] = tuple(float(x) for x in v.split(","))
    return out


class Embedder:
    """The engine's bge-small-en-v1.5 embedding of a short phrase (search_api._Embedder: CLS, unit length, no
    instruction prefix), batched. Needs onnxruntime + tokenizers; used for the kind names read_vecs lacks."""

    def __init__(self, model_dir: str):
        import numpy as np
        import onnxruntime as ort
        from tokenizers import Tokenizer
        self.np = np
        self.tok = Tokenizer.from_file(os.path.join(model_dir, "tokenizer.json"))
        self.tok.enable_truncation(max_length=128)
        self.sess = ort.InferenceSession(os.path.join(model_dir, "model.onnx"), providers=["CPUExecutionProvider"])
        self.inputs = {i.name for i in self.sess.get_inputs()}

    def batch(self, texts):
        np = self.np
        enc = [self.tok.encode(t) for t in texts]
        width = max(len(e.ids) for e in enc)
        ids = np.array([e.ids + [0] * (width - len(e.ids)) for e in enc], dtype=np.int64)
        mask = np.array([e.attention_mask + [0] * (width - len(e.ids)) for e in enc], dtype=np.int64)
        feed = {"input_ids": ids, "attention_mask": mask}
        if "token_type_ids" in self.inputs:
            feed["token_type_ids"] = np.zeros_like(ids)
        cls = self.sess.run(None, feed)[0][:, 0]
        return cls / np.linalg.norm(cls, axis=1, keepdims=True)


def f16_blob(v) -> bytes:
    import numpy as np
    return np.asarray(v, dtype=np.float16).tobytes()


def build(dump: str, registry: str, out: str, release: str, vecs: str = None, embed_dir: str = None, expect: dict = None,
          stamp=None, finish=None, new_db=None) -> dict:
    t0 = time.time()
    expect = EXPECTED if expect is None else expect
    docs, members, counts = parse_dump(dump)
    check_counts(counts, expect)
    if stamp is None:                           # standalone: release_v1 passes its own helpers
        import release_v1 as rv
        stamp, finish, new_db = rv.stamp, rv.finish, rv.new_db
    ids = {}
    c = sqlite3.connect(":memory:")
    ri.idmap(c, registry, "idmap", types=(SITE,))
    ids = dict(c.execute("SELECT ident, doc_id FROM idmap WHERE type = ?", (SITE,)))
    c.close()
    doc_of = {d: ids[d] for d in docs if d in ids}
    stray = sum(1 for m in members if m[1] not in docs)
    if stray:
        sys.exit(f"kinds: {stray} kind_members rows name a domain with no kind_docs row (the build guarantees none)")
    kept = [m for m in members if m[1] in doc_of]
    best: dict = {}
    for k, d, votes, conf, lead in kept:
        row = best.get((k, doc_of[d]))
        if row is None or (conf or 0) > (row[1] or 0):
            best[(k, doc_of[d])] = (votes, conf, lead)
    keys = sorted({k for k, _ in best})
    kid = {k: i + 1 for i, k in enumerate(keys)}
    n_of: dict = {}
    lift: dict = {}
    for (k, _d), (_v, conf, _l) in best.items():
        n_of[k] = n_of.get(k, 0) + 1
        if conf is not None and conf >= LIFT_CONF:
            lift[k] = lift.get(k, 0) + 1
    liftable = [k for k in keys if lift.get(k, 0) >= LIFT_MIN_SITES]
    # vectors: the exported file where it has the name, the model for the rest, nothing when neither exists
    have = read_vecs(vecs) if vecs else {}
    vec, embedded, missing = {}, 0, []
    for k in liftable:
        if k in have:
            vec[k] = have[k]
        else:
            missing.append(k)
    if missing and embed_dir:
        emb = Embedder(embed_dir)
        for i in range(0, len(missing), 256):
            chunk = missing[i:i + 256]
            for k, v in zip(chunk, emb.batch(chunk)):
                vec[k] = tuple(float(x) for x in v)
        embedded, missing = len(vec) - sum(1 for k in liftable if k in have), []
    for k, v in vec.items():
        if len(v) != VEC_DIM:
            sys.exit(f"kinds: vector of {k!r} has {len(v)} dimensions, want {VEC_DIM}")

    db = new_db(out)
    db.execute("CREATE TABLE kind_keys (id INTEGER PRIMARY KEY, key TEXT NOT NULL, nwords INTEGER NOT NULL, n INTEGER NOT NULL)")
    db.executemany("INSERT INTO kind_keys VALUES (?,?,?,?)", ((kid[k], k, len(k.split()), n_of[k]) for k in keys))
    db.execute("CREATE UNIQUE INDEX kind_keys_key ON kind_keys (key)")
    db.execute("CREATE TABLE kind_key_words (word TEXT NOT NULL, kid INTEGER NOT NULL, PRIMARY KEY (word, kid)) WITHOUT ROWID")
    db.executemany("INSERT INTO kind_key_words VALUES (?,?)",
                   sorted({(w, kid[k]) for k in keys for w in k.split(" ")}))
    db.execute("CREATE TABLE kind_members (kid INTEGER NOT NULL, doc_id INTEGER NOT NULL, votes REAL, conf REAL, lead REAL, "
               "PRIMARY KEY (kid, doc_id)) WITHOUT ROWID")
    db.executemany("INSERT INTO kind_members VALUES (?,?,?,?,?)",
                   ((kid[k], d, *v) for (k, d), v in sorted(best.items(), key=lambda x: (kid[x[0][0]], x[0][1]))))
    rows = sorted((doc_of[d], *docs[d]) for d in doc_of)
    db.execute("CREATE TABLE kind_docs (doc_id INTEGER PRIMARY KEY, kinds TEXT, sources INTEGER, sitelinks INTEGER, "
               "pageviews INTEGER, country TEXT)")
    db.executemany("INSERT INTO kind_docs VALUES (?,?,?,?,?,?)", rows)
    db.execute("CREATE VIRTUAL TABLE kind_docs_fts USING fts5(kinds, content='', "
               "tokenize='porter unicode61 remove_diacritics 2')")
    db.executemany("INSERT INTO kind_docs_fts (rowid, kinds) VALUES (?, ?)", ((r[0], r[1] or "") for r in rows))
    db.execute("CREATE TABLE kind_vecs (kid INTEGER PRIMARY KEY, v BLOB NOT NULL)")
    db.executemany("INSERT INTO kind_vecs VALUES (?,?)", ((kid[k], f16_blob(vec[k])) for k in liftable if k in vec))
    n_vec = db.execute("SELECT count(*) FROM kind_vecs").fetchone()[0]
    info = {"licence": LICENCE, "source": SOURCE,
            "key": "site registry doc_id (type 2)", "vec_model": VEC_MODEL, "vec_dim": VEC_DIM, "vec_dtype": "float16",
            "lift_conf": LIFT_CONF, "kind_keys": len(keys), "kind_members": len(best), "kind_docs": len(rows),
            "kind_vecs": n_vec}
    stamp(db, "kinds", release, info)
    db.execute("INSERT INTO kind_docs_fts (kind_docs_fts) VALUES ('optimize')")
    finish(db, out)
    rep = {"stage": "kinds", "seconds": round(time.time() - t0, 1), "path": out, "bytes": os.path.getsize(out),
           "dump_rows": counts, "docs_in_dump": len(docs), "docs_in_registry": len(rows),
           "dropped_docs_not_in_registry": len(docs) - len(rows), "members_in_dump": len(members),
           "members_shipped": len(best), "dropped_members_not_in_registry": len(members) - len(kept),
           "collapsed_duplicate_members": len(kept) - len(best), "kind_keys": len(keys),
           "kind_keys_dropped_empty": len({m[0] for m in members}) - len(keys),
           "members_conf_ge_lift": sum(1 for v in best.values() if v[1] is not None and v[1] >= LIFT_CONF),
           "liftable_keys": len(liftable), "vectors": n_vec, "vectors_from_file": sum(1 for k in liftable if k in have),
           "vectors_embedded": embedded, "liftable_keys_without_vector": len(missing)}
    print(json.dumps(rep), flush=True)
    return rep


# --------------------------------------------------------------------------- the guard (release_v1.sqlguard_file calls it)

KINDS_TABLES = frozenset({"meta", "kind_keys", "kind_key_words", "kind_members", "kind_docs", "kind_vecs", "kind_docs_fts"})
KINDS_COLUMNS = {
    "meta": {"key", "value"},
    "kind_keys": {"id", "key", "nwords", "n"},
    "kind_key_words": {"word", "kid"},
    "kind_members": {"kid", "doc_id", "votes", "conf", "lead"},
    "kind_docs": {"doc_id", "kinds", "sources", "sitelinks", "pageviews", "country"},
    "kind_vecs": {"kid", "v"},
}
_FTS_SHADOW = re.compile(r"^kind_docs_fts_(data|idx|docsize|config|content)$")


def kinds_guard(db) -> list:
    """Problems of a file whose meta.role is `kinds`: only the named tables and columns of the open tier. Any
    other table (kind_site_vec, kinds_member_judged, kinds_judge, kinds_open_rows, ...) or column (jev, a legacy `lead` on a
    kind_members without `conf`, tranco_rank, url/title/descr/search_text) is refused by name; an unknown one is refused
    because it is unknown. The open `lead` is told from the legacy one by the sibling `conf` column (same table)."""
    problems = []
    tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type IN ('table', 'view')")]
    for t in tables:
        if t in KINDS_TABLES or _FTS_SHADOW.match(t):
            continue
        problems.append(f"table {t} (not in the open kinds tier)")
    for t in tables:
        cols = {r[1] for r in db.execute(f'PRAGMA table_info("{t}")')}
        for c in cols:
            if "jev" in c.lower() or "jev" in t.lower():
                problems.append(f"column {t}.{c} (legacy column, ADR-064/065)")
        want = KINDS_COLUMNS.get(t)
        if want is not None and cols != want:
            problems.append(f"{t} columns {sorted(cols ^ want)} differ from the open tier's")
        if t == "kind_members" and "conf" not in cols and "lead" in cols:
            problems.append("kind_members.lead without conf (the legacy layout)")
    return problems


if __name__ == "__main__":
    a = sys.argv[1:]
    opts = {}
    for flag in ("--vecs", "--embed"):
        if flag in a:
            i = a.index(flag)
            opts[flag[2:]] = a[i + 1]
            del a[i:i + 2]
    build(a[3], a[0], a[1], a[2], opts.get("vecs"), opts.get("embed"))
