#!/usr/bin/env python3
"""Opening hours that businesses publish on their own sites, from Common Crawl (2026-10-02).

OpenStreetMap hours run stale with no date on them, and a chain's whole store list is rarely
mapped. Many business sites state their hours in schema.org structured data (JSON-LD
openingHours / openingHoursSpecification, or microdata itemprop="openingHours") — a store
locator for every branch. Common Crawl captured those pages; this reads them out of its
public S3 copy inside AWS us-east-1. No business website is contacted.

    cchours.py select --crawl CC-MAIN-2026-39 --domains domains.tsv --out pages.tsv
    cchours.py fetch  --pages pages.tsv --out hours.jsonl.gz [--rate 150] [--workers 24]

domains.tsv: registrable domain <TAB> number of our places that list it as their website.
A domain with >= CHAIN_MIN places is a chain: its store/location pages are taken (up to
CHAIN_PAGES); otherwise its front page and a few contact/hours/location pages.

Only FACTS leave this job: name, address, point, phone, hours, the page URL and the crawl
date (docs/legal/PLACES-AND-HOURS-LICENSING.md; ADR-059: no readable page text ships).
"""
from __future__ import annotations

import argparse
import gzip
import html as html_mod
import io
import json
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

CHAIN_MIN = 20
CHAIN_PAGES = 3000
SMALL_PAGES = 4
INDEX = "s3://commoncrawl/cc-index/table/cc-main/warc/crawl={crawl}/subset=warc/*.parquet"
LOC_PATH = (r"(^|/)(stores?|locations?|hours|contact|contact-us|find-us|visit|visit-us|branch|branches|"
            r"restaurants?|our-locations|store-locator|storelocator|locator|offices?|clinics?|pharmacy)(/|$|[.-])")


def log(msg: str) -> None:
    print(time.strftime("%H:%M:%S"), msg, file=sys.stderr, flush=True)


# ------------------------------------------------------------------ select
def cmd_select(a) -> None:
    import duckdb
    d = duckdb.connect()
    d.execute(f"SET memory_limit='{a.mem}'; SET threads={a.threads}; SET preserve_insertion_order=false;")
    d.execute("INSTALL httpfs; LOAD httpfs; INSTALL aws; LOAD aws;")
    d.execute("CREATE SECRET cc (TYPE s3, PROVIDER credential_chain, REGION 'us-east-1')")
    d.execute(f"CREATE TABLE doms AS SELECT * FROM read_csv('{a.domains}', delim='\t', header=false, "
              "columns={'domain': 'VARCHAR', 'n': 'INTEGER'})")
    files = [r[0] for r in d.execute(f"SELECT file FROM glob('{INDEX.format(crawl=a.crawl)}') ORDER BY file").fetchall()]
    if a.sample_files:
        files = files[:a.sample_files]                       # a test run: the first N index files
    files = "[" + ",".join("'" + f + "'" for f in files) + "]"
    log(f"{d.execute('SELECT count(*) FROM doms').fetchone()[0]:,} domains; scanning {a.crawl} ({files.count(',') + 1} index files)")
    t0 = time.time()
    d.execute(f"""
    CREATE TABLE cand AS
    SELECT i.url, i.url_host_registered_domain AS domain, length(i.url_path) AS plen,
           CASE WHEN i.url_path IN ('/', '') THEN 'home' ELSE 'loc' END AS kind, doms.n,
           i.warc_filename, i.warc_record_offset, i.warc_record_length, i.fetch_time
    FROM read_parquet({files}, hive_partitioning = false) i
    JOIN doms ON i.url_host_registered_domain = doms.domain
    WHERE i.fetch_status = 200 AND i.content_mime_detected = 'text/html' AND i.url_query IS NULL
      AND (i.url_path IN ('/', '') OR regexp_matches(lower(i.url_path), '{LOC_PATH}'))""")
    log(f"{d.execute('SELECT count(*) FROM cand').fetchone()[0]:,} candidate pages in {(time.time() - t0) / 60:.1f} min")
    d.execute(f"""
    COPY (
      SELECT url, domain, kind, warc_filename, warc_record_offset, warc_record_length, fetch_time FROM (
        SELECT *, row_number() OVER (PARTITION BY domain, kind ORDER BY plen, url) AS rn FROM cand) x
      WHERE (kind = 'home' AND rn <= 1)
         OR (kind = 'loc' AND rn <= CASE WHEN n >= {CHAIN_MIN} THEN least({CHAIN_PAGES}, 3 * n) ELSE {SMALL_PAGES} END)
    ) TO '{a.out}' (FORMAT csv, DELIMITER '\t', HEADER false)""")
    n = d.execute(f"SELECT count(*) FROM read_csv('{a.out}', delim='\t', header=false)").fetchone()[0]
    log(f"{n:,} pages -> {a.out}")


