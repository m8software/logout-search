#!/usr/bin/env bash
################################################################################
# Unit tests for SEARCH_WEB_BACKEND=tantivy-release: the site, page and anchor lists
# from the release's text/sites/{sites,pages,anchors} (doc_id only), rows made through the REAL
# ReleaseLookups of a synthetic release (tests/unit/lib/search_update_fixture.py: real registry,
# lookup.sqlite, signals.bin) with a few type-3 page rows added.
#
# The three indexes are built by the real `tantivy-structure build --kind` (docker image
# logout-tantivy-build:4) when that image exists, else by tantivy-py in the same schema (u64 fast +
# indexed doc_id, nothing stored, en_pg2, freq-only text); the first PASS line says which. The index
# folders are chmod a-w, so the read-only overlay path is the one under test.
# Needs python with tantivy + numpy (PY=...), else SKIP.   Usage: bash tests/unit/test-search-web-release.sh
################################################################################
set -uo pipefail
SCRIPT_DIR_T="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR_T/../.." && pwd)"
API="${SEARCH_API_PY:-$REPO_ROOT/apps/search/serve/api/search_api.py}"
TD="$REPO_ROOT/apps/search/tools/tantivy-structure"
LIB="$SCRIPT_DIR_T/lib"
PY="${PY:-python3}"
IMG="${IMG:-logout-tantivy-build:4}"
export PYTHONDONTWRITEBYTECODE=1
PASS=0; FAIL=0
RED=$'\033[0;31m'; GREEN=$'\033[0;32m'; NC=$'\033[0m'
ok()  { echo "  ${GREEN}PASS${NC} $1"; PASS=$((PASS+1)); }
bad() { echo "  ${RED}FAIL${NC} $1"; shift; [[ $# -gt 0 ]] && printf '       %s\n' "$@"; FAIL=$((FAIL+1)); }
expect() { if grep -qxF -- "$1" <<<"$out"; then ok "$2"; else bad "$2" "want: $1" "got:  $(head -c 1500 <<<"$out")"; fi; }
if ! "$PY" -c "import tantivy, numpy" 2>/dev/null; then
    echo "  SKIP: $PY cannot import tantivy + numpy"; echo ""; echo "Total: 0  PASS: 0  FAIL: 0 (skipped)"; exit 0
fi
tmp=$(mktemp -d); trap 'chmod -R u+w "$tmp" 2>/dev/null; rm -rf "$tmp"' EXIT
fail_now() { bad "$1" "$2"; echo ""; echo "Total: $((PASS+FAIL))  PASS: $PASS  FAIL: $FAIL"; exit 1; }

echo ""; echo "=== fixture: synthetic release + type-3 page rows + the three web indexes ==="
bout=$("$PY" "$LIB/search_update_fixture.py" build "$tmp/fx" --wiki 300 --months 1 --seed 3 --frac 0.04 2>&1) || fail_now "fixture build failed" "$(tail -8 <<<"$bout")"
REL_DIR="$tmp/rel"; cp -a "$tmp/fx/r1" "$REL_DIR"; chmod -R u+w "$REL_DIR"
"$PY" - "$REL_DIR" "$tmp" <<'PY' || fail_now "fixture prep failed" ""
import sqlite3, sys
rel, t = sys.argv[1:3]
S = 28
db = sqlite3.connect(rel + "/registry.sqlite")
db.execute("INSERT INTO hosts VALUES (1, 'https://pages.example')")
db.execute("INSERT INTO registry VALUES (?, NULL, 1, '/a/one', 'Page one', NULL)", ((3 << S) | 0,))
db.execute("INSERT INTO registry VALUES (?, NULL, 1, '/b/two', 'Page two', NULL)", ((3 << S) | 1,))
db.execute("INSERT INTO registry VALUES (?, NULL, NULL, 'x', NULL, 'mailto:odd@odd.example')", ((3 << S) | 2,))
# two government pages with the same words, one New Jersey's and one California's, their state words in the body
db.execute("INSERT INTO hosts VALUES (2, 'https://www.nj.gov')")
db.execute("INSERT INTO hosts VALUES (3, 'https://dmv.ca.gov')")
db.execute("INSERT INTO registry VALUES (?, NULL, 2, '/njsp/firearms', 'Firearms', NULL)", ((3 << S) | 3,))
db.execute("INSERT INTO registry VALUES (?, NULL, 3, '/firearms', 'Firearms', NULL)", ((3 << S) | 4,))
db.commit()
H = ["site0-235287.example", "site10-387425.example", "site100-424322.example", "site102-206870.example"]
# Site 0 (tranco 30) and site 10 (tranco 51845) get the same text: only popularity separates them
open(t + "/sites.rows.tsv", "w").write("\n".join([f"{H[0]}\tzebra\tzebra alpha", f"{H[1]}\tzebra\tzebra alpha",
                                                  f"{H[2]}\tquokka\tzebra quokka quokka quokka", f"{H[3]}\tnarwhal\tnarwhal"]) + "\n")
open(t + "/pages.rows.tsv", "w").write("\n".join(["https://pages.example/a/one\tgnu antelope", "https://pages.example/b/two\tgnu gnu gnu",
                                                  "mailto:odd@odd.example\tokapi",
                                                  "https://www.nj.gov/njsp/firearms\tcard firearms id govstatenj",     # as release_v1 web-input writes them
                                                  "https://dmv.ca.gov/firearms\tfirearms permit govstateca"]) + "\n")
open(t + "/anchors.rows.tsv", "w").write(f"{H[0]}\tshop\n{H[1]}\tshop shop shop\n")
open(t + "/english.stop", "w").write("the\nof\nand\na\n")
PY
mkdir -p "$REL_DIR/text/sites"
IMGOK=0; command -v docker >/dev/null && docker image inspect "$IMG" >/dev/null 2>&1 && command -v zstd >/dev/null && IMGOK=1
if [[ $IMGOK == 1 ]]; then
    python3 "$TD/pop_order.py" hostranks "$REL_DIR/registry.sqlite" "$REL_DIR/signals.bin" "$tmp/hostranks.tsv" >/dev/null 2>&1 || fail_now "hostranks failed" ""
    for k in sites pages anchors; do
        w="$tmp/w-$k"; mkdir -p "$w/in"
        cp "$tmp/english.stop" "$REL_DIR/registry.sqlite" "$tmp/hostranks.tsv" "$w/in/"
        zstd -q "$tmp/$k.rows.tsv" -o "$w/in/$k.rows.tsv.zst"
        KIND=$k ANALYZER=en_pg2 JOBS=1 IMG="$IMG" bash "$TD/local-build.sh" "$w" >"$tmp/build-$k.log" 2>&1 || fail_now "real build of $k failed" "$(tail -5 "$tmp/build-$k.log")"
        cp -a "$w/out/$k" "$REL_DIR/text/sites/$k"
    done
    BUILDER="the real tantivy-structure builder (docker $IMG)"
else
    "$PY" - "$REL_DIR" "$tmp" <<'PY' || fail_now "tantivy-py fixture build failed" ""
import os, re, sqlite3, sys, tantivy
rel, t = sys.argv[1:3]
STOP = open(t + "/english.stop").read().split()
tok = tantivy.Tokenizer.regex(r"[^\W_]+(?:[.@][^\W_]+)+|[^\W_]+")
an = tantivy.TextAnalyzerBuilder(tok).filter(tantivy.Filter.lowercase()).filter(tantivy.Filter.custom_stopword(STOP)).filter(tantivy.Filter.stemmer("english")).build()
reg = sqlite3.connect(f"file:{rel}/registry.sqlite?mode=ro", uri=True)
def doc_id(kind, ident):
    if kind == "pages":
        for d, origin, path, alt in reg.execute("SELECT r.doc_id, h.origin, r.path, r.alt FROM registry r LEFT JOIN hosts h ON h.id = r.host_id WHERE r.doc_id >> 28 = 3"):
            if (alt or (origin or "") + (path or "")) == ident:
                return d
    else:
        return reg.execute("SELECT doc_id FROM registry WHERE doc_id >> 28 = 2 AND path = ?", (ident,)).fetchone()[0]
    raise KeyError(ident)
for kind, fields in (("sites", ("head", "body")), ("pages", ("body",)), ("anchors", ("body",))):
    sb = tantivy.SchemaBuilder()
    sb.add_unsigned_field("doc_id", stored=False, indexed=True, fast=True)
    for f in fields:
        sb.add_text_field(f, stored=False, tokenizer_name="en_pg2", index_option="freq")
    p = f"{rel}/text/sites/{kind}"; os.makedirs(p)
    ix = tantivy.Index(sb.build(), path=p); ix.register_tokenizer("en_pg2", an)
    w = ix.writer(heap_size=50_000_000, num_threads=1)
    for line in open(f"{t}/{kind}.rows.tsv"):
        ident, *cols = line.rstrip("\n").split("\t")
        d = tantivy.Document(); d.add_unsigned("doc_id", doc_id(kind, ident))
        for f, c in zip(fields, cols):
            d.add_text(f, c)
        w.add_document(d)
    w.commit(); w.wait_merging_threads()
    open(p + "/logout-analyzer.txt", "w").write("en_pg2\n")
PY
    BUILDER="tantivy-py in the builder's schema (no docker image $IMG here)"
fi
# the folders stay read-only for the whole test: the overlay is what opens them
find "$REL_DIR/text/sites" -name '.tantivy*lock' -delete
chmod -R a-w "$REL_DIR/text/sites"
[[ ! -w "$REL_DIR/text/sites/sites" ]] || fail_now "setup: index folder is writable" ""
ok "fixture built: 4 sites, 5 pages (two of them government pages with a state word), 2 anchor documents by $BUILDER; index folders read-only"

echo ""; echo "=== tantivy-release: doc_id hits -> site / page / anchor rows through the release lookups ==="
echo '{"source": "CISA .gov registry (public domain)", "domains": {"nj.gov": "NJ", "ca.gov": "CA"}}' > "$tmp/gov-states.json"
out=$(API_DIR="$(dirname "$API")" LOGOUT_TV_OVERLAY_DIR="$tmp/ov" SEARCH_WEB_BACKEND=tantivy-release SEARCH_LOOKUP_BACKEND=release SEARCH_RELEASE_DIR="$REL_DIR" \
      SEARCH_GOV_STATES="$tmp/gov-states.json" "$PY" - "$API" "$REL_DIR" <<'PY' 2>&1
import importlib.util, os, re, sys, types
sys.modules["psycopg"] = types.ModuleType("psycopg")
sys.path.insert(0, os.environ["API_DIR"])
spec = importlib.util.spec_from_file_location("search_api", sys.argv[1])
m = importlib.util.module_from_spec(spec); sys.modules["search_api"] = m; spec.loader.exec_module(m)
rel = sys.argv[2]
S = 28
H0, H1 = "https://site0-235287.example/", "https://site10-387425.example/"
b = m.web_text()                          # the real path: env -> lookups() -> <release>/text/sites
print("backend", b.name, b.num_docs, sorted(b.analyzers.values()))
lk = m.lookups()
print("lookups", lk.name)
m.STRONG_BM25 = 0.0                       # popularity applies to every hit
rows = b.sites("zebra", 10, 2.0)
print("shape", all(len(r) == 5 and isinstance(r[0], str) and isinstance(r[1], str) and isinstance(r[2], str) and isinstance(r[3], float) and isinstance(r[4], float) for r in rows))
print("sites zebra", [r[0] for r in rows])
print("row fields", rows[0][:3] == (H0, "Site 0 w218b", "w179b w179b w260a w201b w120b w116a w171b w201a"))
print("popularity lifts the popular site", rows[0][4] > rows[0][3] and rows[0][3] == rows[1][3] and rows[0][4] > rows[1][4])
m.STRONG_BM25 = 1e9                       # below the threshold: score is the bm25
rows = b.sites("zebra", 10, 2.0)
print("below threshold score == bm25", all(r[3] == r[4] for r in rows))
m.STRONG_BM25 = 0.0
lo = [r for r in b.sites("quokka", 10, 0.0) if "site100" in r[0]][0][3]
hi = [r for r in b.sites("quokka", 10, 5.0) if "site100" in r[0]][0][3]
print("head weight counts", hi > lo > 0)
print("k limits", len(b.sites("zebra", 1, 2.0)))
print("site with no hit absent", not [r for r in b.sites("zebra", 10, 2.0) if "site102" in r[0]])
orig = lk.reg
print("narwhal before", [r[1] for r in b.sites("narwhal", 5, 2.0)])
lk.reg = lambda d: (None, None, None, None) if d == (2 << S) + 3 else orig(d)      # the registry no longer knows it
print("unknown doc_id skipped", b.sites("narwhal", 5, 2.0) == [])
lk.reg = orig
rows = b.pages("gnu", 5)
print("pages", [(r[0], r[1], r[2]) for r in rows])
print("page shape", all(len(r) == 4 and isinstance(r[3], float) for r in rows))
print("page alt url", [(r[0], r[1]) for r in b.pages("okapi", 5)])
print("pages k", len(b.pages("gnu", 1)))
# a state's government pages through the state word the release put in their bag of words
print("gov states loaded", len(m.gov_states()))
print("gov word", b.gov_state_word("NJ"), b.gov_state_word("CA"), b.gov_state_word("TX"))
print("gov pages NJ", [(r[0], r[1], r[2]) for r in b.gov_pages("NJ", "firearms", False, 5)])
print("gov pages CA", [r[0] for r in b.gov_pages("CA", "firearms", False, 5)])
print("gov pages TX", b.gov_pages("TX", "firearms", False, 5))
print("gov pages every word first", [r[0] for r in b.gov_pages("NJ", "firearms card", False, 5)], [r[0] for r in b.gov_pages("NJ", "firearms card", True, 5)])
print("gov pages weak needs every word", b.gov_pages("NJ", "firearms permit", True, 5), [r[0] for r in b.gov_pages("NJ", "firearms permit", False, 5)])
print("plain pages see both", sorted(r[0] for r in b.pages("firearms", 5)))
b._gov_word = {"NJ": None}                      # an older release: no state word in the index, the state is read from each hit's address
print("gov pages NJ by address", [r[0] for r in b.gov_pages("NJ", "firearms", False, 5)])
b._gov_word = {}
rows = b.anchors("shop", 5)
print("anchors", [r[0] for r in rows], len(rows[0]))
print("anchor row", rows[0][1:3] == ("Site 0 w218b", "w179b w179b w260a w201b w120b w116a w171b w201a"))
print("anchor popularity beats a higher bm25", rows[0][0] == H0)
print("stopwords", b.sites("the of and", 5, 2.0), b.pages("the of and", 5), b.anchors("the of and", 5))
try:
    b.exact_anchor("zebra"); print("exact_anchor served")
except RuntimeError as e:
    print("exact_anchor refused", "no exact-anchor lookup" in str(e))
# every way search_api calls the web backend works on this class or is refused (call sites)
src = open(sys.argv[1]).read()
used = sorted(set(re.findall(r"\bweb\.(\w+)(?=[,)(])", src[src.index("def _search("):])) | set(re.findall(r"\bweb_text\(\)\.(\w+)", src)))
print("call sites", used, all(callable(getattr(b, n, None)) for n in used))
m.lookups = lambda: None
try:
    b.sites("zebra", 5, 2.0); print("no lookups served")
except RuntimeError:
    print("no lookups refused")
# selection
m._web_text = None; m.lookups = lambda: lk
for backend, site_backend in (("tantivy-release", "postgres"), ("", "tantivy-release")):
    m._web_text = None; m.SITES_BACKEND = (backend or site_backend)
    print("selected", repr(backend), repr(site_backend), m.web_text().name)
m._web_text = None; m.SITES_BACKEND = "postgres"
print("default", m.web_text().name)
m._web_text = None; m.SITES_BACKEND = "nonsense"
try:
    m.web_text(); print("nonsense served")
except ValueError as e:
    print("nonsense refused", "tantivy-release" in str(e))
m._web_text = None; m.SITES_BACKEND = "tantivy-release"; os.environ["SEARCH_SITES_INDEX"] = "/nonexistent/sites"
try:
    m.web_text(); print("missing index served")
except Exception:
    print("missing index refused")
PY
)
expect "backend tantivy-release {'sites': 4, 'pages': 5, 'anchor_docs': 2} ['en_pg2', 'en_pg2', 'en_pg2']" "SEARCH_WEB_BACKEND=tantivy-release opens the three read-only indexes from <release>/text/sites and counts their documents"
expect "lookups release" "the test runs on the real ReleaseLookups"
expect "shape True" "site rows are (url, title, description, bm25, score), the shape SITES_SQL returns"
expect "sites zebra ['https://site0-235287.example/', 'https://site10-387425.example/', 'https://site100-424322.example/']" "equal text: the popular site (tranco 30) ranks above the unpopular one (tranco 51845), then the body-only match"
expect "row fields True" "url and title come from the registry, the description from lookup.sqlite"
expect "popularity lifts the popular site True" "above the strong-match threshold the score is bm25 times the popularity factor (same bm25, higher score)"
expect "below threshold score == bm25 True" "below the threshold score equals bm25"
expect "head weight counts True" "the head index is searched and weighted by the head weight"
expect "k limits 1" "k limits the site rows"
expect "site with no hit absent True" "a site without the query word is not returned"
expect "narwhal before ['Site 102 w275b']" "control: the narwhal site is found while the registry knows it"
expect "unknown doc_id skipped True" "a doc_id the registry no longer knows is skipped, not shown"
expect "pages [('https://pages.example/b/two', 'Page two', ''), ('https://pages.example/a/one', 'Page one', '')]" "pages: url = host origin + path from the registry, title from the registry, description empty (page text is not shipped); best bm25 first"
expect "page shape True" "page rows are (url, title, description, bm25)"
expect "page alt url [('mailto:odd@odd.example', 'mailto:odd@odd.example')]" "a page with no host uses its alt url, and as its title when the registry has none"
expect "pages k 1" "k limits the page rows"
expect "gov states loaded 2" "the engine reads whose state a government site is from SEARCH_GOV_STATES (the release's gov-states.json)"
expect "gov word govstatenj govstateca None" "the state word is found in the pages index for the states that have pages, and is None for one that has none"
expect "gov pages NJ [('https://www.nj.gov/njsp/firearms', 'Firearms', '')]" ": gov_pages for New Jersey asks the index with the state word as a Must term and gets only New Jersey's page (the same words on California's page do not come)"
expect "gov pages CA ['https://dmv.ca.gov/firearms']" "and California's page for California"
expect "gov pages TX []" "a state with no government pages gets none"
expect "gov pages every word first ['https://www.nj.gov/njsp/firearms'] ['https://www.nj.gov/njsp/firearms']" "a page holding every query word is found with a strong and with a weak state"
expect "gov pages weak needs every word [] ['https://www.nj.gov/njsp/firearms']" "a weak state (a code that is also a word) acts only on pages holding every other word; a strong state also takes a page holding some"
expect "plain pages see both ['https://dmv.ca.gov/firearms', 'https://www.nj.gov/njsp/firearms']" "the state word changes nothing for an ordinary page search"
expect "gov pages NJ by address ['https://www.nj.gov/njsp/firearms']" "a release whose pages carry no state word (older) still answers: the state is read from each hit's address"
expect "anchors ['https://site0-235287.example/', 'https://site10-387425.example/'] 4" "anchors: rows are (url, title, description, score) of the site the anchor document names"
expect "anchor row True" "an anchor row carries its site's title and description"
expect "anchor popularity beats a higher bm25 True" "the popular site's single 'shop' outranks the unpopular site's three"
expect "stopwords [] [] []" "a query of only stopwords matches nothing, in all three lists"
expect "exact_anchor refused True" "exact_anchor raises: the release has no phrase -> sources index (never an empty answer)"
if grep -q "^call sites \['anchors', 'exact_anchor', 'gov_pages', 'local_pages', 'pages', 'sites'\] True" <<<"$out"; then ok "every web.<method> search_api calls exists on the class (sites, pages, gov_pages, local_pages, anchors, exact_anchor)"; else bad "call sites" "$(grep '^call sites' <<<"$out")"; fi
expect "no lookups refused" "without SEARCH_LOOKUP_BACKEND=release the backend fails loudly"
expect "selected 'tantivy-release' 'postgres' tantivy-release" "SEARCH_WEB_BACKEND selects the backend"
expect "selected '' 'tantivy-release' tantivy-release" "SEARCH_SITES_BACKEND=tantivy-release selects it too"
expect "default postgres" "the default is unchanged"
expect "nonsense refused True" "an unknown backend name is an error naming the choices"
expect "missing index refused" "a missing index is an error, never a quiet fall back"
echo ""; echo "=== exact_anchor from anchor-phrases.sqlite (own licence file, release stamp) ==="
AP_DIR="$tmp/relap"; cp -a "$REL_DIR" "$AP_DIR"; chmod u+w "$AP_DIR"
"$PY" - "$REPO_ROOT/apps/search/release" "$AP_DIR" <<'PY' || bad "the anchor-phrases fixture could not be written"
import sqlite3, sys, types
sys.modules.setdefault("psycopg", types.ModuleType("psycopg"))
sys.path.insert(0, sys.argv[1])
import export as ex
rel = sys.argv[2]
reg = sqlite3.connect(rel + "/registry.sqlite")
doc = lambda host: reg.execute("SELECT doc_id FROM registry WHERE doc_id >> 28 = 2 AND path = ?", (host,)).fetchone()[0]
d0, d1 = doc("site0-235287.example"), doc("site10-387425.example")
db = sqlite3.connect(rel + "/anchor-phrases.sqlite")
db.execute("CREATE TABLE anchor_phrase (anchor TEXT NOT NULL, sources INTEGER NOT NULL, doc_id INTEGER NOT NULL, "
           "PRIMARY KEY (anchor, sources DESC, doc_id)) WITHOUT ROWID")
db.executemany("INSERT INTO anchor_phrase VALUES (?, ?, ?)", [
    ("paper of record", 40, d0), ("paper of record", 5, d1), ("paper of record", 2, d1 + 1),
    ("close call", 12, d0), ("close call", 11, d1), ("gone site", 50, (2 << 28) + 999999)])
ex.stamp(db, "anchor-phrases", "fx-1", {"licence": "Common Crawl link text (fixture)"}); db.commit(); db.close()
PY
ap_checks() {
    API_DIR="$(dirname "$API")" LOGOUT_TV_OVERLAY_DIR="$tmp/ov2" SEARCH_WEB_BACKEND=tantivy-release SEARCH_LOOKUP_BACKEND=release \
    SEARCH_RELEASE_DIR="$1" "$PY" - "$API" <<'PY' 2>&1
import importlib.util, os, sys, types
sys.modules["psycopg"] = types.ModuleType("psycopg")
sys.path.insert(0, os.environ["API_DIR"])
spec = importlib.util.spec_from_file_location("search_api", sys.argv[1])
m = importlib.util.module_from_spec(spec); sys.modules["search_api"] = m
try:
    spec.loader.exec_module(m)
    b = m.web_text()
    rows = b.exact_anchor("paper of record")
    print("ap rows", [(r[0], r[1], r[2], r[3]) for r in rows])
    print("ap desc", rows[0][4] == "w179b w179b w260a w201b w120b w116a w171b w201a")
    named = m.exact_anchor(rows)
    print("ap named", named and named.get("url"), named and named.get("anchor_named"))
    print("ap close call", m.exact_anchor(b.exact_anchor("close call")))
    print("ap gone", b.exact_anchor("gone site"))
    print("ap none", b.exact_anchor("nothing links like this"))
except RuntimeError as e:
    print("ap refused", "anchor-phrases" in str(e))
PY
}
out=$(ap_checks "$AP_DIR")
expect "ap rows [('site0-235287.example', 40, 'https://site0-235287.example/', 'Site 0 w218b'), ('site10-387425.example', 5, 'https://site10-387425.example/', 'Site 10 w066a')]" "the phrase's two most-linked sites as (domain, sources, url, title), EXACT_ANCHOR_SQL's shape"
expect "ap desc True" "... with the site's description from lookup.sqlite"
expect "ap named https://site0-235287.example/ True" "40 sites vs 5: the phrase names site 0 (search_api.exact_anchor)"
expect "ap close call None" "12 vs 11 sites: no clear lead, no site named"
expect "ap gone []" "a site the registry no longer knows is skipped"
expect "ap none []" "a phrase nobody uses gives nothing"
"$PY" -c "import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); c.execute(\"UPDATE meta SET value = 'lookup' WHERE key = 'role'\"); c.commit()" "$AP_DIR/anchor-phrases.sqlite"
out=$(ap_checks "$AP_DIR")
expect "ap refused True" "an anchor-phrases file stamped with another role is refused at start"

echo ""; echo "=== Stack Exchange credits: registry.authors -> a page row's fifth element (2026-10-22) ==="
SE_DIR="$tmp/relse"; cp -a "$REL_DIR" "$SE_DIR"; chmod u+w "$SE_DIR" "$SE_DIR/registry.sqlite"
OLD_DIR="$tmp/relold"; cp -a "$REL_DIR" "$OLD_DIR"; chmod u+w "$OLD_DIR" "$OLD_DIR/registry.sqlite"
"$PY" - "$SE_DIR" "$OLD_DIR" <<'PY' || bad "the authors fixture could not be written"
import sqlite3, sys
S = 28
se, old = sys.argv[1:3]
db = sqlite3.connect(se + "/registry.sqlite")
db.execute("CREATE TABLE IF NOT EXISTS authors (doc_id INTEGER NOT NULL, role INTEGER NOT NULL, user_id INTEGER, name TEXT, "
           "PRIMARY KEY (doc_id, role)) WITHOUT ROWID")
db.execute("UPDATE registry SET path = '/questions/784' WHERE doc_id = ?", ((3 << S) | 1,))     # page two is now a question
db.executemany("INSERT INTO authors VALUES (?,?,?,?)", [((3 << S) | 1, 1, 5, "Ann"), ((3 << S) | 1, 2, None, None),
                                                       ((3 << S) | 0, 1, 7, "Bo")])         # page one: not a question address
db.commit(); db.execute("VACUUM"); db.close()
db = sqlite3.connect(old + "/registry.sqlite")                                              # a release built before the table
db.execute("DROP TABLE IF EXISTS authors"); db.commit(); db.execute("VACUUM"); db.close()
PY
se_checks() {
    API_DIR="$(dirname "$API")" LOGOUT_TV_OVERLAY_DIR="$tmp/ov-$2" SEARCH_WEB_BACKEND=tantivy-release SEARCH_LOOKUP_BACKEND=release \
    SEARCH_RELEASE_DIR="$1" "$PY" - "$API" <<'PY' 2>&1
import importlib.util, os, sys, types
sys.modules["psycopg"] = types.ModuleType("psycopg")
sys.path.insert(0, os.environ["API_DIR"])
spec = importlib.util.spec_from_file_location("search_api", sys.argv[1])
m = importlib.util.module_from_spec(spec); sys.modules["search_api"] = m; spec.loader.exec_module(m)
b, lk = m.web_text(), m.lookups()
rows = b.pages("gnu", 5)
print("se has", lk.has_authors)
print("se rows", [(r[0], len(r)) for r in rows])
print("se fields", [m.se_credit(r[0], r[4]) if len(r) > 4 else {} for r in rows])
PY
}
out=$(se_checks "$SE_DIR" se)
expect "se has True" "a release with registry.authors is read for credits"
expect "se rows [('https://pages.example/questions/784', 5), ('https://pages.example/a/one', 5)]" "a credited page row carries its credits as a fifth element"
expect "se fields [{'author': 'Ann', 'author_url': 'https://pages.example/users/5', 'answer_author': None, 'answer_author_url': None}, {'author': 'Bo', 'author_url': None}]" "the asker links to https://<the page's host>/users/<id>; a deleted answerer has no name and no link; a page that is not a question address gets no link"
out=$(se_checks "$OLD_DIR" old)
expect "se has False" "a release without registry.authors (rc9 and older) opens as before"
expect "se rows [('https://pages.example/b/two', 4), ('https://pages.example/a/one', 4)]" "... and its page rows stay (url, title, description, bm25), no credits"

echo ""; echo "=== which env var picks the backend (module read in a fresh process each time) ==="
backend_of() { env -u SEARCH_WEB_BACKEND -u SEARCH_SITES_BACKEND "$@" "$PY" - "$API" <<'PY2' 2>&1
import importlib.util, os, sys, types
sys.modules["psycopg"] = types.ModuleType("psycopg")
sys.path.insert(0, os.path.dirname(sys.argv[1]))          # search_api imports its neighbours (gov_state.py) as the image's /app does
spec = importlib.util.spec_from_file_location("search_api", sys.argv[1])
m = importlib.util.module_from_spec(spec); sys.modules["search_api"] = m; spec.loader.exec_module(m)
print(m.SITES_BACKEND)
PY2
}
[[ "$(backend_of)" == "postgres" ]] && ok "no env: postgres (the default is unchanged)" || bad "no env: postgres" "$(backend_of)"
[[ "$(backend_of SEARCH_WEB_BACKEND=tantivy-release)" == "tantivy-release" ]] && ok "SEARCH_WEB_BACKEND=tantivy-release selects it" || bad "SEARCH_WEB_BACKEND selects"
[[ "$(backend_of SEARCH_SITES_BACKEND=tantivy)" == "tantivy" ]] && ok "SEARCH_SITES_BACKEND=tantivy still selects the old Tantivy backend" || bad "SEARCH_SITES_BACKEND selects"
[[ "$(backend_of SEARCH_WEB_BACKEND=tantivy-release SEARCH_SITES_BACKEND=postgres)" == "tantivy-release" ]] && ok "SEARCH_WEB_BACKEND wins over SEARCH_SITES_BACKEND" || bad "WEB wins over SITES"
[[ "$(backend_of SEARCH_WEB_BACKEND=" Tantivy-Release ")" == "tantivy-release" ]] && ok "the name is trimmed and lowercased" || bad "name normalised"
echo ""; echo "Total: $((PASS+FAIL))  PASS: $PASS  FAIL: $FAIL"; [[ $FAIL -eq 0 ]]
