#!/usr/bin/env python3
"""Page-level index from the lists sites publish themselves (2026-11-10): sitemaps and feeds. Nothing is
crawled and nothing a visitor searched is used. This is the PROBE step: it reads a sample of sites and measures what
a full harvest would get, before anything big runs.

    sitemap-harvest.py probe  TARGETS OUT.jsonl [--workers 4] [--rate 10] [--limit N]   one JSON line per site
    sitemap-harvest.py report OUT.jsonl [--examples 30]                                  coverage table (markdown)

TARGETS: one URL per line, or a TSV whose first column is the site's URL (extra columns rank, band, wiki, doc_id
are carried into the output when present).

Per site: robots.txt, then the home page once (feed links: rel=alternate rss/atom), then the Sitemap: lines of
robots.txt (else /sitemap.xml), indexes followed (newest lastmod first), URL sets read as a STREAM and abandoned
after 5,000 URLs or 20 MB (decompressed) per site. For every URL: loc, lastmod, and the title a news/video/image
sitemap carries. Each announced feed (at most 3) is fetched once: title, link, date of up to 200 items.

Fetch discipline (as site-nav.py): robots.txt obeyed for every path fetched (its own user agent, then *; a 4xx
robots.txt means allowed, a 5xx or a timeout means not allowed), redirects followed by hand so each hop is checked
against the robots.txt of ITS host, one request at a time per host, 10 s timeout, never a retry of a 4xx, gzip
handled (header or magic bytes), a user agent that names a contact, at most --rate requests a second over all
threads. Resumable: sites already in OUT are skipped. Stdlib only.
"""
from __future__ import annotations

import argparse
import collections
import html.parser
import json
import random
import re
import socket
import ssl
import statistics
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zlib
from concurrent.futures import ThreadPoolExecutor

UA = "logout-search-sitemap/0.1 (+https://search.logout.cloud; help@logout.cloud)"
UA_TOKEN = "logout-search-sitemap"
HARVEST_UA = "Mozilla/5.0 (compatible; sitemap-reader)"     # the card-sites harvest: generic, no name, contact or project
HARVEST_TOKEN = "sitemap-reader"
MAX_RESPONSE = 5_000_000            # raw bytes read from one response
TIMEOUT = 10
MAX_URLS = 5000
MAX_XML_BYTES = 20_000_000          # decompressed XML read per site, all its sitemaps together
MAX_SITEMAP_FILES = 30              # sitemap files fetched per site (index children included)
MAX_DEPTH = 3
MAX_SITEMAP_FILES = 30              # sitemap files (index children included) fetched per site
MAX_FEEDS = 3
MAX_FEED_ITEMS = 200
MAX_FEED_BYTES = 2_000_000
MAX_HOME_BYTES = 1_500_000
MAX_ROBOTS_BYTES = 500_000
MAX_HOPS = 5
FILE_SECONDS = 60                   # wall-clock limit for reading one sitemap file (a slow drip)


# --------------------------------------------------------------------------------------------- address -> words
_HEX = re.compile(r"^(?:[0-9a-f]{8,}|[0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{12})$")
_EXT = re.compile(r"\.(?:html?|php\d?|aspx?|jsp|cfm|shtml|xml|json|pdf|jpe?g|png|gif|webp|svg|mp[34]|txt)$", re.I)


def slug_words(url: str) -> list[str]:
    """The real words of an address path: split on - _ / . + %20, lower-cased; ids (any token with a digit), hex
    strings, dates, one-letter bits and vowel-less long runs (hashes, base64) are dropped. Letters of any script
    count as words here (English-only is a later filter)."""
    try:
        path = urllib.parse.unquote(urllib.parse.urlsplit(url).path)
    except ValueError:                                   # "http://[bad/..." in a sitemap
        return []
    path = _EXT.sub("", path)
    out = []
    for tok in re.split(r"[-_/.+\s,;:~=]+", path.lower()):
        if len(tok) < 2 or len(tok) > 24 or not tok.isalpha() or _HEX.match(tok):
            continue
        if len(tok) > 4 and tok.isascii() and not re.search(r"[aeiouy]", tok):
            continue
        out.append(tok)
    return out


# --------------------------------------------------------------------------------------------- polite fetching
class Limiter:
    """At most `rate` request starts a second over every thread."""
    def __init__(self, rate: float):
        self.gap, self.next, self.lock = 1.0 / rate, 0.0, threading.Lock()

    def wait(self):
        with self.lock:
            now = time.monotonic()
            at = max(now, self.next)
            self.next = at + self.gap
        if at > now:
            time.sleep(at - now)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


class Fail(Exception):
    def __init__(self, kind: str, detail: str = ""):
        super().__init__(f"{kind} {detail}")
        self.kind, self.detail = kind, detail


