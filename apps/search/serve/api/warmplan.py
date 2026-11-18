"""Which parts of a release the first searches read, and a reader that brings them into the operating system's file cache with large
sequential reads (2026-11-17).

Why: a cold search touches tens of thousands of pages of the IVF vectors, the lookup database and the registry. Where the OS reads ahead
(Linux) that is fast; where it does not (Windows reads a mapped file's pages on fault, 4 KB at a time, and SQLite's own reads are one
ReadFile each) every page is a synchronous read through the antivirus filter driver: 170,000 major faults and 1.1 million read calls in the
first two minutes on a fast NVMe with readahead switched off. A few large reads of the same bytes cost one I/O each and leave the pages in the
cache, where later faults and preads are served from memory.

The measured hot set of rc15 after ~150 distinct searches (resident pages per file, `hotset.py`): the IVF codes (nearly whole: 568 of 621 MB and
265 of 269 MB), lookup.sqlite 477 MB (names 171, cards 102, sites 52, suggest_items 37, accounts 14), registry.sqlite 422 MB (its `registry`
table), kinds.sqlite 77, signals.bin 84 of 223, the typo index 37 + 16, the Tantivy wiki idx 102 of 1,665 (scattered), nomic weights 23.
"""
from __future__ import annotations

import glob
import json
import os
import sys
import time

# whole files, in the order that matters (a first search needs the IVF centroids and offsets before the codes)
TIER1 = ("vectors/ivf/ivf.json", "vectors/ivf/centroids.u8", "vectors/ivf/offsets.i64", "vectors/ivf/ids.i32", "vectors/ivf/codes.u8",
         "vectors/pages-ivf/ivf.json", "vectors/pages-ivf/centroids.u8", "vectors/pages-ivf/offsets.i64", "vectors/pages-ivf/ids.i32",
         "vectors/pages-ivf/codes.u8", "signals.bin", "kinds.sqlite", "anchor-phrases.sqlite")
# the Tantivy indexes (wiki, sites, pages, anchors, typo): every file under text/ up to this size whole (term dictionaries, stores, fieldnorms,
# fast fields, positions and the small postings); the two big postings files (1.7 GB and 0.65 GB on rc15) are read by their hot pages only as
# the searches touch them: SEARCH_WARM_POSTINGS=1 reads them too
TEXT_FILE_MAX = 200 << 20
# SQLite tables the first searches read (the rest of those files is not): their page ranges come from dbstat
TABLES = {"lookup.sqlite": ("sites", "suggest_items", "accounts", "names"), "registry.sqlite": ("registry", "hosts")}
GAP_PAGES = 64                  # ranges closer than this (256 KB) are read as one


def _merge(pages: list[int], psz: int) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for p in sorted(set(pages)):
        off = (p - 1) * psz
        if out and off - (out[-1][0] + out[-1][1]) <= GAP_PAGES * psz:
            out[-1] = (out[-1][0], off + psz - out[-1][0])
        else:
            out.append((off, psz))
    return out


def table_ranges(path: str, tables: tuple[str, ...]) -> list[tuple[int, int]]:
    """Byte ranges (offset, length) of the pages of these tables and their indexes, from dbstat. [] when this SQLite has no dbstat."""
    import sqlite3
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        psz = c.execute("PRAGMA page_size").fetchone()[0]
        names = set(tables)
        for t in tables:                                          # the table's indexes too
            names.update(r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = ?", (t,)))
        marks = ",".join("?" * len(names))
        pages = [r[0] for r in c.execute(f"SELECT pageno FROM dbstat WHERE name IN ({marks})", sorted(names))]
        return _merge(pages, psz)
    except sqlite3.Error:
        return []
    finally:
        c.close()


def shipped_tier2(release_dir: str) -> list:
    """The hot table ranges kept in the app (warmplans.json beside this file), for the release in release_dir: used only when the release's own
    folder name and the sizes of the files the ranges point into match. Windows' Python has no dbstat, so an install cannot work them out;
    a release already on installs gets its plan with the app, not with a data re-release. Anything that does not match gives [] (no ranges)."""
    try:
        table = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "warmplans.json")))
        rid = os.path.basename(os.path.realpath(release_dir))
        try:
            rid = json.load(open(os.path.join(release_dir, "manifest.json"))).get("release") or rid
        except (OSError, ValueError):
            pass
        e = table.get(rid)
        if not e:
            return []
        for rel, size in e["sizes"].items():
            if os.path.getsize(os.path.join(release_dir, rel)) != size:
                return []
        return e["tier2"]
    except (OSError, ValueError, KeyError, TypeError):
        return []


