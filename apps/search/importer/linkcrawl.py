#!/usr/bin/env python3
"""The pages a site links to, out of Common Crawl's captures (step 3).

    linkcrawl.py crawl --candidates F [F ...] --gov-states GS.json --out DIR [--rounds 4] [--per-site 3000]
                       [--per-state 40000] [--per-site-nostate 1000] [--fill-per-site 500] [--site-by host] [--workers 8] [--rate 8]
                       [--sites a.gov,b.gov]
    linkcrawl.py summary DIR

ccpages' pick by address shape (10 pages an agency, 500 a site) never read a link: dep.nj.gov/njfw/fishing/freshwater/
says nothing about licences in its address, yet New Jersey Fish and Wildlife's front page links to it, and 850 of the
1,138 government queries rc11 missed had no useful page in the first five (Clef 27B, 2026-10-28). This walks the
captures the way a crawler walks a site, without contacting any site: every page is one byte range of a WARC file.

Candidates are `search_import.py ccgov-scan --roots` files (domain, host, agency, score, url, languages, warc file,
offset, length; a front page scores 99), from any number of crawls; an address captured by several keeps its newest
capture. Round 0 fetches every site's front page and each agency's shortest address. Each later round fetches, per
site, the captured addresses the fetched pages link to most (distinct linking pages, then the address score, then the
shortest), until the site's or its state's budget is spent or nothing new is linked. A last round fills each site up
to --fill-per-site by the address score alone, so a site whose links are drawn by JavaScript keeps what ccpages took.

A page that says noindex is written as an error row and never indexed; one that says nofollow is not followed.

Out (DIR):
  pages.tsv      page_docs' COPY columns: domain, crawl, url, http_status, title, description, h1, lang, body, error
                 (body: the page's text without its navigation, header, footer and asides, up to PAGE_TEXT_CHARS; ADR-059
                 still holds: the release ships a page's words sorted, never its text)
  links.tsv.gz   from, to, anchor text, between captured addresses (both normalised: host without www, no query, no
                 trailing slash); the inlinks a page has, for ranking later
  front.tsv      the front pages read (url, crawl, title): seeds only, never a page row (the site row is the front page)
A rerun skips every address pages.tsv and front.tsv hold and rebuilds the link counts from links.tsv.gz: a stopped
crawl resumes where it stopped.
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
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin, urlsplit

import ccfetch

PAGE_TEXT_CHARS = 8000          # rc11 kept 2,000 characters of a page with its menus first; the words ship sorted
MAX_LINKS = 1500                # links read per page
_NAV = frozenset({"nav", "header", "footer", "aside"})
_CRAWL = re.compile(r"(CC-MAIN-\d{4}-\d{2})")


def log(msg: str) -> None:
    print(f"[linkcrawl] {time.strftime('%H:%M:%S')} {msg}", flush=True)


class LinkPage(ccfetch._Page):
    """ccfetch's page, plus its links (href, anchor text), its <base href>, and its text without the navigation."""

    def __init__(self) -> None:
        super().__init__()
        self.links: list[tuple[str, str]] = []
        self.base = ""
        self._a: list | None = None
        self._nav = 0
        self._main: list[str] = []
        self._mn = 0

    def handle_starttag(self, tag, attrs):
        super().handle_starttag(tag, attrs)
        if tag == "a":
            if self._a is not None:
                self._close_a()
            href = next((v for k, v in attrs if k.lower() == "href" and v), None)
            self._a = [href, []] if href else None
        elif tag == "base" and not self.base:
            self.base = next((v for k, v in attrs if k.lower() == "href" and v), "") or ""
        if tag in _NAV:
            self._nav += 1

    def handle_endtag(self, tag):
        super().handle_endtag(tag)
        if tag == "a" and self._a is not None:
            self._close_a()
        if tag in _NAV and self._nav:
            self._nav -= 1

    def _close_a(self):
        href, words = self._a
        self._a = None
        if len(self.links) < MAX_LINKS:
            self.links.append((href, " ".join(" ".join(words).split())[:120]))

    def handle_data(self, data):
        super().handle_data(data)
        if self._in_title or self._skip:
            return
        if self._a is not None:
            self._a[1].append(data)
        if not self._nav and self._mn < PAGE_TEXT_CHARS:
            t = data.strip()
            if t:
                self._main.append(t)
                self._mn += len(t) + 1

    def main_text(self) -> str:
        """The text outside navigation; the whole page's leading text when that leaves a stub (an unclosed <header>)."""
        main = ccfetch._WS.sub(" ", " ".join(self._main))[:PAGE_TEXT_CHARS]
        return main if len(main) >= ccfetch.STUB_MIN_CHARS else self.text()


