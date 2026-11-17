#!/usr/bin/env python3
"""Score a live engine (or the recorded fixture) against apps/search/twin/golden/expect.tsv.

  eval-cards.py --live http://127.0.0.1:8080     # query a running engine (e.g. staging on indexhost)
  eval-cards.py --fixture                        # score the recorded answers in golden/fixture.json
  eval-cards.py --live URL --only-doubtful       # inspect the remaining doubtful rows (before vs after)
  eval-cards.py --live URL --min-ok 1.0          # exit 1 if any enforced (ok) row regresses

Standard library only. Reports card title, official-site host + full URL (so product paths like
google.com/maps are distinct from google.com), and first-result host."""
import argparse
import importlib.util
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
DEFAULT_EXPECT = os.path.join(REPO, "apps/search/twin/golden/expect.tsv")
DEFAULT_FIXTURE = os.path.join(REPO, "apps/search/twin/golden/fixture.json")
GOLDEN_MOD = os.path.join(REPO, "utility/search-golden.py")

_PREFIX_CACHE: dict[str, str] = {}


def _load_golden():
    spec = importlib.util.spec_from_file_location("search_golden", GOLDEN_MOD)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def official_url(d: dict) -> str:
    card = (d or {}).get("card") or {}
    return next((l.get("url") or "" for l in card.get("links", []) if l.get("title") == "Official site"), "")


def _candidate_endpoints(base: str, api_prefix: str | None) -> list[str]:
    base = base.rstrip("/")
    if api_prefix is not None:
        p = api_prefix.strip()
        p = ("/" + p.lstrip("/")) if p else ""
        return [f"{base}{p}/instant"]
    if "GOLDEN_API_PREFIX" in os.environ:
        p = os.environ["GOLDEN_API_PREFIX"].strip()
        p = ("/" + p.lstrip("/")) if p else ""
        return [f"{base}{p}/instant"]
    if base.endswith("/api"):
        return [f"{base}/instant"]
    if base in _PREFIX_CACHE:
        return [f"{base}{_PREFIX_CACHE[base]}/instant"]
    return [f"{base}/api/instant", f"{base}/instant"]


def fetch_instant(url: str, name: str, timeout: int = 20, api_prefix: str | None = None, extra: dict | None = None):
    """Query <url>/api/instant or <url>/instant (Honors GOLDEN_API_PREFIX / api_prefix, else falls back on 404)."""
    base = url.rstrip("/")
    q = urllib.parse.urlencode({"q": name, "results": 1, "full": 1, **(extra or {})})
    err = ""
    t0 = time.monotonic()
    for ep in _candidate_endpoints(base, api_prefix):
        for attempt in (0, 1):
            t0 = time.monotonic()
            try:
                with urllib.request.urlopen(f"{ep}?{q}", timeout=timeout) as r:
                    prefix = ep[len(base):-len("/instant")]
                    _PREFIX_CACHE[base] = prefix
                    return json.loads(r.read()), time.monotonic() - t0, ""
            except urllib.error.HTTPError as e:
                err = f"HTTPError: {e}"
                if e.code == 404:
                    break
                time.sleep(0.5)
            except Exception as e:  # noqa: BLE001
                err = f"{type(e).__name__}: {e}"
                time.sleep(0.5)
    return None, time.monotonic() - t0, err


