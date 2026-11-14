"""Investor-relations pages of listed companies, from Common Crawl only (2026-09-30, ADR-061).

Stock searches link to the company; its investor-relations page is the better link, and no
open dataset lists it (Wikidata has no property). Each company's homepage capture is re-read
from Common Crawl and its "Investor Relations" link kept, if Common Crawl also captured that
page. No company site is ever contacted.

    irlinks.py lookup  --domains missing.txt --out captures-more.tsv     homepage captures from the CC index
    irlinks.py extract --in captures.tsv --out candidates.jsonl          read homepages (s3), pick the IR link
    irlinks.py verify  --in candidates.jsonl --out ir.tsv                keep IR pages CC captured with 200

captures.tsv = domain, url, warc_filename, offset, length (as appmeta.py). ir.tsv = domain, ir_url,
link text. Index requests: one at a time, >= 1 s apart; HTTP 403/429/503 pauses and three in a
row stop the run (Common Crawl's guidance; the 2026-09-23 home-IP block). Runs on AWS
(infra/build-aws/jobs/ir-links.sh), CC_ACCESS=s3 for the WARC reads.
"""
from __future__ import annotations

import gzip
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser

INDEX = "https://index.commoncrawl.org"
USER_AGENT = "logout-search-import/1 (personal search index; polite, sequential)"
GAP_S = 1.1
_IR_TEXT = re.compile(r"^\s*(investor(s)?(\s*(relations|centre|center|information|overview))?|shareholders?"
                      r"(\s*(information|relations))?|ir)\s*$", re.I)
_IR_TEXT_LOOSE = re.compile(r"\binvestor(s)?\b", re.I)
_IR_HOST = re.compile(r"^(ir|investor|investors|investor-relations|shareholders?)\.", re.I)
_IR_PATH = re.compile(r"/(investor[s]?([-_]relations)?|ir|shareholders?)(/|$|\.html?$|\?)", re.I)


def log(msg: str) -> None:
    print(f"[irlinks] {msg}", flush=True)


