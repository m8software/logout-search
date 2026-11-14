#!/usr/bin/env bash
set -euo pipefail
################################################################################
# Unit test — native-messaging host for the logout search extension
# (apps/search/serve/api/native_host.py). The REAL module, a fake search answer:
#   1. framing: 4-byte native-endian length + JSON, both ways
#   2. ping -> pong; info -> how the engine is reached; details -> engine details; bad json / missing q / oversize -> error replies, the loop survives
#   3. concurrent requests all answered, matched by id
#   4. an exception never echoes the query
#   5. only rendered fields are returned (no scores, stages, espn, news)
#   6. a stray print() and a C-level write to fd 1 after private_stdout() do not reach
#      the message channel
# Usage: bash tests/unit/test-search-native-host.sh
################################################################################
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
NH_DIR="${REPO}/apps/search/serve/api" python3 - <<'PY'
import io, json, os, struct, subprocess, sys, threading, time
sys.path.insert(0, os.environ["NH_DIR"])
import native_host as nh

fails = 0
checks = 0
def check(name, ok, detail=""):
    global fails, checks
    checks += 1
    print(("  PASS " if ok else "  FAIL ") + name + ("" if ok else f"  [{detail}]")); fails += 0 if ok else 1

def frame(obj=None, raw=None):
    b = raw if raw is not None else json.dumps(obj).encode()
    return struct.pack("=I", len(b)) + b

def unframe(buf):
    out, i = [], 0
    while i + 4 <= len(buf):
        (n,) = struct.unpack("=I", buf[i:i+4]); out.append(json.loads(buf[i+4:i+4+n])); i += 4 + n
    return out, i == len(buf)

def fake_answer(q, results):
    if q == "boom secret":
        raise ValueError("failed for boom secret")
    time.sleep(0.05 if q.endswith("1") else 0)
    return nh.trim({"suggest": [{"url": "https://a.example/", "title": "A", "source": "site", "score": 3}],
                    "results": [{"url": "https://a.example/", "title": q, "content": "c", "score": 9, "popular": True}] if results else [],
                    "stages": {"x": 1}, "ms": 3, "espn": {}, "news": [1], "card": None, "answer": None})

stdin = io.BytesIO(b"".join([
    frame({"id": "p", "ping": True}),
    frame(raw=b"{nope"),
    frame({"id": "noq"}),
    frame({"id": "short", "q": "a"}),
    frame({"id": "boom", "q": "boom secret"}),
    *[frame({"id": f"q{i}", "q": f"query {i}", "results": i % 2 == 0}) for i in range(8)],
    frame({"id": "info", "info": True}),
    frame({"id": "det", "details": True}),
    struct.pack("=I", 70000) + b"x" * 70000,
]))
out = io.BytesIO()
nh.serve(stdin, out, fake_answer, info=lambda: {"engine": "testbox", "postgres": "18.6", "articles": 5})
msgs, clean = unframe(out.getvalue())
by = {m.get("id"): m for m in msgs}
check("framing: every reply parses, no stray bytes", clean and len(msgs) == 16, (clean, len(msgs)))
# what a connection is told without asking names nothing about the machine; the details are a request of their own
check("info -> how the engine is reached, nothing about the machine", by.get("info", {}).get("info") == {"via": "", "host_version": nh.VERSION}, by.get("info"))
check("details -> engine details", by.get("det", {}).get("details") == {"engine": "testbox", "postgres": "18.6", "articles": 5} and "info" not in by["det"], by.get("det"))
os.environ["LOGOUT_ENGINE_VIA"] = "dev relay over ssh"
check("info keeps `via` (the badge reads it) and never calls the details", nh.handle({"id": 1, "info": True}, None, lambda: 1 / 0) == {"id": 1, "info": {"via": "dev relay over ssh", "host_version": nh.VERSION}})
del os.environ["LOGOUT_ENGINE_VIA"]
check("details that fail -> error type only", nh.handle({"id": 2, "details": True}, None, lambda: 1 / 0) == {"id": 2, "error": "ZeroDivisionError"})
check("ping -> pong + version", by.get("p", {}).get("pong") is True and by["p"].get("version") == nh.VERSION, by.get("p"))
check("bad json -> error, loop continues", any(m.get("error") == "bad json" for m in msgs))
check("missing q -> error", by.get("noq", {}).get("error") == "q missing", by.get("noq"))
check("1-char q -> empty body", by.get("short", {}).get("body", {}).get("results") == [], by.get("short"))
check("exception -> type only, no query", by.get("boom", {}).get("error") == "ValueError" and "secret" not in json.dumps(by.get("boom")), by.get("boom"))
check("all 8 concurrent queries answered by id", all(f"q{i}" in by and "body" in by[f"q{i}"] for i in range(8)))
check("results flag honoured", by["q0"]["body"]["results"] and by["q1"]["body"]["results"] == [], (by["q0"], by["q1"]))
b = by["q0"]["body"]
check("only rendered fields", set(b) <= set(nh.INSTANT_KEYS) and "score" not in b["results"][0] and "score" not in b["suggest"][0], b)
check("oversize message -> error", any(m.get("error") == "message too large" for m in msgs))

