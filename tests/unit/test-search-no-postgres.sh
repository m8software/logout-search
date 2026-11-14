#!/usr/bin/env bash
################################################################################
# Unit test — the engine with NO Postgres (SEARCH_POSTGRES=off, ADR-063): search_api answers whole
# searches from a release folder alone (release lookups + the Tantivy Wikipedia list), and the IVF
# meaning-search hits (Wikipedia page_ids) resolve to articles through the registry, not wiki_docs.
#
# Fixture: tests/unit/lib/search_update_fixture.py (SYNTHETIC release in the real formats). psycopg is
# a stub whose connect() records the call and raises: any Postgres use is a failure here. Every
# statement that reached the router without a release port is listed (NO_POSTGRES_MISSES).
# Needs python with tantivy + numpy (PY=...), else SKIP.   Usage: bash tests/unit/test-search-no-postgres.sh
################################################################################
set -uo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PY="${PY:-python3}"
export PYTHONDONTWRITEBYTECODE=1
if ! "$PY" -c "import tantivy, numpy" 2>/dev/null; then
    echo "  SKIP: $PY cannot import tantivy + numpy (PY=<python with tantivy==0.26.2 and numpy>)"
    echo ""; echo "Total: 0  PASS: 0  FAIL: 0 (skipped)"; exit 0
fi
tmp=$(mktemp -d); trap 'chmod -R u+w "$tmp" 2>/dev/null; rm -rf "$tmp"' EXIT
if ! bout=$("$PY" "$REPO/tests/unit/lib/search_update_fixture.py" build "$tmp/fx" --wiki 300 --months 1 --seed 5 2>&1); then
    echo "  FAIL fixture build failed"; tail -8 <<<"$bout"; echo ""; echo "Total: 1  PASS: 0  FAIL: 1"; exit 1
fi

API="$REPO/apps/search/serve/api" REL="$tmp/fx/r1" SEARCH_POSTGRES=off SEARCH_LOOKUP_BACKEND=release \
SEARCH_WIKI_BACKEND=tantivy-release SEARCH_RELEASE_DIR="$tmp/fx/r1" SEARCH_HOME="" \
"$PY" - <<'PY'
import importlib.util, os, sqlite3, sys, types
fails = checks = 0
def check(name, fn):
    global fails, checks
    checks += 1
    try:
        ok, detail = fn(), ""
        if isinstance(ok, tuple):
            ok, detail = ok
    except Exception as e:
        ok, detail = False, f"{type(e).__name__}: {e}"
    print(("  PASS " if ok else "  FAIL ") + name + ("" if ok else f"  [{str(detail)[:400]}]")); fails += 0 if ok else 1

touched = []
pg = types.ModuleType("psycopg")
pg.Error = type("Error", (Exception,), {}); pg.OperationalError = type("OperationalError", (pg.Error,), {})
def _connect(*a, **k):
    touched.append(k.get("dbname") or "default")
    raise pg.OperationalError("postgres must not be touched (SEARCH_POSTGRES=off)")
pg.connect = _connect
sys.modules["psycopg"] = pg
api, rel = os.environ["API"], os.environ["REL"]
sys.path.insert(0, api)
spec = importlib.util.spec_from_file_location("search_api", api + "/search_api.py")
a = importlib.util.module_from_spec(spec); sys.modules["search_api"] = a; spec.loader.exec_module(a)

reg = sqlite3.connect(rel + "/registry.sqlite")
wiki = reg.execute("SELECT ext, title FROM registry WHERE doc_id >> 28 = 1 ORDER BY doc_id LIMIT 40").fetchall()
print("=== the table list comes from the release, not Postgres ===")
def _has():
    with a._conn() as c:
        return a.tables(c)
try:
    has = _has()
except Exception as e:
    print(f"  (setup: the table list raised {type(e).__name__}: {e})"); has = frozenset()
check("wiki_docs (the Tantivy list) and the release lookups are present", lambda: ("wiki_docs" in has and "places" in has, sorted(has)))
check("no web lists without SEARCH_WEB_BACKEND=tantivy-release, no exact-anchor list", lambda: (not has & {"docs", "page_docs", "anchors"}, sorted(has)))
check("serving_ready() is true with no database", lambda: a.serving_ready())

print("=== whole searches ===")
def titles(rs):
    return [r.get("title", "") for r in rs]
for pid, title in wiki[:6:2]:
    q = title.lower()
    check(f"search {q!r} finds its article first",
          lambda q=q, title=title: (lambda rs: (bool(rs) and title in rs[0].get("title", ""), titles(rs)[:3]))(a.search(q, 10)))