def parse(raw: bytes) -> dict:
    """gzip WARC record -> ccfetch's fields plus links, base and status. Raises ValueError on a malformed record."""
    rec = gzip.decompress(raw)
    parts = rec.split(b"\r\n\r\n", 2)
    if len(parts) < 3:
        raise ValueError("record has no HTTP body")
    head, body = parts[1], parts[2]
    status = head.split(b"\r\n", 1)[0].split()
    m = re.search(rb"charset=([\w-]+)", head, re.I) or re.search(rb'<meta[^>]+charset=["\']?([\w-]+)', body[:4096], re.I)
    charset = m.group(1).decode("ascii", "replace") if m else "utf-8"
    try:
        text = body.decode(charset, "replace")
    except LookupError:
        text = body.decode("utf-8", "replace")
    p = LinkPage()
    try:
        p.feed(text)
    except Exception:  # noqa: BLE001  (malformed HTML: keep what was parsed)
        pass
    # robots_of keeps what the description rule reads (docs_policy); follow / index come from the raw values, where
    # "none" is noindex and nofollow together
    raw_robots = ",".join(p.robots).lower() + "," + ",".join(
        v.decode("latin-1", "replace") for k, v in (ln.split(b":", 1) for ln in head.split(b"\r\n")[1:] if b":" in ln)
        if k.strip().lower() == b"x-robots-tag").lower()
    words = set(re.findall(r"[a-z-]+", raw_robots))
    return {"title": ccfetch._clean(p.title or p.og_title, 300), "description": ccfetch._clean(p.desc or p.og_desc, 500),
            "h1": ccfetch._clean(p.h1, 300), "lang": p.lang.lower(), "text": p.main_text(),
            "noindex": bool(words & {"noindex", "none"}), "nofollow": bool(words & {"nofollow", "none"}),
            "links": p.links, "base": p.base,
            "status": int(status[1]) if len(status) > 1 and status[1].isdigit() else None}


def norm(url: str) -> str | None:
    """https://www.nj.gov/njfw/fishing/?x=1#top -> nj.gov/njfw/fishing: the key two captures of one page share."""
    try:
        s = urlsplit(url.strip())
    except ValueError:
        return None
    if s.scheme not in ("http", "https") or not s.hostname:
        return None
    path = re.sub(r"/{2,}", "/", s.path or "/")
    if len(path) > 1:
        path = path.rstrip("/")
    return s.hostname.lower().removeprefix("www.") + path


def _join(base: str, href: str) -> str | None:
    """urljoin, or None for an address urllib refuses ("http://[bad/" raises "Invalid IPv6 URL")."""
    try:
        return urljoin(base, href)
    except ValueError:
        return None


def state_of(host: str, domains: dict[str, str]) -> str | None:
    """gov_state.host_state's suffix walk: dmv.ca.gov -> CA, www.dot.state.mn.us -> MN."""
    m = re.search(r"(^|\.)state\.([a-z]{2})\.us$", host)
    if m:
        return m.group(2).upper()
    parts = host.split(".")
    for i in range(len(parts) - 1):
        st = domains.get(".".join(parts[i:]))
        if st:
            return st
    return None


class Cand:
    # key: the address's own key string (shared, never a second copy); inl: the distinct pages linking to it.
    # Kept here rather than in dicts keyed by the link file's strings: a resume reads ~90 million link rows.
    __slots__ = ("domain", "site", "agency", "score", "url", "crawl", "wf", "off", "ln", "root", "key", "inl")

    def __init__(self, domain, site, agency, score, url, crawl, wf, off, ln, key=""):
        self.domain, self.site, self.agency, self.score, self.url = domain, site, agency, score, url
        self.crawl, self.wf, self.off, self.ln = crawl, wf, off, ln
        self.root = score >= 99
        self.key, self.inl = key, 0


