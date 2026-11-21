#!/usr/bin/env bash
# requires: cmd:python3
################################################################################
# Unit tests for the Docker option (apps/search/docker): no Docker needed.
#   1. The image's pins are the bundle's pins; every file the Dockerfile copies exists
#   2. The compose file publishes the connector to this computer only, and starts no database
#   3. The entrypoint: installs when there is no data (pinned source and key), stops when the install fails,
#      then starts the engine without the pairing peer and the connector over HTTP
#
# Usage: bash tests/unit/test-search-docker.sh
################################################################################
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

REPO="$REPO" TMP="$TMP" python3 - <<'PY'
import json, os, re, shutil, subprocess, sys

R, T = os.environ["REPO"], os.environ["TMP"]
S = os.path.join(R, "apps/search")
n = [0]
def ok(name, cond, detail=""):
    n[0] += 1
    if not cond:
        print(f"FAIL {n[0]}: {name} {detail}")
        sys.exit(1)
    print(f"ok {n[0]}: {name}")

def pins(path):
    return {l.split("==")[0].strip().lower(): l.strip() for l in open(path) if "==" in l and not l.lstrip().startswith("#")}
mine, bundle = pins(f"{S}/docker/requirements.txt"), pins(f"{S}/installer/bundle/requirements.txt")
ok("the image's own pins are the bundle's", mine and all(bundle.get(k) == v for k, v in mine.items()), (mine, bundle))
ok("the engine's libraries come from the engine's own list", "-r ../serve/api/requirements.txt" in open(f"{S}/docker/requirements.txt").read())
df = open(f"{S}/docker/Dockerfile").read()
copied = [l.split()[1:-1] for l in df.splitlines() if l.startswith("COPY ") and "--from" not in l]
missing = [src for srcs in copied for src in srcs if not os.path.exists(os.path.join(S, src))]
ok("every file the Dockerfile copies exists in apps/search", copied and not missing, missing)
ok("the image runs as a plain user and carries no model file", "USER 1000:1000" in df and "serve/api/models" in open(f"{S}/docker/Dockerfile.dockerignore").read())
ok("the build checks the libraries and this system's SQLite", "engine --check-imports" in df)

dc = open(f"{S}/docker/docker-compose.yml").read()
ports = re.findall(r'^\s*-\s*"([^"]*\d+:\d+)"', dc, re.M)
ok("compose publishes the connector to this computer only", ports == ["127.0.0.1:8091:8091"], ports)
ok("compose starts no database", not re.search(r"postgres|PGHOST|psql", dc, re.I))
ok("compose runs the published image and builds nothing", re.search(r"^\s*image:\s*ghcr\.io/m8software/logout-search:latest\b", dc, re.M) and "build:" not in dc)
bc = open(f"{S}/docker/docker-compose.build.yml").read()
ok("the build file builds this source under a local name", "dockerfile: docker/Dockerfile" in bc and "image: logout-search:local" in bc and "ports" not in bc)
wf = [p for p in (f"{R}/.github/workflows/image.yml", f"{S}/tools/public-repo/root/.github/workflows/image.yml") if os.path.exists(p)]
wt = open(wf[0]).read() if wf else ""
ok("the image workflow runs on its own tag or a promotion (not a release tag), may push packages and sign in to the MCP Registry, nothing more",
   'tags: ["image-*", "promote-v*"]' in wt and '"v*"' not in wt and "packages: write" in wt and "contents: write" not in wt
   and "id-token: write" in wt and sorted(set(re.findall(r"^\s+([a-z-]+): write\b", wt, re.M))) == ["id-token", "packages"]
   and "-f apps/search/docker/Dockerfile" in wt, wf)

# the entrypoint, with a stand-in for the program that records how it was called
app = os.path.join(T, "app")
os.makedirs(os.path.join(app, "installer")); os.makedirs(os.path.join(app, "docker"))
shutil.copy(f"{S}/docker/entrypoint.py", os.path.join(app, "docker"))
open(os.path.join(app, "installer", "logout_search.py"), "w").write('''
import json, os, sys, time
DATA_SOURCE, DATA_PUBKEY = "hf:pinned/source", "ed25519:pinnedkey"
if __name__ == "__main__":
    with open(os.environ["CALLS"], "a") as f:
        f.write(json.dumps(sys.argv[1:]) + "\\n")
    if sys.argv[1] == "install":
        rc = int(os.environ.get("INSTALL_RC", "0"))
        if rc == 0:
            d = sys.argv[sys.argv.index("--dest") + 1]
            os.makedirs(os.path.join(d, "releases", "r1")); open(os.path.join(d, "releases", "r1", "manifest.json"), "w").write("{}")
            os.symlink(os.path.join("releases", "r1"), os.path.join(d, "current"))
        sys.exit(rc)
    if sys.argv[1] == "mcp":
        time.sleep(30)
    if os.environ.get("LOGOUT_MCP_HTTP", "").lower() != "off":      # the connector starts beside it: let it record its call
        want = int(os.environ.get("MCP_CALLS", "1"))
        for _ in range(100):
            if open(os.environ["CALLS"]).read().count('["mcp"') >= want:
                break
            time.sleep(0.05)
    sys.exit(int(os.environ.get("START_RC", "0")))
''')
def run(name, **env):
    data, calls = os.path.join(T, name), os.path.join(T, name + ".calls")
    os.makedirs(data, exist_ok=True)
    p = subprocess.run([sys.executable, os.path.join(app, "docker", "entrypoint.py")], capture_output=True, text=True, timeout=60,
                       env={**os.environ, "LOGOUT_SEARCH_BASE": data, "CALLS": calls, **env})
    got = [json.loads(l) for l in open(calls)] if os.path.exists(calls) else []
    return p, sorted(got, key=lambda a: ["install", "mcp", "start"].index(a[0])), os.path.join(data, "data")
