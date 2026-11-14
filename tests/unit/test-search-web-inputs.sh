#!/usr/bin/env bash
################################################################################
# Unit tests for the release stages that feed the web text side:
#   release_v1.py web-input       sites|pages|anchors .rows.tsv.zst, the web Tantivy indexes' build input
#   release_v1.py anchor-phrases  anchor-phrases.sqlite, the phrase -> site doc_id table behind exact_anchor
# The serving database is a stand-in: `stream` and `check_source` are replaced by canned rows (the SQL itself is
# only proven against the real schema on testbox, see SEARCH-RELEASE-FORMAT.md); what is tested here is everything
# around it: the registry mapping and the drop count, the COPY escapes, the order and byte-reproducibility, the
# refusal paths, that every statement passes export.check_sql_one, and that the output answers exact_anchor.
#
# Usage: bash tests/unit/test-search-web-inputs.sh      (PY=<python3>; needs zstd)
################################################################################
set -uo pipefail
SCRIPT_DIR_T="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR_T/../.." && pwd)"
REL="${REL_DIR:-$REPO_ROOT/apps/search/release}"
API="$REPO_ROOT/apps/search/serve/api"
TD="$REPO_ROOT/apps/search/tools/tantivy-structure"
PY="${PY:-python3}"
export PYTHONDONTWRITEBYTECODE=1
PASS=0; FAIL=0
RED=$'\033[0;31m'; GREEN=$'\033[0;32m'; NC=$'\033[0m'
ok()  { echo "  ${GREEN}PASS${NC} $1"; PASS=$((PASS+1)); }
bad() { echo "  ${RED}FAIL${NC} $1"; shift; [[ $# -gt 0 ]] && printf '       %s\n' "$@"; FAIL=$((FAIL+1)); }
expect() { if grep -qxF -- "$1" <<<"$out"; then ok "$2"; else bad "$2" "want: $1" "got:  $(head -c 1500 <<<"$out")"; fi; }
command -v zstd >/dev/null || { echo "  SKIP: zstd missing"; echo "Total: 0  PASS: 0  FAIL: 0 (skipped)"; exit 0; }
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT

echo ""; echo "=== the stages against a stand-in serving database ==="
out=$("$PY" - "$REL" "$API" "$TD" "$tmp" <<'PY' 2>&1
import hashlib, json, os, sqlite3, subprocess, sys, types
rel, api, td, tmp = sys.argv[1:5]
psycopg = types.ModuleType("psycopg"); psycopg.Error = Exception; sys.modules["psycopg"] = psycopg
sys.path.insert(0, rel)
import release_v1 as rv
import export as ex
import registry_idents as ri
import release_check as rc
import docs_policy as dp
S = 28


def registry(path, pages=True):
    db = sqlite3.connect(path)
    db.executescript("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT); CREATE TABLE hosts (id INTEGER PRIMARY KEY, origin TEXT UNIQUE NOT NULL);"
                     "CREATE TABLE platforms (id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, prefix TEXT, suffix TEXT);"
                     "CREATE TABLE registry (doc_id INTEGER PRIMARY KEY, ext INTEGER, host_id INTEGER, path TEXT, title TEXT, alt TEXT);")
    db.execute("INSERT INTO meta VALUES ('type_shift', ?), ('layout', ?)", (str(S), ri.LAYOUT))
    for i, h in enumerate(["alpha.example", "bücher.example", "gamma.example", "nodocs.example"]):   # nodocs: in the registry, no crawl row
        db.execute("INSERT INTO registry VALUES (?, NULL, NULL, ?, NULL, NULL)", ((2 << S) | i, h))
    if pages:
        db.execute("INSERT INTO hosts VALUES (1, 'https://alpha.example')")
        db.execute("INSERT INTO registry VALUES (?, NULL, 1, '/p/one', 't', NULL)", ((3 << S) | 0,))
        db.execute("INSERT INTO registry VALUES (?, NULL, NULL, 'x', NULL, 'mailto:odd@odd.example')", ((3 << S) | 1,))
    db.commit(); db.close()


SITES = [  # (domain, head, body): sorted as Postgres sorts under COLLATE "C"; ghost.example is not in the registry
    ("alpha.example", "alpha example Alpha", "alpha example Alpha desc words"),
    ("bücher.example", "bücher example", "tab\there new\nline back\\slash ü"),
    ("gamma.example", "gamma example", "gamma body"),
    ("ghost.example", "ghost", "ghost body")]
PAGES = [("https://alpha.example/p/one", "page one words"), ("https://alpha.example/zz/unknown", "unknown page"), ("mailto:odd@odd.example", "odd")]
ANCH = [("alpha.example", "the alpha . the alpha ."), ("gamma.example", "gamma . gamma . gamma ."), ("ghost.example", "ghost anchor .")]
PH = [  # (domain, anchor, sources) as the statement returns them: ordered by anchor, domain
    ("alpha.example", "big name", 40), ("gamma.example", "big name", 12), ("bücher.example", "big name", 40),
    ("ghost.example", "big name", 99999),
    ("gamma.example", "dup phrase", 3), ("gamma.example", "dup phrase", 7),
    ("ghost.example", "ghost only phrase", 50),
    ("alpha.example", "solo", 2)]
calls = []
data = {rv.WEB_INPUT_SQL["sites"]: SITES, rv.WEB_INPUT_SQL["pages"]: PAGES, rv.WEB_INPUT_SQL["anchors"]: ANCH, rv.ANCHOR_PHRASE_SQL: PH,
        rv.PAGES_OUT_SQL: [(4, 1)]}     # pages the homepage capture rule keeps out, of which from an open dump


def fake_stream(dsn, sql, n=20000):
    calls.append("stream")
    yield from data[sql]


rv.stream = fake_stream
ex.check_source = lambda dsn=None: calls.append("check_source") or {}


def zread(p):
    return subprocess.run(["zstd", "-dc", p], capture_output=True, check=True).stdout.decode()


def report(fn, *a):
    import contextlib, io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn(*a)
    return json.loads(buf.getvalue().strip().splitlines()[-1])


registry(tmp + "/reg.sqlite")
r1 = report(rv.cmd_webinput, tmp + "/reg.sqlite", tmp + "/o1", "rel-1")
print("check_source before any stream", calls[0] == "check_source" and calls.index("stream") > 0)
print("sites rows", repr(zread(tmp + "/o1/sites.rows.tsv.zst")))
print("pages rows", repr(zread(tmp + "/o1/pages.rows.tsv.zst")))
print("anchors rows", repr(zread(tmp + "/o1/anchors.rows.tsv.zst")))
k = r1["kinds"]
print("sites counts", {x: k["sites"][x] for x in ("rows", "source_rows", "dropped_not_in_registry", "registry_without_row")})
print("pages counts", {x: k["pages"][x] for x in ("rows", "source_rows", "dropped_not_in_registry", "registry_without_row")})
print("anchors counts", {x: k["anchors"][x] for x in ("rows", "source_rows", "dropped_not_in_registry", "registry_without_row")})
print("pages capture rule", k["pages"].get("dropped_capture_rule"))
print("report stage", r1["stage"], r1["release"])
# the hand-off: web_input.py map accepts the file (no unknown ident, no duplicate, right column count) and keys doc_ids
for kind, want in (("sites", [(2 << S) | 0, (2 << S) | 1, (2 << S) | 2]), ("pages", [(3 << S) | 0, (3 << S) | 1]), ("anchors", [(2 << S) | 0, (2 << S) | 2])):
    z = subprocess.run(["zstd", "-dc", f"{tmp}/o1/{kind}.rows.tsv.zst"], capture_output=True, check=True).stdout
    m = subprocess.run([sys.executable, td + "/web_input.py", "map", kind, tmp + "/reg.sqlite"], input=z, capture_output=True)
    ids = [int(l.split(b"\t")[0]) for l in m.stdout.splitlines()]
    print("map accepts", kind, m.returncode == 0 and ids == want)
# reproducible: the same inputs give the same bytes
r2 = report(rv.cmd_webinput, tmp + "/reg.sqlite", tmp + "/o2", "rel-1")
same = all(open(f"{tmp}/o1/{x}.rows.tsv.zst", "rb").read() == open(f"{tmp}/o2/{x}.rows.tsv.zst", "rb").read() for x in ("sites", "pages", "anchors"))
print("reproducible bytes", same, all(r1["kinds"][x]["tsv_sha256"] == r2["kinds"][x]["tsv_sha256"] for x in r1["kinds"]))
print("tsv sha is the sha of the rows", k["sites"]["tsv_sha256"] == hashlib.sha256(zread(tmp + "/o1/sites.rows.tsv.zst").encode()).hexdigest())
# a registry with no page type
registry(tmp + "/reg0.sqlite", pages=False)
r0 = report(rv.cmd_webinput, tmp + "/reg0.sqlite", tmp + "/o0", "rel-1")
print("no page type", repr(zread(tmp + "/o0/pages.rows.tsv.zst")), r0["kinds"]["pages"]["rows"], r0["kinds"]["pages"]["dropped_not_in_registry"])

# with the release's gov-states.json a page on a state's government site gets that state's word in its bag of words
db = sqlite3.connect(tmp + "/reg.sqlite")
db.execute("INSERT INTO hosts VALUES (2, 'https://www.nj.gov')")
db.execute("INSERT INTO registry VALUES (?, NULL, 2, '/njsp/firearms', 'Firearms', NULL)", ((3 << S) | 2,))
db.commit(); db.close()
PAGES_GOV = [("https://alpha.example/p/one", "page one words"), ("https://alpha.example/zz/unknown", "unknown page"),
             ("https://www.nj.gov/njsp/firearms", "card firearms id"), ("mailto:odd@odd.example", "odd")]
data[rv.WEB_INPUT_SQL["pages"]] = PAGES_GOV
open(tmp + "/gov-states.json", "w").write(json.dumps({"source": "CISA .gov registry (public domain)", "domains": {"nj.gov": "NJ"}}))
rg = report(rv.cmd_webinput, tmp + "/reg.sqlite", tmp + "/og", "rel-1", tmp + "/gov-states.json")
print("gov pages rows", repr(zread(tmp + "/og/pages.rows.tsv.zst")))
g = rg["kinds"]["pages"]["gov_states"]
print("gov report", g["file"], g["domains"], g["pages_with_state"], g["states"], len(g["sha256"]))
print("gov sites rows unchanged", zread(tmp + "/og/sites.rows.tsv.zst") == zread(tmp + "/o1/sites.rows.tsv.zst"))
rn = report(rv.cmd_webinput, tmp + "/reg.sqlite", tmp + "/on", "rel-1")
print("no gov file no word", repr(zread(tmp + "/on/pages.rows.tsv.zst")), "gov_states" in rn["kinds"]["pages"])
data[rv.WEB_INPUT_SQL["pages"]] = PAGES


def refused(fn, *a):
    try:
        fn(*a)
        return "ran"
    except SystemExit as e:
        return "refused: " + str(e)[:200]


# a refused source writes nothing
ex.check_source = lambda dsn=None: sys.exit("ADR-059: readable page text — refusing")
print("source refused", refused(rv.cmd_webinput, tmp + "/reg.sqlite", tmp + "/o3", "rel-1")[:26], os.path.exists(tmp + "/o3"))
print("source refused (phrases)", refused(rv.cmd_anchorphrases, tmp + "/reg.sqlite", tmp + "/o3.sqlite", "rel-1")[:26], os.path.exists(tmp + "/o3.sqlite"))
ex.check_source = lambda dsn=None: calls.append("check_source") or {}
# a gov-states.json that is not the shape the engine reads is refused before anything is read
open(tmp + "/gov-bad.json", "w").write(json.dumps({"domains": {}}))
print("bad gov file refused", refused(rv.cmd_webinput, tmp + "/reg.sqlite", tmp + "/ob", "rel-1", tmp + "/gov-bad.json")[:19], os.path.exists(tmp + "/ob"))
# wrong width / unsorted statements are refused, not written
data[rv.WEB_INPUT_SQL["sites"]] = [("alpha.example", "only head")]
print("wrong width", refused(rv.cmd_webinput, tmp + "/reg.sqlite", tmp + "/o4", "rel-1"))
data[rv.WEB_INPUT_SQL["sites"]] = [("gamma.example", "h", "b"), ("alpha.example", "h", "b")]
print("unsorted", refused(rv.cmd_webinput, tmp + "/reg.sqlite", tmp + "/o5", "rel-1"))
data[rv.WEB_INPUT_SQL["sites"]] = SITES

# the statements: every one passes the release's own SQL guard; the text ones go through page_words, the anchor ones carry the block list
for name, q in list(rv.WEB_INPUT_SQL.items()) + [("phrases", rv.ANCHOR_PHRASE_SQL)]:
    print("guard accepts", name, refused(ex.check_sql_one, q) == "ran")
print("sites via page_words", "string_agg(w" in rv.WEB_INPUT_SQL["sites"], "string_agg(w" in rv.WEB_INPUT_SQL["pages"])
print("one row per ident", "DISTINCT ON (d.domain" in rv.WEB_INPUT_SQL["sites"], "DISTINCT ON (p.url" in rv.WEB_INPUT_SQL["pages"])
print("pages follow the homepage rule", "p.domain NOT IN (" + dp.PG_DOMAINS_NOINDEX + ")" in rv.WEB_INPUT_SQL["pages"],
      "domain NOT IN (" + dp.PG_DOMAINS_NOINDEX + ")" in rv.PAGE_SQL, "domain IN (" + dp.PG_DOMAINS_NOINDEX + ")" in rv.PAGES_OUT_SQL,
      not any(dp.PG_DOMAINS_OUT in q for q in (rv.WEB_INPUT_SQL["pages"], rv.PAGE_SQL, rv.PAGES_OUT_SQL)))
print("guard accepts page statements", refused(ex.check_sql_one, rv.PAGE_SQL) == "ran", refused(ex.check_sql_one, rv.PAGES_OUT_SQL) == "ran",
      rv.PAGES_OUT_SQL in rv.REGISTRY_SQL)
print("only fetched", "d.error IS NULL" in rv.WEB_INPUT_SQL["sites"], "p.error IS NULL" in rv.WEB_INPUT_SQL["pages"], "error IS NULL" in rv.ANCHOR_PHRASE_SQL)
print("block list in the anchor statements", ex.BLOCK_SQL in rv.WEB_INPUT_SQL["anchors"], ex.BLOCK_SQL in rv.ANCHOR_PHRASE_SQL)
print("guard refuses anchor_docs without the block list", refused(ex.check_sql_one, rv.WEB_INPUT_SQL["anchors"].replace(ex.BLOCK_SQL, "x"))[:8])
print("guard refuses anchors without the block list", refused(ex.check_sql_one, rv.ANCHOR_PHRASE_SQL.replace(ex.BLOCK_SQL, "x"))[:8])

# the stages run the guard themselves: a statement without the block list is refused before anything is read
good = rv.WEB_INPUT_SQL["anchors"]
rv.WEB_INPUT_SQL["anchors"] = good.replace(ex.BLOCK_SQL, "x"); data[rv.WEB_INPUT_SQL["anchors"]] = ANCH
n0 = calls.count("stream")
print("web-input runs the SQL guard", refused(rv.cmd_webinput, tmp + "/reg.sqlite", tmp + "/o6", "rel-1")[:19], calls.count("stream") == n0)
rv.WEB_INPUT_SQL["anchors"] = good
good = rv.ANCHOR_PHRASE_SQL
rv.ANCHOR_PHRASE_SQL = good.replace(ex.BLOCK_SQL, "x"); data[rv.ANCHOR_PHRASE_SQL] = PH
print("anchor-phrases runs the SQL guard", refused(rv.cmd_anchorphrases, tmp + "/reg.sqlite", tmp + "/o6.sqlite", "rel-1")[:19], calls.count("stream") == n0)
rv.ANCHOR_PHRASE_SQL = good

# ---- anchor-phrases.sqlite
pr = report(rv.cmd_anchorphrases, tmp + "/reg.sqlite", tmp + "/anchor-phrases.sqlite", "rel-1")
db = sqlite3.connect(tmp + "/anchor-phrases.sqlite")
print("rows", db.execute("SELECT anchor, sources, doc_id - (2 << 28) FROM anchor_phrase ORDER BY anchor, sources DESC, doc_id").fetchall())
Q = "SELECT doc_id, sources FROM anchor_phrase WHERE anchor = ? ORDER BY sources DESC, doc_id LIMIT 2"
print("top two", [(d - (2 << S), s) for d, s in db.execute(Q, ("big name",))])
plan = " | ".join(r[3] for r in db.execute("EXPLAIN QUERY PLAN " + Q, ("big name",)))
print("plan no sort", "TEMP B-TREE" not in plan.upper(), "PRIMARY KEY" in plan.upper() or "sqlite_autoindex" in plan)
print("counts", {x: pr[x] for x in ("source_rows", "rows", "distinct_anchors", "dropped_not_in_registry", "collapsed_duplicates")})
meta = dict(db.execute("SELECT key, value FROM meta"))
print("stamp", db.execute("PRAGMA application_id").fetchone()[0] == ex.APPLICATION_ID, db.execute("PRAGMA user_version").fetchone()[0], meta["role"], meta["built_release"], "Common Crawl" in meta["licence"])
print("no free pages", db.execute("PRAGMA freelist_count").fetchone()[0])
print("sqlguard", rv.sqlguard_file(tmp + "/anchor-phrases.sqlite")["problems"])
os.makedirs(tmp + "/relx", exist_ok=True)
open(tmp + "/relx/anchor-phrases.sqlite", "wb").write(open(tmp + "/anchor-phrases.sqlite", "rb").read())
json.dump({"format": 1, "files": [{"path": "anchor-phrases.sqlite", "bytes": os.path.getsize(tmp + "/anchor-phrases.sqlite")}]}, open(tmp + "/relx/manifest.json", "w"))
print("engine release check", rc.check_release(tmp + "/relx"))
report(rv.cmd_anchorphrases, tmp + "/reg.sqlite", tmp + "/anchor-phrases2.sqlite", "rel-1")
print("sqlite reproducible", open(tmp + "/anchor-phrases.sqlite", "rb").read() == open(tmp + "/anchor-phrases2.sqlite", "rb").read())
db.close()

# the table answers the engine's rule: search_api.exact_anchor() on rows read the documented way
sys.path.insert(0, api)
import importlib.util
spec = importlib.util.spec_from_file_location("search_api", api + "/search_api.py")
m = importlib.util.module_from_spec(spec); sys.modules["search_api"] = m; spec.loader.exec_module(m)
db = sqlite3.connect(tmp + "/anchor-phrases.sqlite")
def exact(key):
    rows = [(d, s, f"https://{d}/", "t", "d") for d, s in db.execute(Q, (key,))]
    r = m.exact_anchor(rows)
    return None if r is None else r["url"].split("/")[2]
print("exact_anchor: 40 vs 40 is not a lead", exact("big name"))
print("exact_anchor: unknown phrase", exact("nothing here"))
print("exact_anchor: 7 sources is below the minimum", exact("dup phrase"))
PY
)
expect "check_source before any stream True" "web-input: the source is checked (release-profiled, no page text) before a row is read"
expect "sites rows 'alpha.example\talpha example Alpha\talpha example Alpha desc words\nbücher.example\tbücher example\ttab\\\\there new\\\\nline back\\\\\\\\slash ü\ngamma.example\tgamma example\tgamma body\n'" "sites: ident, head, body in ident order; a host the registry lacks is gone; tab, newline and backslash are COPY-escaped"
expect "pages rows 'https://alpha.example/p/one\tpage one words\nmailto:odd@odd.example\todd\n'" "pages: the registry's url ident (host origin + path, or alt) with its body; an unknown url is gone"
expect "anchors rows 'alpha.example\tthe alpha . the alpha .\ngamma.example\tgamma . gamma . gamma .\n'" "anchors: the site's host ident and the anchor document; a host the registry lacks is gone"
expect "sites counts {'rows': 3, 'source_rows': 4, 'dropped_not_in_registry': 1, 'registry_without_row': 1}" "the report counts dropped rows and registry sites that had no crawl row"
expect "pages counts {'rows': 2, 'source_rows': 3, 'dropped_not_in_registry': 1, 'registry_without_row': 0}" "the report counts dropped pages"
expect "anchors counts {'rows': 2, 'source_rows': 3, 'dropped_not_in_registry': 1, 'registry_without_row': 2}" "the report counts dropped anchor documents"
expect "pages capture rule {'pages': 4, 'of_which_open_dump': 1}" "the report counts the pages the homepage capture rule kept out (audit 2026-10-22 G4)"
expect "pages follow the homepage rule True True True True" "pages statements (web input, registry) exclude the sites whose homepage said noindex (docs_policy.PG_DOMAINS_NOINDEX), not every Wayback homepage"
expect "guard accepts page statements True True True" "the registry's page statements pass export.check_sql_one (the count is checked with the registry's)"
expect "report stage web-input rel-1" "the report line names the stage and the release"
expect "map accepts sites True" "sites file: web_input.py map accepts it and keys the registry doc_ids (the local-build.sh hand-off)"
expect "map accepts pages True" "pages file: web_input.py map accepts it"
expect "map accepts anchors True" "anchors file: web_input.py map accepts it (the site's doc_id)"
expect "reproducible bytes True True" "the same inputs give the same files and the same row hashes"
expect "tsv sha is the sha of the rows True" "tsv_sha256 is the sha256 of the uncompressed rows"
expect "no page type '' 0 3" "a registry without pages gives an empty pages file, not an abort"
expect "gov pages rows 'https://alpha.example/p/one\tpage one words\nhttps://www.nj.gov/njsp/firearms\tcard firearms id govstatenj govsitegkhflmilopkc\nmailto:odd@odd.example\todd\n'" ": with gov-states.json a page on a state's government site carries that state's word (govstatenj) and its site's (item 8, govsite...) after its bag of words; other pages are untouched"
expect "gov report gov-states.json 1 1 1 64" "the report names the states file (name, sha256), its domains, the pages that got a state word and how many states"
expect "gov sites rows unchanged True" "the sites rows do not change with gov-states.json"
expect "no gov file no word 'https://alpha.example/p/one\tpage one words\nhttps://www.nj.gov/njsp/firearms\tcard firearms id\nmailto:odd@odd.example\todd\n' False" "without gov-states.json no page gets a state word and the report has no gov_states entry (a release built as before)"
expect "bad gov file refused refused: web-input: False" "a gov-states.json of the wrong shape refuses the stage before anything is written"
expect "source refused refused: ADR-059: readable False" "web-input: a source check that refuses leaves no output"
expect "source refused (phrases) refused: ADR-059: readable False" "anchor-phrases: a source check that refuses leaves no output"
expect "wrong width refused: web-input sites: the statement returned 2 columns, web_input wants 3" "a statement of the wrong width is refused, naming the counts"
expect "unsorted refused: web-input sites: 'alpha.example' after 'gamma.example' — not one row per ident in order, refusing" "rows out of order (or a repeated ident) are refused"
expect "guard accepts sites True" "the sites statement passes export.check_sql_one"
expect "guard accepts pages True" "the pages statement passes export.check_sql_one"
expect "guard accepts anchors True" "the anchors statement passes export.check_sql_one"
expect "guard accepts phrases True" "the anchor-phrases statement passes export.check_sql_one"
expect "sites via page_words True True" "site and page bodies go through page_words (ADR-059) inside the statement"
expect "one row per ident True True" "site and page statements keep one crawl per ident (DISTINCT ON, as the registry does)"
expect "only fetched True True True" "only fetched documents (error IS NULL) enter sites, pages and phrases"
expect "block list in the anchor statements True True" "both anchor statements carry the block list (G3 D9)"
expect "guard refuses anchor_docs without the block list refused:" "the guard refuses the anchor-document statement without the block list"
expect "guard refuses anchors without the block list refused:" "the guard refuses the phrase statement without the block list"
expect "web-input runs the SQL guard refused: G3: export True" "web-input applies export.check_sql_one itself, before reading"
expect "anchor-phrases runs the SQL guard refused: G3: export True" "anchor-phrases applies export.check_sql_one itself, before reading"
expect "rows [('big name', 40, 0), ('big name', 40, 1), ('big name', 12, 2), ('dup phrase', 7, 2), ('solo', 2, 0)]" "anchor_phrase rows: sources descending then doc_id; a repeated (site, phrase) keeps the larger count; an unknown site is gone"
expect "top two [(0, 40), (1, 40)]" "the documented query returns the two best sites, doc_id breaking the tie"
expect "plan no sort True True" "the documented query is answered from the primary key (no sort step)"
expect "counts {'source_rows': 8, 'rows': 5, 'distinct_anchors': 3, 'dropped_not_in_registry': 2, 'collapsed_duplicates': 1}" "anchor-phrases report: source rows, rows kept, drops, collapsed duplicates"
expect "stamp True 1 anchor-phrases rel-1 True" "stamped: application_id, format, role anchor-phrases, built release, licence names Common Crawl"
expect "no free pages 0" "no deleted rows are left on disk"
expect "sqlguard []" "the build-side SQLite guard passes the file"
expect "engine release check []" "the engine's release check accepts the file (role is read from the file name)"
expect "sqlite reproducible True" "two builds of the same rows are byte-identical"
expect "exact_anchor: 40 vs 40 is not a lead None" "search_api.exact_anchor on the table's rows: a tie gives no answer, as on Postgres"
expect "exact_anchor: unknown phrase None" "an unknown phrase gives no rows and no answer"
expect "exact_anchor: 7 sources is below the minimum None" "a phrase under EXACT_ANCHOR_MIN gives no answer"

echo ""; echo "Total: $((PASS+FAIL))  PASS: $PASS  FAIL: $FAIL"
[[ $FAIL -eq 0 ]]
