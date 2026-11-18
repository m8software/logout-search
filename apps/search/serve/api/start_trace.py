"""Startup trace for the engine (2026-11-17). Off unless SEARCH_START_TRACE=1: then search_api.py reports, at 30 s, 2 min and
5 min after the process started, tables in the log (WAITING is kept apart from WORKING: a lock, a worker pool's queue, a single-flight wait):

  - the start steps (loads, models): wall, CPU, bytes read, major page faults;
  - per search stage, the FIRST call: wall, CPU, bytes read, major faults, then calls, total and slowest;
  - waits: per named lock, the worker pool's queue ("pool queue": the time a submitted task sat before a worker took it) and single-flight
    waits: how many, total, longest; and the pool's depth now;
  - a request that was slow (>= 1 s) and spent > 500 ms outside its longest stage or waited >= 300 ms gets its own line with what it
    waited for (never the query); a request in flight for over 2 s gets one stack dump of the worker threads and the queue depth.

  - per stage (suggest, sem-ivf, places, fuzzy_wiki, ...): the first call's wall and CPU time, the number of calls, the total and the
    slowest. Wall far above CPU is waiting: on the disk, on a lock, or for another thread.
  - the process's I/O and page faults (Linux: /proc/self/io and /proc/self/stat minor/major faults; Windows: GetProcessIoCounters and
    GetProcessMemoryInfo's PageFaultCount) and, on Linux, the resident bytes of every mapped release file (smaps): what the searches
    so far actually pulled in per file.
  - model and index load times, time spent waiting for the result-store lock, thread counts.

SEARCH_TEST_NO_READAHEAD=1 (a test aid, never for a customer) makes Linux behave like Windows' file access for the release files: no
readahead on the open files (POSIX_FADV_RANDOM) and none for the mapped ones (MADV_RANDOM), so every cold page is one synchronous
read of its own. The Windows cache manager reads a mapped file's pages on fault without Linux's readahead, and every one of those
reads passes through the antivirus filter driver.
"""
from __future__ import annotations

import contextvars
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

ON = os.environ.get("SEARCH_START_TRACE") == "1"
NO_READAHEAD = os.environ.get("SEARCH_TEST_NO_READAHEAD") == "1"
T0 = time.monotonic()
_lock = threading.Lock()
_stages: dict[str, list] = {}          # name -> [first wall ms, first cpu ms, calls, total wall ms, max wall ms]
_loads: dict[str, float] = {}          # name -> seconds (models, indexes)
_waits: dict[str, list] = {}           # lock name -> [n, total ms, max ms]


_seen: set[str] = set()
_first_io: dict[str, tuple] = {}       # stage -> (read MB, major faults) during its first call (the whole process: other threads add noise)
_steps: list[tuple] = []               # (name, wall s, cpu s, read MB, major faults)
_req = contextvars.ContextVar("trace_req", default=None)      # the current request's waits: {name: [n, ms]}


def _io_now() -> tuple[float, int]:
    io = io_counters()
    return (io.get("read_bytes", io.get("rchar", 0)) / (1 << 20), io.get("majflt", io.get("page_faults", 0)))


def stage_begin(name: str):
    """A token for the first call of a stage (io counters before), None for every later call (no cost)."""
    return _io_now() if name not in _seen else None


def stage(name: str, wall_ms: float, cpu_ms: float, tok=None) -> None:
    with _lock:
        r = _stages.get(name)
        if r is None:
            _stages[name] = [wall_ms, cpu_ms, 1, wall_ms, wall_ms]
            _seen.add(name)
            if tok is not None:
                a, b = _io_now()
                _first_io[name] = (round(a - tok[0], 1), b - tok[1])
        else:
            r[2] += 1
            r[3] += wall_ms
            r[4] = max(r[4], wall_ms)


def step(name: str, fn):
    """Run a start step and record its wall, CPU, bytes read and major faults."""
    t0, c0, io0 = time.monotonic(), time.process_time(), _io_now()
    try:
        return fn()
    finally:
        io1 = _io_now()
        with _lock:
            _steps.append((name, time.monotonic() - t0, time.process_time() - c0, io1[0] - io0[0], io1[1] - io0[1]))


