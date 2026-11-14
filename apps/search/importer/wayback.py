"""Homepages from the Wayback Machine for real sites Common Crawl never captures
(2026-09-24).

Many of the biggest sites block Common Crawl's crawler: google.com, amazon.com,
twitter.com, tiktok.com, yahoo.com, live.com, msn.com had no capture at all. 17,069 of
the Tranco top 100k are real websites (they have a Wikipedia/Wikidata official-site
link) with no parsed homepage. The Internet Archive has them.

Per site, one at a time (run on AWS — never from the home IP):
  1. CDX: the newest archived 200 text/html capture of the homepage
     (https://web.archive.org/cdx/search/cdx; limit 60 requests/minute average)
  2. the raw page as captured (web/<timestamp>id_/<url>: no Wayback toolbar or rewriting)
  3. the same parser as Common Crawl pages (ccfetch.parse_body) -> a docs row

Politeness, because the Archive blocks IPs that ignore 429s (1 h, doubling each time):
  - at most one site per PER_SITE_S seconds (default 5: <= 24 requests/minute);
  - a 429 or 503 waits Retry-After (or 60 s) and retries once; a second one within
    10 minutes stops the whole job — what was fetched is kept and exported.

    search_import.py wayback [--per-site S] [--limit N]   sites table (ordered by rank) -> docs
"""

from __future__ import annotations

import time
import urllib.error
import urllib.parse
import urllib.request

from ccfetch import _write, keep_language, log as _log, parse_body

CDX = "https://web.archive.org/cdx/search/cdx"
WEB = "https://web.archive.org/web"
USER_AGENT = "logout-search-import/1 (personal search index; polite, one site per 5 s)"
PER_SITE_S = 5.0


class Blocked(RuntimeError):
    """The Archive asked us to stop (repeated 429/503): end the job, keep the results."""


def log(msg: str) -> None:
    _log(f"[wayback] {msg}")


class _Polite:
    def __init__(self, per_site: float, sleep=time.sleep, now=time.monotonic) -> None:
        self.per_site = per_site
        self.sleep = sleep
        self.now = now
        self.last_start = None
        self.last_limited = None

    def pace(self) -> None:
        """Wait so consecutive sites start at least per_site seconds apart."""
        t = self.now()
        if self.last_start is not None and t - self.last_start < self.per_site:
            self.sleep(self.per_site - (t - self.last_start))
        self.last_start = self.now()

    def limited(self, retry_after: str | None) -> None:
        """A 429/503. The first waits; a second within 10 minutes raises Blocked."""
        t = self.now()
        if self.last_limited is not None and t - self.last_limited < 600:
            raise Blocked("rate-limited twice within 10 minutes — stopping")
        self.last_limited = t
        try:
            wait = min(600.0, max(60.0, float(retry_after or 60)))
        except ValueError:
            wait = 60.0
        log(f"rate-limited — waiting {wait:.0f} s")
        self.sleep(wait)


def _open(url: str, polite: _Polite, opener=urllib.request.urlopen):
    """GET with one polite retry on 429/503. Returns (headers bytes, body bytes)."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in (1, 2):
        try:
            with opener(req, timeout=60) as r:
                head = "".join(f"{k}: {v}\r\n" for k, v in r.headers.items()).encode("latin-1", "replace")
                return head, r.read(3_000_000)
        except urllib.error.HTTPError as e:
            if e.code in (429, 503) and attempt == 1:
                polite.limited(e.headers.get("Retry-After") if e.headers else None)
                continue
            if e.code in (429, 503):
                raise Blocked(f"HTTP {e.code} again after waiting")
            raise
    raise RuntimeError("unreachable")


def cdx_query(domain: str) -> str:
    """The newest 200 text/html capture of the homepage. The CDX index canonicalises
    away www., so domain/ also matches www.domain/."""
    q = urllib.parse.urlencode([("url", domain + "/"), ("fl", "timestamp,original"),
                                ("filter", "statuscode:200"), ("filter", "mimetype:text/html"),
                                ("limit", "-1"), ("from", "2024")])
    return f"{CDX}?{q}"


def parse_cdx(text: str) -> tuple[str, str] | None:
    """'20260901123456 https://www.google.com/' -> (timestamp, url); None when no capture."""
    for line in text.splitlines()[::-1]:
        p = line.split()
        if len(p) >= 2 and p[0].isdigit() and len(p[0]) == 14 and p[1].startswith("http"):
            return p[0], p[1]
    return None


def raw_url(ts: str, original: str) -> str:
    return f"{WEB}/{ts}id_/{original}"


def fetch_one(domain: str, polite: _Polite, opener=urllib.request.urlopen):
    """-> (url, crawl, page dict or None, error or None)."""
    _, body = _open(cdx_query(domain), polite, opener)
    hit = parse_cdx(body.decode("utf-8", "replace"))
    if not hit:
        return None, None, None, "no Wayback capture since 2024"
    ts, original = hit
    head, html = _open(raw_url(ts, original), polite, opener)
    page = parse_body(head, html)
    page["status"] = 200
    if not keep_language("", page["lang"]):
        return original, f"wayback-{ts[:8]}", None, f"not English (html lang {page['lang'][:20]})"
    return original, f"wayback-{ts[:8]}", page, None


def cmd_wayback(conn, per_site: float = PER_SITE_S, limit: int | None = None) -> None:
    rows = [r[0] for r in conn.execute(
        "SELECT domain FROM sites s WHERE NOT EXISTS (SELECT 1 FROM docs d WHERE d.domain = s.domain) "
        "ORDER BY tranco_rank NULLS LAST, domain" + (f" LIMIT {int(limit)}" if limit else ""))]
    log(f"{len(rows):,} sites, one per {per_site:g} s (~{len(rows) * per_site / 3600:.1f} h)")
    polite = _Polite(per_site)
    done = failed = 0
    batch = []
    t0 = time.time()
    try:
        for i, domain in enumerate(rows, 1):
            polite.pace()
            try:
                url, crawl, page, err = fetch_one(domain, polite)
            except Blocked:
                raise
            except Exception as e:                     # one bad site never stops the run
                url, crawl, page, err = None, None, None, f"{type(e).__name__}: {e}"[:200]
            url = url or f"https://{domain}/"
            crawl = crawl or "wayback"
            if page:
                done += 1
                batch.append((domain, crawl, url, page["status"], page["title"], page["description"],
                              page["h1"], page["lang"], page["text"], None))
            else:
                failed += 1
                batch.append((domain, crawl, url, None, None, None, None, None, None, err))
            if len(batch) >= 100:
                _write(conn, batch)
                batch = []
            if i % 200 == 0:
                log(f"{i:,}/{len(rows):,}: {done:,} parsed, {failed:,} without a usable page, "
                    f"{(time.time() - t0) / 3600:.1f} h")
    except Blocked as e:
        log(f"STOPPED: {e}")
    finally:
        if batch:
            _write(conn, batch)
        log(f"done: {done:,} parsed, {failed:,} without a usable page in {(time.time() - t0) / 3600:.1f} h")
