"""Shared by update_pkg.py (builds a monthly update package) and update_apply.py (applies it).

Spec: docs/internal/SEARCH-UPDATE-PACKAGE.md. Everything here is deterministic: the same inputs give
the same bytes (no timestamps, sorted keys, one zstd thread).

  sha256_file, zst_compress/zst_decompress      the file hash and the zstd command (the repo already needs it)
  sqlite_content_sha256(path)                   the LOGICAL hash of a SQLite file: schema + every row, FTS5 and
                                                R*Tree included, ignoring page layout and shadow tables
  sqlite_diff / sqlite_patch                    the row diff of two SQLite files, and applying it IN PLACE to a copy
  ivf_write_fresh / ivf_merge                   the sem_ivf.py folder: a fresh build, and last month's folder + a delta
"""
import base64
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time

import numpy as np

IVF_FORMAT = "logout-sem-ivf/1"          # apps/search/serve/api/sem_ivf.py FORMAT
DIM_BYTES = 96
UPDATE_FORMAT = "logout-update/1"


class UpdateError(Exception):
    """A refusal: the message says what was wrong and what was left untouched."""


# ---- progress: a long apply is never silent. One line to stderr per (file, phase) at most every PROGRESS_EVERY seconds,
# plus one when a phase ends: "update_apply: <file> <phase> <done>/<total> rows (<rate> rows/s)".
PROGRESS_EVERY = float(os.environ.get("LOGOUT_UPDATE_PROGRESS_S", "10"))
_PROGRESS = {}


def progress(label: str, phase: str, done: int, total: int = 0, final: bool = False) -> None:
    now = time.time()
    st = _PROGRESS.setdefault((label, phase), {"t0": now, "last": now})
    if not final and now - st["last"] < PROGRESS_EVERY:
        return
    st["last"] = now
    el = now - st["t0"]
    of = f"/{total}" if total else ""
    rate = f"{done / el:,.0f} rows/s, " if el >= 0.5 else ""
    print(f"update_apply: {label} {phase} {done}{of} rows ({rate}{el:.0f}s){' done' if final else ''}", file=sys.stderr, flush=True)
    if final:
        _PROGRESS.pop((label, phase), None)


def sha256_file(p: str) -> str:
    """sha256 of a file. 8 MiB reads into one reused buffer (hashlib drops the GIL for buffers over 2 KiB, so several of these run
    in parallel threads: sha256_files)."""
    h = hashlib.sha256()
    buf = bytearray(1 << 23)
    mv = memoryview(buf)
    with open(p, "rb", buffering=0) as f:
        while True:
            n = f.readinto(buf)
            if not n:
                break
            h.update(mv[:n])
    return h.hexdigest()


def sha256_files(paths, jobs: int = 4) -> list:
    """sha256 of each path, `jobs` at a time in threads (the hash and the read both release the GIL)."""
    paths = list(paths)
    if jobs <= 1 or len(paths) <= 1:
        return [sha256_file(p) for p in paths]
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=min(jobs, len(paths))) as ex:
        return list(ex.map(sha256_file, paths))


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def zst_compress(src: str, dst: str, level: int = 19) -> int:
    """src -> dst.zst with the zstd command, one thread (deterministic). Returns the compressed size."""
    r = subprocess.run(["zstd", "-q", f"-{level}", "-T1", "-f", "-o", dst, src], capture_output=True)
    if r.returncode:
        raise UpdateError(f"zstd failed on {src}: {r.stderr.decode()[:200]}")
    return os.path.getsize(dst)


def zst_decompress(src: str, dst: str) -> None:
    r = subprocess.run(["zstd", "-dq", "-f", "-o", dst, src], capture_output=True)
    if r.returncode:
        raise UpdateError(f"zstd -d failed on {src}: {r.stderr.decode()[:200]}")


def copy_file(src: str, dst: str) -> None:
    """A real copy (reflink when the filesystem has it): the destination will be written to."""
    r = subprocess.run(["cp", "--reflink=auto", "--", src, dst], capture_output=True)
    if r.returncode:
        shutil.copyfile(src, dst)
    os.chmod(dst, 0o644)


# =============================================================================== SQLite

def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def sqlite_objects(db: sqlite3.Connection):
    """(real tables, rtree tables, fts5 tables, schema SQL list without shadow tables/indexes of them).
    Shadow tables (rtree_node, fts5_data, ...) are SQLite's own storage of a virtual table: never diffed."""
    rows = db.execute("SELECT type, name, tbl_name, sql FROM sqlite_schema WHERE sql IS NOT NULL ORDER BY rowid").fetchall()
    virtual = {n: s for t, n, _tn, s in rows if t == "table" and s.upper().startswith("CREATE VIRTUAL TABLE")}
    shadow = lambda n: any(n.startswith(v + "_") for v in virtual)  # noqa: E731
    plain, rtree, fts = [], [], []
    schema = []
    for t, n, tn, s in rows:
        if t == "table" and n in virtual:
            (rtree if "USING RTREE" in s.upper() else fts if "USING FTS5" in s.upper() else plain).append(n)
            if n not in rtree and n not in fts:
                raise UpdateError(f"virtual table {n}: only rtree and fts5 are supported")
            schema.append(s)
        elif shadow(n) or shadow(tn):
            continue
        elif t == "table":
            if n != "sqlite_sequence":
                plain.append(n)
            schema.append(s)
        else:
            schema.append(s)
    return plain, rtree, fts, schema