# 6a. "More news" (2026-10-01): its own op, a clamped offset, never the instant answer
got_more = []
def more_answer(q, results, full=False, near=None):
    raise AssertionError("the instant path ran for a news_more request")
more_answer.more_news = lambda q, off: (got_more.append((q, off)), {"news": [{"title": "t", "url": "https://u.example/"}], "more": True})[1]
r1 = nh.handle({"id": "m", "q": "iran", "news_more": True, "offset": 999}, more_answer)
r2 = nh.handle({"id": "m", "q": "iran", "news_more": True, "offset": "5; drop"}, more_answer)
r3 = nh.handle({"id": "m", "q": "iran", "news_more": True, "offset": -3}, more_answer)
check("news_more: the more_news call, offset clamped to 0..100, junk reads 0",
      got_more == [("iran", 100), ("iran", 0), ("iran", 0)] and r1.get("body", {}).get("more") is True, (got_more, r1))
r4 = nh.handle({"id": "m", "q": "iran", "news_more": True}, lambda q, r, f=False, near=None: {"suggest": []})
check("news_more: an engine without it answers an empty page, not an instant body", r4.get("body") == {"news": [], "more": False}, r4)
check("news_more flag survives trim", nh.trim({"news_more": True, "results": []}).get("news_more") is True)
# "How this was ranked" (2026-10-08): its own call, never the instant path, never the query log
got_how = []
def how_answer(q, results, full=False, near=None):
    raise AssertionError("the instant path ran for an explain request")
how_answer.explain = lambda q, near=None: (got_how.append((q, near)), {"steps": [{"step": "merged", "n": 1, "rows": []}], "ms": 5})[1]
import tempfile as _tf
_keep_log, nh.QUERY_LOG = nh.QUERY_LOG, os.path.join(_tf.mkdtemp(), "queries.jsonl")
h1 = nh.handle({"id": "h", "q": "  wells   fargo ", "explain": True, "near": "40.597,-105.871"}, how_answer)
h2 = nh.handle({"id": "h", "q": "wells fargo", "explain": True, "near": "39.9; drop"}, how_answer)
check("explain: the explain call with the tidied query and the position; a malformed position is dropped",
      got_how == [("wells fargo", "40.597,-105.871"), ("wells fargo", None)] and h1.get("body", {}).get("steps") and h1["id"] == "h", [got_how, h1])
h3 = nh.handle({"id": "h", "q": "wells fargo", "explain": True}, lambda q, r, f=False, near=None: {"suggest": []})
check("explain: an engine without it answers no steps, not an instant body", h3.get("body") == {"steps": []}, h3)
def how_fails(q, near=None): raise RuntimeError("secret wells fargo")
how_answer.explain = how_fails
h4 = nh.handle({"id": "h", "q": "wells fargo", "explain": True}, how_answer)
check("explain: a failure answers the error's type, never its text", h4 == {"id": "h", "error": "RuntimeError"}, h4)
check("explain: nothing is added to the query log", not os.path.exists(nh.QUERY_LOG))
nh.handle({"id": "h", "q": "wells fargo"}, lambda q, r, f=False, near=None: {"suggest": [], "results": []})
check("explain: (the log was on: a search does write to it)", os.path.exists(nh.QUERY_LOG))
nh.QUERY_LOG = _keep_log

