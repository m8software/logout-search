#!/usr/bin/env bash
################################################################################
# Unit tests for search_api's lookup backend (T4, ADR-063): SEARCH_LOOKUP_BACKEND picks
# Postgres (default) or one SQLite file; with SQLite a pooled connection becomes a
# LookupRouter that answers lookups from the file, sends text/vector SQL to Postgres, and
# refuses a lookup it has no port for (never a quiet fall back to Postgres).
#
# Part 1 needs no database: the real search_api.py with a stand-in psycopg.
# Part 2 builds a tiny lookup file + Tantivy typo index and asks it; it runs when $PY can
# import tantivy (pip install tantivy==0.26.2), else SKIPs.
#
# Usage: bash tests/unit/test-search-lookups-sqlite.sh      (PY=<python with tantivy>)
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

LOADER='
import importlib.util, sys, types
psycopg = types.ModuleType("psycopg")
class Error(Exception): pass
psycopg.Error = Error
sys.modules["psycopg"] = psycopg
sys.path.insert(0, sys.argv[1])
spec = importlib.util.spec_from_file_location("search_api", sys.argv[1] + "/search_api.py")
m = importlib.util.module_from_spec(spec); sys.modules["search_api"] = m; spec.loader.exec_module(m)
'

echo ""; echo "=== Part 1: the switch and the router, no database ==="
out=$(env -u SEARCH_LOOKUP_BACKEND python3 - "$API_DIR" <<PY 2>&1
$LOADER
print("default", m.LOOKUP_BACKEND, m.lookups())
print("sqlite3 not loaded", "lookups_sqlite" not in sys.modules)
m.LOOKUP_BACKEND = "elastic"
try:
    m.lookups(); print("bogus accepted")
except ValueError:
    print("bogus refused")
m.LOOKUP_BACKEND = "sqlite"; m.LOOKUP_DB = "/nonexistent/lookup.sqlite"
try:
    m.lookups(); print("missing file served")
except Exception:
    print("missing file refused")
import lookups_sqlite as L
class PG:
    def __init__(self): self.seen = []
    def execute(self, sql, params=None):
        self.seen.append(sql)
        return [("docs",), ("places",), ("kind_docs",), ("wiki_docs",)] if sql == m._TABLES_SQL else ["pg"]
class Lk:
    tables = frozenset({"places", "place_kinds"})
    def present(self, pg): return L.SqliteLookups.present(self, pg)
    def execute(self, sql, params=None):
        if sql == m.NAV_SITES_SQL: return ["lite"]
        raise L.NoPort(sql)
pg = PG(); r = L.LookupRouter(pg, Lk(), m._TABLES_SQL)
print("lookup", r.execute(m.NAV_SITES_SQL, {"keys": ["x"]}), len(pg.seen))
print("text", r.execute(m.WIKI_SQL, {"q": "x", "k": 1}))
print("set", r.execute("SET pg_trgm.similarity_threshold = 0.6"))
try:
    r.execute(m.HOST_NAV_SQL, {"keys": ["x"]}); print("unported lookup went to postgres")
except L.NoPort:
    print("unported lookup refused")
print("tables", sorted(t for (t,) in r.execute(m._TABLES_SQL)))
print("trgm", round(L.similarity("word", "two words"), 6), L.similarity("İsa", "isa"))
PY
)
expect "default postgres None" "SEARCH_LOOKUP_BACKEND unset: Postgres serves the lookups (default unchanged)"
expect "sqlite3 not loaded True" "the Postgres default never imports the SQLite backend"
expect "bogus refused" "an unknown SEARCH_LOOKUP_BACKEND is an error"
expect "missing file refused" "sqlite with no file fails loudly at open"
expect "lookup ['lite'] 0" "a ported lookup is answered by SQLite, Postgres never asked"
expect "text ['pg']" "a text-list query (BM25) goes to Postgres"
expect "set ['pg']" "session SETs go to Postgres"
expect "unported lookup refused" "a lookup with no SQLite port raises instead of asking Postgres"
expect "tables ['docs', 'places', 'wiki_docs']" "tables(): the file decides lookup tables (no kinds in it = kinds off), Postgres the rest"
expect "trgm 0.363636 1.0" "pg_trgm similarity: padded words, float4, glibc-style lowercase of İ"

