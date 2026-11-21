#!/usr/bin/env bash
# requires: cmd:python3
################################################################################
# Unit tests for `logout-search mcp`, the AI-app connector (apps/search/installer/mcp_server.py):
#   1. Both protocol generations: server/discover + per-request _meta (2026-07-28), and the initialize handshake (2025-11-25 and earlier)
#   2. Version and parameter faults answer with the protocol's own error codes
#   3. web_search renders the direct answer, the subject, news and pages; the engine's internal scores never appear
#   4. The engine down or starting is a tool error that says so; only 127.0.0.1 is ever asked (never the public site)
#   5. A query is never written to stderr, on success or failure
#   6. The real command over real pipes: stdout holds JSON lines only, UTF-8 survives, the process ends when stdin closes
#   7. --http: the same answers at /mcp for both generations; the header checks of 2026-07-28; a web page's request
#      (a foreign Origin) is refused; it listens on 127.0.0.1 unless told otherwise; the query is not logged
#
# Usage: bash tests/unit/test-search-mcp.sh
################################################################################
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

REPO="$REPO" TMP="$TMP" python3 - <<'PY'
import contextlib, http.server, io, json, os, subprocess, sys, threading, urllib.error, urllib.request

R = os.environ["REPO"]
sys.path.insert(0, os.path.join(R, "apps/search/installer"))
import mcp_server as m

n = [0]
def ok(name, cond, detail=""):
    n[0] += 1
    if not cond:
        print(f"FAIL {n[0]}: {name} {detail}")
        sys.exit(1)
    print(f"ok {n[0]}: {name}")

BODY = {"suggest": [], "confident": False, "intent": "howto", "ms": 12, "stages": {"x": 1},
        "answer": {"title": "Eiffel Tower", "answer": "March 31, 1889", "detail": "137 years ago", "label": "Opened",
                   "url": "https://en.wikipedia.org/wiki/Eiffel_Tower", "property": "P1619"},
        "card": {"title": "Eiffel Tower", "url": "https://en.wikipedia.org/wiki/Eiffel_Tower", "lead": "A wrought-iron\nlattice tower in Paris."},
        "news": [{"title": "Tower lights go dark", "url": "https://news.example/a", "source": "Example Wire", "published": "2026-11-19T10:00:00+00:00", "score": 6.03}],
        "results": [{"url": f"https://site{i}.example/", "title": f"Page {i}", "content": f"Excerpt {i} café", "source": "site",
                     "score": 47.62, "lr": 0.125, "pos0": 15, "ce": -0.68} for i in range(1, 8)]}
MODERN = {"io.modelcontextprotocol/protocolVersion": "2026-07-28", "io.modelcontextprotocol/clientCapabilities": {}}
seen = []
def fake(q):
    seen.append(q)
    return BODY
def rpc(method, params=None, rid=1, search=fake):
    msg = {"jsonrpc": "2.0", "id": rid, "method": method}
    if params is not None:
        msg["params"] = params
    return m.handle(msg, search, "9.9.9")

# --- 1. both generations
d = rpc("server/discover", {"_meta": MODERN})["result"]
ok("discover names both generations and the tools capability",
   d["resultType"] == "complete" and "2026-07-28" in d["supportedVersions"] and "2025-11-25" in d["supportedVersions"]
   and d["capabilities"] == {"tools": {}} and d["_meta"]["io.modelcontextprotocol/serverInfo"]["version"] == "9.9.9")
t = rpc("tools/list", {"_meta": MODERN})["result"]
ok("modern tools/list: one tool, resultType, serverInfo", t["resultType"] == "complete" and [x["name"] for x in t["tools"]] == ["web_search"]
   and t["tools"][0]["inputSchema"]["required"] == ["query"] and "_meta" in t)
i = rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "c", "version": "1"}})["result"]
ok("initialize answers in the version the older client asked for", i["protocolVersion"] == "2025-06-18" and i["capabilities"] == {"tools": {}}
   and i["serverInfo"]["name"] == "logout-search" and "resultType" not in i)
