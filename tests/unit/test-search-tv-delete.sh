#!/usr/bin/env bash
################################################################################
# Unit tests for release_v1.tv_delete_ids / tv_id_field and cmd_tvmonth's guard:
# writer.delete_documents_by_term(field, n) builds an i64 term and silently deletes NOTHING on the
# release's u64 doc_id field; tv_delete_ids deletes through a term query (u64 and i64 alike), and
# tvmonth refuses a doc_id index (it re-adds page_id documents). Needs tantivy (PY=<python with tantivy==0.26.2>).
#
# Usage: bash tests/unit/test-search-tv-delete.sh
################################################################################
set -uo pipefail
SCRIPT_DIR_T="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR_T/../.." && pwd)"
REL_DIR="${SEARCH_RELEASE_SRC:-$REPO_ROOT/apps/search/release}"
PY="${PY:-python3}"
export PYTHONDONTWRITEBYTECODE=1
PASS=0; FAIL=0
RED=$'\033[0;31m'; GREEN=$'\033[0;32m'; NC=$'\033[0m'
ok()  { echo "  ${GREEN}PASS${NC} $1"; PASS=$((PASS+1)); }
bad() { echo "  ${RED}FAIL${NC} $1"; shift; [[ $# -gt 0 ]] && printf '       %s\n' "$@"; FAIL=$((FAIL+1)); }
expect() { if grep -qxF -- "$1" <<<"$out"; then ok "$2"; else bad "$2" "want: $1" "got:  $(head -c 900 <<<"$out")"; fi; }

echo ""; echo "=== tv_delete_ids: deletes real ids from a u64 doc_id index and an i64 page_id index ==="
if ! "$PY" -c "import tantivy" 2>/dev/null; then
    echo "  SKIP: $PY cannot import tantivy (PY=<python with tantivy==0.26.2>)"
    echo ""; echo "Total: 0  PASS: 0  FAIL: 0 (skipped)"; exit 0
fi
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
out=$("$PY" - "$REL_DIR" "$tmp" <<'PY' 2>&1
import os, sys
sys.path.insert(0, sys.argv[1])
import tantivy, release_v1 as rv
tmp = sys.argv[2]
IDS = (1 << 28 | 1, 1 << 28 | 2, 1 << 28 | 3)            # registry doc_ids: type 1 (wiki) << 28 | seq
def build(field, unsigned):
    p = f"{tmp}/{field}"; os.makedirs(p)
    sb = tantivy.SchemaBuilder()
    (sb.add_unsigned_field if unsigned else sb.add_integer_field)(field, stored=False, indexed=True, fast=True)
    sb.add_text_field("body", stored=False)
    ix = tantivy.Index(sb.build(), path=p)
    w = ix.writer(heap_size=15_000_000, num_threads=1)
    for i in IDS:
        d = tantivy.Document(); (d.add_unsigned if unsigned else d.add_integer)(field, i); d.add_text("body", "apple"); w.add_document(d)
    w.commit(); w.wait_merging_threads()
    return p, ix
def left(ix, field):
    ix.reload(); s = ix.searcher()
    return sorted(s.fast_field_values(field, [a for _s, a in s.search(tantivy.Query.term_query(ix.schema, "body", "apple"), 10).hits]))
for field, unsigned in (("doc_id", True), ("page_id", False)):
    p, ix = build(field, unsigned)
    w = ix.writer(heap_size=15_000_000, num_threads=1)
    n = rv.tv_delete_ids(w, ix.schema, field, IDS[:2]); w.commit(); w.wait_merging_threads()
    print(f"{field}: reported {n}, left {left(ix, field) == [IDS[2]]}")
    print(f"{field}: id field {rv.tv_id_field(p)}")
    if unsigned:                                          # the bug itself, so the test notices if tantivy-py ever fixes it
        w = ix.writer(heap_size=15_000_000, num_threads=1); w.delete_documents_by_term(field, IDS[2]); w.commit(); w.wait_merging_threads()
        print(f"by_term on u64 still a no-op: {left(ix, field) == [IDS[2]]}")
try:
    rv.cmd_tvmonth(f"{tmp}/doc_id", "")
    print("tvmonth on a doc_id index: ran")
except SystemExit as e:
    print("tvmonth on a doc_id index: refused" if "update_pkg" in str(e) else f"tvmonth: {e}")
PY
)
expect "doc_id: reported 2, left True" "u64 doc_id index: the two ids are gone, the third stays"
expect "page_id: reported 2, left True" "i64 page_id index: the two ids are gone, the third stays"
expect "doc_id: id field doc_id" "a release folder's id field is doc_id"
expect "page_id: id field page_id" "a proto folder's id field is page_id"
expect "by_term on u64 still a no-op: True" "delete_documents_by_term still deletes nothing on u64 (why the helper exists)"
expect "tvmonth on a doc_id index: refused" "tvmonth refuses a release index and points at update_pkg"

echo ""; echo "Total: $((PASS+FAIL))  PASS: $PASS  FAIL: $FAIL"
[[ $FAIL -eq 0 ]]
