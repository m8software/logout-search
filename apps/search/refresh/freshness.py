"""Freshness rules for the search engine's data sources — pure logic, stdlib only.

Nothing here touches a database, the network or a clock: every function takes `now` and the
observations as arguments, so tests/unit/test-search-refresh.sh can drive each boundary.
health.py feeds it observations from indexhost's Postgres; scheduler.py asks it whether a job
may start.

Vocabulary
  last_write  when the table's first data file was last written (pg_stat_file). An UPPER bound
              on how new the data is: a vacuum or hint-bit flush also bumps it, so a stale table
              can look fresh, but a table whose last_write is old IS old.
  as_of       the data's own vintage where the data says it (Wikipedia dump date, Common
              Crawl crawl week, newest headline fetched). Budgets use as_of when it
              is known, last_write otherwise.
  status      ok | stale | dropped | missing | error | unknown. Only `unknown` and `ok` are quiet.
"""
from __future__ import annotations

import datetime as _dt
import re

HOUR = 3600
DAY = 86400

QUIET = ("ok", "unknown")
# Worst first: the status a source reports when several rules fire.
SEVERITY = ("error", "missing", "dropped", "stale", "unknown", "ok")


def worst(statuses):
    """The most severe of `statuses` (an empty list is `ok`)."""
    for s in SEVERITY:
        if s in statuses:
            return s
    return "ok"


# --- data vintage parsers -------------------------------------------------------------------

def wiki_dump_epoch(dump: str | None):
    """'enwiki-20260901' -> epoch seconds of that date (UTC); None when it does not parse."""
    m = re.fullmatch(r"[a-z]+wiki-(\d{4})(\d{2})(\d{2})", (dump or "").strip())
    if not m:
        return None
    try:
        d = _dt.datetime(int(m[1]), int(m[2]), int(m[3]), tzinfo=_dt.timezone.utc)
    except ValueError:
        return None
    return d.timestamp()


def cc_crawl_epoch(crawl: str | None):
    """'CC-MAIN-2026-39' -> epoch of the Monday of ISO week 39 of 2026; None when it does not parse."""
    m = re.fullmatch(r"CC-MAIN-(\d{4})-(\d{2})", (crawl or "").strip())
    if not m:
        return None
    try:
        d = _dt.datetime.fromisocalendar(int(m[1]), int(m[2]), 1).replace(tzinfo=_dt.timezone.utc)
    except ValueError:
        return None
    return d.timestamp()


# --- one source -----------------------------------------------------------------------------

def assess(src: dict, obs: dict | None, prev: dict | None, now: float) -> dict:
    """Judge one source.

    src   the registry entry (sources.py): budget_s, min_rows, drop_frac.
    obs   what health.py saw: {"error": str} | {"missing": [tables]} |
          {"rows": int (-1 = unknown), "bytes": int, "as_of": epoch|None, "last_write": epoch|None}
    prev  the previous run's verdict for this source (its rows/bytes), or None.

    Returns {"status", "reasons": [..], "age_s", "basis"} — basis says which time the age is from.
    """
    if obs and obs.get("busy"):
        # A loader holds an exclusive lock (drop/rename/index build in one transaction): routine,
        # not a fault. Quiet; the next pass reads it again.
        return {"status": "unknown", "reasons": [obs["busy"]], "age_s": None, "basis": None}
    if obs is None or obs.get("error"):
        return {"status": "error", "reasons": [f"check failed: {(obs or {}).get('error', 'no observation')}"],
                "age_s": None, "basis": None}
    if obs.get("missing"):
        return {"status": "missing", "reasons": ["table(s) not in the database: " + ", ".join(obs["missing"])],
                "age_s": None, "basis": None}

    found, reasons = [], []
    basis, t = ("as_of", obs.get("as_of")) if obs.get("as_of") is not None else ("last_write", obs.get("last_write"))
    age = None if t is None else max(0.0, now - t)
    budget = src.get("budget_s")
    if age is None:
        found.append("unknown")
        reasons.append("no as_of and no last_write: age unknown")
    elif budget is not None and age > budget:
        found.append("stale")
        reasons.append(f"{basis} is {age / DAY:.1f} days old, budget {budget / DAY:g} days")

    rows = obs.get("rows")
    rows_known = isinstance(rows, int) and rows >= 0       # pg reltuples -1 = never analysed: unknown, NOT zero
    if rows_known and rows < src.get("min_rows", 0):
        found.append("dropped")
        reasons.append(f"{rows:,} rows, floor {src['min_rows']:,}")
    if rows_known and prev and _dropped(prev, rows, obs.get("bytes"), src.get("drop_frac", 0.2)):
        found.append("dropped")
        reasons.append(f"rows fell {prev['rows']:,} -> {rows:,} and size {prev['bytes']:,} -> {obs['bytes']:,} bytes")

    return {"status": worst(found), "reasons": reasons, "age_s": age, "basis": basis}