def load(name: str, seconds: float) -> None:
    with _lock:
        _loads[name] = _loads.get(name, 0.0) + seconds


def lock_wait(name: str, ms: float) -> None:
    """A wait (a lock, the pool's queue, a single-flight): into the table and into the current request's account."""
    with _lock:
        r = _waits.setdefault(name, [0, 0.0, 0.0])
        r[0] += 1
        r[1] += ms
        r[2] = max(r[2], ms)
    d = _req.get()
    if d is not None:
        e = d.setdefault(name, [0, 0.0])
        e[0] += 1
        e[1] += ms


class TracedLock:
    """threading.Lock that records every acquire that had to wait (>= 1 ms)."""

    def __init__(self, name: str):
        self.name, self._l = name, threading.Lock()

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        if self._l.acquire(False):
            return True
        if not blocking:
            return False
        t = time.monotonic()
        got = self._l.acquire(True, timeout)
        dt = (time.monotonic() - t) * 1000
        if dt >= 1.0:
            lock_wait("lock " + self.name, dt)
        return got

    def release(self) -> None:
        self._l.release()

    def locked(self) -> bool:
        return self._l.locked()

    __enter__ = acquire

    def __exit__(self, *a) -> None:
        self._l.release()


def lock(name: str):
    """A named lock: plain threading.Lock() when the trace is off."""
    return TracedLock(name) if ON else threading.Lock()


_pools: list = []


class TracedPool(ThreadPoolExecutor):
    """ThreadPoolExecutor that records the time each task sat in the queue before a worker took it ("pool queue"), against the request that
    submitted it."""

    def submit(self, fn, *a, **k):
        t = time.monotonic()
        d = _req.get()

        def run(*a2, **k2):
            ms = (time.monotonic() - t) * 1000
            with _lock:
                r = _waits.setdefault("pool queue", [0, 0.0, 0.0])
                r[0] += 1
                r[1] += ms
                r[2] = max(r[2], ms)
            if d is not None:
                e = d.setdefault("pool queue", [0, 0.0])
                e[0] += 1
                e[1] += ms
            return fn(*a2, **k2)
        return super().submit(run, *a, **k)


def pool(max_workers: int, prefix: str):
    p = (TracedPool if ON else ThreadPoolExecutor)(max_workers=max_workers, thread_name_prefix=prefix)
    if ON:
        _pools.append((prefix, max_workers, p))
    return p


_inflight: dict[int, list] = {}        # request id -> [thread id, start, dumped]
_next_id = [0]


def req_begin() -> int:
    """Start of a request: its own wait account, and a place in the in-flight list the watchdog reads."""
    _req.set({})
    with _lock:
        _next_id[0] += 1
        rid = _next_id[0]
        _inflight[rid] = [threading.get_ident(), time.monotonic(), False]
    return rid


def req_end(rid: int) -> None:
    with _lock:
        _inflight.pop(rid, None)


def request_done(ms: float, stages: dict) -> None:
    """After a request's answer is built: a line when it was slow and not explained by its longest stage, or when it waited (never the query)."""
    d = _req.get() or {}
    waited = sum(v[1] for v in d.values())
    longest = max(stages.items(), key=lambda kv: kv[1], default=("-", 0.0))
    outside = ms - longest[1]
    if ms >= 1000 and (outside > 500 or waited >= 300):
        parts = ", ".join(f"{k} n={v[0]} {v[1]:.0f} ms" for k, v in sorted(d.items(), key=lambda kv: -kv[1][1])) or "no traced wait"
        print(f"slow request {ms:.0f} ms: longest stage {longest[0]} {longest[1]:.0f} ms, {outside:.0f} ms outside it; waited {waited:.0f} ms ({parts}); "
              f"stages {','.join(f'{k}:{v:.0f}' for k, v in sorted(stages.items(), key=lambda kv: -kv[1])[:5])}", flush=True)


