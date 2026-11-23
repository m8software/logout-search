#!/usr/bin/env python3
"""Weekly Wikidata/pageview refresh of a release (owner 2026-11-15: facts, pictures, sitelinks and page views weekly).

The daily Wikipedia job (wiki_daily.py) keeps article text fresh; the numbers that come from Wikidata and the pageview
dumps stay at the release they were built in. This job moves them, on a copy of the latest release, from files and
QLever only (no Postgres):

    wiki_weekly.py pageviews-day DAY --cache DIR [--url URL]      one day's English Wikipedia user views per page id
    wiki_weekly.py sitelinks --cache DIR --stamp YYYYMMDD           sitelink count per English article (QLever)
    wiki_weekly.py signals PREV_SIGNALS REGISTRY OUT RELEASE        copy-patch wiki.pageviews / wiki.sitelinks
                    [--pv-cache DIR --end DAY [--days 30]] [--sitelinks FILE] [--pv-ratio R] [--sl-ratio R] [--report F]
    wiki_weekly.py prepare --day DAY --out WEEKLY_DIR --pv-cache DIR [--pv-days 30] [--no-facts] [--no-sitelinks]
                    fetch it all into a dated folder (DONE last); the daily build applies it:
    wiki_daily.py run ... --weekly-dir WEEKLY_DIR     -> apply(): signals.bin and facts.sqlite replaced in that day's release

Signals: the page-view number keeps rc15's unit (one month of user views; here the 30 days to --end, summed from the
daily `pageview_complete` files: ~0.5 GB a day, cached per day as a small TSV). A value moves only when it changed by
more than its ratio, counted on value + floor (page views 50 % on +100, sitelinks 10 % on +5; rehearsal 2026-11-23: 25 % moved 264,186 values a
week and marked 24 % of the sample answers, 50 % 36,380 and 6 %, while 1.47 M moved values changed 2 of 2,000 answers): nearly every count wiggles every week, and each moved
value makes the stored answers that cite the article count as affected (twin/release_delta.py reads signals.bin per
field). A refresh that would zero out a large share of the old non-zero values is refused (a short download, a QLever
outage): the release keeps last week's numbers.

Facts: `facts_build.py fetch` into the weekly folder's own facts/ cache (it never refetches a finished file, so a new
folder each week), then, in the daily build, `facts_build.py build` against the NEW signals (lists.pv embeds page
views). The weekly part rides on one day's release, so ids, packages, the lock and publishing stay the daily job's. Card `f`, `d`, `o`,
the lookup `entities` table and pictures are not refreshed by this job yet (they come through the retired Postgres
bridge; see docs/internal/wikipedia-freshness-plan.md section 11).
"""
from __future__ import annotations

import argparse
import array
import datetime as dt
import gzip
import json
import math
import os
import sqlite3
import struct
import subprocess
import sys
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
SEARCH_DIR = os.path.dirname(HERE)
IMPORTER_DIR = os.path.join(SEARCH_DIR, "importer")
TWIN_DIR = os.path.join(SEARCH_DIR, "twin")

SIGNALS_MAGIC = b"LOSIGNL1"
TYPE_SHIFT_DEFAULT = 28
WIKI_TYPE = "wiki"
PV_FIELD, SL_FIELD = "wiki.pageviews", "wiki.sitelinks"
PV_URL = "https://dumps.wikimedia.org/other/pageview_complete/{y}/{y}-{m}/pageviews-{day}-user.bz2"
UA = "logout-search-build/1 (weekly page views and sitelinks)"
WIKI_PREFIX = "en.wikipedia "
WIKI_IRI = "https://en.wikipedia.org/wiki/"
SITELINKS_BODY = ("?article schema:about ?item ; schema:isPartOf <https://en.wikipedia.org/> . "
                  "?item wikibase:sitelinks ?sl .")