# 6b. the page's position (2026-10-01): reaches the engine only as a clean "lat,lon"; weather and the kinds box come back
seen_near = []
def near_answer(q, results, full=False, near=None):
    seen_near.append(near)
    return {"suggest": [], "results": [], "weather": {"place": "Marsh, NJ", "now": {"temp_f": 60}}, "kinds": {"title": "T", "sites": []}}
for n in ("40.783,-105.916", "-33.87,151.21", "40,-74", "40.806456,-105.839456"):
    nh.handle({"id": "n", "q": "weather", "results": True, "near": n}, near_answer)
check("near: a clean lat,lon reaches the engine", seen_near == ["40.783,-105.916", "-33.87,151.21", "40,-74", "40.806456,-105.839456"], seen_near)
seen_near.clear()
for bad in ("abc", "40.783,-105.916;drop", "40.783,-105.916\n", "1" * 300, "", "999999,1", " 40.783,-105.916", "40.783, -105.916", 40.1, None, ["40.783,-105.916"]):
    nh.handle({"id": "n", "q": "weather", "results": True, "near": bad}, lambda q, r, f=False, near=None: (seen_near.append(near), {"suggest": [], "results": []})[1])
check("near: anything but lat,lon is dropped, never passed on", seen_near == [] or all(x is None for x in seen_near), seen_near)
seen_near.clear()
nh.handle({"id": "n", "q": "weather", "results": True}, lambda q, r, f=False, near=None: (seen_near.append(near), {"suggest": [], "results": []})[1])
check("near: a request without one calls the engine the old way (no near argument)", seen_near == [None], seen_near)
seen_near.clear()
rf = nh.handle({"id": "n", "q": "weather", "full": True, "near": "40.783,-105.916"}, near_answer)
check("near: a full search carries it too", seen_near == ["40.783,-105.916"], seen_near)
tb = nh.trim({"suggest": [], "results": [], "stages": {"x": 1}, "weather": {"place": "Marsh, NJ"}, "kinds": {"title": "T", "sites": []}, "place": {"a": 1}})
check("weather and the kinds box survive trim (the engine's answer to the page); stages and place do not",
      tb.get("weather", {}).get("place") == "Marsh, NJ" and tb.get("kinds", {}).get("title") == "T" and "stages" not in tb and "place" not in tb, tb)

# 7. settings: defaults, validation, save + reload
import tempfile
os.environ["LOGOUT_SETTINGS"] = os.path.join(tempfile.mkdtemp(), "settings.json")
g = nh.handle({"id": "g", "get_settings": True}, fake_answer)
check("settings default: google, Enter passes through", g["settings"] == nh.DEFAULT_SETTINGS and nh.DEFAULT_SETTINGS["pass"] == "google" and "google" in g["engines"], g)
ok = nh.handle({"id": "s", "set_settings": {"pass": "custom", "custom_url": "https://searx.example/search?q=%s", "enter": "logout"}}, fake_answer)
check("custom SearXNG URL + Enter=logout saved", ok.get("settings", {}).get("pass") == "custom" and nh.load_settings()["enter"] == "logout", ok)
for bad, why in [({"pass": "evil"}, "unknown engine"), ({"pass": "custom", "custom_url": "javascript:alert(1)//%s"}, "javascript: URL"),
                 ({"pass": "custom", "custom_url": "https://x.example/search"}, "no %s"), ({"enter": "maybe"}, "bad enter")]:
    r = nh.handle({"id": "b", "set_settings": bad}, fake_answer)
    check(f"refused: {why}", "error" in r and nh.load_settings()["pass"] == "custom", r)

