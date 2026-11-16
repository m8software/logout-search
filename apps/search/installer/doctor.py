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
import shutil
import sqlite3
import time

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
