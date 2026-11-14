"""Is this computer's internet connection metered? (homesettings.py's Data use "Automatic": Full on an ordinary
connection, Light on a metered one — a phone hotspot, a capped plan — so the collectors never run up someone's data.)

    Windows  the Network List Manager's INetworkCostManager.GetCost (the same flag Settings > Network > "Metered
             connection" sets, and what Windows Update honours), called with ctypes: no PowerShell.
    Linux    NetworkManager's own judgement (its Metered property: yes or guess-yes), through busctl.
    macOS    not read (Low Data Mode has no command-line or C reading this program can use): never metered.

metered() -> True, False, or None when it cannot tell (treated as not metered). Standard library only.
"""
from __future__ import annotations

import os
import subprocess
import sys

NLM_UNRESTRICTED = 0x1
NLM_OVER_LIMIT, NLM_ROAMING, NLM_APPROACHING = 0x10000, 0x40000, 0x80000


def _windows() -> bool | None:
    import ctypes
    import uuid

    class GUID(ctypes.Structure):
        _fields_ = [("d1", ctypes.c_uint32), ("d2", ctypes.c_uint16), ("d3", ctypes.c_uint16), ("d4", ctypes.c_ubyte * 8)]

    def guid(s: str) -> GUID:
        u = uuid.UUID(s)
        return GUID(u.fields[0], u.fields[1], u.fields[2], (ctypes.c_ubyte * 8)(*u.bytes[8:]))

    ole32 = ctypes.windll.ole32
    ole32.CoInitializeEx(None, 0)                              # this thread's own; already initialised is fine
    clsid, iid = guid("DCB00C01-570F-4A9B-8D69-199FDBA5723B"), guid("DCB00008-570F-4A9B-8D69-199FDBA5723B")
    mgr = ctypes.c_void_p()
    if ole32.CoCreateInstance(ctypes.c_void_p(ctypes.addressof(clsid)), None, 1, ctypes.c_void_p(ctypes.addressof(iid)),
                              ctypes.c_void_p(ctypes.addressof(mgr))) != 0 or not mgr.value:
        return None
    vtbl = ctypes.cast(mgr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
    try:
        cost = ctypes.c_uint32()
        get_cost = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)(vtbl[3])
        if get_cost(mgr, ctypes.c_void_p(ctypes.addressof(cost)), None) != 0:
            return None
        return classify(cost.value)
    finally:
        ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtbl[2])(mgr)   # Release


def classify(cost: int) -> bool:
    """A Windows connection cost (NLM_CONNECTION_COST) -> metered?"""
    return not (cost & NLM_UNRESTRICTED) or bool(cost & (NLM_OVER_LIMIT | NLM_ROAMING | NLM_APPROACHING))


def _linux(run=subprocess.run) -> bool | None:
    try:
        r = run(["busctl", "get-property", "org.freedesktop.NetworkManager", "/org/freedesktop/NetworkManager",
                 "org.freedesktop.NetworkManager", "Metered"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return nm_metered(r.stdout) if r.returncode == 0 else None


def nm_metered(out: str) -> bool | None:
    """busctl's "u 1" -> NMMetered: 1 yes, 3 guess-yes are metered; 2 no, 4 guess-no are not; 0 unknown."""
    parts = (out or "").split()
    if len(parts) != 2 or parts[0] != "u" or not parts[1].isdigit():
        return None
    v = int(parts[1])
    return True if v in (1, 3) else False if v in (2, 4) else None


CACHE_S = 300                 # `start` asks every 15 seconds; the connection is read at most every 5 minutes
_cache: dict = {}


def metered(platform: str | None = None, now: float | None = None) -> bool | None:
    if os.environ.get("LOGOUT_METERED") in ("0", "1"):        # tests, and a person who wants to say so themselves
        return os.environ["LOGOUT_METERED"] == "1"
    import time
    now = time.time() if now is None else now
    if "at" in _cache and now - _cache["at"] < CACHE_S:
        return _cache["v"]
    platform = platform or ("win32" if os.name == "nt" else sys.platform)
    v = None
    try:
        if platform == "win32":
            v = _windows()
        elif platform.startswith("linux"):
            v = _linux()
    except Exception:  # noqa: BLE001 - a connection we cannot read is an ordinary one
        v = None
    _cache.update(at=now, v=v)
    return v