# ------------------------------------------------------------------ parse
DAYS = ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"]
_DAYNAME = {"monday": "Mo", "tuesday": "Tu", "wednesday": "We", "thursday": "Th", "friday": "Fr", "saturday": "Sa",
            "sunday": "Su", "mon": "Mo", "tue": "Tu", "tues": "Tu", "wed": "We", "thu": "Th", "thur": "Th",
            "thurs": "Th", "fri": "Fr", "sat": "Sa", "sun": "Su", "mo": "Mo", "tu": "Tu", "we": "We", "th": "Th",
            "fr": "Fr", "sa": "Sa", "su": "Su"}
_LD = re.compile(rb"<script[^>]*type\s*=\s*[\"']?application/ld\+json[\"']?[^>]*>(.*?)</script>", re.I | re.S)
_MICRO_TAG = re.compile(rb"<[a-z]+[^>]*itemprop\s*=\s*[\"']openingHours[\"'][^>]*>([^<]{0,200})", re.I)
_CONTENT = re.compile(rb"content\s*=\s*[\"']([^\"']{3,200})[\"']", re.I)
_SPEC_OSM = re.compile(r"^(?:(?:Mo|Tu|We|Th|Fr|Sa|Su)(?:-(?:Mo|Tu|We|Th|Fr|Sa|Su))?(?:,(?:Mo|Tu|We|Th|Fr|Sa|Su)(?:-(?:Mo|Tu|We|Th|Fr|Sa|Su))?)* )?"
                       r"(?:\d{2}:\d{2}-\d{2}:\d{2}(?:,\d{2}:\d{2}-\d{2}:\d{2})*|off|closed)$")


def day_of(v) -> str | None:
    s = str(v or "").strip().rstrip("/").split("/")[-1].lower()     # "https://schema.org/Monday"
    return _DAYNAME.get(s)


def hhmm(v) -> str | None:
    m = re.match(r"^\s*(\d{1,2}):(\d{2})", str(v or ""))
    if not m or int(m.group(1)) > 24 or int(m.group(2)) > 59:
        return None
    return f"{int(m.group(1)):02d}:{m.group(2)}"


def day_ranges(days: set[str]) -> str:
    """{"Mo","Tu","We","Fr"} -> "Mo-We,Fr"."""
    idx = sorted(DAYS.index(x) for x in days)
    runs, start, prev = [], idx[0], idx[0]
    for i in idx[1:] + [None]:
        if i is not None and i == prev + 1:
            prev = i
            continue
        runs.append(DAYS[start] if start == prev else (f"{DAYS[start]},{DAYS[prev]}" if prev == start + 1 else f"{DAYS[start]}-{DAYS[prev]}"))
        if i is not None:
            start = prev = i
    return ",".join(runs)


def regroup(per_day: dict[str, list[str]]) -> str:
    """{"Mo": ["09:00-17:00"], ...} -> "Mo-Fr 09:00-17:00; Sa 10:00-14:00" (one rule per set of
    days with the same hours: in OSM syntax a later rule for a day REPLACES an earlier one)."""
    if not per_day:
        return ""
    if len(per_day) == 7 and all(v == ["00:00-24:00"] for v in per_day.values()):
        return "24/7"
    groups: dict[str, set[str]] = {}
    for d, spans in per_day.items():
        groups.setdefault(",".join(sorted(spans)) or "off", set()).add(d)
    parts = sorted(((min(DAYS.index(x) for x in ds), f"{day_ranges(ds)} {sp}") for sp, ds in groups.items()))
    return "; ".join(p for _, p in parts)


def merge_rules(rules: list[str]) -> str:
    """Several "Days spans" rules (a list of openingHours strings) -> one regrouped value: two
    slots on one day are ADDED ("Mo 06:30-10:30" + "Mo 11:00-23:00"), not the later one alone."""
    per_day: dict[str, list[str]] = {}
    for r in rules:
        m = re.fullmatch(r"((?:Mo|Tu|We|Th|Fr|Sa|Su)(?:-(?:Mo|Tu|We|Th|Fr|Sa|Su))?(?:,(?:Mo|Tu|We|Th|Fr|Sa|Su)(?:-(?:Mo|Tu|We|Th|Fr|Sa|Su))?)*)?\s*(.+)", r.strip())
        if not m or r.strip() == "24/7":
            return "24/7" if r.strip() == "24/7" and len(rules) == 1 else ""
        days = set()
        for part in (m.group(1) or "Mo-Su").split(","):
            a, _, b = part.partition("-")
            i, j = DAYS.index(a), DAYS.index(b or a)
            while True:
                days.add(DAYS[i])
                if i == j:
                    break
                i = (i + 1) % 7
        spans = [] if m.group(2) in ("off", "closed") else [("00:00-24:00" if x in ("00:00-23:59", "00:00-00:00") else x)
                                                          for x in m.group(2).split(",")]
        for d in days:
            cur = per_day.setdefault(d, [])
            cur.extend(x for x in spans if x not in cur)
    return regroup(per_day)


