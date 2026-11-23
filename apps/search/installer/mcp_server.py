#!/usr/bin/env python3
"""`logout-search mcp`: web search for an AI app on this computer, answered by the engine on this computer.

An AI app that speaks the Model Context Protocol (a desktop chat app, an editor, a local-model front end) starts this
program itself and talks to it over stdin/stdout: one JSON-RPC message per line, UTF-8. It offers one tool, `web_search`,
and answers it by asking the running engine on 127.0.0.1:PORT, the same engine the search page uses.

`--http [HOST:]PORT` serves the same thing at http://HOST:PORT/mcp instead (the protocol's Streamable HTTP transport),
for an AI app that cannot start a program: one in a container, or one that only takes an address. It listens on
127.0.0.1 unless told otherwise, answers a POST with one JSON object, and refuses a request a web page sent (an Origin
that is not this computer), so a site open in the browser cannot search through it.

What it never does:
  * ask anything but 127.0.0.1. When the engine is not running the tool says so; it does not fall back to the public
    site, because a search the user believed stayed on this machine would then leave it.
    The one exception is the demo mode, which only the person starting the program can turn on (LOGOUT_MCP_DEMO=1,
    for a directory's test container that has no engine): searches then go to search.logout.cloud, and the server's
    instructions, the tool's title and its description all say so instead of promising the query stays here.
  * write a query anywhere. stderr carries method names and error types only. (The engine does not either: its
    result store is written only by its own warm-up requests, never by a search that comes in.)
  * fetch a result's page. It returns what the index holds (title, address, an excerpt of about 300 characters).

Two generations of the protocol are served from the same process, chosen by how the client opens:
  * 2026-07-28 and later: no handshake; each request names its version in `_meta`, and `server/discover` describes us.
  * 2025-11-25 and earlier: the `initialize` handshake.

Standard library only.
"""
from __future__ import annotations

import base64
import json
import os
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODERN = ("2026-07-28",)
LEGACY = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
META_VERSION = "io.modelcontextprotocol/protocolVersion"
META_CAPS = "io.modelcontextprotocol/clientCapabilities"
META_SERVER = "io.modelcontextprotocol/serverInfo"

NAME = "logout-search"
TITLE = "Logout Search"
DEMO = os.environ.get("LOGOUT_MCP_DEMO") == "1"      # set by whoever starts the program, never by a request
DEMO_URL = "https://search.logout.cloud/api/instant"
INSTALL = "https://github.com/m8software/logout-search"
INSTRUCTIONS = (("DEMO MODE: no Logout Search engine is installed here, so each query is sent to the public site "
                 f"search.logout.cloud and answered there. Install Logout Search ({INSTALL}) for searches that stay on "
                 "your own computer. ") if DEMO else
                "Web search answered from the Logout Search index stored on this computer. Queries stay on this machine. ") \
    + "Results are titles, addresses and short excerpts; the pages themselves are not fetched."
MAX_RESULTS = 10
DEFAULT_RESULTS = 5
MAX_QUERY = 200          # the engine's own limit
CACHE_MS = 3_600_000     # how long a client may reuse our description and tool list
NOTE = ("Note: these are excerpts from the index, not the pages. To read a page in full, open its address with a "
        "page-reading tool if you have one; searching again with the same words returns the same excerpts.")
TIMEOUT_S = 30           # a first search after start-up has taken 7 s on a slow disk

