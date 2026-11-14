"""search-health — read-only freshness and health checker for the search engine on indexhost.

Every CHECK_EVERY_S (default 15 min) it:
  1. reads catalogue facts about each source in sources.py from the build database `search`
     (row estimate, size, data-file mtime, and the data's own date where it has one) — never a
     count(*) or a scan of a big table, so it cannot evict live search from the ZFS cache;
  2. checks the live pollers in the `news` database (feeds, scoreboard, weather), the `serve`
     database, search-api's HTTP port, and the free disk a serve refresh needs;
  3. writes /state/status.json (one entry per source: status, reasons, age, rows, loader);
  4. pushes ntfy alerts through lib/notify.sh (bundled as /app/notify.sh) on a transition into a
     bad status, again every REALERT_S while it stays bad, and a recovery when it clears.

Read-only by construction: every session runs with default_transaction_read_only=on,
statement_timeout and lock_timeout (a check queued behind a table swap gives up after 1 s and
reports "busy" instead of holding up everything behind it).

Env: PG* (from the stack's .env), CHECK_EVERY_S, REALERT_S, ALERTS=0|1, STATE_DIR, STACK_DIR,
     NTFY_URL/NTFY_TOPIC/NTFY_TOKEN (notify.conf), RUN_ONCE=1 (one pass, then exit).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

import psycopg
import sqlite3

import freshness as F
from sources import SOURCES, D

STATE_DIR = os.environ.get("STATE_DIR", "/state")
STACK_DIR = os.environ.get("STACK_DIR", "/stack")
CHECK_EVERY_S = int(os.environ.get("CHECK_EVERY_S", "900"))
REALERT_S = int(os.environ.get("REALERT_S", str(D)))
ALERTS = os.environ.get("ALERTS", "0") == "1"
API_URL = os.environ.get("SEARCH_API_URL", "http://search-api:8090/healthz")
NOTIFY_SH = os.environ.get("NOTIFY_SH", "/app/notify.sh")      # lib/notify.sh, bundled in the image
PGOPTIONS = "-c default_transaction_read_only=on -c statement_timeout=10s -c lock_timeout=1s"
H = 3600


def log(msg: str) -> None:
    print(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {msg}", flush=True)


def connect(db: str):
    return psycopg.connect(dbname=db, autocommit=True, options=PGOPTIONS, connect_timeout=10,
                           application_name="search-health")


# --- build database ---------------------------------------------------------------------------

TABLE_SQL = """
SELECT c.relname, c.reltuples::bigint, pg_total_relation_size(c.oid),
       date_part('epoch', (pg_stat_file(pg_relation_filepath(c.oid), true)).modification)
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' AND c.relkind = 'r' AND c.relname = ANY(%s)
"""
AS_OF_SQL = {
    # One row, any row: every article of a load carries the same dump name.
    "wiki_dump": "SELECT dump FROM wiki_docs LIMIT 1",
}


def observe_source(conn, src: dict) -> dict:
    try:
        rows = {r[0]: r for r in conn.execute(TABLE_SQL, (src["tables"],)).fetchall()}
        missing = [t for t in src["tables"] if t not in rows]
        if missing:
            return {"missing": missing}
        first = rows[src["tables"][0]]
        n = [rows[t][1] for t in src["tables"]]
        obs = {"rows": -1 if any(x < 0 for x in n) else sum(n),
               "bytes": sum(rows[t][2] for t in src["tables"]),
               "last_write": float(first[3]) if first[3] is not None else None, "as_of": None}
        kind = src.get("as_of")
        if kind:
            v = conn.execute(AS_OF_SQL[kind]).fetchone()
            v = v[0] if v else None
            obs["as_of"] = F.wiki_dump_epoch(v) if kind == "wiki_dump" else (float(v) if v is not None else None)
            obs["as_of_raw"] = str(v) if v is not None else None
        return obs
    except psycopg.errors.LockNotAvailable:
        return {"busy": "busy: a table is locked (a load or swap is running)"}
    except psycopg.Error as e:
        return {"error": f"{type(e).__name__}: {str(e).strip()[:200]}"}


# --- live pollers ------------------------------------------------------------------------------

# The live pollers' five numbers (ages in seconds). NEWS_SQLITE=<file>: the news file (ADR-063, the only
# store going forward); unset: the Postgres `news` database (a BRIDGE until no host has one).
LIVE_SQL = {
    "postgres": {
        "feeds": """SELECT count(*) FILTER (WHERE NOT disabled),
                    count(*) FILTER (WHERE NOT disabled AND (last_ok IS NULL OR
                        last_ok < now() - make_interval(mins => 4 * poll_min) - interval '1 hour')),
                    date_part('epoch', now() - max(last_ok)) FROM feeds""",
        "items": "SELECT date_part('epoch', now() - max(fetched)) FROM items",
        "games": """SELECT count(*), date_part('epoch', now() - max(updated)) FROM games
                    WHERE start_utc BETWEEN now() - interval '1 day' AND now() + interval '1 day'""",
        "weather": "SELECT date_part('epoch', now() - max(updated)) FROM weather",
        "wx": """SELECT date_part('epoch', now() - (SELECT max(at) FROM wx_obs)),
                 date_part('epoch', now() - (SELECT max(cycle) FROM wx_fc))""",
    },
    "sqlite": {
        "feeds": """SELECT coalesce(sum(NOT disabled), 0),
                    coalesce(sum(NOT disabled AND (last_ok IS NULL OR last_ok < :now - 4 * poll_min * 60 - 3600)), 0),
                    :now - max(last_ok) FROM feeds""",
        "items": "SELECT :now - max(fetched) FROM items",
        "games": "SELECT count(*), :now - max(updated) FROM games WHERE start_utc BETWEEN :now - 86400 AND :now + 86400",
        "weather": "SELECT :now - max(updated) FROM weather",
        "wx": "SELECT :now - (SELECT max(at) FROM wx_obs), :now - (SELECT max(cycle) FROM wx_fc)",
    },
}


def _news_conn():
    """(query function, close) on the news store; raises sqlite3.Error / psycopg.Error when it is unusable."""
    path = os.environ.get("NEWS_SQLITE", "")
    if path:
        if not os.path.exists(path):                 # an absent file is an error, never "no news"
            raise sqlite3.OperationalError(f"news file {path} does not exist")
        c = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
        now = time.time()
        return (lambda k: c.execute(LIVE_SQL["sqlite"][k], {"now": now}).fetchone()), c.close
    c = connect("news")
    return (lambda k: c.execute(LIVE_SQL["postgres"][k]).fetchone()), c.close


def check_live(now: float) -> list[dict]:
    out = []
    store = "news file" if os.environ.get("NEWS_SQLITE") else "news database"
    try:
        q, close = _news_conn()
        try:
            en, overdue, newest = q("feeds")
            st, why = F.feeds_status(en or 0, overdue or 0, newest)
            (fetched,) = q("items")
            if fetched is None or fetched > 2 * H:
                st, why = F.worst([st, "stale"]), why + [f"newest headline fetched {'never' if fetched is None else f'{fetched / 60:.0f} min ago'}"]
            out.append(_live("news", st, why, newest, f"{en} feeds enabled, {overdue} overdue", "search-news"))

            n_win, upd = q("games")
            st, why = F.games_status(n_win or 0, upd)
            out.append(_live("sports", st, why, upd, f"{n_win} games yesterday-tomorrow", "search-sports"))

            (w,) = q("weather")
            if w is None:
                out.append(_live("weather", "unknown", ["no weather row (SEARCH_HOME unset?)"], None, "", "search-weather"))
            else:
                bad = w > 3 * H
                out.append(_live("weather", "stale" if bad else "ok",
                                 [f"home weather updated {w / 60:.0f} min ago (budget 180)"] if bad else [], w, "", "search-weather"))

            obs_age, fc_age = q("wx")
            why = []
            if obs_age is None or obs_age > 2 * H:
                why.append("METARs " + ("never" if obs_age is None else f"{obs_age / 60:.0f} min old") + " (budget 120)")
            if fc_age is None or fc_age > 12 * H:
                why.append("NBM forecast cycle " + ("never" if fc_age is None else f"{fc_age / H:.1f} h old") + " (budget 12)")
            out.append(_live("wxall", "stale" if why else "ok", why, obs_age, "", "search-wxall"))
        finally:
            close()
    except (psycopg.Error, sqlite3.Error) as e:
        out.append(_live("news", "error", [f"{store}: {type(e).__name__}: {str(e).strip()[:200]}"], None, "", "search-news"))
    return out


def _live(name, status, reasons, age, detail, loader):
    return {"name": name, "kind": "live", "status": status, "reasons": reasons,
            "age_s": age, "detail": detail, "loader": loader + " container (runs continuously)"}


# --- serving -----------------------------------------------------------------------------------

def check_serve(build_writes: dict) -> list[dict]:
    out = []
    try:
        with connect("serve") as c:
            (made,) = c.execute("SELECT date_part('epoch', (pg_stat_file('base/' || oid || '/PG_VERSION', true)).modification) "
                                "FROM pg_database WHERE datname = current_database()").fetchone()
            (size,) = c.execute("SELECT pg_database_size(current_database())").fetchone()
        now = time.time()
        age = None if made is None else now - made
        why, st = [], "ok"
        if age is not None and age > 45 * D:
            st, why = "stale", [f"serve built {age / D:.1f} days ago (budget 45)"]
        served = {k: v for k, v in build_writes.items() if v is not None}
        newer = sorted(k for k, v in served.items() if made is not None and v > made + 14 * D)
        if newer:
            st = "stale"
            why.append("built more than 14 days before newer build data in: " + ", ".join(newer))
        behind = sorted(k for k, v in served.items() if made is not None and v > made + 3600)
        out.append({"name": "serve", "kind": "serve", "status": st, "reasons": why, "age_s": age,
                    "bytes": size, "behind_build": behind,
                    "loader": "deploy.sh serve-refresh (file copy + swap, ~35 min on 2026-09-27)"})
    except psycopg.Error as e:
        out.append({"name": "serve", "kind": "serve", "status": "error",
                    "reasons": [f"serve database: {type(e).__name__}: {str(e).strip()[:200]}"], "loader": "deploy.sh serve-refresh"})

    # search-api answers only its allow-list: a 403 from here still proves the process is up;
    # the database behind it is the `serve` check above.
    try:
        urllib.request.urlopen(API_URL, timeout=5)
        api = ("ok", ["/healthz 200"])
    except urllib.error.HTTPError as e:
        api = ("ok", [f"process answering (HTTP {e.code} outside its allow-list; database checked via serve)"]) \
            if e.code in (403,) else ("error", [f"/healthz HTTP {e.code}"])
    except Exception as e:  # noqa: BLE001 — any failure to connect is the finding
        api = ("error", [f"search-api not answering: {type(e).__name__}: {e}"])
    out.append({"name": "search-api", "kind": "serve", "status": api[0], "reasons": api[1],
                "loader": "search-api container"})
    return out


def check_disk(search_bytes) -> dict:
    """serve-refresh copies the whole build database into serve_next before dropping the
    build-only tables, and keeps serve_old until the swap: it needs about one `search` of room."""
    try:
        free = shutil.disk_usage(STACK_DIR).free
    except OSError as e:
        return {"name": "disk", "kind": "serve", "status": "unknown", "reasons": [f"{STACK_DIR}: {e}"]}
    need = int((search_bytes or 0) * 1.2)
    ok = free >= need
    return {"name": "disk", "kind": "serve", "status": "ok" if ok else "error",
            "reasons": [] if ok else [f"{free / 1e9:.0f} GB free, a serve refresh needs ~{need / 1e9:.0f} GB"],
            "free_bytes": free, "need_bytes": need, "loader": "-"}


# --- scheduler ledger --------------------------------------------------------------------------

def last_runs() -> dict:
    """Last scheduled run per source from the scheduler's runs.jsonl (absent until it is enabled)."""
    last = {}
    try:
        with open(os.path.join(STATE_DIR, "runs.jsonl"), encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                    last[r["source"]] = r
                except (ValueError, KeyError):
                    continue
    except FileNotFoundError:
        pass
    return last


# --- one pass ----------------------------------------------------------------------------------

def run_once(state: dict) -> dict:
    now = time.time()
    prev = state.get("sources", {})
    results, build_writes = [], {}
    runs = last_runs()
    search_bytes = None
    try:
        with connect("search") as c:
            (search_bytes,) = c.execute("SELECT pg_database_size('search')").fetchone()
            for src in SOURCES:
                obs = observe_source(c, src)
                v = F.assess(src, obs, prev.get(src["name"]), now)
                run = runs.get(src["name"])
                if run and run.get("outcome") in ("failed", "partial"):
                    v["status"] = F.worst([v["status"], "error"])
                    v["reasons"].append(f"last scheduled load {run['outcome']} at {run.get('at', '?')}")
                if src.get("served"):
                    build_writes[src["name"]] = obs.get("last_write") if isinstance(obs, dict) else None
                results.append({"name": src["name"], "kind": "build", **v,
                                "rows": obs.get("rows"), "bytes": obs.get("bytes"),
                                "last_write": obs.get("last_write"), "as_of": obs.get("as_of"),
                                "as_of_raw": obs.get("as_of_raw"), "budget_d": src["budget_d"],
                                "tables": src["tables"], "origin": src["origin"], "cadence": src["cadence"],
                                "unattended": src["unattended"], "retired": src.get("retired"),
                                "last_result": run.get("outcome") if run else "unknown (loaded by hand)"})
    except psycopg.Error as e:
        results.append({"name": "build-db", "kind": "build", "status": "error",
                        "reasons": [f"search database: {type(e).__name__}: {str(e).strip()[:200]}"]})
    results += check_live(now)
    results += check_serve(build_writes)
    results.append(check_disk(search_bytes))

    alerts = state.get("alerts", {})
    for r in results:
        if r.get("retired"):        # ADR-065: build-side history only, never paged about
            continue
        action, alerts[r["name"]] = F.alert_action(alerts.get(r["name"]), r["status"], now, REALERT_S)
        if action:
            notify(action, r)
    status = {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)), "alerts_enabled": ALERTS,
              "summary": {s: sum(1 for r in results if r["status"] == s) for s in F.SEVERITY},
              "sources": results}
    _write_json(os.path.join(STATE_DIR, "status.json"), status)
    keep = {r["name"]: F.next_baseline(prev.get(r["name"]), r.get("rows"), r.get("bytes"))
            for r in results if r.get("kind") == "build"}
    return {"sources": keep, "alerts": alerts}