def from_spec(specs) -> str:
    """openingHoursSpecification list -> OSM opening_hours ("Mo-Fr 09:00-17:00; Sa 10:00-14:00"), or ''."""
    if isinstance(specs, dict):
        specs = [specs]
    per_day: dict[str, list[str]] = {}
    for s in specs or []:
        if not isinstance(s, dict) or s.get("validFrom") or s.get("validThrough"):
            continue                                          # seasonal/holiday exceptions: not the usual week
        days = s.get("dayOfWeek")
        days = days if isinstance(days, list) else [days]
        o, c = hhmm(s.get("opens")), hhmm(s.get("closes"))
        if not o or not c:
            continue
        span = "00:00-24:00" if o == c == "00:00" or (o == "00:00" and c in ("23:59", "24:00")) else f"{o}-{c}"
        for d in days:
            k = day_of(d)
            if k and span not in per_day.setdefault(k, []):
                per_day[k].append(span)
    return regroup(per_day)


_FREE_DAY = r"(mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun|mo|tu|we|th|fr|sa|su)[a-z]*"
_FREE_TIME = r"(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?"


def _clock(h: str, m: str | None, ap: str | None) -> str | None:
    h_, m_ = int(h), int(m or 0)
    ap = (ap or "").replace(".", "").lower()
    if ap == "pm" and h_ < 12:
        h_ += 12
    if ap == "am" and h_ == 12:
        h_ = 0
    return f"{h_:02d}:{m_:02d}" if h_ <= 24 and m_ <= 59 else None


def from_text(v: str) -> str:
    """One openingHours string -> OSM syntax, or '' when it is not one we can read safely.
    "Mo-Fr 09:00-17:00" passes; "Monday - Friday 9am - 5pm" is rewritten; prose is dropped."""
    s = " ".join(str(v).split())
    if _SPEC_OSM.match(s) or s in ("24/7", "Mo-Su 00:00-24:00"):
        return "24/7" if s == "Mo-Su 00:00-24:00" else s
    m = re.fullmatch(_FREE_DAY + r"\s*(?:-|–|to|through)\s*" + _FREE_DAY + r"\s*:?\s*" + _FREE_TIME + r"\s*(?:-|–|to)\s*" + _FREE_TIME,
                     s, re.I)
    if m:
        d1, d2 = _DAYNAME.get(m.group(1).lower()[:3]), _DAYNAME.get(m.group(2).lower()[:3])
        o, c = _clock(m.group(3), m.group(4), m.group(5)), _clock(m.group(6), m.group(7), m.group(8))
        if d1 and d2 and o and c:
            return f"{d1}-{d2} {o}-{c}"
    return ""


def _walk(o):
    if isinstance(o, list):
        for x in o:
            yield from _walk(x)
    elif isinstance(o, dict):
        yield o
        for k in ("@graph", "subOrganization", "department", "location", "containsPlace", "itemListElement", "item"):
            if k in o:
                yield from _walk(o[k])


def _txt(v, n: int = 160) -> str:
    if isinstance(v, list):
        v = v[0] if v else ""
    if isinstance(v, dict):
        v = v.get("name") or v.get("@value") or ""
    return " ".join(html_mod.unescape(str(v or "")).split())[:n]


def businesses(html: bytes) -> list[dict]:
    """Every schema.org thing on the page with opening hours -> facts only."""
    out = []
    for blob in _LD.findall(html):
        try:
            data = json.loads(blob.decode("utf-8", "replace").strip().rstrip(";"))
        except ValueError:
            continue
        for o in _walk(data):
            raw = o.get("openingHours")
            spec = o.get("openingHoursSpecification")
            if not raw and not spec:
                continue
            hours = from_spec(spec) if spec else ""
            if not hours and raw:
                vals = raw if isinstance(raw, list) else re.split(r"\s*[;,]\s*(?=(?:Mo|Tu|We|Th|Fr|Sa|Su))", str(raw))
                parts = [from_text(x) for x in vals]
                hours = merge_rules([q for p in parts for q in p.split("; ")]) if parts and all(parts) else ""
            if not hours:
                continue
            addr = o.get("address") if isinstance(o.get("address"), dict) else {}
            geo = o.get("geo") if isinstance(o.get("geo"), dict) else {}
            try:
                lat, lon = float(geo.get("latitude")), float(geo.get("longitude"))
                if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                    lat = lon = None
            except (TypeError, ValueError):
                lat = lon = None
            t = o.get("@type")
            out.append({"name": _txt(o.get("name")), "type": _txt(t if not isinstance(t, list) else ",".join(map(str, t)), 60),
                        "street": _txt(addr.get("streetAddress")), "city": _txt(addr.get("addressLocality"), 60),
                        "region": _txt(addr.get("addressRegion"), 30), "postcode": _txt(addr.get("postalCode"), 12),
                        "lat": lat, "lon": lon, "phone": _txt(o.get("telephone"), 30), "hours": hours, "src": "jsonld"})
    if not out:                                               # microdata: one business per page, hours only
        vals = []
        for m in _MICRO_TAG.finditer(html):
            c = _CONTENT.search(m.group(0))
            vals.append((c.group(1) if c else m.group(1)).decode("utf-8", "replace").strip())
        parts = [from_text(v) for v in vals if v]
        if parts and all(parts) and merge_rules([q for p in parts for q in p.split("; ")]):
            out.append({"name": "", "type": "", "street": "", "city": "", "region": "", "postcode": "",
                        "lat": None, "lon": None, "phone": "", "hours": merge_rules([q for p in parts for q in p.split("; ")]), "src": "microdata"})
    return out


