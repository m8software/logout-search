"""Sandbox cases: they restart the engine, feed its inbox and uninstall it, so they run only with --sandbox against a
DISPOSABLE install (never an owner's real one). Linux only (they read /proc to find the install's own processes).
They use the same (ctx, params) -> [(sub, status, evidence)] contract as rg_checks."""
import json
import os
import shlex
import signal
import sqlite3
import subprocess
import sys
import time

from rg_checks import FAIL, PASS, SKIP, names_from, stored_served
from rg_http import percentile


def own_pids(ctx):
    """PIDs of processes whose program lives under the install folder: exactly the install's own, nothing else."""
    base = os.path.realpath(ctx.install_dir) + os.sep
    out = []
    for d in os.listdir("/proc"):
        if not d.isdigit() or int(d) == os.getpid():
            continue
        try:
            exe = os.path.realpath(f"/proc/{d}/exe")
            cmd = open(f"/proc/{d}/cmdline", "rb").read().split(b"\0")
        except OSError:
            continue
        first = os.path.realpath(cmd[0].decode("utf-8", "replace")) if cmd and cmd[0] else ""
        if exe.startswith(base) or first.startswith(base):
            out.append(int(d))
    return out


def stop(ctx):
    pids = own_pids(ctx)
    for p in pids:
        try:
            os.kill(p, signal.SIGTERM)
        except OSError:
            pass
    t_end = time.time() + 40
    while time.time() < t_end and any(os.path.exists(f"/proc/{p}") for p in pids):
        time.sleep(0.5)
    left = [p for p in pids if os.path.exists(f"/proc/{p}")]
    for p in left:
        try:
            os.kill(p, signal.SIGKILL)
        except OSError:
            pass
    return len(pids), len(left)


def start(ctx):
    log = open(os.path.join(ctx.install_dir, "regress-start.log"), "ab")
    subprocess.Popen(shlex.split(ctx.start_cmd), stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True,
                     env=dict(os.environ, **({"HOME": ctx.home} if ctx.home else {})))


def wait_ready(ctx, limit_s):
    """-> (seconds to /readyz 200 or None, seconds to the first good /instant answer after that or None)"""
    t0 = time.monotonic()
    t_ready = None
    while time.monotonic() - t0 < limit_s:
        st, text = ctx.t.readyz()
        if st == 200:
            t_ready = time.monotonic() - t0
            break
        time.sleep(1)
    if t_ready is None:
        return None, None
    while time.monotonic() - t0 < limit_s:
        d, secs, err = ctx.t.instant("gmail")
        if d is not None and (d.get("card") or d.get("results")):
            return t_ready, time.monotonic() - t0
        time.sleep(1)
    return t_ready, None


def restart(ctx, p):
    if not ctx.start_cmd:
        return None, "needs --start-cmd (the command that starts the sandbox engine)"
    n, left = stop(ctx)
    time.sleep(2)
    start(ctx)
    return wait_ready(ctx, p.get("ready_s", 600)), f"stopped {n} process(es) ({left} needed SIGKILL)"


def restart_serving(ctx, p):
    r, note = restart(ctx, p)
    if r is None:
        return [("restart", SKIP, note)]
    t_ready, t_first = r
    res = [("ready", PASS if t_ready is not None else FAIL, f"/readyz 200 after {t_ready and round(t_ready)} s ({note}); limit {p.get('ready_s', 600)} s"),
           ("first answer", PASS if t_first is not None and t_first <= p.get("ready_s", 600) else FAIL,
            f"first good answer {t_first and round(t_first)} s after the start")]
    if t_first is None:
        return res
    names = names_from(ctx, {"names_file": "apps/search/twin/must.txt"})
    lat, errs, t_end, i = [], [], time.monotonic() + max(p.get("watch_s", 300), ctx.soak_s), 0
    while time.monotonic() < t_end:
        t0 = time.monotonic()
        d, secs, err = ctx.t.instant(names[i % len(names)])
        i += 1
        lat.append(secs * 1000)
        if d is None:
            errs.append(err)
        time.sleep(max(0, 1 - (time.monotonic() - t0)))
    mx = max(lat)
    bad = ([f"{len(errs)} failed ({errs[:2]})"] if errs else []) + ([f"slowest {mx:.0f} ms > {p['max_ms']}"] if mx > p["max_ms"] else [])
    res.append(("first minutes", FAIL if bad else PASS, f"{len(lat)} searches in the first {int(p.get('watch_s', 300))} s: p50 {percentile(lat, .5):.0f} ms, p99 {percentile(lat, .99):.0f} ms, max {mx:.0f} ms" + (" -- " + "; ".join(bad) if bad else "")))
    return res


