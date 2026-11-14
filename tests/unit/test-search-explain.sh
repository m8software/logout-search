#!/usr/bin/env bash
set -euo pipefail
################################################################################
# Unit test — "How this was ranked": the search trace (search_api._trace, 2026-10-08)
################################################################################
# REAL search_api helpers, no database:
#   1. with no trace on (every ordinary request) a step records nothing and returns its list untouched
#   2. with a trace on, a step keeps its count, its first TRACE_ROWS results and only the drawn fields
#   3. trace_run picks the run that answered: the corrected spelling's when it was accepted, else the first
#   4. a trace set for one request is not seen by the next on the same thread (the native host reuses threads)
# Not covered: the hooks inside _search and /explain itself (they need an index) — checked against a running
# engine: curl 'http://<engine>/explain?q=python'.
# Usage: bash tests/unit/test-search-explain.sh   (PY=python with psycopg)
################################################################################
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PY="${PY:-python3}"
if ! "$PY" -c 'import psycopg' 2>/dev/null; then echo "SKIP: $PY lacks psycopg"; exit 0; fi
API="${REPO}/apps/search/serve/api" "$PY" - <<'PY'
import os, sys
sys.path.insert(0, os.environ["API"])
import search_api as s
fails = total = 0
def check(name, ok, detail=""):
    global fails, total
    total += 1
    print(("  PASS " if ok else "  FAIL ") + name + ("" if ok else f"  [{detail}]")); fails += 0 if ok else 1

rows = [dict(s._site(f"https://site{i}.example/", f"Site {i}" + "x" * 200, "a description", 100.0 - i, popular=i == 0),
             secret="LEAK", lr=0.5, pos0=i + 1) for i in range(14)]
# 1. off
check("off by default", s._trace.get() is None)
same = s._tl("sites", rows)
s._tr("merged", rows)
check("off: a list comes back as it was (the same object) and nothing is recorded", same is rows and s._trace.get() is None)
# 2. on
tr = []
tok = s._trace.set(tr)
try:
    s._tr("start", q="nbc")
    got = s._tl("sites", rows)
    s._tr("merged", rows)
    s._tr("fused", [])
finally:
    s._trace.reset(tok)
check("on: the list still comes back as it was", got is rows)
check("on: steps in order, a list under its name", [(x["step"], x.get("name")) for x in tr] == [("start", None), ("list", "sites"), ("merged", None), ("fused", None)], tr[:1])
m = tr[2]
check("on: the count is the whole list, the rows its first TRACE_ROWS", m["n"] == 14 and len(m["rows"]) == s.TRACE_ROWS == 10, [m["n"], len(m["rows"])])
check("on: a row is url, title (cut), source, score, the scores of the rerankers and its flags — nothing else",
      m["rows"][0] == {"url": "https://site0.example/", "title": ("Site 0" + "x" * 200)[:90], "source": "site", "score": 100.0,
                       "lr": 0.5, "pos0": 1, "flags": ["popular"]} and "flags" not in m["rows"][1], m["rows"][0])
check("on: no text of the result, no other field", "LEAK" not in repr(tr) and "a description" not in repr(tr))
check("on: an empty step is kept as empty", tr[3] == {"step": "fused", "n": 0, "rows": []}, tr[3])
check("reset: the next request on this thread has no trace", s._trace.get() is None)
# 3. the run that answered
runs = [{"step": "start", "q": "decathhlon"}, {"step": "merged", "n": 1}, {"step": "start", "q": "decathlon"}, {"step": "merged", "n": 9},
        {"step": "ce", "n": 9}]
check("run: the corrected spelling's steps when it was accepted", s.trace_run(runs, "decathlon") == [{"step": "merged", "n": 9}, {"step": "ce", "n": 9}])
check("run: the first run when the correction was not", s.trace_run(runs, "decathhlon") == [{"step": "merged", "n": 1}])
check("run: one run, and none", s.trace_run(runs[:2], "decathhlon") == [{"step": "merged", "n": 1}] and s.trace_run([], "x") == [])
print(f"\nTotal: {total}  PASS: {total - fails}  FAIL: {fails}")
sys.exit(1 if fails else 0)
PY
