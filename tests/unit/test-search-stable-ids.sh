#!/usr/bin/env bash
################################################################################
# Unit tests for stable ids across releases: build.py --previous keeps
# places.pid, dict ids, cards.t (and accounts.vord / stocks.tord) from the previous release,
# so an unchanged row is byte-identical between month N and N+1 and the update package
# does not have to ship lookup.sqlite / places-*.sqlite whole.
#
# Fixture: tests/unit/lib/search_update_fixture.py (SYNTHETIC world, the real registry and
# lookup builders). Month 2 = ~2% of everything added/removed/changed, and one place deleted
# that held the top sequence number of its cell; month 3 adds a place in that same cell.
#
# Usage: PY=<python with tantivy + numpy> bash tests/unit/test-search-stable-ids.sh
################################################################################
set -uo pipefail

SCRIPT_DIR_T="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR_T/../.." && pwd)"
PY="${PY:-python3}"
export PYTHONDONTWRITEBYTECODE=1
TOOL_DIR="$REPO_ROOT/apps/search/tools/sqlite_lookups"
API_DIR="$REPO_ROOT/apps/search/serve/api"

PASS=0; FAIL=0
RED=$'\033[0;31m'; GREEN=$'\033[0;32m'; NC=$'\033[0m'
show() {   # PASS/FAIL lines from a python driver
    while IFS= read -r l; do
        case "$l" in
            PASS*) echo "  ${GREEN}PASS${NC} ${l#PASS }"; PASS=$((PASS+1));;
            FAIL*) echo "  ${RED}FAIL${NC} ${l#FAIL }"; FAIL=$((FAIL+1));;
            *) echo "       $l";;
        esac
    done
}

echo ""; echo "=== stable ids across releases ==="
if ! "$PY" -c "import tantivy, numpy" 2>/dev/null; then
    echo "  SKIP: $PY cannot import tantivy + numpy"; exit 0
fi
command -v zstd >/dev/null || { echo "  SKIP: no zstd"; exit 0; }
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT

# ---- carry_order unit checks (no build needed)
"$PY" - "$TOOL_DIR" >"$tmp/o1" 2>&1 <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
import build as b
def P(n, ok, d=""):
    print(("PASS " if ok else "FAIL ") + n + ("" if ok else " | " + str(d)))
k = list("abcdefg")
P("carry_order: no previous = dense_rank 1..n", b.carry_order(k, {}, "x") == {c: i + 1 for i, c in enumerate(k)})
prev = {c: i + 1 for i, c in enumerate(k)}
new = ["a", "b", "b2", "b3", "c", "d", "e", "f", "g", "h"]
o = b.carry_order(new, prev, "x")
P("carry_order: unchanged keys keep their rank", all(o[c] == prev[c] for c in k), o)
P("carry_order: new keys sit strictly between their neighbours, order kept", all(o[x] < o[y] for x, y in zip(new, new[1:])), o)
P("carry_order: a key past the end gets the next integer", o["h"] == 8, o)
P("carry_order: a deleted key's neighbours are untouched", b.carry_order(["a", "c", "e"], prev, "x") == {"a": 1, "c": 3, "e": 5})
o = b.carry_order(["a", "z0", "b"], {"a": 5, "b": 3}, "x")
P("carry_order: a previous rank that breaks the order is renumbered, never emitted out of order", o["a"] == 5 and 5 < o["z0"] < o["b"], o)
try:
    b.carry_order(["a"] + ["n%d" % i for i in range(10)] + ["b"], {"a": 1, "b": 1 + 2 ** -50}, "x"); full = False
except SystemExit:
    full = True
P("carry_order: no room left refuses loudly (float precision)", full)
PY
drv=$?
show <"$tmp/o1"
if [[ $drv -ne 0 ]]; then echo "  ${RED}FAIL${NC} the o1 driver crashed (exit $drv)"; tail -5 "$tmp/o1" | cut -c1-200; FAIL=$((FAIL+1)); fi