q = wiki[1][1].split()[1].lower()                # a vocabulary word several articles share
check(f"a common word ({q!r}) returns several results", lambda: (lambda rs: (len(rs) >= 2, titles(rs)))(a.search(q, 10)))
check("a query that matches nothing returns [] (no error)", lambda: a.search("zzqxv nonexistentword", 10) == [])
slip = next(t.lower() for _p, t in wiki if len(a.norm_key(t)) >= 8 and t.isascii())
typed = slip[:3] + slip[4] + slip[3] + slip[5:] if slip[3] != slip[4] and slip[3:5].isalpha() else slip[:3] + slip[4:]
check(f"the name one slip from {typed!r} is found in the release's names, spelt with the typed spaces",
      lambda: (a.norm_key(a.correct_wide(typed) or "") == a.norm_key(slip) or a.norm_key(typed) == a.norm_key(slip), a.correct_wide(typed)))
check("a spelling with no name one slip away is not corrected", lambda: a.correct_wide("zzqxv nonexistentword") is None)
check("no Postgres connection was attempted", lambda: (touched == [], touched))
check("no statement reached the router without a release port", lambda: (not a.NO_POSTGRES_MISSES, sorted(a.NO_POSTGRES_MISSES)))

print("=== the HTTP endpoints the page uses ===")
import json, threading, urllib.parse, urllib.request
from http.server import ThreadingHTTPServer
srv = ThreadingHTTPServer(("127.0.0.1", 0), a.Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{srv.server_address[1]}"
def get(path, **q):
    with urllib.request.urlopen(base + path + "?" + urllib.parse.urlencode(q), timeout=60) as r:
        return r.status, json.loads(r.read())
t0 = wiki[0][1].lower()
yt = reg.execute("SELECT title FROM registry WHERE doc_id >> 28 = 6 ORDER BY doc_id LIMIT 1").fetchone()
yt_name = yt[0] if yt else "Channel 1 w001a"
yt_word = yt_name.split()[-1].lower()
queries = [t0, t0[:4], t0.replace("o", "0", 1), "weather", "weather in twin falls nj", "pizza near me", "how old is " + t0,
           "x instagram", "aapl stock", "w084b", "what is " + t0, "site0 example", "2 + 2", "the",
           "youtube " + yt_word, yt_word + " youtube channels", "youtube channel " + yt_name.lower()]
for q in queries:
    check(f"/instant {q!r} answers 200", lambda q=q: (lambda r: (r[0] == 200, r))(get("/instant", q=q, full=1)))
check("/instant for an article's title returns that article", lambda: (lambda r: (any(wiki[0][1] in (x.get("title") or "") for x in r[1].get("results") or []), str(r[1])[:300]))(get("/instant", q=t0, full=1)))
check("/search answers 200 with results", lambda: (lambda r: (r[0] == 200 and bool(r[1].get("results")), str(r)[:200]))(get("/search", q=t0)))
check("/more news answers (empty: no news store configured)", lambda: get("/more", q=t0, what="news", offset=0)[0] == 200)
check("still no Postgres connection after the endpoints", lambda: (touched == [], touched))
check("no statement reached the router without a release port (endpoints)", lambda: (not a.NO_POSTGRES_MISSES, sorted(a.NO_POSTGRES_MISSES)))
srv.shutdown()

print("=== YouTube channels by topic (YT_SEARCH_SQL) from the release ===")
def yt_search(q):
    try:
        return list(a.lookups().execute(a.YT_SEARCH_SQL, {"q": q}))
    except Exception as e:
        print(f"  (YT_SEARCH_SQL raised {type(e).__name__}: {e})")
        return None
ys = yt_search(yt_name) or []
check("the channel named by the query comes first, in Postgres\x27 row shape", lambda: (bool(ys) and ys[0][2] == yt_name and len(ys[0]) == 7 and ys[0][0].startswith("UC") and isinstance(ys[0][6], float), ys[:2]))
check("followers / sitelinks from signals.bin", lambda: isinstance(ys[0][4], int) and ys[0][4] >= 1000)
check("a word no channel has finds nothing", lambda: yt_search("zzqxvnothing") == [])
many = yt_search("channel " + yt_word) or []
check("results are ranked by bm25, best first", lambda: (len(many) > 1 and [r[6] for r in many] == sorted([r[6] for r in many], reverse=True), [r[6] for r in many][:5]))

print("=== IVF hits (page_ids) resolve through the registry ===")
lk = a.lookups()
pids = [p for p, _t in wiki[:5]]
known = {p for p, _t in reg.execute("SELECT ext, title FROM registry WHERE doc_id >> 28 = 1")}
gap = next(p for p in range(min(pids) + 1, max(known)) if p not in known)     # unknown, inside the range
try:
    rows = list(lk.execute(a.SEM_IVF_DOCS_SQL, {"ids": pids + [gap, 987654321]}))
except Exception as e:
    print(f"  (setup: SEM_IVF_DOCS_SQL raised {type(e).__name__}: {e})"); rows = []
check("each known page_id comes back with its registry title", lambda: ({r[0]: r[1] for r in rows} == {p: t for p, t in wiki[:5]}, rows))
check("... with the card's lead", lambda: bool(rows) and all(isinstance(r[2], str) and r[2] for r in rows))
check("unknown page_ids (inside and above the range) are dropped", lambda: (bool(rows) and all(r[0] not in (gap, 987654321) for r in rows), rows))
# a registry whose doc_id order is NOT page_id order (stable ids append new articles at the end)
newest = reg.execute("SELECT max(ext) FROM registry WHERE doc_id >> 28 = 1").fetchone()[0]
check("the map finds the last and first page_id (sorted by page_id, not doc_id)",
      lambda: {r[0] for r in lk.execute(a.SEM_IVF_DOCS_SQL, {"ids": [newest, min(pids)]})} == {newest, min(pids)})
check("still no Postgres connection", lambda: (touched == [], touched))

def unported():
    with a._conn() as c:
        rows = list(c.execute("SELECT page_id FROM wiki_vec ORDER BY 1 LIMIT 1"))
    return (rows == [] and any("wiki_vec" in m for m in a.NO_POSTGRES_MISSES), (rows, sorted(a.NO_POSTGRES_MISSES)))
check("a statement with no release port answers no rows (never a failed search) and is recorded", unported)
a.NO_POSTGRES_MISSES.clear()
# doc_id order != page_id order (stable ids append new articles): swap two articles' page_ids in a copy
import shutil, lookups_release
shutil.copytree(rel, rel + "-swap")
db = sqlite3.connect(rel + "-swap/registry.sqlite")
(d1, p1), (d2, p2) = db.execute("SELECT doc_id, ext FROM registry WHERE doc_id >> 28 = 1 ORDER BY doc_id LIMIT 2").fetchall()
db.execute("UPDATE registry SET ext = ? WHERE doc_id = ?", (-1, d1)); db.execute("UPDATE registry SET ext = ? WHERE doc_id = ?", (p1, d2))
db.execute("UPDATE registry SET ext = ? WHERE doc_id = ?", (p2, d1)); db.commit(); db.close()
sw = lookups_release.ReleaseLookups(rel + "-swap", a)
try:
    got = {r[0]: r[1] for r in sw.execute(a.SEM_IVF_DOCS_SQL, {"ids": [p1, p2]})}
except Exception as e:
    print(f"  (setup: the swapped registry raised {type(e).__name__}: {e})"); got = {}
check("page_ids out of doc_id order still resolve to the right article", lambda: (got == {p1: sw.title(d2), p2: sw.title(d1)}, (got, d1, d2)))

def announced(file_present):
    old_b, old_f = a.SITES_BACKEND, getattr(lk, "has_anchor_phrases", False)
    a.SITES_BACKEND, lk.has_anchor_phrases = "tantivy-release", file_present
    try:
        return a.release_text_tables()
    finally:
        a.SITES_BACKEND, lk.has_anchor_phrases = old_b, old_f
check("with the release web lists, `anchors` is announced only when anchor-phrases.sqlite exists",
      lambda: ("anchors" in announced(True) and "anchors" not in announced(False) and "docs" in announced(False),
               (sorted(announced(True)), sorted(announced(False)))))

print("=== configuration ===")
import subprocess
env = dict(os.environ, SEARCH_LOOKUP_BACKEND="postgres")
r = subprocess.run([sys.executable, "-c", "import sys,types; m=types.ModuleType('psycopg'); m.Error=Exception; sys.modules['psycopg']=m; "
                    f"sys.path.insert(0, {api!r}); import search_api"], env=env, capture_output=True, text=True)
check("SEARCH_POSTGRES=off without the release lookups refuses to start", lambda: (r.returncode != 0 and "SEARCH_POSTGRES=off needs" in r.stderr, r.stderr[-300:]))
print(f"\nTotal: {checks}  PASS: {checks - fails}  FAIL: {fails}")
sys.exit(1 if fails else 0)
PY
