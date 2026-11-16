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

The state is DATA/state/autoupdate.json: {"enabled", "last_check", "last_result", "requested", "program", "data", "channel",
"verify", "failed", "selftest", "rollback"}. `enabled: false` stops both. Nothing is sent but the two downloads' ordinary
requests (GitHub, Hugging Face).

Channels. "stable" (the default) follows GitHub's latest release, the data release's latest.json and the answers'
manifest.json. "beta" follows the newest release GitHub has, pre-releases included, latest-beta.json and manifest-beta.json,
each falling back to the stable one while there is no beta. Our own boxes run beta; a release is promoted to stable (the
pre-release flag off, `hf_dataset.py promote`, `lists_build.py promote`) when they have run it healthy for a day.

Self-test. An update that needs the engine to restart (program, data release, add-ons, a re-based answers file) leaves
`verify` in the state file; the program that starts afterwards (`logout-search start`) runs selftest.py against its own engine
and, when it fails, goes back: the previous program folder (BASE/app.old, kept until the test passes; on Windows the setup
program's --rollback), the previous data release (`update_apply.py rollback`), the add-on removed, or the answers file
switched off (the engine then answers live) until a newer version replaces it from its base. A version that failed is written
to `failed` and never fetched again: a newer one is. A new answers file that needs no restart is tested in place.

    logout-search autoupdate status|on|off|now|channel stable|beta --dest DATA

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
CHECK_HOURS = (3, 5)                    # the window a daily check prefers, local time (at_hour in the state moves it)
OVERDUE_S = 36 * 3600                   # a computer that was off in the window checks at the next start after this
FIRST_DELAY_S = 600                     # never in the first minutes after a start: the index is loading
API_RELEASES = f"https://api.github.com/repos/{REPO}/releases?per_page=100"
CHANNELS = ("stable", "beta")
INBOX_WAIT_S = 600                       # how long a lists part may take the engine to read before it is tested
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
    return now - last >= DAY - 3600 and in_window(s, now)


def at_hour(s: dict) -> int:
    """The hour a daily check's two-hour window starts (the tray's "Check between"); 3 = 03:00-05:00."""
    v = s.get("at_hour", CHECK_HOURS[0])
    return v if isinstance(v, int) and 0 <= v <= 23 else CHECK_HOURS[0]


def in_window(s: dict, now: float) -> bool:
    return (time.localtime(now).tm_hour - at_hour(s)) % 24 < CHECK_HOURS[1] - CHECK_HOURS[0]


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


def channel_of(s: dict) -> str:
    v = s.get("channel")
    return v if v in CHANNELS else "stable"


def pick_beta(releases: list) -> dict | None:
    """The newest release by version among GitHub's, pre-releases included, drafts never."""
    best = None
    for r in releases:
        v = parse_version(r.get("tag_name") or "")
        if r.get("draft") or v is None:
            continue
        if best is None or v > parse_version(best["tag_name"]):
            best = r
    return best


def latest_release(fetch=None, channel: str = "stable") -> dict:
    """{"version", "assets": {name: url}} of the latest GitHub release (stable) or the newest one, pre-release or not (beta)."""
    if fetch is not None:
        return fetch()
    if channel == "beta":
        with get(API_RELEASES, 30) as r:
            d = pick_beta(json.load(r)) or {}
    else:
        with get(API_LATEST, 30) as r:
            d = json.load(r)
    return {"version": d.get("tag_name") or "", "assets": {a["name"]: a["browser_download_url"] for a in d.get("assets") or []},
            "prerelease": bool(d.get("prerelease"))}


# ================================================================================================ failed versions, the previous kept
def failed_list(s: dict, kind: str) -> list:
    v = (s.get("failed") or {}).get(kind)
    return [str(x) for x in v] if isinstance(v, list) else []


def mark_failed(s: dict, kind: str, ident: str) -> None:
    f = s.setdefault("failed", {})
    f[kind] = sorted(set(failed_list(s, kind)) | {str(ident)})[-20:]


def base_of(data: str) -> str:
    return os.path.dirname(data) if os.path.basename(data) == "data" else data


def begin_verify(s: dict, kind: str, frm, to, **extra) -> None:
    """The next start of the engine tests this update (verify() below) and goes back when it fails."""
    s["verify"] = dict({"kind": kind, "from": frm, "to": to, "at": int(time.time())}, **extra)


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
def program_update(base: str, platform: str, mine: str, app_dir: str, rel: dict, opener=None, runner=subprocess.run,
                   skip=()) -> dict:
    """{"action": "none"|"restart"|"handed-over", ...}. app_dir: the folder this program runs from. skip: versions that failed
    their self-test here. The folder it replaces stays as BASE/app.old until the new program passes its self-test (verify)."""
    shutil.rmtree(os.path.join(base, "app.bad"), ignore_errors=True)             # a rolled-back program's folder, left for this
    if parse_version(mine) is None:
        return {"action": "none", "why": f"version {mine!r} is not a release build: it does not update itself"}
    if not newer(rel["version"], mine):
        return {"action": "none", "version": mine, "latest": rel["version"]}
    if os.path.normcase(os.path.realpath(app_dir)) != os.path.normcase(os.path.realpath(os.path.join(base, "app"))):
        return {"action": "none", "why": f"this program runs from {app_dir}, not {os.path.join(base, 'app')}: not replaced"}
    if rel["version"].lstrip("v") in {str(x).lstrip("v") for x in skip}:
        return {"action": "none", "version": mine, "latest": rel["version"],
                "why": f"{rel['version']} failed its self-test here: it waits for a newer version"}
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
        return {"action": "handed-over", "version": new_version, "setup": path}      # the download stays: its --rollback is the way back
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
    os.replace(app, old)                                   # kept: the self-test's way back (verify removes it once it passes)
    os.replace(new, app)
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
                current=current_release, channel: str = "stable", skip=()) -> dict:
    """Applied or not is read from the `current` pointer before and after (update_apply's own lines share stdout).
    skip: release ids that failed their self-test here: when the channel's latest is one of them, nothing is fetched."""
    key = pem(pubkey, os.path.join(data, "state", "release-key.pem"))
    before = current(data)
    kw = {"creationflags": 0x00004000 | 0x08000000} if platform == "win32" else {"preexec_fn": lambda: os.nice(10)}
    chan = ["--channel", "beta"] if channel == "beta" else []
    if skip:
        c = runner(program + ["update", "--dest", data, "--source", source, "--pubkey", key, "--check"] + chan,
                   capture_output=True, text=True, timeout=600, **kw)
        planned = last_json(c.stdout)
        if planned.get("latest") in set(skip):
            return {"action": "none", "release": before, "latest": planned["latest"],
                    "why": f"release {planned['latest']} failed its self-test here: it waits for a newer one"}
    r = runner(program + ["update", "--dest", data, "--source", source, "--pubkey", key, "--metered", "auto"] + chan,
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


# ================================================================================================ the add-ons
def last_json(text: str) -> dict:
    """The last JSON object a command printed (its own progress lines may come first)."""
    text = (text or "").strip()
    for start in [0] + [i + 1 for i, ch in enumerate(text) if ch == "\n" and text[i + 1:i + 2] == "{"][::-1]:
        try:
            v = json.loads(text[start:])
            return v if isinstance(v, dict) else {}
        except ValueError:
            continue
    return {}


def addons_update(data: str, program: list, source: str, pubkey: str, platform: str, runner=subprocess.run) -> dict:
    """The signed add-ons public and a home server run with (wikilinks, se-howto: install.ADDONS): each one that fits
    this data and is missing, older than the published one, or damaged is installed. -> {"action", "installed": [...]}"""
    kw = {"creationflags": 0x00004000 | 0x08000000} if platform == "win32" else {"preexec_fn": lambda: os.nice(10)}
    r = runner(program + ["addon", "list", "--dest", data, "--source", source, "--pubkey", pubkey],
               capture_output=True, text=True, timeout=600, **kw)
    if r.returncode:
        raise RuntimeError("add-on list refused: " + ((r.stderr or "").strip().splitlines() or ["(no message)"])[-1][:300])
    got = last_json(r.stdout)
    have = {a["name"]: a for a in got.get("installed") or []}
    done = []
    for a in got.get("available") or []:
        if not a.get("fits_current") or "unavailable" in a:
            continue
        h = have.get(a["name"])
        if h and h.get("version") == a.get("version") and h.get("intact") and h.get("fits_current"):
            continue
        r = runner(program + ["addon", "install", a["name"], "--dest", data, "--source", source, "--pubkey", pubkey],
                   capture_output=True, text=True, timeout=6 * 3600, **kw)
        if r.returncode:
            raise RuntimeError(f"add-on {a['name']} refused: " + ((r.stderr or "").strip().splitlines() or ["(no message)"])[-1][:300])
        done.append(a["name"])
    return {"action": "restart" if done else "none", "installed": done}


def lists_update(data: str, program: list, source: str, platform: str, runner=subprocess.run, channel: str = "stable",
                skip=()) -> dict:
    """The hand-picked lists and finished answers public serves (install.py lists): the parts this install has not taken
    are dropped in the engine's store inbox, which the running engine reads by itself: never a restart."""
    kw = {"creationflags": 0x00004000 | 0x08000000} if platform == "win32" else {"preexec_fn": lambda: os.nice(10)}
    extra = (["--channel", "beta"] if channel == "beta" else []) + (["--skip", ",".join(skip)] if skip else [])
    r = runner(program + ["lists", "update", "--dest", data, "--source", source, "--metered", "auto"] + extra,
               capture_output=True, text=True, timeout=6 * 3600, **kw)
    if r.returncode:
        raise RuntimeError("lists refused: " + ((r.stderr or "").strip().splitlines() or ["(no message)"])[-1][:300])
    return last_json(r.stdout) or {"lists": "unknown"}


# ================================================================================================ the self-test, and the way back
def lists_state_file(data: str) -> str:
    return os.path.join(data, "store", "lists.json")


def set_quarantine(data: str, version) -> None:
    """Switch the answers file off for this install (install.engine_env reads it): the engine answers live until a newer
    lists version replaces it from its base (install.lists_update)."""
    p = lists_state_file(data)
    try:
        with open(p, encoding="utf-8") as f:
            st = json.load(f)
    except (OSError, ValueError):
        st = {}
    if version is None:
        st.pop("quarantine", None)
    else:
        st["quarantine"] = str(version)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p + ".tmp", "w", encoding="utf-8") as f:
        json.dump(st, f, indent=1, sort_keys=True)
    os.replace(p + ".tmp", p)


