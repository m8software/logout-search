#!/usr/bin/env bash
################################################################################
# Unit tests for search_api's (fetched_at, value) caches right after a machine boots
################################################################################
# time.monotonic() counts from the MACHINE's boot. The caches used to start at
# (0.0, empty), which looked fresh for the first TTL after a reboot: tables() kept its
# empty start value and every search answered "200, zero results" for a minute
# (repro/407 on the buildbox lab: 153 empty answers). Here the real search_api.py is loaded
# with a stand-in psycopg and a clock reading 5 s after boot; each cache must fetch on
# its first call. No database, no network.
#
# Usage: bash tests/unit/test-search-cache-boot.sh
################################################################################
set -uo pipefail

SCRIPT_DIR_T="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR_T/../.." && pwd)"
API="${SEARCH_API_PY:-$REPO_ROOT/apps/search/serve/api/search_api.py}"

PASS=0
FAIL=0
RED=$'\033[0;31m'; GREEN=$'\033[0;32m'; NC=$'\033[0m'
ok()  { echo "  ${GREEN}PASS${NC} $1"; PASS=$((PASS+1)); }
bad() { echo "  ${RED}FAIL${NC} $1"; shift; [[ $# -gt 0 ]] && printf '       %s\n' "$@"; FAIL=$((FAIL+1)); }

echo ""; echo "=== caches fetch on first use, 5 s after boot ==="
out=$(python3 - "$API" <<'PY' 2>&1
import importlib.util, sys, time, types
psycopg = types.ModuleType("psycopg")
class OperationalError(Exception): pass
psycopg.OperationalError = OperationalError
psycopg.connect = lambda *a, **k: (_ for _ in ()).throw(OperationalError("no database in this test"))
sys.modules["psycopg"] = psycopg
time.monotonic = lambda: 5.0                       # the machine booted 5 s ago
import os; sys.path.insert(0, os.path.dirname(os.path.abspath(sys.argv[1])))   # its neighbours (gov_state.py), as `python /app/search_api.py` finds them
spec = importlib.util.spec_from_file_location("search_api", sys.argv[1])
m = importlib.util.module_from_spec(spec); sys.modules["search_api"] = m; spec.loader.exec_module(m)   # the stage_*.py files import it back

class Conn:
    def __init__(self, rows): self.rows, self.calls = rows, 0
    def execute(self, sql, *a):
        self.calls += 1
        return iter(self.rows)

c = Conn([("docs",), ("wiki_docs",)])
print("tables", sorted(m.tables(c)), c.calls)
c2 = Conn([("wiki_docs",)])
print("stored", sorted(m.stored_text(c2)), c2.calls)
# generation(): the database answers through _conn(); swap in a stand-in
class GenConn:
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def execute(self, sql, *a):
        return types.SimpleNamespace(fetchone=lambda: (4242,))
m._conn = lambda: GenConn()
print("generation", m.generation())
PY
)
rc=$?
if [[ $rc -ne 0 ]]; then
    bad "search_api.py did not load with the stand-in psycopg (rc=$rc)" "$(tail -5 <<<"$out")"
else
    if grep -q "^tables \['docs', 'wiki_docs'\] 1$" <<<"$out"; then
        ok "tables() queries the database on its first call 5 s after boot"
    else
        bad "tables() returned its start value instead of querying (searches would be empty)" "$(grep '^tables' <<<"$out")"
    fi
    if grep -q "^stored \['wiki_docs'\] 1$" <<<"$out"; then
        ok "stored_text() queries on its first call 5 s after boot"
    else
        bad "stored_text() returned its start value" "$(grep '^stored' <<<"$out")"
    fi
    if grep -q "^generation 4242$" <<<"$out"; then
        ok "generation() asks the database on its first call 5 s after boot"
    else
        bad "generation() returned its start value (None)" "$(grep '^generation' <<<"$out")"
    fi
fi

echo ""; echo "=== no cache starts at a 0.0 stamp ==="
left=$(grep -n -E '^_[a-z_0-9]+ = \(0\.0, ' "$API" "$(dirname "$API")"/stage_*.py) || true
if [[ -z "$left" ]]; then
    ok "every (fetched_at, value) cache starts at _NEVER"
else
    bad "a cache still starts at 0.0 — it looks fresh for a TTL after boot" "$left"
fi

echo ""
echo "Results: ${PASS} passed, ${FAIL} failed"
[[ $FAIL -eq 0 ]]