# array typecodes for the dtypes release_v1.py writes (little-endian only: the release format is <)
TYPECODES = {"<u1": "B", "|u1": "B", "<u2": "H", "<u4": "I", "<u8": "Q", "<i4": "i", "<i8": "q", "<f4": "f", "<f8": "d"}
MAX_ZEROED = 0.20                  # refuse when more than this share of the old non-zero values would become 0


def log(msg: str) -> None:
    print(f"[wiki-weekly {time.strftime('%H:%M:%S')}] {msg}", flush=True)


class RefreshRefused(RuntimeError):
    """The new numbers look broken (short download, outage): keep the old ones."""


# ------------------------------------------------------------------ signals.bin
def read_signals(path: str) -> tuple[dict, dict]:
    """(header, {field name: array}) of a LOSIGNL1 file."""
    if sys.byteorder != "little":
        raise RuntimeError("signals.bin is little-endian; this reader needs a little-endian host")
    with open(path, "rb") as f:
        head = f.read(12)
        if head[:8] != SIGNALS_MAGIC:
            raise ValueError(f"{path}: not a LOSIGNL1 file")
        n = struct.unpack("<I", head[8:12])[0]
        header = json.loads(f.read(n))
        base = 12 + n
        arrays = {}
        for fd in header["fields"]:
            tc = TYPECODES.get(fd["dtype"])
            if tc is None:
                raise ValueError(f"{path}: field {fd['name']} has dtype {fd['dtype']}, unknown here")
            a = array.array(tc)
            f.seek(base + fd["offset"])
            a.frombytes(f.read(fd["count"] * a.itemsize))
            if len(a) != fd["count"]:
                raise ValueError(f"{path}: field {fd['name']} is short ({len(a)} of {fd['count']})")
            arrays[fd["name"]] = a
    return header, arrays


def write_signals(path: str, header: dict, arrays: dict) -> None:
    """Write a NEW file (never in place: the release copy hard-links the previous release's signals.bin)."""
    h = json.loads(json.dumps(header))
    off = 0
    for fd in h["fields"]:
        a = arrays[fd["name"]]
        fd["count"] = len(a)
        fd["offset"] = off
        off += len(a) * a.itemsize
    body = json.dumps(h, sort_keys=True, separators=(",", ":")).encode()
    tmp = path + ".part"
    with open(tmp, "wb") as f:
        f.write(SIGNALS_MAGIC + struct.pack("<I", len(body)) + body)
        for fd in h["fields"]:
            arrays[fd["name"]].tofile(f)
    os.replace(tmp, path)


def moved(old: int, new: int, ratio: float, floor: int = 1) -> bool:
    """True when the value changed by more than `ratio`, counted on value + floor: a long-tail article going from 3 to 9
    monthly views (floor 100) is not a move, 1,000 to 1,300 is."""
    if old == new:
        return False
    return abs(math.log((new + floor) / (old + floor))) > math.log1p(ratio)


def wiki_rows(registry: str, mask: int, wiki_type: int, shift: int):
    """(page_id, seq, title) of every article in the registry."""
    db = sqlite3.connect(f"file:{registry}?mode=ro", uri=True)
    try:
        yield from db.execute(f"SELECT ext, doc_id & {mask}, title FROM registry WHERE doc_id >> {shift} = ?", (wiki_type,))
    finally:
        db.close()