TOOL = {
    "name": "web_search",
    "title": "Web search (demo: answered by search.logout.cloud)" if DEMO else "Web search (on this computer)",
    "description": (
        ("Search the web with Logout Search in demo mode: the query is sent to the public site search.logout.cloud "
         "(an installed copy answers on your own computer instead). " if DEMO else
         "Search the web using the Logout Search index stored on this computer. The query never leaves this machine. ") +
        "Returns a direct answer when the index has one (a date, a person, a number), a short summary of the main "
        "subject when there is one, recent news headlines when the query is in the news, and a ranked list of pages: "
        "title, address and an excerpt of about 300 characters. Pages are not fetched, so quote only what the "
        "excerpts say. Works best with a short query of the key words, or one plain question."),
    "inputSchema": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "minLength": 2, "maxLength": MAX_QUERY,
                      "description": "What to search for: key words or one plain question."},
            "max_results": {"type": "integer", "minimum": 1, "maximum": MAX_RESULTS, "default": DEFAULT_RESULTS,
                            "description": "How many pages to list."},
        },
        "required": ["query"],
        "additionalProperties": False,
    },
    "annotations": {"readOnlyHint": True, "idempotentHint": True},
}


class ToolError(Exception):
    """Something the model or the user can act on; returned inside the tool result with isError."""


# ---------------------------------------------------------------------------------------------- the engine

def engine_search(port: int):
    """query -> the engine's answer, over HTTP on this machine only."""
    def search(query: str) -> dict:
        # full=1: a submitted search. Without it the engine treats the text as still being typed and a short query
        # ("iran", "rust") gets suggestions and no pages.
        url = f"{DEMO_URL if DEMO else f'http://127.0.0.1:{port}/instant'}?{urllib.parse.urlencode({'q': query, 'full': '1'})}"
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "logout-search-mcp"}) if DEMO else url, timeout=TIMEOUT_S) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 503:
                raise ToolError("Logout Search is still starting on this computer. Try again in a minute.") from None
            raise ToolError(f"Logout Search answered with an error ({e.code}). Nothing was searched.") from None
        except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as e:
            if isinstance(e, TimeoutError) or "timed out" in str(getattr(e, "reason", e)):
                raise ToolError(f"Logout Search did not answer within {TIMEOUT_S} seconds. Try again.") from None
            if DEMO:
                raise ToolError("The demo could not reach search.logout.cloud, so nothing was searched. Try again.") from None
            raise ToolError("Logout Search is not running on this computer, so nothing was searched. "
                            "Start it (the Logout Search entry in the start menu, or `logout-search start`) and try again. "
                            f"Not installed yet? Get it at {INSTALL}, or try the same engine in a browser at "
                            "https://search.logout.cloud.") from None
        except ValueError:
            raise ToolError("Logout Search sent an answer that could not be read. Nothing was searched.") from None
    return search


def _one_line(text) -> str:
    return " ".join(str(text or "").split())


def _flight_line(it: dict) -> str:
    """One item of an airport's status as a phrase."""
    mins = lambda k: f"{it[k]} min" if isinstance(it.get(k), int) else ""
    why = f" ({_one_line(it.get('reason'))})" if it.get("reason") else ""
    kind = it.get("kind")
    if kind == "ground_stop":
        return "ground stop" + (f" until {_one_line(it.get('until'))}" if it.get("until") else "") + why
    if kind == "ground_delay":
        return "ground delay" + (f" averaging {mins('avg')}" if mins("avg") else "") + (f", up to {mins('max')}" if mins("max") else "") + why
    if kind in ("departure", "arrival"):
        span = " to ".join(x for x in (mins("min"), mins("max")) if x)
        return f"{kind} delays" + (f" of {span}" if span else "") + (f", {_one_line(it.get('trend'))}" if it.get("trend") else "") + why
    if kind == "closure":
        return "airport closed" + (f", reopens {_one_line(it.get('reopen'))}" if it.get("reopen") else "")
    if kind == "notice":
        return f"notice: {_one_line(it.get('text'))}" if it.get("text") else ""
    return ""


