#!/usr/bin/env bash
################################################################################
# Unit tests for search_api's Wikipedia text retrieval interface (SEARCH-TANTIVY-PLAN
# phase 5, ADR-063): SEARCH_WIKI_BACKEND picks Postgres (default) or Tantivy, both
# return WIKI_SQL's row shape (title, lead, bm25), and a Tantivy index that does not
# open is an error, never a quiet fall back to Postgres.
#
# Part 1 needs no database and no tantivy: the real search_api.py with a stand-in
# psycopg. Part 2 builds a four-article index with tools/wiki-tantivy.py and searches
# it; it runs when $PY can import tantivy (pip install tantivy==0.26.2), else SKIPs.
#
# Usage: bash tests/unit/test-search-wiki-text.sh      (PY=<python with tantivy>)
################################################################################
set -uo pipefail

SCRIPT_DIR_T="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR_T/../.." && pwd)"
API="${SEARCH_API_PY:-$REPO_ROOT/apps/search/serve/api/search_api.py}"
TOOL="$REPO_ROOT/apps/search/tools/wiki-tantivy.py"
PY="${PY:-python3}"
export PYTHONDONTWRITEBYTECODE=1   # a stale .pyc would hide a mutation of the same size

PASS=0
FAIL=0
RED=$'\033[0;31m'; GREEN=$'\033[0;32m'; NC=$'\033[0m'
ok()  { echo "  ${GREEN}PASS${NC} $1"; PASS=$((PASS+1)); }
bad() { echo "  ${RED}FAIL${NC} $1"; shift; [[ $# -gt 0 ]] && printf '       %s\n' "$@"; FAIL=$((FAIL+1)); }
expect() {  # expect "<line that must appear in $out>" "<what it proves>"
    if grep -qxF -- "$1" <<<"$out"; then ok "$2"; else bad "$2" "want: $1" "got:  $(head -c 600 <<<"$out")"; fi
}

LOADER='
import importlib.util, sys, types
psycopg = types.ModuleType("psycopg")
sys.modules["psycopg"] = psycopg
import os; sys.path.insert(0, os.path.dirname(os.path.abspath(sys.argv[1])))   # its neighbours (gov_state.py), as `python /app/search_api.py` finds them
spec = importlib.util.spec_from_file_location("search_api", sys.argv[1])
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
'

echo ""; echo "=== Part 1: the interface, no database ==="
out=$(env -u SEARCH_WIKI_BACKEND python3 - "$API" <<PY 2>&1
$LOADER
calls = []
m._rows = lambda sql, params, setup=None: (calls.append((sql, params)), [("Dog", "A dog is ...", 12.5)])[1]
b = m.wiki_text()
print("default", b.name)
rows = m.wiki_rows("dogs", 3, frozenset({"wiki_docs"}))
print("rows", rows)
sql, params = calls[0]
print("sql", "wiki_docs_bm25" in sql and "search_text" in sql, params)
m._wiki_text = None; m.WIKI_BACKEND = "elastic"
try:
    m.wiki_text(); print("bogus accepted")
except ValueError:
    print("bogus refused")
m._wiki_text = None; m.WIKI_BACKEND = "tantivy"; m.WIKI_INDEX = "/nonexistent/wiki-tantivy"
try:
    b = m.wiki_text(); print("missing index served by", b.name)
except Exception:
    print("missing index refused")
PY
)
expect "default postgres" "SEARCH_WIKI_BACKEND unset: the Postgres list serves (default unchanged)"
expect "rows [('Dog', 'A dog is ...', 12.5)]" "the Postgres backend returns WIKI_SQL's rows untouched"
expect "sql True {'q': 'dogs', 'k': 3}" "it runs WIKI_SQL with the stored search_text column, the query and k"
expect "bogus refused" "an unknown SEARCH_WIKI_BACKEND is an error"
expect "missing index refused" "tantivy with no index fails loudly, never falls back to Postgres"
if grep -q 'sub("wiki", wiki_rows, wq, min(5, k), stored)' "$API"; then
    ok "_search() takes the wiki list from wiki_rows() (the flag reaches the engine)"
else
    bad "_search() does not call wiki_rows() — the backend flag would change nothing"
fi

echo ""; echo "=== Part 2: a real Tantivy index ==="
if ! "$PY" -c "import tantivy" 2>/dev/null; then
    echo "  SKIP: $PY cannot import tantivy (PY=<python with tantivy==0.26.2>)"
else
    tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
    # COPY text format: \N = NULL, \t an escaped tab inside a field
    printf '%s\n' \
        $'1\tDog\tdomestic dog|canis familiaris\tSpecies: C. familiaris\tHistory|Breeds\tThe dog is a domesticated descendant of the wolf. Dogs run.' \
        $'2\tRunning\tjogging\t\\N\tTechnique\tRunning is a method of terrestrial locomotion.' \
        $'3\tWolf\tgray wolf\t\\N\t\\N\tThe wolf is a large canine.\\tIt lives in packs.' \
        $'4\tThe Who\twho band\t\\N\t\\N\tThe Who are an English rock band.' > "$tmp/export.tsv"
    if ! bout=$("$PY" "$TOOL" build "$tmp/export.tsv" "$tmp/idx" --heap-mb 50 --threads 1 2>&1); then
        bad "tools/wiki-tantivy.py build failed" "$bout"
    else
        out=$(SEARCH_WIKI_BACKEND=tantivy SEARCH_WIKI_INDEX="$tmp/idx" "$PY" - "$API" <<PY 2>&1
$LOADER
from concurrent.futures import ThreadPoolExecutor
b = m.wiki_text()
print("backend", b.name, b.num_docs)
r = m.wiki_rows("running dogs", 5)
print("shape", all(isinstance(t, str) and isinstance(l, str) and isinstance(s, float) for t, l, s in r))
print("top", r[0][0])
print("stemmed", sorted(t for t, _l, _s in m.wiki_rows("run", 5)))
print("fields", [m.wiki_rows(w, 5)[0][0] for w in ("technique", "species", "jogging")])
print("stopwords", m.wiki_rows("the of and", 5))
print("k", len(m.wiki_rows("dog wolf running band", 2)))
print("snippet", repr(m.wiki_rows("wolf packs", 1)[0][1]))
print("null", m.wiki_rows("n", 5))
want = [m.wiki_rows(q, 5) for q in ("dog", "wolf", "running", "band")] * 25
with ThreadPoolExecutor(12) as ex:
    got = list(ex.map(lambda q: m.wiki_rows(q, 5), ["dog", "wolf", "running", "band"] * 25))
print("threads", got == want)
PY
)
        expect "backend tantivy 4" "SEARCH_WIKI_BACKEND=tantivy opens the index (4 articles)"
        expect "shape True" "rows are (title, lead, float bm25), WIKI_SQL's shape"
        expect "top Dog" "'running dogs' ranks Dog first (title + body terms)"
        expect "stemmed ['Dog', 'Running']" "the English stemmer matches run / runs / running"
        expect "fields ['Running', 'Dog', 'Running']" "headings, infobox and aliases are searched (the body is all five fields)"
        expect "stopwords []" "a query of only Postgres stopwords returns no rows, not an error"
        expect "k 2" "k limits the rows"
        expect "snippet 'The wolf is a large canine.\\tIt lives in packs.'" "the lead comes back from the stored snippet, COPY escapes decoded"
        expect "null []" "\\N fields index as empty, not as the word 'n'"
        expect "threads True" "12 threads share one searcher and get the same rows"
    fi
fi

echo ""
echo "Results: $PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]]
