#!/usr/bin/env bash
# requires: cmd:python3
################################################################################
# Unit tests for the home installer's readiness wait (apps/search/installer/install.py wait_ready,
#): `start` says "ready" only when the engine's /readyz answers 200, not when the port
# opens. Standard library only (test-search-install.sh needs tantivy and skips without it), with a
# fake clock and a real local HTTP server for _readyz. No engine is started.
#
# Usage: bash tests/unit/test-search-install-ready.sh   (PY=/path/to/python to override)
################################################################################
set -uo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PY="${PY:-python3}"
PASS=0
FAIL=0
RED=$'\033[0;31m'; GREEN=$'\033[0;32m'; NC=$'\033[0m'
ok()  { echo "  ${GREEN}PASS${NC} $1"; PASS=$((PASS+1)); }
bad() { echo "  ${RED}FAIL${NC} $1"; shift; [[ $# -gt 0 ]] && printf '       %s\n' "$@"; FAIL=$((FAIL+1)); }
[[ -f "${REPO_ROOT}/apps/search/installer/install.py" ]] || { echo "SKIP: installer not present"; exit 0; }

out=$(INST="${REPO_ROOT}/apps/search/installer" "$PY" - 2>&1 <<'PYEOF'
import contextlib, io, os, sys, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
sys.path.insert(0, os.environ["INST"])
import install
def check(name, got, want):
    print(f"OK {name}" if got == want else f"BAD {name}: got {got!r}, want {want!r}")
class Clock:
    def __init__(self): self.t = 0.0
    def __call__(self): return self.t
    def sleep(self, s): self.t += s
def run(statuses, alive=lambda: True, timeout=300.0):
    """wait_ready against a scripted /readyz; -> (result, what it said, seconds waited)."""
    c, it, buf = Clock(), iter(statuses), io.StringIO()
    last = [0]
    def get(port):
        last[0] = next(it, last[0]); return last[0]
    with contextlib.redirect_stdout(buf):
        r = install.wait_ready(8090, alive, timeout, get=get, sleep=c.sleep, clock=c)
    return r, buf.getvalue(), c.t

r, said, t = run([0, 0, 503, 503, 200])
check("ready only when /readyz answers 200", (r, t), ("ready", 4.0))
check("it says ready with the address", "ready: http://127.0.0.1:8090" in said, True)
check("it says the first searches are slower", "first few searches take 2-5 s" in said, True)
r, said, t = run([200])
check("an engine that is ready at once is ready at once", (r, t, said.count("starting")), ("ready", 0.0, 0))
r, said, t = run([503] * 1000, timeout=40.0)
check("503 for ever is a timeout, not ready", (r, "ready:" in said, "still not ready after 40 s" in said), ("timeout", False, True))
check("a progress line about every 15 s, not every second", said.count("starting..."), 2)
r, said, t = run([0] * 1000, timeout=20.0)
check("nothing listening is not ready either", (r, "ready:" in said), ("timeout", False))
n = [0]
def dies():
    n[0] += 1; return n[0] < 4
r, said, t = run([503] * 1000, alive=dies)
check("an engine that exits ends the wait, without 'ready'", (r, "ready:" in said, t), ("exited", False, 3.0))
r, said, t = run([200], alive=lambda: False)
check("a dead process is never ready, whatever answers on the port", r, "exited")

class H(BaseHTTPRequestHandler):
    code = 503
    def log_message(self, *a): pass
    def do_GET(self):
        self.send_response(H.code if self.path == "/readyz" else 200); self.send_header("Content-Length", "2"); self.end_headers(); self.wfile.write(b"ok")
srv = ThreadingHTTPServer(("127.0.0.1", 0), H); port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
check("_readyz reads 503 while the engine starts (and /healthz would have said 200)", install._readyz(port), 503)
H.code = 200
check("_readyz reads 200 when it is ready", install._readyz(port), 200)
srv.shutdown(); srv.server_close()
check("_readyz reads 0 when nothing listens", install._readyz(port), 0)
src = open(os.environ["INST"] + "/install.py").read()
check("start waits on /readyz before it hands over to the engine", "wait_ready(a.port, lambda: proc.poll() is None)" in src, True)
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