def patch_signals(prev: str, registry: str, out: str, release: str, pv: dict | None, sl: dict | None,
                  pv_ratio: float = 0.50, sl_ratio: float = 0.10, pv_source: str | None = None,
                  sl_source: str | None = None, pv_floor: int = 100, sl_floor: int = 5) -> dict:
    """Copy PREV to OUT with wiki.pageviews (by page id) and wiki.sitelinks (by title) moved where they moved enough.
    Every wiki field is extended to cover articles appended since (their seq is past the old end)."""
    t0 = time.time()
    header, arrays = read_signals(prev)
    shift = header.get("type_shift", TYPE_SHIFT_DEFAULT)
    types = header.get("types") or {}
    if WIKI_TYPE not in types:
        raise ValueError(f"{prev}: no '{WIKI_TYPE}' type in the header")
    mask = (1 << shift) - 1
    rows = list(wiki_rows(registry, mask, types[WIKI_TYPE], shift))
    need = (max(r[1] for r in rows) + 1) if rows else 0
    grown = 0
    for fd in header["fields"]:
        if fd["type"] == WIKI_TYPE and len(arrays[fd["name"]]) < need:
            a = arrays[fd["name"]]
            grown = max(grown, need - len(a))
            a.extend([0] * (need - len(a)))
    report = {"release": release, "articles": len(rows), "wiki_grown_by": grown}

    def apply(field, value_of, ratio, floor, cap, label, source):
        if field not in arrays:
            report[label] = {"skipped": f"{field} not in this signals.bin"}
            return
        a = arrays[field]
        old_nz = sum(1 for _p, s, _t in rows if a[s])
        new_vals = [(s, min(value_of(p, t), cap)) for p, s, t in rows]
        zeroed = sum(1 for s, v in new_vals if a[s] and not v)
        if old_nz and zeroed > MAX_ZEROED * old_nz:
            raise RefreshRefused(f"{field}: {zeroed:,} of {old_nz:,} non-zero values would become 0 "
                                 f"(over {MAX_ZEROED:.0%}): the new numbers look incomplete, keeping the old ones")
        n_moved = n_same_ish = 0
        for s, v in new_vals:
            if moved(a[s], v, ratio, floor):
                a[s] = v
                n_moved += 1
            elif a[s] != v:
                n_same_ish += 1
        report[label] = {"moved": n_moved, "changed_within_ratio_kept": n_same_ish, "zeroed": zeroed,
                         "old_nonzero": old_nz, "ratio": ratio, "floor": floor}
        if source:
            for fd in header["fields"]:
                if fd["name"] == field:
                    fd["source"] = source

    if pv is not None:
        apply(PV_FIELD, lambda p, _t: pv.get(p, 0), pv_ratio, pv_floor, 2 ** 32 - 1, "pageviews", pv_source)
    if sl is not None:
        apply(SL_FIELD, lambda _p, t: sl.get(t, 0), sl_ratio, sl_floor, 2 ** 16 - 1, "sitelinks", sl_source)
    header["built_release"] = release
    write_signals(out, header, arrays)
    report["seconds"] = round(time.time() - t0, 1)
    report["bytes"] = os.path.getsize(out)
    return report


# ------------------------------------------------------------------ page views
def pv_day_path(cache: str, day: str) -> str:
    return os.path.join(cache, f"pv-{day}.tsv.gz")


def fetch_pv_day(day: str, cache: str, url: str | None = None) -> str:
    """One day's English Wikipedia user views summed per page id -> cache/pv-DAY.tsv.gz (page_id TAB views).
    Kept: a rerun never fetches a finished day again."""
    sys.path.insert(0, IMPORTER_DIR)
    import pageviews as pvi                               # lines_of / past_english: the monthly importer's parser
    out = pv_day_path(cache, day)
    if os.path.exists(out):
        return out
    os.makedirs(cache, exist_ok=True)
    url = url or PV_URL.format(y=day[:4], m=day[4:6], day=day)
    t0 = time.time()
    if url.startswith(("http://", "https://")):
        stream = urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=600)
    else:
        stream = open(url[len("file://"):] if url.startswith("file://") else url, "rb")
    views: dict[int, int] = {}
    seen = 0
    with stream:
        for line in pvi.lines_of(stream):
            if not line.startswith(WIKI_PREFIX):
                if seen and pvi.past_english(line):
                    break
                continue
            seen += 1
            p = line.split(" ")
            # daily file: wiki title page_id access daily_total hourly
            if len(p) < 5 or not p[2].isdigit() or not p[4].isdigit():
                continue
            pid = int(p[2])
            views[pid] = views.get(pid, 0) + int(p[4])
    if not views:
        raise RefreshRefused(f"{url}: no English Wikipedia lines")
    tmp = out + ".part"
    with gzip.open(tmp, "wt", encoding="utf-8") as f:
        for pid in sorted(views):
            f.write(f"{pid}\t{views[pid]}\n")
    os.replace(tmp, out)
    log(f"page views {day}: {len(views):,} pages, {sum(views.values()):,} views ({seen:,} lines) in {time.time() - t0:.0f}s")
    return out