def _dropped(prev: dict, rows: int, nbytes, frac: float) -> bool:
    """A sharp drop needs BOTH the row estimate and the size to fall by more than `frac`.

    Rows alone lie: a truncated stage table keeps its old reltuples (wiki_articles_stage: 6.8M
    rows in 16 kB on 2026-10-01), and a table being rebuilt by new-table-then-swap has a fresh,
    small estimate for a moment. Size alone lies the other way (a VACUUM FULL shrinks it).
    """
    p_rows, p_bytes = prev.get("rows"), prev.get("bytes")
    if not (isinstance(p_rows, int) and p_rows > 0 and isinstance(p_bytes, int) and p_bytes > 0):
        return False
    if not isinstance(nbytes, int):
        return False
    return rows < p_rows * (1 - frac) and nbytes < p_bytes * (1 - frac)


def next_baseline(prev: dict | None, rows, nbytes) -> dict:
    """The row/size baseline a drop is judged against: the HIGH-WATER mark of each.

    Not the previous pass: then a drop alerts once and "recovers" 15 minutes later with half the
    data still gone, and two 15% steps never add up to a drop. An unknown reading (-1 or None)
    never moves it. To accept a deliberately smaller table, delete the source's entry from
    state.json (refresh/state/ on indexhost); the next pass starts a new baseline.
    """
    prev = prev or {}
    out = {}
    for key, v in (("rows", rows), ("bytes", nbytes)):
        p = prev.get(key)
        p = p if isinstance(p, int) and p >= 0 else None
        v = v if isinstance(v, int) and v >= 0 else None
        out[key] = max(x for x in (p, v) if x is not None) if (p is not None or v is not None) else None
    return out


# --- live pollers (news DB) -----------------------------------------------------------------

def feeds_status(enabled: int, overdue: int, newest_ok_age_s, max_overdue_frac=0.25, newest_budget_s=HOUR):
    """The news poller as a whole — never one alert per feed (565 feeds; ~18% are routinely
    past 2x their interval because of back-off). `overdue` counts enabled feeds past 4x their
    poll interval + 1 h. Stalled = no feed succeeded within `newest_budget_s`, or more than
    `max_overdue_frac` of them are overdue."""
    if not enabled:
        return "missing", ["no enabled feeds"]
    reasons = []
    if newest_ok_age_s is None or newest_ok_age_s > newest_budget_s:
        reasons.append("no feed polled successfully in " + ("ever" if newest_ok_age_s is None else f"{newest_ok_age_s / 60:.0f} min"))
    if overdue / enabled > max_overdue_frac:
        reasons.append(f"{overdue} of {enabled} feeds overdue (> {max_overdue_frac:.0%})")
    return ("stale" if reasons else "ok"), reasons


def games_status(games_in_window: int, newest_update_age_s, budget_s=2 * 3600):
    """The scoreboard is season-aware: no games from yesterday to tomorrow is not a stall (off
    season, All-Star break). Games in the window that stopped updating are. sports.py re-reads an
    idle league every 30 min and backs off on errors, so the budget is four idle polls."""
    if games_in_window == 0:
        return "ok", ["no games in the window (off season?)"]
    if newest_update_age_s is None or newest_update_age_s > budget_s:
        age = "never" if newest_update_age_s is None else f"{newest_update_age_s / 60:.0f} min ago"
        return "stale", [f"{games_in_window} games in the window, last update {age}"]
    return "ok", []


# --- alerting -------------------------------------------------------------------------------

