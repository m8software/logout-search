"""What an installed logout search fetches beside its engine, and how often (owner 2026-11-14: "should all be
configurable in the tray settings, set update times or check if you want them at all"; then, for the most intuitive
way and a good default: one Data use choice, Automatic by default).

One file, DATA/state/collectors.json, read by `start` (installer/logout_search.py: which collectors run, with what
settings), written by the tray's menu (installer/tray_win.py), the admin section of the search.logout.cloud page and
`logout-search collectors`:

    profile          Data use: auto (Full on an ordinary connection, Light on a metered one: netcost.py), full,
                     balanced, light, search (nothing beside the engine), or custom (the five below)
    news, sports, weather     on or off (custom)
    news_every       1, 2 or 4: every feed polled at its own pace, half as often, a quarter as often (custom)
    weather_every_h  1, 3 or 6: NOAA's hourly forecast bulletin (~28 MB) every N hours (custom)

Changing one of the custom settings while a preset is chosen starts custom from what that preset was doing.
Every preset keeps the hourly forecast at least every 6 hours: the engine calls a forecast older than 8 hours stale.
Daily downloads, measured on testbox on 2026-11-14 (v0.4.6, gzip): Full about 780 MB, Balanced about 475, Light about 320.
Local weather radar (home + queried stations only, ~90 KB GIF): about 13 MB/day per active station
at 10-min checks (Full), about 7 MB/day per active station at 20-min checks (Balanced), off in Light and Search only.
A change takes effect within a minute: `start` restarts the collector whose settings changed. The daily update's own
switches are in DATA/state/autoupdate.json (autoupdate.py). Standard library only: the tray reads it without the engine.
"""
from __future__ import annotations

import json
import os

COLLECTORS = ("news", "sports", "weather")
NEWS_EVERY = (1, 2, 4)
WEATHER_EVERY_H = (1, 3, 6)
PROFILES = ("auto", "full", "balanced", "light", "search", "custom")
DEFAULTS = {"profile": "auto", "news": True, "sports": True, "weather": True, "news_every": 1, "weather_every_h": 3}
H, M = 3600, 60
# each preset: which collectors run, and the collectors' environment (news/poller.py, news/wxall.py)
PRESETS = {
    "full": ({"news": True, "sports": True, "weather": True},
             {"NEWS_POLL_SCALE": 1, "WX_EVERY_NBH_S": 3 * H, "WX_EVERY_NBE_S": 6 * H, "WX_EVERY_METARS_S": 10 * M, "WX_EVERY_ALERTS_S": 5 * M, "WX_EVERY_RADAR_S": 10 * M}),
    "balanced": ({"news": True, "sports": True, "weather": True},
                 {"NEWS_POLL_SCALE": 2, "WX_EVERY_NBH_S": 4 * H, "WX_EVERY_NBE_S": 12 * H, "WX_EVERY_METARS_S": 20 * M, "WX_EVERY_ALERTS_S": 10 * M, "WX_EVERY_RADAR_S": 20 * M}),
    "light": ({"news": True, "sports": True, "weather": True},
              {"NEWS_POLL_SCALE": 4, "WX_EVERY_NBH_S": 6 * H, "WX_EVERY_NBE_S": 12 * H, "WX_EVERY_METARS_S": 30 * M, "WX_EVERY_ALERTS_S": 15 * M, "WX_EVERY_RADAR_S": 0}),
    "search": ({"news": False, "sports": False, "weather": False}, {}),
}
# what each choice is called, and what it downloads a day (the tray and the page show these)
PROFILE_TEXT = {"auto": ("Automatic", "Full, or Light on a metered connection"), "full": ("Full", "about 780 MB a day + ~13 MB/station radar"),
                "balanced": ("Balanced", "about 475 MB a day + ~7 MB/station radar"), "light": ("Light", "about 320 MB a day; no radar"),
                "search": ("Search only", "nothing beside the engine"), "custom": ("Custom", "your own choices below")}


def path(data: str) -> str:
    return os.path.join(data, "state", "collectors.json")