def window_days(end: str, days: int) -> list[str]:
    e = dt.datetime.strptime(end, "%Y%m%d").date()
    return [(e - dt.timedelta(days=i)).strftime("%Y%m%d") for i in range(days - 1, -1, -1)]


def pv_window(cache: str, end: str, days: int = 30) -> dict:
    """page_id -> views summed over the `days` days ending `end`; every day must be in the cache."""
    want = window_days(end, days)
    missing = [d for d in want if not os.path.exists(pv_day_path(cache, d))]
    if missing:
        raise RefreshRefused(f"page views: {len(missing)} of {days} days missing from {cache} ({missing[0]}..{missing[-1]})")
    tot: dict[int, int] = {}
    for d in want:
        with gzip.open(pv_day_path(cache, d), "rt", encoding="utf-8") as f:
            for line in f:
                a, b = line.rstrip("\n").split("\t")
                k = int(a)
                tot[k] = tot.get(k, 0) + int(b)
    return tot


def prune_pv(cache: str, keep_from: str) -> int:
    n = 0
    for fn in os.listdir(cache) if os.path.isdir(cache) else []:
        if fn.startswith("pv-") and fn.endswith(".tsv.gz") and fn[3:11] < keep_from:
            os.unlink(os.path.join(cache, fn))
            n += 1
    return n


# ------------------------------------------------------------------ sitelinks
def parse_sitelink_row(line: str):
    """'<https://en.wikipedia.org/wiki/Linux>\\t"251"^^<…int>' -> ("Linux", 251)."""
    p = line.rstrip("\n").split("\t")
    if len(p) < 2:
        return None
    iri = p[0].strip().strip("<>")
    if not iri.startswith(WIKI_IRI):
        return None
    v = p[1].strip()
    if v.startswith('"'):
        v = v[1:v.index('"', 1)]
    try:
        n = int(float(v))
    except ValueError:
        return None
    return urllib.parse.unquote(iri[len(WIKI_IRI):]).replace("_", " "), n


def sitelinks_path(cache: str, stamp: str) -> str:
    return os.path.join(cache, f"sitelinks-{stamp}.tsv")


def fetch_sitelinks(cache: str, stamp: str) -> str:
    sys.path.insert(0, HERE)
    import facts_build as fb                               # QLever: one request at a time, count-checked, range-split
    os.makedirs(cache, exist_ok=True)
    out = sitelinks_path(cache, stamp)
    fb.fetch_file("sitelinks", "?article ?sl", SITELINKS_BODY, out)   # not ?n: count_query names its own ?n
    return out


def read_sitelinks(path: str) -> dict:
    out: dict[str, int] = {}
    with open(path, encoding="utf-8") as f:
        next(f, None)                                     # header line
        for line in f:
            r = parse_sitelink_row(line)
            if r and r[1] > out.get(r[0], -1):
                out[r[0]] = r[1]
    if not out:
        raise RefreshRefused(f"{path}: no sitelink rows")
    return out


# ------------------------------------------------------------------ prepare (fetch) and apply (inside the daily build)
WEEKLY_FORMAT = 1


def _run(cmd: list[str]) -> str:
    log("run: " + " ".join(cmd))
    p = subprocess.run(cmd, check=True, stdout=subprocess.PIPE, text=True)
    return p.stdout


