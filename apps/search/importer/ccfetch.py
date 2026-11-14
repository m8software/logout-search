"""Fetch + parse the homepage captures located by the URL-index scan.

Each capture is one gzip member inside a Common Crawl WARC file; a range request
for exactly [offset, offset+length) returns it. Nothing here contacts the site
itself — every request goes to Common Crawl: data.commoncrawl.org (CC_ACCESS=https,
the default) or, on the disposable AWS build instance, s3://commoncrawl in
us-east-1 through the instance role (CC_ACCESS=s3).

English only (decided 2026-09-23): a capture is fetched when CC's detected
content languages list English first.
"""

from __future__ import annotations

import gzip
import html
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser

import psycopg

import docs_policy  # stdlib only; lives here so the importer image is self-contained (release code imports it from here)

CC_DATA = "https://data.commoncrawl.org"
CC_ACCESS = os.environ.get("CC_ACCESS", "https")
if CC_ACCESS not in ("https", "s3"):
    raise SystemExit(f"CC_ACCESS must be https or s3, not {CC_ACCESS!r}")
CC_BUCKET = "commoncrawl"
USER_AGENT = "logout-search-import/1 (personal search index)"
TEXT_CHARS = 2000
_WS = re.compile(r"\s+")


def log(msg: str) -> None:
    print(f"[ccfetch] {msg}", flush=True)


