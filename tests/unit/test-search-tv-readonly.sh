#!/usr/bin/env bash
################################################################################
# Unit tests for apps/search/serve/api/tv_open.py (SEARCH-RELEASE-FORMAT rule 10):
# tantivy-py's Index.open() takes .tantivy-meta.lock for writing, so a read-only release folder
# (no lock files: tvnormalize removes them; every file chmod a-w) failed with PermissionDenied.
# open_index() opens such a folder through a writable overlay and never writes into it.
# Also: search_api's wiki backends open through it. Needs tantivy (PY=<python with tantivy==0.26.2>).
#
# Usage: bash tests/unit/test-search-tv-readonly.sh
################################################################################
set -uo pipefail
SCRIPT_DIR_T="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR_T/../.." && pwd)"
API_DIR="${SEARCH_API_DIR:-$REPO_ROOT/apps/search/serve/api}"
PY="${PY:-python3}"
export PYTHONDONTWRITEBYTECODE=1
PASS=0; FAIL=0
RED=$'\033[0;31m'; GREEN=$'\033[0;32m'; NC=$'\033[0m'
ok()  { echo "  ${GREEN}PASS${NC} $1"; PASS=$((PASS+1)); }
bad() { echo "  ${RED}FAIL${NC} $1"; shift; [[ $# -gt 0 ]] && printf '       %s\n' "$@"; FAIL=$((FAIL+1)); }
expect() { if grep -qxF -- "$1" <<<"$out"; then ok "$2"; else bad "$2" "want: $1" "got:  $(head -c 900 <<<"$out")"; fi; }

echo ""; echo "=== tv_open.open_index: a read-only release folder opens and searches ==="
if ! "$PY" -c "import tantivy" 2>/dev/null; then
    echo "  SKIP: $PY cannot import tantivy (PY=<python with tantivy==0.26.2>)"
    echo ""; echo "Total: 0  PASS: 0  FAIL: 0 (skipped)"; exit 0
fi
if [[ "$(id -u)" == 0 ]]; then
    echo "  SKIP: root ignores file modes, the permission failure cannot be reproduced"
    echo ""; echo "Total: 0  PASS: 0  FAIL: 0 (skipped)"; exit 0
fi
tmp=$(mktemp -d)
cleanup() { chmod -R u+w "$tmp" 2>/dev/null; rm -rf "$tmp"; }
trap cleanup EXIT
out=$(LOGOUT_TV_OVERLAY_DIR="$tmp/overlay" "$PY" - "$API_DIR" "$tmp" <<'PY' 2>&1
import os, stat, subprocess, sys
sys.path.insert(0, sys.argv[1])
import tantivy, tv_open
tmp = sys.argv[2]
rel = tmp + "/release/text/wiki"
def schema():
    sb = tantivy.SchemaBuilder()
    sb.add_integer_field("doc_id", stored=False, indexed=True, fast=True)
    sb.add_text_field("body", stored=False)
    return sb.build()
os.makedirs(rel)
ix = tantivy.Index(schema(), path=rel)
w = ix.writer(heap_size=15_000_000, num_threads=1)
for i, t in ((7, "hello world"), (9, "goodbye world")):
    d = tantivy.Document(); d.add_integer("doc_id", i); d.add_text("body", t); w.add_document(d)
w.commit(); w.wait_merging_threads()
del w, ix
for f in (".tantivy-meta.lock", ".tantivy-writer.lock"):     # release_v1.tvnormalize removes the empty locks
    os.remove(f"{rel}/{f}")
for f in os.listdir(rel):
    os.chmod(f"{rel}/{f}", 0o444)
os.chmod(rel, 0o555)
before = sorted(os.listdir(rel))

try:
    tantivy.Index.open(rel)
    print("plain open: opened (the bug is not reproduced here)")
except Exception as e:
    print("plain open: PermissionDenied" if "Permission denied" in str(e) else f"plain open: {e}")

ix = tv_open.open_index(tantivy, rel)
ix.reload()
s = ix.searcher()
hits = s.search(tantivy.Query.term_query(ix.schema, "body", "hello"), 5).hits
ids = s.fast_field_values("doc_id", [a for _s, a in hits])
print("hit doc_ids:", ids)
print("docs:", s.num_docs)
print("release untouched:", sorted(os.listdir(rel)) == before)
ov = tv_open.overlay_dir(rel)
print("overlay under LOGOUT_TV_OVERLAY_DIR:", ov.startswith(tmp + "/overlay/") and os.path.isfile(ov + "/.tantivy-meta.lock"))
names = [f for f in os.listdir(ov) if not f.startswith(".tantivy-")]
print("overlay links point into the release:", bool(names) and all(os.path.islink(ov + "/" + f) and os.path.realpath(ov + "/" + f) == os.path.realpath(rel + "/" + f) for f in names))
ix2 = tv_open.open_index(tantivy, rel)                        # second open: the overlay is reused
print("second open:", ix2.searcher().num_docs)

# a folder whose lock files exist and are writable is opened in place (no overlay needed)
rw = tmp + "/rw"
os.makedirs(rw)
ixw = tantivy.Index(schema(), path=rw)
w = ixw.writer(heap_size=15_000_000, num_threads=1)
d = tantivy.Document(); d.add_integer("doc_id", 1); d.add_text("body", "x"); w.add_document(d); w.commit(); w.wait_merging_threads()
del w, ixw
print("writable folder in place:", tv_open.open_index(tantivy, rw).searcher().num_docs == 1 and not os.path.exists(tv_open.overlay_dir(rw)))

# a release swapped under the same path: the overlay's links follow it
os.chmod(rel, 0o755)
for f in os.listdir(rel):
    os.chmod(f"{rel}/{f}", 0o644)
import shutil; shutil.rmtree(rel)
os.makedirs(rel)
ix3 = tantivy.Index(schema(), path=rel)
w = ix3.writer(heap_size=15_000_000, num_threads=1)
for i in (1, 2, 3):
    d = tantivy.Document(); d.add_integer("doc_id", i); d.add_text("body", "new"); w.add_document(d)
w.commit(); w.wait_merging_threads()
del w, ix3
os.remove(rel + "/.tantivy-meta.lock"); os.remove(rel + "/.tantivy-writer.lock")
for f in os.listdir(rel):
    os.chmod(f"{rel}/{f}", 0o444)
os.chmod(rel, 0o555)
print("swapped release seen:", tv_open.open_index(tantivy, rel).searcher().num_docs)
print("no dangling links after the swap:", all(os.path.exists(ov + "/" + f) for f in os.listdir(ov)))
# Windows (2026-11-14): no os.getuid, and a symlink needs rights a person does not have: hard links instead
real_symlink, real_getuid = os.symlink, os.getuid
def refuse(*a, **k): raise OSError(1314, "A required privilege is not held by the client")
os.symlink = refuse; del os.getuid
os.environ.pop("LOGOUT_TV_OVERLAY_DIR")
os.environ["TMPDIR"] = sys.argv[2] + "/wintmp"; os.makedirs(os.environ["TMPDIR"]); import tempfile; tempfile.tempdir = None
os.environ["USERNAME"] = "lv"
try:
    wov = tv_open.overlay_dir(rel)
    n = tv_open.open_index(tantivy, rel).searcher().num_docs
    print("windows: opens with hard links:", n == 3 and "logout-tv-overlay-lv" in wov
          and all(os.stat(wov + "/" + f).st_nlink > 1 for f in os.listdir(wov) if not f.startswith(".tantivy-")))
    print("windows: second open:", tv_open.open_index(tantivy, rel).searcher().num_docs)
finally:
    os.symlink, os.getuid = real_symlink, real_getuid
os.chmod(rel, 0o755)
PY
)
expect "plain open: PermissionDenied" "the bug reproduces: Index.open on a read-only release folder is refused"
expect "hit doc_ids: [7]" "open_index opens it and a search returns the doc_id of the hit"
expect "docs: 2" "the whole index is readable through the overlay"
expect "release untouched: True" "nothing was written into the release folder"
expect "overlay under LOGOUT_TV_OVERLAY_DIR: True" "the lock files live in the overlay folder LOGOUT_TV_OVERLAY_DIR names"
expect "overlay links point into the release: True" "every overlay file is a link to the release's own file"
expect "second open: 2" "a second open reuses the overlay"
expect "writable folder in place: True" "a folder with writable lock files is opened in place, no overlay made"
expect "swapped release seen: 3" "a release replaced at the same path is seen on the next open"
expect "no dangling links after the swap: True" "links to files the new release no longer has are removed"
expect "windows: opens with hard links: True" "Windows (no os.getuid, symlinks refused): the overlay hard-links the index files"
expect "windows: second open: 3" "... and a second open reuses it"

echo ""; echo "=== search_api's wiki backends open through it ==="
n=$(grep -c "_open_index(" "$API_DIR/search_api.py")
if [[ "$n" -ge 3 ]]; then ok "search_api.py defines _open_index and calls it twice (TantivyWikiText, _TvIndex)"; else bad "search_api.py opens through _open_index" "found $n mentions"; fi
if grep -nE "(tantivy|tv)\.Index\.open\(" "$API_DIR/search_api.py" | grep -v "tv_open" >/dev/null; then
    bad "search_api.py has no direct Index.open left" "$(grep -nE '(tantivy|tv)\.Index\.open\(' "$API_DIR/search_api.py")"
else ok "search_api.py has no direct Index.open left"; fi
out=$(SEARCH_WIKI_BACKEND=tantivy-release SEARCH_WIKI_INDEX="$tmp/ro-wiki" "$PY" - "$API_DIR/search_api.py" "$tmp" <<'PY' 2>&1
import importlib.util, os, sys, types
sys.modules["psycopg"] = types.ModuleType("psycopg")
sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.abspath(sys.argv[1])))   # search_api imports its neighbours
spec = importlib.util.spec_from_file_location("search_api", sys.argv[1])
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
import tantivy
p = sys.argv[2] + "/ro-wiki"
sb = tantivy.SchemaBuilder()
sb.add_integer_field("doc_id", stored=False, indexed=True, fast=True)
sb.add_text_field("body", stored=False, tokenizer_name="en_pg", index_option="freq")
os.makedirs(p)
ix = tantivy.Index(sb.build(), path=p)
ix.register_tokenizer("en_pg", m.TantivyWikiText.build_analyzer(tantivy))
w = ix.writer(heap_size=15_000_000, num_threads=1)
d = tantivy.Document(); d.add_integer("doc_id", 5); d.add_text("body", "wolf pack"); w.add_document(d)
w.commit(); w.wait_merging_threads(); del w, ix
for f in (".tantivy-meta.lock", ".tantivy-writer.lock"):
    os.remove(f"{p}/{f}")
for f in os.listdir(p):
    os.chmod(f"{p}/{f}", 0o444)
os.chmod(p, 0o555)
b = m.TantivyReleaseWikiText(p)
print("release wiki backend on a read-only folder:", b.num_docs)
os.chmod(p, 0o755)
PY
)
expect "release wiki backend on a read-only folder: 1" "TantivyReleaseWikiText opens a read-only release folder"

echo ""; echo "Total: $((PASS+FAIL))  PASS: $PASS  FAIL: $FAIL"
[[ $FAIL -eq 0 ]]