def build_plan(release_dir: str, tables: bool = True) -> dict:
    """{"tier1": [[relpath, offset, length]...], "tier2": [...]} for this release folder (files that are absent are left out)."""
    tier1, tier2 = [], []
    for pat in TIER1:
        for p in sorted(glob.glob(os.path.join(release_dir, pat))):
            if os.path.isfile(p) and os.path.getsize(p):
                tier1.append([os.path.relpath(p, release_dir).replace(os.sep, "/"), 0, os.path.getsize(p)])
    text = os.path.join(release_dir, "text")
    big = []
    for d, _dirs, fs in sorted(os.walk(text)):
        for f in sorted(fs):
            p = os.path.join(d, f)
            size = os.path.getsize(p)
            rel = os.path.relpath(p, release_dir).replace(os.sep, "/")
            if f == "meta.json" or not size:
                continue
            (tier1 if size <= TEXT_FILE_MAX else big).append([rel, 0, size])
    plan_big = big
    for rel, tabs in (TABLES.items() if tables else ()):       # the ranges need a pass over the tables (dbstat: 10 s on NVMe, minutes on a slow disk): prepare only
        p = os.path.join(release_dir, rel)
        if os.path.isfile(p):
            tier2 += [[rel, off, ln] for off, ln in table_ranges(p, tabs)]
    if not tier2:
        tier2 = shipped_tier2(release_dir)                   # no dbstat here (or a start that must not run it): the plan shipped with the app, when it fits this release
    return {"tier1": tier1, "tier2": tier2, "postings": plan_big}


def load_plan(release_dir: str, state_dir: str = "", release_id: str = "", compute: bool = True, tables: bool = True) -> dict | None:
    """The plan for this release: the release's own warmplan.json, else a cached one in the state folder, else (compute=True) built now with
    dbstat (one slow pass over those tables the first time) and cached. None when nothing can be built."""
    for p in (os.path.join(release_dir, "warmplan.json"),
              os.path.join(state_dir, f"warmplan-{release_id}.json") if state_dir and release_id else ""):
        if p and os.path.isfile(p):
            try:
                return json.load(open(p))
            except (OSError, ValueError):
                pass
    if not compute:
        return None
    plan = build_plan(release_dir, tables)
    if tables and state_dir and release_id and os.path.isdir(state_dir) and any(plan.values()):     # never creates the folder: a store that cannot open must stay closed
        try:
            json.dump(plan, open(os.path.join(state_dir, f"warmplan-{release_id}.json"), "w"))
        except OSError:
            pass
    return plan


def plan_bytes(plan: dict, tiers=("tier1", "tier2")) -> int:
    return sum(r[2] for t in tiers for r in plan.get(t, []))


def read_ranges(release_dir: str, ranges: list, budget_bytes: int, deadline: float, progress, idle=None, chunk: int = 8 << 20,
                workers: int = 3, piece: int = 64 << 20) -> int:
    """Read these [relpath, offset, length] ranges with large reads (one syscall per `chunk`) on `workers` threads (an NVMe needs several
    reads in flight; each piece is sequential), in the order given, at most budget_bytes, until `deadline` (time.monotonic).
    progress(n) is told every chunk; idle() (the engine's gate for background work) is called between chunks. Returns the bytes read."""
    import threading
    pieces = []
    for rel, off, ln in ranges:
        for o in range(off, off + ln, piece):
            pieces.append((rel, o, min(piece, off + ln - o)))
    lock = threading.Lock()
    state = {"i": 0, "done": 0}

    def work():
        fh, cur = None, None
        try:
            while True:
                with lock:
                    if state["i"] >= len(pieces) or state["done"] >= budget_bytes or time.monotonic() >= deadline:
                        return
                    rel, off, ln = pieces[state["i"]]
                    state["i"] += 1
                    ln = min(ln, budget_bytes - state["done"])
                    state["done"] += ln                      # reserved; the progress callback counts what was really read
                if cur != rel:
                    if fh:
                        fh.close()
                    fh, cur = None, rel
                    try:
                        fh = open(os.path.join(release_dir, rel), "rb", buffering=0)
                    except OSError:
                        continue
                if fh is None:
                    continue
                pos, end = off, off + ln
                while pos < end and time.monotonic() < deadline:
                    if idle:
                        idle()
                    fh.seek(pos)
                    n = len(fh.read(min(chunk, end - pos)))
                    if not n:
                        break
                    pos += n
                    progress(n)
        finally:
            if fh:
                fh.close()
    ts = [threading.Thread(target=work, daemon=True, name="warm-read") for _ in range(max(1, workers))]
    [t.start() for t in ts]
    [t.join() for t in ts]
    return state["done"]