def render(body: dict, limit: int) -> str:
    """The engine's answer as text for a model: the direct answer, the subject, news, then the pages."""
    out = []
    a = body.get("answer") or {}
    if a.get("answer"):
        line = f"Direct answer: {_one_line(a['answer'])}"
        if a.get("detail"):
            line += f" ({_one_line(a['detail'])})"
        about = " / ".join(x for x in (_one_line(a.get("title")), _one_line(a.get("label"))) if x)
        if about:
            line += f" [{about}]"
        if a.get("url"):
            line += f"\nSource: {a['url']}"
        out.append(line)
    c = body.get("card") or {}
    about = c.get("lead") or c.get("description")
    if c.get("title") and about:
        out.append(f"About {_one_line(c['title'])}: {_one_line(about)}" + (f"\nSource: {c['url']}" if c.get("url") else ""))
    fl = body.get("flights") or {}
    if fl.get("airports"):                       # the FAA's airport status, from the engine's own table
        rows = ["Airport status (FAA" + (f", {fl['age_min']} min old" if isinstance(fl.get("age_min"), int) else "") + "):"]
        for a in fl["airports"][:3]:
            name = f"{_one_line(a.get('name'))} ({_one_line(a.get('code'))})"
            if fl.get("stale"):
                rows.append(f"- {name}: the status is too old to show")
                continue
            lines = [_flight_line(it) for it in (a.get("items") or [])]
            rows.append(f"- {name}: " + ("; ".join(x for x in lines if x) or "no delays reported"))
        if fl.get("link"):
            rows.append(f"Source: {fl['link']}")
        out.append("\n".join(rows))
    ot = body.get("ontime") or {}
    if ot.get("routes"):                         # a flight number's record in the government's monthly file
        rows = [f"{_one_line(ot.get('airline'))} {ot.get('number')}, on-time record for {_one_line(ot.get('month'))} (not live status):"]
        for r in ot["routes"][:3]:
            bits = [f"usually departs {_one_line(r['dep'])}" if r.get("dep") else "", f"arrives {_one_line(r['arr'])}" if r.get("arr") else "",
                    f"on time {r['ontime_pct']}% of {r.get('flights')} flights" if isinstance(r.get("ontime_pct"), int) else f"flew {r.get('flights')} times",
                    f"{r['cancelled']} cancelled" if r.get("cancelled") else ""]
            rows.append(f"- {_one_line(r.get('from'))} to {_one_line(r.get('to'))}: " + ", ".join(b for b in bits if b))
        if ot.get("link"):
            rows.append(f"Source: {ot['link']}")
        out.append("\n".join(rows))
    news = [n for n in (body.get("news") or []) if n.get("title") and n.get("url")][:5]
    if news:
        rows = ["Recent news:"]
        for n in news:
            when = str(n.get("published") or "")[:10]
            by = _one_line(n.get("source"))
            rows.append(f"- {_one_line(n['title'])}" + (f" ({', '.join(x for x in (by, when) if x)})" if by or when else "") + f"\n  {n['url']}")
        out.append("\n".join(rows))
    pages = [r for r in (body.get("results") or []) if r.get("url")][:limit]
    if pages:
        rows = ["Pages:"]
        for i, r in enumerate(pages, 1):
            row = f"{i}. {_one_line(r.get('title')) or r['url']}\n   {r['url']}" + (f"\n   {_one_line(r.get('content'))}" if r.get("content") else "")
            if r.get("content") and r.get("licence"):      # an excerpt of someone's answer (CC BY-SA): the credit travels with it
                who = _one_line(r.get("answer_author") or r.get("author"))
                row += "\n   Excerpt" + (f" by {who}" if who else "") + f", {_one_line(r['licence'])}" + (f": {r['answer_url']}" if r.get("answer_url") else "")
            rows.append(row)
        out.append("\n".join(rows))
    if body.get("corrected"):
        out.insert(0, f"Showing results for: {_one_line(body['corrected'])}")
    if not out:
        return "No results in the index for this query. Try fewer or different words."
    out.append(NOTE)                             # what this tool cannot do, so a model reaches for a page-reading tool instead of repeating itself
    return "\n\n".join(out)


