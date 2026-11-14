#!/usr/bin/env bash
# requires: cmd:python3
################################################################################
# Unit tests for the engine's reply path when the client has gone (apps/search/serve/api/search_api.py
# Handler._send). The clean-machine install of 2026-11-11 printed BrokenPipeError tracebacks at start:
# the installer's /readyz probe gives up and hangs up while the first probe search is still running,
# and the late reply hit a closed socket. A client that went away is not an engine error: no traceback,
# and the connection is closed instead of read again.
#
# Handler._send is taken from the source with ast (importing search_api needs the release), and run
# behind a real local HTTP server whose client hangs up before the reply. Standard library only.
#
# Usage: bash tests/unit/test-search-api-client-gone.sh   (PY=/path/to/python to override)
################################################################################
set -uo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PY="${PY:-python3}"
PASS=0
FAIL=0
RED=$'\033[0;31m'; GREEN=$'\033[0;32m'; NC=$'\033[0m'
ok()  { echo "  ${GREEN}PASS${NC} $1"; PASS=$((PASS+1)); }
bad() { echo "  ${RED}FAIL${NC} $1"; shift; [[ $# -gt 0 ]] && printf '       %s\n' "$@"; FAIL=$((FAIL+1)); }
SRC="${REPO_ROOT}/apps/search/serve/api/search_api.py"
[[ -f "$SRC" ]] || { echo "SKIP: search_api.py not present"; exit 0; }

out=$(SRC="$SRC" "$PY" - 2>&1 <<'PYEOF'
import ast, contextlib, io, os, socket, struct, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
def check(name, got, want):
    print(f"OK {name}" if got == want else f"BAD {name}: got {got!r}, want {want!r}")
tree = ast.parse(open(os.environ["SRC"]).read())
cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Handler")
fn = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "_send")
ns = {}
exec(compile(ast.Module(body=[fn], type_ignores=[]), "search_api._send", "exec"), ns)

class Gone:                                   # a wfile whose client has gone
    def __init__(self, exc): self.exc = exc
    def write(self, b): raise self.exc
    def flush(self): raise self.exc
class Stub(BaseHTTPRequestHandler):
    _send = ns["_send"]
    def __init__(self, exc):
        self.wfile = Gone(exc); self.request_version = "HTTP/1.1"; self.close_connection = False
        self.requestline = "GET /readyz HTTP/1.1"; self.command = "GET"; self.client_address = ("127.0.0.1", 1)
    def log_message(self, *a): pass
for exc in (BrokenPipeError(32, "Broken pipe"), ConnectionResetError(104, "Connection reset by peer"),
            ConnectionAbortedError(10053, "An established connection was aborted")):   # the Windows hang-up
    h, err = Stub(exc), io.StringIO()
    try:
        with contextlib.redirect_stderr(err):
            h._send(200, b"ready", "text/plain")
        raised = None
    except Exception as e:
        raised = type(e).__name__
    check(f"{type(exc).__name__}: _send does not raise", raised, None)
    check(f"{type(exc).__name__}: nothing on stderr", err.getvalue(), "")
    check(f"{type(exc).__name__}: the connection is closed, not read again", h.close_connection, True)
class Other:
    def write(self, b): raise ValueError("not a hang-up")
    def flush(self): pass
h = Stub(None); h.wfile = Other()
try:
    h._send(200, b"x"); raised = None
except Exception as e:
    raised = type(e).__name__
check("any other write error still raises", raised, "ValueError")

# The real case: a probe that hangs up (RST) while the engine is still working on /readyz.
done = threading.Event()
class H(BaseHTTPRequestHandler):
    _send = ns["_send"]
    def log_request(self, *a): pass
    def do_GET(self):
        time.sleep(0.3)                       # the probe search
        try:
            return self._send(200, b"ready" * 20000, "text/plain")
        finally:
            done.set()
srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
err = io.StringIO()
with contextlib.redirect_stderr(err):
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    s = socket.create_connection(srv.server_address)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    s.sendall(b"GET /readyz HTTP/1.1\r\nHost: x\r\n\r\n")
    s.close()                                 # the probe gave up
    check("the engine thread finished its reply", done.wait(5), True)
    time.sleep(0.3)
    srv.shutdown(); srv.server_close()
check("a probe that hung up leaves no traceback", "Traceback" in err.getvalue(), False)
check("... and no error line at all", err.getvalue().strip(), "")
print("END")
PYEOF
)
while IFS= read -r line; do
    case "$line" in
        OK\ *)  ok "${line#OK }" ;;
        BAD\ *) bad "${line#BAD }" ;;
    esac
done <<< "$out"
grep -q -E '^(OK|BAD) ' <<< "$out" || bad "python produced no verdict" "$out"
[[ "$(tail -1 <<< "$out")" == "END" ]] || bad "the checks stopped early" "$(tail -4 <<< "$out")"
echo ""
echo "Total: $((PASS+FAIL))  PASS: ${PASS}  FAIL: ${FAIL}"
[[ $FAIL -eq 0 ]]