# ---- the fixture chain: r1, r2 (--previous r1), r2f (fresh, same registry), r3 (--previous r2)
"$PY" - "$REPO_ROOT" "$tmp" "$API_DIR" "$TOOL_DIR" >"$tmp/o2" 2>&1 <<'PY'
import importlib.util, json, os, random, sqlite3, subprocess, sys, types, zlib
repo, tmp, api, tool = sys.argv[1:5]
sys.path.insert(0, os.path.join(repo, "tests", "unit", "lib"))
import search_update_fixture as fx

def P(n, ok, d=""):
    print(("PASS " if ok else "FAIL ") + n + ("" if ok else " | " + str(d)[:600]), flush=True)

def rows(rel, f, sql):
    c = sqlite3.connect(os.path.join(rel, f)); r = c.execute(sql).fetchall(); c.close(); return r

CELL = lambda lat, lon: int((lat + 90) / 0.5) * 720 + int((lon + 180) / 0.5)
U1 = fx.world(1500, 1)
tmpb = os.path.join(tmp, "_t"); os.makedirs(tmpb)
R1 = fx.build_release(U1, None, None, tmp, "r1", "fx-1", tmpb)
U2 = fx.evolve(U1, 0.02, 1)

# the place holding the top sequence number of a cell in r1 is deleted in month 2, and month 2 adds nothing to that cell
top = {}
for pid, lat, lon in rows(R1, "places-osm.sqlite", "SELECT pid, lat, lon FROM places ORDER BY pid"):
    top[pid >> 20] = (pid, lat, lon)
cell = sorted(top)[0]
A_pid, A_lat, A_lon = top[cell]
victim = next(k for k, v in U1["osm"].items() if (v["lat"], v["lon"]) == (A_lat, A_lon))
U2["osm"].pop(victim); U2["hw"].pop(victim, None)
for k, v in U2["osm"].items():
    if k not in U1["osm"] and CELL(v["lat"], v["lon"]) == cell:
        v["lat"] = round(v["lat"] + 1.0, 5) if v["lat"] < 40 else round(v["lat"] - 1.0, 5)
# month 2 also: a stock added in front of the others and one deleted (their en_US ranks would all shift), and a place of a kind
# the dictionary has not seen (it sorts in front of the old kinds)
U2["stock"]["TK1"] = {"name": "Corp one", "cik": 1001, "sitelinks": 3}
del U2["stock"][sorted(U2["stock"])[6]]
rk = random.Random(5)
for tbl, pid in (("osm", "node/88888"), ("ov", "ov:00000058-0000-4000-8000-000000088888")):
    row = fx.place_row(rk, "Cafe Newkind", 88888)
    row.update(kind="amenity=cafe", lat=40.6, lon=-73.4)
    assert CELL(40.6, -73.4) != cell
    U2[tbl][pid] = row
moved = sorted(k for k in U2["osm"] if k in U1["osm"] and k != victim)[5]       # one place moves to another cell
U2["osm"][moved]["lat"] = round(U2["osm"][moved]["lat"] + 1.0, 5) if U2["osm"][moved]["lat"] < 40 else round(U2["osm"][moved]["lat"] - 1.0, 5)
R2 = fx.build_release(U2, U1, R1, tmp, "r2", "fx-2", tmpb, stable=True)
R2F = fx.build_release(U2, U1, R1, tmp, "r2f", "fx-2", tmpb, stable=False)
U3 = fx.evolve(U2, 0.02, 1)
newp = fx.place_row(random.Random(9), "Osm Place Fresh in the retired cell", 99999)
newp["lat"], newp["lon"] = A_lat, A_lon
U3["osm"]["node/99999"] = newp
R3 = fx.build_release(U3, U2, R2, tmp, "r3", "fx-3", tmpb, stable=True)