class Robots:
    """robots.txt as the 2022 standard (RFC 9309) reads it: the group naming our token (else *), the longest matching
    rule wins, Allow beats Disallow on a tie, * matches any run and a trailing $ the end. Python 3.12's
    urllib.robotparser matches literal prefixes only ("Disallow: /*/feed/" allows /blog/feed/), so it is not used."""
    def __init__(self):
        self.groups: list[tuple[list[str], list[tuple[bool, re.Pattern, int]]]] = []
        self.delays: dict[str, float] = {}

    def crawl_delay(self, token: str) -> float:
        """Crawl-delay seconds of the group naming our token, else of *; 0 when none."""
        for a, d in self.delays.items():
            if a != "*" and a in token.lower():
                return d
        return self.delays.get("*", 0.0)

    def parse(self, lines):
        self.groups = []
        self.delays = {}
        agents: list[str] = []
        rules: list = []
        last_rule = False
        for ln in lines:
            ln = ln.split("#", 1)[0].strip()
            if ":" not in ln:
                continue
            k, v = (x.strip() for x in ln.split(":", 1))
            k = k.lower()
            if k == "user-agent":
                if last_rule:
                    self.groups.append((agents, rules))
                    agents, rules, last_rule = [], [], False
                agents.append(v.lower())
            elif k == "crawl-delay":
                last_rule = True
                try:
                    for a in agents:
                        self.delays[a] = max(0.0, float(v))
                except ValueError:
                    pass
            elif k in ("allow", "disallow"):
                last_rule = True
                if v:
                    pat = re.escape(v).replace(r"\*", ".*")
                    pat = pat[:-2] + "$" if pat.endswith(r"\$") else pat
                    rules.append((k == "allow", re.compile(pat), len(v)))
        if agents:
            self.groups.append((agents, rules))

    def can_fetch(self, token: str, path: str) -> bool:
        rules = []
        named = [g for g in self.groups if any(a != "*" and a in token.lower() for a in g[0])]
        for g in (named or [g for g in self.groups if "*" in g[0]]):
            rules += g[1]
        best = None
        for allow, rx, n in rules:
            if rx.match(path) and (best is None or n > best[1] or (n == best[1] and allow)):
                best = (allow, n)
        return True if best is None else best[0]


def ok_url(u: str) -> bool:
    """An absolute http(s) address with a host. A Sitemap: line or index entry can be relative or scheme-less; those
    are resolved against their parent first, and what is still not absolute is dropped (it used to crash a site)."""
    try:
        p = urllib.parse.urlsplit(u)
    except ValueError:
        return False
    return p.scheme in ("http", "https") and bool(p.hostname)


def error_kind(e: BaseException) -> str:
    if isinstance(e, urllib.error.HTTPError):
        return f"http{e.code // 100}xx"
    r = getattr(e, "reason", e)
    if isinstance(r, socket.gaierror):
        return "dns"
    if isinstance(r, (socket.timeout, TimeoutError)):
        return "timeout"
    if isinstance(r, ssl.SSLError):
        return "tls"
    if isinstance(r, ConnectionRefusedError):
        return "refused"
    if isinstance(r, OSError):
        return "connect"
    return type(e).__name__.lower()


def new_stats() -> dict:
    return {"requests": 0, "bytes": 0, "errors": collections.Counter()}


