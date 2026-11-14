"""What an installed logout search fetches beside its engine, and how often (owner 2026-11-14: "should all be
configurable in the tray settings, set update times or check if you want them at all").

One file, DATA/state/collectors.json, read by `start` (installer/logout_search.py: which collectors run, with what
settings), written by the tray's menu (installer/tray_win.py) and `logout-search collectors`:

    news, sports, weather     on (true) or off (false)
    news_every                1, 2 or 4: every feed polled at its own pace, half as often, a quarter as often
    weather_every_h           1, 3 or 6: NOAA's hourly forecast bulletin (~28 MB) fetched every N hours

A change takes effect within a minute: `start` restarts the collector whose settings changed. The daily update's own
switches (automatic or not, its time of day, the add-ons) are in DATA/state/autoupdate.json (autoupdate.py).
Standard library only: the tray reads it without loading the engine.
"""
from __future__ import annotations

import json
import os

COLLECTORS = ("news", "sports", "weather")
NEWS_EVERY = (1, 2, 4)
WEATHER_EVERY_H = (1, 3, 6)
DEFAULTS = {"news": True, "sports": True, "weather": True, "news_every": 1, "weather_every_h": 3}


def path(data: str) -> str:
    return os.path.join(data, "state", "collectors.json")


def load(data: str) -> dict:
    """The settings, each checked: an unknown or bad value falls back to its default."""
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
    return out


def save(data: str, s: dict) -> None:
    p = path(data)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p + ".tmp", "w", encoding="utf-8") as f:
        json.dump({k: s[k] for k in DEFAULTS}, f, indent=1, sort_keys=True)
    os.replace(p + ".tmp", p)


def set_value(data: str, key: str, value) -> dict:
    """One setting changed; ValueError for a key or value that is not one of the choices."""
    s = load(data)
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
    s[key] = value
    save(data, s)
    return s


def on(data: str) -> dict:
    """{collector: runs?}. LOGOUT_COLLECTORS=0 in the environment: none (tests, a machine that wants only search)."""
    s = load(data)
    off = os.environ.get("LOGOUT_COLLECTORS", "") == "0"
    return {k: (not off) and s[k] for k in COLLECTORS}


def env_for(data: str) -> dict:
    """The collectors' environment from the settings (news/poller.py NEWS_POLL_SCALE, news/wxall.py WX_EVERY_*_S)."""
    s = load(data)
    h = s["weather_every_h"]
    return {"NEWS_POLL_SCALE": str(s["news_every"]), "WX_EVERY_NBH_S": str(h * 3600), "WX_EVERY_NBE_S": str(max(6, h) * 3600)}