echo ""; echo "=== Part 2: a tiny lookup file ==="
if ! "$PY" -c "import tantivy" 2>/dev/null; then
    echo "  SKIP: $PY cannot import tantivy (PY=<python with tantivy==0.26.2>)"
else
    tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
    out=$(SEARCH_LOOKUP_BACKEND=sqlite SEARCH_LOOKUP_DB="$tmp/l.sqlite" SEARCH_TYPO_INDEX="$tmp/typo" "$PY" - "$API_DIR" <<PY 2>&1
import sqlite3, tantivy, os
c = sqlite3.connect("$tmp/l.sqlite")
c.executescript("""
CREATE TABLE place_kinds (id INTEGER PRIMARY KEY, kind TEXT UNIQUE);
CREATE VIRTUAL TABLE place_names USING fts5(t, content='', contentless_delete=1, detail='none', tokenize='trigram');
CREATE TABLE wiki_links (platform TEXT, qid TEXT, value TEXT, url TEXT, name TEXT, description TEXT, wiki_title TEXT,
  sitelinks INTEGER, key TEXT, hkey TEXT, wkey TEXT, skey TEXT, vord INTEGER);
INSERT INTO wiki_links VALUES ('x', 'Q1', 'MahindraRise', 'https://x.com/MahindraRise', 'M', '', 'M&M', 41, NULL, NULL, NULL, NULL, 2);
INSERT INTO wiki_links VALUES ('x', 'Q1', 'mahindrarise', 'https://x.com/mahindrarise', 'M', '', 'M&M', 41, NULL, NULL, NULL, NULL, 1);
CREATE TABLE suggest_items (id INTEGER PRIMARY KEY, kind TEXT, name TEXT, url TEXT, domain TEXT, wiki TEXT, pop REAL,
  hist REAL, score REAL, demand REAL, descr TEXT);
CREATE TABLE suggest_keys (key TEXT, id INTEGER, pos INTEGER, kind TEXT, score REAL, PRIMARY KEY (key, id, pos)) WITHOUT ROWID;
INSERT INTO suggest_items VALUES (1, 'site', 'Amazon', 'https://amazon.com/', 'amazon.com', NULL, 1, 0, 0.699999988079071, 9, NULL);
INSERT INTO suggest_keys VALUES ('amazon', 1, 0, 'site', 1.0);
""")
c.commit(); c.close()
sb = tantivy.SchemaBuilder(); sb.add_text_field("key", stored=True, tokenizer_name="raw", index_option="basic")
os.makedirs("$tmp/typo"); ix = tantivy.Index(sb.build(), path="$tmp/typo"); w = ix.writer(heap_size=50_000_000, num_threads=1)
for k in ("amazon", "amazing", "lazon"):
    w.add_document(tantivy.Document(key=k))
w.commit(); w.wait_merging_threads()
$LOADER
lk = m.lookups()
print("opened", lk.name, lk.typo_keys)
print("profiles", [r[4] for r in lk.execute(m.PROFILES_SQL, {"t": "M&M", "p": ["x"]})])
print("typo", [(r[0], r[2], r[3]) for r in lk.execute(m.TYPO_SQL, {"t": "amzaon"})])
print("present", sorted(lk.present(frozenset({"kind_docs", "docs"}))))
PY
)
    expect "opened sqlite 3" "SEARCH_LOOKUP_BACKEND=sqlite opens the file and the typo index"
    expect "profiles ['mahindrarise']" "PROFILES orders equal-length handles by Postgres's en_US rank (vord), not bytes"
    expect "typo [('Amazon', 0.7, 'amazon')]" "typo: Tantivy fuzzy finds amazon for amzaon; its float4 score comes back as 0.7"
    expect "present ['docs', 'suggest_items', 'suggest_keys', 'wiki_links']" "a file without kinds tables reports no kinds tier"
fi

echo ""
echo "Results: $PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]]