ok("initialize with a version we do not know offers our newest handshake version",
   rpc("initialize", {"protocolVersion": "1999-01-01", "capabilities": {}})["result"]["protocolVersion"] == "2025-11-25")
ok("discover and the modern tool list carry the caching fields a 2026-07-28 client requires",
   all(x["cacheScope"] == "public" and x["ttlMs"] > 0 for x in (d, t)))
lt = rpc("tools/list")["result"]
ok("legacy tools/list carries no modern-only fields", "resultType" not in lt and "_meta" not in lt and "ttlMs" not in lt and lt["tools"][0]["name"] == "web_search")
ok("notifications get no reply", m.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}, fake, "v") is None
   and m.handle({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 3}}, fake, "v") is None)
ok("ping answers", rpc("ping")["result"] == {})

# --- 2. faults
e = rpc("tools/list", {"_meta": {**MODERN, "io.modelcontextprotocol/protocolVersion": "2031-01-01"}})["error"]
ok("an unknown version: -32022 with what we support", e["code"] == -32022 and e["data"]["requested"] == "2031-01-01" and "2026-07-28" in e["data"]["supported"])
ok("a modern request without client capabilities: -32602",
   rpc("tools/list", {"_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}})["error"]["code"] == -32602)
ok("unknown tool: -32602", rpc("tools/call", {"name": "delete_everything", "arguments": {}})["error"]["code"] == -32602)
ok("unknown method: -32601", rpc("resources/list")["error"]["code"] == -32601)
ok("not a request: -32600", m.handle({"hello": 1}, fake, "v")["error"]["code"] == -32600)
ok("a stray reply is ignored", m.handle({"jsonrpc": "2.0", "id": 4, "result": {}}, fake, "v") is None)

# --- 3. the tool
seen.clear()
r = rpc("tools/call", {"name": "web_search", "arguments": {"query": "  when was the eiffel tower built  ", "max_results": 3}, "_meta": MODERN})["result"]
text = r["content"][0]["text"]
ok("the engine is asked the trimmed query once", seen == ["when was the eiffel tower built"], seen)
ok("direct answer, subject, news and pages are all there",
   r["isError"] is False and r["resultType"] == "complete" and "Direct answer: March 31, 1889 (137 years ago)" in text
   and "About Eiffel Tower: A wrought-iron lattice tower in Paris." in text and "Tower lights go dark (Example Wire, 2026-11-19)" in text
   and "1. Page 1\n   https://site1.example/\n   Excerpt 1 café" in text, text)
ok("the result ends by saying these are excerpts and a repeat returns the same ones", text.rstrip().endswith("returns the same excerpts.") and "No results" not in text)
ok("max_results limits the pages", "3. Page 3" in text and "4. Page 4" not in text)
ok("internal scores and timings never reach the model", not any(x in text for x in ("47.62", "0.125", "-0.68", "P1619", "stages", "6.03")))
ok("default is five pages", "5. Page 5" in rpc("tools/call", {"name": "web_search", "arguments": {"query": "eiffel"}})["result"]["content"][0]["text"]
   and "6. Page 6" not in rpc("tools/call", {"name": "web_search", "arguments": {"query": "eiffel"}})["result"]["content"][0]["text"])
ok("a null answer is left out", "Direct answer" not in m.render({**BODY, "answer": {"title": "X", "answer": None, "detail": "No height on record"}}, 5))
ok("nothing found says so", "No results" in m.render({"results": []}, 5))
for loose in ("3", 3.0):
    lt3 = rpc("tools/call", {"name": "web_search", "arguments": {"query": "eiffel", "max_results": loose}})["result"]
    ok(f"max_results sent as {loose!r} is read as 3", lt3["isError"] is False and "3. Page 3" in lt3["content"][0]["text"] and "4. Page 4" not in lt3["content"][0]["text"])
se = m.render({"results": [{"url": "https://diy.example/q/1", "title": "How do I?", "content": "Loosen the nuts first.", "licence": "CC BY-SA 2.5",
                            "answer_author": "freiheit", "answer_url": "https://diy.example/a/2", "sig": "abc"},
                           {"url": "https://diy.example/q/3", "title": "No excerpt", "content": "", "licence": "CC BY-SA 4.0", "answer_author": "x"}]}, 5)
ok("an answer excerpt carries its author, licence and link; a row without an excerpt carries no credit",
   "Excerpt by freiheit, CC BY-SA 2.5: https://diy.example/a/2" in se and se.count("Excerpt by") == 1 and "abc" not in se, se)
ok("a subject with a description and no lead still gets its line", "About X: a thing" in m.render({"card": {"title": "X", "description": "a thing"}}, 5))
for bad in ({}, {"query": "a"}, {"query": 7}, {"query": "ok words", "max_results": "three"}, {"query": "ok words", "max_results": 2.5}, {"query": "ok words", "max_results": True}):
    br = rpc("tools/call", {"name": "web_search", "arguments": bad})["result"]
    ok(f"bad arguments {bad} are a tool error the model can read", br["isError"] is True and "`" in br["content"][0]["text"])
long_q = "w" * 500
seen.clear(); rpc("tools/call", {"name": "web_search", "arguments": {"query": long_q}})
ok("an over-long query is cut to the engine's limit", len(seen[0]) == 200)

# --- 4. the engine down or starting; only this machine is asked
asked = []
real = urllib.request.urlopen
def refuse(url, timeout=None):
    asked.append(url); raise urllib.error.URLError(ConnectionRefusedError(111, "refused"))
def starting(url, timeout=None):
    asked.append(url); raise urllib.error.HTTPError(url, 503, "starting", {}, io.BytesIO(b'{"error":"starting"}'))
def slow(url, timeout=None):
    asked.append(url); raise urllib.error.URLError(TimeoutError("timed out"))
for fn, want in ((refuse, "not running"), (starting, "still starting"), (slow, "did not answer")):
    urllib.request.urlopen = fn
    try:
        dr = rpc("tools/call", {"name": "web_search", "arguments": {"query": "private matter"}}, search=m.engine_search(8090))["result"]
    finally:
        urllib.request.urlopen = real
    ok(f"engine {want}: a tool error that says so", dr["isError"] is True and want in dr["content"][0]["text"], dr)
ok("every request went to 127.0.0.1 and nowhere else", len(asked) == 3 and all(u.startswith("http://127.0.0.1:8090/instant?q=private+matter") for u in asked), asked)
ok("it is asked as a submitted search (full=1), so a one-word query gets pages", all(u.endswith("&full=1") for u in asked), asked)
src = open(os.path.join(R, "apps/search/installer/mcp_server.py")).read()
ok("the source asks one place: 127.0.0.1, or the public site only where the demo switch is on",
   src.count("urlopen(") == 1 and src.count("http://127.0.0.1:{port}/instant") == 1 and src.count("DEMO_URL if DEMO else") == 1
   and src.count('DEMO = os.environ.get("LOGOUT_MCP_DEMO") == "1"') == 1)
ok("demo is off unless the environment says so, and then the tool promises the query stays here",
   m.DEMO is False and "never leaves this machine" in m.TOOL["description"] and "search.logout.cloud" not in m.TOOL["description"] + m.INSTRUCTIONS + m.TOOL["title"])
import subprocess
demo = subprocess.run([sys.executable, "-c", "import json,sys; sys.path.insert(0, sys.argv[1]); import mcp_server as d; print(json.dumps([d.DEMO, d.TOOL['title'], d.TOOL['description'], d.INSTRUCTIONS]))",
                       os.path.join(R, "apps/search/installer")], env={**os.environ, "LOGOUT_MCP_DEMO": "1"}, capture_output=True, text=True)
dj = json.loads(demo.stdout or "[]")
ok("demo on: title, description and instructions all say the query goes to search.logout.cloud, none promises it stays",
   len(dj) == 4 and dj[0] is True and all("search.logout.cloud" in x for x in dj[1:]) and not any("never leaves" in x or "stay on this machine" in x for x in dj[1:]), dj)
off = subprocess.run([sys.executable, "-c", "import sys; sys.path.insert(0, sys.argv[1]); import mcp_server as d; print(d.DEMO)",
                      os.path.join(R, "apps/search/installer")], env={**os.environ, "LOGOUT_MCP_DEMO": "true"}, capture_output=True, text=True)
ok("demo needs exactly LOGOUT_MCP_DEMO=1: another value leaves it off", off.stdout.strip() == "False", off.stdout)

# --- cancellation: no reply follows for a request the client gave up on
import time
gate = threading.Event()
def held(q):
    gate.wait(5); return BODY
rd, wr = os.pipe(); out_r, out_w = os.pipe()
th = threading.Thread(target=m.serve, args=(os.fdopen(rd, "rb"), os.fdopen(out_w, "wb", buffering=0), held, "v"), daemon=True); th.start()
w = os.fdopen(wr, "wb", buffering=0)
w.write(json.dumps({"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "web_search", "arguments": {"query": "slow one"}}}).encode() + b"\n")
time.sleep(0.2)
w.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 7}}).encode() + b"\n")
w.write(json.dumps({"jsonrpc": "2.0", "id": 8, "method": "ping"}).encode() + b"\n")
time.sleep(0.2); gate.set(); w.close(); th.join(10)
got_ids = [json.loads(l).get("id") for l in os.fdopen(out_r, "rb").read().decode().splitlines()]
ok("a cancelled request gets no reply; the next request still does", got_ids == [8], got_ids)

# --- 5 and 6. the real command over real pipes, against a stand-in engine
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        data = json.dumps(BODY).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
    def log_message(self, *a):
        pass
srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
port = srv.server_address[1]
SECRET = "zebra-umbrella-9417 naïve"
lines = [{"jsonrpc": "2.0", "id": "d", "method": "server/discover", "params": {"_meta": MODERN}},
         {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25", "capabilities": {}}},
         {"jsonrpc": "2.0", "method": "notifications/initialized"},
         {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "web_search", "arguments": {"query": SECRET}}}]
LS = os.path.join(R, "apps/search/installer/logout_search.py")
def run(port, extra=b""):
    p = subprocess.run([sys.executable, LS, "mcp", "--port", str(port)], capture_output=True, timeout=60,
                       input=("\n".join(json.dumps(x, ensure_ascii=False) for x in lines) + "\n").encode("utf-8") + extra)
    return p, [json.loads(l) for l in p.stdout.decode("utf-8").splitlines()]
p, out = run(port, b"this is not json\n")
by = {x.get("id"): x for x in out}
ok("the command exits 0 when stdin closes", p.returncode == 0, p.stderr.decode()[-400:])
ok("stdout is JSON lines only: three replies and one parse error", len(out) == 4 and by[None]["error"]["code"] == -32700 and set(by) == {"d", 1, 2, None}, out)
ok("the search came back through the pipes with UTF-8 intact", "Excerpt 1 café" in by[2]["result"]["content"][0]["text"])
ok("the query is not on stderr (engine up)", SECRET.encode() not in p.stderr and b"zebra" not in p.stderr, p.stderr[-300:])
srv.shutdown()
p2, out2 = run(1)
ok("with no engine the tool says so and exits 0", p2.returncode == 0 and {x.get("id"): x for x in out2}[2]["result"]["isError"] is True)
ok("the query is not on stderr (engine down)", b"zebra" not in p2.stderr and b"zebra" not in p2.stdout.replace(SECRET.encode(), b""), p2.stderr[-300:])
c = json.loads(subprocess.run([sys.executable, LS, "mcp", "--print-config", "--port", "8123"], capture_output=True, text=True, timeout=60).stdout)
ok("--print-config gives the settings a client needs", c["mcpServers"]["logout-search"]["args"][-3:] == ["mcp", "--port", "8123"]
   and os.path.isabs(c["mcpServers"]["logout-search"]["command"]))

# --- 7. over HTTP
import base64, socket
hs = m.serve_http(("127.0.0.1", 0), lambda q: BODY, "9.9", origins=("chat.home.example",))
threading.Thread(target=hs.serve_forever, daemon=True).start()
hport = hs.server_address[1]
def post(msg, headers=None, path="/mcp", ctype="application/json", raw=None, method="POST", port=None):
    h = {"Accept": "application/json, text/event-stream"}
    if ctype:
        h["Content-Type"] = ctype
    h.update(headers or {})
    data = raw if raw is not None else (None if msg is None else json.dumps(msg, ensure_ascii=False).encode("utf-8"))
    req = urllib.request.Request(f"http://127.0.0.1:{port or hport}{path}", data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            body = r.read(); return r.status, (json.loads(body) if body else None), r.headers
    except urllib.error.HTTPError as e:
        body = e.read(); return e.code, (json.loads(body) if body else None), e.headers
def modern(method, params=None, **over):
    params = {**(params or {}), "_meta": MODERN}
    h = {"MCP-Protocol-Version": "2026-07-28", "Mcp-Method": method}
    if method == "tools/call":
        h["Mcp-Name"] = params.get("name", "")
    h.update(over)
    return post({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, {k: v for k, v in h.items() if v is not None})
CALL = {"name": "web_search", "arguments": {"query": "eiffel tower"}}
st, d, hd = modern("server/discover")
ok("http: server/discover answers 200 with one JSON object and the caching fields", st == 200 and hd.get_content_type() == "application/json"
   and d["result"]["ttlMs"] > 0 and d["result"]["_meta"][m.META_SERVER]["version"] == "9.9", (st, d))
st, d, _ = modern("tools/call", CALL)
ok("http: tools/call returns the search", st == 200 and "Excerpt 1 café" in d["result"]["content"][0]["text"] and d["result"]["resultType"] == "complete", (st, d))
st, d, _ = modern("tools/call", CALL, **{"Mcp-Name": "=?base64?" + base64.b64encode(b"web_search").decode() + "?="})
ok("http: a Base64 Mcp-Name is decoded before it is compared", st == 200 and d["result"]["isError"] is False, (st, d))
for label, over in (("Mcp-Name differs", {"Mcp-Name": "other_tool"}), ("Mcp-Name is missing", {"Mcp-Name": None}), ("Mcp-Method differs", {"Mcp-Method": "tools/list"}),
                    ("MCP-Protocol-Version is missing", {"MCP-Protocol-Version": None}), ("MCP-Protocol-Version differs", {"MCP-Protocol-Version": "2025-11-25"})):
    st, d, _ = modern("tools/call", CALL, **over)
    ok(f"http: {label} -> 400 HeaderMismatch, and no search", st == 400 and d["error"]["code"] == -32020 and d["id"] == 1, (st, d))
st, d, _ = post({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"_meta": {**MODERN, m.META_VERSION: "2030-01-01"}}},
                {"MCP-Protocol-Version": "2030-01-01", "Mcp-Method": "tools/list"})
ok("http: an unknown version -> 400 with the versions we speak", st == 400 and d["error"]["code"] == -32022 and "2026-07-28" in d["error"]["data"]["supported"], (st, d))
st, d, _ = modern("resources/list")
ok("http: an unknown method -> 404 with the JSON-RPC error", st == 404 and d["error"]["code"] == -32601, (st, d))
st, d, _ = post({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25", "capabilities": {}}})
ok("http: the initialize handshake is answered, with no session id", st == 200 and d["result"]["protocolVersion"] == "2025-11-25" and _.get("Mcp-Session-Id") is None, (st, d))
st, d, _ = post({"jsonrpc": "2.0", "method": "notifications/initialized"}, {"MCP-Protocol-Version": "2025-11-25"})
ok("http: a notification -> 202 and no body", st == 202 and d is None, (st, d))
st, d, _ = post({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": CALL}, {"MCP-Protocol-Version": "2025-11-25"})
ok("http: a handshake-generation tools/call returns the search", st == 200 and "Excerpt 1 café" in d["result"]["content"][0]["text"] and "resultType" not in d["result"], (st, d))
st, d, _ = post({"jsonrpc": "2.0", "id": 2, "method": "nope"}, {"MCP-Protocol-Version": "2025-11-25"})
ok("http: there an unknown method is a JSON-RPC error on a 200", st == 200 and d["error"]["code"] == -32601, (st, d))
st, d, hd = post(None, method="GET", ctype=None)
ok("http: GET /mcp -> 405 (there is no stream to open)", st == 405 and hd.get("Allow") == "POST", (st, d))
st, d, _ = post(None, method="GET", ctype=None, path="/healthz")
ok("http: /healthz answers", st == 200 and d == {"ok": True})
st, d, _ = post({"jsonrpc": "2.0", "id": 1, "method": "ping"}, path="/other")
ok("http: another path -> 404", st == 404, (st, d))
asked_http = []
hs2 = m.serve_http(("127.0.0.1", 0), lambda q: (asked_http.append(q), BODY)[1], "v")
threading.Thread(target=hs2.serve_forever, daemon=True).start()
p2port = hs2.server_address[1]
legacy_call = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": CALL}
for origin in ("https://evil.example", "http://evil.example:%d" % p2port, "null", "http://chat.home.example"):
    st, d, _ = post(legacy_call, {"Origin": origin}, port=p2port)
    ok(f"http: a request from the page {origin} -> 403, and no search", st == 403 and not asked_http, (st, d, asked_http))
st, d, _ = post(None, method="OPTIONS", ctype=None, headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"}, port=p2port)
ok("http: a browser's permission request from another site is refused", st == 403 and _.get("Access-Control-Allow-Origin") is None, (st, d))
st, d, _ = post(legacy_call, raw=json.dumps(legacy_call).encode(), ctype="text/plain", port=p2port)
ok("http: a body that is not application/json -> 415, and no search (a web form cannot send that type unasked)", st == 415 and not asked_http, (st, d))
for origin in ("http://localhost:3000", "http://127.0.0.1:8080", "http://[::1]:3000"):
    st, d, _ = post(legacy_call, {"Origin": origin}, port=p2port)
    ok(f"http: a page on this computer ({origin}) is answered", st == 200 and d["result"]["isError"] is False, (st, d))
st, d, _ = post(legacy_call, {"Origin": "http://chat.home.example"})
ok("http: a host the person listed (--allow-origin) is answered", st == 200, (st, d))
st, d, _ = post(None, raw=b"{not json", port=p2port)
ok("http: a body that is not JSON -> 400 parse error", st == 400 and d["error"]["code"] == -32700, (st, d))
st, d, _ = post(None, raw=b"x" * (m.HTTP_MAX_BODY + 1), port=p2port)
ok("http: an oversized body -> 413", st == 413, (st, d))
ok("--http PORT alone listens on this computer only", m.parse_listen("8091") == ("127.0.0.1", 8091) and m.parse_listen("0.0.0.0:8091") == ("0.0.0.0", 8091)
   and m.parse_listen("[::1]:8091") == ("::1", 8091))
hs.shutdown(); hs2.shutdown()

# the real command: listens, answers, logs no query
srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
with socket.socket() as sk:
    sk.bind(("127.0.0.1", 0)); free = sk.getsockname()[1]
proc = subprocess.Popen([sys.executable, LS, "mcp", "--port", str(srv.server_address[1]), "--http", str(free)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
try:
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", free), timeout=0.2).close(); break
        except OSError:
            time.sleep(0.1)
    st, d, _ = post({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "web_search", "arguments": {"query": SECRET}}}, port=free)
    ok("the command with --http answers a search", st == 200 and "Excerpt 1 café" in d["result"]["content"][0]["text"], (st, d))
finally:
    proc.terminate(); so, se = proc.communicate(timeout=20)
ok("the query is on neither stdout nor stderr (--http)", b"zebra" not in so + se and b"/mcp" in se, se[-300:])
bad = subprocess.run([sys.executable, LS, "mcp", "--http", "nonsense"], capture_output=True, timeout=60)
ok("a bad --http value exits 2 and says what it wants", bad.returncode == 2 and b"[HOST:]PORT" in bad.stderr, bad.stderr)
srv.shutdown()
print(f"\nall {n[0]} passed")
PY
