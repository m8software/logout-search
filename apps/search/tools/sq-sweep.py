#!/usr/bin/env python3
"""The constants sweep of the search-quality epic: one engine arm per constant value, each
measured on DEV halves against a control arm run on the same code, with the acceptance rule in the output.

    tools/sq-sweep.py SPEC.json --dry-run            # the arm list, the count, the estimated wall time; starts nothing
    tools/sq-sweep.py SPEC.json                      # run what is not done yet, then write the report
    tools/sq-sweep.py SPEC.json --report-only        # score what has run; no engine
    tools/sq-sweep.py --same-answers BUILD_A BUILD_B [--sets a,b]    # do two builds' cached answers differ? no engine

  --only A,B        only these arms (short names as --dry-run prints them); the control is added when it has not run
  --max-arms N      stop after N arms of this call (fit a slot window)
  --lock-wait SEC   how long one arm may wait for the engine slot (default 14400); past it the sweep exits 3
  --keep-going      an arm that fails is recorded and the next one starts (default: the sweep stops, exit 2)
  --release DIR     the release copy (default: the spec's "release")
  --port N          the engine's host port (default 18473)

What one arm is: `sq-engine.sh up <release> --name <arm> --env K=V ...`, then `sq-eval.py --arm <arm> --sets ...`
(dev halves: this tool has no way to pass --test and refuses a spec that names one), then `sq-engine.sh down`.
ONE ENGINE AT A TIME, in file order. Before every `up` the tool waits, bounded, until the engine lock
(~/.cache/logout/sq-engine.lock, SQ_LOCK) is free AND podman shows no sq-* container; after every `down` it checks
the same and stops the sweep if something is left. It never looks processes up by pattern.

Resumable: ~/.local/share/logout-search/sq/sweeps/<spec name>/state.json records each finished arm; an arm that
finished with the same settings and sets is not run again. Stop file: `touch <that dir>/STOP` — the sweep ends
before the next arm (the running arm finishes and its engine goes down); remove the file to resume.

The cache: sq-eval.py caches by (engine build, set, query) and sq-engine.sh's build hash covers every --env
setting, so an arm that sets a constant is a build of its own — NOTHING of it is cached before its first run, every
query of its sets is asked. What the cache does give: an interrupted arm resumes at the set it stopped in, a
finished one re-scores for free, and the control (no --env) shares the build of any earlier run of the same code.
That is why a constant's default is never a grid value (it would re-ask the control under another hash) and why
each constant is limited to the sets it can move. --dry-run reads the caches and counts what is left to ask.
If the API code or the release manifest changes after the control ran, the sweep refuses to go on: arms on two
codes are not comparable. Use a new spec name.

The spec (JSON; unknown keys are refused):
  {"name": "sw473-s1", "issue": 473, "release": "~/search-eval/rc8-acc", "improve": "pooled",
   "base": {"SEARCH_TYPO_NAMES": "links,places"},
   "groups": [{"name": "typo", "why": "...", "sets": ["typo-site", "site-nav"], "target": ["typo-site"],
               "constants": {"SEARCH_TYPO_SIM": {"default": "0.25", "values": ["0.2", "0.3"]}}}],
   "arms":   [{"name": "combo", "env": {"SEARCH_TYPO_SIM": "0.2", "SEARCH_RRF_K": "40"}, "sets": [...], "target": [...]}]}
"base" is the configuration the sweep starts from: settings every arm carries, the control too (the control is
then a build with settings of its own, cached like any arm). A group is a one-at-a-time design: each value of each constant is one arm with that one setting. "arms" are
explicit (stage two: the best values combined). "sets" is what the arm asks; "target" the sets it is meant to move;
the rest guard. Every constant must be read by the API code (os.environ.get("<K>", ...) in serve/api/*.py) and its
"default" must be the code's: a setting the engine does not read would be a second control under another name.

Judged sets (a set named judged-*): no Clef call is ever made here. The arm's first result is looked up in
the set's frozen <set>.verdicts.tsv (query, url); hit@1 = a verdict of at least 0.7, paired only over the queries
whose first result has a verdict in both arms. A first result nobody judged is counted and written to
unjudged.<arm>.tsv — what a Clef run would have to judge before such an arm can be believed. hit@5 is not scored.
An arm whose set or verdict file is not in this checkout is BLOCKED and skipped, not failed.

THE ACCEPTANCE RULE (per arm, against the control; 95% paired bootstrap, 10,000 resamples):
  candidate = the target sets improve with an interval that excludes 0
              ("improve": "pooled" — the target sets' rows pooled, the default; "every" — each target set alone)
              AND no set of the arm, target or guard, on hit@1 or hit@5, drops by more than 0.5 pt with an interval
              that excludes 0.
For a candidate the report lists every regressed query by name. Twenty-odd arms at 95% will show a false
candidate now and then: stage two re-measures the candidates on every set, and the test halves are run once, at
the very end, by sq-eval.py --test on the owner's word — never from here.

Writes sweeps/<name>/report.md, report.tsv, regressed.<arm>.tsv. DEV TOOLING — nothing here runs on a box.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import shutil
import signal
import statistics
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sq_common as C  # noqa: E402

REPO = C.TOOLS.parents[2]
API = REPO / "apps" / "search" / "serve" / "api"
LOCK = Path(os.environ.get("SQ_LOCK") or Path.home() / ".cache/logout/sq-engine.lock")
POLL = float(os.environ.get("SQ_SWEEP_POLL", "15"))            # seconds between looks at the slot
GONE_WAIT = float(os.environ.get("SQ_SWEEP_GONE_WAIT", "90"))  # after `down`: how long the slot may take to clear
UP_TIMEOUT = float(os.environ.get("SQ_SWEEP_UP_TIMEOUT", "900"))
SEC_PER_QUERY = 0.34       # 2026-10-13, this workstation, 4 GB / 4 cpus, 3 in flight: 1,669-2,351 s for 5,746 queries
UP_SECONDS = 120.0         # an assumption until the first arm of a sweep has measured it
N_BOOT = 10_000
DROP_MAX = 0.005           # compare-evals.py's 0.5 pt
RIGHT = 0.7                # judged-info-judge.py's "this result is right"
SPEC_KEYS = {"name", "issue", "release", "improve", "base", "groups", "arms", "note"}
GROUP_KEYS = {"name", "why", "sets", "target", "constants", "metric"}
ARM_KEYS = {"name", "why", "env", "sets", "target", "metric"}
EXIT_FAILED, EXIT_BUSY, EXIT_LEFTOVER, EXIT_CODE_MOVED = 2, 3, 4, 5


def log(msg: str) -> None:
    print(f"[sq-sweep {time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


def die(msg: str, code: int = 1) -> None:
    print(f"sq-sweep: {msg}", file=sys.stderr, flush=True)
    sys.exit(code)


def tool(name: str) -> str:
    """sq-engine.sh / sq-eval.py: one on PATH wins (the tests put fakes there), else the one beside this file."""
    return shutil.which(name) or str(C.TOOLS / name)


def safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", name).strip("-.")


# ---------------------------------------------------------------- the spec
def env_defaults(api: Path) -> dict:
    """{setting: default or None} for every os.environ.get("K"[, "D"]) in the API's *.py."""
    out: dict = {}
    for p in sorted(api.glob("*.py")):
        for m in re.finditer(r"""os\.environ\.get\(\s*["']([A-Z][A-Z0-9_]*)["']\s*(?:,\s*["']([^"']*)["'])?""", p.read_text(encoding="utf-8")):
            out.setdefault(m.group(1), m.group(2))
    return out