class _Page(HTMLParser):
    """Title, meta description, og:*, first h1, <html lang>, leading visible text."""

    _SKIP = {"script", "style", "noscript", "template", "svg", "head"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = self.desc = self.og_title = self.og_desc = self.h1 = self.lang = ""
        self.robots: list[str] = []
        self._in_title = self._in_h1 = False
        self._skip = 0
        self._text: list[str] = []
        self._n = 0

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "html" and not self.lang:
            self.lang = a.get("lang", "")[:16]
        elif tag == "title":
            self._in_title = True
        elif tag == "meta":
            name = (a.get("name") or a.get("property") or "").lower()
            content = a.get("content", "")
            if name == "description" and not self.desc:
                self.desc = content
            elif name == "og:title" and not self.og_title:
                self.og_title = content
            elif name == "og:description" and not self.og_desc:
                self.og_desc = content
            elif name in ROBOTS_META:
                self.robots.append(content)
            elif name == "tdm-reservation" and content.strip() == "1":
                self.robots.append("tdm-reservation")
        elif tag == "h1" and not self.h1:
            self._in_h1 = True
        if tag in self._SKIP and tag != "head":
            self._skip += 1

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        elif tag == "h1":
            self._in_h1 = False
        if tag in self._SKIP and tag != "head" and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if self._in_title:
            self.title += data
            return
        if self._skip:
            return
        if self._in_h1:
            self.h1 += data
        if self._n < TEXT_CHARS:
            t = data.strip()
            if t:
                self._text.append(t)
                self._n += len(t) + 1

    def text(self) -> str:
        return _WS.sub(" ", " ".join(self._text))[:TEXT_CHARS]


# What the page says to search engines (G3 D4c): its robots meta tags — the general one and the big engines' own —
# and the X-Robots-Tag / TDM-Reservation response headers. Stored per capture as docs.robots (docs_policy.py
# decides what a release does with it): '' = read and nothing said, NULL = this capture was never read for it.
ROBOTS_META = ("robots", "googlebot", "googlebot-news", "bingbot", "slurp", "duckduckbot")


def robots_of(http_head: bytes, meta: list) -> str:
    vals = list(meta)
    for line in http_head.split(b"\r\n")[1:]:
        k, _, v = line.decode("latin-1", "replace").partition(":")
        k = k.strip().lower()
        if k == "x-robots-tag":
            vals.append(v)
        elif k == "tdm-reservation" and v.strip() == "1":
            vals.append("tdm-reservation")
    return docs_policy.robots_field(vals)


def _clean(s: str, n: int) -> str:
    return _WS.sub(" ", html.unescape(s or "")).strip()[:n]


def parse_record(raw: bytes) -> dict:
    """gzip WARC record -> parsed fields. Raises ValueError on a malformed record."""
    rec = gzip.decompress(raw)
    parts = rec.split(b"\r\n\r\n", 2)
    if len(parts) < 3:
        raise ValueError("record has no HTTP body")
    http_head, body = parts[1], parts[2]
    status_line = http_head.split(b"\r\n", 1)[0].decode("latin-1", "replace")
    page = parse_body(http_head, body)
    page["status"] = (int(status_line.split()[1]) if len(status_line.split()) > 1 and status_line.split()[1].isdigit()
                      else None)
    return page


def parse_body(http_head: bytes, body: bytes) -> dict:
    """An HTTP response's headers + body -> title, description, h1, lang, text. Shared by
    the Common Crawl WARC reader (parse_record) and the Wayback Machine one (wayback.py)."""
    m = re.search(rb"charset=([\w-]+)", http_head, re.I) or re.search(rb'<meta[^>]+charset=["\']?([\w-]+)', body[:4096], re.I)
    charset = m.group(1).decode("ascii", "replace") if m else "utf-8"
    try:
        text = body.decode(charset, "replace")
    except LookupError:
        text = body.decode("utf-8", "replace")
    p = _Page()
    try:
        p.feed(text)
    except Exception:  # malformed HTML: keep whatever was parsed before the error
        pass
    return {
        "title": _clean(p.title or p.og_title, 300),
        "description": _clean(p.desc or p.og_desc, 500),
        "h1": _clean(p.h1, 300),
        "lang": p.lang.lower(),
        "text": p.text(),
        "robots": robots_of(http_head, p.robots),
    }


class _Throttle:
    """One shared request budget for all workers, plus a circuit breaker.

    Common Crawl asks for small requests to stay "below 10 per second" and for
    clients to slow down on any 4xx/5xx. On 2026-09-23, 16 workers (~14.6 req/s)
    tripped CloudFront's WAF: every request from the home IP got 403 — and the
    first version of this fetcher kept going at full speed. Now any 403/429/503
    pauses EVERY worker, halves the rate, and three trips in a row stop the job.
    """

    RECOVER_AFTER_S = 300  # clean seconds before speeding back up
    WINDOW = 100           # recent requests considered for the 503 share
    MAX_503_SHARE = 0.10   # above this, a 503 burst slows every worker down

    def __init__(self, rate: float) -> None:
        import collections
        import threading
        self.recent = collections.deque(maxlen=self.WINDOW)  # True = that request got a 503
        self.max_rate = rate
        self.rate = rate
        self.last_trip = time.monotonic()
        self.lock = threading.Lock()
        self.next_at = time.monotonic()
        self.paused_until = 0.0
        self.trips = 0
        self.stopped = False

    def wait(self) -> None:
        while True:
            with self.lock:
                if self.stopped:
                    raise RuntimeError("stopped by circuit breaker")
                now = time.monotonic()
                t = max(self.next_at, self.paused_until)
                if now >= t:
                    self.next_at = now + 1.0 / self.rate
                    return
            time.sleep(min(t - now, 1.0))

    def slow_down(self, code: int) -> None:
        with self.lock:
            now = time.monotonic()
            if now < self.paused_until:
                return  # this burst is already being handled
            self.trips += 1
            self.last_trip = now
            self.rate = max(0.5, self.rate / 2)
            pause = 60 * self.trips
            self.paused_until = now + pause
            log(f"HTTP {code} from Common Crawl — pausing all workers {pause}s, rate now {self.rate:.1f}/s "
                f"(trip {self.trips}/3)")
            if self.trips >= 3:
                self.stopped = True
                log("three slow-down signals — stopping; resume later, finished pages are kept")

    def saw_503(self) -> bool:
        """Record a 503; True when 503s now exceed MAX_503_SHARE of the window.

        CC documents 503 as its storage backend being overloaded — often for
        everyone — and asks for a per-request retry after >= 1 s, backing off to
        >= 10 s. That is a per-request signal, unlike 403 (the WAF blocking this
        IP). Halving every worker on each 503 held the fetch at ~0.8 req/s.
        """
        with self.lock:
            self.recent.append(True)
            if len(self.recent) >= self.WINDOW // 2 and sum(self.recent) / len(self.recent) > self.MAX_503_SHARE:
                self.recent.clear()
                return True
            return False

    def ok(self) -> None:
        # Halving without ever recovering left the first resume at 0.5 req/s (~23 h)
        # after a few 503s — which CC also sends to everyone when overloaded. Recover
        # 25 % per 5 clean minutes, never above the configured maximum.
        with self.lock:
            self.recent.append(False)
            self.trips = 0
            now = time.monotonic()
            if self.rate < self.max_rate and now - self.last_trip > self.RECOVER_AFTER_S:
                self.rate = min(self.max_rate, self.rate * 1.25)
                self.last_trip = now
                log(f"no slow-down for {self.RECOVER_AFTER_S}s — rate back up to {self.rate:.1f}/s")


class SlowDown(Exception):
    """CC asked us to slow down; the page is not failed, just not fetched yet."""


def _get(url: str, start: int, length: int, throttle: _Throttle) -> bytes:
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT, "Range": f"bytes={start}-{start + length - 1}"})
    for attempt in range(4):
        throttle.wait()
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                data = r.read()
            throttle.ok()
            return data
        except urllib.error.HTTPError as e:
            if e.code in (403, 429):
                # The WAF blocking this IP, or an explicit "too many": everyone stops.
                throttle.slow_down(e.code)
                continue
            if e.code == 503:
                # Backend overload: retry THIS request after >= 10 s (CC's guidance);
                # only a sustained 503 share slows every worker down.
                if throttle.saw_503():
                    throttle.slow_down(503)
                time.sleep(10 * (attempt + 1))
                continue
            raise
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if attempt == 3:
                raise
    raise SlowDown("rate limited")


