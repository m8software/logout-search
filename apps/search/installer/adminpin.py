"""The admin PIN of an installed logout search (owner 2026-11-14: "pin can always be set from the host computer in the
settings, and if the pin is set, can be used to change admin settings from the search.logout.cloud page"; then "pin
shouldnt be required to change pin on the host computer pin should be checkable on host computer settings").

    On the computer the engine runs on: the tray's Admin PIN menu, or `logout-search admin pin show|set PIN|clear`,
    shows, sets and removes it, with no PIN asked. That is the only place it can be read or changed.
    From a paired device: the search.logout.cloud settings page unlocks the admin settings (what is fetched and how
    often, the daily update) with it, for that connection only, for 10 minutes (serve/api/rtc_peer.py). No reply ever
    carries the PIN.

The PIN is kept as typed in DATA/state/admin.json (readable by this user only), so the computer's own settings can
show it. Wrong tries lock it: after 5, for 15 minutes; each later lock twice as long, up to a day (a 4-digit PIN then
takes years to guess). The count is on disk, so restarting does not reset it. Standard library only.
"""
from __future__ import annotations

import hmac
import json
import os
import re
import time

TRIES = 5
LOCK_S = 15 * 60
LOCK_MAX_S = 24 * 3600
PIN_RE = re.compile(r"^\d{4,8}$")


def path(data: str) -> str:
    return os.path.join(data, "state", "admin.json")


def _load(data: str) -> dict:
    try:
        with open(path(data), encoding="utf-8") as f:
            s = json.load(f)
        return s if isinstance(s, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(data: str, s: dict) -> None:
    p = path(data)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    fd = os.open(p + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(s, f)
    os.replace(p + ".tmp", p)
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass


def get(data: str) -> str | None:
    """The PIN, for the host computer's own settings only (the tray, the local command)."""
    pin = _load(data).get("pin")
    return pin if isinstance(pin, str) and PIN_RE.match(pin) else None


def is_set(data: str) -> bool:
    return get(data) is not None


def set_pin(data: str, pin: str) -> None:
    """ValueError unless 4 to 8 digits. A new PIN clears the wrong-try count and any lock."""
    pin = (pin or "").strip()
    if not PIN_RE.match(pin):
        raise ValueError("the PIN is 4 to 8 digits")
    _save(data, {"pin": pin})


def clear(data: str) -> None:
    _save(data, {})


def check(data: str, pin, now: float | None = None) -> str:
    """"ok", "wrong", "locked" (too many wrong tries, for now) or "none" (no PIN set: only the host computer changes
    admin settings). A wrong try is counted."""
    now = time.time() if now is None else now
    s = _load(data)
    want = s.get("pin")
    if not isinstance(want, str) or not PIN_RE.match(want):
        return "none"
    if float(s.get("locked_until") or 0) > now:
        return "locked"
    if isinstance(pin, str) and hmac.compare_digest(pin.strip().encode(), want.encode()):
        if s.get("fails") or s.get("locks"):
            _save(data, {"pin": want})
        return "ok"
    fails = int(s.get("fails") or 0) + 1
    if fails >= TRIES:
        locks = int(s.get("locks") or 0)
        s.update(fails=0, locks=locks + 1, locked_until=now + min(LOCK_MAX_S, LOCK_S * 2 ** locks))
    else:
        s["fails"] = fails
    _save(data, s)
    return "locked" if fails >= TRIES else "wrong"


def locked_for(data: str, now: float | None = None) -> int:
    """Seconds until a locked PIN can be tried again (0: not locked)."""
    now = time.time() if now is None else now
    return max(0, int(float(_load(data).get("locked_until") or 0) - now))