def call_tool(params: dict, search) -> dict:
    """tools/call for web_search -> the tool result (never raises for a failed search: isError carries it)."""
    args = params.get("arguments")
    args = args if isinstance(args, dict) else {}
    try:
        q = args.get("query")
        if not isinstance(q, str) or len(q.strip()) < 2:
            raise ToolError("`query` is needed: at least two characters of text.")
        n = args.get("max_results", DEFAULT_RESULTS)
        if isinstance(n, str) and n.strip().isdigit():        # small local models send "5" or 5.0 for 5
            n = int(n)
        elif isinstance(n, float) and n.is_integer():
            n = int(n)
        if n is None:
            n = DEFAULT_RESULTS
        if isinstance(n, bool) or not isinstance(n, int):
            raise ToolError(f"`max_results` must be a whole number from 1 to {MAX_RESULTS}.")
        text = render(search(q.strip()[:MAX_QUERY]), max(1, min(MAX_RESULTS, n)))
        return {"content": [{"type": "text", "text": text}], "isError": False}
    except ToolError as e:
        return {"content": [{"type": "text", "text": str(e)}], "isError": True}


# ---------------------------------------------------------------------------------------------- the protocol

class RpcError(Exception):
    def __init__(self, code: int, message: str, data=None):
        super().__init__(message)
        self.code, self.message, self.data = code, message, data


def _server_info(version: str) -> dict:
    return {"name": NAME, "title": TITLE, "version": version}


def dispatch(method: str, params: dict, search, version: str) -> dict:
    """One request -> its result. Raises RpcError for a protocol fault."""
    meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
    modern = META_VERSION in meta
    if modern:
        asked = meta.get(META_VERSION)
        if asked not in MODERN:
            raise RpcError(-32022, "Unsupported protocol version", {"supported": list(MODERN + LEGACY), "requested": asked})
        if not isinstance(meta.get(META_CAPS), dict):
            raise RpcError(-32602, f"Invalid params: _meta is missing {META_CAPS}")

    def done(result: dict, cacheable: bool = False) -> dict:
        if modern:                                   # per-response fields of the handshake-free protocol
            result = {"resultType": "complete", **result, "_meta": {META_SERVER: _server_info(version)}}
            if cacheable:                            # required on discover and the lists: the same for every caller, and fixed for this program's life
                result.update(ttlMs=CACHE_MS, cacheScope="public")
        return result

    if method == "server/discover":
        return done({"supportedVersions": list(MODERN + LEGACY), "capabilities": {"tools": {}}, "instructions": INSTRUCTIONS}, cacheable=True)
    if method == "initialize":                       # 2025-11-25 and earlier
        asked = params.get("protocolVersion")
        return {"protocolVersion": asked if asked in LEGACY else LEGACY[0], "capabilities": {"tools": {}},
                "serverInfo": _server_info(version), "instructions": INSTRUCTIONS}
    if method == "ping":
        return done({})
    if method == "tools/list":
        return done({"tools": [TOOL]}, cacheable=True)
    if method == "tools/call":
        if params.get("name") != TOOL["name"]:
            raise RpcError(-32602, f"Unknown tool: {_one_line(params.get('name'))[:60]}")
        return done(call_tool(params, search))
    raise RpcError(-32601, f"Method not found: {_one_line(method)[:60]}")


def handle(msg, search, version: str):
    """One decoded message -> the reply to write, or None (a notification, or something that is not a request)."""
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method"), str):
        if isinstance(msg, dict) and ("result" in msg or "error" in msg):
            return None                              # a reply to a request we never send: nothing to say
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}}
    if "id" not in msg:
        return None                                  # a notification (initialized, cancelled): no reply
    rid = msg["id"]
    params = msg.get("params")
    try:
        if params is not None and not isinstance(params, dict):
            raise RpcError(-32602, "Invalid params: an object is expected")
        return {"jsonrpc": "2.0", "id": rid, "result": dispatch(msg["method"], params or {}, search, version)}
    except RpcError as e:
        err = {"code": e.code, "message": e.message}
        if e.data is not None:
            err["data"] = e.data
        return {"jsonrpc": "2.0", "id": rid, "error": err}
    except Exception as e:                           # never the query in the error
        print(f"logout-search mcp: {msg['method']} failed: {type(e).__name__}", file=sys.stderr)
        return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32603, "message": f"Internal error: {type(e).__name__}"}}