_s3 = None


def _s3_client():
    """One shared client (thread-safe); boto3 is imported only in S3 mode."""
    global _s3
    if _s3 is None:
        import boto3
        from botocore.config import Config
        _s3 = boto3.client("s3", region_name="us-east-1", config=Config(
            retries={"max_attempts": 3, "mode": "standard"}, max_pool_connections=32))
    return _s3


# S3's "slow down" family. AccessDenied is NOT here: that is a permissions problem,
# and retrying it would only hammer the bucket.
_S3_SLOW = {"SlowDown", "503", "ServiceUnavailable", "RequestLimitExceeded", "Throttling"}


def _get_s3(key: str, start: int, length: int, throttle: _Throttle) -> bytes:
    for attempt in range(4):
        throttle.wait()
        try:
            r = _s3_client().get_object(Bucket=CC_BUCKET, Key=key, Range=f"bytes={start}-{start + length - 1}")
            data = r["Body"].read()
            throttle.ok()
            return data
        except Exception as e:  # botocore ClientError, matched by its error code
            code = ((getattr(e, "response", None) or {}).get("Error") or {}).get("Code", "")
            if code not in _S3_SLOW:
                raise
            if throttle.saw_503():
                throttle.slow_down(503)
            time.sleep(2 * (attempt + 1))
    raise SlowDown("rate limited")


# One homepage per domain, from whichever scanned crawl has it:
#   - English anywhere in CC's detected languages, or none detected (2026-09-24: English
#     first = 184,931; English not first +54,472; none detected +10,632 on CC-MAIN-2026-39).
#     An undetected one is still dropped when its HTML declares another language
#     (keep_language).
#   - skip a domain that already has a parsed homepage from ANY crawl, and never retry a
#     (domain, crawl) that already failed — docs' primary key is (domain, crawl).
#   - newest crawl first, English-first captures ahead of the rest; popular sites first.
FETCH_SQL = """
SELECT domain, crawl, host, path, protocol, warc_filename, warc_offset, warc_length, languages FROM (
    SELECT DISTINCT ON (c.domain) c.domain, c.crawl, c.host, c.path, c.protocol, c.warc_filename,
           c.warc_offset, c.warc_length, coalesce(c.languages, '') AS languages, s.tranco_rank
    FROM cc_captures c JOIN sites s ON s.domain = c.domain
    WHERE s.blocked_by IS NULL
      AND (c.languages LIKE '%%eng%%' OR coalesce(c.languages, '') = '')
      -- The site itself only (not developers.facebook.com for facebook.com).
      AND (c.host IN (c.domain, 'www.' || c.domain) OR c.kind = 'subroot')
      -- English first across crawls, also when one crawl is asked for: with --crawl the newest crawl's "tur,eng"
      -- page was fetched before an older crawl's English one was ever a candidate (thunderbird.net/tr/, 2026-10-08).
      AND (c.languages LIKE 'eng%%' OR NOT EXISTS (
            SELECT 1 FROM cc_captures e WHERE e.domain = c.domain AND e.crawl <> c.crawl AND e.languages LIKE 'eng%%'
              AND (e.host IN (e.domain, 'www.' || e.domain) OR e.kind = 'subroot')
              AND NOT EXISTS (SELECT 1 FROM docs x WHERE x.domain = e.domain AND x.crawl = e.crawl)))
      AND (%(crawl)s::text IS NULL OR c.crawl = %(crawl)s)
      AND NOT EXISTS (SELECT 1 FROM docs d WHERE d.domain = c.domain
                      AND (d.error IS NULL OR d.crawl = c.crawl))
    ORDER BY c.domain, (c.languages LIKE 'eng%%') DESC, c.crawl DESC) x
ORDER BY tranco_rank NULLS LAST
"""