def site_key(domain: str, host: str, by: str) -> str:
    """The unit a page budget is kept by. "host" (the default): each host on its own, www folded in, so the agencies
    under one state portal (dmv.ca.gov, sos.ca.gov, courts.ca.gov under ca.gov) are separate sites. "domain": the
    registered domain, as every crawl before 2026-11-03 kept it; it gave wa.gov's 316 hosts 3,000 pages together and
    dmv.ca.gov 3 of its 3,326 captures (census)."""
    if by == "domain" or not host:
        return domain
    return host.lower().removeprefix("www.")


def read_candidates(paths: list[str], sites: set[str] | None, by: str = "host") -> dict[str, Cand]:
    """{key: Cand}, the newest crawl's capture of each address."""
    out: dict[str, Cand] = {}
    wfs: dict[str, str] = {}
    for path in paths:
        n = 0
        with open(path, encoding="utf-8") as f:
            for line in f:
                c = line.rstrip("\n").split("\t")
                if len(c) < 9 or (sites and c[0] not in sites):
                    continue
                key = norm(c[4])
                m = _CRAWL.search(c[6])
                if not key or not m:
                    continue
                old = out.get(key)
                if old is not None and old.crawl >= m.group(1):
                    continue
                wf = wfs.setdefault(c[6], c[6])
                # millions of captures: the site, agency and crawl names are shared strings, not one copy a row
                out[key] = Cand(sys.intern(c[0]), sys.intern(site_key(c[0], c[1], by)), sys.intern(c[2]), int(c[3]), c[4],
                                sys.intern(m.group(1)), wf, int(c[7]), int(c[8]), key)
                n += 1
        log(f"{path}: {n:,} captures kept")
    return out


def _esc(v) -> str:
    if v is None:
        return r"\N"
    return str(v).replace("\\", "\\\\").replace("\t", " ").replace("\n", " ").replace("\r", " ").replace("\x00", "")