def quarantined(data: str):
    try:
        with open(lists_state_file(data), encoding="utf-8") as f:
            return json.load(f).get("quarantine") or None
    except (OSError, ValueError, AttributeError):
        return None


def wait_inbox(data: str, timeout: float = INBOX_WAIT_S, sleep=time.sleep, clock=time.monotonic) -> str:
    """"" when the engine has taken in every lists part; else why not (a refused part is renamed *.bad by the engine)."""
    inbox = os.path.join(data, "store", "inbox")
    t0 = clock()
    while True:
        try:
            names = os.listdir(inbox)
        except OSError:
            return ""
        bad = [n for n in names if n.startswith("lists-") and n.endswith(".bad")]
        if bad:
            return f"the engine refused {len(bad)} answers part(s) ({bad[0]})"
        if not [n for n in names if n.startswith("lists-") and n.endswith(".sqlite")]:
            return ""
        if clock() - t0 >= timeout:
            return f"the engine had not taken in the answers parts after {int(timeout)} s"
        sleep(5)


def rollback_program(base: str, platform: str, v: dict, popen=None) -> dict:
    """Back to the program folder the update kept. Linux and macOS: two renames; the caller exits EXIT_RESTART. Windows: the
    running program cannot replace its own folder: the kept setup program does it (its --rollback), as --update does."""
    app = os.path.join(base, "app")
    old = app + ".old"
    if not os.path.isdir(old):
        raise RuntimeError(f"there is no previous program folder ({old}) to go back to")
    if platform == "win32":
        setup = v.get("setup")
        if not setup or not os.path.isfile(setup):
            raise RuntimeError("the setup program of the update is gone: cannot go back")
        (popen or subprocess.Popen)([setup, "--rollback", "--dest", base], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, creationflags=0x00000008 | 0x00000200 | 0x08000000)
        return {"action": "handed-over"}
    bad = app + ".bad"
    shutil.rmtree(bad, ignore_errors=True)
    os.replace(app, bad)                                   # left in place until the next update: the process running from it ends first
    os.replace(old, app)
    return {"action": "restart"}