p, calls, data = run("fresh")
ok("no data: it installs from the pinned source with the pinned key", calls[0] == ["install", "--source", "hf:pinned/source", "--pubkey", "ed25519:pinnedkey", "--dest", data], calls)
ok("then the engine starts without the pairing peer", calls[-1] == ["start", "--dest", data, "--port", "8090", "--no-peer"] and p.returncode == 0, (calls, p.stderr))
ok("and the connector listens over HTTP beside it", calls[1] == ["mcp", "--port", "8090", "--http", "0.0.0.0:8091"], calls)
ok("the first start says it is downloading, and that a stopped download resumes", "downloading it from hf:pinned/source" in p.stdout and "resumes" in p.stdout, p.stdout)
p, calls, data = run("fresh", MCP_CALLS="2")
ok("data already there: nothing is downloaded again", [c[0] for c in calls] == ["install", "mcp", "mcp", "start", "start"], calls)
p, calls, _ = run("broken", INSTALL_RC="2")
ok("a failed download stops there with its exit code: no engine on half a release", p.returncode == 2 and [c[0] for c in calls] == ["install"], (p.returncode, calls))
p, calls, _ = run("update", START_RC="75")
ok("the engine's exit code is the container's (75 = start me again after an update)", p.returncode == 75, p.returncode)
p, calls, _ = run("origins", LOGOUT_MCP_ALLOW_ORIGIN="chat.home.example, other.example", LOGOUT_MCP_HTTP="0.0.0.0:9000")
ok("allowed pages and the listen address are passed on", calls[1] == ["mcp", "--port", "8090", "--http", "0.0.0.0:9000", "--allow-origin", "chat.home.example", "--allow-origin", "other.example"], calls)
p, calls, _ = run("off", LOGOUT_MCP_HTTP="off")
ok("LOGOUT_MCP_HTTP=off starts no connector", [c[0] for c in calls] == ["install", "start"], calls)
import signal, time
d5, c5 = os.path.join(T, "stop", "data"), os.path.join(T, "stop.calls")
os.makedirs(os.path.join(d5, "releases", "r1")); open(os.path.join(d5, "releases", "r1", "manifest.json"), "w").write("{}"); os.symlink("releases/r1", os.path.join(d5, "current"))
slow = os.path.join(T, "slowapp"); shutil.copytree(app, slow)
src = open(os.path.join(slow, "installer", "logout_search.py")).read().replace('sys.exit(int(os.environ.get("START_RC", "0")))', 'time.sleep(30)')
open(os.path.join(slow, "installer", "logout_search.py"), "w").write(src)
pr = subprocess.Popen([sys.executable, os.path.join(slow, "docker", "entrypoint.py")], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                      env={**os.environ, "LOGOUT_SEARCH_BASE": os.path.dirname(d5), "CALLS": c5})
for _ in range(100):
    if os.path.exists(c5) and open(c5).read().count("\n") >= 2:
        break
    time.sleep(0.1)
t0 = time.time(); pr.send_signal(signal.SIGTERM); rc = pr.wait(20)
ok("`docker stop` (SIGTERM) ends it at once with exit code 0", rc == 0 and time.time() - t0 < 10, (rc, time.time() - t0))
EP = os.path.join(app, "docker", "entrypoint.py")
def health(base, **env):
    return subprocess.run([sys.executable, EP, "--health"], capture_output=True, timeout=30, env={**os.environ, "LOGOUT_SEARCH_BASE": base, **env}).returncode
import http.server, threading
class Hz(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200 if self.path == "/healthz" else 404); self.send_header("Content-Length", "0"); self.end_headers()
    def log_message(self, *a):
        pass
hz = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Hz); threading.Thread(target=hz.serve_forever, daemon=True).start()
up = str(hz.server_address[1])
ok("health: still downloading (no data yet) counts as healthy", health(os.path.join(T, "nothing-here")) == 0)
ok("health: installed and the connector answers -> healthy", health(os.path.join(T, "fresh"), LOGOUT_MCP_HTTP="0.0.0.0:" + up) == 0)
ok("health: installed and nothing answers on the connector's port -> unhealthy", health(os.path.join(T, "fresh"), LOGOUT_MCP_HTTP="0.0.0.0:1") == 1)
ok("the image's health check is that command, so a changed port or no connector is followed", '"/app/docker/entrypoint.py", "--health"' in df and "8091/healthz" not in df)
ok("the data sits one level down, so the daily update's own files stay inside the volume", data.endswith(os.sep + "data") and os.path.isdir(data))
hz.shutdown()
ro = os.path.join(T, "ro"); os.makedirs(ro); os.chmod(ro, 0o555)
if os.getuid() != 0:
    p = subprocess.run([sys.executable, os.path.join(app, "docker", "entrypoint.py")], capture_output=True, text=True, timeout=60,
                       env={**os.environ, "LOGOUT_SEARCH_BASE": ro, "CALLS": os.path.join(T, "ro.calls")})
    ok("a data folder it cannot write says how to fix it", p.returncode == 1 and "chown" in p.stdout and not os.path.exists(os.path.join(T, "ro.calls")), p.stdout)
print(f"\nall {n[0]} passed")
PY
