#!/usr/bin/env python3
"""A state's own government sites, read directly, for the pages Common Crawl does not hold (step 3).

    statecrawl.py crawl --hosts a.gov,b.gov --gov-states GS.json --out DIR [--per-host 4000] [--gap 1] [--archive-gap 3]
    statecrawl.py summary DIR

Common Crawl holds 31 New Jersey Fish and Wildlife addresses and none of the State Police's, so the link crawl
(linkcrawl.py) cannot find "nj fishing license" or "nj gun license" whatever its budget. Owner, 2026-11-01: fetch the
state's sites directly, slowly, inside their robots rules; a site that walls us stays out except through the Wayback
Machine's captures.

The rules: one identifying User-Agent, never a browser's. robots.txt is obeyed, Crawl-delay included. At most one
request per host per --gap seconds, one thread per host. A host answering 429 or 503 five times is left alone. A host
whose first pages are bot-wall answers is never fetched live again: its pages come from the Archive only. Nothing tries
to get past a wall.

Addresses, in order: the front page, the site's sitemaps (robots.txt's, then the usual names; authors, tags, categories
and event listings left out), the Archive's list for a walled host, then the links of fetched pages on the same host.

Out (DIR):
  pages.tsv     page_docs' COPY columns (pages_store.py add-tsv): crawl 'live:<date>' or 'wayback:<date>'. Only real
                pages and noindex rows: a url in several store parts is its latest capture, so a failed fetch written
                here would shadow a good older page.
  errors.tsv    host, url, why
  frontier.tsv  host, url: every address queued, so a rerun resumes with the links it had found.
  links.tsv     from, to, text: every same-host link of every fetched page and its anchor text, for how much of its own
                site points at a page and what the site calls it.
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
import xml.etree.ElementTree as ET
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor

import ccfetch
import linkcrawl

UA = ccfetch.LIVE_UA
UA_TOKEN = "logout-search-import"
MAX_BYTES = ccfetch.LIVE_MAX_BYTES
SITEMAP_NAMES = ("/sitemap_index.xml", "/sitemap.xml", "/wp-sitemap.xml")
SITEMAP_SKIP = re.compile(r"author|users|post_tag|taxonom|category|tribe_|event|venue|organizer", re.I)
PATH_SKIP = re.compile(r"\.(pdf|docx?|xlsx?|pptx?|zip|csv|jpe?g|png|gif|svg|mp[34]|webp|ics|xml|json|js|css)$"
                       r"|/(embed|feed|wp-json|wp-admin|wp-login|tag|author|category|calendar|events?)(/|$)|/page/\d+", re.I)
WALL_LIMIT = 3              # wall answers among a host's first live pages that end its live fetching
BUSY_LIMIT = 5              # 429 / 503 answers that end a host's live fetching
MIN_ARCHIVE_YEAR = "2023"   # an older capture is likelier to give a reader a stale rule than to help them

NEWS_PATH = re.compile(r"/\d{4}/\d{2}/|/news|/press|/release")
MIN_CAPTURE_BYTES = 2000
LIST_MAX_BYTES = 400_000_000   # the page limit cut dep.nj.gov's list at "e", before /njfw/
ARCHIVE_RETRY = (0, 30, 120, 300)


def log(msg: str) -> None:
    print(f"[statecrawl] {time.strftime('%H:%M:%S')} {msg}", flush=True)


def _get(url: str, timeout: float = 25.0, max_bytes: int = MAX_BYTES) -> tuple[int, str, bytes, str, dict]:
    """(status, final url, body, content type, headers); HTTP errors raise."""
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html,application/xml,*/*;q=0.5",
                                               "Accept-Language": "en-US,en;q=0.8"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read(max_bytes + 1)[:max_bytes]
        headers = dict(r.headers.items())
        body = ccfetch.decode_body(r.headers.get("Content-Encoding", ""), body)
        return r.status, r.geturl(), body, r.headers.get("Content-Type", ""), headers


def parse_page(status: int, ctype: str, headers: dict, body: bytes) -> dict:
    """The page through linkcrawl.parse, which reads a gzip WARC record: one is wrapped around the answer, so a page
    read here has exactly the fields, text rules and robots reading of one read from Common Crawl."""
    head = f"HTTP/1.1 {status} OK\r\nContent-Type: {ctype}\r\n"
    robots = headers.get("X-Robots-Tag") or headers.get("x-robots-tag")
    if robots:
        head += f"X-Robots-Tag: {robots}\r\n"
    return linkcrawl.parse(gzip.compress(b"WARC/1.0\r\n\r\n" + head.encode("latin-1", "replace") + b"\r\n" + body, 1))


def domain_of(host: str, domains: dict[str, str]) -> str:
    """The registrable domain page_docs keys on: the longest suffix the state file lists, else the last two labels."""
    labels = host.split(".")
    for i in range(len(labels) - 1):
        if ".".join(labels[i:]) in domains:
            return ".".join(labels[i:])
    return ".".join(labels[-2:])


def sitemap_urls(text: bytes) -> tuple[list[str], list[str]]:
    """(child sitemaps, page addresses) of one sitemap file; ([], []) when it is not XML (a wall's answer)."""
    try:
        root = ET.fromstring(text.strip())       # a WordPress sitemap can open with blank lines
    except ET.ParseError:
        return [], []
    locs = [e.text.strip() for e in root.iter() if e.tag.rsplit("}", 1)[-1] == "loc" and e.text]
    return (locs, []) if root.tag.rsplit("}", 1)[-1] == "sitemapindex" else ([], locs)


def wanted(url: str, host: str) -> bool:
    try:
        s = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    return s.scheme in ("http", "https") and (s.hostname or "").lower() == host and not PATH_SKIP.search(s.path or "/")


def clean(url: str) -> str:
    """No fragment, no query: a state page's query is a sort order or a tracker."""
    s = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((s.scheme, s.netloc.lower(), s.path or "/", "", ""))


class Archive:
    """The Wayback Machine, one request per gap across all hosts (it answered 429 at 1.5 s, 2026-10-27)."""

    def __init__(self, gap: float):
        self.gap, self.lock, self.last = gap, threading.Lock(), 0.0

    def _wait(self) -> None:
        with self.lock:
            due = self.last + self.gap
            self.last = max(time.time(), due)
        time.sleep(max(0.0, due - time.time()))

    def _ask(self, url: str, timeout: float, max_bytes: int = MAX_BYTES):
        """One Archive request, waiting out its outages and limits so a bad hour does not mark pages as missing."""
        last: Exception | None = None
        for pause in ARCHIVE_RETRY:
            time.sleep(pause)
            self._wait()
            try:
                return _get(url, timeout, max_bytes)
            except urllib.error.HTTPError as e:
                if e.code not in (429, 500, 502, 503, 504):
                    raise
                last = e
            except OSError as e:           # URLError and timeouts are both OSError
                last = e
            log(f"the Archive did not answer: {type(last).__name__}: {str(last)[:80]}")
        raise last

    def listing(self, host: str, limit: int) -> dict[str, str]:
        """{address: newest good capture's timestamp} of a host's HTML pages. The list costs one request, and knowing
        the timestamp saves the availability ask per page. Never `collapse` here: it compares neighbouring rows
        whichever page they belong to, and dropped most of dep.nj.gov's addresses. A wall answers 200 too, so a
        capture too small to be a page does not count (dep.nj.gov 2026-11-02: walls 588-1,559 bytes, pages 29,000+)."""
        q = urllib.parse.urlencode([("url", f"{host}/*"), ("filter", "statuscode:200"), ("filter", "mimetype:text/html"),
                                    ("from", MIN_ARCHIVE_YEAR), ("fl", "timestamp,length,original"), ("limit", "2000000")])
        try:
            _, _, body, _, _ = self._ask(f"https://web.archive.org/cdx/search/cdx?{q}", 600.0, LIST_MAX_BYTES)
        except Exception as e:  # noqa: BLE001
            log(f"{host}: the Archive's list failed: {type(e).__name__}: {e}")
            return {}
        out: dict[str, str] = {}
        for line in body.decode("utf-8", "replace").splitlines():
            parts = line.split(" ", 2)
            if len(parts) == 3 and parts[1].isdigit() and int(parts[1]) >= MIN_CAPTURE_BYTES and wanted(parts[2], host):
                u = clean(parts[2])
                if parts[0] > out.get(u, ""):
                    out[u] = parts[0]
        return out

    def page(self, url: str, ts: str) -> dict:
        status, _, body, ctype, headers = self._ask(f"https://web.archive.org/web/{ts}id_/{url}", 45.0)
        if "html" not in ctype:
            raise ValueError(f"not html: {ctype[:40]}")
        page = parse_page(status, ctype, {}, body)
        if "Internet Archive" in (page.get("title") or "") and "Offline" in (page.get("title") or ""):
            raise ValueError("the Archive's own offline page")
        return page


class Host:
    def __init__(self, host: str, a, domains, archive: Archive, sink: "Sink"):
        self.host, self.a, self.domain, self.archive, self.sink = host, a, domain_of(host, domains), archive, sink
        self.robots = urllib.robotparser.RobotFileParser()
        self.gap = a.gap
        self.last = 0.0
        self.walled = False
        self.busy = 0
        self.live_seen = self.live_walls = 0
        self.snaps: dict[str, str] = {}
        self.queue: deque[str] = deque()
        self.queued: set[str] = set()
        self.got = 0
        self.retry: list[str] = []      # pages answered by the wall before the host was known to be walled

    def wait(self) -> None:
        time.sleep(max(0.0, self.last + self.gap - time.time()))
        self.last = time.time()

    def add(self, url: str) -> None:
        if not wanted(url, self.host):
            return
        u = clean(url)
        key = linkcrawl.norm(u)
        if key in self.queued or len(self.queued) >= self.a.per_host * (10 if self.a.shallow_first else 3):
            return
        self.queued.add(key)
        self.queue.append(u)
        self.sink.frontier(self.host, u)

    def read_robots(self) -> list[str]:
        self.wait()
        try:
            _, _, body, _, _ = _get(f"https://{self.host}/robots.txt", 20.0)
            lines = body.decode("utf-8", "replace").splitlines()
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):       # the standard's reading of an unreadable robots.txt: all disallowed
                log(f"{self.host}: robots.txt {e.code}, no live fetching")
                self.walled = True
            lines = []
        except Exception:  # noqa: BLE001
            lines = []
        self.robots.parse(lines)
        delay = self.robots.crawl_delay(UA_TOKEN)
        if delay:
            self.gap = max(self.gap, float(delay))
        return [ln.split(":", 1)[1].strip() for ln in lines if ln.lower().startswith("sitemap:")]

    def read_sitemaps(self, first: list[str]) -> None:
        todo = deque(first or [f"https://{self.host}{n}" for n in SITEMAP_NAMES])
        seen: set[str] = set()
        pages: list[str] = []
        while todo and len(seen) < 60 and len(pages) < self.a.per_host * 3:
            sm = todo.popleft()
            if sm in seen or SITEMAP_SKIP.search(sm.rsplit("/", 1)[-1]):
                continue
            seen.add(sm)
            self.wait()
            try:
                _, _, body, _, _ = _get(sm, 40.0)
            except Exception:  # noqa: BLE001
                continue
            kids, locs = sitemap_urls(body)
            todo.extend(kids)
            pages.extend(locs)
        # "page" sitemaps are the site's standing pages; news posts come after them
        pages.sort(key=lambda u: 1 if NEWS_PATH.search(u) else 0)
        for u in pages:
            self.add(u)
        log(f"{self.host}: {len(pages):,} addresses in {len(seen)} sitemaps")

    def fetch(self, url: str) -> tuple[dict | None, str, str | None, str]:
        """(page, crawl tag, error, the address the page answers for)."""
        key = linkcrawl.norm(url)
        if not self.walled and self.robots.can_fetch(UA_TOKEN, url):
            self.wait()
            try:
                status, final, body, ctype, headers = _get(url)
                if "html" not in ctype and "xml" not in ctype:
                    return None, "", f"not html: {ctype[:40]}", url
                page = parse_page(status, ctype, headers, body)
                self.live_seen += 1
                why = ccfetch.stub_reason(page)
                if why is None:
                    if not wanted(final, self.host):
                        return None, "", f"redirected off the host: {final[:100]}", url
                    return page, "live:" + time.strftime("%Y-%m-%d", time.gmtime()), None, clean(final)
                if why.startswith("wall") or len(body) < 1000:
                    self.live_walls += 1
                    self.retry.append(url)
                    if self.live_walls >= WALL_LIMIT and self.live_walls * 2 >= self.live_seen:
                        self.go_walled()
                    if not self.walled:
                        return None, "", f"live stub: {why}", url
            except urllib.error.HTTPError as e:
                if e.code in (429, 503):
                    self.busy += 1
                    time.sleep(min(60, 5 * self.busy))
                    if self.busy >= BUSY_LIMIT:
                        log(f"{self.host}: {e.code} {self.busy} times, no more live fetching")
                        self.walled = True
                elif e.code == 403:
                    self.live_seen += 1
                    self.live_walls += 1
                    self.retry.append(url)
                    if self.live_walls >= WALL_LIMIT and self.live_walls * 2 >= self.live_seen:
                        self.go_walled()
                    if not self.walled:
                        return None, "", "HTTP 403", url
                else:
                    return None, "", f"HTTP {e.code}", url
            except Exception as e:  # noqa: BLE001
                return None, "", f"{type(e).__name__}: {str(e)[:100]}", url
        ts = self.snaps.get(key)
        if not ts:
            return None, "", "no archive capture", url
        try:
            page = self.archive.page(url, ts)
        except Exception as e:  # noqa: BLE001
            return None, "", f"archive: {type(e).__name__}: {str(e)[:100]}", url
        why = ccfetch.stub_reason(page)
        if why:
            return None, "", f"archive stub: {why}", url
        return page, f"wayback:{ts[:8]}", None, url

    def load_archive(self) -> None:
        listed = self.archive.listing(self.host, self.a.per_host)
        self.snaps = {linkcrawl.norm(u): ts for u, ts in listed.items()}
        # standing pages before old press releases, shortest address first
        for u in sorted(listed, key=lambda u: (1 if NEWS_PATH.search(u) else 0, len(u))):
            self.add(u)
        log(f"{self.host}: {len(listed):,} pages in the Archive")

    def go_walled(self) -> None:
        log(f"{self.host}: walled ({self.live_walls} of {self.live_seen} pages), the Archive only from here")
        self.walled = True
        self.load_archive()
        for u in self.retry:
            self.sink.forget(u)
            self.queue.appendleft(u)
        self.retry = []

    def run(self) -> None:
        try:
            self._run()
        except Exception as e:  # noqa: BLE001  (one host's failure never stops the others)
            log(f"{self.host}: stopped: {type(e).__name__}: {e}")
        log(f"{self.host}: done, {self.got:,} pages")

    def _run(self) -> None:
        maps = self.read_robots()
        self.add(f"https://{self.host}/")
        self.read_sitemaps(maps)
        if self.walled:
            self.load_archive()
        if self.a.shallow_first:                 # a budget spent on hubs: they hold the site's menus, sitemaps list press releases
            self.queue = deque(sorted(self.queue, key=lambda u: (urllib.parse.urlsplit(u).path.rstrip("/").count("/"), len(u))))
        for host, u in self.sink.resume.get(self.host, []):
            self.add(u)
        while self.got < self.a.per_host:
            if not self.queue:
                # a walled site with few addresses never reached WALL_LIMIT: its front page was all there was
                if self.walled or not self.live_walls or self.live_walls * 2 < self.live_seen:
                    break
                self.go_walled()
                if not self.queue:
                    break
            url = self.queue.popleft()
            if self.sink.seen(url):
                continue
            page, tag, err, answer = self.fetch(url)
            if err or page is None:
                self.sink.error(self.host, url, err or "no page")
                continue
            if page["noindex"]:
                self.sink.page(self.domain, tag, answer, page, "robots: noindex")
                continue
            self.sink.page(self.domain, tag, answer, page, None)
            self.got += 1
            if not page["nofollow"]:
                base = linkcrawl._join(answer, page["base"]) or answer if page.get("base") else answer
                for href, words in page["links"]:
                    to = linkcrawl._join(base, href.strip())
                    if to:
                        self.add(to)
                        if (urllib.parse.urlsplit(to).hostname or "").lower() == self.host:
                            self.sink.link(answer, to, words)


