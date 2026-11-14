#!/usr/bin/env python3
"""The search build pipeline runner: files in, files out, every stage cached by what went into it.

A pipeline is a TOML file (release.toml) of STAGES. A stage is a command that reads named INPUTS and
writes files into an output folder. Nothing else: no shared database, no state outside the work folder.

    inputs   source:NAME        an immutable fetched file (sources.py; pinned by sha256 in the pipeline)
             stage:NAME/path    a file or folder another stage wrote (its manifest's sha256 is the input's)
             file:/abs/path     a file or folder outside the work folder (hashed, cached by size+mtime)
             opaque:LABEL       something that cannot be hashed (a database copy): the label is the version
    key      sha256 of the stage's name, command, the params it uses, the code files it declares, every
             input's hash, its bridge label and the container image id. Same key, same bytes: skip.
    output   WORK/stages/NAME/KEY/ with manifest.json (sha256 and bytes of every output file, timings,
             host, code commit); WORK/stages/NAME/current -> the key dir of the last selected build.
             A stage writes to KEY.partial and is renamed only after its outputs and `check` pass, so a
             failed stage never becomes current.
    bridge   kind = "bridge:postgres": a stage that still reads something the pipeline is getting rid of.
             The bridge's up/down commands run around it; `status` counts how many are left.

Commands (all take --work WORK, the per-machine build folder, and the pipeline file):
    plan     what would run and why (missing, input changed, code changed, params changed)
    run      run the stages that need it, in dependency order, one at a time; --to STAGE, --only STAGE,
             --force STAGE (rebuild even on a hit), --set k=v (a param for this run)
    status   every stage: current key, built when, where, hit or stale; the bridge count
    explain  one stage: the key's ingredients, so a surprise rebuild has a reason
    verify   re-hash a stage's current outputs against its manifest
    graph    the dependency graph as Mermaid
    sources  fetch / list / verify the pipeline's sources (sources.py)

Standard library only; Python 3.11+ (tomllib). Runs stages locally or in a container (docker/podman)
with the repo at /code (read-only), the work folder at /w and each file: input at /in/NAME (read-only).
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import platform
import re
import shlex
import shutil
import socket
import subprocess
import sys
import time
import tomllib

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
STAGES = "stages"
SOURCES = "sources"
RUNS = "runs"
HASHCACHE = ".hashes.json"
LOCK = ".lock"
KEY_LEN = 16
_PLACEHOLDER = re.compile(r"\{(code|work|out|in\.[A-Za-z0-9_-]+|param\.[A-Za-z0-9_-]+)\}")


class PipelineError(Exception):
    pass


def now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(*a) -> None:
    print(now(), *a, flush=True)


# ---- hashing -----------------------------------------------------------------------------------------
def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def walk_files(root: str) -> list[str]:
    """Relative paths of every regular file under root, sorted bytewise (a folder's identity)."""
    out = []
    for d, dirs, files in os.walk(root):
        dirs[:] = sorted(x for x in dirs if x != "__pycache__")
        for f in files:
            p = os.path.join(d, f)
            if os.path.isfile(p) and not f.endswith(".pyc"):
                out.append(os.path.relpath(p, root))
    return sorted(out)


class Hasher:
    """sha256 of files and folders, remembered by (realpath, size, mtime_ns) so a 19 GB file is read once."""

    def __init__(self, work: str):
        self.path = os.path.join(work, HASHCACHE)
        try:
            self.cache = json.load(open(self.path))
        except (OSError, ValueError):
            self.cache = {}
        self.dirty = False

    def file(self, path: str) -> tuple[str, int]:
        st = os.stat(path)
        k = os.path.realpath(path)
        c = self.cache.get(k)
        if c and c["size"] == st.st_size and c["mtime_ns"] == st.st_mtime_ns:
            return c["sha256"], st.st_size
        sha = sha256_file(path)
        self.cache[k] = {"size": st.st_size, "mtime_ns": st.st_mtime_ns, "sha256": sha}
        self.dirty = True
        return sha, st.st_size

    def tree(self, root: str) -> dict:
        """{'sha256': hash of the (relpath, sha256) list, 'bytes': total, 'files': {rel: {sha256, bytes}}}."""
        if os.path.isfile(root):
            sha, size = self.file(root)
            return {"sha256": sha, "bytes": size, "files": {".": {"sha256": sha, "bytes": size}}}
        files, total, lines = {}, 0, []
        for rel in walk_files(root):
            sha, size = self.file(os.path.join(root, rel))
            files[rel] = {"sha256": sha, "bytes": size}
            total += size
            lines.append(f"{sha}  {rel}\n")
        return {"sha256": hashlib.sha256("".join(lines).encode()).hexdigest(), "bytes": total, "files": files}

    def save(self) -> None:
        if self.dirty:
            tmp = self.path + ".tmp"
            json.dump(self.cache, open(tmp, "w"), sort_keys=True)
            os.replace(tmp, self.path)
            self.dirty = False


# ---- the pipeline file ---------------------------------------------------------------------------------
def load_pipeline(path: str, overrides: dict | None = None) -> dict:
    with open(path, "rb") as f:
        p = tomllib.load(f)
    p.setdefault("pipeline", {}).setdefault("name", os.path.splitext(os.path.basename(path))[0])
    p["pipeline"].setdefault("version", 1)
    p["pipeline"].setdefault("code", [])
    p.setdefault("params", {})
    p.setdefault("sources", {})
    p.setdefault("bridges", {})
    p.setdefault("runner", {})
    p["_path"] = os.path.abspath(path)
    repo = p["runner"].get("repo", REPO)
    p["_repo"] = repo if os.path.isabs(repo) else os.path.abspath(os.path.join(os.path.dirname(p["_path"]), repo))
    for k, v in (overrides or {}).items():
        if k not in p["params"]:
            raise PipelineError(f"--set {k}: no such param (params: {', '.join(sorted(p['params'])) or 'none'})")
        p["params"][k] = v
    stages = p.get("stages") or {}
    if not stages:
        raise PipelineError("the pipeline has no [stages.*]")
    for name, s in stages.items():
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", name):
            raise PipelineError(f"stage name {name!r}: letters, digits, - and _ only")
        if not isinstance(s.get("cmd"), list) or not s["cmd"]:
            raise PipelineError(f"stage {name}: cmd must be a non-empty list")
        s.setdefault("needs", [])
        s.setdefault("inputs", {})
        s.setdefault("outputs", [])
        s.setdefault("code", [])
        s.setdefault("kind", "pure")
        s.setdefault("check", None)
        s.setdefault("container", p["runner"].get("container", False))
        s.setdefault("memory", None)
        s.setdefault("env", {})
        s.setdefault("mounts", [])
        s.setdefault("options", [])
        s["inputs"] = {k: _params_in(p, name, v) for k, v in s["inputs"].items()}
        s["mounts"] = [_params_in(p, name, m) for m in s["mounts"]]
        s["options"] = [_params_in(p, name, o) for o in s["options"]]
        s["env"] = {k: _params_in(p, name, str(v)) for k, v in s["env"].items()}
        if not s["outputs"]:
            raise PipelineError(f"stage {name}: outputs must list at least one file or folder the command writes")
        kind = s["kind"]
        if kind != "pure" and not (kind.startswith("bridge:") and kind[7:] in p["bridges"]):
            raise PipelineError(f"stage {name}: kind {kind!r} is not 'pure' or 'bridge:<name>' with [bridges.<name>] declared")
        for n in s["needs"]:
            if n not in stages:
                raise PipelineError(f"stage {name} needs {n!r}: no such stage")
        for iname, ref in s["inputs"].items():
            if not re.fullmatch(r"[A-Za-z0-9_-]+", iname):
                raise PipelineError(f"stage {name}: input name {iname!r}")
            scheme, _, rest = ref.partition(":")
            if scheme == "source":
                if rest not in p["sources"]:
                    raise PipelineError(f"stage {name} input {iname}: source {rest!r} is not declared under [sources]")
            elif scheme == "stage":
                up = rest.split("/", 1)[0]
                if up not in stages:
                    raise PipelineError(f"stage {name} input {iname}: stage {up!r} does not exist")
                if up not in s["needs"]:
                    s["needs"].append(up)
            elif scheme not in ("file", "opaque"):
                raise PipelineError(f"stage {name} input {iname}: {ref!r} is not source:, stage:, file: or opaque:")
        for tok in s["cmd"] + (s["check"] or []):
            for m in _PLACEHOLDER.finditer(tok):
                ph = m.group(1)
                if ph.startswith("in.") and ph[3:] not in s["inputs"]:
                    raise PipelineError(f"stage {name}: {{{ph}}} in cmd but no input named {ph[3:]!r}")
                if ph.startswith("param.") and ph[6:] not in p["params"]:
                    raise PipelineError(f"stage {name}: {{{ph}}} in cmd but no param named {ph[6:]!r}")
    return p


def _params_in(p: dict, stage: str, text: str) -> str:
    """{param.NAME} inside an input ref or a mount (paths that change per release, e.g. the previous release's folder)."""
    def repl(m):
        ph = m.group(1)
        if not ph.startswith("param."):
            raise PipelineError(f"stage {stage}: only {{param.*}} may appear in an input or mount, not {{{ph}}}")
        if ph[6:] not in p["params"]:
            raise PipelineError(f"stage {stage}: {{{ph}}} but no param named {ph[6:]!r}")
        return str(p["params"][ph[6:]])
    return _PLACEHOLDER.sub(repl, text)


def topo(p: dict, to: list[str] | None = None) -> list[str]:
    stages = p["stages"]
    order, seen, path = [], set(), []

    def visit(n):
        if n in seen:
            return
        if n in path:
            raise PipelineError("dependency cycle: " + " -> ".join(path + [n]))
        path.append(n)
        for d in stages[n]["needs"]:
            visit(d)
        path.pop()
        seen.add(n)
        order.append(n)

    for n in (to or list(stages)):
        if n not in stages:
            raise PipelineError(f"no such stage: {n}")
        visit(n)
    return order


def params_used(p: dict, s: dict) -> dict:
    used = {}
    for tok in s["cmd"] + (s["check"] or []):
        for m in _PLACEHOLDER.finditer(tok):
            if m.group(1).startswith("param."):
                k = m.group(1)[6:]
                used[k] = p["params"][k]
    return used


# ---- the work folder -----------------------------------------------------------------------------------
class Work:
    def __init__(self, root: str):
        self.root = os.path.abspath(root)
        for d in (STAGES, SOURCES, RUNS):
            os.makedirs(os.path.join(self.root, d), exist_ok=True)
        self.hasher = Hasher(self.root)

    def stage_dir(self, name: str) -> str:
        return os.path.join(self.root, STAGES, name)

    def key_dir(self, name: str, key: str) -> str:
        return os.path.join(self.stage_dir(name), key[:KEY_LEN])

    def current(self, name: str) -> dict | None:
        """The manifest the `current` link points at, or None."""
        link = os.path.join(self.stage_dir(name), "current")
        try:
            return json.load(open(os.path.join(link, "manifest.json")))
        except (OSError, ValueError):
            return None

    def manifest(self, name: str, key: str) -> dict | None:
        try:
            return json.load(open(os.path.join(self.key_dir(name, key), "manifest.json")))
        except (OSError, ValueError):
            return None

    def set_current(self, name: str, key: str) -> None:
        link = os.path.join(self.stage_dir(name), "current")
        tmp = link + ".tmp"
        if os.path.lexists(tmp):
            os.unlink(tmp)
        os.symlink(key[:KEY_LEN], tmp)
        os.replace(tmp, link)

    def source_path(self, p: dict, name: str) -> str:
        """The pinned fetched file of a source, or a PipelineError saying how to fetch it."""
        src = p["sources"][name]
        sha = src.get("sha256")
        if not sha:
            raise PipelineError(f"source {name}: no sha256 pin in the pipeline file. Run: pipeline.py sources fetch {name}  (it prints the pin)")
        d = os.path.join(self.root, SOURCES, name, sha[:KEY_LEN])
        data = os.path.join(d, "data")
        if not os.path.exists(data):
            raise PipelineError(f"source {name} ({sha[:12]}) is not in {self.root}/{SOURCES}: run  pipeline.py sources fetch {name}")
        return data

    def lock(self):
        return _Lock(os.path.join(self.root, LOCK))


class _Lock:
    def __init__(self, path):
        self.path = path

    def __enter__(self):
        for _ in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
                os.write(fd, f"{os.getpid()} {socket.gethostname()} {now()}\n".encode())
                os.close(fd)
                return self
            except FileExistsError:
                try:
                    pid, host, when = open(self.path).read().split()[:3]
                except (OSError, ValueError):
                    pid, host, when = "?", "?", "?"
                alive = host == socket.gethostname() and pid.isdigit() and _pid_alive(int(pid))
                if alive or host != socket.gethostname():
                    raise PipelineError(f"another run holds {self.path} (pid {pid} on {host} since {when}); one heavy job at a time")
                os.unlink(self.path)                      # ours, dead: stale
        raise PipelineError(f"could not take {self.path}")

    def __exit__(self, *exc):
        try:
            os.unlink(self.path)
        except OSError:
            pass


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


# ---- resolving a stage: inputs, key, command -----------------------------------------------------------
def git_commit(repo: str) -> str | None:
    try:
        return subprocess.run(["git", "-C", repo, "rev-parse", "HEAD"], capture_output=True, text=True, timeout=20).stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def code_hash(hasher: Hasher, repo: str, path: str) -> str:
    """A code file's sha256; a code folder's hash over its git-tracked files (so a build's target/ or a stray local
    file does not change every key), falling back to every file when the repo is not a git checkout."""
    if os.path.isfile(path):
        return hasher.file(path)[0]
    try:
        r = subprocess.run(["git", "-C", repo, "ls-files", "-z", "--", path], capture_output=True, timeout=30)
        names = [n for n in r.stdout.decode().split("\0") if n] if r.returncode == 0 else []
    except (OSError, subprocess.SubprocessError):
        names = []
    if not names:
        return hasher.tree(path)["sha256"]
    lines = []
    for n in sorted(names):
        full = os.path.join(repo, n)
        if os.path.isfile(full):
            lines.append(f"{hasher.file(full)[0]}  {os.path.relpath(full, path)}\n")
    return hashlib.sha256("".join(lines).encode()).hexdigest()


def image_id(engine: str, image: str) -> str | None:
    try:
        r = subprocess.run([*shlex.split(engine), "image", "inspect", "--format", "{{.Id}}", image], capture_output=True, text=True, timeout=60)
        return r.stdout.strip() or None if r.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


class Resolved:
    """A stage with its inputs located and hashed, its key computed, and its command ready to run."""

    def __init__(self, p: dict, w: Work, name: str, keys: dict[str, str]):
        s = p["stages"][name]
        self.p, self.w, self.name, self.s = p, w, name, s
        self.inputs: dict[str, dict] = {}            # name -> {ref, path, sha256, bytes}
        self.missing: list[str] = []
        for iname, ref in s["inputs"].items():
            scheme, _, rest = ref.partition(":")
            if scheme == "source":
                try:
                    path = w.source_path(p, rest)
                except PipelineError as e:
                    self.missing.append(str(e))
                    self.inputs[iname] = {"ref": ref, "path": None, "sha256": p["sources"][rest].get("sha256"), "bytes": None}
                    continue
                self.inputs[iname] = {"ref": ref, "path": path, "sha256": p["sources"][rest]["sha256"], "bytes": os.path.getsize(path)}
            elif scheme == "stage":
                up, _, sub = rest.partition("/")
                key = keys.get(up)
                man = w.manifest(up, key) if key else None
                base = w.key_dir(up, key) if key else None
                path = os.path.join(base, sub) if base and sub else base
                entry = (man or {}).get("outputs", {}).get(sub or ".") if man else None
                if entry is None and man is not None:
                    # a file inside a folder output: look it up in that folder's file list
                    for oname, o in man["outputs"].items():
                        if sub.startswith(oname + "/") and sub[len(oname) + 1:] in o.get("files", {}):
                            entry = o["files"][sub[len(oname) + 1:]]
                            break
                if entry is None:
                    self.missing.append(f"input {iname} = {ref}: stage {up} has no output {sub!r}" if man else f"input {iname} = {ref}: stage {up} is not built")
                    self.inputs[iname] = {"ref": ref, "path": path, "sha256": None, "bytes": None}
                    continue
                self.inputs[iname] = {"ref": ref, "path": path, "sha256": entry["sha256"], "bytes": entry["bytes"]}
            elif scheme == "file":
                path = rest if os.path.isabs(rest) else os.path.join(os.path.dirname(p["_path"]), rest)
                if not os.path.exists(path):
                    self.missing.append(f"input {iname} = {ref}: no such file or folder")
                    self.inputs[iname] = {"ref": ref, "path": path, "sha256": None, "bytes": None}
                    continue
                t = w.hasher.tree(path)
                self.inputs[iname] = {"ref": ref, "path": path, "sha256": t["sha256"], "bytes": t["bytes"]}
            else:                                      # opaque: the label is the version
                self.inputs[iname] = {"ref": ref, "path": None, "sha256": "opaque:" + rest, "bytes": None}
        self.params = params_used(p, s)
        self.code: dict[str, str] = {}
        for rel in list(p["pipeline"]["code"]) + list(s["code"]):
            path = os.path.join(p["_repo"], rel)
            if not os.path.exists(path):
                self.missing.append(f"code {rel}: not in {p['_repo']}")
                continue
            self.code[rel] = code_hash(w.hasher, p["_repo"], path)
        self.engine = p["runner"].get("engine", "docker")
        self.image = p["runner"].get("image") if s["container"] else None
        self.image_id = image_id(self.engine, self.image) if self.image else None
        if self.image and not self.image_id:
            self.missing.append(f"container image {self.image} is not present ({self.engine})")
        # the key names the image by its tag (default), so a work folder copied to another machine with the same
        # tag keeps its keys; image_key = "id" keys on the local image id instead (strict, one machine)
        self.image_key = (self.image_id if p["runner"].get("image_key", "name") == "id" else self.image) if self.image else None
        self.bridge = s["kind"][7:] if s["kind"].startswith("bridge:") else None
        ingredients = {
            "pipeline": {"name": p["pipeline"]["name"], "version": p["pipeline"]["version"]},
            "stage": name, "cmd": s["cmd"], "check": s["check"], "outputs": s["outputs"],
            "params": self.params, "code": self.code,
            "inputs": {k: v["sha256"] for k, v in self.inputs.items()},
            "bridge": self.bridge, "image": self.image_key,
        }
        self.ingredients = ingredients
        self.key = hashlib.sha256(json.dumps(ingredients, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    # why it would run
    def reason(self) -> str | None:
        man = self.w.manifest(self.name, self.key)
        if man:
            return None
        cur = self.w.current(self.name)
        if not cur:
            return "never built"
        old = cur.get("ingredients", {})
        why = []
        if old.get("inputs") != self.ingredients["inputs"]:
            why.append("inputs changed: " + ", ".join(sorted(k for k in set(old.get("inputs", {})) | set(self.ingredients["inputs"]) if old.get("inputs", {}).get(k) != self.ingredients["inputs"].get(k))))
        if old.get("code") != self.ingredients["code"]:
            why.append("code changed: " + ", ".join(sorted(k for k in set(old.get("code", {})) | set(self.code) if old.get("code", {}).get(k) != self.code.get(k))))
        if old.get("params") != self.params:
            why.append("params changed: " + ", ".join(sorted(k for k in set(old.get("params", {})) | set(self.params) if old.get("params", {}).get(k) != self.params.get(k))))
        for k in ("cmd", "check", "outputs", "bridge", "image"):
            if old.get(k) != self.ingredients[k]:
                why.append(k + " changed")
        return "; ".join(why) or "key changed"

    # the command, with paths for where it runs
    def substitute(self, tokens: list[str], out_dir: str) -> tuple[list[str], list[str]]:
        """-> (argv, extra container mounts). Inside a container the work folder is /w, the repo /code,
        a file: input outside the work folder /in/NAME."""
        container = bool(self.image)
        mounts = []

        def map_path(path: str, iname: str | None = None) -> str:
            if not container:
                return path
            real = os.path.realpath(path)
            w = os.path.realpath(self.w.root)
            if real == w or real.startswith(w + os.sep):
                return "/w" + real[len(w):]
            if iname is not None:
                mounts.append(f"{real}:/in/{iname}:ro")
                return f"/in/{iname}"
            raise PipelineError(f"{self.name}: {path} is outside the work folder and is not a named input")

        def repl(m):
            ph = m.group(1)
            if ph == "code":
                return "/code" if container else self.p["_repo"]
            if ph == "work":
                return "/w" if container else self.w.root
            if ph == "out":
                return map_path(out_dir)
            if ph.startswith("param."):
                return str(self.p["params"][ph[6:]])
            iname = ph[3:]
            i = self.inputs[iname]
            if i["path"] is None:
                raise PipelineError(f"{self.name}: {{in.{iname}}} is an opaque input; it has no path")
            return map_path(i["path"], iname)

        return [_PLACEHOLDER.sub(repl, t) for t in tokens], mounts

    def argv(self, tokens: list[str], out_dir: str) -> list[str]:
        cmd, mounts = self.substitute(tokens, out_dir)
        if not self.image:
            return cmd
        r = self.p["runner"]
        a = [*shlex.split(self.engine), "run", "--rm", "--user", str(r.get("user", "0")), "--entrypoint", cmd[0]]
        a += ["-v", f"{self.p['_repo']}:/code:ro", "-v", f"{self.w.root}:/w"]
        for m in list(r.get("mounts", [])) + list(self.s["mounts"]) + mounts:
            a += ["-v", m]
        env = {**r.get("env", {}), **self.s["env"]}
        if self.bridge:   # the bridge's connection env reaches the container too, not only the host hooks
            env.update(self.p["bridges"][self.bridge].get("env", {}))
        for k, v in env.items():
            a += ["-e", f"{k}={v}"]
        if self.s["memory"]:
            a += ["--memory", str(self.s["memory"])]
        for opt in list(r.get("options", [])) + list(self.s["options"]):
            a.append(opt)
        a += [self.image, *cmd[1:]]
        return a


# ---- running -------------------------------------------------------------------------------------------
def run_cmd(argv: list[str], env: dict, logf, cwd: str | None = None) -> int:
    logf.write("+ " + shlex.join(argv) + "\n")
    logf.flush()
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env, cwd=cwd, text=True, errors="replace")
    for line in proc.stdout:
        logf.write(line)
        logf.flush()
        sys.stdout.write("    " + line)
        sys.stdout.flush()
    return proc.wait()


def bridge_hook(p: dict, name: str, which: str, env: dict, logf) -> None:
    b = p["bridges"][name]
    cmd = b.get(which)
    if not cmd:
        return
    log(f"bridge {name}: {which}")
    rc = run_cmd(cmd, env, logf, cwd=p["_repo"])
    if rc != 0 and which == "up":
        raise PipelineError(f"bridge {name} {which} failed (exit {rc})")


def build_stage(p: dict, w: Work, r: Resolved, runlog) -> dict:
    s = r.s
    final = w.key_dir(r.name, r.key)
    partial = final + ".partial"
    if os.path.exists(partial):
        shutil.rmtree(partial)
    os.makedirs(partial)
    env = dict(os.environ)
    env.update({k: str(v) for k, v in p["runner"].get("host_env", {}).items()})
    if r.bridge:
        env.update({k: str(v) for k, v in p["bridges"][r.bridge].get("env", {}).items()})
    started = now()
    t0 = time.monotonic()
    ok = False
    with open(os.path.join(partial, "build.log"), "w") as logf:
        logf.write(f"# stage {r.name} key {r.key}\n# started {started} on {socket.gethostname()}\n")
        try:
            if r.bridge:
                bridge_hook(p, r.bridge, "up", env, logf)
            rc = run_cmd(r.argv(s["cmd"], partial), env, logf, cwd=p["_repo"])
            if rc != 0:
                raise PipelineError(f"stage {r.name}: command exited {rc} (log: {partial}/build.log)")
            for o in s["outputs"]:
                if not os.path.exists(os.path.join(partial, o)):
                    raise PipelineError(f"stage {r.name}: declared output {o!r} was not written")
            if s["check"]:
                rc = run_cmd(r.argv(s["check"], partial), env, logf, cwd=p["_repo"])
                if rc != 0:
                    raise PipelineError(f"stage {r.name}: check exited {rc} (log: {partial}/build.log)")
            ok = True
        finally:
            if r.bridge:
                try:
                    bridge_hook(p, r.bridge, "down", env, logf)
                except PipelineError:
                    pass
            logf.write(f"# finished {now()} ok={ok}\n")
    if not ok:
        return {}
    outputs = {}
    for o in s["outputs"]:
        t = w.hasher.tree(os.path.join(partial, o))
        outputs[o] = {"sha256": t["sha256"], "bytes": t["bytes"]}
        if os.path.isdir(os.path.join(partial, o)):
            outputs[o]["files"] = t["files"]
    man = {
        "stage": r.name, "key": r.key, "kind": s["kind"], "pipeline": p["pipeline"]["name"],
        "ingredients": r.ingredients, "params": r.params,
        "inputs": {k: {kk: vv for kk, vv in v.items() if kk != "path"} | {"path": v["path"]} for k, v in r.inputs.items()},
        "outputs": outputs, "started": started, "finished": now(), "seconds": round(time.monotonic() - t0, 1),
        "host": socket.gethostname(), "platform": platform.platform(), "code_commit": git_commit(p["_repo"]),
        "image": r.image, "image_id": r.image_id,
    }
    json.dump(man, open(os.path.join(partial, "manifest.json"), "w"), indent=1, sort_keys=True)
    if os.path.exists(final):
        shutil.rmtree(final)
    os.replace(partial, final)
    w.set_current(r.name, r.key)
    w.hasher.save()
    runlog.write(json.dumps({"t": now(), "stage": r.name, "key": r.key, "seconds": man["seconds"], "ok": True}) + "\n")
    runlog.flush()
    return man


def resolve_all(p: dict, w: Work, order: list[str]) -> tuple[dict[str, Resolved], dict[str, str]]:
    keys: dict[str, str] = {}
    res: dict[str, Resolved] = {}
    for n in order:
        r = Resolved(p, w, n, keys)
        res[n] = r
        keys[n] = r.key
    w.hasher.save()
    return res, keys


def fmt_bytes(n) -> str:
    if n is None:
        return "-"
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or u == "TB":
            return f"{n:.0f}{u}" if u == "B" else f"{n:.1f}{u}"
        n /= 1024
    return str(n)


# ---- commands ------------------------------------------------------------------------------------------
def cmd_plan(p: dict, w: Work, args) -> int:
    order = topo(p, args.to)
    if args.only:
        order = [n for n in order if n in args.only]
    res, _ = resolve_all(p, w, topo(p, args.to))
    todo = 0
    for n in order:
        r = res[n]
        why = r.reason()
        if n in (args.force or []):
            why = (why + "; " if why else "") + "--force"
        state = "RUN " if why else "hit "
        if why:
            todo += 1
        bridge = f" [bridge:{r.bridge}]" if r.bridge else ""
        print(f"{state} {n:<18} {r.key[:KEY_LEN]}{bridge}" + (f"  {why}" if why else ""))
        for m in r.missing:
            print(f"      ! {m}")
    print(f"{todo} of {len(order)} stages would run; {sum(1 for r in res.values() if r.bridge)} bridge stages in the pipeline")
    return 0


def cmd_run(p: dict, w: Work, args) -> int:
    order = topo(p, args.to)
    sel = [n for n in order if not args.only or n in args.only]
    if args.only:
        for n in args.only:
            if n not in p["stages"]:
                raise PipelineError(f"--only {n}: no such stage")
    with w.lock(), open(os.path.join(w.root, RUNS, f"{now().replace(':', '')}.jsonl"), "a") as runlog:
        runlog.write(json.dumps({"t": now(), "run": p["pipeline"]["name"], "params": p["params"], "to": args.to, "only": args.only, "force": args.force, "host": socket.gethostname(), "code_commit": git_commit(p["_repo"])}) + "\n")
        keys: dict[str, str] = {}
        ran = hits = 0
        for n in order:
            r = Resolved(p, w, n, keys)
            keys[n] = r.key
            if n not in sel:
                if not w.manifest(n, r.key):
                    raise PipelineError(f"stage {n} is needed but not built for this key (run without --only, or include it)")
                continue
            why = r.reason()
            forced = n in (args.force or [])
            if not why and not forced:
                hits += 1
                log(f"hit  {n} {r.key[:KEY_LEN]}")
                continue
            if r.missing:
                raise PipelineError(f"stage {n} cannot run:\n  " + "\n  ".join(r.missing))
            log(f"run  {n} {r.key[:KEY_LEN]}  ({why or '--force'})" + (f" [bridge:{r.bridge}]" if r.bridge else ""))
            try:
                man = build_stage(p, w, r, runlog)
            except PipelineError as e:                 # a stage failed: say so, exit 1, nothing became current
                runlog.write(json.dumps({"t": now(), "stage": n, "key": r.key, "ok": False, "error": str(e)}) + "\n")
                log(f"FAIL {n}: {e}")
                return 1
            ran += 1
            outs = ", ".join(f"{o} {fmt_bytes(v['bytes'])}" for o, v in man["outputs"].items())
            log(f"done {n} in {man['seconds']}s: {outs}")
        log(f"{ran} built, {hits} hits; {sum(1 for n in order if p['stages'][n]['kind'] != 'pure')} bridge stages remain in the pipeline")
    return 0


def cmd_status(p: dict, w: Work, args) -> int:
    order = topo(p)
    res, _ = resolve_all(p, w, order)
    bridges = 0
    print(f"pipeline {p['pipeline']['name']} v{p['pipeline']['version']}  work {w.root}")
    print(f"params: " + ", ".join(f"{k}={v}" for k, v in p["params"].items()))
    for n in order:
        r = res[n]
        cur = w.current(n)
        if r.bridge:
            bridges += 1
        if not cur:
            state = "never built"
        elif cur["key"] == r.key:
            state = f"current  {cur['finished']} on {cur['host']} in {cur['seconds']}s"
        else:
            state = f"STALE    ({r.reason()}); last {cur['finished']} on {cur['host']}"
        print(f"{n:<18} {r.key[:KEY_LEN]}  {'bridge:' + r.bridge if r.bridge else 'pure':<16} {state}")
        for m in r.missing:
            print(f"      ! {m}")
    print(f"bridge stages left: {bridges} of {len(order)}  (the exit from Postgres is done at 0)")
    return 0


def cmd_explain(p: dict, w: Work, args) -> int:
    order = topo(p, [args.stage])
    res, _ = resolve_all(p, w, order)
    r = res[args.stage]
    out = {"stage": args.stage, "key": r.key, "ingredients": r.ingredients,
           "inputs": {k: {"ref": v["ref"], "path": v["path"], "sha256": v["sha256"], "bytes": v["bytes"]} for k, v in r.inputs.items()},
           "missing": r.missing, "reason": r.reason(), "current": (w.current(args.stage) or {}).get("key")}
    print(json.dumps(out, indent=1, sort_keys=True))
    return 0


def cmd_verify(p: dict, w: Work, args) -> int:
    bad = 0
    for n in (args.stages or topo(p)):
        cur = w.current(n)
        if not cur:
            print(f"{n}: never built")
            continue
        d = w.key_dir(n, cur["key"])
        for o, want in cur["outputs"].items():
            path = os.path.join(d, o)
            if not os.path.exists(path):
                print(f"{n}: {o} missing")
                bad += 1
                continue
            got = w.hasher.tree(path)["sha256"] if os.path.isdir(path) else sha256_file(path)
            if got != want["sha256"]:
                print(f"{n}: {o} CHANGED since it was built ({got[:12]} != {want['sha256'][:12]})")
                bad += 1
            else:
                print(f"{n}: {o} ok")
    return 1 if bad else 0


def cmd_graph(p: dict, w: Work, args) -> int:
    print("flowchart LR")
    for n, s in p["stages"].items():
        label = n + (" (bridge)" if s["kind"] != "pure" else "")
        print(f"  {n.replace('-', '_')}[\"{label}\"]")
        for iname, ref in s["inputs"].items():
            if ref.startswith("source:"):
                src = ref[7:]
                print(f"  src_{src.replace('-', '_')}[(\"source {src}\")] --> {n.replace('-', '_')}")
        for d in s["needs"]:
            print(f"  {d.replace('-', '_')} --> {n.replace('-', '_')}")
    return 0


def cmd_sources(p: dict, w: Work, args) -> int:
    sys.path.insert(0, HERE)
    import sources as srcmod
    return srcmod.cli(p, os.path.join(w.root, SOURCES), args.sub, args.names)


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("pipeline", help="the pipeline TOML file")
    ap.add_argument("--work", required=True, help="the build folder on this machine (stages/, sources/, runs/)")
    ap.add_argument("--set", action="append", default=[], metavar="K=V", help="a param for this run")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for c in ("plan", "run"):
        sp = sub.add_parser(c)
        sp.add_argument("--to", action="append", help="stop after this stage (and build what it needs)")
        sp.add_argument("--only", action="append", help="this stage alone; its inputs must already be built")
        sp.add_argument("--force", action="append", help="rebuild this stage even on a cache hit")
    sub.add_parser("status")
    sp = sub.add_parser("explain"); sp.add_argument("stage")
    sp = sub.add_parser("verify"); sp.add_argument("stages", nargs="*")
    sub.add_parser("graph")
    sp = sub.add_parser("sources"); sp.add_argument("sub", choices=["fetch", "list", "verify"]); sp.add_argument("names", nargs="*")
    args = ap.parse_args(argv)
    overrides = {}
    for kv in args.set:
        k, eq, v = kv.partition("=")
        if not eq:
            ap.error(f"--set {kv}: expected K=V")
        overrides[k] = v
    try:
        p = load_pipeline(args.pipeline, overrides)
        w = Work(args.work)
        return {"plan": cmd_plan, "run": cmd_run, "status": cmd_status, "explain": cmd_explain,
                "verify": cmd_verify, "graph": cmd_graph, "sources": cmd_sources}[args.cmd](p, w, args)
    except PipelineError as e:
        print(f"pipeline: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