# 6. stray stdout writes after private_stdout()
code = r'''
import os, sys, ctypes
sys.path.insert(0, os.environ["NH_DIR"])
import native_host as nh
chan = nh.private_stdout()
print("stray print from a library")
os.write(1, b"C-level write to fd 1\n")
import threading
nh.write_message(chan, threading.Lock(), {"id": "x", "ok": True})
'''
p = subprocess.run([sys.executable, "-c", code], capture_output=True, env=dict(os.environ))
msgs2, clean2 = unframe(p.stdout)
check("stray print + fd-1 write stay off the channel", clean2 and msgs2 == [{"id": "x", "ok": True}], p.stdout[:120])
check("...and go to stderr", b"stray print" in p.stderr and b"C-level" in p.stderr, p.stderr[:120])

# a SearchSilo has no Postgres: the details come from its release and its open indexes (2026-11-14: "not available")
import tempfile, types
rd = tempfile.mkdtemp()
with open(os.path.join(rd, "manifest.json"), "w") as f:
    json.dump({"release": "rc-x", "total_bytes": 11_400_000_000}, f)
fake = types.ModuleType("search_api"); fake.POSTGRES = "off"
fake._web_text = types.SimpleNamespace(num_docs={"sites": 1200000, "pages": 3400000, "anchor_docs": 5})
fake._wiki_text = types.SimpleNamespace(num_docs=6800000)
def no_db(): raise AssertionError("asked Postgres")
fake._conn = no_db
real_sa = sys.modules.get("search_api"); sys.modules["search_api"] = fake
os.environ.update(SEARCH_RELEASE_ID="rc-x", SEARCH_RELEASE_DIR=rd, LOGOUT_ENGINE_NAME="indexhost")
try:
    d = nh.engine_info()
    check("silo details: name, release, size and counts, no Postgres",
          d.get("engine") == "indexhost" and d.get("release") == "rc-x" and d.get("data_gb") == 11.4
          and d.get("sites") == 1200000 and d.get("pages") == 3400000 and d.get("articles") == 6800000 and "postgres" not in d, d)
    fake._web_text = fake._wiki_text = None
    d = nh.engine_info()
    check("...an index not opened yet is left out, never opened by asking", "sites" not in d and "articles" not in d and d.get("release") == "rc-x", d)
finally:
    if real_sa is not None: sys.modules["search_api"] = real_sa
    else: sys.modules.pop("search_api", None)

# the engine's name is the computer's friendly name (2026-11-14)
import platform as _pf
saved = {k: os.environ.pop(k, None) for k in ("LOGOUT_ENGINE_NAME",)}
real_node, real_open = _pf.node, open
try:
    os.environ["LOGOUT_ENGINE_NAME"] = "  Living room  "
    check("name: LOGOUT_ENGINE_NAME wins", nh.friendly_name() == "Living room", nh.friendly_name())
    del os.environ["LOGOUT_ENGINE_NAME"]
    _pf.node = lambda: "desktop-k3j.home.lan"
    if sys.platform.startswith("linux") and not os.path.exists("/etc/machine-info"):
        check("name: the hostname without its domain", nh.friendly_name() == "desktop-k3j", nh.friendly_name())
    _pf.node = lambda: ""
    check("name: never empty", nh.friendly_name() not in ("",), nh.friendly_name())
    for raw in ("10-8-0-106", "DESKTOP-K3J9QX2", "localhost.localdomain", "fedora", "3f2a9c1b7d4e"):
        _pf.node = (lambda r=raw: r)
        if sys.platform.startswith("linux") and not os.path.exists("/etc/machine-info"):
            check(f"name: a default or address hostname ({raw}) becomes the user's computer", nh.friendly_name().endswith("'s computer"), nh.friendly_name())
    _pf.node = lambda: "living-room-pc"
    check("name: a real hostname is kept", nh.friendly_name() == "living-room-pc" or os.path.exists("/etc/machine-info"), nh.friendly_name())
finally:
    _pf.node = real_node
    for k, v in saved.items():
        if v is not None: os.environ[k] = v

print(f"\nTotal: {checks}  PASS: {checks - fails}  FAIL: {fails}")
sys.exit(1 if fails else 0)
PY