def _cols(db, t):
    info = db.execute(f"PRAGMA table_info({_q(t)})").fetchall()
    return [r[1] for r in info], [r[1] for r in sorted((r for r in info if r[5]), key=lambda r: r[5])]


def _enc(v):
    if isinstance(v, bytes):
        return {"b64": base64.b64encode(v).decode()}
    return v


def _dec(v):
    if isinstance(v, dict):
        return base64.b64decode(v["b64"])
    return v


def _fts_terms(db, t):
    db.execute("DROP TABLE IF EXISTS temp._v")
    db.execute(f"CREATE VIRTUAL TABLE temp._v USING fts5vocab('main', {_q(t)}, 'row')")
    terms = [r[0] for r in db.execute("SELECT term FROM temp._v ORDER BY term")]
    db.execute("DROP TABLE temp._v")
    return terms


def _bytes_default(o):
    if isinstance(o, (bytes, bytearray, memoryview)):
        return {"b64": base64.b64encode(bytes(o)).decode()}
    raise TypeError(f"not JSON serialisable: {type(o).__name__}")


# ONE encoder, reused (json.dumps with non-default separators builds a new JSONEncoder per call: 1.2 M calls per 600 k-row file).
# The bytes it writes are exactly json.dumps(list, separators=(",", ":"), sort_keys=True) with a BLOB as {"b64": ...}: the hash is unchanged.
_ENC = json.JSONEncoder(separators=(",", ":"), sort_keys=True, default=_bytes_default).encode


def sqlite_content_sha256(path: str, label: str = None) -> str:
    """The logical content of a SQLite file as one sha256: application_id, user_version, the schema SQL, every row of
    every table in key order (REAL as repr, BLOB as hex), the rows of R*Tree tables, and for an FTS5 table every
    term's posting list. The same data in a differently laid-out file (after DELETE/INSERT, before VACUUM) hashes the same.
    `label` (a file name) turns on progress lines on stderr."""
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    h = hashlib.sha256()
    upd, enc = h.update, _ENC

    def put(*a):
        upd(enc(a).encode() + b"\n")
    put("hdr", db.execute("PRAGMA application_id").fetchone()[0], db.execute("PRAGMA user_version").fetchone()[0])
    plain, rtree, fts, schema = sqlite_objects(db)
    for s in sorted(schema):
        put("sql", s)
    total = sum(db.execute(f"SELECT count(*) FROM {_q(t)}").fetchone()[0] for t in plain + rtree) if label else 0
    done = 0
    for t in sorted(plain + rtree):
        cols, pk = _cols(db, t)
        order = ", ".join(_q(c) for c in (pk or cols))
        put("table", t, cols)
        for r in db.execute(f"SELECT * FROM {_q(t)} ORDER BY {order}"):
            upd(enc(r).encode() + b"\n")
            if label:
                done += 1
                if not done & 0xFFFF:
                    progress(label, "content-hash", done, total)
    if label:
        progress(label, "content-hash", done, total, final=True)
    for t in sorted(fts):
        put("fts", t)
        put("rowids", [r[0] for r in db.execute(f"SELECT rowid FROM {_q(t)} ORDER BY rowid")])
        for term in _fts_terms(db, t):
            put(term, [r[0] for r in db.execute(f"SELECT rowid FROM {_q(t)} WHERE {_q(t)} MATCH ? ORDER BY rowid",
                                                ('"' + term.replace('"', '""') + '"',))])
    db.close()
    return h.hexdigest()


def sqlite_header(path: str) -> dict:
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    r = {"application_id": db.execute("PRAGMA application_id").fetchone()[0], "user_version": db.execute("PRAGMA user_version").fetchone()[0],
         "page_size": db.execute("PRAGMA page_size").fetchone()[0]}
    db.close()
    return r


# ------------------------------------------------------------------ derived FTS5 tables (re-derived on the machine)
# A contentless FTS5 table cannot be read back, so it is not diffed: the rows whose SOURCE changed are deleted and
# re-inserted from the base rows (the same rule the builders use; apps/search/tools/sqlite_lookups/build.py).
# (file role, fts table) -> (source of the touched ids, text of one id)

_TRGM = None


def _padded(s):
    """trgm.padded(s). trgm is imported ONCE: this used to sys.path.insert on every call, 550,000 calls grew sys.path by
    550,000 entries and cost 116 us per name instead of 2.3 (measured, SEARCH-UPDATE-PACKAGE.md section 10)."""
    global _TRGM
    if _TRGM is None:
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tools", "sqlite_lookups"))
        import trgm
        _TRGM = trgm
    return _TRGM.padded(s)


def fts_touched(role: str, fts: str, diffs: dict, reg_diff: dict):
    """The rowids whose text may have changed, from the base-table diffs."""
    def ids(d, tbl, col=0):
        v = d.get(tbl)
        return {(r[col] if not isinstance(r[col], list) else r[col][0]) for r in v["removed"] + v["upsert"]} if v else set()
    if role == "places-osm" or role == "places-overture":
        return ids(diffs, "places", 0)
    if role == "lookup" and fts in ("wiki_trgm", "label_trgm"):
        want = 1 if fts == "wiki_trgm" else 2
        return {i for i in ids(reg_diff, "registry", 0) if i >> 28 == want}
    return None