def rollback_data(data: str, program: list, v: dict, runner=subprocess.run, current=current_release) -> dict:
    r = runner(program + ["_update-apply", "rollback", data], capture_output=True, text=True, timeout=600)
    got = current(data)
    if r.returncode or (v.get("from") and got != v["from"]):
        raise RuntimeError(f"the previous release {v.get('from')!r} could not be switched back (now {got!r}, exit {r.returncode}): "
                           + ((r.stderr or "").strip().splitlines() or ["(no message)"])[-1][:200])
    return {"action": "restart"}


def finish_verify(base: str, v: dict) -> None:
    """The test passed: what was kept for the way back is no longer needed."""
    if v.get("kind") == "program":
        shutil.rmtree(os.path.join(base, "app.old"), ignore_errors=True)
        shutil.rmtree(os.path.join(base, ".download", f"program-v{v.get('to')}"), ignore_errors=True)
        shutil.rmtree(os.path.join(base, ".download", f"program-{v.get('to')}"), ignore_errors=True)


def revert(data: str, s: dict, v: dict, why: str, *, platform: str, program: list, runner=subprocess.run, popen=None,
           current=current_release) -> str | None:
    """Go back after a failed test and record it. -> "restart" | "handed-over" | None (nothing to go back to)."""
    base, kind, to, frm = base_of(data), v.get("kind"), v.get("to"), v.get("from")
    mark_failed(s, kind, to)
    s["rollback"] = {"kind": kind, "from": frm, "to": to, "why": why[:400], "result": "going back", "at": int(time.time())}
    save(data, s)                                          # before anything that can end this process (the Windows hand-over closes it)
    act, how = None, ""
    try:
        if kind == "program":
            r = rollback_program(base, platform, v, popen)
            act, how = r["action"], f"program {to} -> {frm}"
        elif kind == "data":
            act, how = rollback_data(data, program, v, runner, current)["action"], f"data release {to} -> {frm}"
        elif kind == "addons":
            for n in v.get("names") or []:
                runner(program + ["addon", "remove", n, "--dest", data], capture_output=True, text=True, timeout=600)
            act, how = "restart", "add-ons " + ", ".join(v.get("names") or []) + " removed"
        elif kind == "lists":
            set_quarantine(data, to)
            act, how = "restart", f"answers file {to} switched off (the engine answers live until a newer one)"
    except Exception as e:  # noqa: BLE001 - say why it could not go back; the failed version is still not tried again
        how = f"could not go back: {type(e).__name__}: {e}"[:300]
    s["rollback"] = {"kind": kind, "from": frm, "to": to, "why": why[:400], "result": how, "at": int(time.time())}
    s["last_result"] = f"{kind} {to} failed its self-test: {how}"[:400]
    return act