class Net:
    """The rate limiter, the per-host locks and the robots.txt cache, shared by every thread. `stats` (requests,
    network bytes, errors by kind) is passed in per site."""
    def __init__(self, rate: float = 10.0, opener=None, host_gap: float = 0.0, bw: float = 0.0, ua: str = UA,
                 token: str = UA_TOKEN):
        self.ua, self.token = ua, token
        self.host_gap = host_gap                         # seconds between two requests to one host (and its Crawl-delay)
        self.bw, self.bw_next, self.bwl = bw, 0.0, threading.Lock()   # bytes a second over all threads (0 = no limit)
        self.last: dict[str, float] = {}                 # host -> when its last request ended
        self.crawl_delay: dict[str, float] = {}
        self.window: collections.deque = collections.deque()          # (time, failed) of the last 5 minutes
        self.wl = threading.Lock()
        self.stopped = ""                                # why the run stopped (too many failures), else ""
        self.limiter = Limiter(rate)
        self.opener = opener or urllib.request.build_opener(NoRedirect)
        self.host_locks: dict[str, threading.Lock] = collections.defaultdict(threading.Lock)
        self.hl = threading.Lock()
        self.robots: dict[str, tuple] = {}
        self.rl = threading.Lock()

    def _lock(self, host: str) -> threading.Lock:
        with self.hl:
            return self.host_locks[host]

    def throttle(self, n: int):
        """Account n network bytes against the bandwidth cap; sleeps when over it."""
        if not self.bw:
            return
        with self.bwl:
            now = time.monotonic()
            at = max(now, self.bw_next)
            self.bw_next = at + n / self.bw
        if at > now:
            time.sleep(at - now)

    def note(self, failed: bool):
        """Count a request outcome; stop the whole run when over 30% of the last 5 minutes' requests (at least 100)
        failed. Failures are timeouts, DNS, TLS, connect errors, 5xx and 429; a 404 or a robots refusal is not one."""
        now = time.monotonic()
        with self.wl:
            self.window.append((now, failed))
            while self.window and now - self.window[0][0] > 300:
                self.window.popleft()
            n = len(self.window)
            f = sum(1 for _t, x in self.window if x)
            if n >= 100 and f / n > 0.30 and not self.stopped:
                self.stopped = f"{f} of the last {n} requests failed ({100 * f // n}%) in 5 minutes"

    def request(self, url: str, stats: dict, accept: str):
        """One request, no redirect following, held under the host's lock until `with` is left. Returns an open
        response (status 2xx) or (status, Location) for a redirect. Raises Fail."""
        if not ok_url(url):
            stats["errors"]["badurl"] += 1
            raise Fail("badurl", url[:120])
        if self.stopped:
            raise Fail("stopped", self.stopped)
        host = urllib.parse.urlsplit(url).netloc.lower()
        gap = max(self.host_gap, self.crawl_delay.get(host, 0.0))
        if gap:                                          # called under the host's lock: nobody else asks this host
            wait = self.last.get(host, 0.0) + gap - time.monotonic()
            if wait > 0:
                time.sleep(wait)
        req = urllib.request.Request(url, headers={"User-Agent": self.ua, "Accept": accept, "Accept-Encoding": "gzip",
                                                   "Accept-Language": "en"})
        stats["requests"] += 1
        self.limiter.wait()
        try:
            r = self.opener.open(req, timeout=TIMEOUT)
            self.note(False)
            return r
        except urllib.error.HTTPError as e:
            loc = e.headers.get("Location")
            e.close()
            self.note(e.code >= 500 or e.code == 429)
            if 300 <= e.code < 400 and loc:
                return (e.code, loc)
            stats["errors"][f"http{e.code // 100}xx"] += 1
            raise Fail(f"http{e.code // 100}xx", str(e.code))
        except Exception as e:
            k = error_kind(e)
            self.note(True)
            stats["errors"][k] += 1
            raise Fail(k, str(e)[:120])

    def fetch(self, url: str, stats: dict, accept: str, consume, check_robots: bool = True):
        """GET `url`, following redirects by hand (robots.txt checked for every hop's host), and call
        consume(response, final_url) inside the host's lock, so a host sees one request at a time. Returns
        consume's value."""
        for _ in range(MAX_HOPS):
            if check_robots and not self.allowed(url, stats):
                stats["errors"]["robots"] += 1
                raise Fail("robots", url)
            host = urllib.parse.urlsplit(url).netloc.lower()
            with self._lock(host):
                try:
                    r = self.request(url, stats, accept)
                    if isinstance(r, tuple):
                        url = urllib.parse.urljoin(url, r[1])
                        continue
                    try:
                        return consume(r, url)
                    except Fail:
                        raise
                    except Exception as e:
                        k = error_kind(e)
                        self.note(True)
                        stats["errors"][k] += 1
                        raise Fail(k, str(e)[:120])
                    finally:
                        r.close()
                finally:
                    self.last[host] = time.monotonic()
        stats["errors"]["redirects"] += 1
        raise Fail("redirects", url)

    def robots_for(self, origin: str, stats: dict):
        """(parser, Sitemap: lines, status) for an origin; fetched once per run."""
        with self.rl:
            hit = self.robots.get(origin)
        if hit:
            return hit
        rp = Robots()
        sitemaps: list[str] = []
        status = "ok"

        def read(r, _u):
            data = r.read(MAX_ROBOTS_BYTES)
            stats["bytes"] += len(data)
            self.throttle(len(data))
            return gunzip(data).decode("utf-8", "replace").splitlines()
        try:
            lines = self.fetch(origin + "/robots.txt", stats, "text/plain,*/*;q=0.5", read, check_robots=False)
            rp.parse(lines)
            sitemaps = [urllib.parse.urljoin(origin + "/robots.txt", m.group(1)) for ln in lines
                        if (m := re.match(r"\s*sitemap\s*:\s*(\S+)", ln, re.I))]
            sitemaps = [u for u in sitemaps if ok_url(u)]
            self.crawl_delay[urllib.parse.urlsplit(origin).netloc.lower()] = rp.crawl_delay(self.token)
        except Fail as f:
            if f.kind == "http4xx" and f.detail != "429":
                stats["errors"]["http4xx"] -= 1          # a missing robots.txt is an answer, not a failure
                rp.parse([]); status = "none"                              # no robots.txt: everything allowed
            else:
                rp.parse(["User-agent: *", "Disallow: /"]); status = f.kind  # 5xx, timeout, dns: not allowed
        res = (rp, sitemaps, status)
        with self.rl:
            self.robots[origin] = res
        return res

    def allowed(self, url: str, stats: dict) -> bool:
        p = urllib.parse.urlsplit(url)
        rp, _s, _st = self.robots_for(f"{p.scheme}://{p.netloc}", stats)
        path = (p.path or "/") + ("?" + p.query if p.query else "")
        return rp.can_fetch(self.token, path)


def gunzip(data: bytes, limit: int = 5_000_000) -> bytes:
    if data[:2] == b"\x1f\x8b":
        try:
            return zlib.decompressobj(16 + zlib.MAX_WBITS).decompress(data, limit)
        except zlib.error:
            return b""
    return data


def chunks(resp, budget: list, stats: dict):
    """Decompressed bytes of a response, 64 KB at a time; stops when budget[0] (decompressed bytes this site may
    still read) runs out. Counts network bytes in stats. gzip is found by magic bytes, so a .xml.gz served as
    application/octet-stream and a Content-Encoding: gzip body both work."""
    d = None
    first = True
    t0 = time.monotonic()
    raw_total = 0
    throttle = stats.get("throttle")
    while budget[0] > 0 and time.monotonic() - t0 < FILE_SECONDS and raw_total < MAX_RESPONSE:
        raw = resp.read(65536)
        if not raw:
            break
        raw_total += len(raw)
        stats["bytes"] += len(raw)
        if throttle:
            throttle(len(raw))
        if first:
            first = False
            if raw[:2] == b"\x1f\x8b":
                d = zlib.decompressobj(16 + zlib.MAX_WBITS)
        if d is not None:
            try:
                data = d.decompress(raw, budget[0])
                while d.unconsumed_tail and budget[0] - len(data) > 0:
                    data += d.decompress(d.unconsumed_tail, budget[0] - len(data))
            except zlib.error:
                break
        else:
            data = raw[:budget[0]]
        budget[0] -= len(data)
        if data:
            yield data


# --------------------------------------------------------------------------------------------- parsing
def local(tag) -> str:
    return tag.rsplit("}", 1)[-1].lower() if isinstance(tag, str) else ""


_TITLE_PREF = ("news", "video", "image")


