#!/usr/bin/env bash
################################################################################
# Unit tests for the release-format lookup path (T4, SEARCH-RELEASE-FORMAT.md):
# tools/sqlite_lookups/build.py builds a release (lookup.sqlite with names/cards keyed by
# doc_id, places-osm.sqlite, registry/signals stand-ins, text/typo) from a tiny staging file,
# and search_api with SEARCH_LOOKUP_BACKEND=release answers the lookups from it.
#
# Part 1 (no tantivy needed): the builder's release guards refuse what must never ship.
# Part 2 (needs tantivy, else SKIP): build a fixture release and ask it.
#
# Usage: bash tests/unit/test-search-release-lookups.sh      (PY=<python with tantivy + numpy>)
################################################################################
set -uo pipefail

SCRIPT_DIR_T="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR_T/../.." && pwd)"
API_DIR="$REPO_ROOT/apps/search/serve/api"
TOOL_DIR="$REPO_ROOT/apps/search/tools/sqlite_lookups"
PY="${PY:-python3}"
export PYTHONDONTWRITEBYTECODE=1

PASS=0
FAIL=0
RED=$'\033[0;31m'; GREEN=$'\033[0;32m'; NC=$'\033[0m'
ok()  { echo "  ${GREEN}PASS${NC} $1"; PASS=$((PASS+1)); }
bad() { echo "  ${RED}FAIL${NC} $1"; shift; [[ $# -gt 0 ]] && printf '       %s\n' "$@"; FAIL=$((FAIL+1)); }
expect() {
    if grep -qxF -- "$1" <<<"$out"; then ok "$2"; else bad "$2" "want: $1" "got:  $(head -c 1200 <<<"$out")"; fi
}

echo ""; echo "=== Part 1: the builder's release guards ==="
out=$(python3 - "$TOOL_DIR" <<'PY' 2>&1
import sys
sys.path.insert(0, sys.argv[1])
import build as b
def refused(sql):
    try:
        b.guard_sql(sql); return "allowed"
    except SystemExit as e:
        return "refused"
print("subreddits", refused("SELECT key, name FROM subreddits"))
print("kinds", refused("SELECT domain FROM kind_docs"))
print("history", refused("SELECT domain, history_visits FROM serve_sites"))
print("hist", refused("SELECT id, hist FROM suggest_items WHERE kind <> 'subreddit'"))
print("wdc", refused("SELECT osm, name FROM places"))
print("subreddit items", refused("SELECT id FROM suggest_items"))
print("legacy junk", refused("SELECT domain, junk FROM site_quality WHERE source IN ('ut1', 'jev+ut1')"))
print("stage ok", all(refused(sql) == "allowed" for _c, sql in b.STAGE.values() if sql))
PY
)
expect "subreddits refused" "subreddits never ship"
expect "kinds refused" "the legacy kinds tier never ships (ADR-065)"
expect "history refused" "history_visits is never selected"
expect "hist refused" "suggest_items.hist is never selected"
expect "wdc refused" "places without the WDC filter are refused (G3 D3)"
expect "subreddit items refused" "suggest_items without the subreddit filter are refused"
expect "legacy junk refused" "site_quality's legacy columns are never selected"
expect "stage ok True" "every staging SELECT of the builder passes the guard"

echo ""; echo "=== Part 1b: the stage takes the block lists and UT1's file itself (rc5 W3) ==="
out=$("$PY" - "$TOOL_DIR" "$REPO_ROOT/apps/search/release" <<'PY' 2>&1
import contextlib, gzip, io, os, sqlite3, sys, tempfile, types
sys.path.insert(0, sys.argv[1])
import build as b
b.log = lambda *a: None
d = tempfile.mkdtemp()
st = sqlite3.connect(os.path.join(d, "stage.sqlite"))
for t, (cols, _sql) in b.STAGE.items():
    st.execute(f"CREATE TABLE {t} ({cols})")
I = lambda t, *rows: st.executemany(f"INSERT INTO {t} VALUES ({','.join('?' * len(rows[0]))})", rows)
I("s_docs", ("costco.com", "https://www.costco.com/", "Costco", ""), ("playcostco.com", "https://playcostco.com/", "Play", ""),
            ("tracker.net", "https://tracker.net/", "T", ""), ("ads.tracker.net", "https://ads.tracker.net/", "A", ""))
I("s_serve", ("costco.com", 500, 900), ("tracker.net", 7, 7))
I("s_nav", ("costco.com", "costco", 500, "costco.com"), ("costco.ca", "costco", 9000, "costco.ca"))
I("s_own", ("costco.com", "costco.com", 500, 900), ("costco.ca", "costco.ca", 9000, None), ("tracker.net", "tracker.net", 7, 7),
           ("smallsite.org", "smallsite.org", b.PAGE_SITE_CUT + 1, None),
           ("recipes.example", "recipes.example", b.PAGE_SITE_CUT + 1, None), ("farrecipes.example", "farrecipes.example", b.ANSWER_SITE_CUT + 1, None))
I("s_answer", ("recipes.example",), ("farrecipes.example",), ("unranked-recipes.example",))
I("s_pages", ("https://www.costco.com/membership", "costco.com", "Membership"), ("https://costco.ca/fr/aide", "costco.ca", "Aide"),
             ("https://smallsite.org/about", "smallsite.org", "About"), ("https://unranked.example/x", "unranked.example", "X"),
             ("https://ads.costco.ca/promo", "costco.ca", "Promo"), ("https://recipes.example/banana-bread-recipe", "recipes.example", "Banana bread"),
             ("https://farrecipes.example/stew-recipe", "farrecipes.example", "Stew"), ("https://unranked-recipes.example/pie", "unranked-recipes.example", "Pie"),
             # government sites ship their pages with no rank cut (nj.gov and state.mn.us have no row in s_own); gov.uk is not one
             ("https://www.nj.gov/njsp/firearms/", "nj.gov", "Firearms"), ("https://www.dot.state.mn.us/x", "state.mn.us", "MnDOT"),
             ("https://www.gov.uk/x", "gov.uk", "UK"))
I("s_wsites", (10, "https://tracker.net/", "tracker.net", "infobox company", 7))
I("s_hostnav", ("costcotravel", "https://travel.costco.com/", "Costco Travel", "", 5000), ("trackme", "https://me.tracker.net/x", "Me", "", 9))
I("s_items", (1, "site", "Costco", "https://www.costco.com/", "costco.com", "Costco", 1.0, 2.5, 1000.0, None),
             (2, "site", "Tracker", "https://tracker.net/", "tracker.net", None, 1.0, 2.5, 1000.0, None),
             (3, "service", "Me", "https://me.tracker.net/x", None, None, 1.0, 2.5, 1000.0, None))
I("s_keys", ("costco", 1, 0), ("tracker", 2, 0), ("me", 3, 0))
I("s_unsafe", ("badsite.example",))
st.commit()
print("covers", b.blocked_host("ads.example.com", {"example.com"}), b.blocked_host("example.com", {"ads.example.com"}),
      b.blocked_host("example.com", {"example.com"}), b.blocked_host("com", {"com"}), b.blocked_host("notexample.com", {"example.com"}))
Q = lambda sql: sorted(r[0] for r in st.execute(sql))
print("front", [b.front_page(u) for u in ("https://www.gog.com/en", "https://www.realmadrid.com/en-US/", "https://www.weather.gov/aprfc",
      "https://a.example/", "https://a.example", "https://a.example/?x=1", "https://a.example/index.html", "https://a.example/en/shop", "ftp://x")])
I("s_docs", ("gog.com", "https://www.gog.com/en", "GOG.COM", "games"), ("weather.gov", "https://www.weather.gov/aprfc", "Alaska-Pacific RFC", "rivers"))
print("front pages", b.stage_front_pages(st), sorted(st.execute("SELECT domain, url, title, description FROM s_docs WHERE domain IN ('gog.com', 'weather.gov', 'costco.com')")))
st.execute("DELETE FROM s_docs WHERE domain IN ('gog.com', 'weather.gov')")
print("pages kept", b.stage_pages(st), Q("SELECT url FROM s_pages"))
# the stage's government rule in SQL is the importer's (ccpages.gov_domain), read from its source: duckdb is not here
import re as _re
pat = _re.search(r'^_GOV_DOMAIN = re\.compile\(r"(.*)"\)', open(os.path.join(sys.argv[1], "..", "..", "importer", "ccpages.py")).read(), _re.M).group(1)
gd = _re.compile(pat)
names = ["nj.gov", "irs.gov", "state.nj.us", "www.dot.state.mn.us", "gov.uk", "njportal.com", "k12.nj.us", "governor.com", "gov", "mygov.example", "state.nj.usx"]
print("gov rule disagreements", [x for x in names if bool(gd.search(x)) != bool(st.execute(f"SELECT 1 FROM (SELECT ? AS domain) WHERE {b.GOV_DOMAIN_SQL}", (x,)).fetchone())],
      sum(bool(gd.search(x)) for x in names))
open(os.path.join(d, "blocked.tsv"), "w").write("tracker.net\thagezi\nshop.costco.ca\tnocoin\nads.costco.ca\thagezi\n")
b.stage_blocked(st, os.path.join(d, "blocked.tsv"))
print("pages after block", Q("SELECT url FROM s_pages"))
print("docs", Q("SELECT domain FROM s_docs"))
print("serve wsites", Q("SELECT domain FROM s_serve"), Q("SELECT domain FROM s_wsites"))
print("hostnav", Q("SELECT url FROM s_hostnav"))
print("items keys", Q("SELECT id FROM s_items"), Q("SELECT id FROM s_keys"))
print("nav", Q("SELECT host FROM s_nav"))
print("no list name kept", not any("hagezi" in str(v) or "nocoin" in str(v) for t in b.STAGE for r in st.execute(f"SELECT * FROM {t}") for v in r))
with gzip.open(os.path.join(d, "ut1.tsv.gz"), "wt") as f:
    f.write("playcostco.com\thttps://playcostco.com/\tadult\t\t\nwww.costco.ca\thttps://costco.ca/\tgambling\t\t\n"
            "costco.com\thttps://costco.com/\tadvertising\t\t\nnotinrelease.com\thttps://notinrelease.com/\tadult\t\t\n")
by = b.stage_unsafe(st, os.path.join(d, "ut1.tsv.gz"))
print("unsafe", Q("SELECT domain FROM s_unsafe"), by)
print("source recorded", st.execute("SELECT value FROM s_sugmeta WHERE key = 'unsafe_source'").fetchone()[0].startswith("ut1 file sha256 "))
with gzip.open(os.path.join(d, "empty.tsv.gz"), "wt") as f:
    f.write("costco.com\thttps://costco.com/\tadvertising\t\t\n")
try:
    b.stage_unsafe(st, os.path.join(d, "empty.tsv.gz")); print("empty accepted")
except SystemExit as e:
    print("empty refused", "refusing" in str(e), Q("SELECT domain FROM s_unsafe"))
st.close()
# the registry builder takes the staged pages (release_v1.StageSource.pages)
sys.path.insert(0, sys.argv[2])
try:
    import psycopg  # noqa: F401
except ImportError:
    sys.modules["psycopg"] = types.ModuleType("psycopg"); sys.modules["psycopg"].Error = Exception
import release_v1 as rv
with contextlib.redirect_stdout(io.StringIO()):
    rv.cmd_registry(os.path.join(d, "registry.sqlite"), "t1", None, os.path.join(d, "stage.sqlite"))
r = sqlite3.connect(os.path.join(d, "registry.sqlite"))
print("registry pages", sorted(r.execute("SELECT coalesce(h.origin || g.path, g.alt), g.title FROM registry g LEFT JOIN hosts h ON h.id = g.host_id WHERE g.doc_id >> 28 = 3")))
st0 = sqlite3.connect(os.path.join(d, "old-stage.sqlite"))
for t, (cols, _sql) in b.STAGE.items():
    if t != "s_pages":
        st0.execute(f"CREATE TABLE {t} ({cols})")
st0.commit(); st0.close()
print("old stage", list(rv.StageSource(os.path.join(d, "old-stage.sqlite")).pages()))
pq = rv.WEB_INPUT_SQL["pages"]
print("description sorted", "p.description" in pq and pq.index("string_agg") < pq.index("p.description") and "coalesce(p.title, '') || ' ' || coalesce(p.h1, '')" in pq)
print("END")
PY
)
expect "front [('https://www.gog.com/', True), ('https://www.realmadrid.com/', True), ('https://www.weather.gov/', False), ('https://a.example/', True), ('https://a.example/', True), ('https://a.example/', False), ('https://a.example/', True), ('https://a.example/', False), ('ftp://x', True)]" "a site's address is its front page; a language root or an index file still describes the site, any other page does not"
expect "front pages {'moved to the front page': 2, 'of which lost title and description': 1} [('costco.com', 'https://www.costco.com/', 'Costco', ''), ('gog.com', 'https://www.gog.com/', 'GOG.COM', 'games'), ('weather.gov', 'https://www.weather.gov/', None, None)]" "the stage stores front pages: a deep capture loses its title and description, a landing page keeps them, a front page is untouched"
expect "pages kept 6 ['https://ads.costco.ca/promo', 'https://costco.ca/fr/aide', 'https://recipes.example/banana-bread-recipe', 'https://www.costco.com/membership', 'https://www.dot.state.mn.us/x', 'https://www.nj.gov/njsp/firearms/']" "inner pages are kept only for sites inside our rank's top PAGE_SITE_CUT (a small or an unranked site's pages go), for an answer site inside ANSWER_SITE_CUT (one past it, or unranked, ships none), and for a US government site with no rank cut (.gov and state.xx.us; gov.uk is not one)"
expect "gov rule disagreements [] 3" "the stage's government rule in SQL says what the importer's ccpages.gov_domain says, on 11 names (3 of them government: a registrable domain, not a host)"
expect "pages after block ['https://costco.ca/fr/aide', 'https://recipes.example/banana-bread-recipe', 'https://www.costco.com/membership', 'https://www.dot.state.mn.us/x', 'https://www.nj.gov/njsp/firearms/']" "a page on a blocked host leaves the stage"
expect "registry pages [('https://costco.ca/fr/aide', 'Aide'), ('https://recipes.example/banana-bread-recipe', 'Banana bread'), ('https://www.costco.com/membership', 'Membership'), ('https://www.dot.state.mn.us/x', 'MnDOT'), ('https://www.nj.gov/njsp/firearms/', 'Firearms')]" "the registry built from the stage holds the pages: address and title, nothing else"
expect "old stage []" "a stage made before pages were staged still gives a registry without pages"
expect "description sorted True" "a page's description reaches the index only inside the sorted words, never as prose"
expect "covers True False True False False" "a block-list name covers itself and every host under it, never the site above it nor a look-alike"
expect "docs ['costco.com', 'playcostco.com']" "blocked sites and hosts under them leave the staged homepages"
expect "serve wsites ['costco.com'] []" "... the served sites and the articles' official sites"
expect "hostnav ['https://travel.costco.com/']" "... the subdomain services (a service of a clean site stays)"
expect "items keys [1] [1]" "... the suggestion items and their keys"
expect "nav ['costco.ca', 'costco.com']" "a site is not blocked by an entry for a host under it"
expect "no list name kept True" "no block list's name reaches the stage (removal only)"
expect "unsafe ['costco.ca', 'playcostco.com'] {'adult': 1, 'gambling': 1}" "UT1's file flags every site of the release it lists (www. dropped), only the unsafe categories, replacing the database's rows"
expect "source recorded True" "the UT1 file's sha256 is recorded for the lookup file"
expect "empty refused True ['costco.ca', 'playcostco.com']" "a UT1 file with no unsafe row is refused and changes nothing"
grep -qx END <<<"$out" || bad "Part 1b did not run to the end" "$(tail -4 <<<"$out")"

echo ""; echo "=== Part 2: a fixture release ==="
if ! "$PY" -c "import tantivy, numpy" 2>/dev/null; then
    echo "  SKIP: $PY cannot import tantivy + numpy"
else
    tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
    "$PY" - "$TOOL_DIR" "$tmp/stage.sqlite" <<'PY'
import json, sqlite3, sys
sys.path.insert(0, sys.argv[1])
import build as b
st = sqlite3.connect(sys.argv[2])
for t, (cols, _sql) in b.STAGE.items():
    st.execute(f"CREATE TABLE {t} ({cols})")
I = lambda t, *rows: st.executemany(f"INSERT INTO {t} VALUES ({','.join('?' * len(rows[0]))})", rows)
I("s_wiki", (10, "Costco", 1, "Costco Wholesale is a retailer."), (11, "Costco (band)", 2, "A band."), (12, "Walmart", 3, "Walmart is a chain."),
            (13, "Moby-Dick", 4, "Moby-Dick is a novel by Herman Melville."))
I("s_alias", ("costco", 10), ("costco", 11), ("walmart", 12))
I("s_wsites", (10, "https://www.costco.com/", "costco.com", "infobox company", 100))
I("s_facts", ("Costco", "founded", "1983", None, 9, None), ("Costco", "headquarters", "Issaquah", None, None, None))
I("s_next", ("Costco", "Walmart", 1))
I("s_links", ("x", "Q1", "Costco", "https://x.com/Costco", "Costco", "retailer", "Costco", 44, "costco", "costco", None, None, 2),
             ("x", "Q1", "costco", "https://x.com/costco", "Costco", "retailer", "Costco", 44, "costco", None, None, None, 1),
             ("instagram", "Q1", "costco", "https://www.instagram.com/costco/", "Costco", "retailer", "Costco", 44, "costco", None, None, None, 1),
             # Project Gutenberg: a book by Wikidata's ebook ID, an author, a book matched from the catalog
             ("gutenberg", "Q10", "15", "https://www.gutenberg.org/ebooks/15", "Moby-Dick", "novel", "Moby-Dick", 90, "mobydick", "mobydick", None, None, 1),
             ("gutenberg-author", "Q11", "9", "https://www.gutenberg.org/ebooks/author/9", "Herman Melville", "novelist", "Herman Melville", 120, "hermanmelville", "hermanmelville", None, None, 1),
             ("gutenberg-catalog", "Q12", "1342", "https://www.gutenberg.org/ebooks/1342", "Pride and Prejudice", "", "Pride and Prejudice", 80, "prideandprejudice", None, None, None, 1))
I("s_docs", ("costco.com", "https://www.costco.com/", "Costco Wholesale", "Shop online"), ("playcostco.com", "https://playcostco.com/", "Play Costco", "fan site"),
             ("hot.xxx", "https://hot.xxx/", "Hot", "an adult ending, on no list"))
I("s_serve", ("costco.com", 500, 900))
I("s_nav", ("costco.com", "costco", 500, "costco.com"), ("costco.ca", "costco", 9000, "costco.ca"))
I("s_hostnav", ("costcotravel", "https://travel.costco.com/", "Costco Travel", "trips", 5000))
I("s_yt", ("UC1", "costco", "Costco", "videos", "Costco", 500000, 44, "costco", "costco", None, None))
I("s_stock", ("COST", "Nasdaq", "Costco Wholesale Corp", 909832, "Q1", "Costco", "https://costco.com", 44, None, 0, 1))
I("s_stock_keys", ("costco", "COST"))
I("s_items", (1, "site", "Costco", "https://www.costco.com/", "costco.com", "Costco", 1.0, 2.5, 1000.0, None),
             (2, "wiki", "Costco", "https://en.wikipedia.org/wiki/Costco", None, "Costco", 0.5, 1.5, 300.0, "American retailer"))
I("s_keys", ("costco", 1, 0), ("costco", 2, 0))
I("s_top", ("cos", json.dumps([1, 2]), json.dumps([0, 0])))
I("s_phrases", ("cos", json.dumps(["costco hours", "costco rare"]), json.dumps([500.0, 3.0])))
I("s_places", ("node/1", "Costco", "shop=wholesale", "", 40.583, -105.866, "1 Main St", "Twin Falls", "Mo-Su 10-20", "", "", "costco", None, None),
               ("node/2", "Joe's Gym", "leisure=fitness_centre", "", 40.593, -105.876, "", "", "", "", "", "joesgym", None, None),
               # two more addresses in Twin Falls: with Costco they show where the town is (towns.py)
               ("node/3", "Shore Diner", "amenity=restaurant", "", 40.588, -105.871, "", "Twin Falls", "", "", "", "shorediner", None, None),
               ("node/4", "Shore Books", "shop=books", "", 40.578, -105.861, "", "Twin Falls", "", "", "", "shorebooks", None, None),
               ("node/5", "Far Gym", "leisure=fitness_centre", "", 42.183, -107.216, "", "", "", "", "", "fargym", None, None))
I("s_unsafe", ("badsite.example",))
st.commit()
PY
    if ! bout=$("$PY" "$TOOL_DIR/build.py" "$tmp/rel" --stage "$tmp/stage.sqlite" --release-id fixture-1 2>&1); then
        bad "build.py failed on the fixture" "$(tail -20 <<<"$bout")"
    else
        grep -q "BUILD-OK" <<<"$bout" && ok "build.py builds the fixture release (guards passed, VACUUMed)" || bad "no BUILD-OK" "$bout"
        reader_checks() {   # reader_checks DIR -> prints what the release backend answers from DIR
            (cd "$TOOL_DIR" && SEARCH_LOOKUP_BACKEND=release SEARCH_RELEASE_DIR="$1" "$PY" - "$API_DIR" "$1" <<'PY' 2>&1
import importlib.util, sqlite3, sys, types
psycopg = types.ModuleType("psycopg"); psycopg.Error = Exception; sys.modules["psycopg"] = psycopg
sys.path.insert(0, sys.argv[1])
spec = importlib.util.spec_from_file_location("search_api", sys.argv[1] + "/search_api.py")
m = importlib.util.module_from_spec(spec); sys.modules["search_api"] = m; spec.loader.exec_module(m)
lk = m.lookups()
print("opened", lk.name, lk.release_id)
c = sqlite3.connect(sys.argv[2] + "/lookup.sqlite")
cols = {r[1] for t in [x[0] for x in c.execute("SELECT name FROM sqlite_schema WHERE type='table'")] for r in c.execute(f"PRAGMA table_info('{t}')")}
print("no popularity", not cols & {"tranco_rank", "sitelinks", "demand", "followers", "clicks", "counts", "score", "pop", "rank"})
print("freelist", c.execute("PRAGMA freelist_count").fetchone()[0])
print("no wdc", sqlite3.connect(sys.argv[2] + "/places-osm.sqlite").execute("SELECT count(*) FROM places").fetchone()[0])
lk.prefetch(m.exact_names("costco", m.nav_keys("costco"), None))
n0 = lk.names_queries
X = lambda sql, p: list(lk.execute(sql, p))
print("wiki", [(r[0], r[2], r[4], r[6]) for r in X(m.WIKI_EXACT_SITE_SQL, {"nq": "costco"})])
print("nav", [(r[0], r[3]) for r in X(m.NAV_SQL, {"keys": ["costco"], "generic": m.GENERIC_TLD})])
print("nav_sites", X(m.NAV_SITES_SQL, {"keys": ["costco"]}))
print("self", [r[3] for r in X(m.SELF_NAMED_SQL, {"like": "%costco%"})])
print("one names query", lk.names_queries == n0)
print("account", [(r[0], r[4]) for r in X(m.ACCOUNT_SQL, {"p": ["x"], "key": "costco", "keys": ["costco", "thecostco"]})])
print("profiles", [(r[0], r[4], r[1]) for r in X(m.PROFILES_SQL, {"t": "Costco", "p": ["x", "instagram"]})])
print("gutenberg book", [(r[0], r[1]) for r in X(m.ACCOUNT_SQL, {"p": m.GUTENBERG, "key": "mobydick", "keys": ["mobydick", "themobydick"]})])
print("gutenberg author", [(r[0], r[1]) for r in X(m.ACCOUNT_SQL, {"p": m.GUTENBERG, "key": "hermanmelville", "keys": ["hermanmelville", "thehermanmelville"]})])
print("gutenberg catalog", [(r[0], r[1]) for r in X(m.ACCOUNT_SQL, {"p": m.GUTENBERG, "key": "prideandprejudice", "keys": ["prideandprejudice", "theprideandprejudice"]})])
print("gutenberg profiles", [(r[0], r[1]) for r in X(m.PROFILES_SQL, {"t": "Moby-Dick", "p": m.GUTENBERG})])
print("card", lk.card_parts("Costco", 4))
print("facts", [(r[1], r[2]) for r in X(m.FACTS_SQL, {"keys": ["costco"], "facts": ["founded"]})])
print("stock", [r[0] for r in X(m.STOCK_SQL, {"k": "costco", "cls": "#"})])
print("ticker", [r[0] for r in X(m.STOCK_BARE_SQL, {"k": "cost"})])
print("yt", [r[1] for r in X(m.YT_EXACT_SQL, {"key": "costco"})])
print("suggest", [(r[2], r[1], round(r[8])) for r in X(m.SUGGEST_TOP2_SQL, {"p": "cos"})])
print("phrases", m.pick_phrases("cos", *X(m.SUGGEST_PHRASES_SQL, {"p": "cos"})[0], []))
print("typo", [(r[0], r[3]) for r in X(m.TYPO_SQL, {"t": "cotsco"})][:1])
p = {"lat": 40.583, "lon": -105.866, "dlat": 0.36, "dlon": 0.47, "r": 40}
print("gym", [r[1] for r in X(m.PLACES_SQL.format(match=m.PLACE_MATCH_KINDS), dict(p, kinds=["leisure=fitness_centre"]))])
print("place", [(r[0], r[1]) for r in X(m.PLACES_SQL.format(match=m.PLACE_MATCH_NAME), dict(p, name="costco"))])
print("circle", [dict(lk.db.execute("SELECT key, value FROM p.meta WHERE key GLOB \x27town:*\x27"))],
      [r[1] for r in X(m.PLACE_TOWN_SQL, dict(p, keys=["joesgym"], towns=["twinfalls"], near_km=200, far=False))],
      list(X(m.PLACE_TOWN_SQL, dict(p, keys=["fargym"], towns=["twinfalls"], near_km=200, far=False))),
      list(X(m.PLACE_TOWN_SQL, dict(p, lat=45.0, lon=-93.0, keys=["joesgym"], towns=["twinfalls"], near_km=200, far=False))),
      [r[1] for r in X(m.PLACE_TOWN_SQL, dict(p, lat=45.0, lon=-93.0, keys=["joesgym"], towns=["twinfalls"], near_km=200, far=True))])
print("town", [(r[0], r[1], r[4]) for r in X(m.PLACE_TOWN_SQL, dict(p, keys=["costco"], towns=["twinfalls"]))], list(X(m.PLACE_TOWN_SQL, dict(p, keys=["costco"], towns=["stone"]))), list(X(m.PLACE_TOWN_SQL, dict(p, keys=["joesgym"], towns=["twinfalls"]))))
print("junk", X(m.JUNK_SQL, {}))
print("tables", sorted(t for t in lk.present(frozenset({"docs", "wiki_docs", "subreddits", "kind_docs"})) if t in ("subreddits", "kind_docs", "places", "wiki_links")))
PY
            )
        }
        out=$(reader_checks "$tmp/rel")
        expect "opened release fixture-1" "SEARCH_LOOKUP_BACKEND=release opens the folder; release ids agree"
        expect "no popularity True" "lookup.sqlite holds no popularity column"
        expect "freelist 0" "no free pages (VACUUM at the end of the build)"
        expect "no wdc 5" "places-osm.sqlite holds the OSM places"
        expect "wiki [('Costco', 'https://www.costco.com/', 100, True), ('Costco (band)', None, None, False)]" "wiki exact: own title first, official site and its rank (from signals)"
        expect "nav [('https://www.costco.com/', 500)]" "nav: the homepage named costco with its Tranco rank"
        expect "nav_sites [('costco.com', 500), ('costco.ca', 9000)]" "Tranco hosts by name, best rank first"
        expect "self ['playcostco.com']" "self-named site: play + costco"
        expect "one names query True" "the tiers above were answered from ONE prefetched names query"
        expect "account [('x', 'costco')]" "account: one per entity; the row with no hkey sorts first (Postgres NULLS FIRST under DESC)"
        expect "gutenberg book [('gutenberg', 'https://www.gutenberg.org/ebooks/15')]" "Gutenberg on the box path: a book by name answers with its ebook page (templated from the platform's own rows)"
        expect "gutenberg author [('gutenberg-author', 'https://www.gutenberg.org/ebooks/author/9')]" "Gutenberg on the box path: an author answers with the author's shelf"
        expect "gutenberg catalog [('gutenberg-catalog', 'https://www.gutenberg.org/ebooks/1342')]" "Gutenberg on the box path: a book matched from the catalog answers with its ebook page"
        expect "gutenberg profiles [('gutenberg', 'https://www.gutenberg.org/ebooks/15')]" "Gutenberg on the box path: an article's profile links include the book"
        expect "profiles [('instagram', 'costco', 'https://www.instagram.com/costco/'), ('x', 'costco', 'https://x.com/costco')]" "profiles: per platform the shortest handle, en_US order (vord), url from the platform template"
        expect "card ('American retailer', [('Walmart', 'Walmart is a chain.')], None, False)" "the card in one read: description, related articles with leads, no picture, not a person"
        expect "facts [('founded', '1983')]" "facts from the card"
        expect "stock ['COST']" "stock key -> ticker"
        expect "ticker ['COST']" "a bare ticker (cost) finds its stock through names"
        expect "yt ['costco']" "YouTube channel by key"
        expect "suggest [('Costco', 'site', 1000), ('Costco', 'wiki', 300)]" "suggest_top -> items with demand from signals"
        expect "phrases ['costco hours']" "phrases cut at 200 searches at build time, counts not shipped"
        expect "typo [('Costco', 'costco')]" "typo: Tantivy fuzzy over the keys"
        expect "gym [\"Joe's Gym\"]" "places by kind (R*Tree)"
        expect "place [('node/1', 'Costco')]" "places by name, osm id from the registry"
        expect "circle [{'town:twinfalls': '40.58300,-105.86600,1.50,3'}] [\"Joe's Gym\"] [] [] [\"Joe's Gym\"]" "a place with no city lies where its town's addresses are: found in the circle, not outside it; a far town only when the state was named"
        expect "town [('node/1', 'Costco', 'Twin Falls')] [] []" "a place by name and town: its address names the town; another town or no address, nothing"
        expect "junk [('badsite.example',), ('hot.xxx',)]" "junk filter rows: UT1's flag and our own adult-ending rule (registry hosts)"
        [[ "$(sqlite3 "$tmp/rel/lookup.sqlite" "SELECT group_concat(ut1) FROM (SELECT ut1 FROM sites WHERE ut1 IS NOT NULL ORDER BY ut1)" 2>/dev/null || "$PY" -c "import sqlite3,sys; print(','.join(str(r[0]) for r in sqlite3.connect(sys.argv[1]).execute('SELECT ut1 FROM sites WHERE ut1 IS NOT NULL ORDER BY ut1')))" "$tmp/rel/lookup.sqlite")" == "1,2" ]] \
            && ok "the flag says which rule set it: 1 = UT1 (CC BY-SA), 2 = our ending rule" || bad "the two flag values are not 1 and 2"
        expect "tables ['places', 'wiki_links']" "tables(): no subreddits, no kinds; places and links present"

        # card pictures (pictures.sqlite, release_v1.py pictures): optional; the card shows the article's
        rel_out=$out                                  # the T5 comparison below reads the release's answers
        cp -r "$tmp/rel" "$tmp/relpic"
        "$PY" - "$REPO_ROOT/apps/search/release" "$tmp/relpic" <<'PY' || bad "the pictures fixture could not be written"
import sqlite3, sys, types
sys.modules.setdefault("psycopg", types.ModuleType("psycopg"))
sys.path.insert(0, sys.argv[1])
import export as ex
root = sys.argv[2]
costco = sqlite3.connect(root + "/registry.sqlite").execute("SELECT doc_id FROM doc WHERE title = 'Costco' AND type = 1").fetchone()[0]
db = sqlite3.connect(root + "/pictures.sqlite")
db.execute("CREATE TABLE pictures (doc_id INTEGER PRIMARY KEY, file TEXT, img BLOB, w INTEGER, h INTEGER, author TEXT, licence TEXT, licence_url TEXT)")
db.execute("INSERT INTO pictures VALUES (?, 'Costco logo.svg', x'52494646', 250, 90, NULL, 'Public domain', NULL)", (costco,))
walmart = sqlite3.connect(root + "/registry.sqlite").execute("SELECT doc_id FROM doc WHERE title = 'Walmart' AND type = 1").fetchone()[0]
db.execute("INSERT INTO pictures VALUES (?, 'Lost.jpg', NULL, 250, 250, 'A', 'CC BY 4.0', 'u')", (walmart,))   # bytes missing
ex.stamp(db, "pictures", "fixture-1"); db.commit(); db.close()
PY
        pic_checks() {
            (cd "$TOOL_DIR" && SEARCH_LOOKUP_BACKEND=release SEARCH_RELEASE_DIR="$1" "$PY" - "$API_DIR" <<'PY' 2>&1
import importlib.util, sys, types
psycopg = types.ModuleType("psycopg"); psycopg.Error = Exception; sys.modules["psycopg"] = psycopg
sys.path.insert(0, sys.argv[1])
spec = importlib.util.spec_from_file_location("search_api", sys.argv[1] + "/search_api.py")
m = importlib.util.module_from_spec(spec); sys.modules["search_api"] = m
try:
    spec.loader.exec_module(m)
    lk = m.lookups()
    d, rel, pic, person = lk.card_parts("Costco", 4)
    print("pic", pic)
    print("card_image", sorted(m.card_image(pic)) if pic else None)
    print("no bytes", lk.card_parts("Walmart", 4)[2])
except RuntimeError as e:
    print("refused", "pictures" in str(e))
PY
            )
        }
        out=$(pic_checks "$tmp/relpic")
        expect "pic (b'RIFF', 250, 90, None, 'Public domain', None, 'Costco logo.svg')" "pictures.sqlite: the card gets its article's picture (img, size, credit, file)"
        expect "card_image ['h', 'licence', 'source', 'src', 'w']" "... and search_api turns it into the card's picture"
        expect "no bytes None" "a picture row without its image bytes gives no picture"
        sqlite3 "$tmp/relpic/pictures.sqlite" "UPDATE meta SET value = 'lookup' WHERE key = 'role'" 2>/dev/null \
          || "$PY" -c "import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); c.execute(\"UPDATE meta SET value = 'lookup' WHERE key = 'role'\"); c.commit()" "$tmp/relpic/pictures.sqlite"
        out=$(pic_checks "$tmp/relpic")
        expect "refused True" "a pictures file stamped with another role is refused"
        out=$rel_out

        # the same release built against a registry.sqlite + signals.bin in T5's formats
        # (release_v1.py: registry/hosts/platforms, doc_id = type << 28 | seq; LOSIGNL1 per-type arrays)
        "$PY" - "$tmp/rel" "$tmp/t5" <<'PY'
import json, os, sqlite3, struct, sys
import numpy as np
src, dst = sys.argv[1:3]
os.makedirs(dst, exist_ok=True)
old = sqlite3.connect(src + "/registry.sqlite")
sig_meta = json.load(open(src + "/signals.json"))
sig = np.fromfile(src + "/signals.bin", dtype=np.dtype([(n, t) for n, t in sig_meta["dtype"]]))
NAME = {1: "wiki", 2: "site", 5: "account", 6: "youtube", 7: "stock", 4: "place", 8: "link"}
new = sqlite3.connect(dst + "/registry.sqlite")
new.executescript("""CREATE TABLE registry (doc_id INTEGER PRIMARY KEY, ext INTEGER, host_id INTEGER, path TEXT, title TEXT, alt TEXT);
  CREATE TABLE hosts (id INTEGER PRIMARY KEY, origin TEXT UNIQUE, domain TEXT);
  CREATE TABLE platforms (id INTEGER PRIMARY KEY, name TEXT UNIQUE, prefix TEXT, suffix TEXT);
  CREATE TABLE tombstones (doc_id INTEGER PRIMARY KEY, removed_release TEXT);
  CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT) WITHOUT ROWID;
  INSERT INTO meta VALUES ('layout', '3'), ('type_shift', '28');""")
seq, ids, hosts, plats = {}, {}, {}, {}
for did, t5, key, url, title in old.execute("SELECT doc_id, type, key, url, title FROM doc ORDER BY doc_id"):
    n = seq.get(t5, 0); seq[t5] = n + 1
    d = (t5 << 28) | n
    ids[did] = d
    ext = host = path = alt = None
    if t5 == 1:
        ext = int(key)
    elif t5 == 2:                      # site: path = the host; alt only when the url is not https://host/
        path = key
        alt = None if url in (None, f"https://{key}/") else url
        title = title
    elif t5 == 8:                      # link: hosts.origin + path
        origin = url.split("://", 1)[0] + "://" + url.split("://", 1)[1].split("/", 1)[0]
        path = url[len(origin):]
        host = hosts.setdefault(origin, len(hosts) + 1)
        new.execute("INSERT OR IGNORE INTO hosts VALUES (?,?,?)", (host, origin, None))
    elif t5 == 5:                      # account: platform id, path = handle, ext = the Q number, no title
        platform, handle, q = key.split("\t")
        host = plats.setdefault(platform, len(plats) + 1)
        new.execute("INSERT OR IGNORE INTO platforms VALUES (?,?,?,?)", (host, platform, "", ""))
        path, ext, alt, title = handle, int(q[1:]), url, None
    else:
        path = key
    new.execute("INSERT INTO registry VALUES (?,?,?,?,?,?)", (d, ext, host, path, title, alt))
new.commit(); new.close()
# LOSIGNL1: one array per type.field; index = doc_id & 0x0FFFFFFF; 0 = none
names = {"tranco": "own_rank", "cc_hc": "cc_hc_pos", "nav_rank": "nav_rank", "site_rank": "site_rank", "sitelinks": "sitelinks",
         "followers": "followers", "pop": "suggest_pop", "score": "suggest_score", "demand": "suggest_demand"}
counts = {t: n for t, n in seq.items()}
fields, arrays, off = [], [], 0
for t5, n in sorted(counts.items()):
    for f, nm in names.items():
        dt = "<f8" if f == "followers" else ("<f4" if f in ("pop", "score", "demand") else "<i4")
        a = np.zeros(n, dtype=dt)
        for did, d in ids.items():
            if d >> 28 != t5:
                continue
            v = sig[f][did]
            if (dt != "<i4" and v == v) or (dt == "<i4" and v >= 0):
                a[d & 0x0FFFFFFF] = v
        fields.append({"name": f"{NAME[t5]}.{nm}", "type": NAME[t5], "dtype": dt, "count": n, "offset": off})
        arrays.append(a); off += a.nbytes
header = json.dumps({"format": 1, "built_release": "fixture-1", "type_shift": 28, "fields": fields}).encode()
with open(dst + "/signals.bin", "wb") as f:
    f.write(b"LOSIGNL1" + struct.pack("<I", len(header)) + header)
    for a in arrays:
        f.write(a.tobytes())
PY
        if ! bout2=$("$PY" "$TOOL_DIR/build.py" "$tmp/rel2" --stage "$tmp/stage.sqlite" --registry "$tmp/t5/registry.sqlite" --release-id fixture-1 2>&1); then
            bad "build.py --registry (T5 formats) failed" "$(tail -12 <<<"$bout2")"
        else
            grep -q "registry .*: [0-9]* documents" <<<"$bout2" && ok "build.py reads T5's registry (registry/hosts/platforms) and maps our keys to its doc_ids" || bad "registry mode not used" "$bout2"
            # the registry's NOT EXISTS check must SEARCH doc by (type, key), not SCAN it: doc.type once had no
            # column affinity against things.type INTEGER, the index was unusable and the check never finished
            # on 21M real documents (twice, 2026-10-03)
            plan=$("$PY" - "$tmp/stage.sqlite" <<'PY2'
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
print(" | ".join(r[3] for r in c.execute("EXPLAIN QUERY PLAN SELECT t.type, count(*), min(t.key) FROM things t WHERE NOT EXISTS "
      "(SELECT 1 FROM doc d WHERE d.type = t.type AND d.key = t.key) GROUP BY t.type")))
PY2
)
            if grep -q "SEARCH d USING.*(type=? AND key=?)" <<<"$plan"; then ok "the registry check looks doc up by (type, key) (no scan per thing)"; else bad "the registry check scans doc once per thing" "$plan"; fi
            cp "$tmp/t5/registry.sqlite" "$tmp/t5/old-layout.sqlite"
            sqlite3 "$tmp/t5/old-layout.sqlite" "UPDATE meta SET value = '24' WHERE key = 'type_shift'" 2>/dev/null || "$PY" -c "
import sqlite3, sys; c = sqlite3.connect(sys.argv[1]); c.execute(\"UPDATE meta SET value = '24' WHERE key = 'type_shift'\"); c.commit()" "$tmp/t5/old-layout.sqlite"
            if "$PY" "$TOOL_DIR/build.py" "$tmp/rel4" --stage "$tmp/stage.sqlite" --registry "$tmp/t5/old-layout.sqlite" --release-id fixture-1 >/dev/null 2>&1; then
                bad "a layout-2 (24-bit) registry was accepted"; else ok "a registry with the old 24-bit layout is refused"; fi
            [ -f "$tmp/rel2/registry.sqlite" ] && bad "registry stand-in written despite --registry" || ok "no registry / signals stand-in is written when --registry is given"
            cp "$tmp/t5/signals.bin" "$tmp/t5/registry.sqlite" "$tmp/rel2/"      # T5's files join the release folder
            out2=$(reader_checks "$tmp/rel2" | sed 's#/rel2 #/rel #')       # only the path in the first line differs
            # ... and the start-up line that says which rank the release carries (checked on its own below)
            if [[ "$(grep -v '^site rank: ' <<<"$out2")" == "$(grep -v '^site rank: ' <<<"$out")" ]]; then ok "against T5's registry and LOSIGNL1 signals every answer equals the stand-in build's"; else
                bad "answers differ with T5's registry" "$(diff <(echo "$out") <(echo "$out2") | head -10)"; fi
            # the popularity line follows the rank the release carries (rc4): 10000 was fitted on Tranco's order
            conf_rank() { # release-dir [VAR=value ...] -> "rank N popular-at-20000 True|False"
                local d="$1"; shift
                (cd "$TOOL_DIR" && env "$@" SEARCH_LOOKUP_BACKEND=release SEARCH_RELEASE_DIR="$d" "$PY" - "$API_DIR" <<'PY' 2>&1 | tail -1
import importlib.util, sys, types
psycopg = types.ModuleType("psycopg"); psycopg.Error = Exception; sys.modules["psycopg"] = psycopg
sys.path.insert(0, sys.argv[1])
spec = importlib.util.spec_from_file_location("search_api", sys.argv[1] + "/search_api.py")
m = importlib.util.module_from_spec(spec); sys.modules["search_api"] = m; spec.loader.exec_module(m)
m.lookups()
print("rank", m.CONFIDENT_RANK, "popular-at-20000", m.is_popular(20000, 0))
PY
                )
            }
            [[ "$(conf_rank "$tmp/rel2")" == "rank 25000 popular-at-20000 True" ]] && ok "a release that carries site.own_rank: a site is popular up to our rank 25000" || bad "own-rank release: popularity line" "$(conf_rank "$tmp/rel2")"
            [[ "$(conf_rank "$tmp/rel")" == "rank 10000 popular-at-20000 False" ]] && ok "a release without own rank keeps Tranco's line, 10000" || bad "stand-in release: popularity line" "$(conf_rank "$tmp/rel")"
            [[ "$(conf_rank "$tmp/rel2" SEARCH_CONFIDENT_RANK=7000)" == "rank 7000 popular-at-20000 False" ]] && ok "an explicit SEARCH_CONFIDENT_RANK wins over the own-rank default" || bad "explicit SEARCH_CONFIDENT_RANK ignored" "$(conf_rank "$tmp/rel2" SEARCH_CONFIDENT_RANK=7000)"
            [[ "$(conf_rank "$tmp/rel2" SEARCH_CONFIDENT_RANK_OWN=15000)" == "rank 15000 popular-at-20000 False" ]] && ok "SEARCH_CONFIDENT_RANK_OWN moves the own-rank line" || bad "SEARCH_CONFIDENT_RANK_OWN ignored" "$(conf_rank "$tmp/rel2" SEARCH_CONFIDENT_RANK_OWN=15000)"
        fi
    fi
fi

echo ""; echo "=== Part 3: the places release (one file per licence) ==="
if ! "$PY" -c "import tantivy, numpy" 2>/dev/null || ! command -v zstd >/dev/null; then
    echo "  SKIP: needs tantivy + numpy + the zstd command"
else
    tmp3=$(mktemp -d)
    "$PY" - "$TOOL_DIR" "$tmp/stage.sqlite" "$tmp3" <<'PY'
import hashlib, json, os, subprocess, sys
sys.path.insert(0, sys.argv[1])
d = sys.argv[3] + "/places"
os.makedirs(d)
def tsv(name, rows):
    open(f"{d}/{name}", "wb").write(subprocess.run(["zstd", "-q", "-c"], input=("".join("\t".join(r) + "\n" for r in rows)).encode(), capture_output=True).stdout)
osm = [("node/1", "Costco", "shop=wholesale", "", "40.583", "-105.866", "1 Main St", "Twin Falls", "Mo-Su 10:00-20:00", "https://www.costco.com/?utm_source=x&id=7", "", "2026-05-01", "2026-06-01"),
       ("node/2", "Joe's Gym", "leisure=fitness_centre", "", "40.593", "-105.876", "", "", "", "", "", "", "")]
ov = [("ov:aaaaaaaa-0000-4000-8000-000000000001", "Tony's Pizza Palace", "amenity=restaurant", "", "40.588", "-105.871", "2 Oak", "Twin Falls", "", "https://tonys.example/?fbclid=zzz", "", "", "")]
hw = [("node/1", "Mo-Su 09:00-21:00", "2026-09-08", "https://www.costco.com/warehouse/?utm_medium=y", "Costco Twin Falls"),
      ("ov:aaaaaaaa-0000-4000-8000-000000000001", "Mo-Su 11:00-23:00", "2026-09-10", "https://tonys.example/hours", "Tony's")]
tsv("places-osm.tsv.zst", osm); tsv("places-overture.tsv.zst", ov); tsv("hours_web.tsv.zst", hw)
def sha(n): return hashlib.sha256(open(f"{d}/{n}", "rb").read()).hexdigest()
man = {"release": "fixture", "columns": {"places": ["osm", "name", "kind", "brand", "lat", "lon", "street", "city", "hours", "website", "phone", "hours_checked", "edited"],
       "hours_web": ["osm", "hours", "crawled", "url", "name"]}, "sources": {"osm": {"rows": 2}, "overture": {"release": "x", "providers": {"meta": 1, "Foursquare": 1, "msft": 0}}, "hours": {"crawl": "y"}},
       "files": {"places-osm.tsv.zst": {"sha256": sha("places-osm.tsv.zst"), "rows": 2, "licence": "ODbL-1.0"},
                 "places-overture.tsv.zst": {"sha256": sha("places-overture.tsv.zst"), "rows": 1, "licence": "CDLA-Permissive-2.0"},
                 "hours_web.tsv.zst": {"sha256": sha("hours_web.tsv.zst"), "rows": 2, "licence": "facts"}}}
json.dump(man, open(f"{d}/manifest.json", "w"))
# a bad copy: a wdc: id
os.makedirs(sys.argv[3] + "/bad")
os.system(f"cp {d}/* {sys.argv[3]}/bad/")
bad = open(f"{sys.argv[3]}/bad/manifest.json").read()
tsv_path = f"{sys.argv[3]}/bad/places-osm.tsv.zst"
open(tsv_path, "wb").write(subprocess.run(["zstd", "-q", "-c"], input=b"wdc:abc\tX\tshop=x\t\t40.583\t-105.816\t\t\t\t\t\t\t\n", capture_output=True).stdout)
m2 = json.loads(bad); m2["files"]["places-osm.tsv.zst"]["sha256"] = hashlib.sha256(open(tsv_path, "rb").read()).hexdigest(); m2["files"]["places-osm.tsv.zst"]["rows"] = 1
json.dump(m2, open(f"{sys.argv[3]}/bad/manifest.json", "w"))
os.makedirs(sys.argv[3] + "/tampered"); os.system(f"cp {d}/* {sys.argv[3]}/tampered/")
open(f"{sys.argv[3]}/tampered/hours_web.tsv.zst", "ab").write(b"x")
PY
    if "$PY" "$TOOL_DIR/build.py" "$tmp3/relb" --stage "$tmp/stage.sqlite" --places-dir "$tmp3/bad" --release-id fx >/dev/null 2>&1; then
        bad "a places release with a wdc: id was accepted"; else ok "a wdc: id in any places file refuses the build (G3 D3)"; fi
    if "$PY" "$TOOL_DIR/build.py" "$tmp3/relc" --stage "$tmp/stage.sqlite" --places-dir "$tmp3/tampered" --release-id fx >/dev/null 2>&1; then
        bad "a places file that does not match the manifest sha256 was accepted"; else ok "a file whose sha256 differs from the manifest refuses the build"; fi
    if ! bout3=$("$PY" "$TOOL_DIR/build.py" "$tmp3/rel" --stage "$tmp/stage.sqlite" --places-dir "$tmp3/places" --release-id fx 2>&1); then
        bad "build.py --places-dir failed" "$(tail -12 <<<"$bout3")"
    else
        ok "build.py builds three places files from the dated release (sha256 verified)"
        out=$(cd "$TOOL_DIR" && SEARCH_LOOKUP_BACKEND=release SEARCH_RELEASE_DIR="$tmp3/rel" "$PY" - "$API_DIR" "$tmp3/rel" <<'PY' 2>&1
import importlib.util, sqlite3, sys, types
psycopg = types.ModuleType("psycopg"); psycopg.Error = Exception; sys.modules["psycopg"] = psycopg
sys.path.insert(0, sys.argv[1])
spec = importlib.util.spec_from_file_location("search_api", sys.argv[1] + "/search_api.py")
m = importlib.util.module_from_spec(spec); sys.modules["search_api"] = m; spec.loader.exec_module(m)
root = sys.argv[2]
sys.path.insert(0, sys.argv[1] + "/../../release")
import assemble
for f in ("places-osm", "places-overture", "hours-web"):
    c = sqlite3.connect(f"{root}/{f}.sqlite")
    mt = dict(c.execute("SELECT key, value FROM meta WHERE key IN ('role', 'licence')"))
    if f == "places-overture":     # its own meta says what the release manifest's tag says (audit 2026-10-22 G6)
        mt["licence"] = "= manifest tag" if mt.get("licence") == assemble.LICENCES["places-overture.sqlite"] else mt.get("licence")
    print(f, mt, c.execute("PRAGMA freelist_count").fetchone()[0],
          c.execute("SELECT count(*) FROM " + ("hours_web" if f == "hours-web" else "places")).fetchone()[0])
om = dict(sqlite3.connect(f"{root}/places-overture.sqlite").execute("SELECT key, value FROM meta"))
print("overture providers", om.get("providers"), "Data from" in om.get("attribution", "") or "data from Foursquare, Meta" in om.get("attribution", ""),
      "Foursquare Labs" in om.get("attribution", ""), "OpenStreetMap" in om.get("attribution", ""))
print("overture notice", assemble.overture_notice(om)[0])
print("website", [r[0] for r in sqlite3.connect(f"{root}/places-osm.sqlite").execute("SELECT website FROM places WHERE website <> '' ORDER BY pid")],
      [r[0] for r in sqlite3.connect(f"{root}/places-overture.sqlite").execute("SELECT website FROM places")])
print("dates", sqlite3.connect(f"{root}/places-osm.sqlite").execute("SELECT hours_checked, edited FROM places WHERE hours_checked IS NOT NULL").fetchone())
lk = m.lookups()
p = {"lat": 40.583, "lon": -105.866, "dlat": 0.36, "dlon": 0.47, "r": 40, "kinds": ["amenity=restaurant"], "hint": "%pizz%", "hint_kinds": ["amenity=restaurant"]}
rows = list(lk.execute(m.PLACES_SQL.format(match=m.PLACE_MATCH_KINDS), p))
print("pizza", [(r[0][:3], r[1]) for r in rows])
print("gym", [r[1] for r in lk.execute(m.PLACES_SQL.format(match=m.PLACE_MATCH_KINDS), dict(p, kinds=["leisure=fitness_centre"], hint="", hint_kinds=[]))])
rows = list(lk.execute(m.PLACES_SQL.format(match=m.PLACE_MATCH_NAME), dict(p, name="tony's pizza palace")))
print("by name", [r[0][:3] for r in rows])
list(lk.execute(m.PLACE_EXACT_SQL, {"lat": 40.583, "lon": -105.866, "keys": ["costco"], "r": 200}))       # the engine asks hours after it has seen the places
print("hours", sorted(lk.execute(m.HOURS_INFO_SQL, {"ids": ["node/1", "ov:aaaaaaaa-0000-4000-8000-000000000001", "node/2"]})))
tk = sqlite3.connect(f"{root}/places-overture.sqlite").execute("SELECT name_key FROM places").fetchone()[0]
print("chain", list(lk.execute(m.CHAIN_HOURS_SQL, {"keys": ["costco"]})), sorted(lk.execute(m.CHAIN_HOURS_SQL, {"keys": ["costco", tk]})),
      list(lk.execute(m.CHAIN_HOURS_SQL, {"keys": ["joesgym", "nosuchchain"]})))
PY
        )
        expect "places-osm {'licence': 'ODbL-1.0', 'role': 'places-osm'} 0 2" "places-osm.sqlite: OSM rows only, ODbL tag, no free pages"
        expect "places-overture {'licence': '= manifest tag', 'role': 'places-overture'} 0 1" "places-overture.sqlite: the ov: rows; its meta.licence is the release manifest's tag (ODbL + CDLA + CC0), not the places release's CDLA-only one"
        expect "overture providers [\"Foursquare\", \"Meta\"] True True True" "places-overture meta names the providers the places release counted (none with 0 rows), Foursquare's notice and OSM"
        expect "overture notice Overture Maps Foundation (https://overturemaps.org): places, CDLA-Permissive-2.0, data from Foursquare, Meta." "the release NOTICE's Overture line names those providers, from the file's meta"
        expect "hours-web {'licence': 'facts', 'role': 'hours-web'} 0 2" "hours-web.sqlite: kept apart, keyed by the place's doc_id"
        expect "website ['https://www.costco.com/?id=7'] ['https://tonys.example/']" "tracking parameters are stripped from websites (utm_, fbclid), real ones stay"
        expect "dates ('2026-05-01', '2026-06-01')" "hours_checked and edited ride in the OSM file"
        expect "pizza [('ov:', \"Tony's Pizza Palace\")]" "'pizza' finds a restaurant with pizz in its name, from the Overture file (hint arm of PLACE_MATCH_KINDS)"
        expect "gym [\"Joe's Gym\"]" "places by kind merge the OSM file in"
        expect "by name ['ov:']" "places by name search the Overture file too"
        expect "hours [('node/1', '2026-05-01', '2026-06-01', 'Mo-Su 09:00-21:00', '2026-09-08', 'https://www.costco.com/warehouse/'), ('node/2', None, None, '', '', ''), ('ov:aaaaaaaa-0000-4000-8000-000000000001', None, None, 'Mo-Su 11:00-23:00', '2026-09-10', 'https://tonys.example/hours')]" "HOURS_INFO_SQL: OSM check dates and the business's own hours, joined at query time"
        expect "chain [('Mo-Su 09:00-21:00',)] [('Mo-Su 09:00-21:00',), ('Mo-Su 11:00-23:00',)] []" "CHAIN_HOURS_SQL: the chain's own-page hours from hours-web, across both places files; a place with none gives nothing"
    fi
    rm -rf "$tmp3"
fi

echo ""; echo "=== the registry check is indexed ==="
# without the (type, key) index the NOT EXISTS check scanned doc once per thing: 1.5 h and unfinished on 21M real documents
first=$(grep -n 'CREATE UNIQUE INDEX IF NOT EXISTS doc_tk' "$TOOL_DIR/build.py" | head -1 | cut -d: -f1)
chk=$(grep -n 'miss = st.execute' "$TOOL_DIR/build.py" | head -1 | cut -d: -f1)
if [[ -n "$first" && -n "$chk" && "$first" -lt "$chk" ]]; then ok "doc_tk (type, key) exists before the registry's NOT EXISTS check"; else bad "doc_tk is created after the registry check (quadratic on real data)" "index line ${first:-none}, check line ${chk:-none}"; fi

echo ""; echo "=== a place is never looked up by a column with no index ==="
# places has indexes on pid (its key), name_key, brand_key and brand_lower only. hours_info asked by doc_id and read the
# table from its start for every place listed: 530 ms for three places in New York, 46 in Houston (2026-10-11).
LR="$REPO_ROOT/apps/search/serve/api/lookups_release.py"
unindexed=$(grep -nE '\.places( p)? WHERE' "$LR" | grep -vE 'WHERE (p\.)?(pid|name_key|brand_key|brand_lower) ' || true)
if [[ -z "$unindexed" ]]; then ok "every read of places is by pid, name_key, brand_key or brand_lower"; else bad "places read by a column with no index: $unindexed"; fi
if grep -q 'FROM {a}.places WHERE pid = ?", (pid,)' "$LR" && grep -q 'self.place_doc\[osm\] = (r\[0\], r\[12\], r\[13\])' "$LR"; then ok "hours_info finds the place by its pid, in the file it came from"; else bad "hours_info does not look the place up by pid"; fi
# A search made one registry read for every candidate place and kept three (6,368 reads for "new york times" near New
# York, 2026-10-11): the id is found for the kept rows only. kind_members is keyed (kid, doc_id): by doc_id alone it is
# read whole (72 ms a site).
body=$(awk '/def _place_rows\(/,/return out/' "$LR")
if grep -q 'self.reg(' <<<"$body"; then bad "_place_rows reads the registry for every candidate place"; else ok "_place_rows reads no registry row: the kept places get their id in _place_ids"; fi
n_ids=$(grep -c 'self._place_ids(' "$LR"); n_rows=$(grep -cE '\[:(3|40)\]' <(grep 'self._place_ids(' "$LR"))
if [[ "$n_ids" == 4 && "$n_rows" == 4 ]]; then ok "every place list names its places after cutting to the 3 or 40 it returns"; else bad "_place_ids calls: $n_ids, after a cut: $n_rows (want 4 and 4)"; fi
KR="$(dirname "$LR")/kinds_release.py"
km=$(grep -nE 'kind_members m .*WHERE|WHERE m\.' "$KR" | grep -E 'WHERE m\.' | grep -vE 'WHERE m\.kid ' || true)
if [[ -z "$km" ]]; then ok "every read of kind_members is by kid, its key's first column"; else bad "kind_members read without kid: $km"; fi

echo ""
echo "Results: $PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]]
