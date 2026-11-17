#!/usr/bin/env python3
"""Black-box regression suite for the logout search engine: the same cases judge a Windows install, a Linux install,
staging and public, over the engine's HTTP port (and, for the link group, the public rendezvous).

  run.py --target http://127.0.0.1:8090 [--rv wss://search.logout.cloud/rv] [--suite answers,stored,...]
         [--tag windows --tag release2 ...] [--repo DIR] [--install-dir DIR --pid N] [--sandbox] [--long]
         [--soak SECONDS] [--out report.json]
  run.py parity --a URL --b URL [--names-file F] [--out parity.json]    (the same searches on two targets, compared)
  run.py --list                                                          (the cases, the defect each guards)

Statuses of a case: PASS, FAIL, FLAG (over a speed flag, no baseline yet: reported, not failing), XFAIL (failed, and the case file says it is expected to: reason shown), XPASS (an expected
failure that passed: remove the marker), SKIP (a precondition is missing: the reason is shown), PENDING (written, cannot run yet).
Exit codes: 0 no FAIL/ERROR (XFAIL, SKIP, PENDING allowed); 1 at least one FAIL or ERROR; 2 usage error or target unreachable;
with --strict, XPASS and PENDING also exit 1. Standard library only (the bundle's own Python runs it)."""
import argparse
import concurrent.futures as cf
import glob
import json
import os
import sys
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from rg_checks import CHECKS, PASS, FAIL, SKIP, PENDING, names_from  # noqa: E402
from rg_http import Target, normal  # noqa: E402

ORDER = ["answers", "stored", "takein", "startup", "link", "resources", "matrix", "parity", "lifecycle"]
EXIT_OK, EXIT_FAIL, EXIT_USAGE = 0, 1, 2


class Ctx:
    def __init__(self, a, target):
        self.t = target
        self.tags = set(a.tag or [])
        self.rv = a.rv
        self.repo = a.repo
        self.root = a.repo or os.path.abspath(os.path.join(HERE, "..", ".."))
        self.install_dir = a.install_dir
        self.pid = a.pid
        self.sandbox = a.sandbox
        self.long = a.long
        self.quick = a.quick
        self.soak_s = a.soak
        self.py = a.py or sys.executable
        self.start_cmd = a.start_cmd
        self.home = a.home
        self.port = a.port
        self.compare = a.compare
        self.parity_diffs = []
        self.browser_page = a.browser_page
        self.chrome = a.chrome
        self.node_path = a.node_path
        self.matrix_n = a.matrix_n
        self.matrix_modes = [m for m in a.matrix_modes.split(",") if m]
        self.matrix_workers = a.matrix_workers
        self.matrix_typing = a.matrix_typing
        self.matrix_baseline = a.matrix_baseline
        self.matrix_rows = []
        self._cache = {}
        self._lock = __import__("threading").Lock()

    def platform_key(self):
        for k in ("windows", "linux", "public", "staging"):
            if k in self.tags:
                return k
        return "default"

    def path(self, rel):
        p = os.path.join(self.root, rel)
        return p

    def ask(self, q, **extra):
        k = (q, tuple(sorted(extra.items())))
        with self._lock:
            if k in self._cache:
                return self._cache[k]
        r = self.t.instant(q, **extra)
        with self._lock:
            self._cache[k] = r
        return r


def load_cases(groups):
    cases = []
    for f in sorted(glob.glob(os.path.join(HERE, "cases", "*.json"))):
        d = json.load(open(f, encoding="utf-8"))
        g = d["group"]
        if groups and g not in groups:
            continue
        for c in d["cases"]:
            c["group"] = g
            cases.append(c)
    return cases


def xfail_reason(case, tags, sub=""):
    """The expected-failure reason that applies to this target and this part of the case (`subs`: only those parts), else None."""
    for x in case.get("xfail", []):
        if x.get("tags") and not (set(x["tags"]) & tags):
            continue
        if x.get("subs") and sub not in x["subs"]:
            continue
        return x["reason"]
    return None


