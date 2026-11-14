#!/usr/bin/env bash
################################################################################
# Unit tests — what a site said about being shown decides what a public release ships (G3 D4c)
################################################################################
# importer/docs_policy.py is the rule; importer/ccfetch.py records a capture's robots statements;
# tools/sqlite_lookups/build.py applies the rule to the staged homepages; release_check.py's
# publish gate refuses a lookup file staged any other way.
#   - a Wayback capture and a noindex capture are not used at all
#   - a description is not shown for nosnippet / max-snippet:0 / a TDM reservation, nor for a
#     capture that was never read for its robots statements (unless the build says it is internal)
#   - otherwise it is cut to 160 characters at a word
# Usage: bash tests/unit/test-search-docs-policy.sh
################################################################################
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
export PYTHONDONTWRITEBYTECODE=1
PASS=0; FAIL=0
ok()  { echo "  PASS $1"; PASS=$((PASS+1)); }
bad() { echo "  FAIL $1"; shift; [[ $# -gt 0 ]] && printf '       %s\n' "$@"; FAIL=$((FAIL+1)); }
expect() { if grep -qxF -- "$1" <<<"$out"; then ok "$2"; else bad "$2" "want: $1" "got:  $(head -c 1500 <<<"$out")"; fi; }

echo ""; echo "=== the rule (docs_policy.py) ==="
out=$(python3 - "$REPO/apps/search/release" "$REPO/apps/search/importer" <<'PY' 2>&1
import sys
sys.path[:0] = sys.argv[1:3]
import docs_policy as dp, release_check as rc
F = dp.robots_field
print("field", [F(["NOINDEX, follow"]), F(["index,follow"]), F([]), F(["googlebot: nosnippet", "max-snippet:50, max-image-preview:large"]),
                F(["none"]), F(["max-snippet:-1"]), F(["unavailable_after: 25 Jun 2030 15:00:00 PST"]), F(["tdm-reservation"]), F(["noai, noimageai"]), F(["bingbot: max-snippet:0", "googlebot:noindex"]), F(["max-image-preview:large", "max-snippet: 20"])])
print("capture", [dp.capture_ok("CC-MAIN-2026-30", ""), dp.capture_ok("CC-MAIN-2026-30", None), dp.capture_ok("wayback-20260101", ""),
                  dp.capture_ok("CC-MAIN-2026-30", "noarchive,noindex"), dp.capture_ok("CC-MAIN-2026-30", "none"), dp.capture_ok("CC-MAIN-2026-30", "nosnippet")])
long = "Shop the widest range of handmade oak furniture, delivered free across the country, with a ten year guarantee on every table, chair, bed and bookcase we make in our own workshop."
S = lambda robots, crawl="CC-MAIN-2026-30", text=long, **k: dp.shown_description(text, crawl, robots, **k)
cut = S("")
print("cut", len(long) > 160, len(cut) <= 160, cut.endswith("…"), long.startswith(cut[:-1]), not cut[:-1].endswith((" ", ",")), cut[:-1].rsplit(" ", 1)[-1] in long.split())
print("short kept whole", S("", text="Oak furniture, made by hand."), "| spaces", S("", text="  Oak   furniture \n made by hand. "))
print("said no", [S("nosnippet"), S("max-snippet:0"), S("tdm-reservation"), S("noindex"), S("none"), S("", crawl="wayback-20260101")])
print("max-snippet", len(S("max-snippet:50")) <= 50, len(S("max-snippet:400")) <= 160, len(S("max-snippet:-1")) <= 160)
print("unchecked", S(None), len(S(None, unchecked="keep")) <= 160, "| wayback kept never", S(None, crawl="wayback-20260101", unchecked="keep"))
print("unrelated statements", S("noarchive") == cut, S("noai,noimageai") == cut)
print("empty", S("", text=""), S("", text=None))
print("gate string", rc.DESCR_PUBLISH_POLICY == dp.PUBLISH_POLICY, dp.PUBLISH_POLICY)
print("pg", "wayback" in dp.PG_CAPTURE_OK and "noindex|none" in dp.PG_CAPTURE_OK, "DISTINCT ON (domain" in dp.PG_DOMAINS_OUT and "WHERE NOT (" in dp.PG_DOMAINS_OUT)
PY
)
expect "field ['noindex', '', '', 'max-snippet:50,nosnippet', 'none', 'max-snippet:-1', '', 'tdm-reservation', 'noai,noimageai', 'max-snippet:0,noindex', 'max-snippet:20']" "robots statements are stored sorted, lower-case, only what a release acts on; a bot's own line counts"
expect "capture [True, True, False, False, False, True]" "a Wayback capture and a noindex / none capture are not used; nosnippet and never-read ones are"
expect "cut True True True True True True" "a long description is cut to 160 characters at a word, with an ellipsis"
expect "short kept whole Oak furniture, made by hand. | spaces Oak furniture made by hand." "a short one is kept whole (white space tidied)"
expect "said no [None, None, None, None, None, None]" "nosnippet, max-snippet:0, a TDM reservation, noindex, none and Wayback: no description"
expect "max-snippet True True True" "max-snippet:N cuts to N, never past 160; -1 (no limit) still means 160"
expect "unchecked None True | wayback kept never None" "a capture never read for its statements shows nothing, unless the build is internal (keep)"
expect "unrelated statements True True" "noarchive and noai do not remove a description"
expect "empty None None" "no description, nothing shown"
expect "gate string True max=160;unchecked=drop;wayback=drop;noindex=drop" "the publish gate's spelled-out policy equals the rule's"
expect "pg True True" "the Postgres filters name Wayback, noindex and the latest capture"

echo ""; echo "=== the importer records what the page said (ccfetch) ==="
out=$(python3 - "$REPO/apps/search/importer" <<'PY' 2>&1
import gzip, sys, types
sys.path.insert(0, sys.argv[1])
try:
    import psycopg  # noqa: F401
except ImportError:                      # ccfetch only imports it; the parser needs no database
    sys.modules["psycopg"] = types.ModuleType("psycopg")
import ccfetch
def rec(head, html): return gzip.compress(b"WARC/1.0\r\nWARC-Type: response\r\n\r\n" + head + b"\r\n\r\n" + html)
H = b"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8"
page = lambda head, html: ccfetch.parse_record(rec(head, html))
print("plain", repr(page(H, b"<html><head><title>T</title><meta name=description content='D'></head><body>x</body></html>")["robots"]))
print("meta", page(H, b"<html><head><meta name='ROBOTS' content='NoIndex, NoFollow'><meta name=googlebot content='nosnippet'></head></html>")["robots"])
print("header", page(H + b"\r\nX-Robots-Tag: noarchive\r\nX-Robots-Tag: bingbot: max-snippet:0", b"<html></html>")["robots"])
print("tdm", page(H + b"\r\nTDM-Reservation: 1", b"<html></html>")["robots"], "|", page(H, b"<html><head><meta name='tdm-reservation' content='1'></head></html>")["robots"],
      "|", repr(page(H + b"\r\nTDM-Reservation: 0", b"<html></html>")["robots"]))
print("other meta ignored", repr(page(H, b"<html><head><meta name='keywords' content='noindex'><meta property='og:title' content='nosnippet'></head></html>")["robots"]))
print("description untouched", page(H, b"<html><head><title>T</title><meta name=description content='D'><meta name=robots content=nosnippet></head></html>")["description"])
print("backfill sql", "d.robots IS NULL" in ccfetch.ROBOTS_SQL and "wayback" in ccfetch.ROBOTS_SQL and "cc_captures" in ccfetch.ROBOTS_SQL)
import os, tempfile
d = tempfile.mkdtemp()
pages = {"a.warc.gz": rec(H, b"<html><head><meta name=robots content=noindex></head></html>"), "b.warc.gz": rec(H, b"<html><head><title>x</title></head></html>")}
def fake_get(url, start, length, throttle):
    name = url.rsplit("/", 1)[1]
    if name not in pages: raise OSError("gone")
    return pages[name]
ccfetch._get = fake_get
open(os.path.join(d, "in.tsv"), "w").write("a.example\tCC-MAIN-2026-30\ta.warc.gz\t0\t10\nb.example\tCC-MAIN-2026-30\tb.warc.gz\t5\t10\nc.example\tCC-MAIN-2026-30\tmissing.warc.gz\t5\t10\n")
ccfetch.log = lambda *a: None
ccfetch.cmd_robots_tsv(os.path.join(d, "in.tsv"), os.path.join(d, "out.tsv"), 1, 2, 1000)
print("tsv", [l.rstrip("\n").split("\t") for l in open(os.path.join(d, "out.tsv"))])
open(os.path.join(d, "bad.tsv"), "w").write("only\ttwo\n")
try: ccfetch.cmd_robots_tsv(os.path.join(d, "bad.tsv"), os.path.join(d, "o2.tsv"), 1, 1, 10); print("bad input accepted")
except SystemExit: print("bad input refused")
PY
)
expect "plain ''" "a page that says nothing is recorded as read, with nothing said ('' — not NULL)"
expect "meta noindex,nosnippet" "robots meta tags are read, the general one and a search engine's own, any case"
expect "header max-snippet:0,noarchive" "X-Robots-Tag headers are read, a bot prefix dropped"
expect "tdm tdm-reservation | tdm-reservation | ''" "a TDM reservation is read from the header and the meta tag; 0 is not one"
expect "other meta ignored ''" "other meta tags do not count"
expect "description untouched D" "the importer still stores the description; the release decides"
expect "backfill sql True" "the backfill reads again only never-read Common Crawl captures"
expect "tsv [['a.example', 'CC-MAIN-2026-30', 'noindex'], ['b.example', 'CC-MAIN-2026-30', '']]" "the file-to-file backfill writes a line per capture read ('' = nothing said) and none for one it could not read"
expect "bad input refused" "... and refuses an input that is not the five columns"

echo ""; echo "=== the stage applies it (build.py stage_capture_policy) ==="
out=$(python3 - "$REPO/apps/search/tools/sqlite_lookups" <<'PY' 2>&1
import os, sqlite3, sys, tempfile
sys.path.insert(0, sys.argv[1])
import build as b
b.log = lambda *a: None
long = "Shop the widest range of handmade oak furniture, delivered free across the country, with a ten year guarantee on every table, chair, bed and bookcase we make in our own workshop."
def stage(unchecked=None):
    st = sqlite3.connect(os.path.join(tempfile.mkdtemp(), "stage.sqlite"))
    for t, (cols, _sql) in b.STAGE.items():
        st.execute(f"CREATE TABLE {t} ({cols})")
    I = lambda t, *rows: st.executemany(f"INSERT INTO {t} VALUES ({','.join('?' * len(rows[0]))})", rows)
    names = ("ok", "long", "nosnip", "noidx", "wb", "never", "nodescr", "nosrc")
    I("s_docs", *[(f"{n}.example", f"https://{n}.example/", n.upper(), long if n == "long" else "" if n == "nodescr" else f"about {n}") for n in names])
    I("s_docs_src", ("ok.example", "CC-MAIN-2026-30", ""), ("long.example", "CC-MAIN-2026-30", "noarchive"), ("nosnip.example", "CC-MAIN-2026-30", "nosnippet"),
      ("noidx.example", "CC-MAIN-2026-30", "noindex"), ("wb.example", "wayback-20260101", ""), ("never.example", "CC-MAIN-2026-30", None),
      ("nodescr.example", "CC-MAIN-2026-30", ""))
    I("s_hostnav", ("k", "https://mail.ok.example/", "Mail", long, None))
    I("s_pages", ("https://ok.example/a", "ok.example", "A"), ("https://noidx.example/b", "noidx.example", "B"),
      ("https://wb.example/c", "wb.example", "C"), ("https://never.example/d", "never.example", "D"))
    if unchecked: os.environ["DESCR_UNCHECKED"] = unchecked
    else: os.environ.pop("DESCR_UNCHECKED", None)
    n = b.stage_capture_policy(st)
    return st, n
st, n = stage()
rows = dict((d.split(".")[0], x) for d, x in st.execute("SELECT domain, description FROM s_docs"))
print("left", sorted(rows))
print("descr", rows["ok"], "|", rows["nosnip"], "|", rows["never"], "|", rows["nosrc"], "|", len(rows["long"]) <= 160 and rows["long"].endswith("…"), "|", repr(rows["nodescr"]))
print("titles stay", sorted(st.execute("SELECT title FROM s_docs WHERE domain IN ('nosnip.example', 'never.example')").fetchall()))
print("service", st.execute("SELECT description FROM s_hostnav").fetchall())
print("counts", {k: v for k, v in n.items() if k != "policy"})
print("policy", n["policy"], b.staged_policy(st))
print("pages left", [r[0] for r in st.execute("SELECT url FROM s_pages ORDER BY url")])
print("pages out", dict(st.execute("SELECT key, value FROM s_policy")).get("pages_capture_out"))
sys.path.insert(0, os.path.join(sys.argv[1], "..", "..", "release"))
import types
sys.modules.setdefault("psycopg", types.ModuleType("psycopg")); sys.modules["psycopg"].Error = Exception
import release_v1 as rv
st.commit(); path = st.execute("PRAGMA database_list").fetchone()[2]
print("registry sees", rv.StageSource(path).pages_out(), list(rv.StageSource(path).pages()))
st2, n2 = stage("keep")
rows2 = dict((d.split(".")[0], x) for d, x in st2.execute("SELECT domain, description FROM s_docs"))
print("keep", rows2["never"], "|", rows2["nosnip"], "|", sorted(rows2), "|", len(st2.execute("SELECT description FROM s_hostnav").fetchone()[0]) <= 160, "|", n2["policy"])
old = sqlite3.connect(":memory:")
print("old stage", b.staged_policy(old))
os.environ["DESCR_UNCHECKED"] = "maybe"
try: stage("maybe"); print("bad value accepted")
except SystemExit as e: print("bad value refused")
PY
)
expect "left ['long', 'never', 'nodescr', 'nosnip', 'nosrc', 'ok']" "a Wayback capture and a noindex capture leave the stage whole"
expect "descr about ok | None | None | None | True | ''" "descriptions: kept, dropped for nosnippet, dropped when never read (or with no record at all), cut when long"
expect "titles stay [('NEVER',), ('NOSNIP',)]" "a site that loses its description keeps its title and address"
expect "service [(None,)]" "a subdomain service's description (no record of what it said) is dropped"
expect "counts {'wayback captures dropped': 1, 'noindex captures dropped': 1, 'descriptions kept': 2, 'cut': 1, 'dropped: site said no': 1, 'dropped: never checked': 2, \"pages dropped: their site's homepage capture\": 1, 'service descriptions dropped': 1}" "the stage reports what it did"
expect "pages left ['https://never.example/d', 'https://ok.example/a', 'https://wb.example/c']" "a homepage that said noindex takes its site's inner pages with it (audit 2026-10-22 G4); a Wayback-only or never-read one does not"
expect "pages out {\"pages\": 1}" "the count of pages dropped is recorded in the stage for the registry stage"
expect "registry sees {'pages': 1} [('https://never.example/d', 'D'), ('https://ok.example/a', 'A'), ('https://wb.example/c', 'C')]" "release_v1 registry --from-stage reads the kept pages and reports the count"
expect "policy max=160;unchecked=drop;wayback=drop;noindex=drop {'descr_policy': 'max=160;unchecked=drop;wayback=drop;noindex=drop'}" "the policy is recorded for lookup.sqlite's meta"
expect "keep about never | None | ['long', 'never', 'nodescr', 'nosnip', 'nosrc', 'ok'] | True | max=160;unchecked=keep;wayback=drop;noindex=drop" "DESCR_UNCHECKED=keep (an internal build) keeps unread ones, still honours a no, and says so in the policy"
expect "old stage {}" "a stage made before the policy existed records none (the publish gate refuses it)"
expect "bad value refused" "any other DESCR_UNCHECKED is refused"

echo ""; echo "Total: $((PASS+FAIL))  PASS: $PASS  FAIL: $FAIL"
[[ $FAIL -eq 0 ]]