# Pages inside the top sites (ccpages.py): same fetch, one row per URL in page_docs.
FETCH_PAGES_SQL = """
SELECT c.domain, c.crawl, c.host, c.path, c.protocol, c.warc_filename, c.warc_offset, c.warc_length,
       coalesce(c.languages, '')
FROM cc_pages c JOIN sites s ON s.domain = c.domain
WHERE s.blocked_by IS NULL AND (%(crawl)s::text IS NULL OR c.crawl = %(crawl)s)
  AND NOT EXISTS (SELECT 1 FROM page_docs d WHERE d.url = c.url)
ORDER BY s.tranco_rank NULLS LAST, length(c.path)
"""


def keep_language(cc_languages: str, html_lang: str) -> bool:
    """CC detected English somewhere -> keep. CC detected nothing -> keep unless the page
    itself declares a non-English language."""
    if "eng" in (cc_languages or ""):
        return True
    return not html_lang or html_lang.lower().startswith("en")


def cmd_fetch(conn: psycopg.Connection, crawl: str | None, workers: int, limit: int | None,
              rate: float = 8.0, pages: bool = False, procs: int = 1) -> None:
    table = "page_docs" if pages else "docs"
    rows = conn.execute((FETCH_PAGES_SQL if pages else FETCH_SQL) + (f" LIMIT {int(limit)}" if limit else ""),
                        {"crawl": crawl}).fetchall()
    log(f"{len(rows):,} {'pages' if pages else 'homepages'} to fetch with {procs} x {workers} workers, "
        f"at most {rate}/s from {'s3://' + CC_BUCKET if CC_ACCESS == 's3' else CC_DATA}")
    if not rows:
        return
    t0 = time.time()
    if procs <= 1:
        done, failed, nbytes = _fetch_rows(conn, rows, workers, rate, table)
    else:
        # Parsing is CPU-bound (~10 ms per 100 KB page) and threads share one core, so
        # the rows are split across processes, each with its own threads, its own share
        # of the request budget and its own database connection (2026-09-24: one
        # process sat at the 40/s cap it was given; parse alone tops out ~100/s/core).
        import multiprocessing
        parts = [rows[i::procs] for i in range(procs)]
        with multiprocessing.get_context("fork").Pool(procs) as pool:
            res = pool.starmap(_fetch_part, [(p, workers, rate / procs, table, i + 1) for i, p in enumerate(parts)])
        done, failed, nbytes = (sum(x) for x in zip(*res))
    log(f"done: {done:,} parsed, {failed:,} failed, {nbytes / 1e9:.2f} GB in {(time.time() - t0) / 60:.1f} min")


def _fetch_part(rows, workers: int, rate: float, table: str, part: int):
    """One process's share (forked): its own connection — never the parent's."""
    with psycopg.connect(autocommit=True) as conn:
        return _fetch_rows(conn, rows, workers, rate, table, f"[part {part}] ")


# ---- stub captures (2026-10-27) -------------------------------------------------------------------------------
# A capture that is a bot wall, a "checking your browser" page, a JavaScript shell or a redirect note parses to a
# page with no words: rc10 shipped 9,683 government pages that way (6.6 %; dep.nj.gov/njfw/* were 84-byte Incapsula
# stubs with HTTP 200), and a page without words cannot be found by any query. Such a capture is read once more from
# the site itself; what the site answers now is the page, and a stub both ways is an error row (never indexed).
STUB_MIN_CHARS = 200
STUB_RE = re.compile(
    r"incapsula incident|requested url was rejected|please wait while (your request is being verified|we validate your browser)|"
    r"validate your browser|verify(ing)? (you are|that you are) (a )?human|checking your browser|"
    r"sorry to interrupt css error|\bloading\.\.\.|redirecting[.\u2026]|click here if you are not redirected|"
    r"your browser is not supported|enable javascript|javascript is (required|disabled)|access denied|"
    r"currently experiencing technical difficulties|one more step|attention required", re.I)
LIVE_UA = USER_AGENT
LIVE_MAX_BYTES = 2_000_000