def unmet(case, ctx):
    need = case.get("needs", [])
    for n, have in (("sandbox", ctx.sandbox), ("install", ctx.install_dir), ("pid", ctx.pid), ("rv", ctx.rv), ("long", ctx.long), ("repo", ctx.repo), ("compare", ctx.compare)):
        if n in need and not have:
            return f"needs --{n.replace('install', 'install-dir')}" + (" (a disposable install: the case restarts or feeds the engine)" if n == "sandbox" else "")
    if case.get("only_tags") and not (set(case["only_tags"]) & ctx.tags):
        return f"only for targets tagged {case['only_tags']}"
    if case.get("skip_tags") and set(case["skip_tags"]) & ctx.tags:
        return f"not for targets tagged {sorted(set(case['skip_tags']) & ctx.tags)}"
    return None


def run_case(case, ctx):
    """-> list of result dicts {id, sub, status, evidence, secs}"""
    t0 = time.monotonic()
    cid = case["id"]
    why = unmet(case, ctx)
    if why:
        return [dict(id=cid, sub="", status="SKIP", evidence=why, secs=0.0)]
    fn = CHECKS.get(case["check"])
    if fn is None:
        return [dict(id=cid, sub="", status="ERROR", evidence=f"unknown check {case['check']!r}", secs=0.0)]
    try:
        subs = fn(ctx, case.get("params", {}))
    except Exception:  # noqa: BLE001 - a broken check is an ERROR, not a silent pass
        return [dict(id=cid, sub="", status="ERROR", evidence=traceback.format_exc(limit=3)[-600:], secs=time.monotonic() - t0)]
    out = []
    # an expected-failure part passes -> XPASS (the marker is stale); a part with no marker never becomes one
    for sub, st, ev in subs:
        xr = xfail_reason(case, ctx.tags, sub)
        if st == PASS:
            status = "XPASS" if xr else "PASS"
        elif st == "flag":
            status = "FLAG"
        elif st == FAIL:
            status = "XFAIL" if xr else "FAIL"
        elif st == PENDING:
            status = "PENDING"
        else:
            status = "SKIP"
        if xr and status in ("XFAIL", "XPASS"):
            ev = f"{ev}  [expected failure: {xr}]"
        out.append(dict(id=cid, sub=sub, status=status, evidence=ev, secs=round((time.monotonic() - t0) / max(1, len(subs)), 2)))
    return out


def summarize(results, cases):
    byg = {}
    meta = {c["id"]: c for c in cases}
    for r in results:
        g = meta[r["id"]]["group"]
        s = byg.setdefault(g, {k: 0 for k in ("PASS", "FAIL", "XFAIL", "XPASS", "FLAG", "SKIP", "PENDING", "ERROR")})
        s[r["status"]] += 1
    return byg


