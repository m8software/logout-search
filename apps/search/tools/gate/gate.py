#!/usr/bin/env python3
"""The owner's tasks as pass/fail: each row names the page that does the job, and a release ships only if
the rows pass. rc12 passed every aggregate gate with 'nj fishing license' answered by a National Guard page.

    gate.py GATE.tsv URL [--out FILE]     URL: the engine, e.g. http://127.0.0.1:8090. Exit 1 on any miss.

Ask the engine configured as it serves: indexhost's has SEARCH_HOME in New Jersey, and an arm without it ranked the same
release differently (the freshwater page 2nd on testbox, 9th live, 2026-11-01). Standard library only.
"""
import json, re, sys, urllib.parse, urllib.request


def host_path(url: str) -> str:
    s = urllib.parse.urlsplit(url)
    return (s.hostname or "").lower().removeprefix("www.") + s.path.rstrip("/")


def main(argv):
    out = open(argv[argv.index("--out") + 1], "w", encoding="utf-8") if "--out" in argv else None
    passed = total = 0
    for line in open(argv[0], encoding="utf-8"):
        if line.startswith("#") or not line.strip():
            continue
        q, n, rx, what = line.rstrip("\n").split("\t")
        r = json.load(urllib.request.urlopen(f"{argv[1]}/search?q={urllib.parse.quote(q)}", timeout=90))
        urls = [x["url"] for x in r["results"]]
        at = next((i + 1 for i, u in enumerate(urls) if re.match(rx, host_path(u))), None)
        ok = at is not None and at <= int(n)
        passed, total = passed + ok, total + 1
        print(f"{'PASS' if ok else 'MISS'} {str(at or '-'):>2}  {q:44} {urls[0][:70] if urls else '(none)'}", flush=True)
        if out:
            out.write(json.dumps({"q": q, "at": at, "ok": ok, "top": urls[:5]}) + "\n")
    print(f"{passed}/{total} pass")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