def notify(action: str, r: dict) -> None:
    title = f"search: {r['name']} {r['status']}" if action == "alert" else f"search: {r['name']} recovered"
    body = "; ".join(r.get("reasons") or ["ok"])
    log(f"{action.upper()} {title} — {body}" + ("" if ALERTS else " (ALERTS=0: not pushed)"))
    if not ALERTS:
        return
    fn = "send_alert" if action == "alert" else "send_recovery"
    args = [title, body] + (["4" if r["status"] in ("error", "missing", "dropped") else "3", "warning,mag"] if action == "alert" else [])
    env = dict(os.environ, NTFY_SYNC="1", NOTIFY_SH=NOTIFY_SH, NOTIFY_DEDUP_KEY=f"search-{r['name']}",
               NOTIFY_DEDUP_DIR=os.path.join(STATE_DIR, "dedup"),
               NOTIFY_UNDELIVERED_LOG=os.path.join(STATE_DIR, "undelivered-alerts.log"))
    try:
        subprocess.run(["bash", "-c", f'source "$NOTIFY_SH" && {fn} "$@"', "notify", *args],
                       env=env, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError) as e:
        log(f"notify.sh could not run: {e}")


def _write_json(path: str, obj) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=1, default=str)
    os.replace(tmp, path)


def main() -> None:
    os.makedirs(os.path.join(STATE_DIR, "dedup"), exist_ok=True)
    state_path = os.path.join(STATE_DIR, "state.json")
    log(f"search-health: every {CHECK_EVERY_S}s, alerts {'ON' if ALERTS else 'OFF (log only)'}, state {STATE_DIR}")
    while True:
        try:
            with open(state_path, encoding="utf-8") as f:
                state = json.load(f)
        except (FileNotFoundError, ValueError):
            state = {}
        t0 = time.time()
        try:
            state = run_once(state)
            _write_json(state_path, state)
            s = json.load(open(os.path.join(STATE_DIR, "status.json"), encoding="utf-8"))["summary"]
            log("pass done in %.1fs: %s" % (time.time() - t0, ", ".join(f"{k} {v}" for k, v in s.items() if v)))
        except Exception as e:  # noqa: BLE001 — the loop must survive anything; the next pass retries
            log(f"pass failed: {type(e).__name__}: {e}")
        if os.environ.get("RUN_ONCE") == "1":
            return
        time.sleep(CHECK_EVERY_S)


if __name__ == "__main__":
    sys.exit(main())
