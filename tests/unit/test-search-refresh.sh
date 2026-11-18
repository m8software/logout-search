#!/usr/bin/env bash
# requires: cmd:python3   # stdlib only: apps/search/refresh/freshness.py, sources.py, scheduler.py
################################################################################
# Unit tests for the search data-freshness rules
################################################################################
# Budgets, the "rows dropped sharply" rule, unknown-is-not-zero, alert transitions and
# re-alert interval, the news/scoreboard rules, the night window, and every gate a scheduled
# job must pass (master switch, per-job flag, approval, readiness, lock). Nothing here touches
# a database, docker or the network.
#
# Usage: bash tests/unit/test-search-refresh.sh
################################################################################
set -uo pipefail
# -B / no bytecode: a mutation run restores files within the same second, and a cached .pyc
# with the same size and mtime would keep running the mutant.

SCRIPT_DIR_T="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR_T/../.." && pwd)"
REFRESH="${REPO_ROOT}/apps/search/refresh"
IMPORTER="${REPO_ROOT}/apps/search/importer/search_import.py"
PY="${PY:-python3}"

PASS=0
FAIL=0
RED=$'\033[0;31m'; GREEN=$'\033[0;32m'; NC=$'\033[0m'
ok()  { echo "  ${GREEN}PASS${NC} $1"; PASS=$((PASS+1)); }
bad() { echo "  ${RED}FAIL${NC} $1"; shift; [[ $# -gt 0 ]] && printf '       %s\n' "$@"; FAIL=$((FAIL+1)); }
section() { echo ""; echo "=== $1 ==="; }

[[ -d "$REFRESH" ]] || { echo "SKIP: $REFRESH not present"; exit 0; }

run() {  # run <python-source>
    local out
    out=$(REFRESH="$REFRESH" IMPORTER="$IMPORTER" CHECK="$1" PYTHONDONTWRITEBYTECODE=1 "$PY" -B - 2>&1 <<'PYEOF'
import os, sys
sys.path.insert(0, os.environ["REFRESH"])
exec(os.environ["CHECK"])
PYEOF
)
    while IFS= read -r line; do
        case "$line" in
            OK\ *)  ok "${line#OK }" ;;
            BAD\ *) bad "${line#BAD }" ;;
            *)      [[ -n "$line" ]] && bad "unexpected output" "$line" ;;
        esac
    done <<< "$out"
}

section "data vintage"
run '
import freshness as F, datetime as dt
e = F.wiki_dump_epoch("enwiki-20260901")
print("OK a dump name is its date" if e == dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc).timestamp() else "BAD dump %r" % e)
print("OK a dump name that does not parse is unknown, not 1970" if F.wiki_dump_epoch("enwiki-latest") is None and F.wiki_dump_epoch(None) is None and F.wiki_dump_epoch("enwiki-20261341") is None else "BAD dump parse")
c = F.cc_crawl_epoch("CC-MAIN-2026-39")
print("OK a crawl id is the Monday of its ISO week" if c == dt.datetime(2026, 9, 21, tzinfo=dt.timezone.utc).timestamp() else "BAD crawl %r" % c)
'

