#!/usr/bin/env python3
"""`logout-search doctor`: one plain-text report of this install, to paste into a support email.

Versions, the data release in use, the answers file (version, how many answers it holds, how old), whether the engine is
up, the last update and its self-test, the channel, free disk space, and a few test searches with their timings.

Nothing personal: the test searches are fixed words (and two names read from the published answers file), never a search of
the user's; no name, no location, no PIN, no feed list, no file paths of the user's home (a path under the home folder is
written "~"). It is made to work when the engine is down, and when nothing is installed: each part says what it could not read.

Standard library only.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import sqlite3
import sys
import time
import urllib.request

FIXED = ("wikipedia", "history of the bicycle")


def redact(text, home: str | None = None) -> str:
    """The home folder and the user name never leave this report."""
    t = str(text)
    home = home or os.path.expanduser("~")
    user = os.path.basename(home.rstrip("/\\")) if home else ""
    for h in {home, home.replace("\\", "/"), home.replace("/", "\\")}:
        if h and len(h) > 1:
            t = t.replace(h, "~")
    if user and len(user) > 2:
        t = t.replace(user, "<user>")
    return t


def ago(ts) -> str:
    try:
        d = time.time() - float(ts)
    except (TypeError, ValueError):
        return "never"
    if d < 0:
        return "just now"
    return f"{int(d // 86400)} d {int(d % 86400 // 3600)} h ago" if d >= 86400 else f"{int(d // 3600)} h {int(d % 3600 // 60)} min ago"


def when(ts) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(ts))) + f" ({ago(ts)})"
    except (TypeError, ValueError, OSError):
        return "never"


def answers_counts(data: str, release) -> dict:
    """What the answers file (the store) holds for this release, read-only."""
    p = os.environ.get("SEARCH_STORE") or os.path.join(data, "store", "results.sqlite")
    if not os.path.isfile(p):
        return {}
    try:
        db = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error as e:
        return {"error": str(e)}
    out = {"store_mb": round(os.path.getsize(p) / 1e6)}
    try:
        for t in ("curated", "finished"):
            try:
                n, newest = db.execute(f"SELECT count(*), max(at) FROM {t} WHERE release = ?", (release,)).fetchone()
                out[t] = n
                out[t + "_newest"] = newest
                out[t + "_all_releases"] = db.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
            except sqlite3.Error:
                pass
        try:
            out["dept"] = db.execute("SELECT count(*) FROM dept").fetchone()[0]
        except sqlite3.Error:
            pass
    finally:
        db.close()
    return out


def timed_searches(port: int, names: list, get) -> list:
    rows = []
    for name in names:
        t0 = time.monotonic()
        try:
            d = get(port, name, 30.0)
            ok = isinstance(d, dict) and (bool(d.get("card")) or bool(d.get("results")))
            rows.append(f"  {name!r}: {'answered' if ok else 'EMPTY'}, card {'yes' if isinstance(d, dict) and d.get('card') else 'no'}, "
                        f"{len((d or {}).get('results') or [])} results, {int((time.monotonic() - t0) * 1000)} ms")
        except Exception as e:  # noqa: BLE001
            rows.append(f"  {name!r}: FAILED after {int((time.monotonic() - t0) * 1000)} ms ({type(e).__name__})")
    return rows


def report(data: str, port: int, version: str, platform: str, frozen: bool, *, get=None, ready=None, home=None) -> str:
    import autoupdate as au
    import install
    import release_ptr
    import selftest
    get, ready = get or selftest.instant, ready or install._readyz
    L = ["logout search doctor", "=" * 20, f"made {time.strftime('%Y-%m-%d %H:%M %Z')}", ""]
    L += ["Program", f"  version: {version}{'' if frozen else ' (run from a checkout)'}", f"  system: {platform}", ""]
    cur = prev = None
    try:
        cur, prev = release_ptr.current_id(data), release_ptr.current_id(data, "previous")
    except Exception:  # noqa: BLE001
        pass
    L += ["Search data", f"  release in use: {cur or 'none installed'}", f"  previous release kept: {prev or 'none'}"]
    try:
        lst = install.lists_state(data)
    except Exception:  # noqa: BLE001
        lst = {}
    c = answers_counts(data, cur) if cur else {}
    L += ["", "Answers file"]
    if lst or c:
        L.append(f"  version: {lst.get('version') or 'none taken in yet'}  (parts taken: {len(lst.get('applied') or [])}, for release {lst.get('release') or '?'})")
        if c.get("error"):
            L.append(f"  store unreadable: {c['error']}")
        else:
            L.append(f"  hand-picked lists: {c.get('curated', 0)}   stored answers: {c.get('finished', 0)}   departments: {c.get('dept', 0)}"
                     f"   (file {c.get('store_mb', 0)} MB)")
            if c.get("finished_newest"):
                L.append(f"  newest stored answer: {when(c['finished_newest'])}")
            if c.get("finished_all_releases", 0) > c.get("finished", 0):
                L.append(f"  answers of other releases kept in the file: {c['finished_all_releases'] - c.get('finished', 0)}")
        if lst.get("quarantine"):
            L.append(f"  SWITCHED OFF: version {lst['quarantine']} failed its self-test; searches are answered live until a newer one")
        inbox = os.path.join(data, "store", "inbox")
        try:
            names = os.listdir(inbox)
            L.append(f"  waiting for the engine to take in: {len([n for n in names if n.endswith('.sqlite')])}, refused: {len([n for n in names if n.endswith('.bad')])}")
        except OSError:
            pass
    else:
        L.append("  none (no answers file taken in yet)")
    st = au.load(data)
    L += ["", "Updates", f"  automatic updates: {'on' if au.enabled(st) else 'OFF'}   channel: {au.channel_of(st)}   check window: {au.at_hour(st):02d}:00 for two hours",
          f"  last check: {when(st.get('last_check'))}", f"  last result: {st.get('last_result') or 'none yet'}"]
    for k, label in (("program", "program"), ("data", "data"), ("addon_result", "add-ons"), ("lists_result", "answers")):
        v = st.get(k)
        if isinstance(v, dict) and (v.get("why") or v.get("action") == "failed" or v.get("lists") == "failed"):
            L.append(f"  {label}: {v.get('action') or v.get('lists')} - {v.get('why')}")
    if st.get("failed"):
        L.append("  versions that failed their self-test here (not tried again): " + "; ".join(f"{k} {', '.join(v)}" for k, v in st["failed"].items() if v))
    sr = st.get("selftest")
    if isinstance(sr, dict):
        L.append(f"  last self-test ({sr.get('kind') or '?'}, {when(sr.get('at'))}): {'passed' if sr.get('ok') else 'FAILED'}, {sr.get('ms', 0)} ms")
        for ch in sr.get("checks") or []:
            L.append(f"    {ch['status']:<4} {ch['name']}" + (f" - {ch['why']}" if ch.get("why") else ""))
    rb = st.get("rollback")
    if isinstance(rb, dict):
        L.append(f"  last revert ({when(rb.get('at'))}): {rb.get('result')} - {rb.get('why')}")
    L += ["", "This computer"]
    try:
        free = shutil.disk_usage(data if os.path.isdir(data) else os.path.dirname(data) or ".").free
        L.append(f"  free disk space where the data is: {free / 1e9:.1f} GB")
    except OSError:
        L.append("  free disk space: unknown")
    L += ["", "Engine"]
    code = ready(port)
    if code != 200:
        L.append(f"  engine: not running (port {port} answered {code or 'nothing'})" if code in (0, None) else
                 f"  engine: starting or not ready (port {port} answered {code})")
        if code in (0, None):
            L.append("  test searches skipped")
    else:
        L.append(f"  engine: up (port {port})")
        L.append("  test searches (fixed words, never yours):")
        names = list(FIXED)
        try:
            names += [n for n, _c in selftest.known_names(data, cur, 1)]
        except Exception:  # noqa: BLE001
            pass
        L += timed_searches(port, names, get)
    L += ["", "Nothing in this report is a search of yours, your name, your place or a PIN; paths under your home folder are written ~."]
    return redact("\n".join(L), home)


# ================================================================================================ opt-in daily health report
HEALTH_URL = os.environ.get("LOGOUT_HEALTH_URL", "https://api.logout.cloud/health-report")
HEALTH_SCHEMA = "logout.health.v1"
HEALTH_EVERY_S = 20 * 3600
HEALTH_ID_RE = re.compile(r"^[0-9a-f]{32}$")


def health_state_path(data: str) -> str:
    return os.path.join(data, "state", "health.json")


def health_load(data: str) -> dict:
    """Opt-in daily health report state (health_report is False by default)."""
    default = {"health_report": False, "install_id": None, "last_sent": None, "last_status": None}
    try:
        with open(health_state_path(data), encoding="utf-8") as f:
            got = json.load(f)
        if not isinstance(got, dict):
            return default
    except (OSError, ValueError):
        return default
    out = dict(default)
    out["health_report"] = got.get("health_report") is True
    iid = got.get("install_id")
    out["install_id"] = iid if isinstance(iid, str) and HEALTH_ID_RE.fullmatch(iid) else None
    ls = got.get("last_sent")
    out["last_sent"] = float(ls) if isinstance(ls, (int, float)) and not isinstance(ls, bool) else None
    st = got.get("last_status")
    out["last_status"] = st if isinstance(st, str) else None
    return out


def health_save(data: str, st: dict) -> None:
    p = health_state_path(data)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p + ".tmp", "w", encoding="utf-8") as f:
        json.dump(st, f, indent=1, sort_keys=True)
    os.replace(p + ".tmp", p)


def health_enabled(data: str) -> bool:
    return health_load(data)["health_report"]


def set_health_report(data: str, on: bool) -> dict:
    st = health_load(data)
    st["health_report"] = bool(on)
    if on and not st["install_id"]:
        st["install_id"] = secrets.token_hex(16)
    health_save(data, st)
    return st


def reset_install_id(data: str) -> dict:
    st = health_load(data)
    st["install_id"] = secrets.token_hex(16)
    st["last_sent"] = None
    st["last_status"] = None
    health_save(data, st)
    return st


def health_payload(data: str, port: int = 8090, version: str = "", platform: str = "", frozen: bool = False,
                   *, ready=None, now: float | None = None, home: str | None = None) -> dict:
    """Strictly non-personal health summary reusing doctor.py fields (no searches, paths, names, IPs or PINs)."""
    import autoupdate as au
    import homesettings as hs
    import install
    import release_ptr
    st_h = health_load(data)
    iid = st_h["install_id"] or secrets.token_hex(16)
    cur = prev = None
    try:
        cur, prev = release_ptr.current_id(data), release_ptr.current_id(data, "previous")
    except Exception:  # noqa: BLE001
        pass
    try:
        lst = install.lists_state(data)
    except Exception:  # noqa: BLE001
        lst = {}
    c = answers_counts(data, cur) if cur else {}
    au_st = au.load(data)
    sr = au_st.get("selftest") if isinstance(au_st.get("selftest"), dict) else None
    rb = au_st.get("rollback") if isinstance(au_st.get("rollback"), dict) else None
    try:
        free_gb = round(shutil.disk_usage(data if os.path.isdir(data) else os.path.dirname(data) or ".").free / 1e9, 1)
    except OSError:
        free_gb = None
    try:
        code = int((ready or install._readyz)(port) or 0)
    except Exception:  # noqa: BLE001
        code = 0
    return {
        "schema": HEALTH_SCHEMA,
        "install_id": iid,
        "day": time.strftime("%Y-%m-%d", time.gmtime(time.time() if now is None else now)),
        "program": {"version": str(version or "unknown")[:40], "platform": str(platform or sys.platform)[:24], "frozen": bool(frozen)},
        "data": {"release": cur, "has_previous": bool(prev)},
        "answers": {"version": lst.get("version"), "curated": int(c.get("curated", 0)), "finished": int(c.get("finished", 0)),
                    "dept": int(c.get("dept", 0)), "quarantine": bool(lst.get("quarantine"))},
        "updates": {"enabled": au.enabled(au_st), "channel": au.channel_of(au_st),
                    "last_result": redact(str(au_st.get("last_result") or ""), home)[:120] or None,
                    "selftest_ok": sr.get("ok") if sr else None,
                    "selftest_kind": sr.get("kind") if sr else None,
                    "rollback": rb.get("result") if rb else None},
        "collectors": {"profile": hs.load(data).get("profile")},
        "disk": {"free_gb": free_gb},
        "engine": {"ready": code == 200, "code": code},
    }


def maybe_send_health(data: str, *, port: int = 8090, version: str = "", platform: str = "", frozen: bool = False,
                      opener=None, ready=None, metered=None, now: float | None = None, url: str | None = None) -> str:
    """Send at most once daily after autoupdate when opted in and unmetered. Never retries in a loop; silent on failure."""
    st = health_load(data)
    if not st["health_report"]:
        return "off"
    if metered is None:
        try:
            import netcost
            metered = netcost.metered()
        except Exception:  # noqa: BLE001
            metered = None
    if metered is True:
        return "metered"
    t = time.time() if now is None else float(now)
    if st["last_sent"] is not None and t - st["last_sent"] < HEALTH_EVERY_S:
        return "recent"
    if not st["install_id"]:
        st["install_id"] = secrets.token_hex(16)
    st["last_sent"] = t                                    # stamp before sending so a failure never retries until tomorrow
    health_save(data, st)
    try:
        body = json.dumps(health_payload(data, port=port, version=version, platform=platform, frozen=frozen, ready=ready, now=t),
                          separators=(",", ":")).encode("utf-8")
        req = urllib.request.Request(url or HEALTH_URL, data=body, method="POST",
                                     headers={"Content-Type": "application/json", "User-Agent": "logout-health/1"})
        with (opener or urllib.request.urlopen)(req, timeout=5) as r:
            _ = r.read(256)
        st["last_status"] = "sent"
        health_save(data, st)
        return "sent"
    except Exception:  # noqa: BLE001 - health telemetry must fail silently and never retry in a loop
        try:
            st["last_status"] = "failed"
            health_save(data, st)
        except OSError:
            pass
        return "failed"