# ---------------- places: pid stays, new pids above, retired pids never come back
for f in ("places-osm.sqlite", "places-overture.sqlite"):
    a = {r[1]: (r[0], r[2:]) for r in rows(R1, f, "SELECT * FROM places")}
    b = {r[1]: (r[0], r[2:]) for r in rows(R2, f, "SELECT * FROM places")}
    common = set(a) & set(b)
    shifted = {d for d in common if a[d][0] >> 20 != b[d][0] >> 20}          # only the one place the test moved
    P(f"{f}: only a place that really moved cells changes its cell prefix", len(shifted) == (1 if f == "places-osm.sqlite" else 0), len(shifted))
    common -= shifted
    P(f"{f}: places are added, removed and kept between the months (premise)", len(common) > 100 and set(b) - set(a) and set(a) - set(b))
    P(f"{f}: every place present in both months keeps its pid", all(a[d][0] == b[d][0] for d in common))
    same = [d for d in common if a[d][1] == b[d][1]]
    P(f"{f}: unchanged places are byte-identical rows (pid and all columns)",
      all(rows(R1, f, f"SELECT * FROM places WHERE pid = {a[d][0]}") == rows(R2, f, f"SELECT * FROM places WHERE pid = {b[d][0]}") for d in same))
    P(f"{f}: only the really changed places differ ({len(common) - len(same)} of {len(common)}, under 5%)", len(common) - len(same) < 0.05 * len(common))
    mx = {}
    for p, _ in a.values():
        mx[p >> 20] = max(mx.get(p >> 20, -1), p & 0xFFFFF)
    new_pids = [b[d][0] for d in set(b) - set(a)]
    P(f"{f}: a new place gets a sequence number above everything its cell used before", new_pids and all((p & 0xFFFFF) > mx.get(p >> 20, -1) for p in new_pids), new_pids[:5])
    gone = {a[d][0] for d in set(a) - set(b)}
    P(f"{f}: the pid of a deleted place is not handed to anything else", gone and not (gone & {p for p, _ in b.values()}), gone)
    P(f"{f}: every pid's cell prefix matches the place's coordinates (the reader's R*Tree ranges)",
      all(p >> 20 == CELL(lat, lon) for p, lat, lon in rows(R2, f, "SELECT pid, lat, lon FROM places")))
    P(f"{f}: as many places as a build without --previous", len(rows(R2F, f, "SELECT pid FROM places")) == len(b))

# a place that moves to another cell gets a pid in the NEW cell; its old pid is retired
mo = U1["osm"][moved]
old_pid = rows(R1, "places-osm.sqlite", f"SELECT pid FROM places WHERE lat = {mo['lat']} AND lon = {mo['lon']}")
new_pid = rows(R2, "places-osm.sqlite", f"SELECT pid FROM places WHERE lon = {mo['lon']} AND name = '{mo['name']}'")
P("a place that moved cells gets a new pid in its new cell and its old pid is retired",
  len(old_pid) == 1 and len(new_pid) == 1 and new_pid[0][0] != old_pid[0][0] and new_pid[0][0] >> 20 == CELL(U2["osm"][moved]["lat"], mo["lon"])
  and not rows(R2, "places-osm.sqlite", f"SELECT 1 FROM places WHERE pid = {old_pid[0][0]}") if old_pid and new_pid else False, (old_pid, new_pid))

# the schema of every file is the same as in a release built without --previous (a schema change makes the update package ship the file whole)
for f in ("lookup.sqlite", "places-osm.sqlite", "places-overture.sqlite", "hours-web.sqlite"):
    q = "SELECT name, sql FROM sqlite_master ORDER BY name"
    P(f"{f}: the schema is the same as in the previous month's and a fresh build's", rows(R1, f, q) == rows(R2, f, q) == rows(R2F, f, q))

# two hops: month 2 deleted the top place of a cell, month 3 adds one there: it must not get the retired pid
r3 = {p for (p,) in rows(R3, "places-osm.sqlite", "SELECT pid FROM places")}
fresh = rows(R3, "places-osm.sqlite", "SELECT pid FROM places WHERE name = 'Osm Place Fresh in the retired cell'")
P("two hops: the retired pid of a cell's top place is not reused two releases later",
  len(fresh) == 1 and fresh[0][0] >> 20 == A_pid >> 20 and fresh[0][0] > A_pid and A_pid not in r3, (A_pid, fresh))