ANON_RESERVE = 1300 << 20      # what the engine holds outside the file cache once started: 1.05 GB anonymous measured on rc15 (models, Python, SQLite and Tantivy heaps) plus headroom


def available_ram() -> int:
    """Bytes the file cache can use without pushing the engine into paging or thrashing (0 when unknown): Linux MemAvailable, and under a
    cgroup limit (a container, a systemd scope: the minimum-machine test) the limit less the anonymous memory already in use; Windows the
    physical memory available."""
    try:
        if sys.platform.startswith("linux"):
            avail = 0
            for ln in open("/proc/meminfo"):
                if ln.startswith("MemAvailable:"):
                    avail = int(ln.split()[1]) * 1024
            try:
                lim = open("/sys/fs/cgroup/memory.max").read().strip()
                if lim.isdigit():
                    anon = 0
                    for ln in open("/sys/fs/cgroup/memory.stat"):
                        if ln.startswith("anon "):
                            anon = int(ln.split()[1])
                    avail = min(avail, int(lim) - anon) if avail else int(lim) - anon
            except OSError:
                pass
            return avail
        elif sys.platform == "win32":
            import ctypes

            class MS(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong)] + [(n, ctypes.c_ulonglong) for n in (
                    "ullTotalPhys", "ullAvailPhys", "ullTotalPageFile", "ullAvailPageFile", "ullTotalVirtual", "ullAvailVirtual", "ullAvailExtendedVirtual")]
            ms = MS()
            ms.dwLength = ctypes.sizeof(ms)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(ms)):
                return int(ms.ullAvailPhys)
    except Exception:  # noqa: BLE001
        pass
    return 0


SMALL_MACHINE = 6 << 30        # at or under this much memory the engine runs its small-machine settings (minspec F: -100 MB, latency unchanged)


def total_ram() -> int:
    """Bytes of RAM this process may use: the machine's, or the cgroup limit when lower (0 when unknown)."""
    try:
        if sys.platform.startswith("linux"):
            tot = 0
            for ln in open("/proc/meminfo"):
                if ln.startswith("MemTotal:"):
                    tot = int(ln.split()[1]) * 1024
            try:
                lim = open("/sys/fs/cgroup/memory.max").read().strip()
                if lim.isdigit():
                    tot = min(tot, int(lim)) if tot else int(lim)
            except OSError:
                pass
            return tot
        if sys.platform == "win32":
            import ctypes

            class MS(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong)] + [(n, ctypes.c_ulonglong) for n in (
                    "ullTotalPhys", "ullAvailPhys", "ullTotalPageFile", "ullAvailPageFile", "ullTotalVirtual", "ullAvailVirtual", "ullAvailExtendedVirtual")]
            ms = MS()
            ms.dwLength = ctypes.sizeof(ms)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(ms)):
                return int(ms.ullTotalPhys)
    except Exception:  # noqa: BLE001
        pass
    return 0


def small_machine() -> bool:
    t = total_ram()
    return 0 < t <= SMALL_MACHINE


def file_budget() -> int:
    """How many bytes of release files the start-up may pull into the cache: available memory less what the engine will hold itself
    (ANON_RESERVE); 0 = unknown, the caller decides."""
    ram = available_ram()
    return max(ram - ANON_RESERVE, 0) if ram else 0


if __name__ == "__main__":                       # python warmplan.py RELEASE_DIR OUT.json: for a release builder (assemble.py) or a one-off
    rd, out = sys.argv[1], sys.argv[2]
    plan = build_plan(rd)
    json.dump(plan, open(out, "w"))
    print(f"{out}: tier1 {len(plan['tier1'])} files {plan_bytes(plan, ('tier1',)) >> 20} MB, tier2 {len(plan['tier2'])} ranges {plan_bytes(plan, ('tier2',)) >> 20} MB")
