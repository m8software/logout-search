#!/usr/bin/env bash
################################################################################
# Unit tests for search_api's site / page / anchor retrieval interface (T2 step 2,
# SEARCH-TANTIVY-PLAN phase 5, ADR-063): SEARCH_SITES_BACKEND picks Postgres (default)
# or Tantivy for SITES_SQL, PAGES_SQL, ANCHORS_SQL and EXACT_ANCHOR_SQL; the Tantivy side
# returns the same rows (head/full combination, popularity after a strong match, fetch
# errors filtered, anchors without a fetched page dropped), and an index that does not
# open is an error, never a fall back.
#
# Part 1 needs no database and no tantivy (stand-in psycopg). Part 2 builds four tiny
# indexes with tools/sites-tantivy.py; it runs when $PY can import tantivy, else SKIPs.
#
# Usage: bash tests/unit/test-search-web-text.sh      (PY=<python with tantivy>)
################################################################################
set -uo pipefail

SCRIPT_DIR_T="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR_T/../.." && pwd)"
API="${SEARCH_API_PY:-$REPO_ROOT/apps/search/serve/api/search_api.py}"
TOOL="$REPO_ROOT/apps/search/tools/sites-tantivy.py"
PY="${PY:-python3}"
export PYTHONDONTWRITEBYTECODE=1   # a stale .pyc would hide a mutation of the same size

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
sys.modules["psycopg"] = types.ModuleType("psycopg")
import os; sys.path.insert(0, os.path.dirname(os.path.abspath(sys.argv[1])))   # its neighbours (gov_state.py), as `python /app/search_api.py` finds them
spec = importlib.util.spec_from_file_location("search_api", sys.argv[1])
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
'

echo ""; echo "=== Part 1: the interface, no database ==="
out=$(env -u SEARCH_SITES_BACKEND python3 - "$API" <<PY 2>&1
$LOADER
calls = []
m._rows = lambda sql, params, setup=None: (calls.append((sql, params)), [("row",)])[1]
w = m.web_text()
print("default", w.name)
print("sites", w.sites("cats", 7, 1.5, frozenset({"docs"}), "full"))
sql, p = calls[-1]; print("sites sql", "docs_head_bm25" in sql and "d.search_text" in sql, sorted(p.items()))
w.sites("cats", 7, 2.0, frozenset(), "bow"); print("sites bow", "docs_body_bow_bm25" in calls[-1][0])
w.pages("cats", 3, frozenset({"page_docs"}), "full"); sql, p = calls[-1]
print("pages sql", "page_docs_bm25" in sql and "search_text" in sql, sorted(p.items()))
w.anchors("cats", 4); print("anchors sql", calls[-1][0] == m.ANCHORS_SQL, calls[-1][1])
w.exact_anchor("cat shop"); print("exact sql", calls[-1][0] == m.EXACT_ANCHOR_SQL, calls[-1][1])
print("popularity unknown", m.popularity(None, None), m.popularity(0, 0))
m._web_text = None; m.SITES_BACKEND = "solr"
try:
    m.web_text(); print("bogus accepted")
except ValueError:
    print("bogus refused")
m._web_text = None; m.SITES_BACKEND = "tantivy"; m.SITES_INDEX = "/nonexistent/sites-tantivy"
try:
    print("missing index served by", m.web_text().name)
except Exception:
    print("missing index refused")
PY
)
expect "default postgres" "SEARCH_SITES_BACKEND unset: the Postgres lists serve (default unchanged)"
expect "sites [('row',)]" "the Postgres backend returns SITES_SQL's rows untouched"
expect "sites sql True [('hw', 1.5), ('k', 7), ('q', 'cats'), ('strong', 10.0)]" "sites run SITES_SQL with q, k, head weight and STRONG_BM25"
expect "sites bow True" "the body variant (eval A/B) still reaches the Postgres site list"
expect "pages sql True [('k', 3), ('q', 'cats')]" "pages run PAGES_SQL with the stored column, q and k"
expect "anchors sql True {'q': 'cats', 'k': 4}" "anchors run ANCHORS_SQL with q and k"
expect "exact sql True {'q': 'cat shop'}" "the exact anchor runs EXACT_ANCHOR_SQL with the anchor key"
expect "popularity unknown 1.0 1.0" "an unknown site gets popularity 1 (SQL's coalesce defaults)"
expect "bogus refused" "an unknown SEARCH_SITES_BACKEND is an error"
expect "missing index refused" "tantivy with no index fails loudly, never falls back to Postgres"
for want in 'sub("sites", web.sites, q, k, head_weight(), stored, bv)' 'sub("pages", web.pages, q, min(5, k), stored, bv)' \
            'sub("anchors", web.anchors, q, min(5, k))' 'sub("exact_anchor", web.exact_anchor, akey)'; do
    if grep -qF "$want" "$API"; then ok "_search() calls ${want%%,*}, ...) on the backend"; else bad "_search() does not call: $want"; fi
done

