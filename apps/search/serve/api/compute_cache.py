"""The engine's model outputs kept on disk, keyed by exactly their inputs (phase 1, 2026-11-14).

The dear part of an answer is the models: the query's nomic code (the Wikipedia and pages vector lists), the bge
vector (kinds, news) and the cross-encoder's scores for each (query, batch of result texts). None of them depends on the
engine's ranking code, so a deploy that changes a card rule or the list cleanup has no reason to run them again.
Rebuilding the stored answers after such a deploy reads them from here.

Key = sha256 of the namespace and the exact input. The namespace names the model by the sha256 of its file and every
setting that changes the output (the cross-encoder's token limit and text form), so a new model or setting simply
misses; an old entry is never served for different inputs. Values are the model's own numbers (float32 bytes, the
96-byte code), byte for byte.

SEARCH_COMPUTE_CACHE=<file> turns it on (the answer builder); unset = off and this module is never imported.
Privacy: the keys are hashes; the file holds no query text, but it is still kept on our machines only (the builder's).
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import threading

_FILE_HASHES: dict = {}


def file_sha256(path: str) -> str:
    """sha256 of a model file, once per process."""
    if path not in _FILE_HASHES:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        _FILE_HASHES[path] = h.hexdigest()
    return _FILE_HASHES[path]


class ComputeCache:
    def __init__(self, path: str):
        self.path = path
        self.lock = threading.Lock()
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None, timeout=30)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS kv (k BLOB PRIMARY KEY, v BLOB NOT NULL) WITHOUT ROWID")
        self.stats = {"hit": 0, "miss": 0}

    @staticmethod
    def key(ns: str, item: str) -> bytes:
        return hashlib.sha256(ns.encode() + b"\x00" + item.encode()).digest()

    def get_many(self, ns: str, items: list[str]) -> list:
        keys = [self.key(ns, it) for it in items]
        with self.lock:
            got = {}
            for i in range(0, len(keys), 500):
                part = keys[i:i + 500]
                got.update(self.db.execute(f"SELECT k, v FROM kv WHERE k IN ({','.join('?' * len(part))})", part).fetchall())
        out = [got.get(k) for k in keys]
        hit = sum(v is not None for v in out)
        self.stats["hit"] += hit
        self.stats["miss"] += len(out) - hit
        return out

    def put_many(self, ns: str, items: list[str], values: list[bytes]) -> None:
        rows = [(self.key(ns, it), v) for it, v in zip(items, values)]
        if not rows:
            return
        with self.lock:
            try:
                self.db.executemany("INSERT OR REPLACE INTO kv VALUES (?, ?)", rows)
            except sqlite3.Error:
                pass                            # a full disk or a busy file costs a recompute next time, never an answer


_cache: list = []
_cache_lock = threading.Lock()


def cache() -> ComputeCache | None:
    """The process's cache, or None when SEARCH_COMPUTE_CACHE is unset or the file cannot be opened."""
    path = os.environ.get("SEARCH_COMPUTE_CACHE", "").strip()
    if not path:
        return None
    with _cache_lock:
        if not _cache:
            try:
                _cache.append(ComputeCache(path))
                print(f"compute cache {path}: on", flush=True)
            except (sqlite3.Error, OSError) as e:
                print(f"compute cache {path}: off ({type(e).__name__}: {e})", flush=True)
                _cache.append(None)
    return _cache[0]


class CachedScorer:
    """Wraps a cross-encoder: (q, texts) -> scores, cached by the WHOLE batch. The int8 model quantises its activations
    per batch (dynamic quantisation: one scale for the batch's tensor), so a pair's score depends on the other rows
    scored with it; measured 2026-11-14 on 200 names, caching pair by pair changed 58 of 200 lists. Keyed by the query and
    every text in order, a cached batch is exactly what the model would return for that batch."""

    def __init__(self, inner, ns: str, c: ComputeCache):
        import struct
        self.inner, self.ns, self.c, self.struct = inner, ns, c, struct
        self.device = getattr(inner, "device", "cpu")
        self.name = getattr(inner, "name", "")

    def __call__(self, q: str, texts: list[str]) -> list[float]:
        if not texts:
            return self.inner(q, texts)
        item = q + "\x00" + "\x01".join(texts)
        v = self.c.get_many(self.ns, [item])[0]
        if v is not None and len(v) == 8 * len(texts):
            return list(self.struct.unpack(f"<{len(texts)}d", v))
        out = [float(x) for x in self.inner(q, texts)]
        self.c.put_many(self.ns, [item], [self.struct.pack(f"<{len(out)}d", *out)])
        return out


class CachedCall:
    """Wraps a one-input model call (text -> value) with to_bytes / from_bytes for its value."""

    def __init__(self, inner, ns: str, c: ComputeCache, to_bytes, from_bytes):
        self.inner, self.ns, self.c, self.to_b, self.from_b = inner, ns, c, to_bytes, from_bytes

    def __call__(self, text: str, *args):
        item = text if not args else text + "\x00" + "\x00".join(map(str, args))
        v = self.c.get_many(self.ns, [item])[0]
        if v is not None:
            return self.from_b(v)
        out = self.inner(text, *args)
        if out is not None:
            self.c.put_many(self.ns, [item], [self.to_b(out)])
        return out