def prepare(day: str, out_dir: str, pv_cache: str, pv_days: int = 30, facts: bool = True, sitelinks: bool = True) -> dict:
    """Fetch everything the weekly refresh needs into OUT_DIR (a dated folder): pv.tsv.gz (page_id, views over the
    window), sitelinks.tsv (QLever), facts/ (a fresh facts_build cache) and weekly.json; DONE last. Restartable: each
    finished part is kept (the page view days in PV_CACHE, the facts files in facts/)."""
    t0 = time.time()
    os.makedirs(out_dir, exist_ok=True)
    if os.path.exists(os.path.join(out_dir, "DONE")):
        return json.load(open(os.path.join(out_dir, "weekly.json"), encoding="utf-8"))
    days = window_days(day, pv_days)
    for d in days:
        fetch_pv_day(d, pv_cache)
    pv = pv_window(pv_cache, day, pv_days)
    tmp = os.path.join(out_dir, "pv.tsv.gz.part")
    with gzip.open(tmp, "wt", encoding="utf-8") as f:
        for pid in sorted(pv):
            f.write(f"{pid}\t{pv[pid]}\n")
    os.replace(tmp, os.path.join(out_dir, "pv.tsv.gz"))
    doc = {"format": WEEKLY_FORMAT, "day": day, "pv": {"first": days[0], "last": days[-1], "days": pv_days,
                                                         "pages": len(pv), "views": sum(pv.values())}}
    if sitelinks:
        p = fetch_sitelinks(out_dir, day)
        os.replace(p, os.path.join(out_dir, "sitelinks.tsv"))
        doc["sitelinks"] = {"stamp": day, "rows": sum(1 for _ in open(os.path.join(out_dir, "sitelinks.tsv"), encoding="utf-8")) - 1}
    if facts:
        fc = os.path.join(out_dir, "facts")
        fpy = os.path.join(HERE, "facts_build.py")
        _run([sys.executable, fpy, "fetch", "--cache", fc])
        _run([sys.executable, fpy, "fetch-lists", "--cache", fc])
        sys.path.insert(0, HERE)
        import facts_build as fb                          # the file list `facts_build.py build` requires
        missing = [n for n in ["articles"] + [r["prop"] for r in fb.load_templates(fb.TEMPLATES)]
                   if not os.path.exists(os.path.join(fc, f"{n}.tsv"))]
        if missing:
            raise RefreshRefused(f"facts fetch: {len(missing)} files missing ({', '.join(missing[:8])}); the week is not DONE, a rerun continues")
        doc["facts"] = {"cache": "facts", "files": len([x for x in os.listdir(fc) if x.endswith(".tsv")])}
    doc["seconds"] = round(time.time() - t0, 1)
    json.dump(doc, open(os.path.join(out_dir, "weekly.json"), "w", encoding="utf-8"), indent=1, sort_keys=True)
    open(os.path.join(out_dir, "DONE"), "w").close()
    prune_pv(pv_cache, window_days(day, pv_days + 7)[0])
    log(f"prepared {out_dir}: {json.dumps(doc)}")
    return doc


def read_pv_file(path: str) -> dict:
    out: dict[int, int] = {}
    with gzip.open(path, "rt", encoding="utf-8") as f:
        for line in f:
            a, b = line.rstrip("\n").split("\t")
            out[int(a)] = int(b)
    return out