def load_spec(path: Path, api: Path) -> dict:
    try:
        spec = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        die(f"{path}: {e}")
    bad = set(spec) - SPEC_KEYS
    if bad or not isinstance(spec.get("name"), str) or safe(spec["name"]) != spec["name"]:
        die(f"{path}: unknown keys {sorted(bad)} or no plain \"name\" (letters, digits, . _ -)")
    if spec.get("improve", "pooled") not in ("pooled", "every"):
        die(f"{path}: \"improve\" is pooled or every")
    reads = env_defaults(api)
    arms, seen = [], set()
    base = spec.get("base", {})
    for k, v in base.items():
        if k not in reads or not isinstance(v, str):
            die(f"{path}: \"base\": the API code in {api} never reads {k}, or its value is not a string")

    def add(short, env, sets, target, group, metric, why):
        if not sets or not target or not set(target) <= set(sets):
            die(f"{path}: arm {short}: \"target\" must be a non-empty part of \"sets\"")
        if metric not in ("hit1", "hit5"):
            die(f"{path}: arm {short}: \"metric\" is hit1 or hit5")
        for k, v in env.items():
            if k not in reads:
                die(f"{path}: arm {short}: the API code in {api} never reads {k} — the arm would be the control under "
                    "another name")
            if not isinstance(v, str):
                die(f"{path}: arm {short}: {k} must be a string, as the environment is")
        if short in seen:
            die(f"{path}: two arms named {short}")
        if set(env) & set(base):
            die(f"{path}: arm {short}: {sorted(set(env) & set(base))} is a base setting — every arm, the control too, carries it")
        seen.add(short)
        arms.append({"short": short, "name": f"{spec['name']}.{short}", "env": dict(sorted({**base, **env}.items())),
                     "own": dict(sorted(env.items())), "sets": list(sets),
                     "target": list(target), "group": group, "metric": metric, "why": why})

    for g in spec.get("groups", []):
        if set(g) - GROUP_KEYS or not g.get("constants"):
            die(f"{path}: group {g.get('name')!r}: unknown keys {sorted(set(g) - GROUP_KEYS)} or no \"constants\"")
        for k, c in g["constants"].items():
            if set(c) != {"default", "values"} or not c["values"]:
                die(f"{path}: {k}: wants exactly \"default\" and a non-empty \"values\"")
            if k in reads and reads[k] is not None and reads[k] != c["default"]:
                die(f"{path}: {k}: the spec says the default is {c['default']!r}, the API code says {reads[k]!r}")
            if c["default"] in c["values"]:
                die(f"{path}: {k}: the default {c['default']!r} is a grid value — the control arm is that measurement")
            for v in c["values"]:
                add(safe(f"{g['name']}.{k.removeprefix('SEARCH_')}={v}".replace("=", "_")), {k: v}, g.get("sets", []),
                    g.get("target", []), g["name"], g.get("metric", "hit1"), g.get("why", ""))
    for a in spec.get("arms", []):
        if set(a) - ARM_KEYS or not a.get("env") or safe(a.get("name", "")) != a.get("name"):
            die(f"{path}: arm {a.get('name')!r}: unknown keys {sorted(set(a) - ARM_KEYS)}, no \"env\", or a name that is not plain")
        add(a["name"], a["env"], a.get("sets", []), a.get("target", []), "arms", a.get("metric", "hit1"), a.get("why", ""))
    if not arms:
        die(f"{path}: no arms")
    if any(a["short"] == "ctl" for a in arms):
        die(f"{path}: \"ctl\" is the control arm's name")
    spec["_arms"] = arms
    return spec


