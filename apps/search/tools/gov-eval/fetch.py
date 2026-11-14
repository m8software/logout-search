#!/usr/bin/env python3
"""Read select.py's pages from Common Crawl into a file. DEV ONLY: no database is read or written.

    fetch.py LIST.tsv OUT.jsonl [--rate 8] [--workers 8]

The same range request and the same parser as ccfetch (importer/ccfetch.py), at their request guidance
(under 10 a second) — or, with CC_ACCESS=s3 inside us-east-1, from s3://commoncrawl (infra/build-aws/jobs/gov-fetch.sh:
--rate 150). One JSON line per page: arm, domain, url, title, description, h1, lang, text — or
error. A page that could not be fetched gets no line, so a rerun retries it; lines already in OUT are skipped.
"""
import argparse
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor

import ccfetch


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("list"); ap.add_argument("out")
    ap.add_argument("--rate", type=float, default=8.0); ap.add_argument("--workers", type=int, default=8)
    a = ap.parse_args()
    done = set()
    if os.path.exists(a.out):
        with open(a.out) as f:
            for l in f:
                try:
                    done.add(json.loads(l)["url"])
                except ValueError:
                    pass                                   # a line cut short by a stop: fetched again
    rows = [r for r in (l.rstrip("\n").split("\t") for l in open(a.list)) if r[5] not in done]
    ccfetch.log(f"gov fetch: {len(rows):,} pages to read ({len(done):,} already in {a.out}), at most {a.rate}/s")
    throttle = ccfetch._Throttle(a.rate)

    def work(r):
        arm, domain, _host, _agency, _score, url, langs, wf, off, ln = r
        try:
            raw = (ccfetch._get_s3(wf, int(off), int(ln), throttle) if ccfetch.CC_ACCESS == "s3"
                   else ccfetch._get(f"{ccfetch.CC_DATA}/{wf}", int(off), int(ln), throttle))
            page = ccfetch.parse_record(raw)
            if not ccfetch.keep_language(langs, page["lang"]):
                return {"arm": arm, "domain": domain, "url": url, "error": "not English"}
            return {"arm": arm, "domain": domain, "url": url, "status": page["status"], "title": page["title"],
                    "description": page["description"], "h1": page["h1"], "lang": page["lang"], "text": page["text"]}
        except (ccfetch.SlowDown, RuntimeError):
            return None
        except Exception as e:
            return {"arm": arm, "domain": domain, "url": url, "error": f"{type(e).__name__}: {e}"[:200]}

    t0, n, bad, skipped = time.time(), 0, 0, 0
    with open(a.out, "a") as f, ThreadPoolExecutor(max_workers=a.workers) as pool:
        for res in pool.map(work, rows):
            if res is None:
                skipped += 1
                continue
            n += 1
            bad += "error" in res
            f.write(json.dumps(res, ensure_ascii=False) + "\n")
            if n % 2000 == 0:
                f.flush()
                ccfetch.log(f"gov fetch: {n:,}/{len(rows):,} ({bad:,} failed, {skipped:,} to retry), {n / (time.time() - t0):.1f}/s")
    ccfetch.log(f"gov fetch: done, {n:,} written ({bad:,} failed), {skipped:,} to retry, {(time.time() - t0) / 60:.1f} min")


if __name__ == "__main__":
    main()