def decode_body(encoding: str, body: bytes) -> bytes:
    """The response bytes as the page's bytes: a gzip or deflate Content-Encoding is undone. urllib leaves it in
    place, and the Wayback Machine's raw mode replays the site's compressed answer as it was: 165 rows of the first
    rc11 Wayback pass (2026-10-27) were gzip members stored as text. The gzip magic number counts even without the
    header; a stream cut at LIVE_MAX_BYTES yields what it has. Brotli and zstd are refused: no decoder here."""
    enc = (encoding or "").strip().lower()
    if enc in ("br", "zstd"):
        raise ValueError(f"{enc}-encoded body")
    if body[:2] == b"\x1f\x8b" or enc in ("gzip", "x-gzip"):
        return zlib.decompressobj(zlib.MAX_WBITS | 32).decompress(body)
    if enc == "deflate":
        try:
            return zlib.decompressobj().decompress(body)
        except zlib.error:
            return zlib.decompressobj(-zlib.MAX_WBITS).decompress(body)
    return body


def stub_reason(page: dict | None) -> str | None:
    """Why a parsed page is a stub and not a page: no text, under STUB_MIN_CHARS of it, or a wall / shell pattern
    in its first words. None for a real page."""
    if not page:
        return "no page"
    text = (page.get("text") or "").strip()
    unreadable = sum(1 for c in text[:400] if c == "\ufffd" or (ord(c) < 32 and c not in "\t\n\r"))
    if unreadable >= 3:
        return f"binary ({unreadable} unreadable of the first 400)"
    head = " ".join(((page.get("title") or ""), (page.get("h1") or ""), text[:300]))
    m = STUB_RE.search(head)
    if m:
        return f"wall: {m.group(0).lower()[:40]}"
    if not text:
        return "no text"
    if len(text) < STUB_MIN_CHARS:
        return f"{len(text)} chars"
    return None


class _HostThrottle:
    """At most one live request per host every `gap` seconds, across threads: a site is never hammered."""
    def __init__(self, gap: float = 1.0):
        import threading
        self.gap, self.last, self.lock = gap, {}, threading.Lock()

    def wait(self, host: str) -> None:
        while True:
            with self.lock:
                due = self.last.get(host, 0.0) + self.gap
                now = time.time()
                if now >= due:
                    self.last[host] = now
                    return
            time.sleep(min(due - now, self.gap))


_live_throttle = _HostThrottle()