section "budgets and drops (one source)"
run '
import freshness as F
D = F.DAY; now = 1_000 * D
src = {"budget_s": 45 * D, "min_rows": 100, "drop_frac": 0.2}
a = lambda obs, prev=None: F.assess(src, obs, prev, now)
print("OK exactly at the budget is fresh" if a({"rows": 500, "bytes": 1, "last_write": now - 45 * D})["status"] == "ok" else "BAD at budget")
r = a({"rows": 500, "bytes": 1, "last_write": now - 45 * D - 1})
print("OK one second past the budget is stale, and says by how much" if r["status"] == "stale" and "budget 45 days" in r["reasons"][0] else "BAD past budget %r" % r)
r = a({"rows": 500, "bytes": 1, "as_of": now - 50 * D, "last_write": now - 1})
print("OK the data\x27s own date wins over a recent file write" if r["status"] == "stale" and r["basis"] == "as_of" else "BAD as_of %r" % r)
r = a({"rows": 500, "bytes": 1, "as_of": None, "last_write": now - D})
print("OK without a date of its own, the file write is the basis" if r["status"] == "ok" and r["basis"] == "last_write" else "BAD basis %r" % r)
r = a({"rows": 500, "bytes": 1})
print("OK no time at all is unknown, not fresh" if r["status"] == "unknown" else "BAD no time %r" % r)
r = a({"rows": -1, "bytes": 1, "last_write": now})
print("OK reltuples -1 (never analysed) is unknown rows, not zero rows" if r["status"] == "ok" else "BAD -1 %r" % r)
r = a({"rows": 99, "bytes": 1, "last_write": now})
print("OK below the row floor is dropped" if r["status"] == "dropped" else "BAD floor %r" % r)
prev = {"rows": 1000, "bytes": 1000}
r = a({"rows": 799, "bytes": 799, "last_write": now}, prev)
print("OK rows AND size down more than 20% = dropped" if r["status"] == "dropped" and "1,000 -> 799" in " ".join(r["reasons"]) else "BAD drop %r" % r)
r = a({"rows": 800, "bytes": 800, "last_write": now}, prev)
print("OK exactly 20% down is not a drop" if r["status"] == "ok" else "BAD 20pct %r" % r)
r = a({"rows": 100, "bytes": 990, "last_write": now}, prev)
print("OK rows down but size not (a truncated stage table\x27s stale estimate) is not a drop" if r["status"] == "ok" else "BAD rows only %r" % r)
r = a({"rows": 990, "bytes": 100, "last_write": now}, prev)
print("OK size down but rows not (VACUUM FULL) is not a drop" if r["status"] == "ok" else "BAD size only %r" % r)
try:
    r1 = a({"rows": 500, "bytes": 500, "last_write": now}, {"rows": -1, "bytes": 1000})
    r2 = a({"rows": 500, "bytes": 500, "last_write": now}, {"rows": None, "bytes": 1000})
    print("OK no drop judged against an unknown previous count" if r1["status"] == r2["status"] == "ok" else "BAD unknown prev %r" % ((r1, r2),))
except Exception as e:
    print("BAD unknown prev crashed: %r" % e)
print("OK a failed check is error; a missing table is missing" if a({"error": "x"})["status"] == "error" and a({"missing": ["t"]})["status"] == "missing" and a(None)["status"] == "error" else "BAD error/missing")
r = a({"rows": 10, "bytes": 1, "last_write": now - 99 * D})
print("OK stale AND dropped reports the worse: dropped" if r["status"] == "dropped" and len(r["reasons"]) == 2 else "BAD worst %r" % r)
'

section "the drop baseline (high-water mark)"
run '
import freshness as F
D = F.DAY; now = 1_000 * D
src = {"budget_s": 45 * D, "min_rows": 100, "drop_frac": 0.2}
base = F.next_baseline(None, 1000, 1000)
low = {"rows": 500, "bytes": 500, "last_write": now}
r1 = F.assess(src, low, base, now)
base2 = F.next_baseline(base, 500, 500)
r2 = F.assess(src, low, base2, now + 900)
print("OK a drop stays dropped on the next pass with the same low count (no false recovery)" if r1["status"] == r2["status"] == "dropped" else "BAD sticky %r" % ((r1["status"], r2["status"], base2),))
r3 = F.assess(src, {"rows": 950, "bytes": 950, "last_write": now}, base2, now)
print("OK back above the threshold is ok again" if r3["status"] == "ok" else "BAD back %r" % r3)
print("OK an unknown reading (-1 / None) keeps the old baseline" if F.next_baseline(base, -1, None) == {"rows": 1000, "bytes": 1000} else "BAD unknown %r" % F.next_baseline(base, -1, None))
b = F.next_baseline(None, 1000, 1000)
s1 = F.assess(src, {"rows": 850, "bytes": 850, "last_write": now}, b, now)
b = F.next_baseline(b, 850, 850)
s2 = F.assess(src, {"rows": 720, "bytes": 720, "last_write": now}, b, now)
print("OK two 15% steps add up to a drop" if s1["status"] == "ok" and s2["status"] == "dropped" else "BAD steps %r" % ((s1["status"], s2["status"]),))
print("OK no baseline yet starts one" if F.next_baseline(None, 7, 9) == {"rows": 7, "bytes": 9} and F.next_baseline(None, -1, None) == {"rows": None, "bytes": None} else "BAD first")
r = F.assess(src, {"busy": "busy: a table is locked"}, None, now)
print("OK a table locked by a running load is quiet (unknown, says busy), not an error" if r["status"] == "unknown" and "busy" in r["reasons"][0] else "BAD busy %r" % r)
'