echo ""; echo "=== Part 1b: the exports (ADR-059: page text only as page_words) ==="
out=$(python3 - "$TOOL" <<'PY' 2>&1
import importlib.util, sys
spec = importlib.util.spec_from_file_location("st", sys.argv[1]); st = importlib.util.module_from_spec(spec); spec.loader.exec_module(st)
pw = "string_agg(w, ' ' ORDER BY w COLLATE \"C\")"
for k in ("sites", "pages"):
    print(k, pw in st.export_sql(k) and "coalesce(d.body, '')" not in st.export_sql(k) and "coalesce(p.body, '')" not in st.export_sql(k),
          pw not in st.export_sql(k, readable_body=True))
print("anchors", "d.error IS NULL" in st.export_sql("anchors"))
print("anchor_docs", "LEFT JOIN docs d ON d.domain = a.domain AND d.error IS NULL" in st.export_sql("anchor_docs"))
PY
)
expect "sites True True" "the sites export carries page text only as page_words (raw only with --readable-body)"
expect "pages True True" "the pages export carries page text only as page_words (raw only with --readable-body)"
expect "anchors True" "the exact-anchor export keeps only domains with a fetched page (EXACT_ANCHOR_SQL's join)"
expect "anchor_docs True" "anchor documents carry their domain's fetched page, or nodoc"

echo ""; echo "=== Part 2: real Tantivy indexes ==="
if ! "$PY" -c "import tantivy" 2>/dev/null; then
    echo "  SKIP: $PY cannot import tantivy (PY=<python with tantivy==0.26.2>)"