def apply(rel_dir: str, prev_rel: str, release_id: str, weekly_dir: str, pv_ratio: float = 0.50, sl_ratio: float = 0.10,
          pv_floor: int = 100, sl_floor: int = 5, facts_top: int = 0) -> dict:
    """Inside the daily build, after the registry is updated and before tombstoned rows are purged: signals.bin and
    facts.sqlite of REL_DIR (hard links to PREV_REL's) are replaced by new files. Either both are replaced or, on any
    error, neither: the caller keeps the day's build and reports the weekly part as failed."""
    doc = json.load(open(os.path.join(weekly_dir, "weekly.json"), encoding="utf-8"))
    if not os.path.exists(os.path.join(weekly_dir, "DONE")):
        raise RefreshRefused(f"{weekly_dir}: not finished (no DONE)")
    reg = os.path.join(rel_dir, "registry.sqlite")
    sig, sig_new = os.path.join(rel_dir, "signals.bin"), os.path.join(rel_dir, "signals.bin.weekly")
    pv = read_pv_file(os.path.join(weekly_dir, "pv.tsv.gz")) if "pv" in doc else None
    sl = read_sitelinks(os.path.join(weekly_dir, "sitelinks.tsv")) if "sitelinks" in doc else None
    pv_src = f"Wikimedia pageview_complete user views, {doc['pv']['days']} days {doc['pv']['first']}..{doc['pv']['last']}" if pv else None
    sl_src = f"Wikidata sitelinks through QLever, {doc['sitelinks']['stamp']}" if sl else None
    out = {"weekly": doc["day"]}
    facts_new = None
    try:
        out["signals"] = patch_signals(os.path.join(prev_rel, "signals.bin"), reg, sig_new, release_id, pv, sl,
                                       pv_ratio, sl_ratio, pv_src, sl_src, pv_floor, sl_floor)
        if "facts" in doc:
            facts_new = os.path.join(rel_dir, "facts.sqlite.weekly")
            cmd = [sys.executable, os.path.join(HERE, "facts_build.py"), "build", reg, sig_new, facts_new, release_id,
                   "--cache", os.path.join(weekly_dir, doc["facts"]["cache"])]
            if facts_top:
                cmd += ["--top", str(facts_top)]
            _run(cmd)
            out["facts"] = {"bytes": os.path.getsize(facts_new)}
    except BaseException:
        for p in (sig_new, facts_new):
            if p and os.path.exists(p):
                os.unlink(p)
        raise
    os.replace(sig_new, sig)                             # replaces the hard link; the previous release's file is untouched
    if facts_new:
        os.replace(facts_new, os.path.join(rel_dir, "facts.sqlite"))
    log(f"weekly {doc['day']} applied: {json.dumps(out)}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("pageviews-day")
    d.add_argument("day"), d.add_argument("--cache", required=True), d.add_argument("--url")
    s = sub.add_parser("sitelinks")
    s.add_argument("--cache", required=True), s.add_argument("--stamp", required=True)
    g = sub.add_parser("signals")
    g.add_argument("prev"), g.add_argument("registry"), g.add_argument("out"), g.add_argument("release")
    g.add_argument("--pv-cache"), g.add_argument("--end"), g.add_argument("--days", type=int, default=30)
    g.add_argument("--sitelinks"), g.add_argument("--pv-ratio", type=float, default=0.50)
    g.add_argument("--sl-ratio", type=float, default=0.10), g.add_argument("--report")
    g.add_argument("--pv-floor", type=int, default=100), g.add_argument("--sl-floor", type=int, default=5)
    p = sub.add_parser("prepare")
    p.add_argument("--day", required=True, help="the last page view day of the window, YYYYMMDD")
    p.add_argument("--out", required=True, help="the dated weekly folder (DONE when complete)")
    p.add_argument("--pv-cache", required=True), p.add_argument("--pv-days", type=int, default=30)
    p.add_argument("--no-facts", action="store_true"), p.add_argument("--no-sitelinks", action="store_true")
    a = ap.parse_args()
    try:
        if a.cmd == "pageviews-day":
            print(fetch_pv_day(a.day, a.cache, a.url))
        elif a.cmd == "sitelinks":
            print(fetch_sitelinks(a.cache, a.stamp))
        elif a.cmd == "signals":
            pv = pv_window(a.pv_cache, a.end, a.days) if a.pv_cache else None
            sl = read_sitelinks(a.sitelinks) if a.sitelinks else None
            rep = patch_signals(a.prev, a.registry, a.out, a.release, pv, sl, a.pv_ratio, a.sl_ratio,
                                pv_floor=a.pv_floor, sl_floor=a.sl_floor)
            if a.report:
                open(a.report, "w").write(json.dumps(rep, indent=1) + "\n")
            print(json.dumps(rep))
        elif a.cmd == "prepare":
            print(json.dumps(prepare(a.day, a.out, a.pv_cache, a.pv_days, not a.no_facts, not a.no_sitelinks)))
    except RefreshRefused as e:
        log(f"REFUSED: {e}")
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