def score_rows(rows, fetch_fn, sg, pause: float = 0.0, only_doubtful: bool = False):
    ok_rows, ok_pass, ok_fail = [], 0, []
    doubtful_rows = []
    errors = []
    for r in rows:
        if only_doubtful and r["status"] != "doubtful":
            continue
        d, secs, err = fetch_fn(r["name"])
        if d is None:
            errors.append((r["name"], err))
            continue
        got = sg.facts(d)
        want = (r["card"], r["site"], r["first"])
        rec = {
            "name": r["name"],
            "status": r["status"],
            "expected": {"card": want[0], "site": want[1], "first": want[2]},
            "got": {"card": got[0], "site": got[1], "first": got[2], "official_url": official_url(d)},
            "ms": round(secs * 1000, 1),
            "match": got == want,
        }
        if r["status"] == "ok":
            ok_rows.append(rec)
            if rec["match"]:
                ok_pass += 1
            else:
                ok_fail.append(rec)
        else:
            doubtful_rows.append(rec)
        if pause > 0:
            time.sleep(pause)
    return {
        "ok_total": len(ok_rows),
        "ok_pass": ok_pass,
        "ok_fail": ok_fail,
        "doubtful": doubtful_rows,
        "errors": errors,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--live", help="Base URL of a running engine (e.g. http://127.0.0.1:8080)")
    src.add_argument("--fixture", action="store_true", help="Score against golden/fixture.json")
    ap.add_argument("--expect", default=DEFAULT_EXPECT, help="Path to expect.tsv")
    ap.add_argument("--fixture-path", default=DEFAULT_FIXTURE, help="Path to fixture.json")
    ap.add_argument("--api-prefix", default=None, help="Explicit API path prefix before /instant (e.g. '' or '/api')")
    ap.add_argument("--param", action="append", default=[], help="extra query parameter k=v (x=1 makes the request non-plain: a stored answer is never read, the engine computes)")
    ap.add_argument("--only-doubtful", action="store_true", help="Only evaluate doubtful rows")
    ap.add_argument("--pause", type=float, default=0.55, help="Pause between live requests (seconds)")
    ap.add_argument("--timeout", type=int, default=20, help="Per-request timeout (seconds)")
    ap.add_argument("--min-ok", type=float, default=1.0, help="Fail if ok pass rate is below this (0..1)")
    ap.add_argument("--json", action="store_true", help="Emit JSON summary")
    a = ap.parse_args()

    sg = _load_golden()
    rows = sg.read_expect(a.expect)
    if a.live:
        extra = dict(p.split("=", 1) for p in a.param)
        fetch_fn = lambda name: fetch_instant(a.live, name, timeout=a.timeout, api_prefix=a.api_prefix, extra=extra)
        pause = a.pause
    else:
        fx = json.load(open(a.fixture_path, encoding="utf-8"))
        def fetch_fn(name):
            d = fx.get(name)
            return (d, 0.0, "") if d is not None else (None, 0.0, "missing from fixture")
        pause = 0.0

    rep = score_rows(rows, fetch_fn, sg, pause=pause, only_doubtful=a.only_doubtful)
    if a.json:
        print(json.dumps(rep, indent=2, ensure_ascii=False))
    else:
        if not a.only_doubtful:
            rate = (rep["ok_pass"] / rep["ok_total"]) if rep["ok_total"] else 1.0
            print(f"Enforced (ok): {rep['ok_pass']}/{rep['ok_total']} ({rate:.1%})")
            for rec in rep["ok_fail"]:
                w, g = rec["expected"], rec["got"]
                print(f"  FAIL {rec['name']}: want card={w['card']!r} site={w['site']!r} first={w['first']!r} "
                      f"| got card={g['card']!r} site={g['site']!r} ({g['official_url'] or '-'}) first={g['first']!r}")
        if rep["doubtful"]:
            changed = sum(1 for r in rep["doubtful"] if not r["match"])
            print(f"\nDoubtful ({len(rep['doubtful'])} rows, {changed} changed vs recorded):")
            print(f"{'name':<16} {'recorded (card / site / first)':<44} {'got (card / site / first)':<48} official_url")
            print("-" * 130)
            for rec in rep["doubtful"]:
                w, g = rec["expected"], rec["got"]
                ws = f"{w['card']} / {w['site']} / {w['first']}"
                gs = f"{g['card']} / {g['site']} / {g['first']}"
                mark = "*" if not rec["match"] else "="
                print(f"{mark} {rec['name']:<14} {ws:<44} {gs:<48} {g['official_url'] or '-'}")
        if rep["errors"]:
            print(f"\nErrors ({len(rep['errors'])}):")
            for n, err in rep["errors"]:
                print(f"  {n}: {err}")

    if rep["errors"]:
        sys.exit(1)
    if not a.only_doubtful and rep["ok_total"]:
        if (rep["ok_pass"] / rep["ok_total"]) < a.min_ok:
            sys.exit(1)


if __name__ == "__main__":
    main()