section "alerts: transitions and re-alert"
run '
import freshness as F
D = F.DAY; t = 1_000 * D
act, s = F.alert_action(None, "stale", t)
print("OK the first bad check alerts" if act == "alert" and s == {"status": "stale", "sent": t} else "BAD first %r" % ((act, s),))
act2, s2 = F.alert_action(s, "stale", t + D - 1)
print("OK the same bad status within a day stays quiet" if act2 is None and s2["sent"] == t else "BAD repeat %r" % ((act2, s2),))
act3, _ = F.alert_action(s, "stale", t + D)
print("OK and alerts again after a day" if act3 == "alert" else "BAD realert %r" % act3)
act4, _ = F.alert_action(s, "dropped", t + 60)
print("OK a change from one bad status to another alerts at once" if act4 == "alert" else "BAD change %r" % act4)
act5, s5 = F.alert_action(s, "ok", t + 60)
print("OK going back to ok sends a recovery" if act5 == "recover" and s5["status"] == "ok" else "BAD recover %r" % act5)
act6, _ = F.alert_action({"status": "ok", "sent": 0}, "ok", t)
act7, _ = F.alert_action(None, "unknown", t)
print("OK ok after ok and unknown are quiet" if act6 is None and act7 is None else "BAD quiet %r" % ((act6, act7),))
'

section "live pollers"
run '
import freshness as F
H = 3600
print("OK a quarter of the feeds overdue is still ok" if F.feeds_status(400, 100, 60)[0] == "ok" else "BAD 25pct")
print("OK more than a quarter overdue = stalled" if F.feeds_status(400, 101, 60)[0] == "stale" else "BAD >25pct")
print("OK no feed polled in over an hour = stalled" if F.feeds_status(400, 0, H + 1)[0] == "stale" and F.feeds_status(400, 0, H)[0] == "ok" else "BAD newest")
print("OK never polled = stalled; no feeds = missing" if F.feeds_status(400, 0, None)[0] == "stale" and F.feeds_status(0, 0, 1)[0] == "missing" else "BAD never")
print("OK no games in the window is not a stall (off season)" if F.games_status(0, None)[0] == "ok" else "BAD off season")
print("OK games in the window that stopped updating = stale" if F.games_status(3, 2 * H + 1)[0] == "stale" and F.games_status(3, 2 * H)[0] == "ok" and F.games_status(3, None)[0] == "stale" else "BAD games")
'