def set_missing(s: str) -> str | None:
    """Why this set cannot be measured in this checkout, or None."""
    if not (C.OPEN / f"{s}.tsv").exists():
        return f"{C.OPEN}/{s}.tsv is not here"
    if s.startswith("judged-") and not (C.OPEN / f"{s}.verdicts.tsv").exists():
        return f"{C.OPEN}/{s}.verdicts.tsv is not here"
    return None


def plan(spec: dict) -> list[dict]:
    """The arms in run order, the control first, each with "blocked" (why, or None). The control asks every set a
    runnable arm asks."""
    arms = []
    for a in spec["_arms"]:
        why = next((w for w in map(set_missing, a["sets"]) if w), None)
        arms.append(dict(a, blocked=why))
    union = [s for s in dict.fromkeys(s for a in arms if not a["blocked"] for s in a["sets"])]
    order = [s for s in C.DEFAULT_SETS if s in union] + [s for s in union if s not in C.DEFAULT_SETS]
    ctl = {"short": "ctl", "name": f"{spec['name']}.ctl", "env": dict(sorted(spec.get("base", {}).items())), "own": {}, "sets": order, "target": [], "group": "control",
           "metric": "hit1", "why": "the same code with every default", "blocked": None if order else "no runnable arm"}
    return [ctl] + arms


# ---------------------------------------------------------------- builds, the cache, the estimate
def build_hash(api: Path, release: Path, env: dict) -> str | None:
    """sq-engine.sh's build_hash, in Python: the API's *.py in byte order, the release manifest, and for an arm with
    settings its sorted K=V lines and the API dir's ranker.json. None when the release copy is not here."""
    man = release / "manifest.json"
    if not man.exists():
        return None
    h = hashlib.sha256()
    for p in sorted((p for p in api.glob("*.py") if p.is_file()), key=lambda p: os.fsencode(str(p))):
        h.update(p.read_bytes())
    h.update(man.read_bytes())
    if env:
        for line in sorted((f"{k}={v}\n".encode() for k, v in env.items())):
            h.update(line)
        if (api / "ranker.json").is_file():
            h.update((api / "ranker.json").read_bytes())
    return h.hexdigest()[:12]


def cached_queries(build: str | None, s: str) -> set:
    path = C.SQ_DIR / "cache" / (build or "-") / f"{s}.jsonl"
    have = set()
    if build and path.exists():
        for line in open(path, encoding="utf-8"):
            if line.strip():
                try:
                    o = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if "error" not in o:
                    have.add(o.get("q"))
    return have


def to_ask(arm: dict, build: str | None) -> tuple[int, int]:
    """(queries of the arm's sets, how many of them are not in the build's cache)."""
    n = left = 0
    for s in arm["sets"]:
        qs = list(dict.fromkeys(r["query"] for r in C.load_set(s, "dev")))
        have = cached_queries(build, s)
        n += len(qs)
        left += sum(q not in have for q in qs)
    return n, left


