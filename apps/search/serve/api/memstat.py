"""Memory snapshot for the box measurements: with SEARCH_MEMSTAT=1 the engine answers
GET /memz with what the process holds, by source, so a climb can be named instead of guessed.

    /memz            RSS / anon / file-backed (smaps_rollup), glibc malloc (mallinfo2), SQLite's heap
                     (sqlite3_memory_used), threads, every bounded cache's entry count, and the
                     tracemalloc totals when the engine was started with `python -X tracemalloc=N`
    /memz?maps=1     RSS grouped by mapped file (the ONNX models, the IVF codes, Tantivy segments,
                     the SQLite files, [heap], anonymous)
    /memz?deep=1     the byte size of the result cache's entries (walks them; a few ms at 4,096)
    /memz?top=25     the 25 largest tracemalloc lines (needs -X tracemalloc)
    /memz?trim=1     malloc_trim(0) first: how much of the anonymous climb glibc can give back

Off by default and never on a customer's box: it is a measurement tool, nothing in it is read by a
search. The one hook is in search_api.Handler.do_GET."""

from __future__ import annotations

import ctypes
import ctypes.util
import gc
import json
import os
import sys
import threading
import tracemalloc

_KB_FIELDS = ("Rss", "Pss", "Pss_Anon", "Pss_File", "Pss_Shmem", "Shared_Clean", "Shared_Dirty",
              "Private_Clean", "Private_Dirty", "Anonymous", "Swap")


def _kb_file(path: str, fields=_KB_FIELDS) -> dict[str, int]:
    """`Name:   1234 kB` lines of a /proc file -> {Name: kB} for the names asked for."""
    out: dict[str, int] = {}
    try:
        with open(path, encoding="ascii", errors="replace") as f:
            for line in f:
                k, _, v = line.partition(":")
                if k in fields:
                    out[k] = int(v.split()[0])
    except OSError as e:
        out["error"] = f"{type(e).__name__}"
    return out


def smaps_rollup() -> dict:
    return _kb_file("/proc/self/smaps_rollup")


def status() -> dict:
    return _kb_file("/proc/self/status", ("VmRSS", "VmHWM", "RssAnon", "RssFile", "RssShmem", "VmSwap", "Threads"))


def by_mapping(top: int = 30) -> list[list]:
    """RSS per mapped file, largest first: [[name, rss_kB, count]]. Anonymous mappings are '[anon]'."""
    acc: dict[str, list[int]] = {}
    name = "[anon]"
    try:
        with open("/proc/self/smaps", encoding="ascii", errors="replace") as f:
            for line in f:
                if line[:1] != " " and "-" in line.split(" ", 1)[0]:        # a mapping header
                    parts = line.split()
                    name = parts[5] if len(parts) >= 6 else "[anon]"
                    acc.setdefault(name, [0, 0])[1] += 1
                elif line.startswith("Rss:"):
                    acc.setdefault(name, [0, 0])[0] += int(line.split()[1])
    except OSError as e:
        return [[f"error {type(e).__name__}", 0, 0]]
    rows = sorted(((n, v[0], v[1]) for n, v in acc.items()), key=lambda r: -r[1])
    return [list(r) for r in rows[:top]]


class _Mallinfo2(ctypes.Structure):
    _fields_ = [(n, ctypes.c_size_t) for n in ("arena", "ordblks", "smblks", "hblks", "hblkhd", "usmblks",
                                                "fsmblks", "uordblks", "fordblks", "keepcost")]


def malloc_stats(trim: bool = False) -> dict:
    """glibc's view of its heap, in bytes: arena (sbrk heap), hblkhd (mmapped blocks), uordblks (in use),
    fordblks (free but held). trim=True calls malloc_trim(0) first and says how much RSS it returned."""
    out: dict = {}
    try:
        libc = ctypes.CDLL(None)
        if trim:
            before = smaps_rollup().get("Rss", 0)
            libc.malloc_trim.restype = ctypes.c_int
            out["trimmed"] = bool(libc.malloc_trim(0))
            out["trim_freed_kb"] = before - smaps_rollup().get("Rss", 0)
        libc.mallinfo2.restype = _Mallinfo2
        mi = libc.mallinfo2()
        out.update({n: int(getattr(mi, n)) for n, _t in _Mallinfo2._fields_})
    except (OSError, AttributeError) as e:
        out["error"] = f"{type(e).__name__}: {e}"
    return out