class _Links(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self._href = dict(attrs).get("href")
            self._text = []

    def handle_data(self, data):
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._href is not None:
            self.links.append((self._href, " ".join("".join(self._text).split())[:80]))
            self._href = None


def ir_candidate(page_url: str, html: str) -> tuple[str, str] | None:
    """The page's best investor-relations link (absolute URL, link text), or None."""
    p = _Links()
    try:
        p.feed(html)
    except Exception:
        pass
    best, best_score = None, 0
    for href, text in p.links:
        if not href or href.startswith(("#", "mailto:", "javascript:", "tel:")):
            continue
        url = urllib.parse.urljoin(page_url, href.strip())
        if not url.startswith(("http://", "https://")):
            continue
        u = urllib.parse.urlsplit(url)
        score = 0
        if _IR_TEXT.match(text or ""):
            score += 4
        elif _IR_TEXT_LOOSE.search(text or "") and len(text) <= 30:
            score += 2
        if _IR_HOST.match(u.hostname or ""):
            score += 2
        if _IR_PATH.search(u.path or ""):
            score += 1
        if score > best_score:
            best, best_score = (url.split("#")[0], text), score
    return best if best_score >= 3 else None


class Stop(Exception):
    pass


_blocks = 0


def _index_get(url: str) -> list[dict]:
    """One CC index request; [] for no capture or after three tries. 403/429 are refusals: pause,
    and three in a row stop the run. 5xx and timeouts are an overloaded index (504s, 2026-09-30):
    back off 30/60/90 s and retry, never faster."""
    global _blocks
    for attempt in range(3):
        time.sleep(GAP_S)
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                _blocks = 0
                return [json.loads(l) for l in r.read().decode("utf-8", "replace").splitlines() if l.strip()]
        except urllib.error.HTTPError as e:
            if e.code == 404:
                _blocks = 0
                return []
            if e.code in (403, 429):
                _blocks += 1
                log(f"HTTP {e.code} from the CC index — pausing 60 s ({_blocks}/3)")
                if _blocks >= 3:
                    raise Stop(f"three refusals in a row (HTTP {e.code}) — stopping")
                time.sleep(60)
                return []
            if e.code < 500:
                raise
            log(f"HTTP {e.code} from the CC index (overloaded) — waiting {30 * (attempt + 1)} s")
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            log(f"{type(e).__name__} from the CC index — waiting {30 * (attempt + 1)} s")
        time.sleep(30 * (attempt + 1))
    return []


def latest_crawls(n: int = 3) -> list[str]:
    req = urllib.request.Request(f"{INDEX}/collinfo.json", headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as r:
        return [c["id"] for c in json.load(r)][:n]


def _cdx(crawl: str, url: str, prefix: bool = False) -> list[dict]:
    q = urllib.parse.urlencode({"url": url, "output": "json", "limit": "5", "filter": "status:200",
                                **({"matchType": "prefix"} if prefix else {})})
    return _index_get(f"{INDEX}/{crawl}-index?{q}")


def cmd_lookup(domains: str, out: str) -> None:
    crawls = latest_crawls()
    names = [l.strip() for l in open(domains) if l.strip()]
    log(f"lookup: {len(names)} homepages in {crawls}")
    n = 0
    with open(out, "w") as o:
        try:
            for d in names:
                for crawl in crawls:
                    rows = [r for r in _cdx(crawl, d + "/") if (r.get("mime") or "").startswith("text/html")]
                    if rows:
                        r = rows[0]
                        o.write(f"{d}\t{r['url']}\t{r['filename']}\t{r['offset']}\t{r['length']}\n")
                        n += 1
                        break
        except Stop as e:
            log(str(e))
    log(f"lookup: {n} of {len(names)} found")


def _decode(head: bytes, body: bytes) -> str:
    m = re.search(rb"charset=([\w-]+)", head, re.I) or re.search(rb'<meta[^>]+charset=["\']?([\w-]+)', body[:4096], re.I)
    try:
        return body.decode(m.group(1).decode("ascii", "replace") if m else "utf-8", "replace")
    except LookupError:
        return body.decode("utf-8", "replace")


def cmd_extract(src: str, out: str, rate: float = 20.0) -> None:
    import ccfetch                                       # CC_ACCESS=s3
    throttle = ccfetch._Throttle(rate)
    rows = [l.rstrip("\n").split("\t") for l in open(src) if l.count("\t") == 4]
    found = 0
    with open(out, "w") as o:
        for domain, url, wf, off, ln in rows:
            try:
                rec = gzip.decompress(ccfetch._get_s3(wf, int(off), int(ln), throttle))
                parts = rec.split(b"\r\n\r\n", 2)
                html = _decode(parts[1], parts[2]) if len(parts) == 3 else ""
                c = ir_candidate(url, html)
            except Exception as e:
                log(f"{domain}: {type(e).__name__}")
                continue
            if c:
                found += 1
                o.write(json.dumps({"domain": domain, "url": url, "ir": c[0], "text": c[1]}) + "\n")
    log(f"extract: IR link on {found} of {len(rows)} homepages")


def cmd_verify(src: str, out: str) -> None:
    crawls = latest_crawls()
    cands = [json.loads(l) for l in open(src) if l.strip()]
    kept = 0
    with open(out, "w") as o:
        try:
            for c in cands:
                ok = False
                for crawl in crawls[:2]:
                    rows = _cdx(crawl, c["ir"])
                    if rows:
                        ok = True
                        break
                if ok:
                    kept += 1
                    o.write(f"{c['domain']}\t{c['ir']}\t{c['text']}\n")
        except Stop as e:
            log(str(e))
    log(f"verify: {kept} of {len(cands)} IR pages captured by Common Crawl with status 200")


def main(argv: list[str]) -> None:
    import argparse
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("lookup"); a.add_argument("--domains", required=True); a.add_argument("--out", required=True)
    b = sub.add_parser("extract"); b.add_argument("--in", dest="src", required=True); b.add_argument("--out", required=True)
    c = sub.add_parser("verify"); c.add_argument("--in", dest="src", required=True); c.add_argument("--out", required=True)
    x = ap.parse_args(argv)
    {"lookup": lambda: cmd_lookup(x.domains, x.out), "extract": lambda: cmd_extract(x.src, x.out),
     "verify": lambda: cmd_verify(x.src, x.out)}[x.cmd]()


if __name__ == "__main__":
    main(sys.argv[1:])
