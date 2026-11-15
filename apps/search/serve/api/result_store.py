"""Precomputed results on disk (2026-11-04, owner: "can't we just pre-compute and save to the DB").

The warmer's searches are kept in a SQLite file, so they survive a restart and do not crowd the in-memory cache.
Only the ranked list (and the "has branches" check) is kept: it depends on the release, the engine's code and its
settings, all frozen until a deploy, while news, scores, weather and answers stay live on every search.

Every row carries the engine's FINGERPRINT: the release manifest, the engine's code and models, and every SEARCH_*
setting that changes results. A row with another fingerprint is never served, so a deploy can never serve the old
code's results as its own; the warmer then rebuilds the head first.

Between deploys (2026-11-14, owner: "fix deploy store issue"): every deploy retired every row, so until the warmer
and the home twin had rebuilt them every visitor's search was computed afresh (public then held 3-4 new searches a
second against 40 stored ones). A visitor's search with no row of this fingerprint is now served the PREVIOUS
fingerprint's row (the newest other fingerprint in the file, chosen when the store opens, under the same snapshot
key, younger than max_age). The warmer never reads them: it computes and writes under the new fingerprint, which
replaces the old row for visitors. A deploy that must not show the old results (a results bug fixed) sets
SEARCH_STORE_PREV=0 (search_api).

Privacy: only the warmer's requests write (search_api: warm=1). A visitor's search is never written here.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
import zlib

# Settings that change how fast or where, never which results: left out of the fingerprint.
NOT_RESULTS = frozenset((
    "SEARCH_CACHE_TTL_S", "SEARCH_COMPUTE_CACHE", "SEARCH_PARTS", "SEARCH_FINISHED", "SEARCH_FINISHED_MAX_AGE_S", "SEARCH_CACHE_STATIC_TTL_S", "SEARCH_CACHE_MAX", "SEARCH_ORT_PREPACK", "SEARCH_ORT_SPIN",
    "SEARCH_CE_THREADS", "SEARCH_API_ALLOW_FROM", "SEARCH_MEMSTAT", "SEARCH_SQLITE_CACHE_KB", "SEARCH_IVF_KERN",
    "SEARCH_IVF_KERN_LIB", "SEARCH_STORE", "SEARCH_STORE_MAX_AGE_S", "SEARCH_STORE_REFRESH_S", "SEARCH_BIND",
    "SEARCH_PORT", "SEARCH_LOG", "SEARCH_REQUIRE_KINDS_MODEL", "SEARCH_NEWS_FILL_EVERY_S", "SEARCH_NEWS_VEC_MAX",
    "SEARCH_STORE_INBOX_EVERY_S", "SEARCH_STORE_PREV", "SEARCH_RADAR_DIR", "SEARCH_SHOP_NAMES", "SEARCH_SHOP_MIN", "SEARCH_SHOP_BOX", "SEARCH_SITENAV", "SEARCH_CURATED", "SEARCH_CURATED_FILL", "SEARCH_CARDLINKS",      # the card's site tree: drawn per request, never stored
    "SEARCH_BREADTH", "SEARCH_BREADTH_FLOOR",                                                   # breadth_list shapes /instant after the store
    "SEARCH_WIKILINKS", "SEARCH_BREADTH_WIKI", "SEARCH_BREADTH_CITE", "SEARCH_BREADTH_WIKI_MIN", "SEARCH_BREADTH_WIKI_FROM",
    "SEARCH_SE_INDEX", "SEARCH_SE_ROWS", "SEARCH_SE_MIN", "SEARCH_SE_POOL", "SEARCH_SE_REUSE",                                           # the Stack Exchange how-to rows: after the store too  # W / C letters: same place, after the store
))
STAMP_KEYS = frozenset(("eng", "pk", "dev"))      # what built a finished answer, not what it says
CODE_SUFFIXES = (".py", ".json", ".so")
# Files in the engine folder that never change a search's results (the warmer, the WebRTC peer and its stand-ins,
# dev tooling, this store): a change to them must not retire every stored row.
NOT_RESULT_CODE = frozenset(("warm.py", "rtc_peer.py", "rtc_selfcheck.py", "av.py", "av_absent.py", "memstat.py",
                             "sq_candidates.py", "native_host.py", "result_store.py", "compute_cache.py", "parts.py",
                             "stage_version.py"))


def _hash_tree(h, root: str, suffixes=None, sizes_only: bool = False) -> None:
    """Every file under root (sorted): its path and either its bytes or, for big model files, its size and mtime."""
    if not root or not os.path.exists(root):
        h.update(f"missing:{root}".encode())
        return
    if os.path.isfile(root):
        paths = [root]
    else:
        paths = []
        for d, _ds, fs in os.walk(root):
            paths += [os.path.join(d, f) for f in fs if not f.endswith(".pyc") and "__pycache__" not in d]
    for p in sorted(paths):
        if suffixes and (not p.endswith(suffixes) or os.path.basename(p) in NOT_RESULT_CODE):
            continue
        h.update(p.encode())
        st = os.stat(p)
        if sizes_only:
            h.update(f"{st.st_size}:{int(st.st_mtime)}".encode())
        else:
            with open(p, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)


def _in_manifest(model_dir: str, release_dir: str | None) -> bool:
    """True when every file of model_dir is a file of the release, listed in its manifest with that size: the
    manifest's sha256 (hashed whole into the fingerprint) then stands for its bytes. Install times differ between
    machines holding the same release, so size + mtime would split one engine's rows in two (2026-11-04: the same
    rc13 installed 50 s apart on indexhost and public)."""
    if not release_dir:
        return False
    rel = os.path.realpath(release_dir)
    root = os.path.realpath(model_dir)
    if not root.startswith(rel + os.sep):
        return False
    try:
        with open(os.path.join(rel, "manifest.json")) as f:
            listed = {e["path"]: e.get("bytes") for e in json.load(f).get("files", []) if isinstance(e, dict)}
    except (OSError, ValueError, KeyError, TypeError):
        return False
    found = 0
    for d, _ds, fs in os.walk(root):
        for name in fs:
            p = os.path.join(d, name)
            if listed.get(os.path.relpath(p, rel)) != os.stat(p).st_size:
                return False
            found += 1
    return found > 0


def fingerprint(app_dir: str, release_dir: str | None, model_dirs=(), env=None) -> str:
    """The engine's results fingerprint: code, release manifest, models, result settings. 16 hex digits.
    The same image, release and settings give the same fingerprint on any machine, so rows computed elsewhere can
    be served here (ship.sh, 2026-11-04)."""
    env = os.environ if env is None else env
    h = hashlib.sha256(b"logout-result-store/2")
    _hash_tree(h, app_dir, CODE_SUFFIXES)
    if release_dir:
        _hash_tree(h, os.path.join(release_dir, "manifest.json"))
    for m in model_dirs:
        if _in_manifest(m, release_dir):
            h.update(f"manifest:{os.path.relpath(os.path.realpath(m), os.path.realpath(release_dir))}".encode())
        else:
            _hash_tree(h, m, sizes_only=True)
    for k in sorted(env):
        if k.startswith("SEARCH_") and k not in NOT_RESULTS:
            h.update(f"{k}={env[k]}".encode())
    return h.hexdigest()[:16]


def norm_key(parts) -> str:
    """A stable text key for a cache tuple (frozensets sorted)."""
    def conv(x):
        if isinstance(x, (frozenset, set)):
            return sorted(conv(v) for v in x)
        if isinstance(x, (tuple, list)):
            return [conv(v) for v in x]
        return x
    return json.dumps(conv(parts), separators=(",", ":"), ensure_ascii=False, default=str)


class ResultStore:
    """rows(fp, kind, key, body, at). Thread-safe: one connection behind a lock (reads are well under a millisecond)."""

    def __init__(self, path: str, fp: str, max_age_s: float = 86400.0):
        self.path, self.fp, self.max_age = path, fp, max_age_s
        self.lock = threading.Lock()
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None, timeout=5)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS rows (fp TEXT NOT NULL, kind TEXT NOT NULL, key TEXT NOT NULL, "
                        "body BLOB NOT NULL, at REAL NOT NULL, PRIMARY KEY (fp, kind, key)) WITHOUT ROWID")
        # Curated answers (2026-11-05): a list chosen offline by a large model for a name, keyed by RELEASE, not by
        # this engine's fingerprint (a code change must not retire them; tools/curated-build.py writes them).
        self.db.execute("CREATE TABLE IF NOT EXISTS curated (release TEXT NOT NULL, key TEXT NOT NULL, body BLOB NOT NULL, "
                        "at REAL NOT NULL, PRIMARY KEY (release, key)) WITHOUT ROWID")
        # Finished answers (2026-11-12): the visitor-independent part of a whole answer (list, card, place, kinds...)
        # built ahead of time by a copy of this engine, keyed by RELEASE like the curated lists: a code change does
        # not empty it, entries are replaced as the builder gets to them.
        self.db.execute("CREATE TABLE IF NOT EXISTS finished (release TEXT NOT NULL, key TEXT NOT NULL, body BLOB NOT NULL, "
                        "at REAL NOT NULL, PRIMARY KEY (release, key)) WITHOUT ROWID")
        # Shopping departments (2026-11-13): for a name from a store's search suggestions, how sure a
        # local model is that it is shopped for (p) and where (d, with its confidence c). Labelled at home, shipped
        # like the lists; keyed by the name alone: it does not depend on the release or on this engine's code.
        self.db.execute("CREATE TABLE IF NOT EXISTS dept (key TEXT PRIMARY KEY, p REAL NOT NULL, d TEXT NOT NULL, c REAL NOT NULL, "
                        "at REAL NOT NULL) WITHOUT ROWID")
        # Parts of an answer (phase 2, 2026-11-15, parts.py): one stage's output under an exact key (stage code
        # version, settings, data, inputs); h = the body's own hash, for the stages after it. Never retired by a
        # deploy: a part whose stage did not change is found again under the same key.
        self.db.execute("CREATE TABLE IF NOT EXISTS parts (stage TEXT NOT NULL, key TEXT NOT NULL, h TEXT NOT NULL, "
                        "body BLOB NOT NULL, at REAL NOT NULL, PRIMARY KEY (stage, key)) WITHOUT ROWID")
        self.stats = {"hit": 0, "miss": 0, "put": 0, "skip": 0, "curated_hit": 0, "finished_hit": 0, "prev_hit": 0}
        self.opened = time.time()
        # the fingerprint before this one: the other fingerprint with the newest row (none on a first start)
        r = self.db.execute("SELECT fp FROM rows WHERE fp <> ? GROUP BY fp ORDER BY max(at) DESC LIMIT 1", (fp,)).fetchone()
        self.prev_fp = r[0] if r else None

    def get(self, kind: str, key: str, max_age: float | None = None):
        """(value, age_s) of a row of this fingerprint younger than max_age, else None."""
        with self.lock:
            r = self.db.execute("SELECT body, at FROM rows WHERE fp = ? AND kind = ? AND key = ?", (self.fp, kind, key)).fetchone()
        age = time.time() - r[1] if r else None
        if not r or age > (self.max_age if max_age is None else max_age):
            self.stats["miss"] += 1
            return None
        self.stats["hit"] += 1
        return json.loads(zlib.decompress(r[0])), age

    def get_prev(self, kind: str, key: str):
        """(value, age_s) of the previous fingerprint's row younger than max_age, else None: for a visitor while the
        warmer has not rebuilt this row yet. A row of this fingerprint, once written, is always read first."""
        if not self.prev_fp:
            return None
        with self.lock:
            r = self.db.execute("SELECT body, at FROM rows WHERE fp = ? AND kind = ? AND key = ?", (self.prev_fp, kind, key)).fetchone()
        if not r or time.time() - r[1] > self.max_age:
            return None
        self.stats["prev_hit"] += 1
        return json.loads(zlib.decompress(r[0])), time.time() - r[1]

    def put(self, kind: str, key: str, value) -> bool:
        """Keep value; refused (False) when it does not come back from JSON exactly as it went in."""
        text = json.dumps(value, separators=(",", ":"), ensure_ascii=False)
        if json.loads(text) != value:
            self.stats["skip"] += 1
            return False
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO rows VALUES (?, ?, ?, ?, ?)",
                            (self.fp, kind, key, zlib.compress(text.encode(), 6), time.time()))
        self.stats["put"] += 1
        return True

    def import_file(self, path: str, kinds=("search", "names_places")) -> dict:
        """Rows computed by another engine (ship.sh): only this fingerprint's, only known kinds, only bodies that
        decode, younger than max_age; a row replaces ours only when it is newer. Returns counts."""
        n = {"added": 0, "newer": 0, "kept": 0, "refused": 0}
        now = time.time()
        src = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            has = src.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'rows'").fetchone()
            rows = src.execute("SELECT fp, kind, key, body, at FROM rows").fetchall() if has else []   # a curated-only file
        finally:
            src.close()
        for fp, kind, key, body, at in rows:
            try:
                ok = (fp == self.fp and kind in kinds and isinstance(key, str) and len(key) < 4096
                      and isinstance(body, bytes) and len(body) < (2 << 20) and isinstance(at, float | int)
                      and now - self.max_age < at <= now + 60)
                if ok:
                    json.loads(zlib.decompress(body))
            except (zlib.error, ValueError):
                ok = False
            if not ok:
                n["refused"] += 1
                continue
            with self.lock:
                old = self.db.execute("SELECT at FROM rows WHERE fp = ? AND kind = ? AND key = ?", (fp, kind, key)).fetchone()
                if old and old[0] >= at:
                    n["kept"] += 1
                    continue
                self.db.execute("INSERT OR REPLACE INTO rows VALUES (?, ?, ?, ?, ?)", (fp, kind, key, body, float(at)))
            n["newer" if old else "added"] += 1
        return n

    def get_curated(self, release: str, key: str):
        """The curated body for (release, key), or None."""
        with self.lock:
            r = self.db.execute("SELECT body FROM curated WHERE release = ? AND key = ?", (release, key)).fetchone()
        if not r:
            return None
        self.stats["curated_hit"] += 1
        return json.loads(zlib.decompress(r[0]))

    def curated_at(self, release: str, key: str):
        """When the curated entry for (release, key) was written, or None when there is none."""
        with self.lock:
            r = self.db.execute("SELECT at FROM curated WHERE release = ? AND key = ?", (release, key)).fetchone()
        return r[0] if r else None

    def get_finished(self, release: str, key: str, max_age: float):
        """The finished answer for (release, key) younger than max_age, or None."""
        with self.lock:
            r = self.db.execute("SELECT body, at FROM finished WHERE release = ? AND key = ?", (release, key)).fetchone()
        if not r or time.time() - r[1] > max_age:
            return None
        self.stats["finished_hit"] += 1
        return json.loads(zlib.decompress(r[0]))

    def finished_at(self, release: str, key: str):
        """When the finished answer for (release, key) was built (epoch seconds), or None."""
        with self.lock:
            r = self.db.execute("SELECT at FROM finished WHERE release = ? AND key = ?", (release, key)).fetchone()
        return float(r[0]) if r else None

    def put_finished(self, release: str, key: str, value: dict) -> bool:
        """Keep a finished answer; refused (False) when it does not come back from JSON exactly as it went in."""
        text = json.dumps(value, separators=(",", ":"), ensure_ascii=False)
        if json.loads(text) != value:
            self.stats["skip"] += 1
            return False
        with self.lock:
            # An answer that comes out the same as the stored one is not written again (2026-11-15): only its stamp (eng, pk,
            # dev) moves and its time stays, because the nightly lists publish ships every answer written since the last
            # publish, and an unchanged answer must not reach every install as a download.
            r = self.db.execute("SELECT body FROM finished WHERE release = ? AND key = ?", (release, key)).fetchone()
            if r:
                try:
                    old = json.loads(zlib.decompress(r[0]))
                except (zlib.error, ValueError):
                    old = None
                if old is not None and all(old.get(k) == value.get(k) for k in set(old) | set(value) if k not in STAMP_KEYS):
                    self.db.execute("UPDATE finished SET body = ? WHERE release = ? AND key = ?",
                                    (zlib.compress(text.encode(), 6), release, key))
                    self.stats["finished_kept"] = self.stats.get("finished_kept", 0) + 1
                    return True
            self.db.execute("INSERT OR REPLACE INTO finished VALUES (?, ?, ?, ?)",
                            (release, key, zlib.compress(text.encode(), 6), time.time()))
        return True

    def import_finished(self, path: str, release: str, version: int) -> dict:
        """Finished answers of this release and layout version from a file (twin/ship.sh); newer replaces ours."""
        n = {"added": 0, "newer": 0, "kept": 0, "refused": 0}
        now = time.time()
        src = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            has = src.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'finished'").fetchone()
            rows = src.execute("SELECT release, key, body, at FROM finished").fetchall() if has else []
        finally:
            src.close()
        for rel, key, body, at in rows:
            try:
                ok = (rel == release and isinstance(key, str) and len(key) < 1024 and isinstance(body, bytes)
                      and len(body) < (1 << 20) and isinstance(at, float | int) and at <= now + 60)
                if ok:
                    b = json.loads(zlib.decompress(body))
                    ok = isinstance(b, dict) and b.get("v") == version and isinstance(b.get("results"), list)
            except (zlib.error, ValueError):
                ok = False
            if not ok:
                n["refused"] += 1
                continue
            with self.lock:
                old = self.db.execute("SELECT at FROM finished WHERE release = ? AND key = ?", (rel, key)).fetchone()
                if old and old[0] >= at:
                    n["kept"] += 1
                    continue
                self.db.execute("INSERT OR REPLACE INTO finished VALUES (?, ?, ?, ?)", (rel, key, body, float(at)))
            n["newer" if old else "added"] += 1
        return n

    def get_dept(self, key: str):
        """(p, department, confidence) for a name, or None when it has not been labelled."""
        with self.lock:
            return self.db.execute("SELECT p, d, c FROM dept WHERE key = ?", (key,)).fetchone()

    def import_dept(self, path: str) -> dict:
        """Department labels from a file (twin/dept/dept-ship.sh); a newer label replaces ours."""
        n = {"added": 0, "newer": 0, "kept": 0, "refused": 0}
        now = time.time()
        src = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            has = src.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'dept'").fetchone()
            rows = src.execute("SELECT key, p, d, c, at FROM dept").fetchall() if has else []
        finally:
            src.close()
        for key, p, d, c, at in rows:
            if not (isinstance(key, str) and 0 < len(key) <= 200 and isinstance(p, float | int) and 0 <= p <= 1 and isinstance(d, str)
                    and d.isalpha() and len(d) <= 24 and isinstance(c, float | int) and 0 <= c <= 1 and isinstance(at, float | int) and at <= now + 60):
                n["refused"] += 1
                continue
            with self.lock:
                old = self.db.execute("SELECT at FROM dept WHERE key = ?", (key,)).fetchone()
                if old and old[0] >= at:
                    n["kept"] += 1
                    continue
                self.db.execute("INSERT OR REPLACE INTO dept VALUES (?, ?, ?, ?, ?)", (key, float(p), d, float(c), float(at)))
            n["newer" if old else "added"] += 1
        return n

    def import_curated(self, path: str, release: str) -> dict:
        """Curated rows of this release from a file (tools/curated-build.py); a row replaces ours when newer."""
        n = {"added": 0, "newer": 0, "kept": 0, "refused": 0}
        src = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            has = src.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'curated'").fetchone()
            rows = src.execute("SELECT release, key, body, at FROM curated").fetchall() if has else []
        finally:
            src.close()
        for rel, key, body, at in rows:
            try:
                ok = (rel == release and isinstance(key, str) and len(key) < 1024 and isinstance(body, bytes)
                      and len(body) < (1 << 20) and isinstance(at, float | int))
                if ok:
                    b = json.loads(zlib.decompress(body))
                    ok = isinstance(b, dict) and isinstance(b.get("rows"), list)
            except (zlib.error, ValueError):
                ok = False
            if not ok:
                n["refused"] += 1
                continue
            with self.lock:
                old = self.db.execute("SELECT at FROM curated WHERE release = ? AND key = ?", (rel, key)).fetchone()
                if old and old[0] >= at:
                    n["kept"] += 1
                    continue
                self.db.execute("INSERT OR REPLACE INTO curated VALUES (?, ?, ?, ?)", (rel, key, body, float(at)))
            n["newer" if old else "added"] += 1
        return n

    def reset_lists(self, path: str, release: str) -> dict:
        """A lists BASE (install.py lists update, 2026-11-14): a file whose lists_meta table says reset=1 replaces every
        hand-picked list, finished answer and department label: what is not in it is dropped first. Delta files carry
        no reset and only add or replace (import_curated / import_finished / import_dept)."""
        src = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            has = src.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'lists_meta'").fetchone()
            meta = dict(src.execute("SELECT k, v FROM lists_meta").fetchall()) if has else {}
        finally:
            src.close()
        if str(meta.get("reset", "")) != "1":
            return {}
        with self.lock:
            n = {"reset_curated": self.db.execute("DELETE FROM curated WHERE release = ?", (release,)).rowcount,
                 "reset_finished": self.db.execute("DELETE FROM finished WHERE release = ?", (release,)).rowcount,
                 "reset_dept": self.db.execute("DELETE FROM dept").rowcount}
        return n

    def sample_queries(self, n: int) -> list[str]:
        """n random stored searches' words (for the warmer's audit of rows it did not compute itself)."""
        with self.lock:
            keys = self.db.execute("SELECT key FROM rows WHERE fp = ? AND kind = 'search' ORDER BY random() LIMIT ?",
                                   (self.fp, n)).fetchall()
        out = []
        for (k,) in keys:
            try:
                parts = json.loads(k)[1]
                if isinstance(parts, list) and parts and isinstance(parts[0], str):
                    out.append(parts[0])
            except (ValueError, IndexError, TypeError):
                pass
        return out

    def counts(self) -> dict:
        with self.lock:
            mine = self.db.execute("SELECT kind, count(*) FROM rows WHERE fp = ? GROUP BY kind", (self.fp,)).fetchall()
            other = self.db.execute("SELECT count(*) FROM rows WHERE fp <> ?", (self.fp,)).fetchone()[0]
            fresh = self.db.execute("SELECT count(*) FROM rows WHERE fp = ? AND kind = 'search' AND at > ?",
                                    (self.fp, time.time() - self.max_age)).fetchone()[0]
        # fresh: searches of this fingerprint still servable; up_s: since this engine opened the store. The outside
        # check alerts when an engine up for an hour has few fresh rows (no rebuild after a deploy, or a dead warmer).
            cur = self.db.execute("SELECT release, count(*) FROM curated GROUP BY release").fetchall()
        return {"fp": self.fp, "prev_fp": self.prev_fp, "rows": dict(mine), "other_fp_rows": other, "fresh": fresh, "curated": dict(cur),
                "up_s": int(time.time() - self.opened), **self.stats}

    def get_part(self, stage: str, key: str):
        """The part (stage, key) as the value it was stored from, or None."""
        with self.lock:
            r = self.db.execute("SELECT body FROM parts WHERE stage = ? AND key = ?", (stage, key)).fetchone()
        return json.loads(zlib.decompress(r[0])) if r else None

    def put_part(self, stage: str, key: str, h: str, raw: bytes) -> None:
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO parts VALUES (?, ?, ?, ?, ?)", (stage, key, h, zlib.compress(raw, 6), time.time()))

    def prune(self, keep_other_s: float = 2 * 86400) -> int:
        """Drop rows of other fingerprints older than keep_other_s (a rollback within that time finds its rows)."""
        with self.lock:
            n = self.db.execute("DELETE FROM rows WHERE fp <> ? AND at < ?", (self.fp, time.time() - keep_other_s)).rowcount
        return n