def serve(stdin, stdout, search, version: str = "dev", workers: int = 4) -> None:
    """Read messages from `stdin` (binary) until it closes; write replies to `stdout` (binary), one per line."""
    lock = threading.Lock()
    cancelled: set = set()                           # ids the client gave up on: no reply may follow (stdio cancellation)

    def write(reply) -> None:
        if reply is None:
            return
        data = json.dumps(reply, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"   # json never emits a raw newline
        with lock:
            stdout.write(data)
            stdout.flush()

    def answer(msg) -> None:
        if isinstance(msg, list):                    # a batch (2025-03-26 allowed them)
            replies = [r for r in (handle(m, search, version) for m in msg) if r is not None]
            write(replies or None)
        else:
            reply = handle(msg, search, version)
            if reply is not None and isinstance(msg, dict) and "id" in msg:
                with lock:
                    if msg["id"] in cancelled:
                        cancelled.discard(msg["id"])
                        return
            write(reply)

    def note_cancel(msg) -> bool:
        if isinstance(msg, dict) and msg.get("method") == "notifications/cancelled" and "id" not in msg:
            rid = (msg.get("params") or {}).get("requestId") if isinstance(msg.get("params"), dict) else None
            if isinstance(rid, (str, int)) and not isinstance(rid, bool):
                with lock:
                    if len(cancelled) < 1000:        # a client that only ever cancels cannot grow this without bound
                        cancelled.add(rid)
            return True
        return False

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for raw in iter(stdin.readline, b""):
            line = raw.strip()
            if not line:
                continue
            try:
                msg = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                write({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
                continue
            if not note_cancel(msg):
                pool.submit(answer, msg)


# ---------------------------------------------------------------------------------------------- over HTTP

HTTP_PATH = "/mcp"
HTTP_MAX_BODY = 1 << 20          # a search request is a few hundred bytes
LOCAL_ORIGINS = ("localhost", "127.0.0.1", "::1")
B64_OPEN, B64_CLOSE = "=?base64?", "?="


def origin_allowed(origin: str, extra=()) -> bool:
    """A browser names the page a request came from in Origin. Only a page served from this computer (or a host the
    person listed) may ask: any other is a website reaching for a local server, which is what DNS rebinding does."""
    try:
        host = urllib.parse.urlsplit(origin).hostname or ""
    except ValueError:
        return False
    return host.lower() in LOCAL_ORIGINS or host.lower() in {e.lower() for e in extra}


def _header_value(v: str) -> str:
    """Mcp-Name as the body would hold it: a value that is not plain ASCII travels as =?base64?...?=."""
    if v.startswith(B64_OPEN) and v.endswith(B64_CLOSE):
        try:
            return base64.b64decode(v[len(B64_OPEN):-len(B64_CLOSE)], validate=True).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            raise RpcError(-32020, "Header mismatch: Mcp-Name is not valid Base64") from None
    return v


def check_headers(msg: dict, headers) -> None:
    """The handshake-free protocol repeats the version, the method and the tool's name in headers, and a server that
    reads the body must refuse a request where they disagree. Raises RpcError(-32020)."""
    params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
    meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
    head_version = headers.get("MCP-Protocol-Version")
    if META_VERSION not in meta:                     # the handshake generation: the header appears after `initialize`
        if head_version in MODERN:
            raise RpcError(-32020, f"Header mismatch: MCP-Protocol-Version is {head_version} and the body names no version")
        if head_version is not None and head_version not in LEGACY:
            raise RpcError(-32022, "Unsupported protocol version", {"supported": list(MODERN + LEGACY), "requested": head_version})
        return
    if head_version is None or head_version != meta.get(META_VERSION):
        raise RpcError(-32020, "Header mismatch: MCP-Protocol-Version is missing or differs from the body")
    if headers.get("Mcp-Method") != msg.get("method"):
        raise RpcError(-32020, "Header mismatch: Mcp-Method is missing or differs from the body")
    if msg.get("method") == "tools/call":
        name = headers.get("Mcp-Name")
        if name is None or _header_value(name) != params.get("name"):
            raise RpcError(-32020, "Header mismatch: Mcp-Name is missing or differs from the body")


def http_status(msg, reply: dict) -> int:
    """The status a reply travels with: the handshake-free protocol puts three faults on the status line too."""
    code = (reply.get("error") or {}).get("code")
    params = msg.get("params") if isinstance(msg, dict) and isinstance(msg.get("params"), dict) else {}
    modern = isinstance(params.get("_meta"), dict) and META_VERSION in params["_meta"]
    if code in (-32020, -32022, -32700, -32600):
        return 400
    if code == -32601 and modern:
        return 404
    return 200


def http_handler(search, version: str, origins=()):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "logout-search-mcp"
        sys_version = ""

        def log_message(self, fmt, *args):           # nothing per request: a line would be the start of a search log
            pass

        def _send(self, status: int, body=None, extra=()) -> None:
            data = b"" if body is None else json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            try:
                self.send_response(status)
                if body is not None:
                    self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                for k, v in extra:
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)
            except OSError:                          # the client went away: on this transport that is how it cancels
                self.close_connection = True

        def _fault(self, status: int, code: int, message: str, extra=()) -> None:
            self.close_connection = True             # the body may be unread
            self._send(status, {"jsonrpc": "2.0", "id": None, "error": {"code": code, "message": message}}, extra)

        def _drain(self, length: int) -> None:
            """Read and drop an oversized body (up to 8 MiB) so the client is still listening when the 413 goes out;
            closing on a client mid-send gives it a broken pipe instead of the answer."""
            left = min(max(length, 0), 8 * HTTP_MAX_BODY)
            try:
                while left > 0:
                    chunk = self.rfile.read(min(left, 65536))
                    if not chunk:
                        break
                    left -= len(chunk)
            except OSError:
                pass

        def _path(self) -> str:
            return urllib.parse.urlsplit(self.path).path.rstrip("/") or "/"

        def _refused(self) -> bool:
            origin = self.headers.get("Origin")
            if origin is not None and not origin_allowed(origin, origins):
                self._fault(403, -32600, "Forbidden: this server answers programs on this computer, not web pages")
                return True
            return False

        def do_GET(self):
            if self._path() == "/healthz":           # for a container's health check: says nothing about the engine
                return self._send(200, {"ok": True})
            if self._path() != HTTP_PATH:
                return self._fault(404, -32600, "Not found: the endpoint is " + HTTP_PATH)
            if not self._refused():                  # no stream to open: every reply is the answer to a POST
                self._fault(405, -32600, "Method not allowed: POST one message", [("Allow", "POST")])

        do_DELETE = do_PUT = do_PATCH = do_GET

        def do_OPTIONS(self):                        # a browser's permission request before a cross-site POST: never granted
            if not self._refused():
                self._fault(405, -32600, "Method not allowed: POST one message", [("Allow", "POST")])

        def do_POST(self):
            if self._refused():
                return
            if self._path() != HTTP_PATH:
                return self._fault(404, -32600, "Not found: the endpoint is " + HTTP_PATH)
            if (self.headers.get("Content-Type") or "").split(";")[0].strip().lower() != "application/json":
                return self._fault(415, -32600, "Unsupported media type: send application/json")
            try:
                length = int(self.headers.get("Content-Length") or "")
            except ValueError:
                return self._fault(411, -32600, "Length required")
            if length < 0 or length > HTTP_MAX_BODY:
                self._drain(length)
                return self._fault(413, -32600, "Request too large")
            try:
                raw = self.rfile.read(length)
                msg = json.loads(raw.decode("utf-8"))
            except (OSError, UnicodeDecodeError, ValueError):
                return self._fault(400, -32700, "Parse error")
            if isinstance(msg, list):                # a batch (2025-03-26 allowed them)
                replies = [r for r in (handle(m, search, version) for m in msg) if r is not None]
                return self._send(200, replies) if replies else self._send(202)
            if isinstance(msg, dict) and isinstance(msg.get("method"), str) and "id" in msg:
                try:
                    check_headers(msg, self.headers)
                except RpcError as e:
                    err = {"code": e.code, "message": e.message}
                    if e.data is not None:
                        err["data"] = e.data
                    return self._send(400, {"jsonrpc": "2.0", "id": msg["id"], "error": err})
            reply = handle(msg, search, version)
            if reply is None:                        # a notification, or a reply to a request we never sent
                return self._send(202)
            self._send(http_status(msg, reply), reply)

    return Handler


def parse_listen(value: str) -> tuple:
    """`8091`, `127.0.0.1:8091`, `0.0.0.0:8091`, `[::1]:8091` -> (host, port). A bare port listens on this computer only."""
    host, _, port = value.rpartition(":")
    host = host.strip("[]") or "127.0.0.1"
    if not port.isdigit() or not 0 < int(port) < 65536:
        raise ValueError(f"--http wants [HOST:]PORT, not {value!r}")
    return host, int(port)


def serve_http(listen: tuple, search, version: str = "dev", origins=()) -> ThreadingHTTPServer:
    """The server, bound and ready for serve_forever() (the caller runs it: a test wants the port first)."""
    class Server(ThreadingHTTPServer):
        daemon_threads = True
        address_family = __import__("socket").AF_INET6 if ":" in listen[0] else __import__("socket").AF_INET
    return Server(listen, http_handler(search, version, tuple(origins)))


def private_stdout():
    """The message channel on its own descriptor; descriptor 1 then points at stderr, so a stray print() can never
    land between two messages."""
    sys.stdout.flush()
    chan = os.fdopen(os.dup(1), "wb", buffering=0)
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    return chan


def client_config(program: list, port: int) -> dict:
    """What a client's settings file needs to start this server (the common `mcpServers` shape)."""
    args = list(program[1:]) + ["mcp"] + (["--port", str(port)] if port != 8090 else [])
    return {"mcpServers": {NAME: {"command": program[0], "args": args}}}


def main_http(listen: str, port: int = 8090, version: str = "dev", origins=()) -> int:
    try:
        where = parse_listen(listen)
    except ValueError as e:
        print(f"logout-search mcp: {e}", file=sys.stderr)
        return 2
    try:
        server = serve_http(where, engine_search(port), version, origins)
    except OSError as e:
        print(f"logout-search mcp: cannot listen on {where[0]}:{where[1]}: {e.strerror or e}", file=sys.stderr)
        return 1
    if where[0] not in ("127.0.0.1", "localhost", "::1"):
        print(f"logout-search mcp: listening on {where[0]}: anything that can reach this address can search. There is no password.", file=sys.stderr)
    print(f"logout-search mcp: http://{where[0] if ':' not in where[0] else '[' + where[0] + ']'}:{where[1]}{HTTP_PATH}", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def main(port: int = 8090, version: str = "dev") -> int:
    out = private_stdout()
    serve(sys.stdin.buffer, out, engine_search(port), version)
    return 0


if __name__ == "__main__":
    sys.exit(main(int(sys.argv[1]) if len(sys.argv) > 1 else 8090))
