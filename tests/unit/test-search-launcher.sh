#!/usr/bin/env bash
# =============================================================================
# test-search-launcher.sh — the one program on a home computer (installer/logout_search.py)
# =============================================================================
# What it proves, standard library only (no engine libraries needed, so it runs everywhere):
#   1. the environment it starts the engine with binds the loopback and names the installed release
#   2. `autostart` writes a systemd user unit / a launchd agent / a Task Scheduler task that runs `start`, and
#      refuses to register before a release is installed
#   3. `host register` writes the native-messaging manifests where each browser reads them (Linux and macOS
#      folders, Windows registry keys), with the extension's ids, and a wrapper the browser can start;
#      `unregister` removes them
#   4. `host run` is a faithful forwarder: the browser's framed messages reach the engine over HTTP and
#      come back trimmed to what the page renders; an engine that is down is an error, never a crash
#   5. the update tools run their own command through the launcher when frozen (the env hooks)
#   6. release_ptr's pointer swap still works on this system after the Windows fallback was added
# =============================================================================
set -uo pipefail

SCRIPT_DIR_T="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR_T/../.." && pwd)"
PY="${PY:-python3}"
L="$REPO_ROOT/apps/search/installer/logout_search.py"
RED='\033[0;31m'; GREEN='\033[0;32m'; NC='\033[0m'
PASS=0; FAIL=0
ok()  { echo -e "  ${GREEN}PASS${NC} $1"; PASS=$((PASS+1)); }
bad() { echo -e "  ${RED}FAIL${NC} $1"; shift; [[ $# -gt 0 ]] && printf '       %s\n' "$@"; FAIL=$((FAIL+1)); }
finish() { echo ""; echo "Total: $((PASS+FAIL))  PASS: $PASS  FAIL: $FAIL"; [[ $FAIL -eq 0 ]]; }

tmp=$(mktemp -d); SERVERS=()
cleanup() { for p in "${SERVERS[@]:-}"; do [[ -n "$p" ]] && kill "$p" 2>/dev/null; done; rm -rf "$tmp"; }
trap cleanup EXIT

# a fake install: what engine_env reads (manifest fields and the reranker files), under BASE/current
B="$tmp/base"; R="$B/releases/fx-1"
mkdir -p "$R/models/rerank-turbo" "$B/releases/fx-0"
printf '{"release": "fx-1", "engine": {"reranker": {"cpu": "models/rerank-turbo"}}}' > "$R/manifest.json"
printf '{"release": "fx-0", "engine": {}}' > "$B/releases/fx-0/manifest.json"
: > "$R/models/rerank-turbo/tokenizer.json"; : > "$R/models/rerank-turbo/model_int8.onnx"
"$PY" -c "import sys; sys.path.insert(0, '$REPO_ROOT/apps/search/release'); import release_ptr; release_ptr.set_pointer('$B', 'current', 'fx-1')"
H="$tmp/home"; mkdir -p "$H"
run() { "$PY" "$L" "$@"; }                                      # the launcher, from source
# XDG_CONFIG_HOME too: where it is set (GitHub's Linux machines) the launcher reads it before HOME/.config
on() { LOGOUT_SEARCH_PLATFORM="$1" HOME="$H" XDG_CONFIG_HOME="$H/.config" USERPROFILE="$H" LOCALAPPDATA="$H/AppData/Local" APPDATA="$H/AppData/Roaming" "$PY" "$L" "${@:2}"; }

echo "=== 1. version, start --print-env ==="
out=$(run version --dest "$B" 2>&1); rc=$?
if [[ $rc -eq 0 ]] && "$PY" -c "import json,sys; d=json.loads(sys.argv[1]); assert d['release']=='fx-1' and d['frozen'] is False and d['engine_dir'].endswith('serve/api')" "$out" 2>/dev/null; then
    ok "version: the installed release, not frozen, the engine folder of this checkout"
else bad "version" "rc=$rc" "$out"; fi
out=$(run start --dest "$B" --print-env --format json 2>&1); rc=$?
if [[ $rc -eq 0 ]] && "$PY" -c "import json,sys; d=json.loads(sys.argv[1]); assert d['SEARCH_API_BIND']=='127.0.0.1' and d['SEARCH_API_PORT']=='8090' and d['SEARCH_RELEASE_ID']=='fx-1' and d['SEARCH_CE_DIR'].endswith('models/rerank-turbo') and d['SEARCH_BREADTH']=='JDL'" "$out" 2>/dev/null; then
    ok "start --print-env: the engine binds 127.0.0.1 (nothing on the LAN may query a home machine), the release id and reranker folder are set"
else bad "print-env" "rc=$rc" "$out"; fi
out=$(on win32 start --dest "$B" --print-env 2>&1)
grep -q '^set SEARCH_API_BIND=127.0.0.1$' <<<"$out" && ok "on Windows the default format is cmd" || bad "cmd format" "$out"
grep -q 'SEARCH_API_BIND' "$REPO_ROOT/apps/search/serve/api/search_api.py" && grep -q 'ThreadingHTTPServer((BIND, PORT)' "$REPO_ROOT/apps/search/serve/api/search_api.py" \
    && ok "search_api.py reads SEARCH_API_BIND and binds it (the name the installer sets is one the engine reads)" || bad "engine does not read SEARCH_API_BIND"
out=$(run engine --check-imports 2>&1); rc=$?
"$PY" -c "import json,sys; d=json.loads(sys.argv[1]); assert 'search_api.py' in d['engine_files'] and 'ranker.json' in d['engine_files']" "$out" 2>/dev/null \
    && ok "engine --check-imports names the libraries and finds search_api.py + ranker.json in the engine folder (rc=$rc here: the libraries may be absent on this machine)" || bad "check-imports" "$out"

echo ""; echo "=== 2. autostart: one unit / agent / task per system, dry runs ==="
out=$(on linux autostart on --dest "$B" --port 8123 --print 2>&1); rc=$?
if [[ $rc -eq 0 ]] && grep -q "write  $H/.config/systemd/user/logout-search.service" <<<"$out" && grep -qE "^ +ExecStart=.*logout_search.py start --dest $B --port 8123$" <<<"$out" \
    && grep -q "WantedBy=default.target" <<<"$out" && grep -q "run    systemctl --user enable --now logout-search.service" <<<"$out" && [[ ! -e "$H/.config/systemd/user/logout-search.service" ]]; then
    ok "linux: a systemd user unit whose ExecStart is \`start --dest BASE --port N\`, enabled now and at logon; --print writes nothing"
else bad "linux autostart" "rc=$rc" "$out"; fi
out=$(on linux autostart off --dest "$B" --print 2>&1)
grep -q "remove $H/.config/systemd/user/logout-search.service" <<<"$out" && grep -q "systemctl --user disable --now" <<<"$out" && ok "linux off: disable + remove the unit" || bad "linux off" "$out"
out=$(on darwin autostart on --dest "$B" --print 2>&1); rc=$?
plist=$(sed -n '/<?xml/,/<\/plist>/p' <<<"$out" | sed 's/^    //')
if [[ $rc -eq 0 ]] && "$PY" -c "
import plistlib,sys
d=plistlib.loads(sys.stdin.read().encode())
assert d['Label']=='cloud.logout.search' and d['RunAtLoad'] is True and d['ProgramArguments'][-5:]==['start','--dest','$B','--port','8090'], d['ProgramArguments']
assert d['KeepAlive']=={'SuccessfulExit': False} and d['StandardOutPath'].startswith('$B/')
" <<<"$plist" 2>/dev/null && grep -q "write  $H/Library/LaunchAgents/cloud.logout.search.plist" <<<"$out" && grep -q "launchctl bootstrap gui/" <<<"$out"; then
    ok "macOS: a launchd agent (valid plist: label, RunAtLoad, start command, log under BASE), bootstrapped into the user's session"
else bad "darwin autostart" "rc=$rc" "$out"; fi
out=$(on win32 autostart on --dest "$B" --print 2>&1); rc=$?
if [[ $rc -eq 0 ]] && grep -q "schtasks /Create /F /SC ONLOGON /TN 'logout search' /TR" <<<"$out" && grep -q "start --dest $B --port 8090" <<<"$out" && ! grep -q "^write" <<<"$out"; then
    ok "Windows: a Task Scheduler task at logon running \`start\`; no file written"
else bad "win32 autostart" "rc=$rc" "$out"; fi
out=$(on win32 autostart off --dest "$B" --print 2>&1)
grep -q "schtasks /Delete /F /TN 'logout search'" <<<"$out" && ok "Windows off: the task is deleted" || bad "win32 off" "$out"
empty="$tmp/empty"; mkdir -p "$empty"
out=$(on linux autostart on --dest "$empty" 2>&1); rc=$?
[[ $rc -eq 2 ]] && grep -q "holds no installed release" <<<"$out" && ok "autostart on refuses a folder with no installed release (exit 2)" || bad "autostart refusal" "rc=$rc" "$out"

echo ""; echo "=== 3. host register / unregister: where each browser reads native-messaging manifests ==="
mkdir -p "$H/.config/BraveSoftware/Brave-Browser" "$H/.config/google-chrome-beta" "$H/.mozilla"
out=$(on linux host register --dest "$B" --port 8123 2>&1); rc=$?
cj="$H/.config/BraveSoftware/Brave-Browser/NativeMessagingHosts/cloud.logout.search.json"
fj="$H/.mozilla/native-messaging-hosts/cloud.logout.search.json"
wr="$B/host/cloud.logout.search.sh"
if [[ $rc -eq 0 && -f "$cj" && -f "$fj" && -x "$wr" && -f "$H/.config/google-chrome-beta/NativeMessagingHosts/cloud.logout.search.json" ]] \
    && "$PY" -c "
import json,sys
c=json.load(open('$cj')); f=json.load(open('$fj'))
assert c['name']=='cloud.logout.search' and c['type']=='stdio' and c['path']=='$wr' and c['allowed_origins']==['chrome-extension://dgnaanjmpoeoecakbcfjmcnoliapkebo/'] and 'allowed_extensions' not in c
assert f['allowed_extensions']==['search@logout.cloud'] and 'allowed_origins' not in f and f['path']=='$wr'
" 2>/dev/null && grep -q "host run --dest $B --port 8123" "$wr" && grep -q '^#!/bin/sh' <<<"$(head -1 "$wr")" && grep -q "registered with: google-chrome-beta, BraveSoftware/Brave-Browser, firefox" <<<"$out"; then
    ok "linux: one manifest per browser profile found (Brave, Chrome beta, Firefox), Chromium ids vs the Firefox id, an executable wrapper that runs \`host run\` with the port"
else bad "linux host register" "rc=$rc" "$out" "$(ls -R "$H" 2>&1 | head -20)"; fi
out=$(on linux host unregister --dest "$B" 2>&1); rc=$?
[[ $rc -eq 0 && ! -e "$cj" && ! -e "$fj" && ! -e "$wr" ]] && ok "linux unregister removes every manifest and the wrapper" || bad "unregister" "rc=$rc" "$out"
mkdir -p "$H/Library/Application Support/Google/Chrome" "$H/Library/Application Support/Firefox"
out=$(on darwin host register --dest "$B" --print 2>&1); rc=$?
if [[ $rc -eq 0 ]] && grep -q "write  $H/Library/Application Support/Google/Chrome/NativeMessagingHosts/cloud.logout.search.json" <<<"$out" \
    && grep -q "write  $H/Library/Application Support/Mozilla/NativeMessagingHosts/cloud.logout.search.json" <<<"$out" && grep -q "write  $B/host/cloud.logout.search.sh" <<<"$out"; then
    ok "macOS: manifests under ~/Library/Application Support/<browser>/NativeMessagingHosts (Firefox's under Mozilla/)"
else bad "darwin host" "rc=$rc" "$out"; fi
mkdir -p "$H/AppData/Local/Microsoft/Edge" "$H/AppData/Local/Google/Chrome" "$H/AppData/Roaming/Mozilla/Firefox"
out=$(on win32 host register --dest "$B" --print 2>&1); rc=$?
if [[ $rc -eq 0 ]] && grep -q 'set    HKCU\\Software\\Microsoft\\Edge\\NativeMessagingHosts\\cloud.logout.search = '"$B"'/host/cloud.logout.search.chromium.json' <<<"$out" \
    && grep -q 'set    HKCU\\Software\\Google\\Chrome\\NativeMessagingHosts\\cloud.logout.search = ' <<<"$out" \
    && grep -q 'set    HKCU\\Software\\Mozilla\\NativeMessagingHosts\\cloud.logout.search = '"$B"'/host/cloud.logout.search.firefox.json' <<<"$out" \
    && grep -q "write  $B/host/cloud.logout.search.cmd" <<<"$out" && grep -q '^    @echo off' <<<"$out"; then
    ok "Windows: two manifests under BASE/host (Chromium ids, Firefox id), a .cmd wrapper, one HKCU registry key per browser present (Chrome, Edge, Firefox)"
else bad "win32 host" "rc=$rc" "$out"; fi
out=$(on win32 host unregister --dest "$B" --print 2>&1)
grep -q 'delete    HKCU\\Software\\Microsoft\\Edge\\NativeMessagingHosts\\cloud.logout.search' <<<"$out" && grep -q "remove $B/host/cloud.logout.search.cmd" <<<"$out" \
    && ok "Windows unregister deletes the keys and the files" || bad "win32 unregister" "$out"
rm -rf "$H/.config/BraveSoftware" "$H/.config/google-chrome-beta" "$H/.mozilla"
out=$(on linux host register --dest "$B" 2>&1); rc=$?
[[ $rc -eq 1 ]] && grep -q "no supported browser profile" <<<"$out" && ok "no browser found: says so, exit 1, writes nothing" || bad "no browser" "rc=$rc" "$out"

echo ""; echo "=== 4. host run: the browser's framed messages, forwarded to the engine over HTTP ==="
cat > "$tmp/engine_stub.py" <<'PY'
import json, sys, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit
log = open(sys.argv[2], "a")
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        u = urlsplit(self.path); qs = {k: v[0] for k, v in parse_qs(u.query).items()}
        log.write(json.dumps({"path": u.path, "qs": qs}) + "\n"); log.flush()
        if u.path == "/readyz":
            body = b"ok"
        elif u.path == "/instant":
            body = json.dumps({"suggest": [{"url": "u1", "title": "t1", "source": "wiki", "secret": 1}], "phrases": [], "confident": True,
                               "results": [{"url": "https://nbc.com", "title": "NBC", "content": "c", "source": "site", "score": 9.5, "debug": "x"}],
                               "ms": 7, "stages": {"a": 1}, "intent": "nav", "near": qs.get("near"), "full": qs.get("full"), "r0": qs.get("results")}).encode()
        elif u.path == "/more":
            body = json.dumps({"news": [{"t": qs["offset"]}], "more": True, "other": 1}).encode()
        elif u.path == "/explain":
            body = json.dumps({"steps": ["s1", qs["q"]]}).encode()
        else:
            self.send_response(404); self.end_headers(); return
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
open(sys.argv[1], "w").write(str(srv.server_port))
srv.serve_forever()
PY
"$PY" "$tmp/engine_stub.py" "$tmp/port" "$tmp/engine.log" & SERVERS+=($!)
for _ in $(seq 50); do [[ -s "$tmp/port" ]] && break; sleep 0.1; done
port=$(cat "$tmp/port")
cat > "$tmp/browser.py" <<'PY'
"""Talk to `host run` the way a browser does: 4-byte native-endian length + JSON, both ways."""
import json, struct, subprocess, sys, threading
cmd, msgs = json.loads(sys.argv[1]), json.loads(sys.argv[2])
p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
for m in msgs:
    d = json.dumps(m).encode(); p.stdin.write(struct.pack("=I", len(d)) + d)
p.stdin.flush()
out = {}
def reader():
    while len(out) < len(msgs):
        raw = p.stdout.read(4)
        if len(raw) < 4: break
        (n,) = struct.unpack("=I", raw); r = json.loads(p.stdout.read(n)); out[r.get("id")] = r
t = threading.Thread(target=reader, daemon=True); t.start(); t.join(60)
p.stdin.close(); p.wait(timeout=10)
print(json.dumps(out, sort_keys=True)); print(p.stderr.read().decode()[-500:], file=sys.stderr)
PY
msgs='[{"id":"p","ping":true},{"id":"i","info":true},{"id":"d","details":true},{"id":"q","q":"nbc  news","results":true},{"id":"s","q":"nbc","results":false},
       {"id":"f","q":"nbc","full":true,"near":"40.783,-105.916"},{"id":"m","q":"nbc","news_more":true,"offset":20},{"id":"x","q":"nbc","explain":true},{"id":"t","q":"n"},{"id":"g","get_settings":true}]'
out=$(LOGOUT_SETTINGS="$tmp/settings.json" "$PY" "$tmp/browser.py" "[\"$PY\", \"$L\", \"host\", \"run\", \"--dest\", \"$B\", \"--port\", \"$port\"]" "$msgs" 2>"$tmp/host.err"); rc=$?
if [[ $rc -eq 0 ]] && "$PY" - "$out" "$tmp/engine.log" <<'PY' 2>"$tmp/assert.err"
import json, sys
o = json.loads(sys.argv[1]); log = [json.loads(l) for l in open(sys.argv[2])]
assert o["p"] == {"id": "p", "pong": True, "version": 1}, o["p"]
assert o["i"]["info"]["host_version"] == 1 and o["i"]["info"]["via"] == "", o["i"]
assert o["d"]["details"]["release"] == "fx-1" and o["d"]["details"]["engine_up"] is True and "postgres" not in o["d"]["details"], o["d"]
b = o["q"]["body"]
assert b["results"] == [{"url": "https://nbc.com", "title": "NBC", "content": "c", "source": "site"}], b          # score/debug trimmed
assert b["suggest"] == [{"url": "u1", "title": "t1", "source": "wiki"}] and "stages" not in b and b["ms"] == 7, b
assert o["s"]["body"]["results"] and o["f"]["body"]["results"], (o["s"], o["f"])
assert o["m"]["body"] == {"news": [{"t": "20"}], "more": True}, o["m"]
assert o["x"]["body"] == {"steps": ["s1", "nbc"]}, o["x"]
assert o["t"]["body"] == {"suggest": [], "phrases": [], "results": []}, o["t"]                                    # too short: never asked
assert o["g"]["settings"]["pass"] == "google" and "engines" in o["g"], o["g"]
qs = [l["qs"] for l in log if l["path"] == "/instant"]
assert any(q["q"] == "nbc news" for q in qs), qs                                                                  # whitespace collapsed
assert any(q.get("results") == "0" and "full" not in q for q in qs), qs                                           # suggest-only asked as results=0
assert any(q.get("full") == "1" and q.get("near") == "40.783,-105.916" for q in qs), qs                                # full + the page's position
assert not any(q.get("q") == "n" for q in qs)
PY
then ok "ten browser messages: ping, info, details (release, engine up, no Postgres), results trimmed to the page's fields, suggest-only, full+near, more news, explain, too-short answered locally, settings"
else bad "host run" "rc=$rc" "$out" "$(cat "$tmp/assert.err" "$tmp/host.err" | tail -8)"; fi
out=$("$PY" "$tmp/browser.py" "[\"$PY\", \"$L\", \"host\", \"run\", \"--dest\", \"$B\", \"--port\", \"1\"]" '[{"id":"q","q":"nbc"},{"id":"p","ping":true}]' 2>/dev/null); rc=$?
if [[ $rc -eq 0 ]] && "$PY" -c "import json,sys; o=json.loads(sys.argv[1]); assert o['q']['error']=='URLError' and o['p']['pong'] is True" "$out" 2>/dev/null; then
    ok "engine down: the query answers with an error name (no query text), ping still works, the host does not crash"
else bad "engine down" "rc=$rc" "$out"; fi

echo ""; echo "=== 5. update tools: the frozen launcher's command reaches them ==="
# update_apply imports numpy at the top; self_cmd needs none of it: a stub stands in only where numpy is absent
if "$PY" -c "import numpy" 2>/dev/null; then STUBS=""; else mkdir -p "$tmp/stubs"; : > "$tmp/stubs/numpy.py"; STUBS="$tmp/stubs"; fi
out=$(PYTHONPATH="$STUBS" LOGOUT_UPDATE_SELF_CMD='["/x/logout-search", "_update-apply"]' "$PY" -c "import sys; sys.path.insert(0, '$REPO_ROOT/apps/search/release'); import update_apply; print(update_apply.self_cmd())" 2>&1)
[[ "$out" == "['/x/logout-search', '_update-apply']" ]] && ok "update_apply.self_cmd honours LOGOUT_UPDATE_SELF_CMD (its SQLite workers run through the launcher)" || bad "self_cmd" "$out"
out=$(PYTHONPATH="$STUBS" "$PY" -c "import sys; sys.path.insert(0, '$REPO_ROOT/apps/search/release'); import update_apply; print(update_apply.self_cmd()[1].endswith('update_apply.py'))" 2>&1)
[[ "$out" == "True" ]] && ok "without the hook it names this interpreter and update_apply.py" || bad "self_cmd default" "$out"
out=$(LOGOUT_UPDATE_APPLY_CMD='["/x/logout-search", "_update-apply"]' "$PY" -c "import sys; sys.path.insert(0, '$REPO_ROOT/apps/search/release'); import update_fetch; print(update_fetch.apply_cmd())" 2>&1)
[[ "$out" == "['/x/logout-search', '_update-apply']" ]] && ok "update_fetch.apply_cmd honours LOGOUT_UPDATE_APPLY_CMD" || bad "apply_cmd" "$out"
out=$(run _update-apply status "$B" 2>&1); rc=$?
if [[ $rc -eq 0 ]] && "$PY" -c "import json,sys; d=json.loads(sys.argv[1]); assert d['current']=='fx-1' and d['releases']==['fx-0','fx-1']" "$out" 2>/dev/null; then
    ok "_update-apply passes through to update_apply.py's main (status)"
else
    if grep -q "No module named 'numpy'" <<<"$out"; then ok "_update-apply reaches update_apply.py (it stops at numpy, absent on this machine)"; else bad "_update-apply" "rc=$rc" "$out"; fi
fi
out=$(run status --dest "$B" 2>&1); rc=$?
[[ $rc -eq 0 ]] && grep -q '"current": "fx-1"' <<<"$out" && ok "status passes through to install.py" || bad "status" "rc=$rc" "$out"
out=$(run update --dest "$B" --source "$tmp/nowhere" --pubkey "$tmp/none.pem" --check 2>&1); rc=$?
[[ $rc -ne 0 ]] && ok "update --check with a bad key file is refused, not a traceback (rc=$rc)" || bad "update check" "$out"

echo ""; echo "=== 6. release_ptr after the Windows fallback ==="
out=$("$PY" -c "
import os, sys
sys.path.insert(0, '$REPO_ROOT/apps/search/release'); import release_ptr as rp
b = '$B'
rp.set_pointer(b, 'previous', 'fx-1'); rp.set_pointer(b, 'current', 'fx-0'); rp.set_pointer(b, 'current', 'fx-1')
print(rp.current_id(b), rp.current_id(b, 'previous'), os.path.islink(os.path.join(b, 'current')), os.readlink(os.path.join(b, 'current')), rp.current_id(b, 'nothing'))
" 2>&1)
[[ "$out" == "fx-1 fx-1 True releases/fx-1 None" ]] && ok "set_pointer swaps a symlink twice, current_id reads it, a missing pointer is None" || bad "release_ptr" "$out"
grep -q "CreateJunction" "$REPO_ROOT/apps/search/release/release_ptr.py" && grep -q 'os.name != "nt"' "$REPO_ROOT/apps/search/release/release_ptr.py" \
    && ok "on Windows a refused symlink becomes an NTFS junction (code present; exercised by the public repo's CI on windows-latest)" || bad "junction fallback missing"

finish
