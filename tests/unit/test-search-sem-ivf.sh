#!/usr/bin/env bash
################################################################################
# Unit tests for the release's meaning search (T3b, ADR-063): sem_ivf.py searches the IVF
# folder over nomic 1-bit codes; SEARCH_SEM_BACKEND picks Postgres (default) or ivf.
#
# Part 1 needs only numpy: a tiny IVF folder written here, searched, and checked against a
# brute-force Hamming scan (all lists probed = exact), plus the bit rule and the guards.
# Part 2 loads the real search_api.py with a stand-in psycopg: the switch, the default, the
# refusals, and the rows _sem_rows builds from IVF hits.
#
# Usage: bash tests/unit/test-search-sem-ivf.sh      (PY=<python with numpy >= 2>)
################################################################################
set -uo pipefail

SCRIPT_DIR_T="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR_T/../.." && pwd)"
API_DIR="$REPO_ROOT/apps/search/serve/api"
PY="${PY:-python3}"
export PYTHONDONTWRITEBYTECODE=1

PASS=0
FAIL=0
RED=$'\033[0;31m'; GREEN=$'\033[0;32m'; NC=$'\033[0m'
ok()  { echo "  ${GREEN}PASS${NC} $1"; PASS=$((PASS+1)); }
bad() { echo "  ${RED}FAIL${NC} $1"; shift; [[ $# -gt 0 ]] && printf '       %s\n' "$@"; FAIL=$((FAIL+1)); }
expect() {  # expect "<line that must appear in $out>" "<what it proves>"
    if grep -qxF -- "$1" <<<"$out"; then ok "$2"; else bad "$2" "want: $1" "got:  $(head -c 900 <<<"$out")"; fi
}

if ! "$PY" -c "import numpy; assert hasattr(numpy, 'bitwise_count')" 2>/dev/null; then
    echo "  SKIP: $PY has no numpy >= 2 (bitwise_count)"; echo "Results: 0 passed, 0 failed"; exit 0
fi

tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT

# A tiny IVF folder: 3,000 random codes in 40 lists (nearest of 40 random centres), ids = 1000 + row.
MAKE='
import json, os, numpy as np
def make(d, n=3000, L=40, release="rel-test", fmt="logout-sem-ivf/1", seed=7):
    rng = np.random.default_rng(seed)
    codes = rng.integers(0, 256, (n, 96), dtype=np.uint8)
    cent = rng.integers(0, 256, (L, 96), dtype=np.uint8)
    d2 = np.bitwise_count(codes.view(np.uint64)[:, None, :] ^ cent.view(np.uint64)[None, :, :]).sum(2)
    assign = d2.argmin(1)
    order = np.argsort(assign, kind="stable")
    offs = np.zeros(L + 1, np.int64); offs[1:] = np.cumsum(np.bincount(assign, minlength=L))
    os.makedirs(d, exist_ok=True)
    cent.tofile(d + "/centroids.u8"); offs.tofile(d + "/offsets.i64")
    codes[order].tofile(d + "/codes.u8"); (1000 + order).astype(np.int32).tofile(d + "/ids.i32")
    json.dump({"format": fmt, "release": release, "rows": n, "lists": L}, open(d + "/ivf.json", "w"))
    return codes
'

echo ""; echo "=== Part 1: the IVF folder, numpy only ==="
out=$("$PY" - "$API_DIR" "$tmp" <<PY 2>&1
import sys, numpy as np
sys.path.insert(0, sys.argv[1]); tmp = sys.argv[2]
$MAKE
import sem_ivf as S
codes = make(tmp + "/ivf")
ix = S.IvfIndex(tmp + "/ivf", "rel-test")
print("rows", len(ix))
rng = np.random.default_rng(3)
exact_ok = subset_ok = True
for _ in range(40):
    q = rng.integers(0, 256, 96, dtype=np.uint8)
    d = np.bitwise_count(codes.view(np.uint64) ^ q.view(np.uint64)).sum(1)
    got = ix.search(q, 10, probes=40)
    # exact = the brute-force top-10 distances, each id at its true distance, nearest first
    if ([dd for _p, dd in got] != sorted(int(x) for x in d)[:10] or any(dd != int(d[p - 1000]) for p, dd in got)
            or len({p for p, _d in got}) != 10):
        exact_ok = False
    few = ix.search(q, 10, probes=3)
    if any(p not in {pp for pp in range(1000, 1000 + len(d))} or dd != int(d[p - 1000]) for p, dd in few):
        subset_ok = False
print("all lists probed = exact top 10, nearest first", exact_ok)
print("few lists probed: real ids, true distances", subset_ok)
print("repeatable", ix.search(codes[5], 5, 7) == ix.search(codes[5], 5, 7))
print("a code finds itself at distance 0", ix.search(codes[123], 1, 40) == [(1123, 0)])
v = np.full(768, -1.0); v[0] = 0.0; v[9] = 2.5
c = S.code_of(v)
print("bit rule", len(c), int(c[0]), int(c[1]), int(c[2:].sum()))
h = np.array([[3.0, 4.0] + [0.0] * 766, [9.0] * 768]); m = np.array([1, 0])
print("pool", round(float(np.linalg.norm(S.pool(h, m))), 6), round(float(S.pool(h, m)[0]), 6))
for label, kw in (("release", {"release": "other"}), ("format", {"fmt": "logout-sem-ivf/0"})):
    make(tmp + "/bad-" + label, n=100, L=4, **kw)
    try:
        S.IvfIndex(tmp + "/bad-" + label, "rel-test"); print(label, "mismatch accepted")
    except ValueError:
        print(label, "mismatch refused")
PY
)
expect "rows 3000" "IvfIndex opens the folder"
expect "all lists probed = exact top 10, nearest first True" "probing every list returns the brute-force Hamming top 10, nearest first"
expect "few lists probed: real ids, true distances True" "probing a few lists returns real ids with their true distances"
expect "repeatable True" "the same search gives the same answer (ties broken by row)"
expect "a code finds itself at distance 0 True" "a stored code is its own nearest neighbour"
expect "bit rule 96 128 64 0" "code_of: bit i = component i >= 0, component 0 is the top bit of byte 0"
expect "pool 1.0 0.6" "pool: masked mean, unit length (padding ignored)"
expect "release mismatch refused" "a folder built for another release is refused"
expect "format mismatch refused" "a folder in another format is refused"

echo ""; echo "=== Part 1b: ivf_kern.c gives the numpy path's answer (2026-11-04) ==="
CC_BIN="$(command -v cc || command -v gcc || true)"
if [[ -z "$CC_BIN" ]]; then
    echo "  SKIP: no C compiler (the engine image builds the kernel in its own stage)"
elif ! "$CC_BIN" -O3 -Wall -Wextra -Werror -shared -fPIC -o "$tmp/libivf_kern.so" "$API_DIR/ivf_kern.c" 2>"$tmp/cc.err"; then
    bad "ivf_kern.c compiles without warnings" "$(head -c 900 "$tmp/cc.err")"
else
    ok "ivf_kern.c compiles without warnings"
    out=$(SEARCH_IVF_KERN_LIB="$tmp/libivf_kern.so" "$PY" - "$API_DIR" "$tmp" <<PY 2>&1
import sys, numpy as np
sys.path.insert(0, sys.argv[1]); tmp = sys.argv[2]
$MAKE
import sem_ivf as S
print("loaded", S._kern is not None)
codes = make(tmp + "/ivf-k", n=4000, L=50, seed=11)
ix = S.IvfIndex(tmp + "/ivf-k", "rel-test")
rng = np.random.default_rng(5)
qs = [rng.integers(0, 256, 96, dtype=np.uint8) for _ in range(60)] + [codes[i] for i in (0, 17, 3999)]
# a few codes with every bit the same make long ties at the k-th place
qs += [np.zeros(96, np.uint8), np.full(96, 255, np.uint8)]
kern, diff, n = S._kern, 0, 0
for q in qs:
    for k, probes in ((1, 1), (5, 7), (10, 50), (30, 3), (200, 50)):
        S._kern = None; a = ix.search(q, k, probes)
        S._kern = kern; b = ix.search(q, k, probes)
        diff += a != b; n += 1
print("same", diff == 0, n)
print("k above 4096 falls back", len(ix.search(qs[0], 5000, 2)) > 0)
PY
)
    expect "loaded True" "sem_ivf loads libivf_kern.so"
    expect "same True 325" "the kernel returns exactly the numpy hits, ties at the cut included, over 325 searches"
    expect "k above 4096 falls back True" "a k beyond the kernel's limit uses the numpy path"
    out=$(SEARCH_IVF_KERN=0 SEARCH_IVF_KERN_LIB="$tmp/libivf_kern.so" "$PY" -c "import sys; sys.path.insert(0, '$API_DIR'); import sem_ivf as S; print('off', S._kern is None)" 2>&1)
    expect "off True" "SEARCH_IVF_KERN=0 keeps the numpy path"
fi

echo ""; echo "=== Part 2: the switch in search_api.py, no database ==="
out=$(env -u SEARCH_SEM_BACKEND "$PY" - "$API_DIR" "$tmp" <<PY 2>&1
import importlib.util, sys, types
psycopg = types.ModuleType("psycopg")
class Error(Exception): pass
psycopg.Error = Error
sys.modules["psycopg"] = psycopg
sys.path.insert(0, sys.argv[1]); tmp = sys.argv[2]
spec = importlib.util.spec_from_file_location("search_api", sys.argv[1] + "/search_api.py")
m = importlib.util.module_from_spec(spec); sys.modules["search_api"] = m; spec.loader.exec_module(m)
print("default", m.SEM_BACKEND, m.sem_ivf())
print("sem_ivf not loaded", "sem_ivf" not in sys.modules)
m.SEM_BACKEND = "faiss"
try:
    m.sem_ivf(); print("bogus accepted")
except ValueError:
    print("bogus refused")
m.SEM_BACKEND = "ivf"; m.SEM_IVF = "/nonexistent/ivf2600"
try:
    m.sem_ivf(); print("missing folder served")
except Exception:
    print("missing folder refused")
# _sem_rows from IVF hits: stand-in index + encoder, the docs query recorded
import sem_ivf as S, numpy as np
class Ix:
    def search(self, code, k, probes):
        self.args = (len(code), k, probes); return [(7, 0), (9, 384), (8, 100)]
class Enc:
    def __call__(self, t): return np.ones(768)
ix = Ix(); S._state.update(ix=ix, enc=Enc())
seen = []
def rows(sql, params, setup=None):
    seen.append((sql, params)); return [(7, "Seven", "lead 7"), (9, "Nine", "lead 9")]
m._rows = rows
print("rows", m._sem_rows("anything"))
print("probes", ix.args, m.SEM_PROBES)
print("docs query by page_id", seen[0][0] == m.SEM_IVF_DOCS_SQL and seen[0][1] == {"ids": [7, 9, 8]})
PY
)
expect "default postgres None" "SEARCH_SEM_BACKEND unset: Postgres meaning search (default unchanged)"
expect "sem_ivf not loaded True" "the Postgres default never imports sem_ivf"
expect "bogus refused" "an unknown SEARCH_SEM_BACKEND is an error"
expect "missing folder refused" "ivf with no folder fails loudly, never falls back to wiki_vec"
expect "rows [('Seven', 'lead 7', 1.0), ('Nine', 'lead 9', 0.5)]" "_sem_rows: IVF order kept, sim = share of bits agreeing, a page missing from wiki_docs dropped"
expect "probes (96, 5, 200) 200" "_sem_rows asks for 5 results at SEARCH_SEM_PROBES (default 200) with a 96-byte code"
expect "docs query by page_id True" "titles and leads come from one wiki_docs query by page_id"

echo ""; echo "=== onnxruntime sessions stay lean (_ort_lean; memory measured on testbox 2026-10-05) ==="
out=$(API_DIR="$API_DIR" "$PY" - <<'PY' 2>&1
import os, sys, types
sys.path.insert(0, os.environ["API_DIR"])
import sem_ivf as S
class O:
    def __init__(self): self.enable_cpu_mem_arena, self.enable_mem_pattern, self.conf = True, True, {}
    def add_session_config_entry(self, k, v): self.conf[k] = v
for env in ("", "0", "1"):
    if env: os.environ["SEARCH_ORT_PREPACK"] = env
    else: os.environ.pop("SEARCH_ORT_PREPACK", None)
    o = O(); S._ort_lean(o)
    print(f"prepack={env or 'unset'} arena={o.enable_cpu_mem_arena} pattern={o.enable_mem_pattern} disable_prepacking={o.conf.get('session.disable_prepacking', '-')}")
src = open(os.path.join(os.environ["API_DIR"], "search_api.py")).read()
print("every search_api session lean", src.count("opts = ort.SessionOptions()") == len([l for l in src.splitlines() if l.strip() == "_ort_lean(opts)"]) > 0)
dock = open(os.path.join(os.environ["API_DIR"], "Dockerfile")).read()
print("image sets MALLOC_ARENA_MAX=2", "ENV MALLOC_ARENA_MAX=2" in dock)
PY
)
expect "prepack=unset arena=False pattern=False disable_prepacking=-" "no arena / memory pattern; weights repacked by default (fast)"
expect "prepack=0 arena=False pattern=False disable_prepacking=1" "SEARCH_ORT_PREPACK=0 skips the repacked copies (~300 MB less for nomic)"
expect "prepack=1 arena=False pattern=False disable_prepacking=-" "SEARCH_ORT_PREPACK=1 keeps them"
expect "every search_api session lean True" "every onnxruntime session in search_api goes through _ort_lean"
expect "image sets MALLOC_ARENA_MAX=2 True" "the api image limits malloc arenas (2.67 -> 2.12 GB after 3,060 queries)"

echo ""
echo "Results: $PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]]