def load(data: str) -> dict:
    """The settings, each checked: an unknown or bad value falls back to its default. A file from before Data use
    (only the five custom settings) reads as custom, so nobody's choices change."""
    try:
        with open(path(data), encoding="utf-8") as f:
            got = json.load(f)
        got = got if isinstance(got, dict) else {}
    except (OSError, ValueError):
        got = {}
    out = dict(DEFAULTS)
    for k in COLLECTORS:
        if isinstance(got.get(k), bool):
            out[k] = got[k]
    if got.get("news_every") in NEWS_EVERY:
        out["news_every"] = got["news_every"]
    if got.get("weather_every_h") in WEATHER_EVERY_H:
        out["weather_every_h"] = got["weather_every_h"]
    if got.get("profile") in PROFILES:
        out["profile"] = got["profile"]
    elif any(k in got for k in DEFAULTS if k != "profile"):
        out["profile"] = "custom"
    return out


def save(data: str, s: dict) -> None:
    p = path(data)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p + ".tmp", "w", encoding="utf-8") as f:
        json.dump({k: s[k] for k in DEFAULTS}, f, indent=1, sort_keys=True)
    os.replace(p + ".tmp", p)


def _custom_from(preset: str) -> dict:
    """A preset's choices in the custom settings' terms (the nearest each offers), for "change one thing"."""
    on, env = PRESETS[preset]
    nbh = env.get("WX_EVERY_NBH_S", 3 * H) // H
    return {**on, "news_every": env.get("NEWS_POLL_SCALE", 1),
            "weather_every_h": min(WEATHER_EVERY_H, key=lambda h: abs(h - nbh))}


def set_value(data: str, key: str, value, metered=None) -> dict:
    """One setting changed; ValueError for a key or value that is not one of the choices. A custom setting changed
    while a preset is chosen switches to custom, starting from that preset's choices."""
    s = load(data)
    if key == "profile":
        if value not in PROFILES:
            raise ValueError(f"profile: one of {', '.join(PROFILES)}")
        s["profile"] = value
        save(data, s)
        return s
    if key in COLLECTORS:
        if not isinstance(value, bool):
            value = str(value).lower() in ("1", "on", "true", "yes")
    elif key == "news_every":
        value = int(value)
        if value not in NEWS_EVERY:
            raise ValueError(f"news_every: one of {NEWS_EVERY}")
    elif key == "weather_every_h":
        value = int(value)
        if value not in WEATHER_EVERY_H:
            raise ValueError(f"weather_every_h: one of {WEATHER_EVERY_H}")
    else:
        raise ValueError(f"no setting {key!r}: {', '.join(DEFAULTS)}")
    if s["profile"] != "custom":
        s.update(_custom_from(effective(data, metered)["applied"]) if s["profile"] != "search"
                 else {"news": False, "sports": False, "weather": False})
        s["profile"] = "custom"
    s[key] = value
    save(data, s)
    return s


def effective(data: str, metered=None) -> dict:
    """{"profile", "applied" (the preset in force, or custom), "metered", "on": {collector: runs?}, "env": {...}}.
    metered: True/False, or None to ask netcost (only for auto)."""
    s = load(data)
    p = s["profile"]
    if p == "auto":
        if metered is None:
            import netcost
            metered = netcost.metered()
        applied = "light" if metered else "full"
    else:
        applied = p
    if applied == "custom":
        h = s["weather_every_h"]
        on = {k: s[k] for k in COLLECTORS}
        env = {"NEWS_POLL_SCALE": s["news_every"], "WX_EVERY_NBH_S": h * H, "WX_EVERY_NBE_S": max(6, h) * H,
               "WX_EVERY_METARS_S": 10 * M, "WX_EVERY_ALERTS_S": 5 * M, "WX_EVERY_RADAR_S": 10 * M}
    else:
        on, env = PRESETS[applied]
    if os.environ.get("LOGOUT_COLLECTORS", "") == "0":          # tests, a machine that wants only search
        on = {k: False for k in COLLECTORS}
    return {"profile": p, "applied": applied, "metered": metered if p == "auto" else None,
            "on": dict(on), "env": {k: str(v) for k, v in env.items()}}


def on(data: str, metered=None) -> dict:
    """{collector: runs?}"""
    return effective(data, metered)["on"]


def env_for(data: str, metered=None) -> dict:
    """The collectors' environment (news/poller.py NEWS_POLL_SCALE, news/wxall.py WX_EVERY_*_S)."""
    return effective(data, metered)["env"]