def _watchdog() -> None:
    """One stack dump (the worker threads and the request's own, a few frames each) and the pool depth for a request in flight for over 2 s."""
    while True:
        time.sleep(1.0)
        now = time.monotonic()
        with _lock:
            old = [(rid, v) for rid, v in _inflight.items() if not v[2] and now - v[1] > 2.0]
            for _rid, v in old:
                v[2] = True
        if not old:
            continue
        frames = sys._current_frames()
        names = {t.ident: t.name for t in threading.enumerate()}
        lines = []
        for rid, v in old[:1]:
            depth = ", ".join(f"{pre} queued={p._work_queue.qsize()} workers={len(p._threads)}/{mw}" for pre, mw, p in _pools)
            lines.append(f"request in flight {now - v[1]:.1f} s: pool {depth}; threads waiting:")
        import traceback
        for tid, fr in frames.items():
            nm = names.get(tid, "?")
            if nm.startswith("q_") or tid == old[0][1][0]:
                st = traceback.extract_stack(fr)[-4:]
                lines.append(f"  {nm}: " + " <- ".join(f"{os.path.basename(f.filename)}:{f.lineno} {f.name}" for f in reversed(st)))
        print("\n".join(lines[:20]), flush=True)


def io_counters() -> dict:
    out: dict = {}
    try:
        if sys.platform.startswith("linux"):
            for ln in open("/proc/self/io"):
                k, _, v = ln.partition(":")
                if k in ("rchar", "read_bytes", "syscr"):
                    out[k] = int(v)
            st = open("/proc/self/stat").read().rsplit(")", 1)[1].split()
            out["minflt"], out["majflt"] = int(st[7]), int(st[9])
        elif sys.platform == "win32":
            import ctypes
            from ctypes import wintypes

            class IOC(ctypes.Structure):
                _fields_ = [(n, ctypes.c_ulonglong) for n in ("ro", "wo", "oo", "rb", "wb", "ob")]

            class PMC(ctypes.Structure):
                _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [(n, ctypes.c_size_t) for n in (
                    "PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage", "QuotaPagedPoolUsage",
                    "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage")]
            k32 = ctypes.windll.kernel32
            h = k32.GetCurrentProcess()
            ioc = IOC()
            if k32.GetProcessIoCounters(h, ctypes.byref(ioc)):
                out["read_ops"], out["read_bytes"] = ioc.ro, ioc.rb
            pm = PMC()
            pm.cb = ctypes.sizeof(pm)
            if ctypes.windll.psapi.GetProcessMemoryInfo(h, ctypes.byref(pm), pm.cb):
                out["page_faults"] = pm.PageFaultCount
    except Exception as e:  # noqa: BLE001 - a trace never breaks the engine
        out["error"] = type(e).__name__
    return out


def mapped_files(prefix: str) -> dict[str, int]:
    """Linux: resident KB per mapped file under prefix (smaps)."""
    out: dict[str, int] = {}
    if not prefix or not sys.platform.startswith("linux"):
        return out
    try:
        cur = None
        for ln in open("/proc/self/smaps"):
            if ln[0] in "0123456789abcdef" and "-" in ln.split(" ", 1)[0]:
                parts = ln.split(None, 5)
                cur = parts[5].strip() if len(parts) > 5 and parts[5].startswith(prefix) else None
            elif cur and ln.startswith("Rss:"):
                out[cur] = out.get(cur, 0) + int(ln.split()[1])
    except Exception:  # noqa: BLE001
        pass
    return out


def os_threads() -> int:
    try:
        if sys.platform.startswith("linux"):
            for ln in open("/proc/self/status"):
                if ln.startswith("Threads:"):
                    return int(ln.split()[1])
    except Exception:  # noqa: BLE001
        pass
    return threading.active_count()


