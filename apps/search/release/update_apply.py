#!/usr/bin/env python3
"""Apply a monthly update package to the installed release, atomically (spec: docs/internal/SEARCH-UPDATE-PACKAGE.md).

    update_apply.py adopt    BASE RELEASE_DIR            put an existing release under BASE (hard links) and point `current` at it
    update_apply.py apply    BASE PKG_DIR [--pubkey PEM | --insecure-no-signature] [--fast] [--no-quick-check] [--jobs N] [--progress-every S]
    update_apply.py rollback BASE                        point `current` back at the previous release (its directory was kept)
    update_apply.py status   BASE

BASE/releases/<release id>/   one folder per release, read-only
BASE/current  -> releases/<id>  the one pointer the engine follows; swapped with ONE rename (atomic)
BASE/previous -> releases/<id>  what `rollback` goes back to

apply never touches the running release: it STAGES the new one in BASE/releases/.staging-*, verifies everything there, and only then
renames the folder into place and swaps `current`. Any failure, or a kill at any point, leaves `current` on the old release, whole.
Unchanged files are hard-linked from the old release (no copy, no extra disk); SQLite files that changed are patched on a COPY, in
place, with secure_delete ON. This script never runs VACUUM.
"""
import argparse
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def self_cmd() -> list:
    """The command that runs this program again (the SQLite workers). A frozen launcher (installer/logout_search.py) has no
    interpreter of its own and sets LOGOUT_UPDATE_SELF_CMD to a JSON list that reaches update_apply.py's main."""
    j = os.environ.get("LOGOUT_UPDATE_SELF_CMD", "")
    return json.loads(j) if j else [sys.executable, os.path.abspath(__file__)]
import numpy as np  # noqa: E402

import release_check as rc  # noqa: E402  (client-safe: no psycopg, no exporter)
from release_ptr import current_id, fsync_path, fsync_tree, set_pointer  # noqa: E402,F401  (stdlib-only; shared with installer/install.py)
import update_common as uc  # noqa: E402

FAIL_AFTER = os.environ.get("LOGOUT_UPDATE_FAIL_AFTER", "")      # test hook: kill -9 self at "staged" | "renamed"
FAIL_WORKER = os.environ.get("LOGOUT_UPDATE_FAIL_WORKER", "")    # test hook: a SQLite worker kill -9s itself after patching this file (a basename)