section "scheduler gates"
run '
import freshness as F
D = F.DAY
print("OK the night window: 01 in, 06 out" if F.in_night_window(1) and F.in_night_window(5) and not F.in_night_window(6) and not F.in_night_window(0) else "BAD night")
print("OK a window across midnight (23-05)" if F.in_night_window(23, 23, 5) and F.in_night_window(0, 23, 5) and not F.in_night_window(5, 23, 5) and not F.in_night_window(22, 23, 5) else "BAD wrap")
print("OK an empty window never opens" if not any(F.in_night_window(h, 3, 3) for h in range(24)) else "BAD empty")
print("OK due: never ran, or exactly one cadence ago; not a second before" if F.job_due(30 * D, None, 100 * D) and F.job_due(30 * D, 70 * D, 100 * D) and not F.job_due(30 * D, 70 * D + 1, 100 * D) else "BAD due")
job = {"name": "blocklists", "ready": True, "heavy": True, "approval": None}
on = {"enabled": True, "jobs": {"blocklists": True}, "approved": {}, "night": [1, 6]}
idle = {"flock": False, "busy_backends": 0, "busy_containers": []}
print("OK every gate open = may start" if F.may_start(job, on, 2, idle) == (True, "ok") else "BAD open %r" % (F.may_start(job, on, 2, idle),))
r = F.may_start(job, {"jobs": {"blocklists": True}}, 2, idle)
print("OK no master switch in the config = the scheduler is off (the default)" if r[0] is False and r[1].startswith("scheduler disabled") else "BAD default %r" % (r,))
print("OK master switch off" if not F.may_start(job, dict(on, enabled=False), 2, idle)[0] else "BAD master")
print("OK master on but this job off" if not F.may_start(job, dict(on, jobs={}), 2, idle)[0] else "BAD job flag")
paid = dict(job, approval="AWS spend")
on_paid = dict(on, jobs={"blocklists": True})
r = F.may_start(paid, on_paid, 2, idle)
print("OK a job that spends money needs approval beyond its flag" if not r[0] and "approval" in r[1] and F.may_start(paid, dict(on_paid, approved={"blocklists": True}), 2, idle)[0] else "BAD approval %r" % (r,))
print("OK a job not wired for unattended runs never starts" if not F.may_start(dict(job, ready=False), on, 2, idle)[0] else "BAD ready")
print("OK heavy jobs only at night; light ones any time" if not F.may_start(job, on, 14, idle)[0] and F.may_start(dict(job, heavy=False), on, 14, idle)[0] else "BAD heavy")
print("OK refused while another refresh holds the lock" if not F.may_start(job, on, 2, dict(idle, flock=True))[0] else "BAD flock")
print("OK refused while the build database has a client" if not F.may_start(job, on, 2, dict(idle, busy_backends=1))[0] else "BAD backends")
print("OK refused while a heavy container runs" if not F.may_start(job, on, 2, dict(idle, busy_containers=["search-g1build"]))[0] else "BAD containers")
r = F.may_start(dict(job, retired="ADR-065"), dict(on, approved={"blocklists": True}), 2, idle)
print("OK a retired job never starts, whatever the flags say" if not r[0] and r[1].startswith("retired") else "BAD retired %r" % (r,))
pats = (r"^search-job-", r"bench")
print("OK a heavy container counts only while it uses CPU (an idle bench does not block)" if F.busy_from_stats([("search-bitbench", 0.0), ("search-job-x", 120.5), ("search-api", 300.0), ("search-g1bench", 5.0)], pats) == ["search-job-x", "search-g1bench"] else "BAD busy %r" % F.busy_from_stats([("search-bitbench", 0.0), ("search-job-x", 120.5), ("search-api", 300.0), ("search-g1bench", 5.0)], pats))
from zoneinfo import ZoneInfo
import datetime as dt
ny = ZoneInfo("America/New_York")
ts = lambda *a: dt.datetime(*a, tzinfo=ny).timestamp()
n1 = F.next_start(ts(2026, 10, 24, 17, 0), ts(2026, 10, 1, 17, 0), ny, (1, 6))
n2 = F.next_start(ts(2026, 10, 1, 0, 30), ts(2026, 10, 1, 17, 0), ny, (1, 6))
n3 = F.next_start(ts(2026, 10, 2, 0, 30), ts(2026, 10, 1, 17, 0), ny, (1, 6))
n4 = F.next_start(ts(2026, 10, 2, 3, 0), ts(2026, 10, 1, 17, 0), ny, (1, 6))
n5 = F.next_start(ts(2026, 10, 2, 15, 0), ts(2026, 10, 1, 17, 0), ny, (1, 6), heavy=False)
want = [(10, 25, 1), (10, 2, 1), (10, 2, 1), (10, 2, 3), (10, 2, 15)]
got = [(n.month, n.day, n.hour) for n in (n1, n2, n3, n4, n5)]
print("OK next start: due by day -> 01:00 next night; overdue -> tonight; inside the window -> then; light -> when due" if got == want else "BAD next_start %r" % got)
print("OK outcome: exit 1 failed, FAILED in the log partial, else ok" if F.run_outcome(1, "") == "failed" and F.run_outcome(0, "celebrity: FAILED (HTTPError) — previous rows kept") == "partial" and F.run_outcome(0, "done in 3.1 min") == "ok" else "BAD outcome")
'