def report(label: str, release_dir: str = "") -> str:
    now = time.monotonic() - T0
    with _lock:
        stages = sorted(_stages.items(), key=lambda kv: -kv[1][3])
        loads = dict(_loads)
        waits = {k: list(v) for k, v in _waits.items()}
    lines = [f"start trace {label} (process age {now:.0f} s)"]
    with _lock:
        steps = list(_steps)
        fio = dict(_first_io)
    if steps:
        lines.append(f"  {'start step':<34}{'wall s':>8}{'cpu s':>8}{'read MB':>9}{'maj flt':>9}")
        for n_, w_, c_, mb_, f_ in steps:
            lines.append(f"  {n_:<34}{w_:>8.2f}{c_:>8.2f}{mb_:>9.0f}{f_:>9}")
    lines.append(f"  {'stage (first call)':<20}{'wall ms':>9}{'cpu ms':>8}{'read MB':>9}{'maj flt':>9}{'calls':>7}{'total ms':>10}{'max ms':>9}")
    for name, (fw, fc, n, tot, mx) in stages[:24]:
        mb_, fl_ = fio.get(name, (0, 0))
        lines.append(f"  {name:<20}{fw:>9.0f}{fc:>8.0f}{mb_:>9.0f}{fl_:>9}{n:>7}{tot:>10.0f}{mx:>9.0f}")
    io = io_counters()
    lines.append("  process: " + " ".join(f"{k}={v}" for k, v in io.items()) + f" threads python={threading.active_count()} os={os_threads()}")
    if loads:
        lines.append("  loads (s): " + ", ".join(f"{k} {v:.1f}" for k, v in sorted(loads.items(), key=lambda kv: -kv[1])))
    if waits:
        lines.append("  WAITING (not working): " + ", ".join(f"{k} n={v[0]} total={v[1]:.0f} ms max={v[2]:.0f} ms" for k, v in sorted(waits.items(), key=lambda kv: -kv[1][1])))
    if _pools:
        lines.append("  pool now: " + ", ".join(f"{pre} queued={p._work_queue.qsize()} threads={len(p._threads)}/{mw}" for pre, mw, p in _pools))
    mf = mapped_files(release_dir)
    if mf:
        top = sorted(mf.items(), key=lambda kv: -kv[1])[:8]
        lines.append("  resident mapped release files (MB): " + ", ".join(f"{os.path.relpath(p, release_dir)} {kb / 1024:.0f}" for p, kb in top))
    return "\n".join(lines)


def _readahead_off(prefix: str) -> int:
    """Linux test aid: no readahead for the release's open files and mappings. Returns how many were set."""
    n = 0
    try:
        for fd in os.listdir("/proc/self/fd"):
            try:
                p = os.readlink(f"/proc/self/fd/{fd}")
            except OSError:
                continue
            if p.startswith(prefix) and os.path.isfile(p):
                try:
                    os.posix_fadvise(int(fd), 0, 0, os.POSIX_FADV_RANDOM)
                    n += 1
                except OSError:
                    pass
        import ctypes
        libc = ctypes.CDLL(None, use_errno=True)
        libc.madvise.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int]
        seen = set()
        for ln in open("/proc/self/maps"):
            parts = ln.split(None, 5)
            if len(parts) > 5 and parts[5].strip().startswith(prefix):
                a, b = (int(x, 16) for x in parts[0].split("-"))
                if (a, b) not in seen:
                    seen.add((a, b))
                    if libc.madvise(a, b - a, 1) == 0:        # MADV_RANDOM
                        n += 1
    except Exception:  # noqa: BLE001
        pass
    return n


def start(release_dir: str = "") -> None:
    """Called once by the engine's main block: the report thread (when ON) and the test aid (when NO_READAHEAD)."""
    if NO_READAHEAD and release_dir:
        def keep_off():
            while True:
                _readahead_off(release_dir)
                time.sleep(0.5)
        threading.Thread(target=keep_off, daemon=True, name="no-readahead").start()
        print("start trace: TEST AID on: no readahead for the release files (like Windows page faults)", flush=True)
    if not ON:
        return

    def run():
        for label, at in (("30 s", 30), ("2 min", 120), ("5 min", 300)):
            time.sleep(max(0.0, at - (time.monotonic() - T0)))
            print(report(label, release_dir), flush=True)
    threading.Thread(target=run, daemon=True, name="start-trace").start()
    threading.Thread(target=_watchdog, daemon=True, name="trace-watchdog").start()