def sqlite_memory() -> dict:
    """SQLite's whole-process heap (page caches, statements, temp tables), bytes: used and high water."""
    try:
        lib = ctypes.CDLL(ctypes.util.find_library("sqlite3") or "libsqlite3.so.0")
        lib.sqlite3_memory_used.restype = ctypes.c_int64
        lib.sqlite3_memory_highwater.restype = ctypes.c_int64
        lib.sqlite3_memory_highwater.argtypes = [ctypes.c_int]
        return {"used": int(lib.sqlite3_memory_used()), "highwater": int(lib.sqlite3_memory_highwater(0))}
    except (OSError, AttributeError) as e:
        return {"error": f"{type(e).__name__}: {e}"}


def deep_size(obj, seen: set | None = None) -> int:
    """Bytes held by obj and what it refers to (dicts, lists, tuples, sets, str/bytes/numbers); each object once."""
    seen = set() if seen is None else seen
    stack = [obj]
    total = 0
    while stack:
        o = stack.pop()
        i = id(o)
        if i in seen:
            continue
        seen.add(i)
        total += sys.getsizeof(o)
        if isinstance(o, dict):
            stack.extend(o.keys())
            stack.extend(o.values())
        elif isinstance(o, (list, tuple, set, frozenset)):
            stack.extend(o)
    return total


def caches(ns: dict, deep: bool = False) -> dict:
    """Entry counts of every bounded cache the engine keeps (search_api's and the release lookups'); deep=True
    adds the byte size of the result cache's entries."""
    out: dict = {}
    for name in ("_cache", "_news_vecs"):
        c = ns.get(name)
        if c is not None:
            out[name] = {"entries": len(c)}
    ip = ns.get("intent_probs")
    if ip is not None and hasattr(ip, "cache_info"):
        ci = ip.cache_info()
        out["intent_probs"] = {"entries": ci.currsize, "hits": ci.hits, "misses": ci.misses}
    lk = ns.get("_lookups")
    if lk is not None:
        for name in ("_regc", "_cards", "_df", "_claims", "_chain_hours", "place_doc"):
            c = getattr(lk, name, None)
            if isinstance(c, dict):
                out["lookups." + name] = {"entries": len(c)}
    if deep:
        c = ns.get("_cache")
        if c is not None:
            with ns["_cache_lock"]:
                items = list(c.items())
            seen: set = set()
            out["_cache"]["bytes"] = sum(deep_size(v, seen) + deep_size(k, seen) for k, v in items)
            out["_cache"]["results"] = sum(len(v[2]) for _k, v in items if isinstance(v, tuple) and len(v) == 3
                                           and hasattr(v[2], "__len__"))
        for name in ("_regc", "_cards", "_df"):
            c = getattr(lk, name, None) if lk is not None else None
            if isinstance(c, dict):
                out["lookups." + name]["bytes"] = deep_size(dict(c))
    return out


def trace(top: int = 0) -> dict:
    """tracemalloc totals, and the `top` largest lines when asked (only when the process traces)."""
    if not tracemalloc.is_tracing():
        return {"tracing": False}
    cur, peak = tracemalloc.get_traced_memory()
    out: dict = {"tracing": True, "current": cur, "peak": peak, "overhead": tracemalloc.get_tracemalloc_memory()}
    if top > 0:
        snap = tracemalloc.take_snapshot()
        out["top"] = [{"where": str(s.traceback[0]), "size": s.size, "count": s.count}
                      for s in snap.statistics("lineno")[:top]]
    return out


def report(ns: dict, qs: dict | None = None) -> bytes:
    """The JSON body of /memz. ns is search_api's module namespace (its caches); qs the parsed query string."""
    qs = qs or {}
    flag = lambda k: (qs.get(k) or ["0"])[0] not in ("", "0")  # noqa: E731
    try:
        top = int((qs.get("top") or ["0"])[0])
    except ValueError:
        top = 0
    body = {
        "smaps_rollup_kb": smaps_rollup(),
        "status_kb": status(),
        "malloc": malloc_stats(trim=flag("trim")),
        "sqlite": sqlite_memory(),
        "gc": {"counts": gc.get_count(), "objects": len(gc.get_objects()) if flag("deep") else None},
        "threads": {"active": threading.active_count(),
                    "names": sorted({t.name.split("_")[0] for t in threading.enumerate()})},
        "caches": caches(ns, deep=flag("deep")),
        "tracemalloc": trace(top),
    }
    if flag("maps"):
        body["by_mapping_kb"] = by_mapping()
    return json.dumps(body).encode()