class Sink:
    def __init__(self, out: str):
        os.makedirs(out, exist_ok=True)
        self.lock = threading.Lock()
        self.done: set[str] = set()
        self.resume: dict[str, list[tuple[str, str]]] = {}
        for name, col in (("pages.tsv", 2), ("errors.tsv", 1)):
            path = os.path.join(out, name)
            if os.path.exists(path):
                with open(path, encoding="utf-8") as f:
                    for line in f:
                        c = line.rstrip("\n").split("\t")
                        if len(c) > col:
                            self.done.add(linkcrawl.norm(c[col]))
        path = os.path.join(out, "frontier.tsv")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                for line in f:
                    c = line.rstrip("\n").split("\t")
                    if len(c) == 2:
                        self.resume.setdefault(c[0], []).append((c[0], c[1]))
        self.pages_f = open(os.path.join(out, "pages.tsv"), "a", encoding="utf-8")
        self.errors_f = open(os.path.join(out, "errors.tsv"), "a", encoding="utf-8")
        self.frontier_f = open(os.path.join(out, "frontier.tsv"), "a", encoding="utf-8")
        self.links_f = open(os.path.join(out, "links.tsv"), "a", encoding="utf-8")
        self.n = 0

    def seen(self, url: str) -> bool:
        with self.lock:
            return linkcrawl.norm(url) in self.done

    def forget(self, url: str) -> None:
        with self.lock:
            self.done.discard(linkcrawl.norm(url))

    def frontier(self, host: str, url: str) -> None:
        with self.lock:
            self.frontier_f.write(f"{host}\t{linkcrawl._esc(url)}\n")

    def link(self, src: str, to: str, text: str = "") -> None:
        with self.lock:
            self.links_f.write(f"{linkcrawl._esc(src)}\t{linkcrawl._esc(to)}\t{linkcrawl._esc(text)}\n")

    def error(self, host: str, url: str, why: str) -> None:
        with self.lock:
            self.done.add(linkcrawl.norm(url))
            self.errors_f.write(f"{host}\t{linkcrawl._esc(url)}\t{linkcrawl._esc(why)}\n")
            self.errors_f.flush()

    def page(self, domain: str, tag: str, url: str, page: dict, err: str | None) -> None:
        if err:
            row = (domain, tag, url, page.get("status"), None, None, None, None, None, err)
        else:
            row = (domain, tag, url, page.get("status"), page["title"], page["description"], page["h1"], page["lang"],
                   page["text"], None)
        with self.lock:
            key = linkcrawl.norm(url)
            if key in self.done:
                return
            self.done.add(key)
            self.pages_f.write("\t".join(linkcrawl._esc(v) for v in row) + "\n")
            self.pages_f.flush()
            self.frontier_f.flush()
            self.links_f.flush()
            self.n += 1
            if self.n % 500 == 0:
                log(f"{self.n:,} pages")


