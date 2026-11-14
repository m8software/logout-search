"""Open a release's Tantivy index when the release is read-only (SEARCH-RELEASE-FORMAT, rule 10).

tantivy-py's Index.open() takes .tantivy-meta.lock for writing even to read, so a release folder
whose files are read-only fails with "Failed to acquire Lockfile: PermissionDenied" unless both
lock files exist and the process may write them (measured on tantivy-py 0.26.2: dir and index
files read-only is fine; lock files missing or read-only is not).

open_index(tantivy, path) opens `path` in place when both lock files are there and writable.
Otherwise it opens a writable overlay: a cache folder holding the two lock files and one symlink
per index file, so nothing is ever written into the release (its manifest hashes stay true).
The overlay folder is named by the index's real path, so a restart reuses it; its links are
refreshed on every open, so a release swapped in place is seen. Where the overlay lives:
$LOGOUT_TV_OVERLAY_DIR, else <tmp>/logout-tv-overlay-<uid>.

Windows (2026-11-14, the downloaded program never started: os.getuid does not exist there, and making a symlink
needs administrator rights or developer mode): the folder is named by the user name, and an index file whose
symlink is refused is hard-linked instead (no rights needed, same drive: the installer puts the overlay in the
data folder, install.engine_env).
"""
from __future__ import annotations

import hashlib
import os
import tempfile

LOCKS = (".tantivy-meta.lock", ".tantivy-writer.lock")


def _locks_writable(path: str) -> bool:
    return all(os.access(os.path.join(path, f), os.W_OK) and os.path.isfile(os.path.join(path, f)) for f in LOCKS)


def _who() -> str:
    if hasattr(os, "getuid"):
        return str(os.getuid())
    name = os.environ.get("USERNAME") or os.environ.get("USER") or "user"
    return "".join(c for c in name if c.isalnum() or c in "-_") or "user"


def overlay_dir(path: str) -> str:
    root = os.environ.get("LOGOUT_TV_OVERLAY_DIR") or os.path.join(
        tempfile.gettempdir(), f"logout-tv-overlay-{_who()}")
    real = os.path.realpath(path)
    return os.path.join(root, hashlib.sha256(real.encode()).hexdigest()[:16])


def make_overlay(path: str) -> str:
    """A writable folder that looks like `path` to Tantivy: own lock files, symlinks to the rest."""
    real = os.path.realpath(path)
    ov = overlay_dir(path)
    os.makedirs(ov, mode=0o700, exist_ok=True)
    want = {n for n in os.listdir(real) if n not in LOCKS}
    for n in os.listdir(ov):
        if n not in want and n not in LOCKS:
            os.unlink(os.path.join(ov, n))                # an index file the release no longer has
    for n in sorted(want):
        link, target = os.path.join(ov, n), os.path.join(real, n)
        if os.path.islink(link):
            if os.readlink(link) == target:
                continue
        elif os.path.isfile(link) and os.path.isfile(target) and os.path.samefile(link, target):
            continue                                       # a hard link (Windows) to this very file
        tmp = f"{link}.{os.getpid()}.tmp"
        if os.path.lexists(tmp):
            os.unlink(tmp)
        try:
            os.symlink(target, tmp)
        except OSError:
            if not os.path.isfile(target):
                raise
            os.link(target, tmp)                           # symlinks refused (Windows without the right): a hard link
        os.replace(tmp, link)                              # atomic: a concurrent opener never sees a gap
    for f in LOCKS:
        with open(os.path.join(ov, f), "ab"):
            pass
    return ov


def open_index(tantivy, path: str):
    """tantivy.Index.open(path), working whether or not the folder is writable. The returned
    Index reads the release's own files; only its lock files may live elsewhere."""
    if _locks_writable(path):
        return tantivy.Index.open(path)
    return tantivy.Index.open(make_overlay(path))