def print_summary(byg, results, cases, target_name, show_all):
    meta = {c["id"]: c for c in cases}
    cols = ("PASS", "FAIL", "XFAIL", "XPASS", "FLAG", "SKIP", "PENDING", "ERROR")
    print(f"\n== regression results for {target_name} ==")
    print(f"{'group':12s} {'checks':>7s} " + " ".join(f"{c.lower():>7s}" for c in cols))
    tot = {c: 0 for c in cols}
    for g in sorted(byg, key=lambda x: ORDER.index(x) if x in ORDER else 99):
        s = byg[g]
        print(f"{g:12s} {sum(s.values()):7d} " + " ".join(f"{s[c]:7d}" for c in cols))
        for c in cols:
            tot[c] += s[c]
    print(f"{'total':12s} {sum(tot.values()):7d} " + " ".join(f"{tot[c]:7d}" for c in cols))
    shown = ("FAIL", "ERROR", "XPASS", "FLAG") if not show_all else cols
    rows = [r for r in results if r["status"] in shown]
    if rows:
        print("\n" + ("all results" if show_all else "failures and surprises"))
        for r in rows:
            c = meta[r["id"]]
            sub = f" [{r['sub']}]" if r["sub"] else ""
            print(f"  {r['status']:7s} {r['id']}{sub}  ({c['severity']}; guards {', '.join(c.get('guards', []))})")
            print(f"          {c['title']}\n          {r['evidence'][:420]}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--target", help="the engine's base URL, e.g. http://127.0.0.1:8090 or https://search.logout.cloud")
    ap.add_argument("--prefix", help="path prefix of /instant: '' for an install, '/api' for public (default: detected)")
    ap.add_argument("--matrix-n", type=int, default=100, help="queries per type in the matrix group (public: 30)")
    ap.add_argument("--matrix-modes", default="cold,repeat,typing,parallel", help="ways to ask in the matrix group")
    ap.add_argument("--matrix-workers", type=int, default=4, help="requests in flight in the parallel way (1 or 0: no parallel way)")
    ap.add_argument("--matrix-baseline", help="JSON of baseline p90s per platform and type: a type over 1.5x baseline + 50 ms fails")
    ap.add_argument("--matrix-typing", type=int, default=25, help="queries per type whose every prefix 2..10 is asked in the typing way")
    ap.add_argument("--matrix-out", help="write every matrix request (JSON lines) here")
    ap.add_argument("--browser-page", help="the search page for the headless-browser first-run case (e.g. https://search.logout.cloud/)")
    ap.add_argument("--chrome", help="path of a Chrome/Chromium for that case (default: the runner's installed Chrome)")
    ap.add_argument("--node-path", help="NODE_PATH holding playwright-core for that case")
    ap.add_argument("--rate", type=float, default=0.0, help="seconds between requests (public: 0.3; the monitor rate is far below its limit)")
    ap.add_argument("--rv", help="the public rendezvous, wss://search.logout.cloud/rv (link group)")
    ap.add_argument("--suite", help="comma-separated groups (default all): " + ",".join(ORDER))
    ap.add_argument("--tag", action="append", help="a label for the target (public, staging, windows, linux, release1, release2): expected-failure markers match on it")
    ap.add_argument("--repo", help="a checkout of the repository: repo-level guard cases (existing unit tests) run from there")
    ap.add_argument("--py", help="python for the repo-level guards (default: this one)")
    ap.add_argument("--install-dir", help="the install's base folder (disk, uninstall and log cases)")
    ap.add_argument("--pid", type=int, help="the engine's PID, for the memory cases (Linux)")
    ap.add_argument("--home", help="the sandbox's fake HOME (uninstall residue)")
    ap.add_argument("--port", type=int, default=8090, help="the sandbox engine's port (doctor)")
    ap.add_argument("--compare", help="the other platform's engine URL (parity group)")
    ap.add_argument("--start-cmd", help="the command that (re)starts the sandbox engine (restart cases)")
    ap.add_argument("--sandbox", action="store_true", help="the target is a DISPOSABLE install: cases that restart it or feed its inbox may run")
    ap.add_argument("--long", action="store_true", help="the long variants (10,000-search memory run)")
    ap.add_argument("--quick", action="store_true", help="short variants for the suite's own unit test (3-second soak)")
    ap.add_argument("--soak", type=int, default=0, help="seconds for the stability soak (default: the case's own, 30)")
    ap.add_argument("--timeout", type=float, default=60, help="seconds a single search may take before it counts as no answer")
    ap.add_argument("--out", help="write the machine report (JSON) here")
    ap.add_argument("--all", action="store_true", help="print every result, not only failures")
    ap.add_argument("--strict", action="store_true", help="XPASS and PENDING also fail the run")
    ap.add_argument("--list", action="store_true", help="list the cases and the defect each guards, then exit")
    ap.add_argument("--name", help="a name for the target in the report (default: the URL)")
    a = ap.parse_args(argv)
    groups = [g for g in (a.suite or "").split(",") if g]
    cases = load_cases(groups)
    bad = [g for g in groups if g not in ORDER]
    if bad:
        print(f"unknown group(s) {bad}; groups are {ORDER}", file=sys.stderr)
        return EXIT_USAGE
    if a.list:
        for c in cases:
            print(f"{c['id']:10s} {c['group']:10s} {c['severity']:8s} {c['title']}  <- {'; '.join(c.get('guards', []))}")
        print(f"{len(cases)} cases")
        return EXIT_OK
    if not a.target:
        ap.error("--target is required")
    t = Target(a.target, prefix=a.prefix, timeout=a.timeout, rate=a.rate)
    if not t.detect():
        print(f"target {a.target} does not answer /instant or /api/instant", file=sys.stderr)
        return EXIT_USAGE
    ctx = Ctx(a, t)
    results = []
    t0 = time.monotonic()
    for c in sorted(cases, key=lambda c: (ORDER.index(c["group"]) if c["group"] in ORDER else 99, c["id"])):
        results += run_case(c, ctx)
    secs = time.monotonic() - t0
    byg = summarize(results, cases)
    name = a.name or a.target
    print_summary(byg, results, cases, name, a.all)
    print(f"\n{len(results)} checks in {secs:.0f} s against {a.target} (prefix {t.prefix!r}, tags {sorted(ctx.tags)}); {t.requests} requests, {t.errors} transport errors, {t.throttled} throttled (HTTP 429, retried)")
    if a.matrix_out and ctx.matrix_rows:
        with open(a.matrix_out, "w", encoding="utf-8") as f:
            for r in ctx.matrix_rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        import rg_matrix
        print(rg_matrix.table(rg_matrix.summarize(ctx.matrix_rows), name))
        print(f"matrix rows: {a.matrix_out}")
    if a.out:
        rep = dict(target=name, url=a.target, prefix=t.prefix, tags=sorted(ctx.tags), at=time.strftime("%Y-%m-%d %H:%M:%S"), seconds=round(secs, 1),
                   groups=byg, cases={c["id"]: {k: c.get(k) for k in ("group", "title", "guards", "severity", "xfail")} for c in cases}, results=results)
        with open(a.out, "w", encoding="utf-8") as f:
            json.dump(rep, f, indent=1, ensure_ascii=False)
        print(f"report: {a.out}")
    tot = {k: sum(g[k] for g in byg.values()) for k in ("FAIL", "ERROR", "XPASS", "PENDING")}
    if tot["FAIL"] or tot["ERROR"] or (a.strict and (tot["XPASS"] or tot["PENDING"])):
        return EXIT_FAIL
    return EXIT_OK


