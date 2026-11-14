"""Which release folder the engine reads (SEARCH-UPDATE-PACKAGE.md section 1).

update_apply.py keeps  BASE/releases/<id>/  and one symlink  BASE/current -> releases/<id>,  swapped by a
single rename. resolve(path) turns what the engine was configured with into the real release folder:

  path is a release folder (has manifest.json)      -> itself
  path is a symlink (BASE/current)                  -> the folder it points at, right now
  path is a BASE (no manifest.json, has `current`)  -> the folder BASE/current points at, right now
  anything else (no pointer, missing path)          -> path unchanged: the default behaviour

The answer is remembered for the life of the process, so every reader (lookups, typo index, signals,
meaning search, text indexes) opens the same release and an update applied while the engine runs
changes nothing until the next start. A BASE whose `current` is broken or not a release stops the
start with a clear error; it never falls back to some other folder.
"""
from __future__ import annotations

import os
import threading

_pinned: dict = {}
_lock = threading.Lock()


class ReleaseRootError(RuntimeError):
    pass


def _is_release(p: str) -> bool:
    return os.path.isfile(os.path.join(p, "manifest.json"))


def _resolve(path: str) -> str:
    p = path.rstrip("/") or path
    cur = os.path.join(p, "current")
    if not _is_release(p) and os.path.islink(cur):
        real = os.path.realpath(cur)
        if not _is_release(real):
            raise ReleaseRootError(f"{cur} -> {os.readlink(cur)} is not a release folder (no manifest.json): "
                                   "refusing to start on a broken pointer; fix it or `update_apply.py rollback`")
        return real
    real = os.path.realpath(p)
    return p if real == os.path.abspath(p) else real      # a symlinked path is pinned to its target


def resolve(path: str) -> str:
    """The release folder for `path`, decided once per process (see the module doc)."""
    with _lock:
        if path not in _pinned:
            _pinned[path] = _resolve(path)
        return _pinned[path]


def forget() -> None:
    """Drop what was pinned (tests only: the engine never re-reads the pointer)."""
    with _lock:
        _pinned.clear()