def fts_text(role: str, fts: str, rowid: int, db: sqlite3.Connection, registry: sqlite3.Connection):
    """The text the builder indexes for rowid, or None when the row has none."""
    if role in ("places-osm", "places-overture"):
        r = db.execute("SELECT name FROM places WHERE pid = ?", (rowid,)).fetchone()
        return _padded(r[0]) if r and r[0] else None
    r = registry.execute("SELECT path, title FROM registry WHERE doc_id = ?", (rowid,)).fetchone()
    if not r:
        return None
    if fts == "wiki_trgm":
        return _padded(r[1]) if r[1] else None
    host = r[0]
    return _padded(host[:(host + ".").index(".")]) if host else None


# ------------------------------------------------------------------ which tables a same-layout rewrite really touched
PAGE = 4096


def page_diff(a: str, b: str, cap: int = 5_000_000) -> list:
    """Page numbers (1-based) whose bytes differ between two SQLite files of the same size, or [-1] when they cannot be
    compared page by page (another size) or too many differ."""
    if os.path.getsize(a) != os.path.getsize(b):
        return [-1]
    out, pg = [], 0
    with open(a, "rb") as fa, open(b, "rb") as fb:
        while True:
            x, y = fa.read(PAGE * 1024), fb.read(PAGE * 1024)
            if not x and not y:
                break
            if x != y:
                for i in range(0, len(x), PAGE):
                    if x[i:i + PAGE] != y[i:i + PAGE]:
                        out.append(pg + i // PAGE + 1)
                        if len(out) > cap:
                            return [-1]
            pg += 1024
    return out


def owner_tables(path: str, pages: list) -> set:
    """The tables (an index counts as its table; an FTS5 shadow table as itself) that own these pages, from dbstat. Pages no
    b-tree owns (the freelist) belong to none."""
    want = set(pages)
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        tbl = {n: t for n, t in db.execute("SELECT name, tbl_name FROM sqlite_schema")}
        if len(want) <= 500:
            ph = ",".join("?" * len(want))
            names = {r[0] for r in db.execute(f"SELECT name FROM dbstat WHERE pageno IN ({ph})", sorted(want))}
        else:
            names = {n for pg, n in db.execute("SELECT pageno, name FROM dbstat") if pg in want}
        return {tbl.get(n, n) for n in names}
    finally:
        db.close()


# ------------------------------------------------------------------ the row diff

def sqlite_diff(old: str, new: str, role: str, registry_new: str = None, only=None, narrow: bool = False) -> dict:
    """The row diff old -> new, or raises UpdateError('whole: why') when only a whole-file ship is safe.
    Per plain/rtree table: removed keys, upserted rows. Per FTS5 table: rowids to re-derive and whether each
    exists in `new`. The R*Tree and FTS5 are NOT shipped as pages.
    `only` (a set of table names; twin/release_delta.py, which wants the changed rows and nothing else): diff just those tables
    and skip the FTS5 part (its rowids are derived, no answer holds them).
    `narrow` (the update package): the two files have the same size, so the pages that differ say which tables can have changed
    rows; the others are not scanned (a rewrite with identical rows, or one table touched in a 7 GB file, costs a page compare
    and one table, not a scan of everything). An FTS5 table none of whose pages changed and none of whose base rows changed is
    not scanned either. Any doubt (another size, too many pages, a schema difference) scans everything, as before."""
    db = sqlite3.connect(f"file:{new}?mode=ro", uri=True)
    db.execute("ATTACH ? AS o", (f"file:{old}?mode=ro",))
    po, ro, fo, so = sqlite_objects(sqlite3.connect(f"file:{old}?mode=ro", uri=True))
    pn, rn, fn, sn = sqlite_objects(db)
    if sorted(so) != sorted(sn):
        raise UpdateError("whole: the schema differs")
    out = {"tables": {}, "fts": {}}
    fts_pages = None                                   # tables with a changed page (narrow mode), shadow tables included
    if narrow and only is None and sorted(so) == sorted(sn):
        pages = page_diff(old, new)
        if pages != [-1]:
            fts_pages = owner_tables(new, pages) if pages else set()
            # an R*Tree keeps its rows in <t>_node/_rowid/_parent, which dbstat reports under those names: the table owns them (audit DB-02)
            only = {t for t in pn + rn if t in fts_pages or (t in rn and any(x.startswith(t + "_") for x in fts_pages))}
    for t in sorted(pn + rn):
        if only is not None and t not in only:
            continue
        cols, pk = _cols(db, t)
        key = pk or cols
        up = db.execute(f"SELECT * FROM main.{_q(t)} EXCEPT SELECT * FROM o.{_q(t)} ORDER BY {', '.join(_q(c) for c in key)}").fetchall()
        gone = db.execute(f"SELECT * FROM o.{_q(t)} EXCEPT SELECT * FROM main.{_q(t)}").fetchall()
        kidx = [cols.index(c) for c in key]
        upkeys = {tuple(r[i] for i in kidx) for r in up}
        removed = sorted(({tuple(r[i] for i in kidx) for r in gone} - upkeys), key=lambda k: [(x is not None, x) for x in k])
        if not pk:                                     # no declared key: the key is the whole row, so a change = remove + add
            removed = sorted((tuple(r) for r in gone), key=lambda k: [(x is not None, x) for x in k])
            if db.execute(f"SELECT count(*) - (SELECT count(*) FROM (SELECT DISTINCT * FROM main.{_q(t)})) FROM main.{_q(t)}").fetchone()[0] \
               or db.execute(f"SELECT count(*) - (SELECT count(*) FROM (SELECT DISTINCT * FROM o.{_q(t)})) FROM o.{_q(t)}").fetchone()[0]:
                raise UpdateError(f"whole: table {t} has no primary key and duplicate rows")
        if up or removed:
            out["tables"][t] = {"cols": cols, "key": key,
                                "removed": [[_enc(x) for x in k] for k in removed],
                                "upsert": [[_enc(x) for x in r] for r in up]}
    old_db = sqlite3.connect(f"file:{old}?mode=ro", uri=True)
    reg = None
    for t in sorted(fn if (only is None or fts_pages is not None) else ()):
        if fts_pages is not None and role != "lookup" and not any(x == t or x.startswith(t + "_") for x in fts_pages):
            base = fts_touched(role, t, out["tables"], {})
            if base is not None and not base:
                out["fts"][t] = {"touched": [], "present": []}        # no page of it changed and no row it is derived from did
                continue
        have_old = {r[0] for r in old_db.execute(f"SELECT rowid FROM {_q(t)}")}
        have_new = {r[0] for r in db.execute(f"SELECT rowid FROM main.{_q(t)}")}
        if role == "lookup" and reg is None:
            if not registry_new:
                raise UpdateError("whole: the lookup FTS5 tables are derived from the registry, which was not given")
            reg = registry_diff(old_registry_of(old), registry_new)
        touched = fts_touched(role, t, out["tables"], reg or {})
        if touched is None:
            raise UpdateError(f"whole: no re-derive rule for FTS5 table {role}.{t}")
        touched = set(touched) | (have_old ^ have_new)
        out["fts"][t] = {"touched": sorted(touched), "present": sorted(touched & have_new)}
    old_db.close()
    db.close()
    out["header"] = sqlite_header(new)
    return out


# ------------------------------------------------------------------ what a row diff costs to apply (the whole-vs-diff rule, update_pkg.py)
# Measured on the dev workstation (16 cores, files in the page cache, one process, tests/unit/lib/bench_places_apply.py on a places-osm-like file
# of 597 k places, SEARCH-UPDATE-PACKAGE.md section 10): patching ran at ~137 k changed rows/s (places + R*Tree + text-index rows) and the
# content hash at ~124 k rows/s (table + R*Tree rows; the text index adds a little). A home box (4 cores, 4 GB, maybe a spinning disk) is ASSUMED
# 3x slower per core, which is the factor below; it is an assumption, not a measurement of any such box.
APPLY_PATCH_ROWS_PER_S = 137_000 / 3
APPLY_HASH_ROWS_PER_S = 124_000 / 3
APPLY_HASH_PASSES = 2                    # the installed file (base_content_sha256) and the patched file (content_sha256)
APPLY_LIMIT_S = 600.0                    # a diff that takes longer than 10 minutes of one core to apply is not a "monthly update"
WHOLE_MAX_FACTOR = 3.0                   # ... is replaced by the whole file when that is at most 3x the diff's download (zstd bytes):
                                         # 3x of a ~150 MB diff is 450 MB, about 1 minute at 10 MB/s, far less than the CPU time saved


def count_rows(path: str) -> int:
    """Rows the content hash visits in a SQLite file: every table and R*Tree row, plus the text index's documents."""
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    plain, rtree, fts, _ = sqlite_objects(db)
    n = sum(db.execute(f"SELECT count(*) FROM {_q(t)}").fetchone()[0] for t in plain + rtree + fts)
    db.close()
    return n


def estimate_apply_seconds(changed_rows: int, file_rows: int) -> float:
    """Seconds of one core to apply a row diff of `changed_rows` rows to a file of `file_rows` rows on the assumed home box: the patch
    plus the two content-hash passes (the hash is the fixed cost, which is why a small diff of a big file is not free)."""
    return changed_rows / APPLY_PATCH_ROWS_PER_S + APPLY_HASH_PASSES * file_rows / APPLY_HASH_ROWS_PER_S


def ship_whole_for_cost(est_s: float, whole_z: int, diff_z: int, limit_s: float = None, factor: float = None) -> bool:
    """The cost rule: ship the whole file instead of the row diff when applying the diff is estimated above `limit_s` seconds (strictly) AND the
    whole file's zstd bytes are at most `factor` x the diff's (so the download stays in the same league). Both are needed: a slow diff that
    is 10x smaller than the file stays a diff (the download is what the user pays for), and a cheap diff stays a diff however small the file."""
    limit_s = APPLY_LIMIT_S if limit_s is None else limit_s
    factor = WHOLE_MAX_FACTOR if factor is None else factor
    return est_s > limit_s and whole_z <= factor * diff_z


def old_registry_of(old_file: str) -> str:
    return os.path.join(os.path.dirname(old_file), "registry.sqlite")


def registry_diff(old_reg: str, new_reg: str) -> dict:
    db = sqlite3.connect(f"file:{new_reg}?mode=ro", uri=True)
    db.execute("ATTACH ? AS o", (f"file:{old_reg}?mode=ro",))
    up = db.execute("SELECT * FROM main.registry EXCEPT SELECT * FROM o.registry").fetchall()
    gone = db.execute("SELECT doc_id FROM o.registry WHERE doc_id NOT IN (SELECT doc_id FROM main.registry)").fetchall()
    db.close()
    return {"registry": {"removed": [list(r) for r in gone], "upsert": [list(r) for r in up]}}


# ------------------------------------------------------------------ the rows file (logout-rows/2: JSON lines, applied as a stream)
# One JSON document of a 2.4 M-row diff is 350 MB of text per 1.2 M rows and json.load held 1.75 GB of Python objects for it (measured);
# a 4 GB machine cannot apply the real nationwide places month that way. logout-rows/2 is the same diff as JSON LINES, a few thousand
# rows per line, read one line at a time. A file written by the first form (one JSON document with "tables") is still read.
ROWS_FORMAT = "logout-rows/2"
ROWS_CHUNK = 20000


def write_rows(diff: dict, path: str) -> int:
    """The diff (as sqlite_diff returns it) as a deterministic logout-rows/2 file. Returns the number of bytes written."""
    def line(f, o):
        f.write(json.dumps(o, separators=(",", ":"), sort_keys=True) + "\n")
    with open(path, "w") as f:
        line(f, {"format": ROWS_FORMAT, "header": diff["header"],
                 "counts": {"tables": {t: [len(d["removed"]), len(d["upsert"])] for t, d in diff["tables"].items()},
                            "fts": {t: len(d["touched"]) for t, d in diff["fts"].items()}}})
        for t, d in diff["tables"].items():
            line(f, {"table": t, "cols": d["cols"], "key": d["key"]})
            for i in range(0, max(len(d["removed"]), len(d["upsert"])), ROWS_CHUNK):
                line(f, {"t": t, "removed": d["removed"][i:i + ROWS_CHUNK], "upsert": d["upsert"][i:i + ROWS_CHUNK]})
        for t, d in diff["fts"].items():
            have = set(d["present"])
            for i in range(0, len(d["touched"]), ROWS_CHUNK):
                part = d["touched"][i:i + ROWS_CHUNK]
                line(f, {"fts": t, "touched": part, "present": [r for r in part if r in have]})
    return os.path.getsize(path)


class _open_rows:
    """Open a rows file for reading lines. A `.zst` path is streamed through `zstd -dc` (nothing is decompressed to disk), and a
    non-zero exit from zstd, even after the last line, raises UpdateError."""
    def __init__(self, src):
        self.src, self.proc = src, None

    def __enter__(self):
        if self.src.endswith(".zst"):
            self.proc = subprocess.Popen(["zstd", "-dc", self.src], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1 << 20)
            return self.proc.stdout
        self.f = open(self.src)
        return self.f

    def __exit__(self, et, ev, tb):
        if self.proc is None:
            self.f.close()
            return False
        if et is not None and not issubclass(et, ValueError):
            self.proc.kill()                       # the caller gave up (or an error that is not about the text): stop zstd
        else:
            self.proc.stdout.read()                # a bad line may be zstd's truncated output: drain, then ask zstd
        self.proc.stdout.close()
        err = self.proc.stderr.read()
        rc = self.proc.wait()
        self.proc.stderr.close()
        if rc and (et is None or issubclass(et, ValueError)):
            raise UpdateError(f"zstd -d failed on {self.src}: {err[:200]}")
        return False


def iter_rows(src):
    """Events of a rows file (a path) or of a diff dict: ("header", header, counts), ("table", t, cols, key), ("rows", t, removed, upsert),
    ("fts", t, touched, present). Tables before FTS5 tables (the FTS5 text is derived from the patched base rows)."""
    if isinstance(src, dict):
        diff = src
    else:
        with _open_rows(src) as f:
            first = json.loads(f.readline())
            if first.get("format") == ROWS_FORMAT:
                yield ("header", first["header"], first["counts"])
                for ln in f:
                    o = json.loads(ln)
                    if "table" in o:
                        yield ("table", o["table"], o["cols"], o["key"])
                    elif "fts" in o:
                        yield ("fts", o["fts"], o["touched"], o["present"])
                    else:
                        yield ("rows", o["t"], o["removed"], o["upsert"])
                return
        if "tables" not in first:
            raise UpdateError(f"{src}: not a row diff this code reads (neither {ROWS_FORMAT} nor the first form)")
        diff = first
    yield ("header", diff["header"], {"tables": {t: [len(d["removed"]), len(d["upsert"])] for t, d in diff["tables"].items()},
                                      "fts": {t: len(d["touched"]) for t, d in diff["fts"].items()}})
    for t, d in diff["tables"].items():
        yield ("table", t, d["cols"], d["key"])
        for i in range(0, max(len(d["removed"]), len(d["upsert"])), ROWS_CHUNK):
            yield ("rows", t, d["removed"][i:i + ROWS_CHUNK], d["upsert"][i:i + ROWS_CHUNK])
    for t, d in diff["fts"].items():
        have = set(d["present"])
        for i in range(0, len(d["touched"]), ROWS_CHUNK):
            part = d["touched"][i:i + ROWS_CHUNK]
            yield ("fts", t, part, [r for r in part if r in have])


def sqlite_patch(path: str, rows, role: str, registry_path: str = None, label: str = None, cache_kib: int = 200000) -> dict:
    """Apply the row diff IN PLACE to `path` (a copy of last month's file), in one transaction, with secure_delete ON
    (removed rows leave no bytes behind) and NO VACUUM. Then re-derive the FTS5 rows. Returns counts.
    `rows` is a rows file's path or a diff dict. The staged copy is the only file written with journal_mode OFF / synchronous OFF.

    A DELETE on an R*Tree must be `WHERE id = ?`: the virtual table's index only understands `=`, and `id IS ?` makes every delete a
    full scan of the R*Tree (measured: 41 k deletes in a 120 k-row file took 260 s, 4.5x for each doubling; at the real 2.4 M upserts
    that is days). Every other table keeps `IS ?` (a NULL key part must match; a b-tree lookup handles IS)."""
    db = sqlite3.connect(path, isolation_level=None)
    db.execute("PRAGMA secure_delete = ON")
    db.execute("PRAGMA journal_mode = OFF")
    db.execute("PRAGMA synchronous = OFF")
    db.execute(f"PRAGMA cache_size = -{int(cache_kib)}")
    rtree = set(sqlite_objects(db)[1])
    label = label or ""
    n = {"removed": 0, "upserted": 0, "fts_rederived": 0}
    counts = {}
    reg = sqlite3.connect(f"file:{registry_path}?mode=ro", uri=True) if registry_path else None
    cur = None
    hdr = None
    done = total = 0
    db.execute("BEGIN")
    try:
        for ev in iter_rows(rows):
            if ev[0] == "header":
                hdr, counts = ev[1], ev[2]
                total = sum(a + b for a, b in counts["tables"].values()) + sum(counts["fts"].values())
            elif ev[0] == "table":
                _, t, cols, key = ev
                if t in rtree:                      # delete by the id column alone, with `=`
                    if cols[0] not in key:
                        raise UpdateError(f"{t}: the row diff's key {key} does not contain the R*Tree id {cols[0]!r}")
                    kcols, kpos = [cols[0]], [key.index(cols[0])]
                    where = f"{_q(cols[0])} = ?"
                else:
                    kcols, kpos = key, list(range(len(key)))
                    where = " AND ".join(f"{_q(c)} IS ?" for c in key)
                ipos = [cols.index(c) for c in kcols]
                cur = (t, cols, where, kpos, ipos)
            elif ev[0] == "rows":
                _, t, removed, upsert = ev
                t_, cols, where, kpos, ipos = cur
                assert t_ == t
                db.executemany(f"DELETE FROM {_q(t)} WHERE {where}", [[_dec(k[i]) for i in kpos] for k in removed])
                db.executemany(f"DELETE FROM {_q(t)} WHERE {where}", [[_dec(r[i]) for i in ipos] for r in upsert])
                db.executemany(f"INSERT INTO {_q(t)} ({', '.join(_q(c) for c in cols)}) VALUES ({','.join('?' * len(cols))})",
                               [[_dec(x) for x in r] for r in upsert])
                n["removed"] += len(removed)
                n["upserted"] += len(upsert)
                done += len(removed) + len(upsert)
                progress(label, f"patch {t}", done, total) if label else None
            else:
                _, t, touched, present = ev
                present = set(present)
                for rid in touched:
                    db.execute(f"DELETE FROM {_q(t)} WHERE rowid = ?", (rid,))
                    if rid in present:
                        txt = fts_text(role, t, rid, db, reg)
                        if txt is None:
                            raise UpdateError(f"{role}.{t}: rowid {rid} should be indexed but has no text")
                        db.execute(f"INSERT INTO {_q(t)} (rowid, t) VALUES (?, ?)", (rid, txt))
                    n["fts_rederived"] += 1
                done += len(touched)
                progress(label, f"patch {t} (text index)", done, total) if label else None
        if hdr is None:
            raise UpdateError("the row diff has no header")
        db.execute(f"PRAGMA application_id = {int(hdr['application_id'])}")
        db.execute(f"PRAGMA user_version = {int(hdr['user_version'])}")
        db.execute("COMMIT")
        if label:
            progress(label, "patch", done, total, final=True)
    except BaseException:
        db.execute("ROLLBACK")
        raise
    finally:
        db.close()
    return n


# =============================================================================== Tantivy

def tv_delete_doc_ids(writer, schema, doc_ids) -> int:
    """Delete documents by `doc_id` the way that works on the release's index. The real wiki index has `doc_id` as u64 (the Rust
    builder); `writer.delete_documents_by_term("doc_id", n)` with a Python int builds an i64 term there and silently deletes NOTHING
    (measured: u64 index, the document still matches after commit). A term QUERY takes the field's own type."""
    import tantivy
    n = 0
    for d in doc_ids:
        writer.delete_documents_by_query(tantivy.Query.term_query(schema, "doc_id", int(d)))
        n += 1
    return n


def tv_sort_segments(path: str) -> bool:
    """Put meta.json's segment list in segment_id order, after release_v1.tv_normalize. Tantivy writes the list in an order that
    differs from run to run once an index has 3 or more segments (measured: two identical typo_month chains, every FILE
    byte-identical, only meta.json's segment order different), so without this a delta build is not reproducible. The order is
    not meaningful to Tantivy. Multi-segment indexes are always rewritten in compact JSON even if the random initial order
    already matched sorted order (otherwise one run keeps Tantivy's indented formatting while another gets compact JSON).
    Returns True when it rewrote the file (a one-segment index is left byte for byte as it was)."""
    p = os.path.join(path, "meta.json")
    raw = open(p, encoding="utf-8").read()
    meta = json.loads(raw)
    segs = meta.get("segments", [])
    if len(segs) <= 1:
        return False
    meta["segments"] = sorted(segs, key=lambda x: x["segment_id"])
    text = json.dumps(meta, separators=(",", ":"))
    if text == raw:
        return False
    with open(p + ".tmp", "w", encoding="utf-8") as f:      # write and replace: a copied release file may be read-only
        f.write(text)
    os.replace(p + ".tmp", p)
    return True


# =============================================================================== IVF folder (sem_ivf.py format)

def _assign(codes: np.ndarray, cent: np.ndarray) -> np.ndarray:
    """Nearest centre by Hamming distance, ties to the lowest list number."""
    c64 = np.ascontiguousarray(cent).view(np.uint64)
    out = np.empty(len(codes), np.int32)
    step = max(1, (1 << 24) // max(1, len(cent) * (DIM_BYTES // 8)))
    for s in range(0, len(codes), step):
        q = np.ascontiguousarray(codes[s:s + step]).view(np.uint64)
        d = np.bitwise_count(q[:, None, :] ^ c64[None, :, :]).sum(2, dtype=np.int32)
        out[s:s + step] = d.argmin(1)
    return out


def _ivf_write(out: str, cent, lists_assign, ids, codes_getter, rows: int, meta: dict) -> None:
    order = np.lexsort((ids, lists_assign))
    L = len(cent)
    offs = np.zeros(L + 1, np.int64)
    offs[1:] = np.cumsum(np.bincount(lists_assign, minlength=L))
    os.makedirs(out, exist_ok=True)
    np.ascontiguousarray(cent).tofile(os.path.join(out, "centroids.u8"))
    offs.tofile(os.path.join(out, "offsets.i64"))
    ids[order].astype(np.int32).tofile(os.path.join(out, "ids.i32"))
    with open(os.path.join(out, "codes.u8"), "wb") as f:
        step = 200_000
        for s in range(0, rows, step):
            f.write(np.ascontiguousarray(codes_getter(order[s:s + step])).tobytes())
    with open(os.path.join(out, "ivf.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=1, sort_keys=True)
        f.write("\n")


def ivf_write_fresh(out: str, cent: np.ndarray, ids: np.ndarray, codes: np.ndarray, release: str) -> None:
    """A fresh folder for fixture month 1 (the real build is ivf_build.py with faiss k-means); same layout, same row order:
    lists by number, page_id order inside a list."""
    ids = np.asarray(ids, np.int32)
    assign = _assign(codes, cent)
    meta = {"format": IVF_FORMAT, "release": release, "rows": int(len(ids)), "lists": int(len(cent)), "dim_bits": DIM_BYTES * 8,
            "encoder": "nomic-embed-text-v1.5, mean-pooled, unit length, bit = component >= 0, MSB first", "recipe": {"fixture": True}}
    _ivf_write(out, cent, assign, ids, lambda idx: codes[idx], len(ids), meta)


def ivf_merge(old_dir: str, out_dir: str, removed_ids: np.ndarray, new_ids: np.ndarray, new_codes: np.ndarray, release: str) -> dict:
    """Last month's folder minus `removed_ids` plus the new codes, in the engine's own layout. The centres do NOT move
    (a delta cannot change them; a re-cluster is a full release). A new code goes to its nearest centre; inside a list
    rows stay in page_id order (ivf_build.py's order), so the result is the folder a fresh build over the same lists
    would write. This is a local rewrite of codes.u8/ids.i32/offsets.i64, not a download."""
    meta = json.load(open(os.path.join(old_dir, "ivf.json"), encoding="utf-8"))
    if meta.get("format") != IVF_FORMAT:
        raise UpdateError(f"{old_dir}/ivf.json: format {meta.get('format')!r}, this code merges {IVF_FORMAT!r}")
    cent = np.fromfile(os.path.join(old_dir, "centroids.u8"), np.uint8).reshape(-1, DIM_BYTES)
    offs = np.fromfile(os.path.join(old_dir, "offsets.i64"), np.int64)
    n = int(offs[-1])
    ids_old = np.fromfile(os.path.join(old_dir, "ids.i32"), np.int32)
    if len(ids_old) != n:
        raise UpdateError(f"{old_dir}: {len(ids_old)} ids but offsets say {n}")
    codes_old = np.memmap(os.path.join(old_dir, "codes.u8"), np.uint8, "r", shape=(n, DIM_BYTES)) if n else np.zeros((0, DIM_BYTES), np.uint8)
    list_old = np.repeat(np.arange(len(cent), dtype=np.int32), np.diff(offs))
    keep = ~np.isin(ids_old, np.asarray(removed_ids, np.int32))
    kept = np.flatnonzero(keep)
    new_ids = np.asarray(new_ids, np.int32)
    assign_new = _assign(new_codes, cent) if len(new_ids) else np.zeros(0, np.int32)
    ids = np.concatenate((ids_old[kept], new_ids))
    lists = np.concatenate((list_old[kept], assign_new))
    if len(np.unique(ids)) != len(ids):
        raise UpdateError("ivf merge: a page_id would appear twice (a new id that was not removed first)")
    src = np.concatenate((kept, n + np.arange(len(new_ids))))

    def getter(idx):
        s = src[idx]
        out = np.empty((len(s), DIM_BYTES), np.uint8)
        old = s < n
        if old.any():
            out[old] = codes_old[s[old]]
        if (~old).any():
            out[~old] = new_codes[s[~old] - n]
        return out
    m2 = dict(meta)
    m2.update({"release": release, "rows": int(len(ids)), "merged_from": meta["release"], "merges": int(meta.get("merges", 0)) + 1})
    _ivf_write(out_dir, cent, lists, ids, getter, len(ids), m2)
    return {"rows": int(len(ids)), "removed": int((~keep).sum()), "added": int(len(new_ids))}


IVF_FILES = ("centroids.u8", "offsets.i64", "ids.i32", "codes.u8", "ivf.json")


# When a merged folder must be re-clustered instead (docs/internal/SEARCH-UPDATE-PACKAGE.md section 5; the numbers behind them are measured there).
IVF_LIMITS = {"dist_growth": 0.02, "list_p99_growth": 1.0, "rows_growth": 0.8, "heavy_share_delta": 0.10, "merges": 12}


def ivf_drift(cur: dict, base: dict, merges: int, limits: dict = None) -> dict:
    """cur/base are ivf_stats() of the folder now and of the folder as it was trained (ivf.json `trained`). `rebuild` is True when
    any metric is past its limit; `reasons` says which."""
    lim = IVF_LIMITS if limits is None else limits
    m = {"dist_growth": round(cur["mean_dist"] / base["mean_dist"] - 1, 5), "list_p99_growth": round(cur["list_p99"] / max(1, base["list_p99"]) - 1, 5),
         "rows_growth": round(cur["rows"] / max(1, base["rows"]) - 1, 5), "heavy_share_delta": round(cur["heavy_share"] - base["heavy_share"], 5),
         "merges": merges}
    reasons = [f"{k} {m[k]:g} > {lim[k]:g}" for k in lim if k in m and m[k] > lim[k]]
    return {"metrics": m, "limits": dict(lim), "rebuild": bool(reasons), "reasons": reasons}


def ivf_stats(d: str) -> dict:
    """How well the folder's (fixed) centres still fit its codes: list sizes, and the mean Hamming distance from a code to the
    centre of its list. Read-only; about 10 s on 6.8 M codes."""
    cent = np.fromfile(os.path.join(d, "centroids.u8"), np.uint8).reshape(-1, DIM_BYTES)
    offs = np.fromfile(os.path.join(d, "offsets.i64"), np.int64)
    n, L = int(offs[-1]), len(cent)
    counts = np.diff(offs)
    c64 = np.ascontiguousarray(cent).view(np.uint64)
    codes = np.memmap(os.path.join(d, "codes.u8"), np.uint8, "r", shape=(n, DIM_BYTES)) if n else np.zeros((0, DIM_BYTES), np.uint8)
    lst = np.repeat(np.arange(L, dtype=np.int32), counts)
    tot, step = 0, 1 << 19
    for s0 in range(0, n, step):
        x = np.ascontiguousarray(codes[s0:s0 + step]).view(np.uint64)
        tot += int(np.bitwise_count(x ^ c64[lst[s0:s0 + step]]).sum(dtype=np.int64))
    mean = n / max(1, L)
    return {"rows": n, "lists": L, "list_p50": int(np.median(counts)), "list_p99": int(np.percentile(counts, 99)), "list_max": int(counts.max()) if L else 0,
            "empty": int((counts == 0).sum()), "heavy_share": round(float(counts[counts > 2 * mean].sum() / max(1, n)), 6),
            "mean_dist": round(tot / max(1, n), 4)}


def ivf_delta(old_dir: str, new_dir: str):
    """(removed page_ids, new page_ids, new codes) that turn the old folder into the new one, or None when the centres moved
    (a re-cluster is not a delta). A page_id whose code changed is removed and added."""
    for d in (old_dir, new_dir):
        if json.load(open(os.path.join(d, "ivf.json")))["format"] != IVF_FORMAT:
            return None
    if sha256_file(os.path.join(old_dir, "centroids.u8")) != sha256_file(os.path.join(new_dir, "centroids.u8")):
        return None

    def load(d):
        offs = np.fromfile(os.path.join(d, "offsets.i64"), np.int64)
        n = int(offs[-1])
        return (np.fromfile(os.path.join(d, "ids.i32"), np.int32),
                np.memmap(os.path.join(d, "codes.u8"), np.uint8, "r", shape=(n, DIM_BYTES)) if n else np.zeros((0, DIM_BYTES), np.uint8))
    io_, co = load(old_dir)
    in_, cn = load(new_dir)
    return ivf_diff(io_, co, in_, cn)


def ivf_diff(old_ids: np.ndarray, old_codes, new_ids: np.ndarray, new_codes):
    """(removed ids, added ids, added codes) between two (ids, codes) sets, rows in any order. An id whose code changed is
    removed and added. Vectorised: it runs on 6.8 M rows."""
    old_ids, new_ids = np.asarray(old_ids, np.int64), np.asarray(new_ids, np.int64)
    so, sn = np.argsort(old_ids, kind="stable"), np.argsort(new_ids, kind="stable")
    io_s, in_s = old_ids[so], new_ids[sn]
    if len(np.unique(io_s)) != len(io_s) or len(np.unique(in_s)) != len(in_s):
        raise UpdateError("an IVF set holds a page_id twice")
    both, a, b = np.intersect1d(io_s, in_s, assume_unique=True, return_indices=True)
    changed = np.zeros(len(both), bool)
    step = 1 << 19
    for s0 in range(0, len(both), step):
        changed[s0:s0 + step] = (old_codes[so[a[s0:s0 + step]]] != new_codes[sn[b[s0:s0 + step]]]).any(1)
    chg = both[changed]
    removed = np.union1d(np.setdiff1d(io_s, in_s, assume_unique=True), chg).astype(np.int32)
    added = np.union1d(np.setdiff1d(in_s, io_s, assume_unique=True), chg).astype(np.int32)
    pos = sn[np.searchsorted(in_s, added)]
    codes = np.ascontiguousarray(new_codes[pos]) if len(added) else np.zeros((0, DIM_BYTES), np.uint8)
    return removed, added, codes
