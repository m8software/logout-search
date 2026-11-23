"""The `current` / `previous` pointers of an install directory, and the fsync helpers (stdlib only).

Shared by update_apply.py (monthly updates) and ../installer/install.py (first install), so both swap
the pointer the same way: ONE rename(2) of a fresh symlink. Layout (SEARCH-UPDATE-PACKAGE.md section 1):

    BASE/releases/<release id>/     one folder per release, read-only
    BASE/current  -> releases/<id>  the one pointer the engine follows
    BASE/previous -> releases/<id>  what `update_apply.py rollback` goes back to

Imports nothing outside the standard library on purpose: a client without numpy can use it.
"""
import os
import sys


def fsync_path(p: str) -> None:
    if os.name == "nt":
        # Windows cannot open a directory for fsync (NTFS journals the rename itself), and its fsync (_commit) refuses a
        # handle opened read-only: EBADF, which stopped every Windows install at its last step (owner, 2026-11-14). A file
        # is opened for writing to flush it; one that cannot be (read-only, in use) was written and closed already.
        if os.path.isdir(p):
            return
        try:
            fd = os.open(p, os.O_RDWR | getattr(os, "O_BINARY", 0))
        except OSError:
            return
        try:
            os.fsync(fd)
        except OSError:
            pass
        finally:
            os.close(fd)
        return
    fd = os.open(p, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def fsync_tree(root: str) -> None:
    for d, _dirs, fs in os.walk(root):
        for f in fs:
            fsync_path(os.path.join(d, f))
        fsync_path(d)


def _remove_pointer(p: str) -> None:
    """Remove a symlink or a junction (never what it points at)."""
    if os.path.islink(p):
        os.remove(p)
    elif os.path.isdir(p):
        os.rmdir(p)                              # a junction is removed like an empty directory; a real folder would not be empty


def set_pointer(base: str, name: str, target: str) -> None:
    """Point BASE/name at releases/<target> with one atomic rename of a fresh symlink.

    Windows without Developer Mode refuses a symlink; the pointer is then an NTFS junction (no privilege needed, and
    os.path.realpath follows it). A junction cannot be renamed over an existing one, so that swap is remove + rename."""
    tmp = os.path.join(base, f".{name}.new-{os.getpid()}")
    dst = os.path.join(base, name)
    if os.path.lexists(tmp):
        _remove_pointer(tmp)
    rel = os.path.join("releases", target)
    try:
        os.symlink(rel, tmp, target_is_directory=True)
    except OSError:
        if os.name != "nt":
            raise
        import _winapi
        _winapi.CreateJunction(os.path.join(os.path.abspath(base), rel), tmp)
    try:
        os.replace(tmp, dst)
    except OSError:
        if os.name != "nt":
            raise
        _remove_pointer(dst)
        os.rename(tmp, dst)
    fsync_path(base)


def current_id(base: str, name: str = "current"):
    p = os.path.join(base, name)
    if os.path.islink(p):
        return os.path.basename(os.readlink(p))
    if os.name == "nt" and os.path.isdir(p):     # a junction: not a link to Python, but realpath resolves it
        real = os.path.realpath(p)
        if os.path.normcase(real) != os.path.normcase(os.path.abspath(p)):
            return os.path.basename(real)
    return None


def rmtree_all(d: str) -> None:
    """shutil.rmtree that also removes read-only files: on Windows a read-only file cannot be deleted, and releases are
    installed read-only, so an old release folder was never pruned (2026-11-23). Errors after the retry are ignored, as before."""
    import shutil
    import stat

    def retry(fn, path, _exc):
        try:
            for q in (os.path.dirname(path), path):       # the folder that holds it, then the entry itself
                try:
                    os.chmod(q, os.stat(q).st_mode | stat.S_IWRITE | (stat.S_IEXEC if os.path.isdir(q) else 0))
                except OSError:
                    pass
            fn(path)
        except OSError:
            pass
    if sys.version_info >= (3, 12):
        shutil.rmtree(d, onexc=retry)
    else:
        shutil.rmtree(d, onerror=retry)

