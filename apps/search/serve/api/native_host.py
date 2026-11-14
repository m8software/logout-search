#!/usr/bin/env python3
"""Native-messaging host for the logout search extension (search.logout.cloud, local mode).

The browser starts this program itself and talks to it over stdin/stdout — no port, no
HTTP, nothing another website can probe. Each message is a 4-byte native-endian length
followed by UTF-8 JSON (Chrome/Firefox native messaging).

    request  {"id": "r1", "q": "nbc", "results": true}   -> {"id": "r1", "body": {...}}
    request  {"id": "p1", "ping": true}                  -> {"id": "p1", "pong": true, "version": 1}
    request  {"id": "i1", "info": true}                  -> {"id": "i1", "info": {via, host_version}}
    request  {"id": "d1", "details": true}               -> {"id": "d1", "details": {engine, ip, postgres, ...}}
    request  {"id": "s1", "get_settings": true}          -> {"id": "s1", "settings": {...}}
    request  {"id": "s2", "set_settings": {...}}         -> {"id": "s2", "settings": {...}} (validated, saved)
    anything else                                        -> {"id": ..., "error": "..."}

`body` is what search-api's /instant returns (the same code answers — search_api.Handler),
trimmed to the fields the page renders, exactly like the public gateway does.

Several requests are answered at once (typing sends one per keystroke); replies carry the
request id, so order does not matter. This process never prints a query: stdout is the
message channel and stderr is kept free of request content.
"""
from __future__ import annotations

import json
import os
import re
import struct
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

VERSION = 1
MAX_IN = 64 * 1024              # a request is a query, never large
MAX_OUT = 1024 * 1024           # Chrome's limit for host -> browser messages
INSTANT_KEYS = ("suggest", "phrases", "results", "confident", "card", "answer", "intent", "corrected",
                "ms",                             # engine time, shown on the page
                "news", "news_hot", "news_more", "games",   # the user's feeds + personal sources
                "mine",                           # "From your sources": own feed items of any age (2026-11-13)
                "kinds", "weather")               # "Top <kind>" box; a town's forecast (2026-10-01)
RESULT_KEYS = ("url", "title", "content", "source", "official", "lat", "lon",   # lat/lon: a place's point, for directions
               "pid", "hours", "hours_asof", "hours_src", "hours_stale", "site",           # a place's hours, their date and source
               "author", "author_url", "answer_author", "answer_author_url",             # a Stack Exchange page's credits
               "excerpt", "accepted", "answer_url", "licence", "licence_url")            # an answer excerpt's marks (ADR-081)
NEAR_RE = re.compile(r"-?\d{1,2}(\.\d{1,6})?,-?\d{1,3}(\.\d{1,6})?")     # "lat,lon", as the page sends it


