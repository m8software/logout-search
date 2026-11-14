#!/usr/bin/env bash
################################################################################
# Unit tests for SEARCH_WIKI_BACKEND=tantivy-release (SEARCH-RELEASE-FORMAT rule 3): the
# Wikipedia text index stores nothing but a fast doc_id, and search_api turns the hits
# into (title, lead, score) rows through the release lookups (registry title + card lead).
#
# The index is built here with tantivy-py in the release's schema (doc_id fast + indexed,
# not stored; title, body not stored; the engine's own en_pg analyzer); the lookups are a
# stand-in with the ReleaseLookups interface (name, title(d), card(d)). Needs tantivy
# (PY=<python with tantivy==0.26.2>), else SKIPs.
#
# Usage: bash tests/unit/test-search-wiki-release.sh
################################################################################
set -uo pipefail
SCRIPT_DIR_T="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR_T/../.." && pwd)"
API="${SEARCH_API_PY:-$REPO_ROOT/apps/search/serve/api/search_api.py}"
PY="${PY:-python3}"
export PYTHONDONTWRITEBYTECODE=1
PASS=0; FAIL=0
RED=$'\033[0;31m'; GREEN=$'\033[0;32m'; NC=$'\033[0m'
ok()  { echo "  ${GREEN}PASS${NC} $1"; PASS=$((PASS+1)); }
bad() { echo "  ${RED}FAIL${NC} $1"; shift; [[ $# -gt 0 ]] && printf '       %s\n' "$@"; FAIL=$((FAIL+1)); }
expect() { if grep -qxF -- "$1" <<<"$out"; then ok "$2"; else bad "$2" "want: $1" "got:  $(head -c 800 <<<"$out")"; fi; }

echo ""; echo "=== tantivy-release: doc_id hits -> (title, lead, score) through the release lookups ==="
if ! "$PY" -c "import tantivy" 2>/dev/null; then
    echo "  SKIP: $PY cannot import tantivy (PY=<python with tantivy==0.26.2>)"
    echo ""; echo "Total: 0  PASS: 0  FAIL: 0 (skipped)"; exit 0
fi
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
out=$(SEARCH_WIKI_BACKEND=tantivy-release SEARCH_WIKI_INDEX="$tmp/idx" "$PY" - "$API" "$tmp" <<'PY' 2>&1
import importlib.util, sys, types, os
psycopg = types.ModuleType("psycopg"); sys.modules["psycopg"] = psycopg
spec = importlib.util.spec_from_file_location("search_api", sys.argv[1])
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
import tantivy
tmp = sys.argv[2]
sb = tantivy.SchemaBuilder()
sb.add_integer_field("doc_id", stored=False, indexed=True, fast=True)
sb.add_text_field("title", stored=False, tokenizer_name="en_pg", index_option="position")
sb.add_text_field("body", stored=False, tokenizer_name="en_pg", index_option="freq")
os.makedirs(tmp + "/idx")
ix = tantivy.Index(sb.build(), path=tmp + "/idx")
ix.register_tokenizer("en_pg", m.TantivyWikiText.build_analyzer(tantivy))
w = ix.writer(heap_size=50_000_000, num_threads=1)
DOCS = {268435457: ("Dog", "The dog is a domesticated descendant of the wolf. Dogs run."),
        268435458: ("Running", "Running is a method of terrestrial locomotion."),
        268435459: ("Wolf", "The wolf is a large canine. It lives in packs."),
        268435460: ("Gone", "A removed article about dogs and wolves.")}
for d, (t, lead) in DOCS.items():
    doc = tantivy.Document(); doc.add_integer("doc_id", d); doc.add_text("title", t); doc.add_text("body", t + " " + lead)
    w.add_document(doc)
w.commit(); w.wait_merging_threads()

class FakeLookups:
    name = "release"
    def title(self, d): return None if d == 268435460 else DOCS[d][0]
    def card(self, d): return {"l": DOCS[d][1] * 10}          # a long lead: the snippet is cut at 300
m.lookups = lambda: FakeLookups()
b = m.wiki_text()
print("backend", b.name, b.num_docs)
r = m.wiki_rows("running dogs", 5)
print("shape", all(isinstance(t, str) and isinstance(l, str) and isinstance(s, float) for t, l, s in r))
print("top", r[0][0])
print("titles", sorted(t for t, _l, _s in r))
print("lead", [l for t, l, _s in r if t == "Dog"][0][:30])
print("snippet cut", all(len(l) <= 300 for _t, l, _s in r))
print("stopwords", m.wiki_rows("the of and", 5))
print("k", len(m.wiki_rows("dog wolf running", 2)))
print("removed skipped", "Gone" not in [t for t, _l, _s in m.wiki_rows("removed article", 5)])
m.lookups = lambda: None
try:
    m.wiki_rows("dog", 3); print("no lookups served")
except RuntimeError as e:
    print("no lookups refused")
m._wiki_text = None; m.WIKI_BACKEND = "tantivy-release"; os.environ["SEARCH_WIKI_INDEX"] = "/nonexistent/wiki"
try:
    m.wiki_text(); print("missing index served")
except Exception:
    print("missing index refused")
PY
)
expect "backend tantivy-release 4" "SEARCH_WIKI_BACKEND=tantivy-release opens the index and counts its articles"
expect "shape True" "rows are (title str, lead str, score float), the shape TantivyWikiText returns"
expect "top Dog" "the stemmed query 'running dogs' ranks the article about dogs first"
expect "titles ['Dog', 'Running']" "titles come from the lookups for every hit (Gone is skipped, Wolf has neither word)"
expect "lead The dog is a domesticated desc" "the lead comes from the card (l)"
expect "snippet cut True" "the lead is cut at 300 characters like _wiki() shows it"
expect "stopwords []" "a query of only stopwords matches nothing"
expect "k 2" "k limits the rows"
expect "removed skipped True" "a doc_id the registry no longer knows is skipped, not shown"
expect "no lookups refused" "without SEARCH_LOOKUP_BACKEND=release the backend fails loudly"
expect "missing index refused" "a missing index is an error, never a quiet fall back"
echo ""; echo "Total: $((PASS+FAIL))  PASS: $PASS  FAIL: $FAIL"; [[ $FAIL -eq 0 ]]