def verify(data: str, *, platform: str, program: list, port: int, state: str, has_se: bool = False, run=None,
           runner=subprocess.run, popen=None, current=current_release, now=None, mine=None) -> str | None:
    """After an update's restart, once the engine has reported ready (state "ready") or not (the engine exited or timed
    out): test it, and go back when it fails. -> None (passed, or nothing to test), "restart" or "handed-over". The marker is
    cleared before anything runs, so one failed test is never repeated by the next start."""
    s = load(data)
    v = s.pop("verify", None)
    if not isinstance(v, dict):
        return None
    save(data, s)
    stale = (now or time.time()) - float(v.get("at") or 0) > 7 * DAY
    took = (v.get("kind") != "program" or mine is None or parse_version(mine) == parse_version(v.get("to"))) and \
           (v.get("kind") != "data" or current(data) == v.get("to"))
    if stale or not took:                                  # the update never took (a hand-over that failed, a week-old marker): nothing to test
        return None
    if state != "ready":
        res = {"ok": False, "at": int(time.time()), "ms": 0, "checks": [{"name": "engine ready", "status": "fail", "ms": 0,
               "why": f"the engine did not become ready ({state})"}]}
    else:
        import selftest
        res = (run or selftest.run)(port, data, current(data), has_se=has_se, quarantined=bool(quarantined(data)))
    res["kind"] = v.get("kind")
    s["selftest"] = res
    act = None
    if res.get("ok"):
        finish_verify(base_of(data), v)
    else:
        import selftest
        act = revert(data, s, v, selftest.summary(res), platform=platform, program=program, runner=runner, popen=popen, current=current)
    save(data, s)
    return act