def parse_sitemap(chunk_iter, want: int):
    """Stream a sitemap. Yields ("sitemap", loc, lastmod) for an index entry and ("url", loc, lastmod, title) for a URL
    entry, at most `want` urls. The title is the news, else video, else image title of the entry (what the sitemap
    itself carries); lastmod falls back to a news publication date. Plain-text sitemaps (one address a line) are read
    too. Truncated or broken XML keeps what was parsed before the break."""
    p = ET.XMLPullParser(events=("start", "end"))
    stack: list[str] = []
    cur: dict = {}
    n = 0
    mode = None
    tail = b""
    try:
        for data in chunk_iter:
            if mode is None:
                mode = "xml" if data.lstrip(b"\xef\xbb\xbf \t\r\n")[:1] == b"<" else "text"
            if mode == "text":
                lines = (tail + data).split(b"\n")
                tail = lines.pop()
                for ln in lines:
                    s = ln.decode("utf-8", "replace").strip()
                    if s.startswith("http"):
                        n += 1
                        yield ("url", s, "", "")
                        if n >= want:
                            return
                continue
            broken = False
            try:
                p.feed(data)
            except ET.ParseError:
                broken = True                                 # keep the events parsed before the break
            for ev, el in p.read_events():
                t = local(el.tag)
                if ev == "start":
                    stack.append(t)
                    if t in ("url", "sitemap"):
                        cur = {"titles": {}}
                    continue
                if stack:
                    stack.pop()
                par = stack[-1] if stack else ""
                txt = (el.text or "").strip()
                if t == "loc" and par in ("url", "sitemap"):
                    cur["loc"] = txt
                elif t == "lastmod" and par in ("url", "sitemap"):
                    cur["lm"] = txt
                elif t == "title" and par in _TITLE_PREF:
                    cur["titles"].setdefault(par, " ".join(txt.split()))
                elif t == "publication_date" and par == "news":
                    cur["pub"] = txt
                elif t in ("url", "sitemap"):
                    loc = cur.get("loc", "")
                    lm = cur.get("lm") or cur.get("pub", "")
                    if loc and t == "sitemap":
                        yield ("sitemap", loc, lm)
                    elif loc:
                        title = next((cur["titles"][k] for k in _TITLE_PREF if cur["titles"].get(k)), "")
                        n += 1
                        yield ("url", loc, lm, title)
                        if n >= want:
                            return
                    cur = {"titles": {}}
                    el.clear()
            if broken:
                return
    except ET.ParseError:
        return