def read_message(stream):
    raw = stream.read(4)
    if len(raw) < 4:
        return None                                   # browser closed the port
    (n,) = struct.unpack("=I", raw)
    if n > MAX_IN:
        stream.read(n)
        return {"_error": "message too large"}
    try:
        return json.loads(stream.read(n).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {"_error": "bad json"}


def write_message(stream, lock, obj) -> None:
    data = json.dumps(obj).encode("utf-8")
    if len(data) > MAX_OUT:
        data = json.dumps({"id": obj.get("id"), "error": "answer too large"}).encode()
    with lock:
        stream.write(struct.pack("=I", len(data)))
        stream.write(data)
        stream.flush()


# ---- query log (opt-in) ----------------------------------------------------------------
# The public site and the shipped app keep no search history, and this stays off unless the
# container is started with ENGINE_QUERY_LOG=<file> (dev-engine.sh does, for the owner's own dev
# engine on indexhost only: "make that happen so I can see what's working and how long queries
# take", 2026-09-28). One JSON line per query the engine answers: the text, how it was asked,
# how long, and what came back. `dev-engine.sh stats` reads it. Never leaves the state dir.
QUERY_LOG = os.environ.get("ENGINE_QUERY_LOG", "")
QUERY_LOG_SRC = os.environ.get("ENGINE_QUERY_SRC", "")
QUERY_LOG_MAX = 50 * 1024 * 1024
_tls = threading.local()


def log_query(q: str, kind: str, ms_total: int, body: dict | None, err: str = "") -> None:
    if not QUERY_LOG:
        return
    rec = {"t": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "q": q, "kind": kind, "src": QUERY_LOG_SRC,
           "ms": ms_total}
    if err:
        rec["err"] = err
    if body:
        res = body.get("results") or []
        rec.update({"engine_ms": body.get("ms"), "n": len(res), "sug": len(body.get("suggest") or []),
                    "phr": len(body.get("phrases") or []), "conf": bool(body.get("confident")),
                    "intent": body.get("intent") or "", "card": bool(body.get("card")),
                    "answer": bool(body.get("answer")), "news": len(body.get("news") or []),
                    "top": (res[0].get("url") if res else "") or ((body.get("suggest") or [{}])[0].get("url") or ""),
                    "stages": getattr(_tls, "stages", None) or {}})
    try:
        if os.path.exists(QUERY_LOG) and os.path.getsize(QUERY_LOG) > QUERY_LOG_MAX:
            os.replace(QUERY_LOG, QUERY_LOG + ".1")
        with open(QUERY_LOG, "a", encoding="utf-8") as f:        # one write: lines from parallel threads stay whole
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass                                                     # a full disk must not fail a search


def trim(body: dict) -> dict:
    out = {k: body.get(k) for k in INSTANT_KEYS if k in body}
    out["results"] = [{k: r[k] for k in RESULT_KEYS if k in r} for r in (body.get("results") or [])]
    out["suggest"] = [{k: s.get(k) for k in ("url", "title", "source") if k in s} for s in (body.get("suggest") or [])]
    return out


# ---- settings (search.logout.cloud's ⚙, stored with the engine so every device shares them)
PASS_ENGINES = {           # pass-through engines: the page shows these names, %s = the query
    "brave": "https://search.brave.com/search?q=%s",
    "duckduckgo": "https://duckduckgo.com/?q=%s",
    "startpage": "https://www.startpage.com/do/search?query=%s",
    "google": "https://www.google.com/search?q=%s",
    "bing": "https://www.bing.com/search?q=%s",
    "kagi": "https://kagi.com/search?q=%s",
    "ecosia": "https://www.ecosia.org/search?q=%s",
    "mojeek": "https://www.mojeek.com/search?q=%s",
}
DEFAULT_SETTINGS = {"pass": "google", "custom_url": "", "enter": "pass", "feeds": [], "sources": {}}
# personal sources: leagues the page offers a URL field for; the engine's sports poller
# fetches what the user fills in (news/sports.py PERSONAL_FORMATS)
SOURCE_LEAGUES = {"nfl": "NFL", "nba": "NBA", "mlb": "MLB", "nhl": "NHL"}
SOURCE_FORMATS = {"espn": "ESPN scoreboard JSON", "mlb": "MLB Stats API JSON", "nhl": "NHL schedule JSON"}
SOURCE_LEAGUE_FORMAT = {"nfl": "espn", "nba": "espn", "mlb": "mlb", "nhl": "nhl"}   # a league's format is not a choice
MAX_FEEDS = 100


def settings_path() -> str:
    import os
    return os.environ.get("LOGOUT_SETTINGS") or os.path.join(
        os.path.dirname(os.environ.get("RTC_STATE", "/state/engine.json")), "settings.json")


def valid_settings(new: dict) -> dict:
    """-> a clean settings dict, or ValueError naming the problem."""
    import re
    if not isinstance(new, dict):
        raise ValueError("settings must be an object")
    out = dict(DEFAULT_SETTINGS)
    p = new.get("pass", out["pass"])
    if p not in PASS_ENGINES and p != "custom":
        raise ValueError("unknown pass-through engine")
    out["pass"] = p
    url = str(new.get("custom_url") or "").strip()
    if url and not (re.match(r"^https?://[^\s/]+(/\S*)?$", url) and "%s" in url and len(url) <= 300):
        raise ValueError("custom URL must be http(s)://… with %s where the query goes")
    if p == "custom" and not url:
        raise ValueError("custom engine needs a URL")
    out["custom_url"] = url
    e = new.get("enter", out["enter"])
    if e not in ("logout", "pass"):
        raise ValueError("enter must be 'logout' or 'pass'")
    out["enter"] = e
    feeds = new.get("feeds") or []
    if not isinstance(feeds, list) or len(feeds) > MAX_FEEDS:
        raise ValueError(f"feeds: a list of at most {MAX_FEEDS}")
    clean, seen = [], set()
    for f in feeds:
        fu = str((f or {}).get("url") or "").strip() if isinstance(f, dict) else ""
        if not re.match(r"^https?://[^\s/]+(/\S*)?$", fu) or len(fu) > 500:
            raise ValueError(f"feed URL must be http(s)://…: {fu[:60] or '(empty)'}")
        if fu not in seen:
            seen.add(fu)
            clean.append({"name": str(f.get("name") or "").strip()[:60], "url": fu})
    out["feeds"] = clean
    srcs = new.get("sources") or {}
    if not isinstance(srcs, dict):
        raise ValueError("sources must be an object")
    out["sources"] = {}
    for lg, v in srcs.items():
        su = str((v or {}).get("url") or "").strip() if isinstance(v, dict) else ""
        if not su:
            continue                                       # empty field = source off
        fmt = (v or {}).get("format") or SOURCE_LEAGUE_FORMAT.get(lg, "")
        if lg not in SOURCE_LEAGUES or fmt not in SOURCE_FORMATS or fmt != SOURCE_LEAGUE_FORMAT[lg]:
            raise ValueError(f"unknown source {lg}/{fmt}")
        if not re.match(r"^https://[^\s/]+/\S*$", su) or len(su) > 500:
            raise ValueError(f"{SOURCE_LEAGUES[lg]} source must be an https:// URL")
        out["sources"][lg] = {"url": su, "format": fmt}
    return out


def settings_status() -> dict:
    """For ⚙: how each of the user's feeds and sources is doing on this engine (best effort)."""
    import os
    st = {"feeds": {}, "sources": {}}
    try:
        with open(os.path.join(os.path.dirname(settings_path()), "sports-status.json")) as f:
            st["sources"] = json.load(f)
    except (OSError, ValueError):
        pass
    try:
        lite = os.environ.get("SEARCH_NEWS_SQLITE", "") if os.environ.get("SEARCH_NEWS_BACKEND") == "sqlite" else ""
        if lite:                                  # the news file (ADR-063): last_ok is Unix seconds already
            import sqlite3
            rows = []
            # the user's own feeds may be polled into a second file (SEARCH_NEWS_SQLITE_EXTRA); its word wins
            for path in filter(None, (lite, os.environ.get("SEARCH_NEWS_SQLITE_EXTRA", ""))):
                try:
                    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
                    rows += c.execute("SELECT url, CAST(last_ok AS INTEGER), last_error, disabled, items_total "
                                      "FROM feeds WHERE section = 'mine'").fetchall()
                    c.close()
                except sqlite3.Error:
                    continue
        else:                                     # BRIDGE: the Postgres news database, until no host has one
            import psycopg
            with psycopg.connect(dbname=os.environ.get("SEARCH_NEWS_DB", "news"), connect_timeout=2) as c:
                rows = c.execute("SELECT url, extract(epoch FROM last_ok)::bigint, last_error, disabled, items_total "
                                 "FROM feeds WHERE section = 'mine'").fetchall()
        for url, last_ok, err, disabled, n in rows:
            st["feeds"][url] = {"ok": last_ok is not None and not disabled, "at": last_ok, "items": n,
                                "error": ("disabled: " if disabled else "") + (err or "") if (err or disabled) else ""}
    except Exception:
        pass
    return st


def load_settings() -> dict:
    try:
        with open(settings_path()) as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return dict(DEFAULT_SETTINGS)
    try:
        return valid_settings(raw)
    except ValueError:
        return dict(DEFAULT_SETTINGS)


def save_settings(new: dict) -> dict:
    import os
    clean = valid_settings(new)
    path = settings_path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp{os.getpid()}"
    with open(tmp, "w") as f:
        json.dump(clean, f)
    os.replace(tmp, path)
    return clean


def lan_ip() -> str:
    """The address this machine would use toward the network (UDP connect: no packet is sent).
    In a container it is the container's — installers pass the machine's in LOGOUT_ENGINE_IP."""
    import socket
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))                # TEST-NET-1: never routed anywhere real
            return s.getsockname()[0]
    except OSError:
        return ""