def _open_tantivy(tantivy, path: str):
    """tantivy.Index.open that works on a read-only folder (serve/api/tv_open.py writable overlay)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "tv_open", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "serve", "api", "tv_open.py"))
    tv_open = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tv_open)
    return tv_open.open_index(tantivy, path)


def adopt(base: str, release_dir: str) -> str:
    m = json.load(open(os.path.join(release_dir, "manifest.json")))
    rid = m["release"]
    os.makedirs(os.path.join(base, "releases"), exist_ok=True)
    dst = os.path.join(base, "releases", rid)
    if os.path.exists(dst):
        raise uc.UpdateError(f"{dst} already exists")
    r = subprocess.run(["cp", "-a", "--reflink=auto", "-l", "--", release_dir.rstrip("/"), dst], capture_output=True)
    if r.returncode:
        shutil.rmtree(dst, ignore_errors=True)         # `cp -l` may have made the destination (and some links) before it was refused
        shutil.copytree(release_dir, dst)
    set_pointer(base, "current", rid)
    return rid


def verify_signature(pubkey: str, data: bytes, sig_path: str, what: str) -> None:
    try:
        from cryptography.hazmat.primitives import serialization
    except ImportError:
        raise uc.UpdateError("a public key was given but the cryptography package is missing: cannot verify the signature")
    try:
        sig = open(sig_path, "rb").read()
        serialization.load_pem_public_key(open(pubkey, "rb").read()).verify(sig, data)
    except Exception as e:  # noqa: BLE001 - any failure is a refusal
        raise uc.UpdateError(f"{what}: signature invalid or missing ({type(e).__name__})")


def check_requires(req: dict) -> None:
    lo, hi = rc.ENGINE_FORMATS
    if not (isinstance(req.get("release_format"), int) and lo <= req["release_format"] <= hi):
        raise uc.UpdateError(f"package needs release format {req.get('release_format')}, this engine reads {lo}..{hi}: update the engine first")
    if req.get("tantivy") and not str(req["tantivy"]).startswith(rc.ENGINE_TANTIVY):
        raise uc.UpdateError(f"package indexes were written by Tantivy {req['tantivy']}, this engine reads {rc.ENGINE_TANTIVY}x")
    if req.get("registry_layout") and req["registry_layout"] != rc.ri.LAYOUT:
        raise uc.UpdateError(f"package registry layout {req['registry_layout']!r}, this engine reads {rc.ri.LAYOUT!r}")
    if req.get("ivf_format") != uc.IVF_FORMAT:
        raise uc.UpdateError(f"package meaning-search format {req.get('ivf_format')!r}, this engine reads {uc.IVF_FORMAT!r}")
    if req.get("signals_magic") != rc.SIGNALS_MAGIC.decode():
        raise uc.UpdateError(f"package signals magic {req.get('signals_magic')!r}, this engine reads {rc.SIGNALS_MAGIC!r}")


def verify_tree(rel: str, m: dict, check_hash: bool = True, jobs: int = 1) -> None:
    for f in m["files"]:
        p = os.path.join(rel, f["path"])
        if not os.path.exists(p):
            raise uc.UpdateError(f"{rel}: {f['path']} is missing")
        if os.path.getsize(p) != f["bytes"]:
            raise uc.UpdateError(f"{rel}: {f['path']} does not match its manifest")
    if check_hash:
        for f, got in zip(m["files"], uc.sha256_files([os.path.join(rel, f["path"]) for f in m["files"]], jobs)):
            if got != f["sha256"]:
                raise uc.UpdateError(f"{rel}: {f['path']} does not match its manifest")


def tombstoned_page_ids(db: sqlite3.Connection, tomb: list) -> list:
    """The page_ids (registry.ext) of the tombstoned article doc_ids, found through a TEMP table: a month can tombstone 1.7 M ids, and an
    `IN (?, ?, ...)` of that many variables is refused ('too many SQL variables', SQLITE_MAX_VARIABLE_NUMBER)."""
    db.execute("DROP TABLE IF EXISTS temp._tomb_ids")
    db.execute("CREATE TEMP TABLE _tomb_ids (id INTEGER PRIMARY KEY)")
    for i in range(0, len(tomb), 100_000):
        db.executemany("INSERT OR IGNORE INTO _tomb_ids VALUES (?)", [(x,) for x in tomb[i:i + 100_000]])
    out = [r[0] for r in db.execute("SELECT ext FROM registry WHERE doc_id IN (SELECT id FROM _tomb_ids) AND doc_id >> 28 = 1")]
    db.execute("DROP TABLE temp._tomb_ids")
    return out


def check_tombstones(stage: str, base_dir: str, tomb: list, notes: dict) -> None:
    """The one tombstone list, checked against every part of the new release."""
    if not tomb:
        return
    reg = sqlite3.connect(f"file:{stage}/registry.sqlite?mode=ro", uri=True)
    ids = set(tomb)
    live = [d for d in tomb if reg.execute("SELECT 1 FROM registry WHERE doc_id = ?", (d,)).fetchone()]
    if live:
        raise uc.UpdateError(f"{len(live)} tombstoned doc_ids are still in the new registry (first: {live[:3]})")
    dead = {r[0] for r in reg.execute("SELECT doc_id FROM tombstones")}
    if not ids <= dead:
        raise uc.UpdateError("the new registry does not list every tombstoned doc_id in its tombstones table")
    reg.close()
    refs = 0
    for d, _, fs in os.walk(stage):
        for f in fs:
            if not f.endswith(".sqlite") or f == "registry.sqlite":
                continue
            db = sqlite3.connect(f"file:{os.path.join(d, f)}?mode=ro", uri=True)
            plain, _r, _f, _s = uc.sqlite_objects(db)
            db.execute("CREATE TEMP TABLE _tomb (id INTEGER PRIMARY KEY)")
            db.executemany("INSERT INTO _tomb VALUES (?)", [(i,) for i in tomb])
            for t in plain:
                if "doc_id" in [r[1] for r in db.execute(f"PRAGMA table_info({uc._q(t)})")]:
                    n = db.execute(f"SELECT count(*) FROM {uc._q(t)} WHERE doc_id IN (SELECT id FROM _tomb)").fetchone()[0]
                    if n:
                        raise uc.UpdateError(f"{os.path.relpath(os.path.join(d, f), stage)}.{t}: {n} rows still point at a tombstoned doc_id")
                    refs += 1
            db.close()
    notes["tables_checked_for_tombstoned_doc_ids"] = refs
    # meaning search: the page_ids of tombstoned articles (from the OLD registry) are gone from every IVF folder
    old = sqlite3.connect(f"file:{base_dir}/registry.sqlite?mode=ro", uri=True)
    pids = tombstoned_page_ids(old, tomb)
    old.close()
    for d, _, fs in os.walk(stage):
        if "ivf.json" in fs and pids:
            ids_ = np.fromfile(os.path.join(d, "ids.i32"), np.int32)
            if np.isin(ids_, np.array(pids, np.int32)).any():
                raise uc.UpdateError(f"{os.path.relpath(d, stage)}: a tombstoned article still has a code in the meaning index")
    # Tantivy: a delete list really removed them
    try:
        import tantivy
    except ImportError:
        notes["tantivy_tombstone_check"] = "skipped: tantivy not importable"
        return
    checked = 0
    for d, _, fs in os.walk(os.path.join(stage, "text")):
        if "meta.json" not in fs:
            continue
        if "doc_id" not in [f["name"] for f in json.load(open(os.path.join(d, "meta.json")))["schema"]]:
            continue
        idx = _open_tantivy(tantivy, d)
        s = idx.searcher()
        for i in tomb:
            if s.search(tantivy.Query.term_query(idx.schema, "doc_id", i), 1).count:
                raise uc.UpdateError(f"{os.path.relpath(d, stage)}: tombstoned doc_id {i} still matches in the Tantivy index")
        checked += 1
    notes["tantivy_indexes_checked_for_tombstones"] = checked


def default_jobs() -> int:
    return max(1, min(4, os.cpu_count() or 1))


def _log(msg: str) -> None:
    print(f"update_apply: {msg}", file=sys.stderr, flush=True)


def _pdeathsig() -> None:
    """In a worker process: die with the parent (Linux PR_SET_PDEATHSIG), so a killed apply leaves nothing writing into a staging folder."""
    try:
        import ctypes
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, int(signal.SIGKILL))
    except Exception:  # noqa: BLE001 - best effort, not every platform has it
        pass


VERIFIED = ".verified.json"


def _stat_key(p: str) -> list:
    st = os.stat(p)
    return [st.st_size, st.st_mtime_ns, st.st_ino]


def load_verified(rel: str) -> dict:
    """path -> [size, mtime_ns, inode, sha256] recorded when `rel` was applied (every file whose sha256 was proved then). Missing or damaged = {}."""
    try:
        d = json.load(open(os.path.join(rel, VERIFIED)))
        return d["files"] if d.get("format") == "logout-verified/1" else {}
    except (OSError, ValueError, KeyError, AttributeError):
        return {}


def _trusted(verified: dict, rel_path: str, src: str, sha: str) -> bool:
    """True when the last apply proved `sha` for this file and its size, mtime and inode are unchanged since. A file that was WRITTEN by an apply is
    never trusted on its own say: only the record of a previous, completed apply (which hashed it when it wrote it) counts, and any change of
    size/mtime/inode, or a missing/damaged record, means it is hashed again."""
    rec = (verified or {}).get(rel_path)
    return bool(rec) and os.path.exists(src) and rec[3] == sha and rec[:3] == _stat_key(src)


def _simple_op(op: dict, cur_dir: str, stage: str, pkg: str, verify_links: bool, verified: dict = None) -> dict:
    """A `link` or `blob` op: independent of every other file, so it runs in a thread (zstd is a subprocess, hashing drops the GIL)."""
    p, dst = op["path"], os.path.join(stage, op["path"])
    r = {"path": p, "entry": {"bytes": op["bytes"], "sha256": op["sha256"]}, "proved": True, "linked": False, "copied_instead_of_linked": 0,
         "linked_bytes": 0, "written_bytes": 0, "blob_files": 0}
    if op["op"] == "link":
        src = os.path.join(cur_dir, op["from_path"])
        trusted = _trusted(verified, op["from_path"], src, op["sha256"])
        r["trusted_by_record"] = trusted
        r["proved"] = verify_links
        if not os.path.exists(src) or os.path.getsize(src) != op["bytes"] or (verify_links and not trusted and uc.sha256_file(src) != op["sha256"]):
            raise uc.UpdateError(f"installed {op['from_path']} is not the file this package expects (missing, wrong size or sha256): nothing changed")
        try:
            os.link(src, dst)
            r["linked"] = True
            r["linked_bytes"] = op["bytes"]
        except OSError:
            uc.copy_file(src, dst)
            r["copied_instead_of_linked"] = 1
            r["written_bytes"] = op["bytes"]
    else:
        z = os.path.join(pkg, op["blob"])
        if not os.path.exists(z):
            raise uc.UpdateError(f"{op['blob']} is missing from the package")
        uc.zst_decompress(z, dst)
        if uc.sha256_file(dst) != op["sha256"]:
            raise uc.UpdateError(f"{p}: sha256 after decompressing differs from update.json (a damaged or tampered package)")
        r["blob_files"] = 1
        r["written_bytes"] = op["bytes"]
    return r


def _sqlite_op(a: tuple) -> dict:
    """One `sqlite-rows` op: check the installed file, copy it, patch the copy from the streamed rows file, check the result. Runs in a
    worker process (the content hash and the row loop are Python-bound) or inline."""
    cur_dir, stage, tmp, pkg, op, quick_check, cache_kib = a
    p = op["path"]
    src, dst = os.path.join(cur_dir, p), os.path.join(stage, p)
    label = os.path.basename(p)
    tm = {}
    t = time.time()
    _log(f"{label}: checking the installed file against the package's base content hash")
    if uc.sqlite_content_sha256(src, label) != op["base_content_sha256"]:
        raise uc.UpdateError(f"installed {p} is not the file this package's row diff was made against: nothing changed")
    tm["base_hash_s"] = round(time.time() - t, 1)
    t = time.time()
    uc.copy_file(src, dst)
    tm["copy_s"] = round(time.time() - t, 1)
    t = time.time()
    counts = uc.sqlite_patch(dst, os.path.join(pkg, op["rows"]), op["role"], os.path.join(stage, "registry.sqlite") if op["role"] == "lookup" else None,
                             label, cache_kib)
    tm["patch_s"] = round(time.time() - t, 1)
    if FAIL_WORKER and FAIL_WORKER == label:
        os.kill(os.getpid(), signal.SIGKILL)
    t = time.time()
    if uc.sqlite_content_sha256(dst, label) != op["content_sha256"]:
        raise uc.UpdateError(f"{p}: the patched file's content differs from the new release (row diff did not reproduce it): nothing changed")
    tm["content_hash_s"] = round(time.time() - t, 1)
    if quick_check:
        t = time.time()
        qc = sqlite3.connect(dst).execute("PRAGMA quick_check").fetchone()[0]
        if qc != "ok":
            raise uc.UpdateError(f"{p}: quick_check says {qc!r}")
        tm["quick_check_s"] = round(time.time() - t, 1)
    db = sqlite3.connect(dst)
    free = db.execute("PRAGMA freelist_count").fetchone()[0]
    db.close()
    size = os.path.getsize(dst)
    _log(f"{label}: done ({', '.join(f'{k} {v}' for k, v in tm.items())})")
    return {"path": p, "counts": dict(counts, freelist_pages=free, seconds=tm), "bytes": size, "sha256": uc.sha256_file(dst),
            "entry": {"bytes": size, "content_sha256": op["content_sha256"], "published_sha256": op["sha256"], "published_bytes": op["bytes"]}}


class _SqliteWorkers:
    """Runs `sqlite-rows` ops in worker PROCESSES (this same script, `_sqlite-op`: the row loop and the content hash are Python-bound, so
    threads would not run in parallel), at most `n` at a time. A worker prints one JSON line (result or refusal) on stdout; its progress
    lines go straight to our stderr. (subprocess, not multiprocessing: no fork/spawn trouble with callers that are `python -` or a REPL.)"""

    def __init__(self, n: int):
        self.pool = ThreadPoolExecutor(max_workers=n)
        self.procs, self.lock = [], threading.Lock()

    def submit(self, args: tuple):
        return self.pool.submit(self._run, args)

    def _run(self, args: tuple) -> dict:
        p = subprocess.Popen(self_cmd() + ["_sqlite-op"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             preexec_fn=_pdeathsig, text=True)
        with self.lock:
            self.procs.append(p)
        out, _ = p.communicate(json.dumps(args))
        try:
            r = json.loads(out.strip().splitlines()[-1])
        except (ValueError, IndexError):
            raise uc.UpdateError(f"{args[4]['path']}: the worker process died (exit {p.returncode}) without a result: nothing changed")
        if "error" in r:
            raise uc.UpdateError(r["error"])
        return r

    def stop(self) -> None:
        """Stop every worker for good before the staging folder is removed."""
        self.pool.shutdown(wait=False, cancel_futures=True)
        with self.lock:
            procs = list(self.procs)
        for pr in procs:
            try:
                pr.kill()
            except OSError:
                pass
        for pr in procs:
            pr.wait()


FAKE_FREE = os.environ.get("LOGOUT_UPDATE_FAKE_FREE_BYTES", "")      # test hook: pretend the disk has this much room


def bytes_needed(upd: dict, new_m: dict) -> int:
    """What staging writes: every file the package patches or ships as bytes (a link costs none), plus 10% and 256 MiB slack."""
    size = {f["path"]: int(f.get("bytes") or 0) for f in new_m.get("files", []) if isinstance(f, dict)}
    n = sum(size.get(op["path"], 0) for op in upd["files"] if op["op"] in ("sqlite-rows", "blob", "ivf-merge"))
    return int(n * 1.1) + (256 << 20)


def check_free_space(base: str, upd: dict, new_m: dict) -> None:
    """Refuse at the start, with the numbers, instead of hours into the hashing and patching (audit DB-11)."""
    need = bytes_needed(upd, new_m)
    free = int(FAKE_FREE) if FAKE_FREE else shutil.disk_usage(base).free
    if free < need:
        raise uc.UpdateError(f"not enough disk space in {base}: {free >> 20} MiB free, about {need >> 20} MiB needed to stage {upd['to_release']}: nothing changed")


def prune_old_releases(base: str, keep: set) -> list:
    """After a successful swap: release folders other than the ones named in `keep` (current, previous) are removed; each
    month would otherwise add the whole changed-file set (about 10 GB) to releases/ (audit DB-11). Only folders that hold a
    manifest.json (a release); never a staging folder or anything else."""
    rel_dir = os.path.join(base, "releases")
    gone = []
    for name in sorted(os.listdir(rel_dir)):
        d = os.path.join(rel_dir, name)
        if name in keep or name.startswith(".") or not os.path.isfile(os.path.join(d, "manifest.json")):
            continue
        shutil.rmtree(d, ignore_errors=True)
        gone.append(name)
    return gone


def apply(base: str, pkg: str, pubkey: str = None, insecure_no_signature: bool = False, verify_links: bool = True, quick_check: bool = True,
          jobs: int = None) -> dict:
    t0 = time.time()
    jobs = max(1, int(jobs or default_jobs()))
    base, pkg = os.path.abspath(base), os.path.abspath(pkg)
    lock = open(os.path.join(base, ".apply.lock"), "w")
    try:
        if os.name == "nt":      # no fcntl on Windows: every Windows data update failed to import (2026-11-23, buildbox)
            import msvcrt
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        raise uc.UpdateError("another apply is running on this directory")
    cur = current_id(base)
    if cur is None:
        raise uc.UpdateError(f"{base}/current is not a symlink to a release: adopt a release first")
    cur_dir = os.path.join(base, "releases", cur)
    # ---- the package itself
    try:
        body = open(os.path.join(pkg, "update.json"), "rb").read()
        upd = json.loads(body)
    except (OSError, ValueError) as e:
        raise uc.UpdateError(f"{pkg}/update.json unreadable: {e}")
    if upd.get("format") != uc.UPDATE_FORMAT:
        raise uc.UpdateError(f"package format {upd.get('format')!r}, this code reads {uc.UPDATE_FORMAT!r}")
    if pubkey:
        verify_signature(pubkey, body, os.path.join(pkg, "update.json.sig"), "update.json")
        if os.path.exists(os.path.join(pkg, "manifest.json.sig")):
            verify_signature(pubkey, open(os.path.join(pkg, "manifest.json"), "rb").read(), os.path.join(pkg, "manifest.json.sig"), "manifest.json")
    elif not insecure_no_signature:
        raise uc.UpdateError("no --pubkey given: refusing an unverified package (--insecure-no-signature for tests only)")
    check_requires(upd["requires"])
    new_raw = open(os.path.join(pkg, "manifest.json"), "rb").read()
    if uc.sha256_bytes(new_raw) != upd["to_manifest_sha256"]:
        raise uc.UpdateError("the package's manifest.json is not the one update.json was built for")
    new_m = json.loads(new_raw)
    to = upd["to_release"]
    if new_m["release"] != to:
        raise uc.UpdateError("manifest.json release differs from update.json to_release")
    pub_name = "manifest.published.json" if os.path.exists(os.path.join(cur_dir, "manifest.published.json")) else "manifest.json"
    base_pub = open(os.path.join(cur_dir, pub_name), "rb").read()
    if cur != upd["from_release"]:
        if cur == to:
            raise uc.UpdateError(f"already on release {to}")
        raise uc.UpdateError(f"the package updates release {upd['from_release']!r}, this machine is on {cur!r}: nothing changed")
    if uc.sha256_bytes(base_pub) != upd["from_manifest_sha256"]:
        raise uc.UpdateError(f"release {cur}'s manifest is not the one this package was built from: nothing changed")
    rel_dir = os.path.join(base, "releases")
    for stale in sorted(os.listdir(rel_dir)):
        if stale.startswith(".staging-"):
            shutil.rmtree(os.path.join(rel_dir, stale), ignore_errors=True)
    final = os.path.join(rel_dir, to)
    if os.path.exists(final):                       # an earlier apply died between the rename and the pointer swap
        old_m = json.load(open(os.path.join(final, "manifest.json")))
        problems = rc.check_release(final)
        # it must be the release THIS package builds, not just one with the same name (audit DB-11): the manifest's bytes are hashed
        try:
            same = uc.sha256_bytes(open(os.path.join(final, "manifest.published.json"), "rb").read()) == upd["to_manifest_sha256"]
        except OSError:
            same = False
        if old_m.get("release") == to and not problems and same:
            try:
                verify_tree(final, old_m, jobs=jobs)
                set_pointer(base, "previous", cur)
                set_pointer(base, "current", to)
                return {"resumed": True, "release": to, "seconds": round(time.time() - t0, 2)}
            except uc.UpdateError:
                pass
        shutil.rmtree(final)
    # ---- verify the base the package reads
    base_m = json.load(open(os.path.join(cur_dir, "manifest.json")))
    check_free_space(base, upd, new_m)
    stage = os.path.join(rel_dir, f".staging-{to}-{os.getpid()}")
    os.makedirs(stage)
    stats = {"linked": 0, "copied_instead_of_linked": 0, "blob_files": 0, "sqlite_patched": 0, "ivf_merged": 0, "linked_bytes": 0, "written_bytes": 0}
    notes = {}
    tpool = ppool = None
    try:
        tmp = os.path.join(stage, ".work")
        os.makedirs(tmp)
        local = {}                                         # path -> {"bytes", "sha256"} of what is really on disk
        linked = set()                                     # shared inodes with the old release: their mode is not ours to change
        ivf_done = set()
        proved = set()                                     # files whose sha256 was established in THIS apply (or trusted from the last one's record)
        sq = [op for op in upd["files"] if op["op"] == "sqlite-rows"]
        simple = [op for op in upd["files"] if op["op"] in ("link", "blob")]
        ivf = [op for op in upd["files"] if op["op"] == "ivf-merge"]
        for op in upd["files"]:
            if op["op"] not in ("link", "blob", "sqlite-rows", "ivf-merge"):
                raise uc.UpdateError(f"{op['path']}: unknown operation {op['op']!r}: update the apply script")
            dst = os.path.join(stage, op["path"])
            if os.path.lexists(dst) and op["op"] != "ivf-merge":
                raise uc.UpdateError(f"{op['path']}: listed twice in update.json")
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            if op["op"] != "ivf-merge":                    # reserve the name so a second listing is caught before any work starts
                open(dst, "xb").close()
        for op in simple + sq:
            os.remove(os.path.join(stage, op["path"]))
        _log(f"applying {to} on top of {cur} with {jobs} job(s): {len(simple)} link/blob files, {len(sq)} SQLite row diffs, {len(ivf)} meaning-search files")
        # SQLite row diffs are the slow part and independent of each other (except lookup, which reads the staged registry): processes
        cache_kib = max(32768, 262144 // max(1, min(jobs, len(sq))))
        verified = load_verified(cur_dir) if verify_links else {}
        futs, inline = {}, []
        if sq and jobs > 1:
            ppool = _SqliteWorkers(min(jobs, len(sq)))
            args = lambda op: (cur_dir, stage, tmp, pkg, op, quick_check, cache_kib)  # noqa: E731
            for op in sq:
                if op["role"] != "lookup":
                    futs[op["path"]] = ppool.submit(args(op))
            reg = [f for pth, f in futs.items() if pth == "registry.sqlite"]
            if reg:
                reg[0].result()                            # lookup's text index is derived from the STAGED registry
            for op in sq:
                if op["role"] == "lookup":
                    futs[op["path"]] = ppool.submit(args(op))
        else:
            inline = sq
        tpool = ThreadPoolExecutor(max_workers=jobs)
        sfuts = [tpool.submit(_simple_op, op, cur_dir, stage, pkg, verify_links, verified) for op in simple]
        for op in ivf:                                     # numpy, memory-heavy, one folder at a time: this thread, while the others run
            p, dst, d = op["path"], os.path.join(stage, op["path"]), op["dir"]
            if d not in ivf_done:
                iv = op["ivf"]
                for n, sha in iv["base_sha256"].items():
                    if not _trusted(verified, f"{d}/{n}", os.path.join(cur_dir, d, n), sha) and uc.sha256_file(os.path.join(cur_dir, d, n)) != sha:
                        raise uc.UpdateError(f"installed {d}/{n} is not the file this package's meaning-search delta was made against")
                pay = os.path.join(pkg, iv["payload"])
                for n in ("removed_ids.i32", "new_ids.i32", "new_codes.u8"):
                    uc.zst_decompress(os.path.join(pay, n + ".zst"), os.path.join(tmp, n))
                ivf_tmp = os.path.join(tmp, "ivf-" + d.replace("/", "__"))
                uc.ivf_merge(os.path.join(cur_dir, d), ivf_tmp, np.fromfile(os.path.join(tmp, "removed_ids.i32"), "<i4"),
                             np.fromfile(os.path.join(tmp, "new_ids.i32"), "<i4"),
                             np.fromfile(os.path.join(tmp, "new_codes.u8"), np.uint8).reshape(-1, uc.DIM_BYTES), to)
                ivf_done.add(d)
                stats["ivf_merged"] += 1
            os.replace(os.path.join(tmp, "ivf-" + d.replace("/", "__"), os.path.basename(p)), dst)
            if uc.sha256_file(dst) != op["sha256"]:
                raise uc.UpdateError(f"{p}: merged meaning-search file differs from the new release")
            stats["written_bytes"] += op["bytes"]
            local[p] = {"bytes": op["bytes"], "sha256": op["sha256"]}
            proved.add(p)
        for f in sfuts:
            r = f.result()
            local[r["path"]] = r["entry"]
            stats["hash_skipped_by_record"] = stats.get("hash_skipped_by_record", 0) + int(r.get("trusted_by_record", False))
            if r["proved"]:
                proved.add(r["path"])
            if r["linked"]:
                linked.add(os.path.join(stage, r["path"]))
                stats["linked"] += 1
            for k in ("copied_instead_of_linked", "linked_bytes", "written_bytes", "blob_files"):
                stats[k] += r[k]
        sres = [_sqlite_op((cur_dir, stage, tmp, pkg, op, quick_check, cache_kib)) for op in inline] + [f.result() for f in futs.values()]
        for r in sres:
            local[r["path"]] = {"bytes": r["bytes"], "sha256": r["sha256"], **r["entry"]}
            proved.add(r["path"])
            stats["sqlite_patched"] += 1
            stats["written_bytes"] += r["bytes"]
            notes.setdefault("sqlite", {})[r["path"]] = r["counts"]
        stats["jobs"] = jobs
        shutil.rmtree(tmp)
        listed = {f["path"] for f in new_m["files"]}
        if set(local) != listed:
            raise uc.UpdateError(f"update.json covers {len(local)} files, the manifest lists {len(listed)}: refusing")
        # ---- tombstones
        tb = open(os.path.join(pkg, "tombstones.json"), "rb").read()
        if uc.sha256_bytes(tb) != upd["tombstones"]["sha256"]:
            raise uc.UpdateError("tombstones.json does not match update.json")
        check_tombstones(stage, cur_dir, json.loads(tb)["doc_ids"], notes)
        # ---- the manifests on this machine: the published one kept verbatim, the local one says what is on disk
        open(os.path.join(stage, "manifest.published.json"), "wb").write(new_raw)
        if os.path.exists(os.path.join(pkg, "manifest.json.sig")):
            shutil.copyfile(os.path.join(pkg, "manifest.json.sig"), os.path.join(stage, "manifest.published.json.sig"))
        lm = json.loads(new_raw)
        for f in lm["files"]:
            loc = local[f["path"]]
            if "published_sha256" in loc:
                f.update(loc)
            elif loc["sha256"] != f["sha256"]:
                raise uc.UpdateError(f"{f['path']}: staged sha256 differs from the manifest")
        lm["local"] = {"applied_from": cur, "update_sha256": uc.sha256_bytes(body),
                       "note": "manifest.published.json is the signed manifest; SQLite files patched in place differ from its sha256/bytes (published_*) and are verified by content_sha256"}
        open(os.path.join(stage, "manifest.json"), "w").write(json.dumps(lm, indent=1, sort_keys=True))
        # ---- the engine's own checks, on the staged folder
        errs = rc.check_release(stage)
        if errs:
            raise uc.UpdateError("the staged release fails the engine's checks: " + "; ".join(errs[:5]))
        for d, _, fs in os.walk(stage):
            if "ivf.json" in fs:
                api = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "serve", "api")
                if os.path.isdir(api):
                    sys.path.insert(0, api)
                    import sem_ivf
                    sem_ivf.IvfIndex(d, to)
        # ---- what this apply proved, so the next one need not re-read an unchanged hard-linked file (size + mtime + inode + the sha256 proved)
        rec = {f["path"]: _stat_key(os.path.join(stage, f["path"])) + [local[f["path"]]["sha256"]] for f in new_m["files"] if f["path"] in proved}
        with open(os.path.join(stage, VERIFIED), "w") as vf:
            json.dump({"format": "logout-verified/1", "release": to, "files": rec}, vf, sort_keys=True)
        # ---- read-only, durable, then the swap
        for d, dirs, fs in os.walk(stage):
            for f in fs:
                if f.endswith(".lock"):                  # Tantivy made it while opening the index above; it must be writable
                    os.remove(os.path.join(d, f))        # when the engine opens the folder, so none is left behind
                elif os.path.join(d, f) not in linked:
                    os.chmod(os.path.join(d, f), 0o444)
        fsync_tree(stage)
        if FAIL_AFTER == "staged":
            os.kill(os.getpid(), signal.SIGKILL)
        os.rename(stage, final)
        fsync_path(rel_dir)
        if FAIL_AFTER == "renamed":
            os.kill(os.getpid(), signal.SIGKILL)
        try:                                                   # the warm-up files of the new release, before `current` moves (never fails the update)
            sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "installer"))
            import install as _inst
            notes["prepare"] = {k: v for k, v in _inst.prepare_release(base, to, final).items() if k in ("ok", "seconds", "error")}
        except Exception as e:  # noqa: BLE001
            notes["prepare"] = {"ok": False, "error": f"{type(e).__name__}: {e}"[:200]}
        set_pointer(base, "previous", cur)
        set_pointer(base, "current", to)
        notes["pruned_releases"] = prune_old_releases(base, {to, cur})
    except BaseException:
        if tpool:
            tpool.shutdown(wait=True, cancel_futures=True)
        if ppool:
            ppool.stop()
        shutil.rmtree(stage, ignore_errors=True)
        raise
    if ppool:
        ppool.pool.shutdown()
    tpool.shutdown()
    return {"release": to, "from": cur, "seconds": round(time.time() - t0, 2), **stats, **notes}


def rollback(base: str) -> str:
    prev = current_id(base, "previous")
    cur = current_id(base)
    if not prev or not os.path.isdir(os.path.join(base, "releases", prev)):
        raise uc.UpdateError("no previous release to go back to")
    set_pointer(base, "current", prev)
    set_pointer(base, "previous", cur)
    return prev


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sp = ap.add_subparsers(dest="cmd", required=True)
    a1 = sp.add_parser("adopt")
    a1.add_argument("base")
    a1.add_argument("release")
    a2 = sp.add_parser("apply")
    a2.add_argument("base")
    a2.add_argument("pkg")
    a2.add_argument("--pubkey")
    a2.add_argument("--insecure-no-signature", action="store_true")
    a2.add_argument("--fast", action="store_true", help="do not hash the unchanged (linked) files, only check their size")
    a2.add_argument("--no-quick-check", action="store_true")
    a2.add_argument("--jobs", type=int, default=None, help=f"files applied in parallel (processes for SQLite row diffs, threads for the rest); default min(4, cpus) = {default_jobs()}")
    a2.add_argument("--progress-every", type=float, default=None, help="seconds between progress lines on stderr (default 10)")
    sp.add_parser("_sqlite-op")                           # internal: one SQLite row diff, args as JSON on stdin (see _SqliteWorkers)
    a3 = sp.add_parser("rollback")
    a3.add_argument("base")
    a4 = sp.add_parser("status")
    a4.add_argument("base")
    a = ap.parse_args()
    if a.cmd == "_sqlite-op":
        try:
            print(json.dumps(_sqlite_op(tuple(json.loads(sys.stdin.read())))))
        except uc.UpdateError as e:
            print(json.dumps({"error": str(e)}))
        return
    try:
        if a.cmd == "adopt":
            r = {"adopted": adopt(a.base, a.release)}
        elif a.cmd == "apply":
            if a.progress_every is not None:
                uc.PROGRESS_EVERY = a.progress_every
                os.environ["LOGOUT_UPDATE_PROGRESS_S"] = str(a.progress_every)    # the worker processes read it at import
            r = apply(a.base, a.pkg, a.pubkey, a.insecure_no_signature, not a.fast, not a.no_quick_check, a.jobs)
        elif a.cmd == "rollback":
            r = {"current": rollback(a.base)}
        else:
            r = {"current": current_id(a.base), "previous": current_id(a.base, "previous"),
                 "releases": sorted(x for x in os.listdir(os.path.join(a.base, "releases")))}
    except uc.UpdateError as e:
        print(f"update_apply: REFUSED: {e}", file=sys.stderr)
        sys.exit(2)
    print(json.dumps(r, sort_keys=True))


if __name__ == "__main__":
    main()
