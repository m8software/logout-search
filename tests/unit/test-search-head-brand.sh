#!/usr/bin/env bash
# requires: cmd:python3
################################################################################
# Unit tests for apps/search/tools/head-brand-evalset.py (step 2): which articles are
# "a site's own article, by name", which host and query a row gets, the split, the strata.
# Pure functions only: no network (QLever and the pageview API are not called), no database.
#
# Usage: bash tests/unit/test-search-head-brand.sh   (PY=/path/to/python to override)
################################################################################
set -uo pipefail

SCRIPT_DIR_T="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR_T/../.." && pwd)"
TOOLS="${REPO_ROOT}/apps/search/tools"
PY="${PY:-python3}"

PASS=0
FAIL=0
RED=$'\033[0;31m'; GREEN=$'\033[0;32m'; NC=$'\033[0m'
ok()  { echo "  ${GREEN}PASS${NC} $1"; PASS=$((PASS+1)); }
bad() { echo "  ${RED}FAIL${NC} $1"; shift; [[ $# -gt 0 ]] && printf '       %s\n' "$@"; FAIL=$((FAIL+1)); }

[[ -f "${TOOLS}/head-brand-evalset.py" ]] || { echo "SKIP: ${TOOLS}/head-brand-evalset.py not present"; exit 0; }

out=$(TOOLS="$TOOLS" REPO="$REPO_ROOT" "$PY" - 2>&1 <<'PYEOF'
import importlib.util, os, re, sys
def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path); m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m; spec.loader.exec_module(m); return m
T = os.environ["TOOLS"]
hb = load("head_brand", T + "/head-brand-evalset.py")
def check(name, got, want):
    print(f"OK {name}" if got == want else f"BAD {name}: got {got!r}, want {want!r}")

# --- which title names which host
check("a title that is the host's first label", hb.name_query("Reddit", "reddit.com"), ("reddit", "title"))
check("a trailing (...) is not part of the name", hb.name_query("Telegram (software)", "telegram.org"), ("telegram", "title"))
check("a title that is the whole host asks by its label", hb.name_query("GOG.com", "gog.com"), ("gog", "label"))
check("spaces and case do not matter", hb.name_query("The Weather Channel", "theweatherchannel.com"), ("the weather channel", "title"))
check("a place whose site has another name is not a brand row", hb.name_query("Italy", "italia.it"), None)
check("a longer title than the host is not a match", hb.name_query("Amazon Prime Video", "primevideo.com"), None)
check("a one-letter name is refused", hb.name_query("X", "x.com"), None)

# --- front pages
check("www. is dropped", hb.front_host("https://www.gog.com/"), "gog.com")
check("no path at all is a front page", hb.front_host("http://reddit.com"), "reddit.com")
check("a language path is still a front page", hb.front_host("https://example.org/en"), "example.org")
check("a deep link is not", hb.front_host("https://www.amazon.co.jp/gp/video/storefront"), None)
check("a query string is not", hb.front_host("https://example.org/?lang=en"), None)
check("a subdomain keeps its host", hb.front_host("https://desktop.telegram.org/"), "desktop.telegram.org")

# --- candidates from the QLever TSV
ONE = '"1"^^<http://www.w3.org/2001/XMLSchema#int>'
def W(title, site, links, notsite="", org=ONE):
    return f"<https://en.wikipedia.org/wiki/{title}>\t<{site}>\t\"{links}\"^^<http://www.w3.org/2001/XMLSchema#int>\t{org}\t{notsite}"
rows, amb = hb.candidates(["?article\t?site\t?links\t?org\t?notsite",
    W("Reddit", "https://www.reddit.com/", 150),
    W("Reddit", "https://old.reddit.com", 150),
    W("GOG.com", "https://www.gog.com/", 33),
    W("C%C3%A1diz", "http://www.cadiz.es", 114, ONE),
    W("Snoop_Dogg", "https://snoopdogg.com", 98, ONE),
    W("Italy", "https://www.italia.it/", 409, ONE),
    W("Amazon_Prime_Video", "https://www.primevideo.com/", 53),
    W("Mercury_(company)", "https://mercury.com/", 10),
    W("Mercury_(band)", "https://mercury.org/", 12),
    W("Deep_Only", "https://deeponly.com/about/us", 70),
    W("Project_Hail_Mary", "https://projecthailmary.com/", 60, "", ""),
    W("Wikipedia", "https://wikipedia.org/", 381),
    W("Wikipedia", "https://wikipedia.com/", 381)])
check("rows are the named front pages, most linked first; every host the title names is an answer",
      [(r[0], r[1], r[2], r[3]) for r in rows],
      [("Wikipedia", "wikipedia.com|wikipedia.org", "wikipedia", "title"), ("Reddit", "reddit.com", "reddit", "title"), ("GOG.com", "gog.com", "gog", "label")])
check("sitelinks are read", [r[4] for r in rows], [381, 150, 33])
check("a person and a place are left out even when the name matches", [r[0] for r in rows if r[0] in ("Cádiz", "Snoop Dogg", "Italy")], [])
check("a film that names its own site is no brand row (no organisation type)", [r[0] for r in rows if "Hail" in r[0]], [])
check("a name two articles share is ambiguous, not a row", amb, [("mercury", 2)])
check("the hosts do not depend on row order",
      hb.candidates([W("Wikipedia", "https://wikipedia.org/", 1), W("Wikipedia", "https://wikipedia.com/", 1)])[0][0][1],
      "wikipedia.com|wikipedia.org")

# --- the set
views = {"Wikipedia": 5, "Reddit": 900, "GOG.com": 50}
res = hb.build(rows, views, {"gog"}, 2)
check("the most viewed n, in that order", [r["query"] for r in res], ["reddit", "gog"])
check("columns are open-evalsets' columns", sorted(res[0]), sorted(hb.COLS))
check("category, set and expected", (res[0]["category"], res[0]["set"], res[0]["expected"]), ("site", "head-brand", "reddit.com"))
check("a name that is also another article's title is marked shared", [r["stratum"].split(",")[2] for r in res], ["name:only", "name:shared"])
check("the template is in the stratum", [r["stratum"].split(",")[1] for r in res], ["template:title", "template:label"])
check("the entity is the normalised title", [r["entity"] for r in res], ["reddit", "gogcom"])
big = hb.build([(f"T{i}", f"t{i}.com", f"t{i}", "title", 0) for i in range(350)], {f"T{i}": 1000 - i for i in range(350)}, set(), 350)
check("view-rank bands", (big[99]["stratum"].split(",")[0], big[100]["stratum"].split(",")[0], big[300]["stratum"].split(",")[0]),
      ("views-rank:1-100", "views-rank:101-300", "views-rank:301+"))

# --- the same split and the same exclusion list as their sources
oe_src = open(T + "/open-evalsets.py", encoding="utf-8").read()
ns = {"hashlib": __import__("hashlib"), "SEED": re.search(r'^SEED = "(\w+)"', oe_src, re.M).group(1)}
exec(re.search(r"^def h\(.*?\n\n\n", oe_src, re.S | re.M).group(0) + re.search(r"^def split_of\(.*?\n\n\n", oe_src, re.S | re.M).group(0), ns)
names = ["reddit", "gogcom", "wikipedia", "barackobama", "chesscom", "telegram", "a", "zzz9"]
check("split_of is open-evalsets' split_of", [hb.split_of(n) for n in names], [ns["split_of"](n) for n in names])
check("both halves occur", sorted({hb.split_of(f"e{i}") for i in range(40)}), ["dev", "test"])
check("the type lists are read from the importer: 37 organisation types, 8 excluded kinds", (len(hb.ORG_TYPES), len(hb.NOT_SITE_TYPES)), (37, 8))
check("business and human are where they belong", ("Q4830453" in hb.ORG_TYPES, "Q5" in hb.NOT_SITE_TYPES, "Q5" in hb.ORG_TYPES), (True, True, False))
check("the candidate query walks subclass-of for both lists", "wdt:P31/wdt:P279* ?u" in hb.CANDIDATE_QUERY and "wdt:P31/wdt:P279* ?n" in hb.CANDIDATE_QUERY and "wd:Q5 " in hb.CANDIDATE_QUERY and "wd:Q4830453 " in hb.CANDIDATE_QUERY, True)
print("END")
PYEOF
)
while IFS= read -r line; do
    case "$line" in
        OK\ *)  ok "${line#OK }" ;;
        BAD\ *) bad "${line#BAD }" ;;
    esac
done <<< "$out"
grep -q -E '^(OK|BAD) ' <<< "$out" || bad "python produced no verdict" "$out"
# a check that raises ends the python early: fewer lines, no BAD. That must not read as a pass.
[[ "$(tail -1 <<< "$out")" == "END" ]] || bad "the checks stopped early" "$(tail -4 <<< "$out")"

echo ""
echo "Total: $((PASS+FAIL))  PASS: ${PASS}  FAIL: ${FAIL}"
[[ $FAIL -eq 0 ]]