# ---------------- dict
for f in ("lookup.sqlite", "places-osm.sqlite", "places-overture.sqlite"):
    a, b = dict(rows(R1, f, "SELECT id, s FROM dict")), dict(rows(R2, f, "SELECT id, s FROM dict"))
    P(f"{f} dict: every previous id keeps its string", all(b.get(i) == s for i, s in a.items()))
    P(f"{f} dict: ids stay unique", len(set(b.values())) == len(b))
    P(f"{f} dict: a new string gets an id above the previous maximum", all(i > max(a) for i in set(b) - set(a)))
a, b = dict(rows(R1, "lookup.sqlite", "SELECT id, s FROM dict")), dict(rows(R2, "lookup.sqlite", "SELECT id, s FROM dict"))
P("lookup dict: month 2 really adds strings (premise)", len(set(b) - set(a)) > 0)

# ---------------- cards, accounts.vord, stocks.tord
def card(rel):
    return {d: (c, json.loads(zlib.decompress(c))) for d, c in rows(rel, "lookup.sqlite", "SELECT doc_id, c FROM cards")}
def titles(rel):
    return {d: n for n, d in rows(rel, "lookup.sqlite", "SELECT name, doc_id FROM names WHERE kind = 1")}
c1, c2, c2f = card(R1), card(R2), card(R2F)
t1, t2 = titles(R1), titles(R2)
common = set(c1) & set(c2)
P("cards: the t of every article whose title did not change is unchanged", all(c1[d][1].get("t") == c2[d][1].get("t") for d in common if t1.get(d) == t2.get(d)))
diff = [d for d in common if c1[d][0] != c2[d][0]]
P(f"cards: only the really changed cards differ ({len(diff)} of {len(common)}, under 5%)", len(diff) < 0.05 * len(common))
P("cards: a build WITHOUT --previous moves most of them (premise: the problem)", sum(c1[d][0] != c2f[d][0] for d in common) > 0.5 * len(common))
order = sorted((d for d in c2f if "t" in c2f[d][1]), key=lambda d: c2f[d][1]["t"])
P("cards: t follows the title order of a fresh build (the reader sorts on it)",
  all(c2[x][1]["t"] <= c2[y][1]["t"] for x, y in zip(order, order[1:])) and any(not isinstance(c2[d][1]["t"], int) for d in c2 if "t" in c2[d][1]))
for tb, k in (("accounts", "vord"), ("stocks", "tord")):
    a, b = dict(rows(R1, "lookup.sqlite", f"SELECT doc_id, {k} FROM {tb}")), dict(rows(R2, "lookup.sqlite", f"SELECT doc_id, {k} FROM {tb}"))
    P(f"{tb}.{k}: unchanged for every row present in both months", len(set(a) & set(b)) > 5 and all(a[d] == b[d] for d in set(a) & set(b)))
for tb in ("names", "sites", "suggest_items"):
    a = {r[0]: r for r in rows(R1, "lookup.sqlite", f"SELECT doc_id, * FROM {tb}")}
    b = {r[0]: r for r in rows(R2, "lookup.sqlite", f"SELECT doc_id, * FROM {tb}")}
    ch = sum(a[d] != b[d] for d in set(a) & set(b))
    P(f"{tb}: rows differ only where the content changed ({ch} of {len(set(a) & set(b))}, under 10%)", ch < 0.1 * len(set(a) & set(b)))

# ---------------- reader: answers equal a fresh no-previous build of the same month
psycopg = types.ModuleType("psycopg"); psycopg.Error = Exception; sys.modules["psycopg"] = psycopg
sys.path.insert(0, api)
spec = importlib.util.spec_from_file_location("search_api", api + "/search_api.py")
m = importlib.util.module_from_spec(spec); sys.modules["search_api"] = m; spec.loader.exec_module(m)