def fetch_live(url: str, timeout: float = 20.0) -> dict:
    """The page as the site serves it now: one GET, parsed like a capture. Raises on any HTTP or network error.
    Only used for a capture that parsed to a stub; the host is asked at most once a second."""
    host = urllib.parse.urlsplit(url).hostname or ""
    _live_throttle.wait(host)
    req = urllib.request.Request(url, headers={"User-Agent": LIVE_UA, "Accept": "text/html,*/*;q=0.5",
                                               "Accept-Language": "en-US,en;q=0.8"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        ctype = r.headers.get("Content-Type", "")
        if "html" not in ctype and "xml" not in ctype:
            raise ValueError(f"not html: {ctype[:40]}")
        body = r.read(LIVE_MAX_BYTES + 1)
        status = r.status
    if len(body) > LIVE_MAX_BYTES:
        body = body[:LIVE_MAX_BYTES]
    body = decode_body(r.headers.get("Content-Encoding", ""), body)
    page = parse_body(f"Content-Type: {ctype}".encode("latin-1", "replace"), body)
    page["status"] = status
    return page


WAYBACK_AVAILABLE = "https://archive.org/wayback/available"
_wayback_throttle = _HostThrottle(3.0)          # the Archive answered 429 to ~900 of 2,657 asks at 1.5 s (2026-10-27)


def fetch_wayback(url: str, timeout: float = 30.0) -> dict:
    """The newest Wayback Machine capture of `url`, raw (web/<ts>id_/), parsed like a capture. The Archive's own
    crawler gets through walls that stop a plain client (dep.nj.gov, Incapsula: a 22 KB real page on 2026-10-20
    beside an 84-byte stub in Common Crawl). Raises when the Archive has none."""
    _wayback_throttle.wait("archive.org")
    q = urllib.request.Request(f"{WAYBACK_AVAILABLE}?url={urllib.parse.quote(url, safe='')}", headers={"User-Agent": LIVE_UA})
    with urllib.request.urlopen(q, timeout=timeout) as r:
        import json
        snap = (json.load(r).get("archived_snapshots") or {}).get("closest") or {}
    if not snap.get("available") or snap.get("status") != "200":
        raise LookupError("no Wayback capture")
    ts = snap["timestamp"]
    _wayback_throttle.wait("archive.org")
    raw = urllib.request.Request(f"https://web.archive.org/web/{ts}id_/{url}", headers={"User-Agent": LIVE_UA})
    with urllib.request.urlopen(raw, timeout=timeout) as r:
        ctype = r.headers.get("Content-Type", "")
        body = r.read(LIVE_MAX_BYTES + 1)[:LIVE_MAX_BYTES]
        status = r.status
    body = decode_body(r.headers.get("Content-Encoding", ""), body)
    page = parse_body(f"Content-Type: {ctype}".encode("latin-1", "replace"), body)
    page["status"] = status
    page["wayback"] = ts
    return page


def page_or_live(url: str, page: dict, live=fetch_live, wayback=fetch_wayback) -> tuple[dict, str | None, str | None]:
    """(page to store, its crawl tag or None for the capture's, error): the capture when it is a page; else what the
    site serves now, tagged 'live:<date>'; else the Wayback Machine's newest capture, tagged 'wayback:<date>'; else
    an error naming every stub, so the row is written but never indexed."""
    why = stub_reason(page)
    if why is None:
        return page, None, None
    try:
        fresh = live(url)
        why2 = stub_reason(fresh)
    except Exception as e:  # noqa: BLE001  (any HTTP, network or parse failure: the capture's stub stands)
        fresh, why2 = None, f"{type(e).__name__}: {str(e)[:80]}"
    if why2 is None:
        return fresh, "live:" + time.strftime("%Y-%m-%d", time.gmtime()), None
    try:
        old = wayback(url)
        why3 = stub_reason(old)
    except Exception as e:  # noqa: BLE001
        old, why3 = None, f"{type(e).__name__}: {str(e)[:80]}"
    if why3 is None:
        return old, "wayback:" + old["wayback"][:8], None
    return page, None, f"stub: {why}; live: {why2}; wayback: {why3}"


def _fetch_rows(conn, rows, workers: int, rate: float, table: str, tag: str = ""):
    throttle = _Throttle(rate)

    def work(r):
        domain, cr, host, path, proto, wf, off, ln, cc_langs = r
        try:
            raw = (_get_s3(wf, off, ln, throttle) if CC_ACCESS == "s3"
                   else _get(f"{CC_DATA}/{wf}", off, ln, throttle))
            page = parse_record(raw)
            if not keep_language(cc_langs, page["lang"]):
                return r, None, len(raw), f"not English (html lang {page['lang'][:20]})"
            if table == "page_docs":
                page, tag_crawl, err = page_or_live(f"{proto}://{host}{path}", page)
                if err:
                    return r, None, len(raw), err
                if tag_crawl:
                    r = (domain, tag_crawl) + tuple(r[2:])
            return r, page, len(raw), None
        except (SlowDown, RuntimeError):
            return r, None, 0, "SKIP"  # not fetched — no row, so the next run retries it
        except Exception as e:
            return r, None, 0, f"{type(e).__name__}: {e}"[:200]

    done = failed = nbytes = 0
    t0 = time.time()
    batch = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for r, page, n, err in pool.map(work, rows):
            domain, cr, host, path, proto = r[:5]
            nbytes += n
            if err == "SKIP":
                continue
            if err:
                failed += 1
                batch.append((domain, cr, f"{proto}://{host}{path}", None, None, None, None, None, None, err, None))
            else:
                done += 1
                batch.append((domain, cr, f"{proto}://{host}{path}", page["status"], page["title"],
                              page["description"], page["h1"], page["lang"], page["text"], None, page["robots"]))
            if len(batch) >= 500:
                _write(conn, batch, table)
                batch = []
            if (done + failed) % 2000 == 0:
                el = time.time() - t0
                log(f"{tag}{done + failed:,}/{len(rows):,} ({failed:,} failed), {nbytes / 1e9:.2f} GB, "
                    f"{(done + failed) / el:.1f}/s")
    if batch:
        _write(conn, batch, table)
    return done, failed, nbytes


def _nul_free(v):
    # Postgres text cannot hold NUL; some captured pages contain them (2026-09-23).
    return v.replace("\x00", "") if isinstance(v, str) else v


def _write(conn, batch, table: str = "docs"):
    if table not in ("docs", "page_docs"):
        raise ValueError(table)
    batch = [tuple(_nul_free(v) for v in b) for b in batch]
    robots = ", robots" if table == "docs" else ""       # page_docs has no such column: a page's description never ships
    with conn.transaction():
        with conn.cursor().copy(
            f"COPY {table} (domain, crawl, url, http_status, title, description, h1, lang, body, error{robots}) FROM STDIN"
        ) as cp:
            for b in batch:                              # wayback.py and the pages path pass no robots value
                cp.write_row((tuple(b) + (None,))[:11] if robots else b[:10])


# ---- stub backfill: rows already in page_docs that are stubs, read again from the site -----------------------
BINARY_RE = "[\x01-\x08\x0e-\x1f\ufffd]"   # control characters or the replacement character in a body's first 400

STUBS_SQL = """
SELECT url FROM page_docs
WHERE (error IS NULL OR (%(retry)s::boolean AND error LIKE 'stub%%' AND error NOT LIKE '%%; live: no text%%'
                                                 AND error !~ '; live: [0-9]+ chars'))   -- a JavaScript shell is a shell in the Archive too
  AND (%(gov)s::boolean IS FALSE OR domain LIKE '%%.gov' OR domain LIKE 'state.__.us')
  AND (error IS NOT NULL OR length(coalesce(body, '')) < %(min)s
       OR left(coalesce(body, ''), 300) ~* %(re)s OR coalesce(title, '') ~* %(re)s OR coalesce(h1, '') ~* %(re)s   -- stub_reason's head
       OR left(coalesce(body, ''), 400) ~ %(binary)s)   -- compressed bytes stored as text (decode_body)
ORDER BY domain COLLATE "C", url COLLATE "C"
"""


def cmd_refetch_stubs(conn, workers: int, limit: int | None, gov_only: bool, dry_run: bool = False,
                      retry_errors: bool = False) -> None:
    """page_docs rows whose capture was a stub (bot wall, shell, redirect, under STUB_MIN_CHARS): the page as the
    site serves it now replaces the row (crawl 'live:<date>'); a stub both ways becomes an error row. Nothing is
    deleted: a row that stays a stub keeps its address with the error, so the next crawl's capture can replace it."""
    rows = [r[0] for r in conn.execute(STUBS_SQL, {"gov": gov_only, "min": STUB_MIN_CHARS, "re": STUB_RE.pattern,
                                                   "retry": retry_errors, "binary": BINARY_RE}).fetchall()]
    if limit:
        rows = rows[:limit]
    log(f"{len(rows):,} stub rows to read again from their sites{' (government only)' if gov_only else ''}, "
        f"{workers} workers, one request per host per second{' — dry run' if dry_run else ''}")
    if dry_run:
        for u in rows[:20]:
            log("  " + u)
        return
    t0 = time.time(); fixed = still = 0

    def work(url):
        # the stored row is the stub: page_or_live reads the site, then the Wayback Machine
        fresh, tag, err = page_or_live(url, {"text": "", "title": "", "h1": ""})
        return url, (fresh if tag else None), tag, err

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for i, (url, fresh, tag, err) in enumerate(pool.map(work, rows), 1):
            if fresh:
                fixed += 1
                conn.execute("UPDATE page_docs SET crawl = %s, fetched_at = now(), http_status = %s, title = %s, description = %s, "
                             "h1 = %s, lang = %s, body = %s, error = NULL WHERE url = %s",
                             (tag, fresh["status"], _nul_free(fresh["title"]),
                              _nul_free(fresh["description"]), _nul_free(fresh["h1"]), fresh["lang"], _nul_free(fresh["text"]), url))
            else:
                still += 1
                conn.execute("UPDATE page_docs SET error = %s WHERE url = %s", (err, url))
            if i % 500 == 0:
                log(f"{i:,}/{len(rows):,}: {fixed:,} pages read from their sites, {still:,} still stubs, {i / (time.time() - t0):.1f}/s")
    log(f"done: {fixed:,} pages read from their sites or the Wayback Machine, {still:,} still stubs (error rows, never indexed) in {(time.time() - t0) / 60:.1f} min")


# ---- robots backfill (G3 D4c): captures parsed before docs.robots existed -----------------------------------
# Read again from Common Crawl, the same range request as the first time; nothing else about the row changes. Only
# rows a release could show a description for. A capture that cannot be read stays NULL, and a published release
# shows no description for a NULL row (docs_policy.py).
ROBOTS_SQL = """
SELECT d.domain, d.crawl, c.warc_filename, c.warc_offset, c.warc_length
FROM docs d JOIN cc_captures c ON c.domain = d.domain AND c.crawl = d.crawl
     AND c.protocol || '://' || c.host || c.path = d.url
WHERE d.error IS NULL AND d.robots IS NULL AND left(d.crawl, 7) <> 'wayback' AND coalesce(d.description, '') <> ''
ORDER BY d.domain
"""


def cmd_robots(conn: psycopg.Connection, workers: int, limit: int | None, rate: float) -> None:
    rows = conn.execute(ROBOTS_SQL).fetchall()
    if limit:
        rows = rows[:limit]
    log(f"{len(rows):,} homepage captures to read again for their robots statements, {workers} workers, <= {rate:g} req/s")
    throttle = _Throttle(rate)

    def work(r):
        domain, cr, wf, off, ln = r
        try:
            raw = _get_s3(wf, off, ln, throttle) if CC_ACCESS == "s3" else _get(f"{CC_DATA}/{wf}", off, ln, throttle)
            return domain, cr, parse_record(raw)["robots"]
        except Exception:                      # not read: the row stays NULL and the next run tries it again
            return domain, cr, None

    done = failed = said = 0
    t0 = time.time()
    batch = []

    def flush():
        with conn.transaction():
            conn.cursor().executemany("UPDATE docs SET robots = %s WHERE domain = %s AND crawl = %s", batch)
        batch.clear()

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for domain, cr, rb in pool.map(work, rows):
            if rb is None:
                failed += 1
            else:
                done += 1
                said += bool(rb)
                batch.append((rb, domain, cr))
            if len(batch) >= 500:
                flush()
            if (done + failed) % 2000 == 0:
                log(f"{done + failed:,}/{len(rows):,} ({failed:,} not read, {said:,} with a statement), "
                    f"{(done + failed) / max(time.time() - t0, 1e-9):.1f}/s")
    if batch:
        flush()
    log(f"done: {done:,} read ({said:,} with a statement), {failed:,} not read (still NULL)")


# The same backfill with no database, for the AWS build instance (infra/build-aws/jobs/cc-robots.sh, CC_ACCESS=s3):
#   python ccfetch.py robots-tsv IN.tsv OUT.tsv [--procs N] [--workers N] [--rate R]
# IN: domain, crawl, warc_filename, warc_offset, warc_length (ROBOTS_SQL's columns). OUT: domain, crawl, robots for
# every capture that was read ('' = nothing said); a capture that could not be read has no line and stays NULL at home.
def _robots_part(rows, workers: int, rate: float, out: str, tag: str):
    throttle = _Throttle(rate)

    def work(r):
        domain, cr, wf, off, ln = r
        try:
            raw = _get_s3(wf, int(off), int(ln), throttle) if CC_ACCESS == "s3" else _get(f"{CC_DATA}/{wf}", int(off), int(ln), throttle)
            return domain, cr, parse_record(raw)["robots"]
        except Exception:
            return domain, cr, None

    done = failed = 0
    t0 = time.time()
    with open(out, "w", encoding="utf-8") as fo, ThreadPoolExecutor(max_workers=workers) as pool:
        for domain, cr, rb in pool.map(work, rows):
            if rb is None:
                failed += 1
            else:
                done += 1
                fo.write(f"{domain}\t{cr}\t{rb}\n")
            if (done + failed) % 5000 == 0:
                log(f"{tag}{done + failed:,}/{len(rows):,} ({failed:,} not read), {(done + failed) / max(time.time() - t0, 1e-9):.1f}/s")
    return done, failed


def cmd_robots_tsv(inp: str, out: str, procs: int, workers: int, rate: float) -> None:
    rows = [tuple(l.rstrip("\n").split("\t")) for l in open(inp, encoding="utf-8") if l.strip()]
    bad = [r for r in rows if len(r) != 5]
    if bad:
        raise SystemExit(f"{inp}: {len(bad)} lines do not have 5 columns, e.g. {bad[0]!r}")
    log(f"{len(rows):,} captures to read for their robots statements: {procs} x {workers} workers, <= {rate:g} req/s, access {CC_ACCESS}")
    if procs <= 1:
        res = [_robots_part(rows, workers, rate, out, "")]
    else:
        import multiprocessing
        parts = [rows[i::procs] for i in range(procs)]
        with multiprocessing.Pool(procs) as pool:
            res = pool.starmap(_robots_part, [(p, workers, rate / procs, f"{out}.part{i}", f"[part {i + 1}] ") for i, p in enumerate(parts)])
        with open(out, "w", encoding="utf-8") as fo:
            for i in range(procs):
                with open(f"{out}.part{i}", encoding="utf-8") as fi:
                    fo.write(fi.read())
                os.remove(f"{out}.part{i}")
    done, failed = sum(r[0] for r in res), sum(r[1] for r in res)
    log(f"done: {done:,} read, {failed:,} not read -> {out}")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    rt = sub.add_parser("robots-tsv")
    rt.add_argument("inp")
    rt.add_argument("out")
    rt.add_argument("--procs", type=int, default=1)
    rt.add_argument("--workers", type=int, default=8)
    rt.add_argument("--rate", type=float, default=8.0)
    a = ap.parse_args()
    cmd_robots_tsv(a.inp, a.out, a.procs, a.workers, a.rate)