def restart_stored(ctx, p):
    r, note = restart(ctx, p)
    if r is None or r[1] is None:
        return [("restart", SKIP if r is None else FAIL, note if r is None else "the engine did not answer after the restart")]
    names = names_from(ctx, {"names_file": "apps/search/twin/must.txt", "limit": 30})
    return stored_served(ctx, {"names": names, "min_share": 0.8})


def store_path(ctx):
    """The engine's result store file inside the install (found, not assumed)."""
    for root, dirs, files in os.walk(os.path.join(ctx.install_dir, "data")):
        if "store" in os.path.basename(root) and "inbox" not in root:
            for f in files:
                if f.endswith(".sqlite") and os.path.getsize(os.path.join(root, f)) > 10**6:
                    return os.path.join(root, f), os.path.join(root, "inbox")
    return None, None


def takein_serving(ctx, p):
    src, inbox = store_path(ctx)
    if not src:
        return [("takein", SKIP, "no result store file found under the install's data folder")]
    os.makedirs(inbox, exist_ok=True)
    z0 = ctx.t.storez() or {}
    tmp = os.path.join(inbox, "regress-takein.tmp")
    t = time.monotonic()
    a = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    b = sqlite3.connect(tmp)
    a.backup(b)
    b.close()
    a.close()
    size = os.path.getsize(tmp)
    final = os.path.join(inbox, "regress-takein.sqlite")
    os.replace(tmp, final)
    names = names_from(ctx, {"names_file": "apps/search/twin/must.txt"})
    lat, errs, i, ups = [], [], 0, []
    t_end = time.monotonic() + 300
    gone_at = None
    while time.monotonic() < t_end:
        t0 = time.monotonic()
        d, secs, err = ctx.t.instant(names[i % len(names)])
        i += 1
        lat.append(secs * 1000)
        if d is None:
            errs.append(err)
        if not os.path.exists(final) and gone_at is None:
            gone_at = time.monotonic() - t
        if gone_at is not None and time.monotonic() - t > gone_at + 5:
            break
        time.sleep(max(0, 0.25 - (time.monotonic() - t0)))
    z1 = ctx.t.storez() or {}
    mx, p99 = max(lat), percentile(lat, .99)
    log = os.path.join(ctx.install_dir, "data", "state", "engine.log")
    taken = ""
    try:
        taken = [l for l in open(log, errors="replace").read().splitlines() if "regress-takein" in l][-1][:200]
    except (OSError, IndexError):
        pass
    bad = []
    if gone_at is None:
        bad.append("the inbox file was not taken in within 300 s")
    if errs:
        bad.append(f"{len(errs)} failed requests ({errs[:2]})")
    if mx > p["max_ms"]:
        bad.append(f"slowest search {mx:.0f} ms > {p['max_ms']}")
    if p99 > p["p99_ms"]:
        bad.append(f"p99 {p99:.0f} ms > {p['p99_ms']}")
    if z1.get("up_s", 0) < z0.get("up_s", 0):
        bad.append("the engine restarted during the take-in")
    if "refused" in taken:
        bad.append("the engine refused its own copy")
    return [("takein", FAIL if bad else PASS,
             f"{size / 1e6:.0f} MB file taken in after {gone_at and round(gone_at, 1)} s; {len(lat)} searches meanwhile: p99 {p99:.0f} ms, max {mx:.0f} ms; up_s {z0.get('up_s')} -> {z1.get('up_s')}; log: {taken}" + (" -- " + "; ".join(bad) if bad else ""))]


