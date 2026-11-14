#!/usr/bin/env python3
"""Automatic updates for an installed logout search: the program and the search data (owner 2026-11-14: "yes build
the auto updates").

The running engine's `start` process checks once a day, between 03:00 and 05:00 local time (or at the next start when
the computer was off then, after 36 hours), and on request (`logout-search autoupdate now`, the tray's "Check for updates
now"):

  program   GitHub's latest release of m8software/logout-search. A newer version than this program's: the file for this
            system is downloaded and its SHA-256 compared with the release's SHA256SUMS before anything runs it.
              Linux, macOS  the new program folder is unpacked beside BASE/app, its `version` must answer with the new
                            version, then the two folders are swapped and the engine restarts (exit 75: systemd and
                            launchd start it again; the Windows tray does too).
              Windows       the new logout-search-setup.exe runs with --update: it closes this program and the tray,
                            unpacks the new folder over BASE/app and starts the tray again (a running program's files
                            cannot be replaced from inside it on Windows).
            Only a program built from a release tag updates itself (version.txt, written by bundle/build.py); a build
            from a branch or this checkout never does.
  data      the signed monthly update of the search data (release/update_fetch.py and update_apply.py, the existing
            `logout-search update`): every file checked against the pinned key, the new release switched in by one
            rename, the old one kept for `update_apply.py rollback`. Run at low priority; the engine restarts only when
            an update was applied.

The state is DATA/state/autoupdate.json: {"enabled", "last_check", "last_result", "requested", "program", "data"}.
`enabled: false` stops both. Nothing is sent but the two downloads' ordinary requests (GitHub, Hugging Face).

    logout-search autoupdate status|on|off|now --dest DATA

Standard library only.
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.request

REPO = "m8software/logout-search"
API_LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"
UA = "logout-search-updater"
EXIT_RESTART = 75                       # EX_TEMPFAIL: systemd's Restart=on-failure, launchd's KeepAlive and the tray restart it
DAY = 86400
CHECK_HOURS = (3, 5)                    # the window a daily check prefers, local time
OVERDUE_S = 36 * 3600                   # a computer that was off in the window checks at the next start after this
FIRST_DELAY_S = 600                     # never in the first minutes after a start: the index is loading
ASSETS = {"win32": "logout-search-setup.exe", "linux": "logout-search-linux-x86_64.tar.gz",
          "darwin": "logout-search-macos-arm64.tar.gz"}


# ================================================================================================ state
def state_path(data: str) -> str:
    return os.path.join(data, "state", "autoupdate.json")


def load(data: str) -> dict:
    try:
        with open(state_path(data), encoding="utf-8") as f:
            s = json.load(f)
        return s if isinstance(s, dict) else {}
    except (OSError, ValueError):
        return {}


def save(data: str, s: dict) -> None:
    p = state_path(data)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p + ".tmp", "w", encoding="utf-8") as f:
        json.dump(s, f, indent=1, sort_keys=True)
    os.replace(p + ".tmp", p)


def enabled(s: dict) -> bool:
    return s.get("enabled", True) is not False


def due(s: dict, now: float, started: float) -> bool:
    """Whether to check now: asked for, or a day since the last check in the night window, or overdue."""
    if not enabled(s):
        return False
    if s.get("requested"):
        return True
    if now - started < FIRST_DELAY_S:
        return False
    last = float(s.get("last_check") or 0)
    if now - last >= OVERDUE_S:
        return True
    hour = time.localtime(now).tm_hour
    return now - last >= DAY - 3600 and CHECK_HOURS[0] <= hour < CHECK_HOURS[1]


# ================================================================================================ versions
def parse_version(v: str):
    """(0, 4, 2) from "v0.4.2" / "0.4.2"; None for anything that is not a release version ("dev", "0.1-dev")."""
    m = re.fullmatch(r"v?(\d+)\.(\d+)(?:\.(\d+))?", (v or "").strip())
    return tuple(int(x or 0) for x in m.groups()) if m else None


def newer(latest: str, mine: str) -> bool:
    a, b = parse_version(latest), parse_version(mine)
    return a is not None and b is not None and a > b


# ================================================================================================ downloads
def get(url: str, timeout: float = 60):
    return urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/octet-stream, application/json"}),
                                  timeout=timeout)


def latest_release(fetch=None) -> dict:
    """{"version", "assets": {name: url}} of the latest GitHub release."""
    if fetch is not None:
        return fetch()
    with get(API_LATEST, 30) as r:
        d = json.load(r)
    return {"version": d.get("tag_name") or "", "assets": {a["name"]: a["browser_download_url"] for a in d.get("assets") or []}}


def parse_sums(text: str) -> dict:
    out = {}
    for ln in text.splitlines():
        parts = ln.strip().split()
        if len(parts) == 2 and re.fullmatch(r"[0-9a-f]{64}", parts[0]):
            out[parts[1].lstrip("*")] = parts[0]
    return out


def download(url: str, dest: str, sha256: str, opener=None) -> None:
    """dest, with its SHA-256 checked; a file that does not match is removed and refused."""
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    h = hashlib.sha256()
    with (opener or get)(url, 600) as r, open(dest + ".part", "wb") as f:
        while True:
            b = r.read(1 << 20)
            if not b:
                break
            h.update(b)
            f.write(b)
    if h.hexdigest() != sha256:
        os.remove(dest + ".part")
        raise RuntimeError(f"{os.path.basename(dest)}: SHA-256 {h.hexdigest()[:16]}... is not the published {sha256[:16]}...")
    os.replace(dest + ".part", dest)


# ================================================================================================ the program
def program_update(base: str, platform: str, mine: str, app_dir: str, rel: dict, opener=None, runner=subprocess.run) -> dict:
    """{"action": "none"|"restart"|"handed-over", ...}. app_dir: the folder this program runs from."""
    if parse_version(mine) is None:
        return {"action": "none", "why": f"version {mine!r} is not a release build: it does not update itself"}
    if not newer(rel["version"], mine):
        return {"action": "none", "version": mine, "latest": rel["version"]}
    if os.path.normcase(os.path.realpath(app_dir)) != os.path.normcase(os.path.realpath(os.path.join(base, "app"))):
        return {"action": "none", "why": f"this program runs from {app_dir}, not {os.path.join(base, 'app')}: not replaced"}
    name = ASSETS.get(platform)
    if not name or name not in rel["assets"] or "SHA256SUMS" not in rel["assets"]:
        return {"action": "none", "why": f"release {rel['version']} has no {name} for this system"}
    with (opener or get)(rel["assets"]["SHA256SUMS"], 60) as r:
        sums = parse_sums(r.read().decode("utf-8", "replace"))
    if name not in sums:
        return {"action": "none", "why": f"{name} is not in the release's SHA256SUMS"}
    dl = os.path.join(base, ".download", f"program-{rel['version']}")
    shutil.rmtree(dl, ignore_errors=True)
    path = os.path.join(dl, name)
    download(rel["assets"][name], path, sums[name], opener)
    new_version = rel["version"].lstrip("v")
    if platform == "win32":
        # the setup program closes this program and the tray, replaces BASE/app and starts the tray again
        runner_popen = getattr(runner, "popen", None) or subprocess.Popen
        runner_popen([path, "--update", "--dest", base], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, creationflags=0x00000008 | 0x00000200 | 0x08000000)
        return {"action": "handed-over", "version": new_version}
    app = os.path.join(base, "app")
    new = app + ".new"
    shutil.rmtree(new, ignore_errors=True)
    os.makedirs(new)
    with tarfile.open(path) as t:
        members = []
        for m in t.getmembers():
            parts = m.name.split("/", 1)
            if len(parts) < 2 or not parts[1] or m.name.startswith("/") or ".." in m.name.split("/"):
                continue
            if m.issym() or m.islnk():
                if os.path.isabs(m.linkname) or ".." in m.linkname.split("/"):
                    continue
            m.name = parts[1]                              # --strip-components=1, as install.sh does
            members.append(m)
        t.extractall(new, members=members)
    exe = os.path.join(new, "logout-search")
    r = runner([exe, "version"], capture_output=True, text=True, timeout=300)
    try:
        got = json.loads(r.stdout).get("launcher")
    except (ValueError, AttributeError):
        got = None
    if r.returncode or got != new_version:
        shutil.rmtree(new, ignore_errors=True)
        raise RuntimeError(f"the new program did not start here (exit {r.returncode}, version {got!r}): kept {mine}")
    old = app + ".old"
    shutil.rmtree(old, ignore_errors=True)
    os.replace(app, old)
    os.replace(new, app)
    shutil.rmtree(old, ignore_errors=True)
    shutil.rmtree(dl, ignore_errors=True)
    return {"action": "restart", "version": new_version}


# ================================================================================================ the data
def pem(pubkey: str, path: str) -> str:
    """The pinned key as a PEM file (update_apply.py reads one): ed25519:<64 hex> -> SubjectPublicKeyInfo."""
    import base64
    raw = bytes.fromhex(pubkey.split(":", 1)[1])
    der = bytes.fromhex("302a300506032b6570032100") + raw
    text = "-----BEGIN PUBLIC KEY-----\n" + base64.b64encode(der).decode() + "\n-----END PUBLIC KEY-----\n"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="ascii") as f:
        f.write(text)
    return path


def current_release(data: str):
    try:
        import release_ptr
        return release_ptr.current_id(data)
    except Exception:  # noqa: BLE001
        return None


def data_update(data: str, program: list, source: str, pubkey: str, platform: str, runner=subprocess.run,
                current=current_release) -> dict:
    """Applied or not is read from the `current` pointer before and after (update_apply's own lines share stdout)."""
    key = pem(pubkey, os.path.join(data, "state", "release-key.pem"))
    before = current(data)
    kw = {"creationflags": 0x00004000 | 0x08000000} if platform == "win32" else {"preexec_fn": lambda: os.nice(10)}
    r = runner(program + ["update", "--dest", data, "--source", source, "--pubkey", key],
               capture_output=True, text=True, timeout=6 * 3600, **kw)
    out = {}
    text = r.stdout or ""
    at = text.rfind("\n{")                                # update_fetch's summary is the last JSON object printed
    for chunk in (text, text[at + 1:] if at >= 0 else ""):
        try:
            out = json.loads(chunk)
            break
        except ValueError:
            continue
    if r.returncode == 3:
        return {"action": "none", "why": "no update path from this data: a new full release is out (install it again to move to it)",
                "latest": out.get("latest")}
    if r.returncode:
        raise RuntimeError("data update refused: " + ((r.stderr or "").strip().splitlines() or ["(no message)"])[-1][:300])
    after = current(data)
    if (before is not None and after is not None and after != before) or out.get("applied"):
        return {"action": "restart", "release": after or out.get("latest")}
    return {"action": "none", "release": after or out.get("current") or out.get("latest")}


# ================================================================================================ one check
def check(base: str, data: str, *, platform: str, mine: str, app_dir: str, program: list, source: str, pubkey: str,
          fetch=None, opener=None, runner=subprocess.run, now=None) -> str | None:
    """Program first, then data. -> None, "restart" (the caller stops the engine and exits EXIT_RESTART) or
    "handed-over" (Windows: the setup program takes over and closes this program)."""
    s = load(data)
    s.update(requested=False, last_check=now or time.time())
    result, act = [], None
    try:
        p = program_update(base, platform, mine, app_dir, latest_release(fetch), opener, runner)
        s["program"] = p
        if p["action"] != "none":
            result.append(f"program {mine} -> {p['version']}")
            act = p["action"]
    except Exception as e:  # noqa: BLE001 - an update that fails leaves everything as it was
        s["program"] = {"action": "failed", "why": f"{type(e).__name__}: {e}"[:300]}
        result.append("program check failed")
    if act is None:                                        # a new program restarts first; the data waits for the next check
        try:
            d = data_update(data, program, source, pubkey, platform, runner)
            s["data"] = d
            if d["action"] == "restart":
                result.append(f"data -> {d.get('release')}")
                act = act or "restart"
        except Exception as e:  # noqa: BLE001
            s["data"] = {"action": "failed", "why": f"{type(e).__name__}: {e}"[:300]}
            result.append("data update failed")
    s["last_result"] = "; ".join(result) or "up to date"
    save(data, s)
    return act


class Lock:
    """One check at a time per install (a lock folder; one older than 8 hours is a leftover)."""

    def __init__(self, data: str):
        self.p, self.owned = os.path.join(data, "state", "autoupdate.lock"), False

    def __enter__(self):
        os.makedirs(os.path.dirname(self.p), exist_ok=True)
        try:
            os.mkdir(self.p)
        except FileExistsError:
            if time.time() - os.path.getmtime(self.p) < 8 * 3600:
                return None                                # held by another check: its lock is left alone
            try:
                os.rmdir(self.p)
                os.mkdir(self.p)
            except OSError:
                return None
        self.owned = True
        return self

    def __exit__(self, *exc):
        if self.owned:
            try:
                os.rmdir(self.p)
            except OSError:
                pass
            self.owned = False
        return False


def loop(data: str, stop, on_action, run_check, started=None, tick: float = 60.0, jitter=random.random) -> None:
    """The `start` process's thread: every minute, a check when one is due; on_action(act) for restart/handed-over.
    stop: a threading.Event."""
    started = started or time.time()
    first = True
    while not stop.wait(tick if not first else min(tick, 5.0)):
        first = False
        s = load(data)
        if not due(s, time.time(), started):
            continue
        if not s.get("requested"):
            time.sleep(jitter() * 60)                     # a minute's spread: not every install at the same second
        with Lock(data) as held:
            if held is None:
                continue
            act = run_check()
        if act:
            on_action(act)
            return


def out(text: str) -> None:
    if sys.stdout is not None:                             # the windowed tray has no console
        print(text, flush=True)


RESCUE_EVERY_S = 3600                                      # a crashing engine is restarted every few seconds: check hourly


def rescue(data: str, run_check, now=None) -> str | None:
    """The engine stopped before it was ready (a release whose engine cannot start here): one check, at most hourly,
    so a broken release can still be replaced by the next one. -> what check() returned, or None."""
    s = load(data)
    now = now or time.time()
    if not enabled(s) or (now - float(s.get("last_check") or 0) < RESCUE_EVERY_S and not s.get("requested")):
        return None
    with Lock(data) as held:
        if held is None:
            return None
        return run_check()


def command(action: str, data: str) -> int:
    """`logout-search autoupdate status|on|off|now --dest DATA`."""
    s = load(data)
    if action in ("on", "off"):
        s["enabled"] = action == "on"
        save(data, s)
        out("automatic updates are " + ("on" if action == "on" else "off"))
        return 0
    if action == "now":
        s["requested"] = True
        save(data, s)
        out("asked: the running engine checks within a minute (see `autoupdate status`)")
        return 0
    out(json.dumps({"enabled": enabled(s), **{k: s.get(k) for k in ("last_check", "last_result", "program", "data")}},
                     indent=1, sort_keys=True))
    return 0