def alert_action(prev: dict | None, status: str, now: float, realert_s: float = DAY):
    """Decide what to push for one source, from its previous alert state.

    Pushes on a transition into a bad status, on a change between bad statuses, and again every
    `realert_s` while it stays bad; a recovery when it goes back to quiet. Never repeats a push
    within `realert_s` for the same status (lib/notify.sh's own dedup forgets after 60 min).

    Returns (action, new_state): action is "alert" | "recover" | None.
    """
    prev = prev or {}
    was_bad = prev.get("status") not in (None, *QUIET)
    bad = status not in QUIET
    if bad:
        if not was_bad or prev.get("status") != status or now - prev.get("sent", 0) >= realert_s:
            return "alert", {"status": status, "sent": now}
        return None, {"status": status, "sent": prev.get("sent", now)}
    if was_bad:
        return "recover", {"status": status, "sent": now}
    return None, {"status": status, "sent": prev.get("sent", 0)}


# --- scheduling -----------------------------------------------------------------------------

def in_night_window(local_hour: int, start: int = 1, end: int = 6) -> bool:
    """True in [start, end) local hours; wraps midnight when start > end (e.g. 23 -> 5)."""
    if start == end:
        return False
    if start < end:
        return start <= local_hour < end
    return local_hour >= start or local_hour < end


def job_due(cadence_s: float, last_ok, now: float) -> bool:
    """Due when it never succeeded, or its last success is at least one cadence ago."""
    return last_ok is None or now - last_ok >= cadence_s


def may_start(job: dict, cfg: dict, local_hour: int, lock: dict):
    """Every gate a scheduled job must pass. Returns (ok, reason).

    job   {"name", "approval": str|None, "ready": bool, "heavy": bool}
    cfg   {"enabled": bool, "jobs": {name: bool}, "approved": {name: bool}, "night": [start, end]}
    lock  {"flock": bool (another scheduler run holds it), "busy_backends": int,
           "busy_containers": [names]}
    Order matters only for the reason shown; every gate must pass.
    """
    if job.get("retired"):
        return False, "retired: " + job["retired"]
    if not cfg.get("enabled", False):
        return False, "scheduler disabled (refresh.json enabled=false)"
    if not cfg.get("jobs", {}).get(job["name"], False):
        return False, "job disabled (refresh.json jobs.%s=false)" % job["name"]
    if job.get("approval") and not cfg.get("approved", {}).get(job["name"], False):
        return False, "needs the user's approval: " + job["approval"]
    if not job.get("ready", False):
        return False, "not wired for unattended runs yet"
    night = cfg.get("night", [1, 6])
    if job.get("heavy", True) and not in_night_window(local_hour, night[0], night[1]):
        return False, f"outside the night window {night[0]:02d}-{night[1]:02d} local"
    if lock.get("flock"):
        return False, "another refresh job holds the lock"
    if lock.get("busy_backends", 0) > 0:
        return False, f"build database busy ({lock['busy_backends']} sessions)"
    if lock.get("busy_containers"):
        return False, "heavy containers running: " + ", ".join(lock["busy_containers"])
    return True, "ok"


def busy_from_stats(stats, patterns, cpu_min: float = 5.0) -> list:
    """Containers that mean "a heavy job is running": the name matches one of `patterns` AND it
    is using CPU. Name alone is not enough: another session's bench Postgres (search-bitbench)
    sat idle for days and would have held every job back."""
    out = []
    for name, cpu in stats:
        if any(re.search(p, name) for p in patterns) and cpu >= cpu_min:
            out.append(name)
    return out


def next_start(due: float, now: float, tz, night=(1, 6), heavy: bool = True) -> _dt.datetime:
    """The earliest local time a job may start: when it is due, moved into the next night
    window if it is heavy."""
    t = _dt.datetime.fromtimestamp(max(due, now), tz)
    if not heavy or in_night_window(t.hour, night[0], night[1]):
        return t
    start = t.replace(hour=night[0], minute=0, second=0, microsecond=0)
    if start <= t:
        start = (start + _dt.timedelta(days=1)).replace(hour=night[0])
    return start


def run_outcome(exit_code: int, log_tail: str) -> str:
    """ok | partial | failed. The importers log 'FAILED … kept as before' and still exit 0 when one
    QLever category or fact is refused (entities.py, facts.py) — that is a partial load, and it was
    invisible all day on 2026-09-25 (celebrity)."""
    if exit_code != 0:
        return "failed"
    if re.search(r"\bFAILED\b", log_tail or ""):
        return "partial"
    return "ok"