def split_record(raw: bytes) -> bytes:
    """One gzipped WARC response record -> the HTML body (first 2 MB)."""
    rec = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
    _, _, http = rec.partition(b"\r\n\r\n")                  # WARC headers
    head, _, body = http.partition(b"\r\n\r\n")              # HTTP headers
    if b"chunked" in head.lower():
        return b""                                           # CC stores de-chunked bodies; a chunked one is rare: skip
    return body[:2_000_000]


# ------------------------------------------------------------------ fetch
class Throttle:
    def __init__(self, rate: float):
        self.gap, self.next, self.lock = 1.0 / rate, time.monotonic(), threading.Lock()

    def wait(self):
        with self.lock:
            now = time.monotonic()
            t = max(now, self.next)
            self.next = t + self.gap
        if t > now:
            time.sleep(t - now)

    def slower(self):
        with self.lock:
            self.gap = min(self.gap * 1.5, 1.0)


def cmd_fetch(a) -> None:
    import boto3
    from botocore.config import Config
    s3 = boto3.client("s3", region_name="us-east-1",
                      config=Config(retries={"max_attempts": 4, "mode": "adaptive"}, max_pool_connections=a.workers + 4))
    rows = [l.rstrip("\n").split("\t") for l in open(a.pages)]
    log(f"{len(rows):,} pages to fetch at <= {a.rate}/s")
    th = Throttle(a.rate)
    stats = {"pages": 0, "with_hours": 0, "businesses": 0, "errors": 0}
    lock = threading.Lock()
    out = gzip.open(a.out, "wt")

    def one(r):
        url, domain, kind, fn, off, length, fetched = r[:7]
        for attempt in range(4):
            th.wait()
            try:
                raw = s3.get_object(Bucket="commoncrawl", Key=fn, Range=f"bytes={off}-{int(off) + int(length) - 1}")["Body"].read()
                break
            except Exception as e:
                code = ((getattr(e, "response", None) or {}).get("Error") or {}).get("Code", "")
                if code in ("SlowDown", "503", "ServiceUnavailable", "RequestLimitExceeded", "Throttling"):
                    th.slower(); time.sleep(2 * (attempt + 1)); continue
                with lock:
                    stats["errors"] += 1
                return
        else:
            with lock:
                stats["errors"] += 1
            return
        try:
            biz = businesses(split_record(raw))
        except Exception:
            biz = []
            with lock:
                stats["errors"] += 1
        with lock:
            stats["pages"] += 1
            if biz:
                stats["with_hours"] += 1
                stats["businesses"] += len(biz)
                for b in biz:
                    out.write(json.dumps(dict(b, url=url, domain=domain, kind=kind, crawled=fetched[:10]), ensure_ascii=False) + "\n")
            if stats["pages"] % 20000 == 0:
                log(json.dumps(stats))

    with ThreadPoolExecutor(a.workers) as ex:
        list(ex.map(one, rows, chunksize=64))
    out.close()
    log("done " + json.dumps(stats))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("select"); p.add_argument("--crawl", required=True); p.add_argument("--domains", required=True)
    p.add_argument("--out", required=True); p.add_argument("--mem", default="5GB"); p.add_argument("--threads", type=int, default=4)
    p.add_argument("--sample-files", type=int, default=0, help="scan only the first N index files (a test run)")
    p = sp.add_parser("fetch"); p.add_argument("--pages", required=True); p.add_argument("--out", required=True)
    p.add_argument("--rate", type=float, default=150); p.add_argument("--workers", type=int, default=24)
    a = ap.parse_args()
    {"select": cmd_select, "fetch": cmd_fetch}[a.cmd](a)


if __name__ == "__main__":
    main()