def engine_kind() -> dict:
    """What every connection is told ({info: true}): how the engine is reached (the badge reads "relay" in it),
    nothing about the machine. Its name, LAN address and index size were sent here on every connection until
   ; the page's own code could read them. They are {details: true} now, asked for by the person."""
    return {"via": os.environ.get("LOGOUT_ENGINE_VIA") or "", "host_version": VERSION}


def engine_info() -> dict:
    """{details: true} — what the badge shows once the person asks: where the engine runs and what it holds.
    Row counts are the planner's estimates (instant; an exact count(*) of 6.8M articles is not)."""
    import os
    import platform
    import search_api
    info = {"engine": os.environ.get("LOGOUT_ENGINE_NAME") or platform.node(),
            "ip": os.environ.get("LOGOUT_ENGINE_IP") or lan_ip(),
            "via": os.environ.get("LOGOUT_ENGINE_VIA") or "", "host_version": VERSION}
    with search_api._conn() as c:
        info["postgres"] = c.execute("SHOW server_version").fetchone()[0]
        info["database_gb"] = round(c.execute("SELECT pg_database_size(current_database())").fetchone()[0] / 1e9, 1)
        for name, table in (("sites", "docs"), ("articles", "wiki_docs"), ("pages", "page_docs"), ("places", "places")):
            row = c.execute("SELECT reltuples::bigint FROM pg_class WHERE oid = to_regclass(%s)", (table,)).fetchone()
            if row and row[0] and row[0] > 0:
                info[name] = row[0]
    return info


