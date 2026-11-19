"""An answer as parts, each stored under an exact key (phase 2, 2026-11-15; design: docs/internal/precompute-plan.md).

A part is the output of one stage of the answer. Its key is a hash of everything the stage's output is a function of:

    key = sha256(format, stage, the stage's code version, the values of the settings it reads, the values of the
                 per-request state (ContextVars) it reads, the identity of the data files it reads, its inputs)  [the data enters through the settings that point at it]

  * code version: stage_version.Module.version(roots): the source of every top-level definition the stage's entry
    functions reach in search_api.py, transitively (no hand-bumped numbers);
  * settings and ContextVars: derived from the same reach (every os.environ name and ContextVar a reached statement
    reads), never from a hand-kept list; the stage's declared list must equal the derived one (the test fails when
    code the stage reaches starts reading a setting nobody declared);
  * data: the sha256 of every file or folder a reached setting points at (a release folder counts as its manifest);
  * inputs: the content of the stage's inputs, hashed (a stage whose input came out identical after an upstream change
    keeps its part: early cutoff for free).

A part's body is stored as JSON only when it survives a JSON round trip unchanged (tuples, sets or NaN would not), so a
part read back is the value the stage computed, not a look-alike. SEARCH_PARTS=card,... turns stages on (default off);
only the builder (warm=1) writes. Any failure here falls back to computing the stage live: a part is an optimisation,
never an input the answer cannot do without. Stored in the result store's SQLite file, table parts."""
from __future__ import annotations

import hashlib
import json
import os
import threading
import start_trace

from stage_version import Module

FORMAT = 1                                    # the key's layout; a change retires every part
# ContextVars that only observe or steer the request (timing, tracing, which store a row came from), never the output
INSTRUMENTATION = frozenset(("_stages", "_stages_cpu", "_trace", "_trace_n", "_trace_more", "_warm", "_audit", "_pcheck", "_part_keys",
                             "_store_note", "_prev_served", "_cache_state", "_parts_off", "_prov", "_card_live",
                             "_in_pool"))      # _in_pool: whether this code runs on a pool worker; it decides when a warm request waits, never what a stage returns


def _plain(x):
    if isinstance(x, (set, frozenset)):
        return sorted((_plain(v) for v in x), key=lambda v: json.dumps(v, sort_keys=True, default=repr))
    return repr(x)