def lists_verify(data: str, s: dict, version, *, port: int, platform: str, program: list, has_se: bool = False, run=None,
                 wait=wait_inbox, runner=subprocess.run, current=current_release) -> str | None:
    """A new answers file that needs no restart: wait for the engine to take it in, test it in place, and switch it off when
    the test fails. -> "restart" when it was switched off."""
    import selftest
    why = wait(data)
    res = None
    if not why:
        res = (run or selftest.run)(port, data, current(data), has_se=has_se, quarantined=bool(quarantined(data)))
        if not res.get("ok"):
            why = selftest.summary(res)
    if res is None:
        res = {"ok": False, "at": int(time.time()), "ms": 0, "checks": [{"name": "answers taken in", "status": "fail", "ms": 0, "why": why}]}
    res["kind"] = "lists"
    s["selftest"] = res
    if res.get("ok"):
        return None
    return revert(data, s, {"kind": "lists", "to": version, "from": None}, why, platform=platform, program=program, runner=runner,
                  current=current)


# ================================================================================================ one check
def check(base: str, data: str, *, platform: str, mine: str, app_dir: str, program: list, source: str, pubkey: str,
          fetch=None, opener=None, runner=subprocess.run, now=None, port: int | None = None, has_se: bool = False,
          selftest_run=None, wait=wait_inbox) -> str | None:
    """Program first, then data. -> None, "restart" (the caller stops the engine and exits EXIT_RESTART) or
    "handed-over" (Windows: the setup program takes over and closes this program). port: the running engine's, for the
    in-place test of a new answers file."""
    s = load(data)
    s.update(requested=False, last_check=now or time.time())
    ch = channel_of(s)
    result, act = [], None
    try:
        p = program_update(base, platform, mine, app_dir, latest_release(fetch, ch), opener, runner, skip=failed_list(s, "program"))
        s["program"] = p
        if p["action"] != "none":
            result.append(f"program {mine} -> {p['version']}")
            act = p["action"]
            begin_verify(s, "program", mine, p["version"], **({"setup": p["setup"]} if p.get("setup") else {}))
    except Exception as e:  # noqa: BLE001 - an update that fails leaves everything as it was
        s["program"] = {"action": "failed", "why": f"{type(e).__name__}: {e}"[:300]}
        result.append("program check failed")
    if act is None:                                        # a new program restarts first; the data waits for the next check
        try:
            before = current_release(data)
            d = data_update(data, program, source, pubkey, platform, runner, channel=ch, skip=failed_list(s, "data"))
            s["data"] = d
            if d["action"] == "restart":
                result.append(f"data -> {d.get('release')}")
                act = act or "restart"
                begin_verify(s, "data", before, d.get("release"))
        except Exception as e:  # noqa: BLE001
            s["data"] = {"action": "failed", "why": f"{type(e).__name__}: {e}"[:300]}
            result.append("data update failed")
        if s.get("addons", True) is not False:            # "addons": false in the state file: none installed for you
            try:
                a = addons_update(data, program, source, pubkey, platform, runner)
                s["addon_result"] = a
                if a["action"] == "restart":
                    result.append("add-ons " + ", ".join(a["installed"]))
                    act = act or "restart"
                    if not s.get("verify"):                # a data release's test covers the add-ons it restarts with
                        begin_verify(s, "addons", None, ",".join(a["installed"]), names=a["installed"])
            except Exception as e:  # noqa: BLE001
                s["addon_result"] = {"action": "failed", "why": f"{type(e).__name__}: {e}"[:300]}
                result.append("add-on update failed")
            try:
                li = lists_update(data, program, source, platform, runner, channel=ch, skip=failed_list(s, "lists"))
                s["lists_result"] = li
                if li.get("lists") == "fetched":
                    result.append(f"lists {li.get('version')}")
                    if li.get("was_quarantined"):          # the engine runs with the answers off: it starts again to take them
                        act = act or "restart"
                        if not s.get("verify"):
                            begin_verify(s, "lists", None, li.get("version"))
                    elif port and act is None:
                        save(data, s)                      # the test takes minutes: what happened so far is on record
                        back = lists_verify(data, s, li.get("version"), port=port, platform=platform, program=program, has_se=has_se,
                                            run=selftest_run, wait=wait, runner=runner)
                        if back:
                            result.append(f"lists {li.get('version')} failed its self-test: switched off")
                            act = back
            except Exception as e:  # noqa: BLE001
                s["lists_result"] = {"lists": "failed", "why": f"{type(e).__name__}: {e}"[:300]}
                result.append("lists update failed")
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