def search_api_answer():
    """-> answer(q, results) using search_api's /instant code in-process."""
    import search_api

    class Local(search_api.Handler):
        def __init__(self):                           # no socket: only _instant/_send are used
            self.body = None

        def _send(self, code, body, ctype="application/json"):
            self.body = (code, body)

        def log_message(self, fmt, *args):            # search_api logs counts/timings only;
            pass                                      # stay silent anyway (stderr is not ours)

    def answer(q: str, results: bool, full: bool = False, near: str | None = None) -> dict:
        h = Local()
        qs = {"q": [q]}
        if near:
            qs["near"] = [near]
        if not results:
            qs["results"] = ["0"]
        elif full:
            qs["full"] = ["1"]
        with search_api._busy_lock:                     # a visitor in flight: the in-process warmer waits
            search_api._busy[0] += 1
        try:
            h._instant(qs)
        finally:
            with search_api._busy_lock:
                search_api._busy[0] -= 1
        code, raw = h.body
        if code != 200:
            raise RuntimeError(f"search-api {code}")
        full_body = json.loads(raw)
        _tls.stages = full_body.get("stages")             # per-stage ms, for the query log only
        return trim(full_body)

    def more_news(q: str, offset: int) -> dict:
        """One page of "More news" (/more?what=news&offset=N), same code as the site's engine."""
        h = Local()
        h._more({"q": [q], "what": ["news"], "offset": [str(offset)]})
        code, raw = h.body
        if code != 200:
            raise RuntimeError(f"more {code}")
        d = json.loads(raw)
        return {"news": d.get("news") or [], "more": d.get("more") is True}

    def explain(q: str, near: str | None = None) -> dict:
        """The steps of one search (/explain), same code as the site's engine: asked for by the page's button."""
        h = Local()
        h._explain({"q": [q], **({"near": [near]} if near else {})})
        code, raw = h.body
        if code != 200:
            raise RuntimeError(f"explain {code}")
        return json.loads(raw)

    answer.more_news = more_news
    answer.explain = explain
    return answer