def answers(rel):
    import lookups_release
    lk = lookups_release.ReleaseLookups(rel, m)
    X = lambda sql, p: list(lk.execute(sql, p))
    out = []
    pts = rows(rel, "places-osm.sqlite", "SELECT name, lat, lon, name_key FROM places WHERE name <> '' ORDER BY doc_id")[::7] + \
          rows(rel, "places-overture.sqlite", "SELECT name, lat, lon, name_key FROM places WHERE name <> '' ORDER BY doc_id")[::7]
    for name, lat, lon, nk in pts:
        p = {"lat": lat, "lon": lon, "dlat": 0.36, "dlon": 0.47, "r": 40}
        out.append(("name", name, X(m.PLACES_SQL.format(match=m.PLACE_MATCH_NAME), dict(p, name=name.lower()))))
        out.append(("kind", name, X(m.PLACES_SQL.format(match=m.PLACE_MATCH_KINDS), dict(p, kinds=["amenity=restaurant", "shop=wholesale"]))))
        out.append(("exact", name, X(m.PLACE_EXACT_SQL, dict(p, keys=[nk]))))
    ttl = [t for t, in rows(rel, "lookup.sqlite", "SELECT name FROM names WHERE kind = 1 ORDER BY doc_id")][::11]
    for t in ttl:
        out.append(("card", t, lk.card_parts(t, 4)))
    for acc in ("x", "instagram", "github"):
        out.append(("acct", acc, X(m.PROFILES_SQL, {"t": ttl[0], "p": [acc]})))
    return out
try:
    a, b = answers(R2), answers(R2F)
    P(f"reader: {len(a)} answers (places by name / kind / exact, cards, profiles) equal a fresh build of the same month", a == b and len(a) > 100,
      [x for x, y in zip(a, b) if x != y][:1])
    P("reader: the place answers are not empty (the comparison is not vacuous)", sum(1 for x in a if x[0] in ("name", "exact") and x[2]) > 20)
except Exception as e:                       # report, do not hide
    P("reader: answers from both builds", False, repr(e))

# ---------------- guards
r = subprocess.run([sys.executable, os.path.join(tool, "build.py"), os.path.join(tmp, "x"), "--stage", "/nonexistent", "--previous", R1], capture_output=True, text=True)
P("--previous without --registry is refused (stand-in doc_ids are not stable)", r.returncode != 0 and "needs --registry" in r.stderr + r.stdout, r.stderr[-300:])
# SEQ_BITS overflow: a copy of the builder with 4-bit sequences (16 places per cell) cannot carry r1's pids, so every
# place is new and the cells overflow: it must refuse loudly, not let the sequence spill into the cell bits
import shutil
t6 = os.path.join(tmp, "tl6"); shutil.copytree(tool, t6)
src = open(os.path.join(t6, "build.py")).read()
assert "CELL, NLON, SEQ_BITS = 0.5, 720, 20" in src
open(os.path.join(t6, "build.py"), "w").write(src.replace("CELL, NLON, SEQ_BITS = 0.5, 720, 20", "CELL, NLON, SEQ_BITS = 0.5, 720, 4"))
r = subprocess.run([sys.executable, os.path.join(t6, "build.py"), os.path.join(tmp, "o6"), "--stage", os.path.join(tmpb, "r2-stage.sqlite"),
                    "--registry", os.path.join(R2, "registry.sqlite"), "--places-dir", os.path.join(tmpb, "r2-places"), "--release-id", "fx-2",
                    "--previous", R1], capture_output=True, text=True)
P("a cell that would overflow SEQ_BITS refuses the build", r.returncode != 0 and "overflow SEQ_BITS" in r.stderr + r.stdout, (r.stderr + r.stdout)[-400:])
PY
drv=$?
show <"$tmp/o2"
if [[ $drv -ne 0 ]]; then echo "  ${RED}FAIL${NC} the o2 driver crashed (exit $drv)"; tail -5 "$tmp/o2" | cut -c1-200; FAIL=$((FAIL+1)); fi

echo ""; echo "Passed: $PASS  Failed: $FAIL"
[[ $FAIL -eq 0 ]]