def command(action: str, data: str, value=None) -> int:
    """`logout-search autoupdate status|on|off|now|at HOUR|addons on|off --dest DATA`."""
    s = load(data)
    if action == "at":
        try:
            h = int(value)
            assert 0 <= h <= 23
        except (TypeError, ValueError, AssertionError):
            out("autoupdate at HOUR: 0-23, the start of the two-hour window")
            return 2
        s["at_hour"] = h
        save(data, s)
        out(f"daily check between {h:02d}:00 and {(h + 2) % 24:02d}:00")
        return 0
    if action == "channel":
        if value is None:
            out(channel_of(s))
            return 0
        if value not in CHANNELS:
            out("channel stable|beta: stable follows the releases everyone gets; beta gets the next one a day early")
            return 2
        s["channel"] = value
        s["requested"] = True                              # the running engine checks within a minute
        save(data, s)
        out(f"channel: {value}" + (" (the next version arrives here before it reaches everyone)" if value == "beta" else ""))
        return 0
    if action == "addons":
        if value not in ("on", "off"):
            out("autoupdate addons on|off")
            return 2
        s["addons"] = value == "on"
        save(data, s)
        out("add-ons are " + ("installed and kept up to date" if value == "on" else "left as they are"))
        return 0
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
    out(json.dumps({"enabled": enabled(s), "channel": channel_of(s), "at_hour": at_hour(s), "addons": s.get("addons", True) is not False,
                    **{k: s.get(k) for k in ("last_check", "last_result", "program", "data", "addon_result", "lists_result",
                                             "selftest", "rollback", "failed")}},
                     indent=1, sort_keys=True))
    return 0