def handle(msg: dict, answer, info=None) -> dict:
    mid = msg.get("id") if isinstance(msg, dict) else None
    if not isinstance(msg, dict) or "_error" in msg:
        return {"id": mid, "error": (msg or {}).get("_error", "bad message") if isinstance(msg, dict) else "bad message"}
    if msg.get("ping"):
        return {"id": mid, "pong": True, "version": VERSION}
    if msg.get("get_settings"):
        return {"id": mid, "settings": load_settings(), "engines": PASS_ENGINES,
                "sources": {"leagues": SOURCE_LEAGUES, "formats": SOURCE_FORMATS, "league_format": SOURCE_LEAGUE_FORMAT}, "status": settings_status(),
                # what this engine can do with the settings: one with no feed fetcher beside it (LOGOUT_FEEDS=0, the
                # downloaded program) says so, and the page then offers no sources to add
                "can": {"feeds": __import__("os").environ.get("LOGOUT_FEEDS", "1") != "0"}}
    if "set_settings_json" in msg:           # signed form (rtc_peer checks the signature first)
        try:
            msg = dict(msg, set_settings=json.loads(str(msg["set_settings_json"])))
        except ValueError:
            return {"id": mid, "error": "bad settings"}
    if "set_settings" in msg:
        try:
            return {"id": mid, "settings": save_settings(msg["set_settings"])}
        except ValueError as e:
            return {"id": mid, "error": str(e)}
        except OSError:
            return {"id": mid, "error": "settings not writable on this engine"}
    if msg.get("details"):                   # before "info": a message with both is the explicit request
        try:
            return {"id": mid, "details": (info or engine_info)()}
        except Exception as e:
            return {"id": mid, "error": type(e).__name__}
    if msg.get("info"):
        return {"id": mid, "info": engine_kind()}
    q = msg.get("q")
    if not isinstance(q, str):
        return {"id": mid, "error": "q missing"}
    q = " ".join(q.split())[:200]
    if msg.get("news_more"):                 # "More news" under the box: the next page, never the instant path
        more = getattr(answer, "more_news", None)
        if more is None or len(q) < 3:
            return {"id": mid, "body": {"news": [], "more": False}}
        off = msg.get("offset")
        off = min(max(off, 0), 100) if isinstance(off, int) and not isinstance(off, bool) else 0
        try:
            return {"id": mid, "body": more(q, off)}
        except Exception as e:
            return {"id": mid, "error": type(e).__name__}
    near = msg.get("near") if isinstance(msg.get("near"), str) and NEAR_RE.fullmatch(msg["near"]) else None   # the page's position, if it shared one
    if msg.get("explain"):                   # "How this was ranked": the steps of one search, never the instant path
        how = getattr(answer, "explain", None)
        if how is None or len(q) < 2:
            return {"id": mid, "body": {"steps": []}}
        try:
            return {"id": mid, "body": how(q, near)}
        except Exception as e:
            return {"id": mid, "error": type(e).__name__}
    if len(q) < 2:
        return {"id": mid, "body": {"suggest": [], "phrases": [], "results": []}}
    full = bool(msg.get("full"))
    want = full or bool(msg.get("results", True))
    kind = "full" if full else "results" if want else "suggest"
    t0 = time.monotonic()
    _tls.stages = None
    try:
        body = (answer(q, True, True, near) if full else answer(q, want, False, near)) if near else \
            (answer(q, True, True) if full else answer(q, want))
    except Exception as e:                            # never the query in the error
        log_query(q, kind, int((time.monotonic() - t0) * 1000), None, type(e).__name__)
        return {"id": mid, "error": type(e).__name__}
    log_query(q, kind, int((time.monotonic() - t0) * 1000), body)
    return {"id": mid, "body": body}


def serve(stdin, stdout, answer, workers: int = 4, info=None) -> None:
    lock = threading.Lock()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        while True:
            msg = read_message(stdin)
            if msg is None:
                break
            pool.submit(lambda m=msg: write_message(stdout, lock, handle(m, answer, info)))


def private_stdout():
    """The message channel on its own fd; fd 1 then points at stderr, so a stray print() or a
    C library writing to stdout (onnxruntime) can never corrupt the framing."""
    import os
    sys.stdout.flush()
    chan = os.fdopen(os.dup(1), "wb", buffering=0)
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    return chan


def warm(answer) -> None:
    """Load the embedding and rerank models now (6 s cold on indexhost), while the page loads —
    not on the user's first keystroke. The warm-up query is fixed; nothing is recorded."""
    try:
        answer("wikipedia", True)
    except Exception:
        pass


if __name__ == "__main__":
    out = private_stdout()
    ans = search_api_answer()
    threading.Thread(target=warm, args=(ans,), daemon=True).start()
    serve(sys.stdin.buffer, out, ans)