class Head(html.parser.HTMLParser):
    """rel=alternate feed links of a home page."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.feeds: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "link":
            a = {k: (v or "") for k, v in attrs}
            if "alternate" in a.get("rel", "").lower().split() and re.search(r"rss|atom", a.get("type", ""), re.I) and a.get("href"):
                self.feeds.append(a["href"])

    def handle_endtag(self, tag):
        if tag == "head":
            raise StopIteration


def home_feeds(body: str, base: str) -> list[str]:
    h = Head()
    try:
        h.feed(body)
    except StopIteration:
        pass
    except Exception:
        pass
    out = []
    for f in h.feeds:
        u = urllib.parse.urljoin(base, f.strip())
        if u.startswith("http") and u not in out:
            out.append(u)
    return out[:MAX_FEEDS]


def parse_feed(data: bytes) -> dict:
    """title and up to MAX_FEED_ITEMS (title, link, date) of an RSS or Atom document."""
    out = {"title": "", "items": []}
    p = ET.XMLPullParser(events=("start", "end"))
    stack: list[str] = []
    cur: dict | None = None
    try:
        p.feed(data)
        for ev, el in p.read_events():
            t = local(el.tag)
            if ev == "start":
                stack.append(t)
                if t in ("item", "entry"):
                    cur = {}
                continue
            if stack:
                stack.pop()
            par = stack[-1] if stack else ""
            txt = (el.text or "").strip()
            if cur is not None and par in ("item", "entry"):
                if t == "title":
                    cur["t"] = " ".join(txt.split())
                elif t == "link":
                    cur.setdefault("l", el.get("href") or txt)
                elif t in ("pubdate", "published", "updated", "date"):
                    cur.setdefault("d", txt)
            elif t == "title" and cur is None and par in ("channel", "feed") and not out["title"]:
                out["title"] = " ".join(txt.split())
            if t in ("item", "entry") and cur is not None:
                if cur.get("l") or cur.get("t"):
                    out["items"].append([cur.get("t", ""), cur.get("l", ""), cur.get("d", "")])
                cur = None
                el.clear()
                if len(out["items"]) >= MAX_FEED_ITEMS:
                    break
    except ET.ParseError:
        pass
    return out


# --------------------------------------------------------------------------------------------- one site
def origin_of(url: str) -> str:
    p = urllib.parse.urlsplit(url)
    return f"{p.scheme}://{p.netloc}"


NET_KINDS = ("dns", "connect", "tls", "refused")


def variants(target: str) -> list[str]:
    """The address as given, then (only when the host did not answer at all) its www. twin and plain http."""
    p = urllib.parse.urlsplit(target)
    out = [target]
    if not p.netloc.startswith("www."):
        out.append(urllib.parse.urlunsplit((p.scheme, "www." + p.netloc, p.path or "/", "", "")))
    if p.scheme == "https":
        out.append(urllib.parse.urlunsplit(("http", p.netloc, p.path or "/", "", "")))
    return out


def harvest_site(net: Net, target: str, **kw) -> dict:
    """harvest_once, retried at the www. twin and at plain http only when the host could not be reached (DNS, connect,
    TLS, refused): never after an HTTP answer, so a 4xx is asked once."""
    t0 = time.monotonic()
    tried, rec = [], {}
    tot_req = tot_bytes = 0
    errs: collections.Counter = collections.Counter()
    for v in variants(target):
        rec = harvest_once(net, v, **kw)
        tried.append(v)
        if net.stopped:
            break
        tot_req += rec["requests"]; tot_bytes += rec["bytes"]; errs.update(rec["errors"])
        if rec["status"] not in NET_KINDS:
            break
    rec.update(requests=tot_req, bytes=tot_bytes, errors=dict(errs), seconds=round(time.monotonic() - t0, 2),
               attempts=tried, site=target)
    return rec


def harvest_once(net: Net, target: str, keep_urls: int = MAX_URLS, max_urls: int = MAX_URLS,
                 max_xml: int = MAX_XML_BYTES, fetch_home: bool = True, max_delay: float = 0.0) -> dict:
    t0 = time.monotonic()
    stats = new_stats()
    stats["throttle"] = net.throttle
    rec: dict = {"site": target, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    origin = origin_of(target)
    base = origin
    rp, robots_sm, rstatus = net.robots_for(origin, stats)
    rec["robots"] = rstatus
    feeds: list[str] = []
    home = {}

    def read_home(r, final):
        data = r.read(MAX_HOME_BYTES)
        stats["bytes"] += len(data)
        ct = (r.headers.get("Content-Type") or "").lower()
        return final, ct, gunzip(data, 4_000_000).decode(r.headers.get_content_charset() or "utf-8", "replace")
    if not fetch_home:
        home = {"status": "skipped"}
    else:
        try:
            final, ct, body = net.fetch(target, stats, "text/html,*/*;q=0.5", read_home)
            home = {"status": "ok", "final": final}
            base = origin_of(final)
            if "html" in ct or "<" in body[:2000]:
                feeds = home_feeds(body, final)
        except Fail as f:
            home = {"status": f.kind}
    rec["home"] = home
    if base != origin:
        rp, robots_sm, rstatus = net.robots_for(base, stats)
        rec["robots_final"] = rstatus

    # sitemaps: robots Sitemap: lines (their own hosts allowed), else /sitemap.xml
    delay = rp.crawl_delay(net.token)
    rec["crawl_delay"] = delay
    queue = collections.deque((u, 0) for u in dict.fromkeys(robots_sm))
    if not queue:
        queue.append((base + "/sitemap.xml", 0))
    if max_delay and delay > max_delay:                  # a site asking for more than we will wait: leave it alone
        queue.clear()
        rec["skipped"] = f"crawl-delay {delay:g}s"
    rec["robots_sitemaps"] = len(robots_sm)
    budget = [max_xml]
    urls: list[list] = []
    seen_maps = set()
    files = []
    n_files = 0
    nurls = 0
    while queue and nurls < max_urls and budget[0] > 0 and n_files < MAX_SITEMAP_FILES:
        u, depth = queue.popleft()
        if u in seen_maps:
            continue
        seen_maps.add(u)
        n_files += 1
        info = {"url": u, "depth": depth}
        children: list[tuple[str, str]] = []

        def read_map(r, final):
            nonlocal nurls
            k = 0
            before = stats["bytes"]
            head: list = []

            def tap(it):
                for c in it:
                    if not head:
                        head.append(c[:400].lower())
                    yield c
            for item in parse_sitemap(tap(chunks(r, budget, stats)), max_urls - nurls):
                if item[0] == "sitemap":
                    cu = urllib.parse.urljoin(final, item[1])
                    if ok_url(cu):
                        children.append((cu, item[2]))
                else:
                    k += 1
                    nurls += 1
                    if len(urls) < keep_urls:
                        urls.append([item[1], item[2], item[3]])
            info["urls"] = k
            info["net_bytes"] = stats["bytes"] - before
            if k == 0 and not children:                  # 200, but nothing a sitemap lists: a soft-404 page or an empty file
                info["status"] = "html" if head and (b"<html" in head[0] or b"<!doctype html" in head[0]) else "empty"
            return k
        try:
            net.fetch(u, stats, "application/xml,text/xml,text/plain,*/*;q=0.5", read_map)
            info.setdefault("status", "ok")
        except Fail as f:
            info["status"] = f.kind
        files.append(info)
        if children and depth < MAX_DEPTH:
            info["children"] = len(children)
            # newest lastmod first (unknown last, original order kept): the cap should keep the fresh pages
            children.sort(key=lambda c: c[1] or "", reverse=True)
            for c in reversed(children[:MAX_SITEMAP_FILES]):
                queue.appendleft((c[0], depth + 1))
    rec["sitemap_files"] = files
    rec["has_sitemap"] = any(f_["status"] == "ok" and (f_.get("urls") or f_.get("children")) for f_ in files)
    rec["stop"] = ("urls" if nurls >= max_urls else "bytes" if budget[0] <= 0 else
                   "files" if queue and n_files >= MAX_SITEMAP_FILES else "done")
    rec["capped"] = rec["stop"] != "done"
    rec["n_urls"] = nurls
    rec["urls"] = urls

    # feeds
    fout = []
    for fu in feeds:
        def read_feed(r, final):
            data = r.read(MAX_FEED_BYTES)
            stats["bytes"] += len(data)
            return parse_feed(gunzip(data, 6_000_000))
        try:
            fd = net.fetch(fu, stats, "application/rss+xml,application/atom+xml,application/xml,*/*;q=0.5", read_feed)
            fout.append({"url": fu, "status": "ok", **fd})
        except Fail as f:
            fout.append({"url": fu, "status": f.kind})
    rec["feeds"] = fout
    rec["requests"] = stats["requests"]
    rec["bytes"] = stats["bytes"]
    rec["errors"] = dict(stats["errors"])
    rec["seconds"] = round(time.monotonic() - t0, 2)
    if home.get("status") == "ok" or rec["has_sitemap"] or (not fetch_home and rstatus in ("ok", "none")):
        rec["status"] = "ok"
    else:
        rec["status"] = home.get("status", "fail") if rstatus in ("ok", "none") else rstatus
    return rec


# --------------------------------------------------------------------------------------------- commands
def read_targets(path: str) -> list[dict]:
    out, seen = [], set()
    for ln in open(path, encoding="utf-8"):
        f = ln.rstrip("\n").split("\t")
        if not f or not f[0].startswith("http"):
            continue
        key = urllib.parse.urlsplit(f[0]).hostname or f[0]
        if key in seen:
            continue
        seen.add(key)
        d = {"target": f[0]}
        if len(f) >= 6:
            d.update(domain=f[1], rank=int(f[2] or 0), band=f[3], wiki=int(f[4]), doc_id=int(f[5]))
        out.append(d)
    return out


def cmd_probe(a):
    done = set()
    try:
        for ln in open(a.out, encoding="utf-8"):
            try:
                done.add(json.loads(ln)["target"])
            except Exception:
                pass
    except FileNotFoundError:
        pass
    todo = [t for t in read_targets(a.targets) if t["target"] not in done]
    if a.limit:
        todo = todo[:a.limit]
    strict = a.cmd == "harvest"
    print(f"{a.cmd}: {len(done)} done, {len(todo)} to do, {a.workers} workers, {a.rate}/s", file=sys.stderr)
    if strict:      # the card-sites harvest: 1 s between requests to a host, generic agent, 1.5 MB/s, no home page, no feeds
        net = Net(a.rate, host_gap=1.0, bw=a.bw, ua=HARVEST_UA, token=HARVEST_TOKEN)
        kw = {"fetch_home": False, "max_delay": 10.0}
    else:
        net, kw = Net(a.rate), {}
    lock = threading.Lock()
    out = open(a.out, "a", encoding="utf-8")
    cnt = [0]

    def one(t):
        if net.stopped:
            return                                   # the run is stopping: leave the site for the resume
        try:
            rec = harvest_site(net, t["target"], keep_urls=a.keep_urls or a.max_urls, max_urls=a.max_urls, **kw)
        except Exception as e:                       # never lose the run to one odd site
            rec = {"site": t["target"], "status": "crash:" + type(e).__name__, "detail": str(e)[:200]}
        if net.stopped:
            return                                   # its requests were refused by the stop, not answered
        rec["target"] = t["target"]
        rec.update({k: v for k, v in t.items() if k != "target"})
        with lock:
            out.write(json.dumps(rec, ensure_ascii=False) + "\n")
            out.flush()
            cnt[0] += 1
            if cnt[0] % 50 == 0:
                print(f"{a.cmd}: {cnt[0]}/{len(todo)} {time.strftime('%H:%M:%S')}", file=sys.stderr)
    with ThreadPoolExecutor(a.workers) as ex:
        list(ex.map(one, todo))
    print(f"{a.cmd}: finished {cnt[0]} sites", file=sys.stderr)
    if net.stopped:
        print(f"{a.cmd}: STOPPED, {net.stopped}; rerun to resume", file=sys.stderr)
        sys.exit(3)


def pct(n, d) -> str:
    return f"{100 * n / d:.1f}%" if d else "n/a"


def quant(xs: list, q: float):
    if not xs:
        return 0
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))]


def summarize(recs: list[dict]) -> dict:
    n = len(recs)
    with_sm = [r for r in recs if r.get("has_sitemap")]
    counts = [r["n_urls"] for r in with_sm]
    urls = [u for r in with_sm for u in r.get("urls", [])]
    wordy = sum(1 for u in urls if len(slug_words(u[0])) >= 2)
    fails = collections.Counter()
    for r in recs:
        for k, v in (r.get("errors") or {}).items():
            fails[k] += v
    site_fail = collections.Counter(r["status"] for r in recs if r.get("status") != "ok")
    feeds_sites = [r for r in recs if any(f.get("status") == "ok" for f in r.get("feeds", []))]
    secs = [r.get("seconds", 0) for r in recs]
    return {
        "sites": n, "sites_with_sitemap": len(with_sm), "urls_total": len(urls),
        "median_urls": statistics.median(counts) if counts else 0, "p90_urls": quant(counts, 0.9),
        "capped_sites": sum(1 for r in with_sm if r.get("capped")),
        "words2": wordy, "lastmod": sum(1 for u in urls if u[1]), "title": sum(1 for u in urls if u[2]),
        "sites_with_title": sum(1 for r in with_sm if any(u[2] for u in r.get("urls", []))),
        "sites_with_feed": len(feeds_sites),
        "feeds_ok": sum(1 for r in recs for f in r.get("feeds", []) if f.get("status") == "ok"),
        "feed_items": sum(len(f.get("items", [])) for r in recs for f in r.get("feeds", [])),
        "bytes": sum(r.get("bytes", 0) for r in recs), "requests": sum(r.get("requests", 0) for r in recs),
        "sec_median": statistics.median(secs) if secs else 0, "sec_p90": quant(secs, 0.9), "sec_mean": sum(secs) / n if n else 0,
        "req_errors": fails, "site_fail": site_fail,
        "robots": collections.Counter(r.get("robots") for r in recs),
    }


def cmd_report(a):
    recs = [json.loads(ln) for ln in open(a.out, encoding="utf-8") if ln.strip()]
    s = summarize(recs)
    n, u = s["sites"], s["urls_total"]
    w = []
    w.append("| measure | value |\n|---|---|")
    rows = [
        ("sites probed", f"{n:,}"),
        ("sites with a sitemap (>=1 URL or a child index)", f"{s['sites_with_sitemap']:,} ({pct(s['sites_with_sitemap'], n)})"),
        ("URLs read (sites with a sitemap)", f"{u:,}"),
        ("URLs per site with a sitemap: median / p90", f"{s['median_urls']:,.0f} / {s['p90_urls']:,}"),
        ("sites that hit the 5,000-URL or 20 MB cap", f"{s['capped_sites']:,} ({pct(s['capped_sites'], s['sites_with_sitemap'])})"),
        ("URLs whose path has >= 2 real words", f"{pct(s['words2'], u)}"),
        ("URLs with lastmod", pct(s["lastmod"], u)),
        ("URLs with a title (news/image/video sitemap)", f"{pct(s['title'], u)} ({s['sites_with_title']} sites)"),
        ("sites with a feed that read", f"{s['sites_with_feed']:,} ({pct(s['sites_with_feed'], n)}); {s['feeds_ok']:,} feeds, {s['feed_items']:,} items"),
        ("requests", f"{s['requests']:,} ({s['requests'] / n:.1f} per site)" if n else "0"),
        ("bytes fetched (network)", f"{s['bytes'] / 1e6:,.0f} MB ({s['bytes'] / n / 1e3:,.0f} KB per site)" if n else "0"),
        ("seconds per site: mean / median / p90", f"{s['sec_mean']:.1f} / {s['sec_median']:.1f} / {s['sec_p90']:.1f}"),
        ("robots.txt", ", ".join(f"{k} {v}" for k, v in s["robots"].most_common())),
        ("request failures by kind", ", ".join(f"{k} {v}" for k, v in s["req_errors"].most_common()) or "none"),
        ("sites with no usable answer, by kind", ", ".join(f"{k} {v}" for k, v in s["site_fail"].most_common()) or "none"),
    ]
    for k, v in rows:
        w.append(f"| {k} | {v} |")
    w.append("\nBy band (Common Crawl harmonic-centrality position) and Wikipedia article:\n")
    w.append("| band | wiki | sites | with sitemap | median URLs | p90 URLs | >=2 words | lastmod | title |\n|---|---|---|---|---|---|---|---|---|")
    groups = collections.defaultdict(list)
    for r in recs:
        if "band" in r:
            groups[(r["band"], r["wiki"])].append(r)
    order = ["1-10k", "10k-100k", "100k-1M", "1M+", "unranked"]
    for key in sorted(groups, key=lambda k: (order.index(k[0]), -k[1])):
        g = summarize(groups[key])
        w.append(f"| {key[0]} | {'yes' if key[1] else 'no'} | {g['sites']} | {pct(g['sites_with_sitemap'], g['sites'])} | "
                 f"{g['median_urls']:,.0f} | {g['p90_urls']:,} | {pct(g['words2'], g['urls_total'])} | "
                 f"{pct(g['lastmod'], g['urls_total'])} | {pct(g['title'], g['urls_total'])} |")
    print("\n".join(w))
    print(extra_block(recs))
    if a.weights:
        print(weighted_block(recs, json.load(open(a.weights))))
    if a.examples:
        rnd = random.Random(573)
        pool = [r for r in recs if r.get("urls")]
        rnd.shuffle(pool)
        print("\n| site | URL | derived words / title |\n|---|---|---|")
        for r in pool[:a.examples]:
            x = rnd.choice(r["urls"])
            what = ("title: " + x[2]) if x[2] else " ".join(slug_words(x[0])) or "(no words)"
            print(f"| {r.get('domain') or r['target']} | {x[0][:110]} | {what[:90]} |")


def wquant(pairs: list, q: float):
    """Weighted quantile of (value, weight) pairs."""
    pairs = sorted(pairs)
    tot = sum(w for _v, w in pairs)
    acc = 0.0
    for v, w in pairs:
        acc += w
        if acc >= q * tot:
            return v
    return pairs[-1][0] if pairs else 0


KEEP = 200       # URLs kept per site in the full harvest (the issue's starting figure)


def weighted_block(recs: list[dict], pop: dict) -> str:
    """Whole-registry estimates: each site stands for population[stratum] / sampled[stratum] sites, strata being
    (band, wiki). pop = {"band|wiki": number of sites in the release}."""
    groups = collections.defaultdict(list)
    for r in recs:
        if "band" in r:
            groups[f"{r['band']}|{r['wiki']}"].append(r)
    N = sum(pop.get(k, 0) for k in groups)
    W = lambda k: pop.get(k, 0) / len(groups[k])                      # weight of one sampled site in stratum k
    def share(f):                                                      # weighted mean of f(site)
        return sum(W(k) * sum(f(r) for r in g) for k, g in groups.items()) / N
    def url_share(f):                                                  # per stratum pooled URL share, weighted by population
        num = 0.0
        for k, g in groups.items():
            us = [u for r in g if r.get("has_sitemap") for u in r.get("urls", [])]
            if us:
                num += pop.get(k, 0) * sum(1 for u in us if f(u)) / len(us)
        return num / N
    sm_sites = [(r["n_urls"], W(k)) for k, g in groups.items() for r in g if r.get("has_sitemap")]
    mean_req = share(lambda r: r.get("requests", 0))
    mean_kept = share(lambda r: min(r.get("n_urls", 0), KEEP) if r.get("has_sitemap") else 0)
    stored = [len(json.dumps(u, ensure_ascii=False)) for r in recs for u in r.get("urls", [])[:KEEP]]
    avg_b = sum(stored) / len(stored) if stored else 0
    out = [f"\nWhole-registry estimate (weighted by stratum size, N = {N:,} sites; each stratum = band x Wikipedia article):\n",
           "| measure | estimate |\n|---|---|"]
    rows = [
        ("sites with a sitemap", f"{100 * share(lambda r: bool(r.get('has_sitemap'))):.1f}%"),
        ("sites with a feed that read", f"{100 * share(lambda r: any(f.get('status') == 'ok' for f in r.get('feeds', []))):.1f}%"),
        ("URLs per site with a sitemap: median / p90 (weighted)", f"{wquant(sm_sites, 0.5):,} / {wquant(sm_sites, 0.9):,}"),
        ("URLs with >= 2 real words in the path", f"{100 * url_share(lambda u: len(slug_words(u[0])) >= 2):.1f}%"),
        ("URLs with lastmod", f"{100 * url_share(lambda u: bool(u[1])):.1f}%"),
        ("URLs with a title", f"{100 * url_share(lambda u: bool(u[2])):.1f}%"),
        ("requests per site (mean)", f"{mean_req:.1f}"),
        ("network bytes per site (mean)", f"{share(lambda r: r.get('bytes', 0)) / 1e3:,.0f} KB"),
        ("seconds per site (mean, 3 workers sharing 10 req/s)", f"{share(lambda r: r.get('seconds', 0)):.1f}"),
        (f"URLs kept per site at {KEEP} a site (mean over all sites)", f"{mean_kept:.1f}"),
        ("average stored size of one kept URL row (loc, lastmod, title as JSON)", f"{avg_b:.0f} bytes"),
        ("full harvest: requests", f"{mean_req * N / 1e6:.1f} M"),
        ("full harvest: days at 10 / 20 requests a second", f"{mean_req * N / 10 / 86400:.1f} / {mean_req * N / 20 / 86400:.1f}"),
        (f"full harvest: URLs kept / raw JSON size at {KEEP} a site", f"{mean_kept * N / 1e6:.0f} M / {mean_kept * N * avg_b / 1e9:.1f} GB"),
    ]
    out += [f"| {k} | {v} |" for k, v in rows]
    return "\n".join(out)


def extra_block(recs: list[dict]) -> str:
    """Checks on the probe itself: guessed targets, why reading stopped, titles by capped / uncapped."""
    out = ["\nProbe checks:\n", "| check | value |\n|---|---|"]
    guessed = [r for r in recs if r.get("domain") and r["target"].rstrip("/").split("://", 1)[-1] == r["domain"]]
    given = [r for r in recs if r not in guessed]
    for name, g in (("target built from the domain (no home URL in the release)", guessed), ("target is the release's home URL", given)):
        if g:
            out.append(f"| {name}: sites / answered / with sitemap | {len(g)} / {pct(sum(1 for r in g if r['status'] == 'ok'), len(g))} / {pct(sum(1 for r in g if r.get('has_sitemap')), len(g))} |")
    out.append(f"| why reading stopped (sites with a sitemap) | {dict(collections.Counter(r.get('stop') for r in recs if r.get('has_sitemap')))} |")
    sm = [r for r in recs if r.get("has_sitemap")]
    for name, g in (("capped", [r for r in sm if r.get("capped")]), ("not capped", [r for r in sm if not r.get("capped")])):
        us = [u for r in g for u in r.get("urls", [])]
        out.append(f"| title share, {name} sites ({len(g)}) | {pct(sum(1 for u in us if u[2]), len(us))} |")
    fs = collections.Counter(f["status"] for r in recs for f in r.get("sitemap_files", []))
    out.append(f"| sitemap files by outcome (html = a 200 page that is not a sitemap, empty = a sitemap with no entry) | {dict(fs)} |")
    retried = [r for r in recs if len(r.get("attempts", [])) > 1]
    out.append(f"| sites that needed the www. / http retry; of them answered | {len(retried)}; {sum(1 for r in retried if r['status'] == 'ok')} |")
    out.append(f"| crashes | {sum(1 for r in recs if str(r.get('status', '')).startswith('crash'))} |")
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("probe"); p.add_argument("targets"); p.add_argument("out")
    p.add_argument("--workers", type=int, default=4); p.add_argument("--rate", type=float, default=10.0)
    p.add_argument("--limit", type=int, default=0); p.add_argument("--keep-urls", type=int, default=0)
    p.add_argument("--max-urls", type=int, default=MAX_URLS)
    p = sub.add_parser("harvest"); p.add_argument("targets"); p.add_argument("out")
    p.add_argument("--workers", type=int, default=16); p.add_argument("--rate", type=float, default=12.0)
    p.add_argument("--bw", type=float, default=1_500_000, help="bytes a second over all threads")
    p.add_argument("--limit", type=int, default=0); p.add_argument("--keep-urls", type=int, default=0)
    p.add_argument("--max-urls", type=int, default=MAX_URLS, help="URLs read per site (the 20 MB cap still applies)")
    p = sub.add_parser("report"); p.add_argument("out"); p.add_argument("--examples", type=int, default=0)
    p.add_argument("--weights", help="JSON {\"band|wiki\": sites in the release} for the whole-registry estimate")
    a = ap.parse_args()
    {"probe": cmd_probe, "harvest": cmd_probe, "report": cmd_report}[a.cmd](a)


if __name__ == "__main__":
    main()