def canon(obj) -> bytes:
    """Deterministic bytes of a JSON-like value: sorted keys, no whitespace, sets sorted, anything else by repr."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=_plain).encode()


def digest(obj) -> str:
    return hashlib.sha256(canon(obj)).hexdigest()[:32]


_IDENT: dict = {}
_MANIFESTS: dict = {}
DIR_HASH_MAX = 2 << 30                       # a manifest-less folder bigger than this is refused (the stage then runs live)


def _manifest_above(path: str):
    """(root, sha256 of its manifest.json, {relative path: bytes}) of the nearest folder above or at path that has a
    manifest.json (a release folder), or None."""
    d = path if os.path.isdir(path) else os.path.dirname(path)
    while True:
        m = os.path.join(d, "manifest.json")
        if os.path.exists(m):
            if m not in _MANIFESTS:
                raw = open(m, "rb").read()
                try:
                    listed = {e["path"]: e.get("bytes") for e in json.loads(raw).get("files", []) if isinstance(e, dict) and "path" in e}
                except (ValueError, AttributeError):
                    listed = {}
                _MANIFESTS[m] = (d, hashlib.sha256(raw).hexdigest()[:32], listed)
            return _MANIFESTS[m]
        up = os.path.dirname(d)
        if up == d:
            return None
        d = up


def _file_sha(p: str) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def data_identity(path: str) -> str:
    """What a data path holds, so a key follows the data and not where it is mounted:
      * a folder with its own manifest.json (a release): that manifest's sha256 (it lists every file's hash; the engine's
        fingerprint relies on the same);
      * a file or folder inside such a folder, every file listed there with its size: the manifest's sha256 and the
        relative path;
      * anything else: the sha256 of the content (a folder: of its files' relative names and content hashes; refused
        above DIR_HASH_MAX bytes).
    Computed once per process (again when a size or mtime moved)."""
    path = os.path.abspath(path)
    try:
        st = os.stat(path)
    except OSError:
        return "missing"
    memo = (path, st.st_size, int(st.st_mtime))
    if memo in _IDENT:
        return _IDENT[memo]
    out = None
    if os.path.isdir(path) and os.path.exists(os.path.join(path, "manifest.json")):
        out = "m:" + _manifest_above(path)[1]
    else:
        man = _manifest_above(path)
        if man:
            root, mh, listed = man
            rel = os.path.relpath(path, root)
            if os.path.isfile(path):
                files = [(rel, st.st_size)]
            else:
                files = [(os.path.relpath(os.path.join(d, f), root), os.stat(os.path.join(d, f)).st_size)
                         for d, _ds, fs in os.walk(path) for f in fs]
            if files and all(listed.get(r) == n for r, n in files):
                out = f"m:{mh}:{rel}"
    if out is None:
        h = hashlib.sha256()
        if os.path.isdir(path):
            total = 0
            for d, ds, fs in os.walk(path):
                ds.sort()
                for f in sorted(fs):
                    fp = os.path.join(d, f)
                    total += os.stat(fp).st_size
                    if total > DIR_HASH_MAX:
                        raise ValueError(f"{path}: a folder with no manifest and more than {DIR_HASH_MAX} bytes")
                    h.update(f"{os.path.relpath(fp, path)}\0{_file_sha(fp)}\0".encode())
        else:
            h.update(_file_sha(path).encode())
        out = "c:" + h.hexdigest()[:32]
    _IDENT[memo] = out
    return out


class Stage:
    """name; roots: the entry functions in the module (what the stage's code is); settings: the environment variables
    it declares it reads (must equal what the reach derives, less not_results); not_results: settings reached that only
    change speed or capacity (cache sizes, timeouts), named so the key ignores them; clock_ok: reached functions that
    read the clock for cache expiry only."""

    def __init__(self, name, roots=(), settings=(), not_results=(), clock_ok=(), unseen_env_ok=(), runtime=None,
                 method=None, stop=()):
        self.method, self.stop = method, (stop if stop == "*" else tuple(stop))        # method=(class, name): the stage is a handler method's own code; stop: other stages' entry points, not entered
        self.runtime = runtime                    # callable -> JSON-like facts only known at run time (the device a model really runs on)
        self.name, self.roots = name, tuple(roots)
        self.settings, self.not_results = tuple(sorted(settings)), frozenset(not_results)
        self.clock_ok, self.unseen_env_ok = frozenset(clock_ok), frozenset(unseen_env_ok)


def _aliases() -> dict:
    try:
        import stage_aliases
        return stage_aliases.ALIASES
    except ImportError:                       # an engine folder without the table: every version stands as computed
        return {}


class Parts:
    def __init__(self, store, module_path: str, stages, ctxvars: dict, env=None):
        self.store, self.mod, self.ctx = store, Module(module_path), ctxvars
        self.stages = {s.name: s for s in stages}
        self.env = os.environ if env is None else env
        self.lock = start_trace.lock("parts")
        self._info: dict = {}
        self.stats = {"hit": 0, "miss": 0, "put": 0, "unstorable": 0, "error": 0}
        self.by: dict = {}                       # the same per stage: {"card": {"hit": n, "miss": n, "put": n}}

    def info(self, stage: str) -> dict:
        """The stage's code version and what it reads, derived once from the source."""
        with self.lock:
            if stage not in self._info:
                s = self.stages[stage]
                roots = list(s.roots) + (self.mod.roots_of(*s.method) if s.method else [])
                env_names = [n for n in self.mod.env_reads(roots, s.stop) if n not in s.not_results]
                real = self.mod.version(roots, settings=env_names, stop=s.stop)
                self._info[stage] = {
                    "version": _aliases().get(stage, {}).get(real, real),          # a start-up-only change declared answer-identical (stage_aliases.py)
                    "real": real,
                    "env": env_names,
                    "ctx": [n for n in self.mod.contextvars_read(roots, s.stop) if n not in INSTRUMENTATION],
                    "clock": [n for n in self.mod.clock_reads(roots, s.stop) if n not in s.clock_ok],
                    "unseen": [n for n in self.mod.unseen_env(roots, s.stop) if n not in s.unseen_env_ok],
                    "roots": roots,
                }
            return self._info[stage]

    def key(self, stage: str, inputs) -> str:
        i = self.info(stage)
        env = {n: self.env.get(n) for n in i["env"]}
        ctx = {n: self.ctx[n].get() for n in i["ctx"] if n in self.ctx}
        # a setting that points at data is keyed by what the data holds, not by where it is (machines mount differently)
        vals = {n: (["data", data_identity(v)] if v and os.path.exists(v) else v) for n, v in env.items()}
        rt = self.stages[stage].runtime
        return digest([FORMAT, stage, i["version"], vals, ctx, rt() if rt else None, inputs])

    def get(self, stage: str, key: str):
        try:
            body = self.store.get_part(stage, key)
        except Exception:  # noqa: BLE001 - a part is extra: compute it live
            self.stats["error"] += 1
            return None
        self.stats["hit" if body is not None else "miss"] += 1
        c = self.by.setdefault(stage, {"hit": 0, "miss": 0, "put": 0})
        c["hit" if body is not None else "miss"] += 1
        return body

    def put(self, stage: str, key: str, body) -> bool:
        """Keep a part; refused when the body would not read back as itself."""
        try:
            try:
                raw = json.dumps(body, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
            except (ValueError, TypeError):                      # NaN, or a value JSON cannot hold
                self.stats["unstorable"] += 1
                return False
            if json.loads(raw) != body:
                self.stats["unstorable"] += 1
                return False
            self.store.put_part(stage, key, digest(body), raw.encode())
            self.stats["put"] += 1
            self.by.setdefault(stage, {"hit": 0, "miss": 0, "put": 0})["put"] += 1
            return True
        except Exception:  # noqa: BLE001
            self.stats["error"] += 1
            return False