section "the registry and the shipped defaults"
run '
import os, re, json, sources as S
names = [s["name"] for s in S.SOURCES]
print("OK source names are unique" if len(names) == len(set(names)) else "BAD dupes")
need = {"tables", "budget_d", "budget_s", "min_rows", "origin", "cadence", "unattended", "job"}
print("OK every source has tables, a budget, a floor, an origin and a job" if all(need <= s.keys() and s["tables"] and s["budget_s"] > 0 for s in S.SOURCES) else "BAD fields")
subs = set(re.findall(r"add_parser\(\"([a-z0-9-]+)\"", open(os.environ["IMPORTER"]).read()))
steps = [st[0] for s in S.SOURCES for st in s["job"]["steps"]]
builtin = {"@serve-refresh"}
print("OK every scheduled step is a real importer subcommand or a scheduler built-in" if steps and all(x in subs or x in builtin for x in steps) else "BAD steps %r" % [x for x in steps if x not in subs and x not in builtin])
retired = {s["name"] for s in S.SOURCES if s.get("retired")}
print("OK the legacy sources are retired (ADR-065): kinds, site_quality, d2q" if {"kinds", "site_quality", "d2q"} <= retired else "BAD retired %r" % retired)
print("OK every job that reloads a table is heavy (night only, re-warm after)" if all(s["job"]["heavy"] for s in S.SOURCES if s["job"]["ready"]) else "BAD light reloads %r" % [s["name"] for s in S.SOURCES if s["job"]["ready"] and not s["job"]["heavy"]])
print("OK a ready job has steps; a job with no steps is not ready" if all(bool(s["job"]["steps"]) == s["job"]["ready"] for s in S.SOURCES) else "BAD ready/steps")
spend = {s["name"] for s in S.SOURCES if s["job"]["approval"]}
print("OK the AWS, model and Common Crawl jobs need approval" if {"anchors", "kinds", "site_quality", "homepages", "pages"} <= spend else "BAD approval set %r" % spend)
ex = json.load(open(os.path.join(os.environ["REFRESH"], "refresh.json.example")))
print("OK the example config ships with everything off" if ex["enabled"] is False and not any(ex["jobs"].values()) and not any(ex["approved"].values()) else "BAD example")
print("OK the example lists only real sources" if set(ex["jobs"]) <= set(names) else "BAD example names")
import scheduler as SC
SC.CONFIG = "/nonexistent/refresh.json"
print("OK the scheduler with no config file is disabled" if SC.load_config()["enabled"] is False else "BAD no config")
import tempfile
t = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False); t.write("{not json"); t.close()
SC.CONFIG = t.name; SC.log = lambda m: None
print("OK a broken config file disables everything" if SC.load_config()["enabled"] is False else "BAD broken config")
import datetime as dt
print("OK last month across a year boundary" if SC.prev_month(dt.datetime(2026, 1, 15)) == "2025-12" and SC.prev_month(dt.datetime(2026, 10, 1)) == "2026-09" else "BAD prev_month")
'

section "health.py pushes through lib/notify.sh"
run '
import os, sys, types, tempfile, json
sys.modules["psycopg"] = types.SimpleNamespace(Error=Exception, errors=types.SimpleNamespace(LockNotAvailable=Exception))
d = tempfile.mkdtemp()
rec = os.path.join(d, "calls")
sh = os.path.join(d, "notify.sh")
open(sh, "w").write("send_alert() { printf \"alert|%s|%s|%s|%s|%s|%s\\n\" \"$1\" \"$2\" \"$3\" \"$4\" \"$NOTIFY_DEDUP_KEY\" \"$NTFY_SYNC\" >> " + rec + "; }\n"
                    "send_recovery() { printf \"recover|%s|%s|%s\\n\" \"$1\" \"$2\" \"$NOTIFY_DEDUP_KEY\" >> " + rec + "; }\n")