def takein_refused(ctx, p):
    src, inbox = store_path(ctx)
    if not src:
        return [("refused", SKIP, "no result store file found under the install's data folder")]
    os.makedirs(inbox, exist_ok=True)
    f = os.path.join(inbox, "regress-garbage.sqlite")
    with open(f + ".tmp", "wb") as fh:
        fh.write(b"this is not a sqlite file " * 100)
    os.replace(f + ".tmp", f)
    t_end = time.monotonic() + 300
    while time.monotonic() < t_end and os.path.exists(f):
        time.sleep(2)
    renamed = os.path.exists(f + ".bad")
    d, secs, err = ctx.t.instant("gmail")
    z = ctx.t.storez() or {}
    ok = renamed and d is not None and not z.get("off")
    if renamed:
        os.remove(f + ".bad")
    return [("refused", PASS if ok else FAIL, f"file {'renamed .bad' if renamed else 'still there or gone without a trace'}; a search {'answered' if d else 'did not: ' + err}; store {'off' if z.get('off') else 'on'}")]


def setup_exits(ctx, p):
    if not sys.platform.startswith("linux"):
        return [("setup", SKIP, "reads /proc (Linux)")]
    stray = []
    for d in own_pids(ctx):
        try:
            cmd = open(f"/proc/{d}/cmdline", "rb").read().replace(b"\0", b" ").decode("utf-8", "replace")
        except OSError:
            continue
        if " setup" in cmd or " install" in cmd:
            stray.append(f"{d}: {cmd[:120]}")
    engines = [d for d in own_pids(ctx) if open(f"/proc/{d}/cmdline", "rb").read().split(b"\0")[1:2] == [b"engine"]]
    bad = []
    if stray:
        bad.append(f"setup/install still running: {stray}")
    if len(engines) != 1:
        bad.append(f"{len(engines)} engine processes (expected 1)")
    return [("setup", FAIL if bad else PASS, f"engine processes {engines}" + (" -- " + "; ".join(bad) if bad else ""))]


def app_path(ctx):
    """The install's program: logout-search, or logout-search.exe on Windows."""
    return os.path.join(ctx.install_dir, "app", "logout-search" + (".exe" if os.name == "nt" else ""))


def install_tools(ctx, p):
    app = app_path(ctx)
    if not os.path.exists(app):
        return [("tools", SKIP, f"{app} not found")]
    env = dict(os.environ, **({"HOME": ctx.home} if ctx.home else {}))
    res = []
    for sub, argv in (("version", [app, "version", "--dest", os.path.join(ctx.install_dir, "data")]),
                      ("doctor", [app, "doctor", "--dest", os.path.join(ctx.install_dir, "data"), "--port", str(ctx.port)]),
                      ("channel", [app, "channel", "--dest", os.path.join(ctx.install_dir, "data")]),
                      ("autoupdate status", [app, "autoupdate", "status", "--dest", os.path.join(ctx.install_dir, "data")])):
        try:
            r = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120, env=env)
        except subprocess.TimeoutExpired:
            res.append((sub, FAIL, "timed out after 120 s"))
            continue
        out = (r.stdout + r.stderr).strip()
        bad = []
        if r.returncode != 0:
            bad.append(f"exit {r.returncode}")
        if sub == "doctor" and ("@" in out and "http" not in out.split("@")[0][-20:]):
            bad.append("the report may hold an e-mail address")
        res.append((sub, FAIL if bad else PASS, (out.splitlines() or [""])[0][:100] + (" -- " + "; ".join(bad) if bad else "") + f"  ({len(out.splitlines())} lines)"))
    return res