else
    tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
    # sites: domain url title desc err head body tranco cchc  (as export-sql writes them)
    printf '%s\n' \
        $'alpha.com\thttps://alpha.com/\tAlpha Widgets\tWe sell things\t0\talpha com Alpha Widgets\talpha com Alpha Widgets We sell things a b c d\t0\t0' \
        $'beta.org\thttps://beta.org/\tBeta\tGear reviews\t0\tbeta org Beta\tbeta org Beta Gear reviews widgets widgets widgets widgets\t5\t10' \
        $'dead.net\thttps://dead.net/\tDead Widgets\t\t1\tdead net Dead Widgets\tdead net widgets widgets widgets widgets widgets\t0\t0' \
        $'alpha.com\thttps://old.alpha.com/\tOld Alpha\t\t1\talpha com Old Alpha\talpha com Old Alpha\t0\t0' > "$tmp/sites.tsv"
    printf '%s\n' \
        $'https://alpha.com/help\tHelp\tHow to fix widgets\t0\tHelp How to fix widgets fix widgets' \
        $'https://dead.net/fix\tFix\t\t1\tFix fix widgets widgets widgets' \
        $'https://beta.org/about\tAbout\tWho we are\t0\tAbout Who we are team' > "$tmp/pages.tsv"
    printf '%s\n' \
        $'alpha.com\tthe widget shop\t0\thttps://alpha.com/\tAlpha Widgets\tWe sell things\t0\t0' \
        $'beta.org\twidget reviews\t0\thttps://beta.org/\tBeta\tGear reviews\t5\t10' \
        $'ghost.io\twidget widget widget\t1\t\t\t\t0\t0' > "$tmp/anchor_docs.tsv"
    printf '%s\n' $'widget shop\talpha.com\t40' $'widget shop\tbeta.org\t12' $'widget shop\tdelta.com\t3' \
        $'reviews\tbeta.org\t9' > "$tmp/anchors.tsv"
    printf '%s\n' $'delta.com\thttps://delta.com/\tDelta\t\t0\tdelta com Delta\tdelta com Delta\t0\t0' >> "$tmp/sites.tsv"
    built=1
    for k in sites pages anchor_docs anchors; do
        if ! bout=$("$PY" "$TOOL" build "$k" "$tmp/$k.tsv" "$tmp/idx/$k" --heap-mb 50 --threads 1 2>&1); then
            bad "tools/sites-tantivy.py build $k failed" "$bout"; built=0
        fi
    done
    if [[ $built -eq 1 ]]; then
        out=$(SEARCH_SITES_BACKEND=tantivy SEARCH_SITES_INDEX="$tmp/idx" "$PY" - "$API" <<PY 2>&1
$LOADER
from concurrent.futures import ThreadPoolExecutor
w = m.web_text()
print("backend", w.name, sorted(w.num_docs.items()))
m.STRONG_BM25 = 1e9                                   # no popularity
r = w.sites("widgets", 10, 2.0)
print("shape", all(isinstance(x[0], str) and isinstance(x[3], float) and isinstance(x[4], float) for x in r) and len(r[0]) == 5)
print("hw2", [x[0] for x in r])
print("hw0", [x[0] for x in w.sites("widgets", 10, 0.0)])
print("k1", len(w.sites("widgets", 1, 2.0)))
print("plain", all(x[3] == x[4] for x in r))
m.STRONG_BM25 = 0.0                                   # every match strong: popularity applies
r = w.sites("widgets", 10, 2.0)
print("strong", [x[0] for x in r])
beta = [x for x in r if x[0] == "https://beta.org/"][0]
print("boost", abs(beta[4] / beta[3] - m.popularity(5, 10)) < 1e-6)
print("title fallback", [x[1] for x in r])
m.STRONG_BM25 = 10.0
print("pages", [(u, t, d) for u, t, d, _s in w.pages("fix widgets", 5)])
print("anchors", [x[0] for x in w.anchors("widget", 5)])
print("anchors k", len(w.anchors("widget", 1)))
print("exact", [(d, s, u) for d, s, u, _t, _ds in w.exact_anchor("widget shop")])
print("exact none", w.exact_anchor("widget"))
print("stop", w.sites("the of and", 5, 2.0), w.pages("the of", 5), w.anchors("the", 5))
want = [w.sites(q, 5, 2.0) for q in ("widgets", "gear", "alpha", "beta")] * 25
with ThreadPoolExecutor(12) as ex:
    got = list(ex.map(lambda q: w.sites(q, 5, 2.0), ["widgets", "gear", "alpha", "beta"] * 25))
print("threads", got == want)
print("analyzers", w.analyzers)
PY
)
        expect "backend tantivy [('anchor_docs', 3), ('anchors', 4), ('pages', 3), ('sites', 5)]" "SEARCH_SITES_BACKEND=tantivy opens the four indexes"
        expect "shape True" "site rows are (url, title, description, float bm25, float score)"
        expect "hw2 ['https://alpha.com/', 'https://beta.org/']" "head weight 2: the title match leads; the fetch-error rows (dead.net) never appear"
        expect "hw0 ['https://beta.org/', 'https://alpha.com/']" "head weight 0: only the full text counts (head and full are separate BM25 lists)"
        expect "k1 1" "k limits the site rows"
        expect "plain True" "below STRONG_BM25 the score is the bm25 (no popularity)"
        expect "strong ['https://beta.org/', 'https://alpha.com/']" "after a strong match a popular site (tranco 5) moves up"
        expect "boost True" "the strong-match score is bm25 x SITES_SQL's popularity factor"
        expect "title fallback ['Beta', 'Alpha Widgets']" "titles come from the fetched row of the domain"
        expect "pages [('https://alpha.com/help', 'Help', 'How to fix widgets')]" "pages: matches with their stored title and description; fetch errors filtered"
        expect "anchors ['https://beta.org/', 'https://alpha.com/']" "anchors: a domain with no fetched page is dropped, popularity weighs the rest"
        expect "anchors k 1" "k limits the anchor rows"
        expect "exact [('alpha.com', 40, 'https://alpha.com/'), ('beta.org', 12, 'https://beta.org/')]" "exact anchor: the two most-used sites, homepage from the fetched row (not old.alpha.com)"
        expect "exact none []" "exact anchor matches the whole phrase only"
        expect "stop [] [] []" "a query of only stopwords returns no rows, not an error"
        expect "threads True" "12 threads share the searchers and get the same rows"
        expect "analyzers {'sites': 'en_pg', 'pages': 'en_pg', 'anchor_docs': 'en_pg'}" "an index built without --analyzer is read with en_pg"
    fi
    # en_pg2: dotted tokens whole, as Postgres's parser keeps hosts/files/versions
    mkdir -p "$tmp/idx2"
    printf '%s\n' $'nlsite.example\thttps://golf.nl/\tNL\t\t0\tnlsite example NL\tgolf.nl golf.nl hier\t0\t0' \
                   $'golfclub.example\thttps://club.example/\tClub\t\t0\tclub\tgolf club\t0\t0' > "$tmp/sites2.tsv"
    ok2=1
    for k in pages anchor_docs anchors; do cp -r "$tmp/idx/$k" "$tmp/idx2/$k"; done
    "$PY" "$TOOL" build sites "$tmp/sites2.tsv" "$tmp/idx2/sites" --heap-mb 50 --threads 1 --analyzer en_pg2 >/dev/null 2>&1 || { bad "build --analyzer en_pg2 failed"; ok2=0; }
    if [[ $ok2 -eq 1 ]]; then
        out=$(SEARCH_SITES_BACKEND=tantivy SEARCH_SITES_INDEX="$tmp/idx2" "$PY" - "$API" <<PY 2>&1
$LOADER
w = m.web_text()
print("an2", w.analyzers["sites"], w.terms("Golf.nl fable5.1 golf", "sites"))
print("golf", [r[0] for r in w.sites("golf", 5, 2.0)])
print("golf.nl", [r[0] for r in w.sites("golf.nl", 5, 2.0)])
open("$tmp/idx2/pages/logout-analyzer.txt", "w").write("klingon\n"); m._web_text = None
try:
    m.web_text(); print("bad analyzer accepted")
except ValueError:
    print("bad analyzer refused")
PY
)
        expect "an2 en_pg2 ['golf.nl', 'fable5.1', 'golf']" "en_pg2 keeps dotted tokens whole, in the index and the query"
        expect "golf ['https://club.example/']" "en_pg2: 'golf' does not match a page that only says golf.nl (as in Postgres)"
        expect "golf.nl ['https://golf.nl/']" "en_pg2: the whole host matches"
        expect "bad analyzer refused" "an index naming an unknown analyzer is an error"
    fi
fi

echo ""
echo "Results: $PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]]
