"""search-refresh — runs the refresh jobs in sources.py on their schedule.

A job starts only when ALL of these hold (freshness.may_start):
  - it is not retired (ADR-065: the Jev-fed sources never run again)
  - refresh.json "enabled": true                      (master switch, default false)
  - refresh.json "jobs": {"<source>": true}           (per job, default false)
  - refresh.json "approved": {"<source>": true}       (only for jobs that spend money or risk a
                                                       block of the home IP — sources.py `approval`)
  - the job is wired for unattended runs (`ready`)
  - heavy jobs: inside the night window (America/New_York, default 01-06)
  - nobody else is working: no other refresh holds /state/refresh.lock, no client session on the
    build database, no heavy container using CPU (search-job-*, *build*, *bench*, …)
and it is due: its last successful run (or the hand load it was seeded with, see `seed`) is at
least one cadence ago. A job with "when": "inputs_changed" (serve-refresh) is due only while
search-health reports served build data newer than `serve`.

One job at a time, one per pass. An importer step is
`docker compose run --rm --no-deps search-importer <argv>` (--no-deps: never recreate
search-postgres — lesson-compose-up-recreates-dependencies). "@serve-refresh" runs the script
`deploy.sh refresh-stage` put in refresh/jobs/ in a postgres-client container, as
`deploy.sh serve-refresh` does. The outcome (ok / partial / failed, freshness.run_outcome) goes
to /state/runs.jsonl, which search-health reads. After a heavy job the serving indexes are
re-read into the cache (pg_prewarm 'read' on `serve`).

Modes (env): PLAN=1 print the schedule and exit; REWARM=1 re-warm serve once; SEED=1 record each wired job's hand load
(table write time from status.json) as its last run, so the first automatic runs spread over the
month instead of all landing on the first night; RUN_JOB=<source> run that one job now —
every gate except the night window and "due" still applies (supervised runs).

Pause everything: docker stop search-refresh.
"""
from __future__ import annotations

import datetime as dt
import fcntl
import json
import os
import subprocess
import sys
import time
from zoneinfo import ZoneInfo

import freshness as F
from sources import SOURCES, HEAVY_CONTAINER_PATTERNS, D

CONFIG = os.environ.get("REFRESH_CONFIG", "/config/refresh.json")
STATE_DIR = os.environ.get("STATE_DIR", "/state")
# The stack directory at its HOST path: compose and `docker run -v` resolve paths on the host.
STACK_DIR = os.environ.get("STACK_DIR", "/mnt/nas/logout/search")
TZ = ZoneInfo(os.environ.get("REFRESH_TZ", "America/New_York"))
EVERY_S = int(os.environ.get("REFRESH_EVERY_S", "600"))
PG_IMAGE = os.environ.get("REFRESH_PG_IMAGE", "logout/search-postgres:18.6-pgts1.4.0")
DEFAULT_CONFIG = {"enabled": False, "jobs": {}, "approved": {}, "night": [1, 6]}
LAST_OK = ("ok", "seed")