def update_check(ctx, p):
    """The in-product updater on the beta channel: asked to check now, it must report a check with no failure and no rollback
    within wait_s. (The install is disposable: the channel is changed.)"""
    app = app_path(ctx)
    if not os.path.exists(app):
        return [("update check", FAIL, f"{app} not found")]
    env = dict(os.environ, **({"HOME": ctx.home} if ctx.home else {}))
    data = os.path.join(ctx.install_dir, "data")

    def run(*argv):
        r = subprocess.run([app, *argv, "--dest", data], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180, env=env)
        return r.returncode, (r.stdout + r.stderr).strip()

    def status():
        rc, out = run("autoupdate", "status")
        try:
            return json.loads(out[out.index("{"):])
        except ValueError:
            return None
    before = status() or {}
    res = []
    rc, out = run("channel", "beta")
    res.append(("channel beta", PASS if rc == 0 else FAIL, f"exit {rc}: {(out.splitlines() or [''])[-1][:100]}"))
    rc, out = run("autoupdate", "now")
    res.append(("autoupdate now", PASS if rc == 0 else FAIL, f"exit {rc}: {(out.splitlines() or [''])[-1][:100]}"))
    t0, st = time.time(), None
    while time.time() - t0 < float(p.get("wait_s", 150)):
        st = status()
        if st and (st.get("last_check") or 0) > (before.get("last_check") or 0):
            break
        time.sleep(5)
    if not st or (st.get("last_check") or 0) <= (before.get("last_check") or 0):
        res.append(("checked", FAIL, f"no new check within {p.get('wait_s', 150)} s (last_check {before.get('last_check')} -> {(st or {}).get('last_check')})"))
        return res
    bad = [f"{k}: {st.get(k)}" for k in ("failed", "rollback") if st.get(k)]
    res.append(("checked", FAIL if bad else PASS, f"after {int(time.time() - t0)} s: channel {st.get('channel')}, result {st.get('last_result')!r}, "
                f"program {st.get('program')}" + (" -- " + "; ".join(bad) if bad else "")))
    return res


WIN_RESIDUE = r"""
$d = $args[0]; $left = @()
if (Test-Path -LiteralPath $d) { $n = @(Get-ChildItem -LiteralPath $d -Recurse -Force -File -ErrorAction SilentlyContinue).Count; if ($n -gt 0) { $left += "folder ${d}: $n file(s)" } }
$run = Get-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' -ErrorAction SilentlyContinue
if ($run) { $run.PSObject.Properties | Where-Object { "$($_.Value)" -like "*$d*" } | ForEach-Object { $left += "Run key: $($_.Name)" } }
Get-ChildItem 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall' -ErrorAction SilentlyContinue | Where-Object { "$((Get-ItemProperty $_.PSPath).InstallLocation)$((Get-ItemProperty $_.PSPath).UninstallString)" -like "*$d*" } | ForEach-Object { $left += "Apps entry: $($_.PSChildName)" }
Get-ChildItem "$env:APPDATA\Microsoft\Windows\Start Menu\Programs" -Recurse -Filter '*logout*' -ErrorAction SilentlyContinue | ForEach-Object { $left += "Start menu: $($_.Name)" }
Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -and $_.ExecutablePath -like "$d\*" } | ForEach-Object { $left += "process: $($_.Name) $($_.ProcessId)" }
$left
"""