def parity(argv):
    from rg_checks import compare_targets
    ap = argparse.ArgumentParser(prog="run.py parity", description="the same searches on two targets: cards and result lists compared (live fields set aside)")
    ap.add_argument("--a", required=True)
    ap.add_argument("--b", required=True)
    ap.add_argument("--names-file", default="tests/regress/sets/parity-names.txt")
    ap.add_argument("--limit", type=int, default=300)
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--out")
    a = ap.parse_args(argv)
    root = os.path.abspath(os.path.join(HERE, "..", ".."))
    names = []
    for ln in open(os.path.join(root, a.names_file), encoding="utf-8"):
        if ln.strip() and not ln.startswith("#") and ln.strip() not in names:
            names.append(ln.strip())
    names = names[: a.limit]
    ta, tb = Target(a.a), Target(a.b)
    for t in (ta, tb):
        if not t.detect():
            print(f"{t.base} does not answer", file=sys.stderr)
            return EXIT_USAGE
    same, diffs, errs = compare_targets(ta, tb, names, a.workers)
    fields = {}
    for d in diffs:
        for f in d["fields"]:
            fields[f] = fields.get(f, 0) + 1
    print(f"parity {a.a}  vs  {a.b}: {len(names)} searches: {same} identical, {len(diffs)} differ, {len(errs)} not answered by one side")
    print("differing fields:", fields)
    for d in diffs[:25]:
        print(f"  {d['name']!r}: {d['fields']}")
        for f in d["fields"][:2]:
            print(f"      {f}: A {str(d['a'][f])[:110]}  |  B {str(d['b'][f])[:110]}")
    for n, ev in errs[:10]:
        print(f"  NOANSWER {n!r}: {ev}")
    if a.out:
        json.dump(dict(a=a.a, b=a.b, searches=len(names), identical=same, differ=diffs, unanswered=errs, fields=fields), open(a.out, "w"), indent=1, ensure_ascii=False)
    return EXIT_FAIL if diffs or errs else EXIT_OK


def matrix_report(argv):
    """run.py matrix-report ROWS_A.jsonl[=NAME] [ROWS_B.jsonl[=NAME]]: the table per platform and, with two, the diff table."""
    import rg_matrix as M
    sets = []
    for a in argv:
        path, _, name = a.partition("=")
        sets.append((name or os.path.basename(path), [json.loads(l) for l in open(path, encoding="utf-8") if l.strip()]))
    for name, rows in sets:
        print(M.table(M.summarize(rows), name))
    if len(sets) == 2:
        cmp, diffs = M.compare(sets[0][1], sets[1][1], sets[0][0], sets[1][0])
        print(M.compare_table(cmp, sets[0][0], sets[1][0]))
        print(f"\n{len(diffs)} searches answered differently")
        for d in diffs[:30]:
            print(f"  [{d['type']}] {d['q']!r}{' (near)' if d['near'] else ''}: A {d['a']}  |  B {d['b']}")
    return EXIT_OK


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "parity":
        sys.exit(parity(sys.argv[2:]))
    if len(sys.argv) > 1 and sys.argv[1] == "matrix-report":
        sys.exit(matrix_report(sys.argv[2:]))
    sys.exit(main())