def rates(state: dict) -> tuple[float, float, str]:
    """(seconds per query, seconds to start an engine, where the numbers come from)."""
    done = [a for a in state.get("arms", {}).values() if a.get("status") == "done" and a.get("asked", 0) >= 200]
    up = [a["up_s"] for a in state.get("arms", {}).values() if a.get("up_s")]
    up_s = statistics.median(up) if up else UP_SECONDS
    up_src = f"engine start {up_s:.0f} s (" + ("measured in this sweep" if up else "assumed") + ")"
    if done:
        return sum(a["eval_s"] for a in done) / sum(a["asked"] for a in done), up_s, f"{len(done)} arms of this sweep; {up_src}"
    seen = []
    for p in C.SQ_DIR.glob("*/run.json"):
        try:
            d = json.loads(p.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if d.get("asked", 0) >= 500 and d.get("seconds", 0) > 0 and not d.get("test"):
            seen.append(d["seconds"] / d["asked"])
    if seen:
        return statistics.median(seen), up_s, f"the median of {len(seen)} earlier sq-eval runs on this machine; {up_src}"
    return SEC_PER_QUERY, up_s, f"the 2026-10-13 measurement ({SEC_PER_QUERY} s a query); {up_src}"


# ---------------------------------------------------------------- state
def sweep_dir(spec: dict) -> Path:
    return C.SQ_DIR / "sweeps" / spec["name"]


def load_state(spec: dict) -> dict:
    p = sweep_dir(spec) / "state.json"
    if p.exists():
        return json.loads(p.read_text())
    return {"spec": spec["name"], "code": None, "arms": {}}


def save_state(spec: dict, state: dict) -> None:
    d = sweep_dir(spec)
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / "state.json.tmp"
    tmp.write_text(json.dumps(state, indent=1))
    os.replace(tmp, d / "state.json")


def is_done(arm: dict, state: dict) -> bool:
    """Finished with these settings and these sets, and its per-query rows are still on disk."""
    rec = state.get("arms", {}).get(arm["short"]) or {}
    return (rec.get("status") == "done" and rec.get("env") == arm["env"] and rec.get("sets") == arm["sets"]
            and all((C.SQ_DIR / arm["name"] / f"{s}.dev.tsv").exists() for s in arm["sets"]))


# ---------------------------------------------------------------- the engine slot
def lock_holder() -> str:
    try:
        return LOCK.read_text().strip()
    except OSError:
        return ""


def sq_containers() -> list[str]:
    """Every sq-* container, running or not. podman missing or failing is an error: the slot cannot be judged free."""
    try:
        r = subprocess.run(["podman", "ps", "-a", "--filter", "name=^sq-", "--format", "{{.Names}}"],
                           capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        die(f"podman ps did not answer ({type(e).__name__}): cannot tell whether an engine is up", EXIT_LEFTOVER)
    if r.returncode:
        die(f"podman ps failed: {r.stderr.strip()[-200:]}", EXIT_LEFTOVER)
    return [x for x in r.stdout.split() if x]


def slot_busy() -> str:
    """'' when the lock is free and no sq-* container exists; else who is there."""
    held = lock_holder()
    if held:
        return f"the engine lock is held ({held})"
    left = sq_containers()
    return f"sq-* container(s) still here: {', '.join(left)}" if left else ""


def wait_slot(seconds: float, stop: Path) -> str:
    """Wait, at most `seconds`, for the slot. -> "free", "stop" (the stop file appeared) or "busy" (time is up)."""
    end = time.monotonic() + seconds
    told = 0.0
    while True:
        if stop.exists():
            return "stop"
        busy = slot_busy()
        if not busy:
            return "free"
        left = end - time.monotonic()
        if left <= 0:
            log(f"the slot did not come free in {seconds:.0f} s: {busy}")
            return "busy"
        if time.monotonic() - told >= 600 or not told:
            log(f"waiting for the engine slot (up to {left / 60:.0f} min more): {busy}")
            told = time.monotonic()
        time.sleep(min(POLL, max(0.05, left)))


def slot_cleared(seconds: float) -> str:
    """After `down`: '' once the lock is free and no sq-* container is left, else what is still there."""
    end = time.monotonic() + seconds
    while True:
        busy = slot_busy()
        if not busy or time.monotonic() >= end:
            return busy
        time.sleep(min(POLL, 1.0))


# ---------------------------------------------------------------- one arm
def run_arm(arm: dict, spec: dict, release: Path, port: int, control: str) -> dict:
    """up, eval on the dev halves, down. -> the state record. The engine is taken down whatever happens."""
    ctr = safe(arm["name"])
    engine, evaler = tool("sq-engine.sh"), tool("sq-eval.py")
    up = [engine, "up", str(release), "--port", str(port), "--name", ctr, "--issue", str(spec.get("issue", "?"))]
    for k, v in arm["env"].items():
        up += ["--env", f"{k}={v}"]
    ev = [evaler, "--arm", arm["name"], "--engine", f"http://127.0.0.1:{port}", "--sets", ",".join(arm["sets"])]
    if arm["short"] != "ctl":
        ev += ["--baseline", control]
    assert "--test" not in ev and "--both" not in ev and "--force" not in ev       # dev halves, always
    rec = {"env": arm["env"], "sets": arm["sets"], "when": time.strftime("%Y-%m-%dT%H:%M:%S"), "status": "failed"}
    n_queries = sum(len(C.load_set(s, "dev")) for s in arm["sets"])
    t0 = time.monotonic()
    try:
        log(f"arm {arm['short']}: {' '.join(up[1:])}")
        try:
            r = subprocess.run(up, capture_output=True, text=True, timeout=UP_TIMEOUT)
        except subprocess.TimeoutExpired:
            rec["error"] = f"sq-engine.sh up did not return in {UP_TIMEOUT:.0f} s"
            return rec
        if r.returncode:
            rec["error"] = "sq-engine.sh up failed: " + r.stderr.strip()[-400:]
            return rec
        rec["build"] = (r.stdout.strip().splitlines() or ["?"])[-1]
        rec["up_s"] = round(time.monotonic() - t0)
        t1 = time.monotonic()
        try:
            r = subprocess.run(ev, capture_output=True, text=True, timeout=600 + 6 * n_queries)
        except subprocess.TimeoutExpired:
            rec["error"] = f"sq-eval.py did not return in {600 + 6 * n_queries} s"
            return rec
        rec["eval_s"] = round(time.monotonic() - t1)
        if r.returncode:
            rec["error"] = "sq-eval.py failed: " + r.stderr.strip()[-400:]
            return rec
        failed = sum(int(n) for n in re.findall(r"(\d+) queries failed", r.stderr))
        run = {}
        try:
            run = json.loads((C.SQ_DIR / arm["name"] / "run.json").read_text())
        except (OSError, json.JSONDecodeError):
            pass
        rec.update(asked=run.get("asked", 0), cached=run.get("cached", 0))
        if run.get("test"):
            rec["error"] = "sq-eval.py says it scored TEST halves — this arm is not used"      # cannot happen from here
            return rec
        if failed:
            rec["error"] = f"{failed} queries got no answer (scored as misses, not cached): run again, only they are asked"
            return rec
        missing = [s for s in arm["sets"] if not (C.SQ_DIR / arm["name"] / f"{s}.dev.tsv").exists()]
        if missing:
            rec["error"] = f"sq-eval.py wrote no rows for {', '.join(missing)}"
            return rec
        rec["status"] = "done"
        return rec
    finally:
        d = subprocess.run([engine, "down", "--name", ctr], capture_output=True, text=True)
        if d.returncode:
            log(f"arm {arm['short']}: sq-engine.sh down failed: {d.stderr.strip()[-300:]}")


# ---------------------------------------------------------------- scoring
def boot(diffs: list[float], seed: int = 7) -> tuple[float, float]:
    """compare-evals.py's paired bootstrap (percentile, 10,000 resamples of the per-query differences), drawn with
    random.choices so twenty arms score in seconds. Not the same random stream: the two agree to sampling noise."""
    n = len(diffs)
    if not any(diffs):
        return 0.0, 0.0
    rnd = random.Random(seed)
    means = sorted(sum(rnd.choices(diffs, k=n)) / n for _ in range(N_BOOT))
    return means[int(0.025 * N_BOOT)], means[int(0.975 * N_BOOT) - 1]


def compare(a: dict, b: dict, metric: str) -> dict | None:
    """Control a, arm b: {key: {metric: 0/1}} -> n paired, both means, b - a, its interval."""
    qs = sorted(q for q in set(a) & set(b) if metric in a[q] and metric in b[q])
    if not qs:
        return None
    d = [b[q][metric] - a[q][metric] for q in qs]
    lo, hi = boot(d)
    return {"n": len(qs), "a": sum(a[q][metric] for q in qs) / len(qs), "b": sum(b[q][metric] for q in qs) / len(qs),
            "delta": sum(d) / len(d), "lo": lo, "hi": hi}


def improves(c: dict | None) -> bool:
    return c is not None and c["delta"] > 0 and c["lo"] > 0


def regresses(c: dict | None) -> bool:
    return c is not None and c["delta"] < -DROP_MAX and c["hi"] < 0


def verdicts_of(s: str) -> dict:
    """{(query key, url): score} from a judged set's frozen verdict file."""
    out = {}
    with open(C.OPEN / f"{s}.verdicts.tsv", encoding="utf-8") as f:
        cols = None
        for line in f:
            if line.startswith("#") or not line.strip():
                continue
            parts = line.rstrip("\n").split("\t")
            if cols is None:
                cols = parts
                continue
            r = dict(zip(cols, parts))
            try:
                out[(r["key"], r["url"])] = float(r["score"])
            except (KeyError, ValueError):
                continue
    return out


def set_hits(arm_name: str, s: str) -> tuple[dict, list]:
    """One arm's rows of one set -> ({key: {"hit1", "hit5", "rank", "top", "q"}}, unjudged [(query, url)]).
    The key is sq-eval.py's: the query, with #2, #3 on a query the set asks more than once."""
    out, unjudged, seen = {}, [], {}
    verdict = verdicts_of(s) if s.startswith("judged-") else None
    with open(C.SQ_DIR / arm_name / f"{s}.dev.tsv", encoding="utf-8") as f:
        next(f)
        for line in f:
            if not line.strip():
                continue
            q, _cat, rank, _score, top, *_ = line.rstrip("\n").split("\t")
            seen[q] = seen.get(q, 0) + 1
            key = q if seen[q] == 1 else f"{q}\t#{seen[q]}"
            if verdict is not None:
                if not top:
                    out[key] = {"hit1": 0.0, "rank": "-", "top": "", "q": q}       # an empty list answers nothing
                    continue
                v = verdict.get((C.norm_query(q), top))
                if v is None:
                    unjudged.append((q, top))
                    continue
                out[key] = {"hit1": float(v >= RIGHT), "rank": "1" if v >= RIGHT else "-", "top": top, "q": q}
            else:
                r = int(rank) if rank != "-" else 99
                out[key] = {"hit1": float(r == 1), "hit5": float(r <= 5), "rank": rank, "top": top, "q": q}
    return out, unjudged


def judge_arm(arm: dict, control: str, improve: str) -> dict:
    """The arm against the control, set by set, and the acceptance rule. -> {"rows", "candidate", "why", "regressed"}"""
    rows, pooled_a, pooled_b, regressed, unjudged = [], {}, {}, [], []
    for s in arm["sets"]:
        a, _ua = set_hits(control, s)
        b, ub = set_hits(arm["name"], s)
        unjudged += [(s, q, u) for q, u in ub]
        c1, c5 = compare(a, b, "hit1"), (None if s.startswith("judged-") else compare(a, b, "hit5"))
        role = "target" if s in arm["target"] else "guard"
        if role == "target":
            for q in set(a) & set(b):
                pooled_a[f"{s}\t{q}"], pooled_b[f"{s}\t{q}"] = a[q], b[q]
        for q in sorted(set(a) & set(b)):
            lost1 = a[q]["hit1"] > b[q]["hit1"]
            lost5 = a[q].get("hit5", 0) > b[q].get("hit5", 0)
            if lost1 or lost5:
                regressed.append((s, role, a[q]["q"], a[q]["rank"], b[q]["rank"], "hit@1" if lost1 else "hit@5 only", b[q]["top"]))
        rows.append({"set": s, "role": role, "n": len(b), "c1": c1, "c5": c5,
                     "gained": sum(b[q]["hit1"] > a[q]["hit1"] for q in set(a) & set(b)),
                     "lost": sum(a[q]["hit1"] > b[q]["hit1"] for q in set(a) & set(b)), "unjudged": len(ub)})
    m = arm["metric"]
    pooled = compare(pooled_a, pooled_b, m)
    targets = [r for r in rows if r["role"] == "target"]
    if improve == "every":
        gain = bool(targets) and all(improves(r["c1"] if m == "hit1" else r["c5"]) for r in targets)
    else:
        gain = improves(pooled)
    drops = [f"{r['set']} {name} {100 * c['delta']:+.1f} pt [{100 * c['lo']:+.1f}, {100 * c['hi']:+.1f}]"
             for r in rows for name, c in (("hit@1", r["c1"]), ("hit@5", r["c5"])) if regresses(c)]
    if drops:
        why = "a real drop: " + "; ".join(drops)
    elif not gain:
        why = ("no gain the interval separates from 0 on the target sets" if pooled is not None
               else "nothing to pair on the target sets")
    else:
        why = "the target sets gain and no set drops"
    return {"rows": rows, "pooled": pooled, "candidate": gain and not drops, "why": why, "regressed": regressed,
            "unjudged": unjudged}


def pt(c: dict | None, key: str = "delta") -> str:
    return "" if c is None else f"{100 * c[key]:+.1f}" if key == "delta" else f"{100 * c[key]:.1f}"


def ci(c: dict | None) -> str:
    return "" if c is None else f"[{100 * c['lo']:+.1f}, {100 * c['hi']:+.1f}]"


def report(spec: dict, arms: list[dict], state: dict) -> str:
    d = sweep_dir(spec)
    d.mkdir(parents=True, exist_ok=True)
    ctl = arms[0]
    improve = spec.get("improve", "pooled")
    out = [f"## sq-sweep `{spec['name']}` — dev halves, keys `{C.KEYS}`, against the control arm `{ctl['name']}`", ""]
    tsv = ["arm\tsettings\tset\trole\tn\tcontrol hit@1\thit@1\tΔ hit@1 pt\t95% CI\tΔ hit@5 pt\t95% CI hit@5\tgained\tlost\tunjudged\tcandidate"]
    if not is_done(ctl, state):
        out.append("The control arm has not finished: nothing can be paired yet.")
        out += [f"- {a['short']}: BLOCKED — {a['blocked']}" for a in arms[1:] if a["blocked"]]
        (d / "report.md").write_text("\n".join(out) + "\n")
        return "\n".join(out) + "\n"
    rec = state["arms"]["ctl"]
    base = " ".join(f"{k}={v}" for k, v in ctl["env"].items())
    out += [f"Base settings on every arm, the control included: `{base or 'none (every default)'}`.", "",
            f"Control: build `{rec.get('build')}`, {len(ctl['sets'])} sets. Rule: an arm is a **candidate** when its target sets "
            f"improve with a 95% interval that excludes 0 ({improve}) and no set of the arm, on hit@1 or hit@5, drops by "
            "more than 0.5 pt with an interval that excludes 0. Paired bootstrap, 10,000 resamples.", "",
            "| arm | settings | verdict | target Δ pt [95% CI] | why |", "|---|---|---|---|---|"]
    detail, cands, pending = [], [], []
    for a in arms[1:]:
        sett = " ".join(f"{k}={v}" for k, v in a["own"].items())
        if a["blocked"]:
            out.append(f"| {a['short']} | `{sett}` | BLOCKED | | {a['blocked']} |")
            continue
        if not is_done(a, state):
            st = (state["arms"].get(a["short"]) or {})
            out.append(f"| {a['short']} | `{sett}` | {'FAILED' if st.get('status') == 'failed' else 'not run'} | | {st.get('error', '')[:160]} |")
            pending.append(a["short"])
            continue
        j = judge_arm(a, ctl["name"], improve)
        out.append(f"| {a['short']} | `{sett}` | {'**CANDIDATE**' if j['candidate'] else 'no'} | "
                   f"{pt(j['pooled'])} {ci(j['pooled'])} | {j['why']} |")
        detail += ["", f"### {a['short']} — `{sett}` — {'CANDIDATE' if j['candidate'] else 'not a candidate'}: {j['why']}", "",
                   "| set | role | n | control hit@1 | hit@1 | Δ hit@1 pt | 95% CI | Δ hit@5 pt | 95% CI | gained | lost | unjudged |",
                   "|---|---|---:|---:|---:|---:|---|---:|---|---:|---:|---:|"]
        for r in j["rows"]:
            c1, c5 = r["c1"], r["c5"]
            cells = [r["set"], r["role"], str(c1["n"] if c1 else r["n"]), pt(c1, "a"), pt(c1, "b"), pt(c1), ci(c1), pt(c5), ci(c5),
                     str(r["gained"]), str(r["lost"]), str(r["unjudged"])]
            detail.append("| " + " | ".join(cells) + " |")
            tsv.append("\t".join([a["short"], sett] + cells + [str(int(j["candidate"]))]))
        if j["unjudged"]:
            with open(d / f"unjudged.{a['short']}.tsv", "w", encoding="utf-8") as f:
                f.write("set\tquery\turl\n" + "".join("\t".join(x) + "\n" for x in j["unjudged"]))
            detail.append(f"\n{len(j['unjudged'])} first results have no frozen verdict and are left out of the judged "
                          f"rows (`unjudged.{a['short']}.tsv`): the judged delta covers only results the judge has seen.")
        if j["candidate"]:
            cands.append(a["short"])
            with open(d / f"regressed.{a['short']}.tsv", "w", encoding="utf-8") as f:
                f.write("set\trole\tquery\tcontrol rank\tarm rank\tlost\tarm first url\n"
                        + "".join("\t".join(x) + "\n" for x in j["regressed"]))
            detail += ["", f"Regressed queries ({len(j['regressed'])}; `regressed.{a['short']}.tsv`):", ""]
            detail += [f"- `{s}` ({role}) **{q}** — rank {ra} → {rb} ({lost}); now first: {top or '(nothing)'}"
                       for s, role, q, ra, rb, lost, top in j["regressed"]] or ["- none"]
    out += ["", f"Candidates: {', '.join(cands) if cands else 'none'}." + (f" Not run yet: {', '.join(pending)}." if pending else "")]
    out += detail
    md = "\n".join(out) + "\n"
    (d / "report.md").write_text(md)
    (d / "report.tsv").write_text("\n".join(tsv) + "\n")
    return md


# ---------------------------------------------------------------- --same-answers
def same_answers(build_a: str, build_b: str, sets: list[str]) -> int:
    """Do two builds' cached answers differ? Prints per set: queries both answered, lists whose URLs differ in order,
    lists with the same URLs and another score. -> how many lists differ, in order or in a score."""
    total = 0
    print("set\tin both\turl lists differ\tscores differ\tfirst difference")
    for s in sets:
        def load(b):
            p, out = C.SQ_DIR / "cache" / b / f"{s}.jsonl", {}
            if p.exists():
                for line in open(p, encoding="utf-8"):
                    if line.strip():
                        o = json.loads(line)
                        out[o["q"]] = o.get("results") or []
            return out
        a, b = load(build_a), load(build_b)
        both = sorted(set(a) & set(b))
        urls = [q for q in both if [x.get("url") for x in a[q]] != [x.get("url") for x in b[q]]]
        scores = [q for q in both if q not in urls and [x.get("score") for x in a[q]] != [x.get("score") for x in b[q]]]
        total += len(urls) + len(scores)
        print(f"{s}\t{len(both)}\t{len(urls)}\t{len(scores)}\t{urls[0] if urls else ''}")
    print(f"# {total} result lists differ (order or score) between {build_a} and {build_b}")
    return total


# ---------------------------------------------------------------- main
def dry_run(spec: dict, arms: list[dict], state: dict, release: Path, only: set | None) -> None:
    spq, up_s, src = rates(state)
    print(f"sq-sweep {spec['name']} — DRY RUN: nothing is started, nothing is written")
    print(f"release {release}   api {API}   tools {tool('sq-engine.sh')}, {tool('sq-eval.py')}")
    print("base settings on every arm, the control included: "
          + (" ".join(f"{k}={v}" for k, v in arms[0]["env"].items()) or "none (every default)"))
    print(f"{'arm':<34} {'state':<8} {'queries':>7} {'to ask':>7} {'min':>6}  settings / sets")
    total = n_run = 0
    for a in arms:
        if only is not None and a["short"] not in only and a["short"] != "ctl":
            continue
        sett = " ".join(f"{k}={v}" for k, v in a["own"].items()) or "(the control)"
        if a["blocked"]:
            print(f"{a['short']:<34} {'BLOCKED':<8} {'':>7} {'':>7} {'':>6}  {sett} — {a['blocked']}")
            continue
        n, left = to_ask(a, build_hash(API, release, a["env"]))
        if is_done(a, state):
            print(f"{a['short']:<34} {'done':<8} {n:>7} {0:>7} {0:>6}  {sett} | {','.join(a['sets'])}")
            continue
        sec = up_s + 15 + left * spq
        total += sec
        n_run += 1
        st = "failed" if (state["arms"].get(a["short"]) or {}).get("status") == "failed" else "pending"
        print(f"{a['short']:<34} {st:<8} {n:>7} {left:>7} {sec / 60:>6.1f}  {sett} | {','.join(a['sets'])}"
              f"  [target {','.join(a['target']) or '-'}]")
    print(f"\n{n_run} arms to run (of {len(arms)} with the control), one engine at a time: about {total / 3600:.1f} h "
          f"({total / 60:.0f} min) of engine time, waits for the slot not counted.")
    print(f"Rate: {spq:.2f} s a query — {src}.")
    print("Cache: an arm with a setting is its own build, so every query of its sets is asked once; \"to ask\" is what "
          "the build's cache does not already hold (an interrupted arm, or a control that ran on this code).")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("spec", nargs="?", help="the sweep spec (JSON)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--report-only", action="store_true")
    ap.add_argument("--only", default="")
    ap.add_argument("--max-arms", type=int, default=0)
    ap.add_argument("--lock-wait", type=float, default=14400.0)
    ap.add_argument("--keep-going", action="store_true")
    ap.add_argument("--release")
    ap.add_argument("--port", type=int, default=18473)
    ap.add_argument("--same-answers", nargs=2, metavar=("BUILD_A", "BUILD_B"))
    ap.add_argument("--sets", default="", help="with --same-answers: comma-separated sets (default: every open set)")
    a = ap.parse_args()

    if a.same_answers:
        sets = [s for s in a.sets.split(",") if s] or C.all_sets()
        sys.exit(1 if same_answers(a.same_answers[0], a.same_answers[1], sets) else 0)
    if not a.spec:
        ap.error("a spec file is required")
    spec = load_spec(Path(a.spec), API)
    release = Path(os.path.expanduser(a.release or spec.get("release") or "")).resolve()
    arms = plan(spec)
    state = load_state(spec)
    only = {x for x in a.only.split(",") if x} or None
    if only and only - {x["short"] for x in arms}:
        die(f"--only: no such arm: {', '.join(sorted(only - {x['short'] for x in arms}))}")
    if a.dry_run:
        dry_run(spec, arms, state, release, only)
        return
    if a.report_only:
        print(report(spec, arms, state), end="")
        return
    if not (release / "manifest.json").exists():
        die(f"{release} has no manifest.json: not a release copy (--release, or the spec's \"release\")")

    code = build_hash(API, release, {})
    if state["code"] and state["code"] != code and any(x.get("status") == "done" for x in state["arms"].values()):
        die(f"the API code or the release changed since this sweep's first arm (build {state['code']} then, {code} now): "
            f"arms on two codes are not comparable. Start again under a new spec name.", EXIT_CODE_MOVED)
    state["code"] = code
    stop = sweep_dir(spec) / "STOP"
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    ran = 0
    rc = 0
    for arm in arms:
        if arm["blocked"] or is_done(arm, state):
            if arm["blocked"] and arm["short"] != "ctl":
                log(f"arm {arm['short']}: BLOCKED — {arm['blocked']}")
            continue
        if only is not None and arm["short"] not in only and arm["short"] != "ctl":
            continue
        if a.max_arms and ran >= a.max_arms:
            log(f"--max-arms {a.max_arms} reached")
            break
        got = wait_slot(a.lock_wait, stop)
        if got == "stop":
            log(f"stop file {stop} is there: stopping before arm {arm['short']} (remove it to resume)")
            break
        if got == "busy":
            rc = EXIT_BUSY
            break
        try:
            rec = run_arm(arm, spec, release, a.port, arms[0]["name"])
        except KeyboardInterrupt:
            log(f"interrupted during arm {arm['short']}: its engine was taken down; the arm is not recorded")
            rc = 130
            rec = None
        left = slot_cleared(GONE_WAIT)
        if rec is not None:
            state["arms"][arm["short"]] = rec
            save_state(spec, state)
            ran += 1
            log(f"arm {arm['short']}: {rec['status']}" + (f" — {rec['error']}" if rec.get("error") else
                f" (build {rec.get('build')}, {rec.get('asked')} asked, {rec.get('eval_s')} s)"))
        if left:
            log(f"after arm {arm['short']} the slot is not clear: {left}. Not starting another engine.")
            rc = EXIT_LEFTOVER
            break
        if rec is None:
            break
        if rec["status"] != "done":
            if arm["short"] == "ctl":
                log("the control arm failed: nothing can be paired without it")
                rc = EXIT_FAILED
                break
            if not a.keep_going:
                rc = EXIT_FAILED
                break
    print(report(spec, arms, state), end="")
    log(f"report: {sweep_dir(spec) / 'report.md'}")
    sys.exit(rc)


if __name__ == "__main__":
    main()