def crawl(a) -> int:
    with open(a.gov_states, encoding="utf-8") as f:
        domains = json.load(f)["domains"]
    hosts = [h.strip().lower() for h in a.hosts.split(",") if h.strip()]
    sink = Sink(a.out)
    archive = Archive(a.archive_gap)
    with ThreadPoolExecutor(max_workers=max(1, min(a.workers, len(hosts)))) as ex:
        list(ex.map(lambda h: Host(h, a, domains, archive, sink).run(), hosts))
    log(f"done: {sink.n:,} new pages")
    return 0


def summary(out: str) -> int:
    tags, hosts = Counter(), Counter()
    with open(os.path.join(out, "pages.tsv"), encoding="utf-8") as f:
        for line in f:
            c = line.rstrip("\n").split("\t")
            tags[c[1].split(":")[0] + (" noindex" if c[9] != r"\N" else "")] += 1
            hosts[urllib.parse.urlsplit(c[2]).hostname] += 1
    print(dict(tags))
    for h, n in hosts.most_common():
        print(f"{n:7,} {h}")
    why = Counter()
    p = os.path.join(out, "errors.tsv")
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            for line in f:
                why[re.sub(r"\d+", "N", line.rstrip("\n").split("\t")[-1])[:50]] += 1
    print("errors:", why.most_common(12))
    return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("crawl")
    c.add_argument("--hosts", required=True)
    c.add_argument("--gov-states", required=True)
    c.add_argument("--out", required=True)
    c.add_argument("--per-host", type=int, default=4000)
    c.add_argument("--gap", type=float, default=1.0)
    c.add_argument("--archive-gap", type=float, default=3.0)
    c.add_argument("--workers", type=int, default=24)
    c.add_argument("--shallow-first", action="store_true", help="fetch the addresses nearest each site's top first")
    s = sub.add_parser("summary")
    s.add_argument("out")
    a = ap.parse_args(argv)
    return crawl(a) if a.cmd == "crawl" else summary(a.out)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
