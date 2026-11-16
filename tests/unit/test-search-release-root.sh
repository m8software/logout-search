#!/usr/bin/env bash
################################################################################
# Unit tests for apps/search/serve/api/release_root.py: the engine follows
# BASE/current at start, pins the answer for the life of the process, and without a pointer
# leaves every path exactly as configured. Pure python, no tantivy needed.
#
# Usage: bash tests/unit/test-search-release-root.sh
################################################################################
set -uo pipefail
SCRIPT_DIR_T="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR_T/../.." && pwd)"
API_DIR="$REPO_ROOT/apps/search/serve/api"
PY="${PY:-python3}"
export PYTHONDONTWRITEBYTECODE=1
PASS=0; FAIL=0
RED=$'\033[0;31m'; GREEN=$'\033[0;32m'; NC=$'\033[0m'
ok()  { echo "  ${GREEN}PASS${NC} $1"; PASS=$((PASS+1)); }
bad() { echo "  ${RED}FAIL${NC} $1"; shift; [[ $# -gt 0 ]] && printf '       %s\n' "$@"; FAIL=$((FAIL+1)); }
expect() { if grep -qxF -- "$1" <<<"$out"; then ok "$2"; else bad "$2" "want: $1" "got:  $(head -c 800 <<<"$out")"; fi; }

echo ""; echo "=== release_root: follow BASE/current once, at start ==="
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
out=$("$PY" - "$API_DIR" "$tmp" <<'PY' 2>&1
import os, sys
sys.path.insert(0, sys.argv[1])
import release_root as rr
tmp = sys.argv[2]
def rel(name):
    d = f"{tmp}/base/releases/{name}"; os.makedirs(d); open(f"{d}/manifest.json", "w").write("{}")
def point(name, to):
    t = f"{tmp}/base/.{name}.new"
    if os.path.lexists(t): os.remove(t)
    os.symlink(f"releases/{to}", t); os.replace(t, f"{tmp}/base/{name}")
rel("r1"); rel("r2"); point("current", "r1")
base = f"{tmp}/base"
print("base", rr.resolve(base) == f"{base}/releases/r1")
print("trailing slash", rr.resolve(base + "/") == f"{base}/releases/r1")
print("via pointer", rr.resolve(f"{base}/current") == f"{base}/releases/r1")
print("release folder", rr.resolve(f"{base}/releases/r2") == f"{base}/releases/r2")
# an update is applied while the engine runs: the pointer moves, this process does not
point("current", "r2")
print("pinned base", rr.resolve(base) == f"{base}/releases/r1")
print("pinned pointer", rr.resolve(f"{base}/current") == f"{base}/releases/r1")
rr.forget()
print("next start base", rr.resolve(base) == f"{base}/releases/r2")
print("next start pointer", rr.resolve(f"{base}/current") == f"{base}/releases/r2")
rr.forget()
# no pointer: unchanged, whatever the path is
os.makedirs(f"{tmp}/flat"); open(f"{tmp}/flat/manifest.json", "w").write("{}")
os.makedirs(f"{tmp}/plain")
print("flat release", rr.resolve(f"{tmp}/flat") == f"{tmp}/flat")
print("trailing slash, no pointer", rr.resolve(f"{tmp}/flat/") == f"{tmp}/flat")
print("no manifest, no pointer", rr.resolve(f"{tmp}/plain") == f"{tmp}/plain")
print("missing path", rr.resolve("/nonexistent/release") == "/nonexistent/release")
# a release folder wins over a stray `current` inside it
os.symlink("/nonexistent", f"{tmp}/flat/current")
rr.forget()
print("release beats stray current", rr.resolve(f"{tmp}/flat") == f"{tmp}/flat")
# a broken pointer stops the start; it never falls back
os.makedirs(f"{tmp}/bad"); os.symlink("releases/gone", f"{tmp}/bad/current")
try:
    rr.resolve(f"{tmp}/bad"); print("broken pointer served")
except rr.ReleaseRootError as e:
    print("broken pointer refused", "not a release folder" in str(e))
# search_api, loaded by file path like the other unit tests (psycopg stubbed)
import importlib.util, types
sys.modules["psycopg"] = types.ModuleType("psycopg")
os.makedirs(f"{tmp}/base/releases/r2/vectors/x")
rr.forget()
os.environ["SEARCH_RELEASE_DIR"] = base
os.environ["SEARCH_SEM_IVF"] = f"{base}/current/vectors/x"
import gov_state   # a plain top-level import of search_api; loaded while api/ is on the path, so only release_root takes the by-file fallback
sys.path.remove(sys.argv[1])
sys.modules.pop("release_root")
spec = importlib.util.spec_from_file_location("search_api", os.path.join(sys.argv[1], "search_api.py"))
m = importlib.util.module_from_spec(spec); sys.modules["search_api"] = m   # stage_* import search_api back, so it must be registered first
import importlib.machinery
class _StageFinder:    # only the stage_* parts are found; the api dir stays off sys.path so release_root still takes the by-file fallback
    @staticmethod
    def find_spec(name, path=None, target=None):
        if name.startswith("stage_"):
            return importlib.machinery.PathFinder.find_spec(name, [sys.argv[1]])
sys.meta_path.insert(0, _StageFinder)
spec.loader.exec_module(m)
print("search_api RELEASE_DIR is the release current names", m.RELEASE_DIR == f"{base}/releases/r2")
print("search_api SEM_IVF follows too", m.SEM_IVF == f"{base}/releases/r2/vectors/x")
# ReleaseLookups' root goes through it (source check: the real class needs a whole release)
src = open(os.path.join(sys.argv[1], "lookups_release.py")).read()
print("lookups wired", "release_root.resolve(root)" in src)
PY
)
expect "base True" "a BASE folder resolves to the release its current points at"
expect "trailing slash True" "a trailing slash changes nothing"
expect "via pointer True" "SEARCH_RELEASE_DIR=BASE/current resolves to the release folder"
expect "release folder True" "a release folder is used as it is"
expect "pinned base True" "an update applied while the process runs does not move a BASE already resolved"
expect "pinned pointer True" "... nor a BASE/current already resolved"
expect "next start base True" "the next start follows the new pointer (BASE)"
expect "next start pointer True" "the next start follows the new pointer (BASE/current)"
expect "flat release True" "no pointer: a plain release folder is unchanged"
expect "trailing slash, no pointer True" "no pointer: a trailing slash is dropped, nothing else changes"
expect "no manifest, no pointer True" "no pointer, no manifest: unchanged"
expect "missing path True" "a path that does not exist is returned unchanged"
expect "release beats stray current True" "a release folder is never redirected by a stray current inside it"
expect "broken pointer refused True" "a broken current stops the start with a clear error, no fallback"
expect "search_api RELEASE_DIR is the release current names True" "search_api reads SEARCH_RELEASE_DIR=BASE as that release (psycopg stubbed)"
expect "search_api SEM_IVF follows too True" "SEARCH_SEM_IVF=BASE/current/vectors/x resolves to the release's folder"
expect "lookups wired True" "ReleaseLookups resolves its root through release_root"
echo ""; echo "Total: $((PASS+FAIL))  PASS: $PASS  FAIL: $FAIL"; [[ $FAIL -eq 0 ]]