os.environ.update(NOTIFY_SH=sh, STATE_DIR=d, ALERTS="1")
import health as Hm
Hm.log = lambda m: None
Hm.notify("alert", {"name": "facts", "status": "dropped", "reasons": ["rows fell"]})
Hm.notify("recover", {"name": "facts", "status": "ok", "reasons": []})
got = open(rec).read().splitlines() if os.path.exists(rec) else []
print("OK an alert goes to send_alert: title, reasons, priority 4, a per-source dedup key, synchronous" if got[:1] == ["alert|search: facts dropped|rows fell|4|warning,mag|search-facts|1"] else "BAD alert %r" % got)
print("OK a recovery goes to send_recovery with the same dedup key" if got[1:2] == ["recover|search: facts recovered|ok|search-facts"] else "BAD recover %r" % got)
Hm.ALERTS = False
Hm.notify("alert", {"name": "news", "status": "stale", "reasons": ["x"]})
print("OK with ALERTS off nothing is pushed" if len(open(rec).read().splitlines()) == 2 else "BAD pushed with alerts off")
'

section "health.py reads the news FILE (NEWS_SQLITE, ADR-063)"
run '
import os, sys, types, tempfile, sqlite3, time
PgError = type("PgError", (Exception,), {})
def no_pg(*a, **k):
    raise PgError("no postgres in this test")
sys.modules["psycopg"] = types.SimpleNamespace(Error=PgError, connect=no_pg, errors=types.SimpleNamespace(LockNotAvailable=Exception))
d = tempfile.mkdtemp()
f = os.path.join(d, "news.sqlite")
os.environ["NEWS_SQLITE"] = f
import health as Hm
by = lambda rows: {r["name"]: r for r in rows}
r = by(Hm.check_live(time.time()))
print("OK a missing news file is an error naming the file, not quiet" if r.get("news", {}).get("status") == "error" and "news file" in r["news"]["reasons"][0] and f in r["news"]["reasons"][0] else "BAD missing file %r" % r)
schema = open(os.path.join(os.environ["REFRESH"], "..", "news", "schema-sqlite.sql")).read()
c = sqlite3.connect(f); c.executescript(schema)
now = time.time()
c.executemany("INSERT INTO feeds (url, section, name, kind, poll_min, last_ok, disabled) VALUES (?, ?, ?, ?, ?, ?, ?)",
              [("u1", "n", "A", "rss", 15, now - 60, 0), ("u2", "n", "B", "rss", 15, now - 120, 0),
               ("u3", "n", "C", "rss", 15, now - 7 * 3600, 0), ("u4", "n", "D", "rss", 15, None, 1),
               ("u5", "n", "E", "rss", 60, now - 3 * 3600, 0)])     # hourly feed, 3 h: within 4 x 60 min + 1 h
c.execute("INSERT INTO items (feed_url, guid, url, title, published, fetched) VALUES (?, ?, ?, ?, ?, ?)", ("u1", "g", "x", "t", now - 300, now - 300))
c.execute("INSERT INTO wx_obs VALUES (?, ?, ?)", ("K", "{}", now - 600))
c.execute("INSERT INTO wx_fc VALUES (?, ?, ?, ?, ?)", ("K", "{}", "{}", now - 3 * 3600, now - 3 * 3600))
c.execute("INSERT INTO weather VALUES (?, ?, ?, ?)", ("home", "{}", "nws", now - 4 * 3600))
c.commit(); c.close()
r = by(Hm.check_live(time.time()))
n = r.get("news", {})
print("OK feeds counted from the file: 4 enabled, 1 overdue (an hourly feed 3 h old is not)" if n.get("detail") == "4 feeds enabled, 1 overdue" else "BAD feeds %r" % n)
print("OK the newest poll age comes from last_ok seconds" if n.get("age_s") is not None and 50 < n["age_s"] < 200 else "BAD age %r" % n.get("age_s"))
print("OK wxall fresh from wx_obs / wx_fc" if r.get("wxall", {}).get("status") == "ok" else "BAD wxall %r" % r.get("wxall"))
print("OK home weather 4 h old is stale (budget 3 h)" if r.get("weather", {}).get("status") == "stale" else "BAD weather %r" % r.get("weather"))
print("OK no games in the window: the sports line is still reported" if "sports" in r and r["sports"]["detail"] == "0 games yesterday-tomorrow" else "BAD sports %r" % r.get("sports"))
'

echo ""
echo "Total: $((PASS + FAIL))"
echo "PASS:  $PASS"
echo "FAIL:  $FAIL"
[[ $FAIL -eq 0 ]]