def log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {msg}"
    print(line, flush=True)
    try:        # also kept on disk: one-off runs (--rm) take their container log with them
        with open(os.path.join(STATE_DIR, "scheduler.log"), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def load_config() -> dict:
    try:
        with open(CONFIG, encoding="utf-8") as f:
            cfg = json.load(f)
    except FileNotFoundError:
        return dict(DEFAULT_CONFIG)
    except ValueError as e:
        log(f"{CONFIG} is not valid JSON ({e}) — treating every job as disabled")
        return dict(DEFAULT_CONFIG)
    return {**DEFAULT_CONFIG, **cfg}


def prev_month(now_local: dt.datetime) -> str:
    first = now_local.replace(day=1)
    return (first - dt.timedelta(days=1)).strftime("%Y-%m")


def job_of(src: dict) -> dict:
    return {"name": src["name"], **src["job"], "retired": src.get("retired")}


def last_ok_runs() -> dict:
    out = {}
    try:
        with open(os.path.join(STATE_DIR, "runs.jsonl"), encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("outcome") in LAST_OK:
                    out[r["source"]] = r["t"]
    except FileNotFoundError:
        pass
    return out


def health_status() -> dict:
    try:
        with open(os.path.join(STATE_DIR, "status.json"), encoding="utf-8") as f:
            return {r["name"]: r for r in json.load(f)["sources"]}
    except (FileNotFoundError, ValueError, KeyError):
        return {}


def inputs_changed(status: dict) -> bool:
    return bool(status.get("serve", {}).get("behind_build"))


def busy_containers() -> list[str]:
    p = subprocess.run(["docker", "stats", "--no-stream", "--format", "{{.Name}}\t{{.CPUPerc}}"],
                       capture_output=True, text=True, timeout=60)
    stats = []
    for line in p.stdout.splitlines():
        name, _, cpu = line.partition("\t")
        try:
            stats.append((name, float(cpu.strip().rstrip("%") or 0)))
        except ValueError:
            continue
    return F.busy_from_stats(stats, HEAVY_CONTAINER_PATTERNS)


def busy_backends() -> int:
    import psycopg
    with psycopg.connect(dbname="postgres", autocommit=True, connect_timeout=10,
                         options="-c default_transaction_read_only=on -c statement_timeout=10s",
                         application_name="search-refresh") as c:
        (n,) = c.execute("""SELECT count(*) FROM pg_stat_activity WHERE datname = 'search'
                            AND backend_type = 'client backend' AND pid <> pg_backend_pid()
                            AND application_name NOT IN ('search-health', 'search-refresh')""").fetchone()
    return n


def _run(cmd: list[str]) -> tuple[int, str]:
    log("run: " + " ".join(cmd))
    p = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True)
    out = p.stdout + p.stderr
    with open(os.path.join(STATE_DIR, "last-job.log"), "w", encoding="utf-8") as f:
        f.write(out)
    return p.returncode, "\n".join(out.splitlines()[-40:])


def compose_run(name: str, argv: list[str]) -> tuple[int, str]:
    if argv == ["@serve-refresh"]:
        script = os.path.join(STACK_DIR, "refresh", "jobs", "serve-refresh.sh")
        if not os.path.exists(script):
            return 1, f"{script} missing — run deploy.sh refresh-stage"
        return _run(["docker", "run", "--rm", "--name", "search-job-serve", "--network", "search_default",
                     "-e", "PGPASSWORD", "-v", f"{script}:/job/serve-job.sh:ro", PG_IMAGE, "bash", "/job/serve-job.sh"])
    return _run(["docker", "compose", "-p", "search", "--project-directory", STACK_DIR,
                 "-f", os.path.join(STACK_DIR, "docker-compose.yml"), "--env-file", os.path.join(STACK_DIR, ".env"),
                 "--profile", "tools", "run", "--rm", "--no-deps", "-T", "--name", f"search-job-sched-{name}",
                 "search-importer", *argv])


def rewarm() -> None:
    import psycopg
    t0 = time.time()
    with psycopg.connect(dbname="serve", autocommit=True, connect_timeout=10, application_name="search-refresh") as c:
        # serve already has pg_prewarm: the serve swap creates it before every swap.
        (n,) = c.execute("""SELECT count(*) FROM (SELECT pg_prewarm(c.oid, 'read') FROM pg_class c
                            JOIN pg_namespace n ON n.oid = c.relnamespace
                            WHERE n.nspname = 'public' AND c.relkind = 'i') x""").fetchone()
    log(f"re-warmed {n} serving indexes in {time.time() - t0:.0f}s")


def record(entry: dict) -> None:
    with open(os.path.join(STATE_DIR, "runs.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def run_job(src: dict, cfg: dict, local: dt.datetime, ignore_window: bool = False) -> bool:
    """Take the lock, check the expensive gates, run every step. True if it ran."""
    job = job_of(src)
    lock_f = open(os.path.join(STATE_DIR, "refresh.lock"), "w")
    try:
        fcntl.flock(lock_f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log(f"{src['name']}: another refresh holds the lock")
        lock_f.close()
        return False
    try:
        lock = {"busy_backends": busy_backends(), "busy_containers": busy_containers()}
        hour = cfg["night"][0] if ignore_window else local.hour
        ok, why = F.may_start(job, cfg, hour, lock)
        if not ok:
            log(f"{src['name']}: not started — {why}")
            return False
        t0 = time.time()
        outcome, tail = "ok", ""
        for step in job["steps"]:
            argv = [a.replace("{prev_month}", prev_month(local)) for a in step]
            rc, tail = compose_run(src["name"], argv)
            outcome = F.run_outcome(rc, tail)
            if outcome == "failed":
                break
        record({"source": src["name"], "t": time.time(), "at": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "outcome": outcome, "minutes": round((time.time() - t0) / 60, 1), "tail": tail[-2000:]})
        log(f"{src['name']}: {outcome} in {(time.time() - t0) / 60:.1f} min")
        if job.get("heavy"):
            rewarm()
        return True
    finally:
        fcntl.flock(lock_f, fcntl.LOCK_UN)
        lock_f.close()


def one_pass() -> None:
    cfg = load_config()
    now = time.time()
    local = dt.datetime.now(TZ)
    done = last_ok_runs()
    status = health_status()
    for src in SOURCES:
        job = job_of(src)
        if not F.job_due(job["cadence_d"] * D, done.get(src["name"]), now):
            continue
        if job.get("when") == "inputs_changed" and not inputs_changed(status):
            continue
        # Cheap gates first: nothing below touches docker or Postgres while a job is off.
        ok, why = F.may_start(job, cfg, local.hour, {})
        if not ok:
            if cfg.get("jobs", {}).get(src["name"]):
                log(f"{src['name']}: due, not started — {why}")
            continue
        if run_job(src, cfg, local):
            return          # one job per pass; the next pass re-checks every gate


def plan() -> list[str]:
    cfg = load_config()
    now = time.time()
    done = last_ok_runs()
    status = health_status()
    rows = [f"master switch: {'ON' if cfg.get('enabled') else 'off'}; night window "
            f"{cfg['night'][0]:02d}-{cfg['night'][1]:02d} {TZ.key}; now {dt.datetime.now(TZ):%Y-%m-%d %H:%M %Z}"]
    for src in SOURCES:
        job = job_of(src)
        on = cfg.get("jobs", {}).get(src["name"], False)
        ok, why = F.may_start(job, dict(cfg, enabled=True, jobs={src["name"]: True}), cfg["night"][0], {})
        if job.get("retired") or not ok or not on:
            state = "RETIRED" if job.get("retired") else ("off" if ok else "blocked")
            rows.append(f"{src['name']:<13} {state:<8} {why if not ok else 'refresh.json jobs.' + src['name'] + '=false'}")
            continue
        last = done.get(src["name"])
        due = (last or 0) + job["cadence_d"] * D
        when = F.next_start(due, now, TZ, cfg["night"], job.get("heavy", True))
        extra = ""
        if job.get("when") == "inputs_changed":
            extra = " (only if serve is behind the build data: " + ("yes" if inputs_changed(status) else "no") + ")"
        last_s = "never" if last is None else dt.datetime.fromtimestamp(last, TZ).strftime("%Y-%m-%d %H:%M")
        rows.append(f"{src['name']:<13} ON       last ok {last_s}, every {job['cadence_d']} d -> next {when:%Y-%m-%d %H:%M}{extra}")
    return rows


def seed() -> None:
    """Record each wired job's last hand load as a 'seed' run (its table's last write)."""
    status = health_status()
    have = last_ok_runs()
    for src in SOURCES:
        lw = status.get(src["name"], {}).get("last_write")
        if src["job"]["ready"] and src["name"] not in have and lw:
            record({"source": src["name"], "t": lw, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(lw)),
                    "outcome": "seed", "minutes": None, "tail": "hand load (table write time), seeded"})
            log(f"seeded {src['name']}: {time.strftime('%Y-%m-%d %H:%M', time.localtime(lw))}")


def main() -> None:
    if os.environ.get("PLAN") == "1":
        print("\n".join(plan()))
        return
    if os.environ.get("REWARM") == "1":
        rewarm()
        return
    if os.environ.get("SEED") == "1":
        seed()
        return
    if os.environ.get("RUN_JOB"):
        src = next((s for s in SOURCES if s["name"] == os.environ["RUN_JOB"]), None)
        if not src:
            sys.exit(f"no source {os.environ['RUN_JOB']}")
        run_job(src, load_config(), dt.datetime.now(TZ), ignore_window=True)
        return
    log(f"search-refresh: config {CONFIG}, every {EVERY_S}s, night window in {TZ}")
    while True:
        try:
            one_pass()
        except Exception as e:  # noqa: BLE001
            log(f"pass failed: {type(e).__name__}: {e}")
        if os.environ.get("RUN_ONCE") == "1":
            return
        time.sleep(EVERY_S)


if __name__ == "__main__":
    sys.exit(main())
