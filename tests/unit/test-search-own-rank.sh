#!/usr/bin/env bash
################################################################################
# Unit tests for our own site-popularity rank (G3 D2):
#   apps/search/importer/own_rank.py   the recipe (universe, four ranked lists, reciprocal-rank fusion)
#   release/release_check.py           the release carries site.own_rank and refuses anything Tranco-derived
#   release/release_v1.py              signals source rules + the build-database gate
#   serve/api/lookups_release.py       the reader answers "tranco" from site.own_rank, old files from tranco_rank
#   serve/api/search_api.py            SEARCH_SITE_RANK=own rewrites the SQL; the default does not
# Needs python with numpy (the Signals reader); the recipe tests need only the standard library.
# Usage: bash tests/unit/test-search-own-rank.sh      (PY=<python with numpy>)
################################################################################
set -uo pipefail
SCRIPT_DIR_T="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR_T/../.." && pwd)"
IMP="$REPO_ROOT/apps/search/importer"
REL="$REPO_ROOT/apps/search/release"
API="$REPO_ROOT/apps/search/serve/api"
PY="${PY:-python3}"
export PYTHONDONTWRITEBYTECODE=1
PASS=0; FAIL=0
RED=$'\033[0;31m'; GREEN=$'\033[0;32m'; NC=$'\033[0m'
ok()  { echo "  ${GREEN}PASS${NC} $1"; PASS=$((PASS+1)); }
bad() { echo "  ${RED}FAIL${NC} $1"; shift; [[ $# -gt 0 ]] && printf '       %s\n' "$@"; FAIL=$((FAIL+1)); }
expect() { if grep -qxF -- "$1" <<<"$out"; then ok "$2"; else bad "$2" "want: $1" "got:  $(head -c 1200 <<<"$out")"; fi; }

echo ""; echo "=== fetch-cc: the head, and asked names at any depth ==="
out=$(IMP="$IMP" "$PY" - <<'PY' 2>&1
import os, sys, tempfile
sys.path.insert(0, os.environ["IMP"])
import own_rank as o
d = tempfile.mkdtemp()
LINES = {"domain": ["#harmonicc_pos\tharmonicc_val\tpr_pos\tpr_val\thost_rev\tn_hosts", "1\t9\t1\t9\tcom.a\t1", "2\t8\t2\t8\tcom.b\t1", "3\t7\t3\t7\tcom.c\t1", "short\tline", "4\t6\t4\t6\tcom.deep\t1", "5\t5\t5\t5\tcom.e\t1"],
         "host": ["1\t9\t1\t9\tcom.a.www", "2\t8\t2\t8\tru.gov.tomsk", "3\t7\t3\t7\tcom.c.x", "4\t6\t4\t6\tgov.uscourts.nysd"]}
read = {"domain": 0, "host": 0}
def stream(url):
    kind = "domain" if "-domain-ranks" in url else "host"
    for ln in LINES[kind]:
        read[kind] += 1
        yield ln
o._stream_gz = stream
P = lambda n: [tuple(l.split("\t")) for l in open(os.path.join(d, n)).read().split("\n") if l]
o.fetch_cc("R", d + "/dom", d + "/host", domain_top=2, host_top=1)
print("head", P("dom"), P("host"))
print("stopped early", read["domain"] < len(LINES["domain"]), read["host"] < len(LINES["host"]))
read.update(domain=0, host=0)
o.fetch_cc("R", d + "/dom", d + "/host", domain_top=2, host_top=1, keep_domains=frozenset({"deep.com", "absent.com", "a.com"}), keep_hosts=frozenset({"nysd.uscourts.gov"}))
print("kept", P("dom"), P("host"))
print("read whole", read["domain"] == len(LINES["domain"]), read["host"] == len(LINES["host"]))
# a failed download leaves the earlier file as it was
open(d + "/dom", "w").write("keep.com\t1\n")
def broken(url):
    yield "1\t9\t1\t9\tcom.a\t1"
    raise OSError("connection lost")
o._stream_gz = broken
try:
    o.fetch_cc("R", d + "/dom", d + "/host", domain_top=5, host_top=5)
except OSError:
    pass
print("failed fetch", P("dom"))
o._stream_gz = stream
import urllib.error, io
calls, slept = [], []
class Resp(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *a): pass
def fake_open(req, timeout=0):
    calls.append(1)
    if len(calls) < 3:
        raise urllib.error.HTTPError(req.full_url, 503, "Slow Down", {"Retry-After": "7"} if len(calls) == 1 else {}, None)
    return Resp(b"ok")
real = o.urllib.request.urlopen; o.urllib.request.urlopen = fake_open
print("retried", o._open_url("https://h/x", sleep=slept.append).read(), len(calls), slept)
def always(req, timeout=0): raise urllib.error.HTTPError(req.full_url, 503, "Slow Down", {}, None)
o.urllib.request.urlopen = always; slept.clear()
try:
    o._open_url("https://h/x", sleep=slept.append); print("gave up NOT")
except urllib.error.HTTPError:
    print("gave up after", len(slept), "waits")
def gone(req, timeout=0): raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, None)
o.urllib.request.urlopen = gone; slept.clear()
try:
    o._open_url("https://h/x", sleep=slept.append); print("404 NOT raised")
except urllib.error.HTTPError:
    print("404 raised at once", slept)
o.urllib.request.urlopen = real
open(d + "/served.tsv", "w").write("served.com\n\n")
open(d + "/wiki_sites.tsv", "w").write("1\twiki.com\tinfobox company\thttps://wiki.com/\n2\tgov.ru\tinfobox government\thttps://www.tomsk.gov.ru/\n3\tdeeplink.com\tinfobox company\thttps://deeplink.com/a/b\n")
doms, hosts = o.wanted_names(d)
print("wanted", sorted(doms), sorted(hosts))
PY
)
expect "head [('a.com', '1'), ('b.com', '2')] [('www.a.com', '1')]" "without names to keep: the head of each file, names un-reversed, comment and short lines skipped"
expect "stopped early True True" "...and the download stops at the head"
expect "kept [('a.com', '1'), ('b.com', '2'), ('deep.com', '4')] [('www.a.com', '1'), ('nysd.uscourts.gov', '4')]" "an asked name below the head is kept with its real position; others below it are not; a head name is not written twice"
expect "read whole True True" "...and for that the whole file is read"
expect "failed fetch [('keep.com', '1')]" "a download that fails leaves the earlier rank file as it was (rc5 step 0: a 503 left an empty file)"
expect "retried b'ok' 3 [7, 60]" "503 is waited out and tried again: Retry-After when given, else the next wait"
expect "gave up after 5 waits" "...five times, then the error"
expect "404 raised at once []" "another error is not retried"
expect "wanted ['deeplink.com', 'gov.ru', 'served.com', 'wiki.com'] ['tomsk.gov.ru', 'www.tomsk.gov.ru']" "the names asked for: every served and official domain, and every official host under a domain (with and without www.)"

echo ""; echo "=== the recipe: lists and fusion ==="
out=$(IMP="$IMP" "$PY" - <<'PY' 2>&1
import os, random, sys
sys.path.insert(0, os.environ["IMP"])
import own_rank as o
U = lambda *hs: [(h, h) for h in hs]
# 1. direction: the best Common Crawl position is rank 1
r = o.fuse(U("a.com", "b.com", "c.com"), {"a.com": 50, "b.com": 7, "c.com": 900}, {}, {}, {})
print("direction", [(x[0], x[2]) for x in r])
# 2. no signal at all: not ranked (0 / NULL downstream)
r = o.fuse(U("a.com", "none.com"), {"a.com": 5}, {}, {}, {})
print("no signal absent", [x[0] for x in r])
# 3. a site in two lists beats a better CC position that is in one list only
r = o.fuse(U("both.com", "cc.com", "x.com"), {"cc.com": 1, "both.com": 2, "x.com": 3}, {}, {"both.com": 50}, {})
print("two lists beat one", [x[0] for x in r][:2])
# 4. K is the textbook 60: P (cc rank 1, pageviews rank 50) loses to Q (cc rank 3, pageviews rank 5) with K = 60
#    and would win with K = 1
hosts = [f"h{i:02d}.com" for i in range(1, 61)]
cc = {h: i + 1 for i, h in enumerate(hosts)}
P, Q = "h01.com", "h03.com"
fill = [h for h in hosts if h not in (P, Q)]
seq = fill[:4] + [Q] + fill[4:48] + [P] + fill[48:]
pv = {h: 10000 - i for i, h in enumerate(seq)}
ranks = o.competition_ranks(pv)
order60 = [x[0] for x in o.fuse(U(*hosts), cc, pv, {}, {}, k=60)]
order1 = [x[0] for x in o.fuse(U(*hosts), cc, pv, {}, {}, k=1)]
print("K constant", o.K_RRF)
print("K matters", order60.index(Q) < order60.index(P) and order1.index(P) < order1.index(Q), ranks[Q] == 5 and ranks[P] == 50)
# 5. a subdomain never takes the wiki / inlink signal of its site
r = o.fuse([("a.com", "a.com"), ("m.a.com", "a.com")], {"a.com": 5, "m.a.com": 6}, {"a.com": 1000, "m.a.com": 900}, {"a.com": 9, "m.a.com": 8}, {"a.com": 40, "m.a.com": 30})
print("subdomain gets no site signal", [(x[0], x[3]) for x in r])
# 5b. pageviews of a named article do not rank a domain the web graph has never seen
r = o.fuse(U("fan.com", "real.com"), {"real.com": 90}, {"fan.com": 10 ** 6}, {"fan.com": 200}, {"fan.com": 500})
print("no graph position, no rank", [x[0] for x in r])
# 6. ties share the best rank of their group
print("ties", sorted(o.competition_ranks({"a": 5, "b": 5, "c": 3}).items()))
uni = [(f"s{i}.org", f"s{i}.org") for i in range(200)]
cc = {h: random.Random(i).randrange(1, 5000) for i, (h, _d) in enumerate(uni)}
pv = {h: i % 7 for i, (h, _d) in enumerate(uni)}
a = o.fuse(uni, cc, pv, {}, {})
# 7b. hosts with the same score are ordered by host, whatever order they came in
tied = [(f"t{i:02d}.net", f"t{i:02d}.net") for i in range(40)]
same = {h: 7 for h, _d in tied}
x = [r[0] for r in o.fuse(tied, same, {}, {}, {})]
random.Random(2).shuffle(tied)
y = [r[0] for r in o.fuse(tied, same, {}, {}, {})]
print("ties by host", x == y == sorted(x), len(x))
# 8. dense 1..N, N = hosts with a signal
print("dense", [x[2] for x in a] == list(range(1, len(a) + 1)), len(a) == 200)
PY
)
expect "direction [('b.com', 1), ('a.com', 2), ('c.com', 3)]" "the best Common Crawl position is own_rank 1 (same direction as Tranco)"
expect "no signal absent ['a.com']" "a host with no signal at all gets no rank"
expect "two lists beat one ['both.com', 'cc.com']" "a site in two lists outranks a better position in one list"
expect "K constant 60" "the fusion constant is the textbook K = 60"
expect "K matters True True" "K = 60 (not 1) decides cc 1 + pageviews 50 against cc 3 + pageviews 5"
expect "subdomain gets no site signal [('a.com', 'cc,pv,sl,in'), ('m.a.com', 'cc')]" "a subdomain takes neither the pageviews, sitelinks nor inlinks of its site"
expect "no graph position, no rank ['real.com']" "a domain with no Common Crawl position gets no rank from article pageviews, sitelinks or inlinks alone"
expect "ties [('a', 1), ('b', 1), ('c', 3)]" "equal values share the best rank of their group"
expect "ties by host True 40" "hosts with equal scores are ordered by host, whatever order they came in"
expect "dense True True" "own_rank is dense 1..N over the ranked hosts"

echo ""; echo "=== the recipe: universe from files ==="
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
out=$(IMP="$IMP" T="$tmp" "$PY" - <<'PY' 2>&1
import os, sys
sys.path.insert(0, os.environ["IMP"])
import own_rank as o
d = os.environ["T"]
W = lambda n, rows: open(f"{d}/{n}", "w").write("".join("\t".join(map(str, r)) + "\n" for r in rows))
reg = lambda h: ".".join(h.split(".")[-2:])
W("cc-domains.tsv", [("top.com", 10), ("edge.com", 1_000_000), ("far.com", 1_000_001), ("served.com", 5_000_000), ("wiki.com", 6_000_000)])
W("cc-hosts.tsv", [("shop.top.com", 40), ("www.top.com", 41), ("api.top.com", 42), ("blog.far.com", 43), ("top.com", 44), ("deep.top.com", 1_000_001), ("docs.top.com", 6_000_000), ("www.docs.top.com", 7_000_000), ("elsewhere.org", 100), ("www.wonly.top.com", 8_000_000)])
open(f"{d}/served.tsv", "w").write("served.com\n")
W("wiki_sites.tsv", [(1, "wiki.com", "infobox company", "https://www.wiki.com/"), (14, "top.com", "infobox company", "https://docs.top.com/"),
                     (15, "top.com", "infobox company", "https://nocc.top.com/"), (16, "top.com", "infobox company", "https://top.com/deep/page"),
                     (17, "top.com", "infobox company", "https://elsewhere.org/"), (19, "top.com", "infobox company", "https://wonly.top.com/"),
                     (20, "top.com", "infobox company", "https://top.com/")])
W("wiki_pop.tsv", [(1, 5000, 30)])
W("inlinks.tsv", [("top.com", 77)])
W("tranco.tsv", [("far.com", 1), ("nowhere.com", 2)])           # must never be read
plumb = lambda h, dm: h.startswith("api.")
o._plumbing = plumb                      # compute() resolves ccpages.infra_host (it imports duckdb): the test needs neither
uni, cc = o.build_universe(d, reg, plumbing=plumb)
hosts = sorted(h for h, _ in uni)
print("universe", hosts)
print("official subdomain beyond the host cut", cc.get("docs.top.com"))
print("subdomain host rank", cc.get("shop.top.com"), "bare domain keeps the domain rank", cc.get("top.com"))
rows = o.compute(d, f"{d}/out.tsv", reg)
os.rename(f"{d}/tranco.tsv", f"{d}/elsewhere.tsv")
rows2 = o.compute(d, f"{d}/out2.tsv", reg)
print("tranco file ignored", rows == rows2, not any("nowhere" in h or h == "far.com" for h, *_ in rows))
rows_in = o.compute(d, f"{d}/outin.tsv", reg, None, False, True, False)
print("wiki and inlinks counted", dict((h, b) for h, _d, _r, b in rows_in)["top.com"], dict((h, b) for h, _d, _r, b in rows_in)["wiki.com"])
print("inlinks are off by default", dict((h, b) for h, _d, _r, b in rows)["top.com"])
rk = {h: (r, b) for h, _d, r, b in rows}
print("official host without a graph rank inherits its parent", rk["nocc.top.com"][1], rk["nocc.top.com"][0] == rk["top.com"][0], "docs.top.com keeps the better", rk["docs.top.com"][0] == rk["top.com"][0])
r_no = {h: r for h, _d, r, _b in o.compute(d, f"{d}/outni.tsv", reg, None, False, False, False)}
print("no inheritance", "nocc.top.com" in r_no, r_no["docs.top.com"] > r_no["top.com"])
hs, ps = o.official_hosts(d)
print("official hosts", sorted(hs.items()), sorted(ps))
rows_i = [("uscourts.gov", "uscourts.gov", 100, "cc"), ("gud.uscourts.gov", "uscourts.gov", 5000, "cc"), ("gov.ru", "gov.ru", 50, "cc"),
          ("tomsk.gov.ru", "gov.ru", 9000, "cc"), ("en.big.com", "big.com", 3, "cc"), ("big.com", "big.com", 10, "cc")]
hosts_i = {"gud.uscourts.gov": "uscourts.gov", "tomsk.gov.ru": "gov.ru", "en.big.com": "big.com", "ghost.uscourts.gov": "uscourts.gov", "orphan.x.com": "x.com"}
got = {h: (r, b) for h, _d, r, b in o.inherit_parent_rank(rows_i, hosts_i, {"uscourts.gov", "big.com"})}
print("inherit", got["gud.uscourts.gov"], got["tomsk.gov.ru"], got["en.big.com"], got["ghost.uscourts.gov"], "orphan.x.com" in got)
print("inherit sorted", [r[2] for r in o.inherit_parent_rank(rows_i, hosts_i, {"uscourts.gov", "big.com"})] == sorted(r[2] for r in o.inherit_parent_rank(rows_i, hosts_i, {"uscourts.gov", "big.com"})))
# the inlink list can be left out (D10), and the wide box counts courts, museums and officeholders
rows5 = o.compute(d, f"{d}/out5.tsv", reg, None, False, False)
print("no inlinks", dict((h, b) for h, _d, _r, b in rows5)["top.com"], "in" in {x for r5 in rows5 for x in r5[3].split(",")})
# a blocked domain is never ranked and takes its subdomains with it
open(f"{d}/blocked.tsv", "w").write("wiki.com\thagezi-nsfw\ntop.com\tx\n")
rows3 = o.compute(d, f"{d}/out3.tsv", reg, f"{d}/blocked.tsv")
print("blocked never ranked", sorted(h for h, *_ in rows3))
# the wiki signal of a domain is the MAX over the articles whose official site it is
W("wiki_sites.tsv", [(1, "wiki.com", "infobox company", "https://wiki.com/"), (2, "wiki.com", "wikidata organization", "http://www.wiki.com"),
                     (3, "other.com", "infobox software", "https://other.com/"),
                     (4, "city.com", "infobox settlement", "https://city.com/"), (5, "who.com", "infobox person", "https://who.com/"),
                     (6, "club.com", "infobox football club", "https://club.com/"), (7, "season.com", "infobox football club season", "https://season.com/"),
                     (8, "bare.com", "", "https://bare.com/"),
                     (9, "gov.ru", "infobox government agency", "http://www.tomsk.gov.ru/"),
                     (10, "deep.com", "infobox company", "https://deep.com/people/front/1"),
                     (11, "port.com", "infobox company", "https://port.com:8080/"),
                     (12, "sub.com", "infobox company", "https://shop.sub.com/"),
                     (13, "noargs.com", "infobox company", ""), (18, "top2.com", "infobox company", "https://elsewhere.net/")])
W("wiki_pop.tsv", [(1, 5000, 30), (2, 10, 99), (3, 7, 1), (4, 9999999, 300), (5, 8888888, 250), (6, 40, 3), (7, 900, 9), (8, 800, 8),
                   (9, 7777777, 280), (10, 700, 7), (11, 600, 6), (12, 500, 5), (13, 400, 4), (18, 300, 3)])
print("wiki max", o.wiki_signals(d))
W("wiki_sites.tsv", [(21, "court.com", "infobox court", "https://court.com/"), (22, "mus.com", "infobox museum", "https://mus.com/"),
                     (23, "pol.com", "infobox officeholder", "https://pol.com/"), (24, "town.com", "infobox settlement", "https://town.com/"),
                     (25, "bio.com", "infobox officeholder biography", "https://bio.com/")])
W("wiki_pop.tsv", [(21, 11, 1), (22, 12, 2), (23, 13, 3), (24, 14, 4), (25, 15, 5)])
print("narrow box", sorted(o.wiki_signals(d)[0]), "wide box", sorted(o.wiki_signals(d, o.ORG_BOX_WIDE)[0]))
open(f"{d}/cc-domains.tsv", "a").write("court.com\t5000000\n")
bas = lambda rows_: dict((h, b) for h, _d, _r, b in rows_).get("court.com")
print("compute wide", bas(o.compute(d, f"{d}/out6.tsv", reg, None, True, True)), "narrow", bas(o.compute(d, f"{d}/out7.tsv", reg, None, False, True)))
W("wiki_sites.tsv", [(1, "wiki.com", "infobox company", "https://wiki.com/")])
open(f"{d}/wiki_sites.tsv", "w").write("1\twiki.com\tinfobox company\n")
try:
    o.wiki_signals(d); print("three columns", "read")
except SystemExit:
    print("three columns", "refused")
PY
)
expect "universe ['docs.top.com', 'edge.com', 'served.com', 'shop.top.com', 'top.com', 'wiki.com', 'wonly.top.com']" "universe: graph top domains, served sites, official sites, and non-plumbing subdomains of those within the top host ranks; nothing else"
expect "official subdomain beyond the host cut 6000000" "the front page of a host under a domain joins the universe when the web graph ranks that host, at any position (www. variant too); one with no host rank, a deep link or a host outside the domain does not"
expect "subdomain host rank 40 bare domain keeps the domain rank 10" "a subdomain takes its host rank, a bare domain its domain rank"
expect "tranco file ignored True True" "a Tranco file in the input folder changes nothing and is never read"
expect "inlinks are off by default cc" "the inlink list (build-only site_inlinks, D10) is not fused unless asked for"
expect "official host without a graph rank inherits its parent inh True docs.top.com keeps the better True" "an official host under a domain that is itself an article's official site takes the parent's rank, with or without a graph rank of its own; its own rank when that is better"
expect "no inheritance False True" "without inheritance a host with no graph rank has no rank and one with a worse rank keeps it"
expect "official hosts [('docs.top.com', 'top.com'), ('nocc.top.com', 'top.com'), ('wonly.top.com', 'top.com')] ['top.com', 'wiki.com']" "official_hosts: the front pages under a domain, and the domains that are an article's official front page"
expect "inherit (100, 'cc') (9000, 'cc') (3, 'cc') (100, 'inh') False" "a host takes its parent's rank when the parent is somebody's site (uscourts.gov), keeps its own when better (en.big.com) or when the parent is nobody's (gov.ru: tomsk.gov.ru); an unranked parent gives nothing"
expect "inherit sorted True" "rows stay ordered by rank"
expect "wiki and inlinks counted cc,in cc,pv,sl" "the inlink list reaches the domain, pageviews and sitelinks the official site"
expect "no inlinks cc False" "compute with the inlink list left out ranks a host without it (D10)"
expect "compute wide cc,pv,sl narrow cc" "compute counts a court's article with the wide box and not with the narrow one"
expect "narrow box [] wide box ['court.com', 'mus.com', 'pol.com']" "the wide box counts court, museum and officeholder articles, not a settlement or a biography; the narrow one none of them"
expect "blocked never ranked ['edge.com', 'served.com']" "a blocked domain is never ranked, and its subdomains go with it"
expect "wiki max ({'wiki.com': 5000, 'other.com': 7, 'club.com': 40, 'tomsk.gov.ru': 7777777, 'shop.sub.com': 500}, {'wiki.com': 99, 'other.com': 1, 'club.com': 3, 'tomsk.gov.ru': 280, 'shop.sub.com': 5})" "only an organisation article whose official site is a front page lifts that site (not a place, a person, a season, an untyped article, a deep link, a port): a front page of a host under a domain lifts the HOST (tomsk.gov.ru, never gov.ru); a site takes the maxima over its articles"
expect "three columns refused" "a wiki_sites.tsv without the official url is refused (an old export must not lift a shared domain)"

echo ""; echo "=== the release: files in, fields out ==="
if ! "$PY" -c "import numpy" 2>/dev/null; then
    echo "  SKIP: $PY cannot import numpy"
else
out=$(REL="$REL" API="$API" TOOL="$REPO_ROOT/apps/search/tools/sqlite_lookups" IMP="$IMP" T="$tmp" "$PY" - <<'PY' 2>&1
import hashlib, json, os, sqlite3, struct, sys, types
sys.path.insert(0, os.environ["REL"])
import release_check as rc
import numpy as np
print("required", sorted(rc.REQUIRED_SIGNALS) == ["site.cc_hc_pos", "wiki.sitelinks"], rc.RANK_SIGNALS)
sys.modules.setdefault("psycopg", types.ModuleType("psycopg"))
sys.modules["psycopg"].Error = Exception
import release_v1 as rv
t = os.environ["T"]

# the SQL of the release names no Tranco-derived table or column
sqls = [q for _db, q in rv.SIGNAL_SQL.values()] + rv.REGISTRY_SQL
print("no Tranco SQL", not any(w in q.lower() for q in sqls for w in ("tranco", "nav_sites")))
ow = {"a.com": 1, "tv.a.com": 2}
print("own rows", rv.own_rows({"b.org": 5, "a.com": 1, "c.net": 9}))
ow["gov.ru"] = 7; ow["tomsk.gov.ru"] = 9
print("wiki site rows", list(rv.wiki_site_rows([(1, "a.com", "https://a.com/"), (2, "none.com", "https://none.com/"), (3, None, None),
                                                (4, "gov.ru", "http://www.tomsk.gov.ru/"), (5, "a.com", "https://a.com/deep"),
                                                (6, "gov.ru", "https://www.gov.ru/")], ow)))
print("link rows", list(rv.link_rows(["https://tv.a.com/x", "https://x.org/", "mailto:odd"], ow)))

# read_own_rank: a rank file is never half-read
def own_file(name, lines):
    open(f"{t}/{name}", "w").write("".join(lines))
    return f"{t}/{name}"
def refused(path):
    try:
        rv.read_own_rank(path); return "read"
    except SystemExit:
        return "refused"
print("own file ok", rv.read_own_rank(own_file("o1.tsv", ["a.com\ta.com\t1\t5\tcc\n", "b.com\tb.com\t2\t\\N\tcc,pv\n"])))
print("own file: bad rank", refused(own_file("o2.tsv", ["a.com\ta.com\t0\t5\tcc\n"])))
print("own file: short line", refused(own_file("o3.tsv", ["a.com\ta.com\t1\n"])))
print("own file: repeated host", refused(own_file("o4.tsv", ["a.com\ta.com\t1\t5\tcc\n", "a.com\ta.com\t2\t5\tcc\n"])))
print("own file: empty", refused(own_file("o5.tsv", [])))
# signals and the Postgres registry refuse to run without it
for name, call in (("signals", lambda: rv.cmd_signals("r.sqlite", f"{t}/o.bin", "x")),
                   ("registry from Postgres", lambda: rv.PgSource("host=x"))):
    try:
        call(); print(name, "ran")
    except SystemExit as e:
        print(name, "needs the file", "own" in str(e).lower() or "--own-rank" in str(e))
print("url_host", rv.url_host("https://Tv.YouTube.com:443/x?y"), rv.url_host("http://a.b/"))

# the suggestion weights of site and service items come from OUR rank (suggest.sql's rules, no Tranco)
r = lambda x: round(x, 4)
print("rank_pop", r(rank_pop := rv.rank_pop(1)), r(rv.rank_pop(1000)), rv.rank_pop(100001), rv.rank_pop(150000), rv.rank_pop(None), rv.rank_pop(0))
print("article_pop", r(rv.article_pop(250, None)), r(rv.article_pop(1000, 500000)), r(rv.article_pop(1, 0)), r(rv.article_pop(250, 0)))
print("demand_curve", round(rv.demand_curve(1)), round(rv.demand_curve(1000)), rv.demand_curve(100001), rv.demand_curve(None))
print("org site", tuple(r(x) for x in rv.site_weights(1000, True, 0.9)))
print("plain site", tuple(r(x) for x in rv.site_weights(1000, False, 0.9)))
print("org site, no rank", tuple(r(x) for x in rv.site_weights(None, True, 0.9)), tuple(r(x) for x in rv.site_weights(None, False, 0.5)))
print("service", tuple(r(x) for x in rv.service_weights(1000)), tuple(r(x) for x in rv.service_weights(None)))
rows = [(1, "site", "Costco", "costco.com", "https://costco.com/", 0.1, 0.2, 0.3), (2, "site", None, None, "https://tv.a.com/x", 0.1, 0.2, 0.3),
        (3, "service", None, None, "https://tv.a.com/x", 0.1, 0.2, 0.3), (4, "youtube", None, None, "https://yt/x", 0.5, 0.6, 0.7),
        (5, "wiki", "Costco", None, "https://en.wikipedia.org/wiki/Costco", 0.4, 0.5, 0.6), (6, "site", "Gov", "gov.br", "https://gov.br/", 0.9, 0.9, 0.9)]
own2 = {"costco.com": 1000, "tv.a.com": 1000}
out = list(rv.suggest_rows(rows, own2, {"costco.com"}, lambda t: 0.9 if t in ("Costco", "Gov") else 0.0))
print("site org", tuple(r(x) for x in out[0][5:]) == tuple(r(x) for x in rv.site_weights(1000, True, 0.9)))
print("site by url host", tuple(r(x) for x in out[1][5:]) == tuple(r(x) for x in rv.site_weights(1000, False, 0.0)))
print("service", tuple(r(x) for x in out[2][5:]) == tuple(r(x) for x in rv.service_weights(1000)))
print("others untouched", out[3] == rows[3], out[4] == rows[4])
print("unranked non-org site keeps a tenth of its article's fame (gov.br), no demand", tuple(r(x) for x in out[5][5:]) == (0.09, 0.0, 0.0))
print("ids and keys kept", [(o[0], o[1], o[2], o[3], o[4]) for o in out] == [(x[0], x[1], x[2], x[3], x[4]) for x in rows])
# the inlink list needs the owner's OK (D10): a rank file fused with it is refused unless --allow-inlinks
with_in = own_file("in1.tsv", ["a.com\ta.com\t1\t5\tcc,in\n"])
no_in = own_file("in2.tsv", ["a.com\ta.com\t1\t5\tcc,pv,sl\n", "b.com\tb.com\t2\t5\tcc\n"])
print("uses inlinks", rv.own_rank_uses_inlinks(with_in), rv.own_rank_uses_inlinks(no_in))
def sig(*flags):
    try:
        rv.cmd_signals("r.sqlite", f"{t}/o.bin", "x", *flags); return "ran"
    except SystemExit as e:
        return "D10" if "D10" in str(e) else "other exit"
    except Exception:
        return "past the guard"
print("signals with inlinks", sig("--own-rank", with_in), sig("--own-rank", with_in, "--allow-inlinks"), sig("--own-rank", no_in))

# --from-dump: the builder's Postgres reads can come from COPY-text TSVs
os.makedirs(f"{t}/dump", exist_ok=True)
open(f"{t}/dump/items.tsv", "w").write("7\tsite\tCostco\tcostco.com\thttps://costco.com/\t0.5\t1.5\t200\n8\tservice\t\\N\t\\N\thttps://tv.a.com/\t0.1\t\\N\t3\n"
                                       "9\twiki\ta\\tb\\\\c\t\\N\thttps://x/\t1\t2\t3\n")
open(f"{t}/dump/bad.tsv", "w").write("1\t2\n")
got = list(rv.read_dump(f"{t}/dump/items.tsv", rv.DUMP_TYPES["items"]))
print("dump rows", got[0], got[1][2:5], got[1][5:])
print("dump escapes", repr(got[2][2]))
try:
    list(rv.read_dump(f"{t}/dump/bad.tsv", "iii")); print("dump columns", "read")
except SystemExit:
    print("dump columns", "refused")
rv.SIGNAL_DUMP = f"{t}/dump"
print("feed reads the dump", [r[0] for r in rv.feed("items")])
rv.SIGNAL_DUMP = None
print("dump types cover every source", sorted(rv.DUMP_TYPES) == sorted(rv.SIGNAL_SQL), all(len(rv.DUMP_TYPES[k]) > 0 for k in rv.DUMP_TYPES))

def signals(path, names):
    fields, arrays, off = [], [], 0
    for n, vals, lic, note in names:
        dt = "<u2" if n.endswith("sitelinks") else "<u4"
        a = np.array(vals, dtype=dt)
        fields.append({"name": n, "type": n.split(".")[0], "dtype": dt, "count": len(a), "offset": off, "source": "x", "licence": lic, "note": note})
        arrays.append(a); off += a.nbytes
    head = json.dumps({"format": 1, "built_release": "t", "type_shift": 28, "fields": fields}).encode()
    with open(path, "wb") as f:
        f.write(b"LOSIGNL1" + struct.pack("<I", len(head)) + head)
        for a in arrays:
            f.write(a.tobytes())

# the reader (lookups_release.Signals): the SITE doc type is 2, doc_id = 2 << 28 | seq
sys.path.insert(0, os.environ["API"])
for m in ("tldextract", "mwparserfromhell"):
    sys.modules.setdefault(m, types.ModuleType(m))
import lookups_release as lr
D = (2 << 28) | 1
for sub, names in (("own", [("site.own_rank", [0, 11], "CC0", ""), ("site.tranco_rank", [0, 99], "CC0", "")]),
                   ("old", [("site.tranco_rank", [0, 99], "CC0", "")]),
                   ("none", [("site.own_rank", [0, 0], "CC0", "")])):
    os.makedirs(f"{t}/{sub}", exist_ok=True)
    signals(f"{t}/{sub}/signals.bin", names)
print("reader: own_rank wins", lr.Signals(f"{t}/own").get(D, "tranco"))
print("reader: old file falls back", lr.Signals(f"{t}/old").get(D, "tranco"))
print("reader: 0 is none", lr.Signals(f"{t}/none").get(D, "tranco"))

# release_check: both kinds of release apply; --tranco-free (the publish gate) refuses anything Tranco-derived
def release(path, names):
    os.makedirs(path, exist_ok=True)
    signals(f"{path}/signals.bin", names)
    body = open(f"{path}/signals.bin", "rb").read()
    json.dump({"format": 1, "files": [{"path": "signals.bin", "bytes": len(body), "sha256": hashlib.sha256(body).hexdigest()}]}, open(f"{path}/manifest.json", "w"))
base = [("wiki.sitelinks", [0], "CC0", ""), ("site.cc_hc_pos", [0], "CC0", "")]
own = ("site.own_rank", [0], "CC0", "")
release(f"{t}/r-own", base + [own])
release(f"{t}/r-old", base + [("site.tranco_rank", [0], "Tranco x", "")])
release(f"{t}/r-none", base)
release(f"{t}/r-both", base + [own, ("site.tranco_rank", [0], "CC0", "")])
release(f"{t}/r-lic", base + [("site.own_rank", [0], "Tranco — G3 D2 OPEN", "")])
release(f"{t}/r-note", base + [own, ("site.suggest_pop", [0], "M8 layer (CC0)", "Tranco-derived")])
E = lambda name, **kw: rc.check_release(f"{t}/{name}", **kw)
print("check: own rank applies", E("r-own"), E("r-own", tranco_free=True))
print("check: a release in the field still applies", E("r-old"))
print("check: no rank at all is refused", [e for e in E("r-none") if "no site rank" in e] != [])
print("check: gate refuses tranco_rank", [e for e in E("r-old", tranco_free=True)] != [])
print("check: gate refuses a Tranco field next to ours", any("is Tranco-derived" in e for e in E("r-both", tranco_free=True)), E("r-both"))
print("check: gate refuses a Tranco licence", any("is Tranco-derived" in e for e in E("r-lic", tranco_free=True)))
print("check: gate refuses the suggest note", any("site.suggest_pop is Tranco-derived" in e for e in E("r-note", tranco_free=True)), E("r-note"))

# the lookup builder's stage: s_own, s_nav and every rank column from the file; nothing Tranco-derived is read
sys.path.insert(0, os.environ["TOOL"])
import build as b
st = sqlite3.connect(":memory:")
for tb, (cols, sql) in b.STAGE.items():
    st.execute(f"CREATE TABLE {tb} ({cols})")
print("stage: every Postgres select is guarded", all(b.guard_sql(sql) is not None for _c, sql in b.STAGE.values() if sql))
print("stage: no Tranco select", not any(w in sql.lower() for _c, sql in b.STAGE.values() if sql for w in ("tranco", "nav_sites", "own_rank")))
st.executemany("INSERT INTO s_serve VALUES (?,?,?)", [("a.com", None, 3), ("z.org", None, 4)])
st.executemany("INSERT INTO s_wsites VALUES (?,?,?,?,?)", [(1, "https://a.com/", "a.com", "box", None), (2, "https://z.org/", "z.org", None, None),
                                                          (3, "http://www.tomsk.gov.ru/", "gov.ru", None, None), (4, "https://a.com/deep", "a.com", None, None)])
st.executemany("INSERT INTO s_hostnav VALUES (?,?,?,?,?)", [("youtubetv", "https://tv.a.com/x", "TV", "d", None), ("old", "https://old.z.org/", "O", "d", None)])
own_f = own_file("own.tsv", ["a.com\ta.com\t1\t3\tcc\n", "tv.a.com\ta.com\t2\t9\tcc\n", "m.a.com\ta.com\t3\t\\N\tcc\n", "gov.ru\tgov.ru\t4\t8\tcc\n", "tomsk.gov.ru\tgov.ru\t5\t8\tcc\n"])
b.stage_own(st, own_f)
print("stage: s_own", st.execute("SELECT host, rank, cc_pos FROM s_own ORDER BY rank").fetchall())
print("stage: s_nav keys", sorted(r[:2] for r in st.execute("SELECT host, key FROM s_nav WHERE host IN ('a.com', 'tv.a.com')")))
print("stage: ranks come from the file", st.execute("SELECT own_rank FROM s_serve ORDER BY domain").fetchall(), st.execute("SELECT own_rank FROM s_wsites ORDER BY page_id").fetchall())
print("stage: host_nav keeps only own hosts", st.execute("SELECT url, rank FROM s_hostnav").fetchall())
sys.path.insert(0, os.environ["IMP"])
import own_rank as o
print("nav_keys", o.nav_keys("costco.com", "costco.com"), o.nav_keys("tv.youtube.com", "youtube.com"), o.nav_keys("a.b.x.co.uk", "x.co.uk"))
PY
)
expect "required True ('site.own_rank', 'site.tranco_rank')" "REQUIRED_SIGNALS no longer asks for a Tranco field; a site rank of either kind is required"
expect "no Tranco SQL True" "no SQL of the registry or of signals.bin selects a Tranco-derived table or column"
expect "own rows [('a.com', 1), ('b.org', 5), ('c.net', 9)]" "site.own_rank and site.nav_rank carry the file's rank of every host, in host order"
expect "wiki site rows [(1, 1), (2, 0), (3, 0), (4, 9), (5, 0), (6, 7)]" "wiki.site_rank is the file's rank of the official site: the domain for its front page, the host for a host under a shared domain (tomsk.gov.ru gets its own, not gov.ru's); 0 for none or a deep link"
expect "link rows [('https://tv.a.com/x', 2), ('https://x.org/', 0), ('mailto:odd', 0)]" "link.nav_rank is the file's rank of the url's host (0 for none)"
expect "rank_pop 1.0 0.4 0.0 0.0 0.0 0.0" "rank_pop is suggest.sql's: 1 at rank 1, 0 at 100,001 and beyond or with no rank"
expect "article_pop 1.0 1.0 0.0 0.5" "article_pop is suggest.sql's: sitelinks alone without pageviews, half and half with them"
expect "demand_curve 10000000 19953 0.0 0.0" "demand_curve is suggest.sql's fallback 1e7 / r^0.9, none beyond rank 100k"
expect "org site (0.9, 4.3, 19952.6231)" "an organisation's site takes the better of its rank and its article's fame; score is log10(1 + demand)"
expect "plain site (0.49, 4.3, 19952.6231)" "any other site keeps its rank plus a tenth of its article's fame (gov.br must not take Brazil's)"
expect "org site, no rank (0.9, 0.0, 0.0) (0.05, 0.0, 0.0)" "a site with no rank has only its article's fame, and no demand"
expect "service (0.4, 3.8229, 6650.8744) (0.0, 0.0, 0.0)" "a service takes the site formula on its host rank, demand at a third"
expect "site org True" "a site item's weights are site_weights of its domain's rank, with the organisation lift when its domain is an organisation's"
expect "site by url host True" "a domainless site item is ranked by the host of its url"
expect "service True" "a service item's weights are service_weights of its host's rank"
expect "others untouched True True" "youtube and wiki items keep the weights the build database gave them"
expect "unranked non-org site keeps a tenth of its article's fame (gov.br), no demand True" "a site that has no own rank keeps nothing of the Tranco-fitted weights; a non-organisation takes only a tenth of its article's fame"
expect "ids and keys kept True" "only the three weights change"
expect "uses inlinks True False" "a rank file that was fused with the inlink list is recognised by its lists column"
expect "signals with inlinks D10 past the guard past the guard" "signals refuses a rank fused with the inlink counts (G3 D10) unless --allow-inlinks; a rank without them passes the guard"
expect "dump rows (7, 'site', 'Costco', 'costco.com', 'https://costco.com/', 0.5, 1.5, 200.0) (None, None, 'https://tv.a.com/') (0.1, None, 3.0)" "read_dump types every column (\\N is None, ints stay ints, floats floats)"
expect "dump escapes 'a\\tb\\\\c'" "read_dump undoes COPY text escapes (tab, backslash)"
expect "dump columns refused" "a row with the wrong number of columns refuses the dump"
expect "feed reads the dump [7, 8, 9]" "feed returns the dump's rows when signals runs --from-dump"
expect "dump types cover every source True True" "DUMP_TYPES names a type string for every SIGNAL_SQL statement"
expect "own file ok {'a.com': 1, 'b.com': 2}" "read_own_rank reads host and rank (and a \\N cc position)"
expect "own file: bad rank refused" "a rank below 1 refuses the file"
expect "own file: short line refused" "a short line refuses the file"
expect "own file: repeated host refused" "a repeated host refuses the file"
expect "own file: empty refused" "an empty file is refused"
expect "signals needs the file True" "release_v1 signals refuses to run without --own-rank"
expect "registry from Postgres needs the file True" "the Postgres registry source refuses to run without --own-rank (nav_sites is no longer read)"
expect "url_host tv.youtube.com a.b" "url_host is the host of a url (no port, no query, lowercase)"
expect "reader: own_rank wins 11" "Signals answers the site rank from site.own_rank even when a tranco_rank field is also present"
expect "reader: old file falls back 99" "a release from before the rename still answers from site.tranco_rank"
expect "reader: 0 is none None" "a 0 in site.own_rank is 'no rank'"
expect "check: own rank applies [] []" "check_release passes a release with our own rank, also under --tranco-free"
expect "check: a release in the field still applies []" "the default check still passes a release that carries site.tranco_rank (the updater must apply it)"
expect "check: no rank at all is refused True" "a signals.bin with no site rank of either kind is refused"
expect "check: gate refuses tranco_rank True" "--tranco-free refuses a release with only site.tranco_rank"
expect "check: gate refuses a Tranco field next to ours True []" "--tranco-free refuses a Tranco field even next to site.own_rank (the default accepts it)"
expect "check: gate refuses a Tranco licence True" "--tranco-free refuses a field whose licence says Tranco"
expect "check: gate refuses the suggest note True []" "--tranco-free refuses the suggestion weights tagged Tranco-derived"
expect "stage: every Postgres select is guarded True" "every staging SELECT of the builder still passes the release guards"
expect "stage: no Tranco select True" "the stage reads no Tranco-derived column or table (nav_sites, tranco_rank) and no own_rank table from Postgres"
expect "stage: s_own [('a.com', 1, 3), ('tv.a.com', 2, 9), ('m.a.com', 3, None), ('gov.ru', 4, 8), ('tomsk.gov.ru', 5, 8)]" "stage_own stages the file (a missing cc position is NULL)"
expect "stage: s_nav keys [('a.com', 'a'), ('tv.a.com', 'atv'), ('tv.a.com', 'tva')]" "s_nav keys: a domain its first label, a subdomain the labels run together both ways"
expect "stage: ranks come from the file [(1,), (None,)] [(1,), (None,), (5,), (None,)]" "s_serve and s_wsites take their own_rank from the file (a host under a shared domain gets its own rank, a deep link none)"
expect "stage: host_nav keeps only own hosts [('https://tv.a.com/x', 2)]" "a host_nav row of a host the file lacks is dropped, the others get the file's rank"
expect "nav_keys ['costco'] ['tvyoutube', 'youtubetv'] ['abx', 'xab']" "nav_keys is serve-prep.sql's rule: first label, or sub+label and label+sub"

echo ""; echo "=== search-api: SEARCH_SITE_RANK ==="
out=$(API="$API" "$PY" - <<'PY' 2>&1
import os, subprocess, sys
code = r'''
import os, sys, types
sys.path.insert(0, os.environ["API"])
for m in ("psycopg", "tldextract", "mwparserfromhell"):
    sys.modules.setdefault(m, types.ModuleType(m))
import search_api as s
if os.environ.get("LK"):
    s.lookups = lambda: types.SimpleNamespace(h={s.NAV_SITES_SQL: 1})      # a release backend that owns NAV_SITES_SQL
sql = s.NAV_SQL + s.NAV_SITES_SQL + s.HOST_NAV_SQL + s.WIKI_EXACT_SITE_SQL
out = s.pop_sql(sql)
print("rank", s.SITE_RANK)
print("unchanged" if out == sql else "rewritten")
print("tranco kept" if "s.tranco_rank" in out and "FROM nav_sites " in out else "tranco gone")
print("own tables", "nav_sites_own" in out, "host_nav_own" in out, "own_rank o WHERE o.host = s.domain" in out, "o.host = ws.domain) AS tranco_rank" in out)
print("owned sql untouched", s.pop_sql(s.NAV_SITES_SQL) == s.NAV_SITES_SQL)
'''
for val, extra in ((None, {}), ("tranco", {}), ("own", {"SEARCH_OWN_RANK_TSV": "/x.tsv"}), ("own", {"SEARCH_LOOKUP_BACKEND": "release", "LK": "1"}), ("own", {}), ("bogus", {})):
    env = dict(os.environ)
    for k in ("SEARCH_SITE_RANK", "SEARCH_OWN_RANK_TSV", "SEARCH_LOOKUP_BACKEND", "LK"):
        env.pop(k, None)
    if val:
        env["SEARCH_SITE_RANK"] = val
    env.update(extra)
    r = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)
    print(f"== {val} {sorted(extra)}")
    print((r.stdout if r.returncode == 0 else r.stdout + r.stderr.strip().splitlines()[-1]).strip())
PY
)
sect() { awk -v h="== $1" 'index($0, "== ") == 1 { on = ($0 == h) } on' <<<"$out" | sed 1d; }
if sect "None []" | grep -q "^unchanged" && sect "None []" | grep -q "^rank tranco"; then ok "default is SEARCH_SITE_RANK=tranco and leaves every SQL string untouched"; else bad "default leaves the SQL untouched" "$out"; fi
if sect "tranco []" | grep -q "^unchanged"; then ok "SEARCH_SITE_RANK=tranco leaves the SQL untouched"; else bad "tranco leaves the SQL untouched" "$out"; fi
x=$(sect "own ['SEARCH_OWN_RANK_TSV']")
if grep -q "own tables True True True True" <<<"$x" && grep -q "tranco gone" <<<"$x"; then ok "Postgres engine, own: reads own_rank, nav_sites_own and host_nav_own and no tranco_rank column"; else bad "own rewrites the SQL" "$x"; fi
x=$(sect "own ['LK', 'SEARCH_LOOKUP_BACKEND']")
if grep -q "owned sql untouched True" <<<"$x" && grep -q "own tables True" <<<"$x"; then ok "release lookups, own: SQL a lookup backend answers is left alone, Postgres-run SQL is rewritten"; else bad "lookup-owned SQL must not be rewritten" "$x"; fi
x=$(sect "own []")
if grep -q "needs SEARCH_OWN_RANK_TSV" <<<"$x"; then ok "Postgres engine with SEARCH_SITE_RANK=own and no rank file stops at start"; else bad "own without a file stops the engine" "$x"; fi
x=$(sect "bogus []")
if grep -q "use 'tranco' or 'own'" <<<"$x"; then ok "an unknown SEARCH_SITE_RANK stops the engine at start"; else bad "unknown value refused" "$x"; fi
fi

echo ""; echo "Total: $((PASS+FAIL))  PASS: $PASS  FAIL: $FAIL"; [[ $FAIL -eq 0 ]]
