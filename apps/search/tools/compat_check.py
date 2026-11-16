#!/usr/bin/env python3
"""Does a candidate answers publish work on the installer versions customers have? (the promise)

    compat_check.py --candidate OUT --pubkey ed25519:HEX [--last 3 | --versions v0.4.23,v0.4.22,v0.4.21]
                    [--source-dir NAME=TREE ...] [--work DIR] [--cache DIR] [--sample 50]
    compat_check.py ... --engine --base-from BASE      (also start each version's real program; see below)

OUT is what `lists_build.py build --out OUT` wrote (OUT/lists/manifest.json, its signature and the parts), before `upload`. For
each of the last three released versions (GitHub's release list; or the ones named) the tool fetches that version's source
(the release's `logout-search-vX.tar.gz`, about 3 MB, cached) and runs it in a scratch folder with its own code
(compat_worker.py): its `lists update` against the candidate, its engine's inbox intake of what that dropped, and a sample of
the candidate's answers read back from the store. A version passes when the manifest is accepted, nothing is refused or renamed
*.bad, and every sampled answer comes back identical. Exit 0 when every version passes, 1 when one fails, 2 on a usage error.

--engine adds the real thing for each version: its program from the release (`logout-search-linux-x86_64.tar.gz`, about 96 MB,
checked against SHA256SUMS) is unpacked, the candidate is taken in by it, its engine is started on BASE's release (a read-only
symlink; BASE is an install made by `install.py`), and /explain says "finished" for
the sampled names. One engine at a time, niced, on its own port, no peer, no collectors, no updates; stopped by its exact PID.
It needs a release's worth of RAM and minutes per version: run it on a machine with spare memory, not a busy one.

How lists-publish calls it (not wired into the live publisher; the main session does that): after `lists_build.py build` and
before `upload`:

    python3 apps/search/tools/compat_check.py --candidate "$OUT" --pubkey "$PUBKEY" --last 3 || { alert; exit 1; }

Standard library only; a failure prints which version failed and why.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request

REPO = "m8software/logout-search"
API = f"https://api.github.com/repos/{REPO}/releases?per_page=100"
UA = "logout-search-compat-check"
HERE = os.path.dirname(os.path.abspath(__file__))


def get(url: str, timeout: float = 120):
    return urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/octet-stream, application/json"}),
                                  timeout=timeout)


def vkey(tag: str):
    m = re.fullmatch(r"v?(\d+)\.(\d+)(?:\.(\d+))?", tag or "")
    return tuple(int(x or 0) for x in m.groups()) if m else None


def releases(fetch=None) -> list:
    if fetch:
        return fetch()
    with get(API, 30) as r:
        return json.load(r)


def last_versions(n: int, fetch=None) -> list:
    """The n newest released versions: [{"tag", "assets": {name: url}}], newest first (drafts and pre-releases left out)."""
    rel = [r for r in releases(fetch) if not r.get("draft") and not r.get("prerelease") and vkey(r.get("tag_name"))]
    rel.sort(key=lambda r: vkey(r["tag_name"]), reverse=True)
    return [{"tag": r["tag_name"], "assets": {a["name"]: a["browser_download_url"] for a in r.get("assets") or []}} for r in rel[:n]]


def download(url: str, dest: str, sha256: str | None = None) -> str:
    if os.path.isfile(dest) and (sha256 is None or hashlib.sha256(open(dest, "rb").read()).hexdigest() == sha256):
        return dest
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    h = hashlib.sha256()
    with get(url, 600) as r, open(dest + ".part", "wb") as f:
        for b in iter(lambda: r.read(1 << 20), b""):
            h.update(b)
            f.write(b)
    if sha256 and h.hexdigest() != sha256:
        os.remove(dest + ".part")
        raise RuntimeError(f"{os.path.basename(dest)}: SHA-256 is not the published one")
    os.replace(dest + ".part", dest)
    return dest


def unpack(archive: str, dest: str) -> str:
    """Unpack a tarball (every member inside dest) and return its single top folder."""
    shutil.rmtree(dest, ignore_errors=True)
    os.makedirs(dest)
    with tarfile.open(archive) as t:
        for m in t.getmembers():
            p = os.path.normpath(os.path.join(dest, m.name))
            if not p.startswith(os.path.abspath(dest) + os.sep) or m.islnk() or m.issym() or m.isdev():
                continue
            t.extract(m, dest, filter="data") if sys.version_info >= (3, 12) else t.extract(m, dest)
    tops = [d for d in os.listdir(dest) if os.path.isdir(os.path.join(dest, d))]
    return os.path.join(dest, tops[0]) if len(tops) == 1 else dest


def release_id_for(candidate: str) -> str:
    m = json.load(open(os.path.join(candidate, "lists", "manifest.json"), encoding="utf-8"))
    return m["fits_releases"][0].replace("*", "compat").replace("?", "x")


def source_of(version: dict, cache: str) -> str:
    asset = f"logout-search-{version['tag']}.tar.gz"
    if asset not in version["assets"]:
        raise RuntimeError(f"release {version['tag']} has no {asset}")
    return unpack(download(version["assets"][asset], os.path.join(cache, asset)), os.path.join(cache, "src-" + version["tag"]))


def check_version(name: str, tree: str, candidate: str, pubkey: str, work: str, sample: int, rid: str, runner=subprocess.run) -> dict:
    w = os.path.join(work, name)
    shutil.rmtree(w, ignore_errors=True)
    os.makedirs(w)
    t0 = time.time()
    r = runner([sys.executable, "-I", os.path.join(HERE, "compat_worker.py"), "--src", tree, "--candidate", candidate, "--work", w,
                "--pubkey", pubkey, "--release-id", rid, "--sample", str(sample)], capture_output=True, text=True, cwd=w, timeout=3600)
    try:
        res = json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        res = {"ok": False, "problems": [f"the check itself failed (exit {r.returncode}): {(r.stderr or r.stdout).strip()[-400:]}"]}
    res["version"], res["seconds"] = name, round(time.time() - t0, 1)
    return res


# ================================================================================================ --engine
def free_port() -> int:
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def check_engine(name: str, version: dict, candidate: str, pubkey: str, work: str, base_from: str, rid_src: str, cache: str,
                 sample: int, ready_timeout: float = 1800.0) -> dict:
    """The version's real program: unpack, `lists update`, start the engine, ask /explain for sampled names, stop it."""
    sys.path.insert(0, os.path.join(os.path.dirname(HERE), "installer"))
    import selftest                                          # this checkout's: it only talks HTTP to the engine
    res = {"version": name, "engine": True, "ok": False, "problems": []}
    pr = res["problems"]
    sums = {}
    with get(version["assets"]["SHA256SUMS"], 60) as r:
        for ln in r.read().decode().splitlines():
            p = ln.split()
            if len(p) == 2:
                sums[p[1].lstrip("*")] = p[0]
    asset = "logout-search-linux-x86_64.tar.gz"
    app = unpack(download(version["assets"][asset], os.path.join(cache, f"{version['tag']}-{asset}"), sums.get(asset)),
                 os.path.join(work, f"app-{name}"))
    exe = os.path.join(app, "logout-search")
    data = os.path.join(work, f"engine-{name}", "data")
    os.makedirs(os.path.join(data, "releases"), exist_ok=True)
    real = os.path.realpath(os.path.join(base_from, "current"))
    os.symlink(real, os.path.join(data, "releases", os.path.basename(real)))             # the release itself is shared, read-only
    sys.path.insert(0, os.path.join(os.path.dirname(HERE), "release"))
    import release_ptr
    release_ptr.set_pointer(data, "current", os.path.basename(real))
    env = dict(os.environ, LOGOUT_AUTOUPDATE="0", SEARCH_STORE_INBOX_EVERY_S="5", LOGOUT_SEARCH_PIN="")
    nice = ["nice", "-n", "10"]
    subprocess.run(nice + [exe, "collectors", "set", "profile", "search", "--dest", data], env=env, capture_output=True, timeout=120)
    lu = subprocess.run(nice + [exe, "lists", "update", "--dest", data, "--source", candidate, "--pubkey", pubkey], env=env,
                        capture_output=True, text=True, timeout=3600)
    if lu.returncode:
        pr.append(f"`lists update` of this program: exit {lu.returncode}: {(lu.stderr or lu.stdout).strip()[-300:]}")
        return res
    port = free_port()
    log = open(os.path.join(work, f"engine-{name}.log"), "w")
    proc = subprocess.Popen(nice + [exe, "start", "--dest", data, "--port", str(port), "--no-peer", "--no-smoke"], env=env,
                            stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    try:
        t0 = time.time()
        while time.time() - t0 < ready_timeout and proc.poll() is None:
            try:
                with get(f"http://127.0.0.1:{port}/readyz", 5) as r:
                    if r.status == 200:
                        break
            except Exception:  # noqa: BLE001
                pass
            time.sleep(5)
        else:
            pr.append(f"the engine was not ready after {int(time.time() - t0)} s (exit {proc.poll()})")
            return res
        inbox = os.path.join(data, "store", "inbox")
        t1 = time.time()
        while time.time() - t1 < 300 and [f for f in os.listdir(inbox) if f.endswith(".sqlite")]:
            time.sleep(3)
        left = [f for f in os.listdir(inbox)]
        bad = [f for f in left if f.endswith(".bad")]
        if bad or [f for f in left if f.endswith(".sqlite")]:
            pr.append(f"the engine left {len(left)} part(s) in the inbox ({len(bad)} refused)")
        sys.path.insert(0, HERE)
        import compat_worker
        rows, _t = compat_worker.candidate_rows(candidate, sample)
        names = sorted({k.split("\x1f", 1)[0] for t, k, _v in rows if t == "finished" and k.endswith("\x1ffull")})[:max(1, sample // 5)]
        served = 0
        for n in names:
            try:
                if selftest.explain_origin(port, n, 60).get("kind") == "finished":
                    served += 1
                else:
                    pr.append(f"{n!r} was not answered from its stored answer")
            except Exception as e:  # noqa: BLE001
                pr.append(f"{n!r}: {type(e).__name__}")
        res["served"] = {"names": len(names), "finished": served}
        res["ok"] = not pr and bool(names)
        if not names:
            pr.append("the candidate holds no finished answer with a full form to ask for")
    finally:
        try:
            os.killpg(proc.pid, signal.SIGTERM)                  # the group this check started (its own session), by its exact PID
            proc.wait(60)
        except Exception:  # noqa: BLE001
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except Exception:  # noqa: BLE001
                pass
        log.close()
    return res


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--candidate", required=True, help="the publish folder (OUT of lists_build.py build): OUT/lists/manifest.json")
    ap.add_argument("--pubkey", required=True, help="ed25519:HEX of the key that signed the candidate (the release key; a test key in tests)")
    ap.add_argument("--last", type=int, default=3, help="the newest N released versions (default 3)")
    ap.add_argument("--versions", help="comma-separated tags instead of --last, e.g. v0.4.23,v0.4.22,v0.4.21")
    ap.add_argument("--source-dir", action="append", default=[], metavar="NAME=TREE", help="a source tree on disk as a version (tests)")
    ap.add_argument("--work", help="scratch folder (default: a new one under $TMPDIR, removed afterwards)")
    ap.add_argument("--cache", help="where downloaded release files are kept (default: WORK/cache)")
    ap.add_argument("--sample", type=int, default=50)
    ap.add_argument("--engine", action="store_true", help="also start each version's real program (needs --base-from)")
    ap.add_argument("--base-from", help="--engine: an install folder made by install.py whose `current` release the engines use")
    ap.add_argument("--json", action="store_true", help="print the result as JSON only")
    a = ap.parse_args(argv)
    a.candidate = os.path.abspath(a.candidate)                          # the workers run in scratch folders
    if not os.path.isfile(os.path.join(a.candidate, "lists", "manifest.json")):
        print(f"compat_check: {a.candidate}/lists/manifest.json not found", file=sys.stderr)
        return 2
    if a.engine and not a.base_from:
        print("compat_check: --engine needs --base-from", file=sys.stderr)
        return 2
    own = a.work is None
    work = os.path.abspath(a.work) if a.work else tempfile.mkdtemp(prefix="compat-check-")
    os.makedirs(work, exist_ok=True)
    cache = os.path.abspath(a.cache) if a.cache else os.path.join(work, "cache")
    rid = release_id_for(a.candidate)
    results = []
    try:
        trees = []                                                       # (name, source tree, release dict or None)
        for spec in a.source_dir:
            n, _, p = spec.partition("=")
            trees.append((n, os.path.abspath(p), None))
        if not a.source_dir or a.versions:
            vs = ([v for v in last_versions(60) if v["tag"] in set(x.strip() for x in a.versions.split(","))] if a.versions
                  else last_versions(a.last))
            if a.versions:
                for want in (x.strip() for x in a.versions.split(",")):
                    if want not in {v["tag"] for v in vs}:
                        results.append({"version": want, "ok": False, "problems": [f"{want} is not a release of {REPO}"]})
            for v in vs:
                try:
                    trees.append((v["tag"], source_of(v, cache), v))
                except Exception as e:  # noqa: BLE001
                    results.append({"version": v["tag"], "ok": False, "problems": [f"its source could not be fetched: {type(e).__name__}: {e}"]})
        if not trees and not results:
            print("compat_check: no installer versions to check", file=sys.stderr)
            return 2
        for name, tree, v in trees:
            print(f"== {name}", file=sys.stderr, flush=True)
            r = check_version(name, tree, a.candidate, a.pubkey, work, a.sample, rid)
            if a.engine and v is not None and r["ok"]:
                r["engine_run"] = check_engine(name, v, a.candidate, a.pubkey, work, a.base_from, rid, cache, a.sample)
                r["ok"] = r["ok"] and r["engine_run"]["ok"]
                r["problems"] += r["engine_run"]["problems"]
            results.append(r)
            print(f"   {'PASS' if r['ok'] else 'FAIL'} {name}: " + (json.dumps(r.get("served")) if r["ok"] else "; ".join(r["problems"])),
                  file=sys.stderr, flush=True)
    finally:
        if own:
            shutil.rmtree(work, ignore_errors=True)
    ok = all(r["ok"] for r in results)
    print(json.dumps({"ok": ok, "candidate": a.candidate, "versions": results}, indent=None if a.json else 1, sort_keys=True))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