def uninstall_residue(ctx, p):
    """`uninstall --yes` exits 0, and afterwards nothing of the install is left: no process whose program is in its folder, no
    autostart entry, no Apps entry, no Start menu item, no file. On Windows the program folder is removed after the command has
    ended, so the look waits up to settle_s."""
    app = app_path(ctx)
    if not os.path.exists(app):
        return [("uninstall", FAIL, f"{app} not found")]
    env = dict(os.environ, **({"HOME": ctx.home} if ctx.home else {}))
    keep = os.environ.get("REGRESS_KEEP_LOGS")                 # the engine's logs are evidence: copied out before they are removed
    if keep and os.path.isdir(keep):
        import glob, shutil
        for f in glob.glob(os.path.join(ctx.install_dir, "data", "state", "*.log")) + glob.glob(os.path.join(ctx.install_dir, "data", "state", "*.json")):
            try:
                shutil.copy2(f, keep)
            except OSError:
                pass
    r = subprocess.run([app, "uninstall", "--yes", "--dest", ctx.install_dir], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300, env=env)
    says = ((r.stdout + r.stderr).strip().splitlines() or [""])[-1][:120]
    left, t0 = [], time.time()
    while True:
        time.sleep(3)
        left = []
        if os.name == "nt":
            q = subprocess.run(["powershell", "-NoProfile", "-Command", WIN_RESIDUE.replace("$args[0]", "'" + os.path.normpath(ctx.install_dir).replace("'", "''") + "'")],
                               capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
            left = [ln.strip() for ln in q.stdout.splitlines() if ln.strip()]
            if q.returncode != 0:
                left.append("the residue look itself failed: " + (q.stderr.strip().splitlines() or [""])[-1][:120])
        else:
            if sys.platform.startswith("linux"):
                left += [f"process {d}" for d in own_pids(ctx)]
            for root in [ctx.install_dir] + ([ctx.home] if ctx.home else []):
                for dp, dn, fn in os.walk(root):
                    left += [os.path.join(dp, f) for f in fn if f not in ("regress-start.log", "install.log")]
        if not left or time.time() - t0 > float(p.get("settle_s", 60)):
            break
    return [("uninstall", PASS if r.returncode == 0 and not left else FAIL,
             f"exit {r.returncode}; left over after {int(time.time() - t0)} s: {len(left)} item(s) {left[:8]}; says: {says}")]


def proc_sample(pid):
    """(cpu clock ticks, bytes read, bytes written, rss kB) of a process from /proc, or None."""
    try:
        f = open(f"/proc/{pid}/stat").read().rsplit(")", 1)[1].split()
        ticks = int(f[11]) + int(f[12])
        rd = wr = 0
        for ln in open(f"/proc/{pid}/io"):
            if ln.startswith("read_bytes:"):
                rd = int(ln.split()[1])
            elif ln.startswith("write_bytes:"):
                wr = int(ln.split()[1])
        rss = next((int(l.split()[1]) for l in open(f"/proc/{pid}/status") if l.startswith("VmRSS:")), 0)
        return ticks, rd, wr, rss
    except (OSError, IndexError, ValueError):
        return None


def restart_profile(ctx, p):
    """A restart with the install's own processes sampled every 2 s for the first `watch_s` seconds: CPU and disk per process,
    written to restart-profile.csv beside the report, with the time to ready. (Windows: tools/proc-profile.ps1 does the same.)"""
    if not ctx.start_cmd:
        return [("profile", SKIP, "needs --start-cmd")]
    hz = os.sysconf("SC_CLK_TCK")
    stop(ctx)
    time.sleep(2)
    start(ctx)
    t0 = time.monotonic()
    prev, rows, ready_at = {}, [], None
    names = {}
    while time.monotonic() - t0 < p.get("watch_s", 300):
        time.sleep(2)
        now = time.monotonic() - t0
        for pid in own_pids(ctx):
            s = proc_sample(pid)
            if not s:
                continue
            if pid not in names:
                names[pid] = " ".join(open(f"/proc/{pid}/cmdline", "rb").read().decode("utf-8", "replace").split("\0")[1:3])[:30]
            if pid in prev:
                dt = now - prev[pid][0]
                rows.append((round(now), pid, names[pid], round(100 * (s[0] - prev[pid][1][0]) / hz / dt), round((s[1] - prev[pid][1][1]) / 1e6 / dt, 1), round((s[2] - prev[pid][1][2]) / 1e6 / dt, 1), round(s[3] / 1024)))
            prev[pid] = (now, s)
        if ready_at is None and ctx.t.readyz()[0] == 200:
            ready_at = now
    out = os.path.join(ctx.install_dir, "restart-profile.csv")
    with open(out, "w") as f:
        f.write("t_s,pid,process,cpu_pct,read_MBps,write_MBps,rss_MB\n")
        for r in rows:
            f.write(",".join(str(x) for x in r) + "\n")
    peak = {}
    for r in rows:
        k = r[2]
        q = peak.setdefault(k, [0, 0.0, 0.0, 0])
        q[0] = max(q[0], r[3]); q[1] += r[4] * 2; q[2] += r[5] * 2; q[3] = max(q[3], r[6])
    summ = "; ".join(f"{k}: peak CPU {v[0]}%, read {v[1]:.0f} MB, wrote {v[2]:.0f} MB, RSS {v[3]} MB" for k, v in peak.items())
    return [("profile", PASS if ready_at is not None else FAIL, f"ready after {ready_at and round(ready_at)} s; first {p.get('watch_s', 300)} s: {summ}; samples in {out}")]


CHECKS = {f.__name__: f for f in (restart_profile, restart_serving, restart_stored, takein_serving, takein_refused, setup_exits, install_tools, update_check, uninstall_residue)}