class Crawl:
    def __init__(self, a):
        self.a = a
        os.makedirs(a.out, exist_ok=True)
        sites = set(a.sites.split(",")) if a.sites else None
        self.cands = read_candidates(a.candidates, sites, getattr(a, "site_by", "host"))
        with open(a.gov_states, encoding="utf-8") as f:
            doms = json.load(f)["domains"]
        self.state = {}
        self.by_site: dict[str, list[str]] = defaultdict(list)
        dstate: dict[str, str | None] = {}
        for k, c in self.cands.items():
            if c.domain not in dstate:
                dstate[c.domain] = state_of(c.domain, doms)
            if dstate[c.domain] is None:
                c.site = c.domain           # a site with no state (federal) keeps one budget per domain: the hosts of
                                            # nih.gov or noaa.gov are not what a state's resident looks for
            self.by_site[c.site].append(k)
            self.state[c.site] = dstate[c.domain]
        self.done: set[str] = set()
        self.pages_site: dict[str, int] = defaultdict(int)
        self.pages_state: dict[str, int] = defaultdict(int)
        self.linked: dict[str, set[str]] = defaultdict(set)      # site -> linked, not yet fetched keys
        self.lock = threading.Lock()
        self._resume()
        self.pages_f = open(os.path.join(a.out, "pages.tsv"), "a", encoding="utf-8")
        self.front_f = open(os.path.join(a.out, "front.tsv"), "a", encoding="utf-8")
        self.links_f = gzip.open(os.path.join(a.out, "links.tsv.gz"), "at", encoding="utf-8")
        self.throttle = ccfetch._Throttle(a.rate)

    def _resume(self):
        for name in ("pages.tsv", "front.tsv"):
            path = os.path.join(self.a.out, name)
            if os.path.exists(path):
                with open(path, encoding="utf-8") as f:
                    for line in f:
                        c = line.rstrip("\n").split("\t")
                        url = c[2] if name == "pages.tsv" else c[0]
                        k = norm(url)
                        if k:
                            self.done.add(k)
                            if name == "pages.tsv" and len(c) > 9 and c[9] == r"\N" and k in self.cands:
                                self._page(self.cands[k].site)
        path = os.path.join(self.a.out, "links.tsv.gz")
        if os.path.exists(path):
            # a page's links are written together, once (a page is read once), so a repeat is found within its block;
            # a set of every (from, to) pair would hold ~90 million of them
            block, seen = None, set()
            try:
                with gzip.open(path, "rt", encoding="utf-8") as f:
                    for line in f:
                        c = line.split("\t", 2)
                        if len(c) < 2:
                            continue
                        if c[0] != block:
                            block, seen = c[0], set()
                        if c[1] not in seen:
                            seen.add(c[1])
                            self._count(c[1])
            except (EOFError, OSError):          # a crawl stopped mid-write: the counts read so far stand
                pass
        if self.done:
            log(f"resume: {len(self.done):,} addresses read before, {sum(self.pages_site.values()):,} of them pages")

    def _page(self, site: str):
        """One more page read on `site`. It counts toward its state's budget only up to --per-site: a site an earlier
        crawl read past that (ceqanet.lci.ca.gov's 25,032 under the per-domain budget) would otherwise spend the
        state's room that its other sites need."""
        self.pages_site[site] += 1
        st = self.state.get(site)
        if st and self.pages_site[site] <= self.a.per_site:
            self.pages_state[st] += 1

    def _count(self, to: str):
        c = self.cands.get(to)
        if c is None:
            return
        c.inl += 1
        if c.key not in self.done:
            self.linked[c.site].add(c.key)

    def room(self, site: str) -> int:
        st = self.state.get(site)
        left = (self.a.per_site if st else self.a.per_site_nostate) - self.pages_site[site]
        if st:
            left = min(left, self.a.per_state - self.pages_state[st])
        return max(left, 0)

    # ---- one page ------------------------------------------------------------------------------------------------
    def work(self, key: str):
        c = self.cands[key]
        try:
            raw = (ccfetch._get_s3(c.wf, c.off, c.ln, self.throttle) if ccfetch.CC_ACCESS == "s3"
                   else ccfetch._get(f"{ccfetch.CC_DATA}/{c.wf}", c.off, c.ln, self.throttle))
            return key, parse(raw), None
        except (ccfetch.SlowDown, RuntimeError):
            return key, None, "SKIP"
        except Exception as e:  # noqa: BLE001
            return key, None, f"{type(e).__name__}: {e}"[:200]

    def record(self, key: str, page: dict | None, err: str | None):
        c = self.cands[key]
        if err == "SKIP":
            return
        if page is not None and page["noindex"]:
            err = "robots: noindex"
        elif page is not None:
            why = ccfetch.stub_reason(page)
            if why:
                err = f"stub: {why}"
        follow = page is not None and not page["nofollow"]
        links = []
        if follow:
            base = _join(c.url, page["base"]) or c.url if page.get("base") else c.url
            seen = set()
            for href, words in page["links"]:
                to = norm(_join(base, href.strip()) or "")
                if to and to != key and to not in seen and to in self.cands:
                    seen.add(to)
                    links.append((to, words))
        with self.lock:
            self.done.add(key)
            for to, words in links:
                self.links_f.write(f"{key}\t{to}\t{_esc(words)}\n")
                self._count(to)
            self.linked[c.site].discard(key)
            if c.root:
                self.front_f.write(f"{_esc(c.url)}\t{c.crawl}\t{_esc((page or {}).get('title'))}\n")
                return
            if err:
                row = (c.domain, c.crawl, c.url, (page or {}).get("status"), None, None, None, None, None, err)
            else:
                row = (c.domain, c.crawl, c.url, page["status"], page["title"], page["description"], page["h1"],
                       page["lang"], page["text"], None)
                self._page(c.site)
            self.pages_f.write("\t".join(_esc(v) for v in row) + "\n")

    def fetch(self, keys: list[str], label: str):
        keys = [k for k in keys if k not in self.done]
        log(f"{label}: {len(keys):,} captures to read")
        t0, n = time.time(), 0
        with ThreadPoolExecutor(max_workers=self.a.workers) as pool:
            for key, page, err in pool.map(self.work, keys):
                self.record(key, page, err)
                n += 1
                if n % 2000 == 0:
                    self.pages_f.flush(); self.links_f.flush(); self.front_f.flush()
                    log(f"{label}: {n:,}/{len(keys):,}, {n / (time.time() - t0):.1f}/s, "
                        f"{sum(self.pages_site.values()):,} pages so far")
        self.pages_f.flush(); self.links_f.flush(); self.front_f.flush()

    # ---- the rounds ----------------------------------------------------------------------------------------------
    def seeds(self) -> list[str]:
        out = []
        for d, keys in self.by_site.items():
            front, agencies = [], {}
            for k in keys:
                c = self.cands[k]
                if c.root:
                    front.append(k)
                else:
                    best = agencies.get(c.agency)
                    if best is None or len(k) < len(best):
                        agencies[c.agency] = k
            out += front + sorted(agencies.values())
        return out

    def _shared(self, wants: dict[str, int]) -> dict[str, int]:
        """How many of its `wants` each site gets this round. A state's room left is shared evenly among its sites
        (the smallest wants are met first and what they leave goes to the rest), never in name order: with a budget
        per host a state has hundreds of sites, and the first ones alphabetically would otherwise spend it all."""
        got: dict[str, int] = {}
        by_state: dict[str | None, list[str]] = defaultdict(list)
        for d, w in wants.items():
            if w > 0:
                by_state[self.state.get(d)].append(d)
        for st, ds in by_state.items():
            if st is None:
                got.update({d: wants[d] for d in ds})
                continue
            left = max(self.a.per_state - self.pages_state[st], 0)
            ds.sort(key=lambda d: (wants[d], d))
            for i, d in enumerate(ds):
                give = min(wants[d], left // (len(ds) - i))
                got[d] = give
                left -= give
        return got

    def by_links(self) -> list[str]:
        """Per site, the linked captures not yet read, most-linked first, within the site's room and its share of
        the state's."""
        wants = {d: min(self.room(d), self.a.per_round, len(self.linked[d])) for d in self.linked}
        out = []
        for d, n in sorted(self._shared(wants).items()):
            if n > 0:
                out += sorted(self.linked[d], key=lambda k: (-self.cands[k].inl, -self.cands[k].score, len(k), k))[:n]
        return out

    def fill(self) -> list[str]:
        """Per site, up to --fill-per-site pages by the address score alone (ccpages' order), within its share of
        the state's room."""
        ranked = {d: sorted((k for k in keys if k not in self.done and not self.cands[k].root),
                            key=lambda k: (-self.cands[k].score, len(k), k))
                  for d, keys in self.by_site.items()}
        wants = {d: min(self.a.fill_per_site - self.pages_site[d], self.room(d), len(ranked[d])) for d in ranked}
        out = []
        for d, n in sorted(self._shared(wants).items()):
            out += ranked[d][:n]
        return out

    def run(self):
        log(f"{len(self.cands):,} captures on {len(self.by_site):,} sites (by {getattr(self.a, 'site_by', 'host')}); "
            f"per site {self.a.per_site}, per state {self.a.per_state}, {self.a.rounds} link rounds, fill to {self.a.fill_per_site}")
        self.fetch(self.seeds(), "round 0 (front pages and agency fronts)")
        for r in range(1, self.a.rounds + 1):
            keys = self.by_links()
            if not keys:
                log(f"round {r}: nothing new is linked")
                break
            self.fetch(keys, f"round {r} (linked)")
        self.fetch(self.fill(), "fill (address score)")
        log(f"done: {sum(self.pages_site.values()):,} pages on {sum(1 for v in self.pages_site.values() if v):,} sites")


def summary(out: str) -> None:
    rows = errs = 0
    per_state: dict[str, int] = defaultdict(int)
    with open(os.path.join(out, "pages.tsv"), encoding="utf-8") as f:
        for line in f:
            c = line.rstrip("\n").split("\t")
            rows += 1
            if c[9] != r"\N":
                errs += 1
    print(json.dumps({"rows": rows, "pages": rows - errs, "errors": errs}))


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("crawl")
    c.add_argument("--candidates", nargs="+", required=True)
    c.add_argument("--gov-states", required=True)
    c.add_argument("--out", required=True)
    c.add_argument("--rounds", type=int, default=4)
    c.add_argument("--per-site", type=int, default=3000)
    c.add_argument("--per-state", type=int, default=40000)
    c.add_argument("--per-site-nostate", type=int, default=1000, help="a site with no state (federal): at most N pages")
    c.add_argument("--per-round", type=int, default=1000, help="at most N linked pages a site per round")
    c.add_argument("--fill-per-site", type=int, default=500)
    c.add_argument("--workers", type=int, default=8)
    c.add_argument("--rate", type=float, default=8.0, help="requests a second, all workers together")
    c.add_argument("--sites", default="", help="only these domains (comma-separated), for a trial")
    c.add_argument("--site-by", choices=("host", "domain"), default="host",
                   help="the unit of --per-site and --fill-per-site: each host (default), or the registered domain (crawls before 2026-11-03)")
    s = sub.add_parser("summary")
    s.add_argument("out")
    a = ap.parse_args(argv)
    if a.cmd == "crawl":
        Crawl(a).run()
    else:
        summary(a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
