#!/usr/bin/env python3
"""The site tree under a card's official site (2026-11-05): the few sections of the site a person most often wants
(tickets, shop, news, support, ...), taken from the site's own home page navigation. Precomputed per release, so
a search never fetches anything and never tells a site what was searched.

    site-nav.py fetch TARGETS OUT.jsonl [--workers 32] [--limit N]   one GET of robots.txt and the home page per site
    site-nav.py pick  OUT.jsonl SITENAV.jsonl [--max 6]              the section links, re-runnable with no refetch
    site-nav.py report SITENAV.jsonl [--sample 50]                   coverage and a sample table (markdown)
    site-nav.py cc GAPS OUT.jsonl [--coll CC-MAIN-2026-43]           the home page from Common Crawl's copy instead,
                                                                     for the sites that refused us or failed
    site-nav.py sitemap GAPS OUT.jsonl [--workers 16]                the sections from the site's own sitemap, for sites
                                                                     whose home page gave none (script-drawn menus)
    site-nav.py search TARGETS OUT.jsonl [--rate 10] [--workers 16]  the site's own search address: the home
                                                                     page's schema.org SearchAction or its OpenSearch
                                                                     link (one more GET for the description file)
    site-nav.py search TARGETS OUT.jsonl [--rate 10] [--workers 16]  the site's own search address: the home
                                                                     page's schema.org SearchAction or its OpenSearch
                                                                     link (one more GET for the description file)

TARGETS: one URL per line (a card's official site), or a TSV whose first http(s) column is one; sites are
deduplicated by host. fetch keeps every link of the home page (href, text, and whether it sat in a nav, header
or menu element), not only the chosen ones, so the choice can change without fetching again. It obeys
robots.txt for "/" (its own user agent, then *), sends one request at a time per site, gives up after 10 s, and
never retries a 4xx.
"""
from __future__ import annotations

import argparse
import collections
import gzip
import html.parser
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
import zlib
from concurrent.futures import ThreadPoolExecutor

UA = "logout-search-sitenav/0.1 (+https://search.logout.cloud; help@logout.cloud)"
TIMEOUT = 10
MAX_BYTES = 1_500_000
MAX_LINKS = 400

# Section words (English first; the most common ones in a few other languages). A link is a section when its text,
# lower-cased and stripped, is one of these or starts with one ("Tickets & Hospitality"). Order is the order shown.
SECTIONS = [
    ("tickets", r"tickets?|buy tickets|book tickets|billets?|entradas|karten"),
    ("shop", r"shop|store|online store|shop online|boutique|tienda|products|our products|shop all|shop all products|shop our products"
             r"|shop merch|merch|merchandise|official store|official shop|webshop|e-?shop|online shop"),
    ("menu", r"menu|our menu|food menu"),
    ("locations", r"locations?|find a store|store locator|stores|find us|restaurants|branches|find a location"),
    ("order", r"order|order online|order now|delivery"),
    ("book", r"book|book now|reservations?|book a table|bookings?"),
    ("fixtures", r"fixtures|schedule|matches|calendar|events"),
    ("news", r"news|newsroom|press|latest news|noticias|actualités|nachrichten|blog"),
    ("pricing", r"pricing|plans|prices|plans and pricing"),
    ("support", r"support|help|help center|customer service|faqs?|ayuda|aide|hilfe"),
    ("account", r"log ?in|sign ?in|my account|account"),
    ("contact", r"contact|contact us|kontakt|contacto"),
    ("careers", r"careers|jobs|work with us|join us"),
    ("about", r"about|about us|who we are|our story|company"),
]
# Most sections take the whole link text, with at most a filler word or two ("Contact us", "Order online",
# "Shop now"): "Help Project Shepherd" is not support. A few take up to two words of their own after the
# section word ("Latest news & insights", "Tickets & Hospitality", "Menus").
_FILL = r"(?:\s+(?:&|and|us|now|online|here|page|centre|center|today))*"
# Not shop (2026-11-06): "Shop All Basses", "Shop Batman", "Shop with Points" name one aisle, not the store; the
# broad wordings ("Shop all", "Shop merch") are listed in SECTIONS instead.
_LOOSE = {"tickets", "news", "locations", "menu", "careers", "fixtures"}
_SECTION_RE = [(k, re.compile(rf"^(?:{p})s?{_FILL}" + (r"(?:\s+\S+){0,2}$" if k in _LOOSE else "$"), re.I))
               for k, p in SECTIONS]
# Whole-text matches first: "Store Locator" is the locations page, not shop ("store" + one more word).
_SECTION_EXACT = [(k, re.compile(rf"^(?:{p})s?{_FILL}$", re.I)) for k, p in SECTIONS]
_NOISE = re.compile(r"\s*\((?:opens?|link)[^)]*\)|\s*[›»>|]+\s*$", re.I)


def label(text: str) -> str:
    """The link text as shown: no "(opens in new window)", no trailing arrows; SHOUTED text in title case."""
    t = _NOISE.sub("", " ".join(text.split())).strip()
    return t.title() if t.isupper() and len(t) > 3 else t
_TRACKING = re.compile(r"^(?:utm_|lfm_|gclid$|fbclid$|mc_|_ga$|ref$|cmp$|cid$)", re.I)
_NAVISH = re.compile(r"nav|menu|header|masthead|topbar", re.I)


class Links(html.parser.HTMLParser):
    """Every <a href> with its text and whether an enclosing element is navigation."""
    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack: list[bool] = []          # per open element: is it navigation?
        self.links: list[list] = []
        self.a: list | None = None
        self.title = ""
        self._in_title = False
        self.opensearch: str | None = None     # <link rel="search" type="application/opensearchdescription+xml" href>
        self.ldjson: list[str] = []            # the text of each <script type="application/ld+json"> (first few, capped)
        self._ld: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "link" and self.opensearch is None and a.get("href") and "search" in (a.get("rel") or "").lower().split() \
                and "opensearchdescription" in (a.get("type") or "").lower():
            self.opensearch = a["href"].strip()
        if tag == "script" and "ld+json" in (a.get("type") or "").lower() and len(self.ldjson) < 8:
            self._ld = []
        nav = tag in ("nav", "header") or a.get("role") == "navigation" or bool(
            _NAVISH.search((a.get("class") or "") + " " + (a.get("id") or "")))
        if tag == "title":
            self._in_title = True
        if tag == "a" and a.get("href") and len(self.links) < MAX_LINKS:
            self.a = [a["href"], "", any(self.stack) or nav]
        if tag not in self.VOID:
            self.stack.append(nav)

    def handle_endtag(self, tag):
        if tag == "script" and self._ld is not None:
            self.ldjson.append("".join(self._ld)[:200_000])
            self._ld = None
        if tag == "title":
            self._in_title = False
        if tag == "a" and self.a is not None:
            self.a[1] = " ".join(self.a[1].split())[:80]
            self.links.append(self.a)
            self.a = None
        if tag not in self.VOID and self.stack:
            self.stack.pop()

    def handle_data(self, data):
        if self._ld is not None:
            self._ld.append(data)
        if self.a is not None:
            self.a[1] += data
        if self._in_title and len(self.title) < 200:
            self.title += data


def host(u: str) -> str:
    h = (urllib.parse.urlsplit(u).hostname or "").lower()
    return h[4:] if h.startswith("www.") else h


def _get(url: str, limit: int = MAX_BYTES):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html,*/*;q=0.5",
                                               "Accept-Encoding": "gzip, deflate", "Accept-Language": "en"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        raw = r.read(limit)
        enc = (r.headers.get("Content-Encoding") or "").lower()
        if enc == "gzip":
            raw = gzip.decompress(raw)
        elif enc == "deflate":
            raw = zlib.decompress(raw, -zlib.MAX_WBITS)
        cs = r.headers.get_content_charset() or "utf-8"
        return r.status, r.geturl(), (r.headers.get("Content-Type") or ""), raw.decode(cs, "replace")


def fetch_one(site: str) -> dict:
    out = {"site": site, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    base = f"https://{site}/"
    try:
        rp = urllib.robotparser.RobotFileParser()
        try:
            _s, _u, _t, txt = _get(base + "robots.txt", 300_000)
            rp.parse(txt.splitlines())
        except urllib.error.HTTPError as e:
            rp.parse([] if e.code >= 400 else ["User-agent: *", "Disallow: /"])
        except Exception:
            rp.parse([])
        if not rp.can_fetch(UA, base):
            return dict(out, status="robots")
        try:
            status, final, ctype, body = _get(base)
        except urllib.error.HTTPError as e:
            return dict(out, status=f"http{e.code}")
        except Exception:
            status, final, ctype, body = _get(f"https://www.{site}/")
        if "html" not in ctype.lower() or "\x00" in body[:2000]:
            return dict(out, status="not_html", final=final)
        p = Links()
        try:
            p.feed(body)
        except Exception:
            pass
        return dict(out, status="ok", final=final, title=" ".join(p.title.split())[:200], links=p.links)
    except Exception as e:
        return dict(out, status=type(e).__name__)


def targets(path: str) -> list[str]:
    seen, out = set(), []
    for line in open(path, encoding="utf-8"):
        url = next((c for c in line.rstrip("\n").split("\t") if c.startswith(("http://", "https://"))), None)
        h = host(url) if url else ""
        if h and h not in seen:
            seen.add(h)
            out.append(h)
    return out


CC_INDEX = "https://index.commoncrawl.org/{coll}-index"
CC_DATA = "https://data.commoncrawl.org/"


def cc_one(site: str, coll: str) -> dict:
    """A site's home page from Common Crawl's copy (for a site that refused us or failed): the index's newest 200
    HTML capture of https://site/ (or www.), then that one WARC record by byte range. Nothing is asked of the site."""
    out = {"site": site, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "src": coll}
    for u in (f"https://{site}/", f"https://www.{site}/", f"http://{site}/", f"http://www.{site}/"):
        q = urllib.parse.urlencode({"url": u, "output": "json", "filter": ["status:200", "mime:text/html"]}, doseq=True)
        lines, err = None, None
        for wait in (0, 10, 30, 60, 120):                 # the index server sheds load with 5xx; it asks for patience
            time.sleep(wait)
            try:
                req = urllib.request.Request(CC_INDEX.format(coll=coll) + "?" + q, headers={"User-Agent": UA})
                with urllib.request.urlopen(req, timeout=60) as r:
                    lines = [json.loads(x) for x in r.read().decode("utf-8", "replace").splitlines() if x.strip()]
                break
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    lines = []                            # no capture of this form of the address
                    break
                err = f"cc-index-http{e.code}"
            except Exception as e:  # noqa: BLE001
                err = "cc-index-" + type(e).__name__
        if lines is None:
            return dict(out, status=err or "cc-index-failed")
        if not lines:
            continue
        c = lines[-1]                                     # the newest capture
        try:
            off, ln = int(c["offset"]), int(c["length"])
            req = urllib.request.Request(CC_DATA + c["filename"], headers={"User-Agent": UA, "Range": f"bytes={off}-{off + ln - 1}"})
            with urllib.request.urlopen(req, timeout=60) as r:
                raw = gzip.decompress(r.read())
        except Exception as e:  # noqa: BLE001
            return dict(out, status="cc-data-" + type(e).__name__)
        parts = raw.split(b"\r\n\r\n", 2)                # WARC headers, HTTP headers, body
        if len(parts) < 3:
            return dict(out, status="cc-bad-record")
        head, body = parts[1].decode("latin-1", "replace"), parts[2]
        if re.search(r"(?im)^content-encoding:\s*gzip", head):
            try:
                body = gzip.decompress(body)
            except OSError:
                pass
        m = re.search(r"(?im)^content-type:.*charset=([\w-]+)", head)
        text = body.decode(m.group(1) if m else "utf-8", "replace")
        p = Links()
        try:
            p.feed(text)
        except Exception:  # noqa: BLE001
            pass
        return dict(out, status="ok", final=c.get("url") or u, captured=c.get("timestamp"),
                    title=" ".join(p.title.split())[:200], links=p.links)
    return dict(out, status="cc-none")


# Common Crawl's own sorted index (ZipNum): cluster.idx says which ~3,000-line block of cdx-NNNNN.gz holds a key; one
# byte range of that file is the block. On AWS (owner rule 2026-11-06: Common Crawl is read from AWS) every read is a
# ranged GET of s3://commoncrawl with the instance role; the public endpoints throttle bulk readers from outside.
class ZipNum:
    def __init__(self, coll: str, cluster_path: str):
        import bisect  # noqa: F401
        self.coll = coll
        self.keys, self.rows = [], []
        for line in open(cluster_path, encoding="utf-8"):
            k, f, off, ln, _ = line.rstrip("\n").split("\t")
            self.keys.append(k)
            self.rows.append((f, int(off), int(ln)))
        import boto3
        self.s3 = boto3.client("s3", region_name="us-east-1")

    def get(self, key: str, start: int, length: int) -> bytes:
        for wait in (0, 2, 5, 15, 30):
            time.sleep(wait)
            try:
                r = self.s3.get_object(Bucket="commoncrawl", Key=key, Range=f"bytes={start}-{start + length - 1}")
                return r["Body"].read()
            except Exception as e:  # noqa: BLE001 - SlowDown and friends: back off, try again
                err = e
        raise err

    def lookup(self, surt: str) -> list[dict]:
        import bisect
        i = max(0, bisect.bisect_right(self.keys, surt + " ") - 1)
        out = []
        while i < len(self.rows):
            f, off, ln = self.rows[i]
            block = zlib.decompress(self.get(f"cc-index/collections/{self.coll}/indexes/{f}", off, ln), 16 + zlib.MAX_WBITS)
            past = False
            for line in block.decode("utf-8", "replace").splitlines():
                k, _, rest = line.partition(" ")
                if k == surt:
                    out.append(json.loads(rest.split(" ", 1)[1]))
                elif k > surt:
                    past = True
                    break
            if past:
                break
            i += 1                                         # the key may run on into the next block
        return out


def surt_home(site: str) -> str:
    parts = site.lower().strip(".").split(".")
    if parts and parts[0] == "www":
        parts = parts[1:]
    return ",".join(reversed(parts)) + ")/"


def cc_zip_one(site: str, zn: "ZipNum") -> dict:
    out = {"site": site, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "src": zn.coll}
    try:
        caps = [c for c in zn.lookup(surt_home(site)) if str(c.get("status")) == "200" and "html" in str(c.get("mime", ""))]
    except Exception as e:  # noqa: BLE001
        return dict(out, status="cc-index-" + type(e).__name__)
    if not caps:
        return dict(out, status="cc-none")
    c = max(caps, key=lambda x: x.get("timestamp", ""))
    try:
        raw = gzip.decompress(zn.get(c["filename"], int(c["offset"]), int(c["length"])))
    except Exception as e:  # noqa: BLE001
        return dict(out, status="cc-data-" + type(e).__name__)
    parts = raw.split(b"\r\n\r\n", 2)
    if len(parts) < 3:
        return dict(out, status="cc-bad-record")
    head, body = parts[1].decode("latin-1", "replace"), parts[2]
    m = re.search(r"(?im)^content-type:.*charset=([\w-]+)", head)
    try:
        text = body.decode(m.group(1) if m else "utf-8", "replace")
    except LookupError:
        text = body.decode("utf-8", "replace")
    p = Links()
    try:
        p.feed(text)
    except Exception:  # noqa: BLE001
        pass
    return dict(out, status="ok", final=c.get("url"), captured=c.get("timestamp"), title=" ".join(p.title.split())[:200], links=p.links)


def cmd_cczip(a):
    """Fill fetch gaps from Common Crawl's sorted index over S3 (run on AWS: infra/build-aws/jobs/cc-sitenav.sh)."""
    zn = ZipNum(a.coll, a.cluster)
    sites = [l.split("\t")[0].strip() for l in open(a.gaps, encoding="utf-8") if l.strip()]
    done = set()
    try:
        done = {json.loads(l)["site"] for l in open(a.out, encoding="utf-8")}
    except FileNotFoundError:
        pass
    todo = [s for s in sites if s not in done]
    print(f"{len(sites)} sites, {len(done)} done, {len(todo)} to look up in {a.coll}", file=sys.stderr, flush=True)
    t0, n, st = time.time(), 0, collections.Counter()
    with ThreadPoolExecutor(a.workers) as ex, open(a.out, "a", encoding="utf-8") as f:
        for r in ex.map(lambda s: cc_zip_one(s, zn), todo):
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
            n += 1
            st[r["status"]] += 1
            if n % 200 == 0:
                f.flush()
                print(f"{n}/{len(todo)} {round(time.time() - t0)} s {dict(st)}", file=sys.stderr, flush=True)
    print(f"done {n} {round(time.time() - t0)} s {dict(st)}", file=sys.stderr, flush=True)


def cmd_cc(a):
    """Fill the gaps of a fetch from Common Crawl: every site in GAPS (one per line, first column) not already in OUT."""
    sites = [l.split("\t")[0].strip() for l in open(a.gaps, encoding="utf-8") if l.strip()]
    done = set()
    try:
        done = {json.loads(l)["site"] for l in open(a.out, encoding="utf-8")}
    except FileNotFoundError:
        pass
    todo = [s for s in sites if s not in done]
    print(f"{len(sites)} sites, {len(done)} done, {len(todo)} to look up in {a.coll}", file=sys.stderr, flush=True)
    t0, n, st = time.time(), 0, collections.Counter()
    with ThreadPoolExecutor(a.workers) as ex, open(a.out, "a", encoding="utf-8") as f:
        for r in ex.map(lambda s: cc_one(s, a.coll), todo):
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
            n += 1
            st["ok" if r["status"] == "ok" else r["status"].split("-")[0] + "-" + r["status"].split("-")[1] if "-" in r["status"] else r["status"]] += 1
            if n % 200 == 0:
                f.flush()
                print(f"{n}/{len(todo)} {round(time.time() - t0)} s {dict(st)}", file=sys.stderr, flush=True)
    print(f"done {n} {round(time.time() - t0)} s {dict(st)}", file=sys.stderr, flush=True)


def cmd_fetch(a):
    sites = targets(a.targets)[: a.limit or None]
    done = set()
    try:
        done = {json.loads(l)["site"] for l in open(a.out, encoding="utf-8")}
    except FileNotFoundError:
        pass
    todo = [s for s in sites if s not in done]
    print(f"{len(sites)} sites, {len(done)} already fetched, {len(todo)} to fetch", file=sys.stderr, flush=True)
    t0, n, st = time.time(), 0, collections.Counter()
    with ThreadPoolExecutor(a.workers) as ex, open(a.out, "a", encoding="utf-8") as f:
        for r in ex.map(fetch_one, todo):
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
            n += 1
            st[r["status"] if r["status"] in ("ok", "robots", "not_html") or r["status"].startswith("http") else "error"] += 1
            if n % 1000 == 0:
                f.flush()
                print(f"{n}/{len(todo)} {round(time.time() - t0)} s {dict(st)}", file=sys.stderr, flush=True)
    print(f"done {n} {round(time.time() - t0)} s {dict(st)}", file=sys.stderr, flush=True)


def section_of(text: str) -> str | None:
    t = _NOISE.sub("", text).strip().lower().strip("›»>|·-–— ")
    if not t or len(t) > 40:
        return None
    return next((k for k, rx in _SECTION_EXACT if rx.match(t)), None) or next((k for k, rx in _SECTION_RE if rx.match(t)), None)


def pick(rec: dict, mx: int) -> list[dict]:
    """Up to mx section links of the site itself: one per section, in SECTIONS order, nav links first."""
    if rec.get("status") != "ok":
        return []
    final = rec.get("final") or f"https://{rec['site']}/"
    home = host(final)
    site_root = ".".join(home.split(".")[-2:])
    best: dict[str, tuple] = {}
    for href, text, nav in rec.get("links") or []:
        k = section_of(text)
        if not k or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        u = urllib.parse.urljoin(final, href)
        sp = urllib.parse.urlsplit(u)
        h = host(u)
        if sp.scheme not in ("http", "https") or not (h == home or h.endswith("." + site_root) or h == site_root):
            continue                                     # the site's own pages (shop.whufc.com counts), never elsewhere
        if (sp.path.rstrip("/") or "/") == (urllib.parse.urlsplit(final).path.rstrip("/") or "/") and not sp.query:
            continue                                     # the home page itself
        if sp.query:                                     # tracking parameters off ("?utm_source=whufc")
            kept = [(k2, v) for k2, v in urllib.parse.parse_qsl(sp.query, keep_blank_values=True) if not _TRACKING.match(k2)]
            u = urllib.parse.urlunsplit(sp._replace(query=urllib.parse.urlencode(kept), fragment=""))
        rank = (0 if nav else 1, len(sp.path))
        if k not in best or rank < best[k][0]:
            best[k] = (rank, {"section": k, "text": label(text)[:40], "url": u})
    order = [k for k, _ in SECTIONS]
    return [best[k][1] for k in order if k in best][:mx]


def cmd_pick(a):
    n = kept = 0
    with open(a.out, "w", encoding="utf-8") as f:
        for line in open(a.fetched, encoding="utf-8"):
            rec = json.loads(line)
            links = pick(rec, a.max)
            n += 1
            kept += bool(links)
            f.write(json.dumps({"site": rec["site"], "status": rec.get("status"), "final": rec.get("final"),
                                "links": links}, ensure_ascii=False) + "\n")
    print(f"{n} sites, {kept} with at least one section link", file=sys.stderr)


_LOC = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.I)
_LANG = re.compile(r"^(?:[a-z]{2}|[a-z]{2}[-_][a-z]{2})$", re.I)
_EXT = re.compile(r"\.(?:html?|php|aspx?|jsp)$", re.I)


def _xml(url: str) -> str:
    _s, _u, _t, body = _get(url, 8_000_000)
    if body[:2] == "\x1f\x8b" or url.endswith(".gz"):          # a .xml.gz sitemap served as bytes
        body = gzip.decompress(body.encode("latin-1", "replace")).decode("utf-8", "replace")
    return body


_SLUG_TEXT = {"faq": "FAQ", "faqs": "FAQ", "signin": "Sign In", "login": "Log In", "myaccount": "My Account",
              "aboutus": "About Us", "contactus": "Contact Us"}


def sitemap_section(text: str) -> str | None:
    """A bare address is weaker evidence than a link's text: only an exact section name ("/tickets", "/contact-us"),
    never a section word with more words after it ("/ticket-giveaways", "/store-non-trovato", "/blog-post-one")."""
    t = text.strip().lower()
    return next((k for k, rx in _SECTION_EXACT if rx.match(t)), None) if t and len(t) <= 40 else None


def slug_label(text: str) -> str:
    return _SLUG_TEXT.get(text.lower().replace(" ", ""), text.title())[:40]


def sitemap_one(site: str) -> dict:
    """The site's sections from its sitemap: a page one level down (or under a language prefix, /en/tickets) whose
    own address names a section ("/tickets", "/contact-us"). Only addresses the sitemap lists; robots.txt obeyed for
    every file read; at most 4 files per site (robots.txt, the sitemap or its index, two of the index's sitemaps)."""
    out = {"site": site, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "links": []}
    base = f"https://{site}/"
    try:
        rp = urllib.robotparser.RobotFileParser()
        try:
            _s, _u, _t, txt = _get(base + "robots.txt", 300_000)
            rp.parse(txt.splitlines())
        except urllib.error.HTTPError as e:
            rp.parse([] if e.code >= 400 else ["User-agent: *", "Disallow: /"])
        maps = list(rp.site_maps() or []) or [base + "sitemap.xml"]
        locs: list[str] = []
        for sm in maps[:2]:
            if not rp.can_fetch(UA, sm):
                continue
            try:
                body = _xml(sm)
            except Exception:
                continue
            found = _LOC.findall(body)
            if "<sitemapindex" in body[:2000].lower():
                kids = sorted(found, key=lambda u: (not re.search(r"page|main|static|site", u, re.I), len(u)))[:2]
                for k in kids:
                    if rp.can_fetch(UA, k):
                        try:
                            locs += _LOC.findall(_xml(k))
                        except Exception:
                            pass
            else:
                locs += found
            if len(locs) > 200_000:
                break
        if not locs:
            return dict(out, status="no_sitemap")
        best: dict[str, tuple] = {}
        for u in locs:
            u = u.replace("&amp;", "&")
            sp = urllib.parse.urlsplit(u)
            if sp.query or host(u) != site or sp.scheme not in ("http", "https"):
                continue
            segs = [x for x in sp.path.split("/") if x]
            lang = 0
            if len(segs) == 2 and _LANG.match(segs[0]):
                lang = 1 if segs[0].lower().startswith("en") else 2      # /en/news before /id/blog
                segs = segs[1:]
            if len(segs) != 1:
                continue
            text = _EXT.sub("", urllib.parse.unquote(segs[0])).replace("-", " ").replace("_", " ").strip()
            k = sitemap_section(text)
            if not k:
                continue
            rank = (lang, len(sp.path), u)
            if k not in best or rank < best[k][0]:
                best[k] = (rank, {"section": k, "text": slug_label(text), "url": u})
        order = [k for k, _ in SECTIONS]
        links = [best[k][1] for k in order if k in best][:6]
        return dict(out, status="ok" if links else "sitemap_none", final=base, links=links, src="sitemap")
    except Exception as e:
        return dict(out, status=type(e).__name__)


def cmd_sitemap(a):
    sites = targets(a.gaps) if a.gaps.endswith(".tsv") else [l.strip() for l in open(a.gaps, encoding="utf-8") if l.strip()]
    sites = [host(s if "://" in s else "https://" + s) for s in sites]
    t0, st = time.time(), collections.Counter()
    with ThreadPoolExecutor(a.workers) as ex, open(a.out, "w", encoding="utf-8") as f:
        for n, r in enumerate(ex.map(sitemap_one, sites), 1):
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
            st[r["status"]] += 1
            if n % 500 == 0:
                f.flush()
                print(f"{n}/{len(sites)} {round(time.time() - t0)} s {dict(st.most_common(6))}", file=sys.stderr, flush=True)
    print(f"done {len(sites)} {round(time.time() - t0)} s {dict(st.most_common(8))}", file=sys.stderr, flush=True)


def cmd_report(a):
    rows = [json.loads(l) for l in open(a.sitenav, encoding="utf-8")]
    st = collections.Counter(r["status"] if r["status"] in ("ok", "robots", "not_html") or str(r["status"]).startswith("http")
                             else "error" for r in rows)
    k = collections.Counter(min(len(r["links"]), 4) for r in rows if r["status"] == "ok")
    sec = collections.Counter(x["section"] for r in rows for x in r["links"])
    print(f"## Site tree coverage\n\n{len(rows)} sites: " + ", ".join(f"{s} {v}" for s, v in st.most_common()))
    print(f"\nof the {st['ok']} fetched: 0 links {k[0]}, 1 {k[1]}, 2 {k[2]}, 3 {k[3]}, 4+ {k[4]}")
    print("\nsections: " + ", ".join(f"{s} {v}" for s, v in sec.most_common()))
    import random
    sample = [r for r in rows if r["links"]]
    random.Random(20261105).shuffle(sample)
    print("\n| site | links |\n|---|---|")
    for r in sample[: a.sample]:
        print(f"| {r['site']} | " + " · ".join(f"[{x['text']}]({x['url']})" for x in r["links"]) + " |")


# ---- the site's own search -------------------------------------------------------------------------------
# A home page can publish the address of its own search for machines: a schema.org SearchAction (urlTemplate with a
# placeholder) or an OpenSearch description (<link rel="search">, one more GET). Kept per site, validated, never
# fetched at search time.
_PRIVATE_SUFFIX = {"github.io", "blogspot.com", "wordpress.com", "herokuapp.com", "vercel.app", "netlify.app",
                   "pages.dev", "web.app", "firebaseapp.com", "appspot.com", "azurewebsites.net", "myshopify.com",
                   "wixsite.com", "weebly.com", "tumblr.com", "gitlab.io", "fly.dev", "onrender.com"}
_SLD = re.compile(r"^(?:com|co|net|org|gov|edu|ac|or|ne|go|gob|nom|mil)$")


def reg_domain(h: str) -> str:
    """The registered domain of a host (www. off): bbc.co.uk, sony.com, foo.github.io. Not the full public-suffix list:
    the country second levels the engine knows (_CC_SLD) and a few hosting suffixes."""
    parts = (h or "").lower().strip(".").split(".")
    if parts and parts[0] == "www":
        parts = parts[1:]
    if len(parts) <= 2:
        return ".".join(parts)
    if ".".join(parts[-2:]) in _PRIVATE_SUFFIX:
        return ".".join(parts[-3:])
    if len(parts[-1]) == 2 and _SLD.match(parts[-2]):
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


# The placeholder names a search template may use for the typed words. A template with any other, or with two, is not used.
TERM_NAMES = {"search_term_string", "searchterms", "search_term", "searchterm", "query", "q", "s", "term", "terms",
              "keyword", "keywords", "text", "search", "searchtext", "searchquery", "search_query", "k", "qry"}
_PLACEHOLDER = re.compile(r"\{([^{}]*)\}|%7B([^%]*?)%7D", re.I)


def validate_template(tmpl: str, site: str, base: str = "", declared: str = "") -> tuple[str | None, str]:
    """-> (template, "") or (None, reason). The template is made absolute against base and kept only when it is https,
    on the site's own registered domain, has no credentials, and has exactly one search-term placeholder of a known name
    (the one the markup declares, else a name in TERM_NAMES). Optional OpenSearch parameters ({count?}) are dropped.
    The placeholder is written back as {searchTerms}."""
    t = (tmpl or "").strip()
    if not t or len(t) > 500 or any(c in t for c in "\r\n\t\x00 "):
        return None, "shape"
    if base:
        t = urllib.parse.urljoin(base, t)
    sp = urllib.parse.urlsplit(t)
    if sp.scheme != "https":
        return None, "not_https"
    if sp.username or sp.password or not sp.hostname:
        return None, "credentials"
    if reg_domain(sp.hostname) != reg_domain(site):
        return None, "other_domain"
    # drop optional parameters: key={name?}  (the whole pair)
    q = [kv for kv in sp.query.split("&") if not re.fullmatch(r"[^=]*=\{[^{}]*\?\}", kv)] if sp.query else []
    t = urllib.parse.urlunsplit(sp._replace(query="&".join(q), fragment=""))
    found = [m.group(1) if m.group(1) is not None else m.group(2) for m in _PLACEHOLDER.finditer(t)]
    if len(found) != 1:
        return None, "placeholders_%d" % len(found)
    if found[0].strip().endswith("?"):
        return None, "optional_term"
    name = found[0].strip().lower()
    if name != declared.strip().lower() and name not in TERM_NAMES:
        return None, "unknown_placeholder"
    rest = _PLACEHOLDER.sub("", t)
    if "{" in rest or "}" in rest:
        return None, "stray_brace"
    t = _PLACEHOLDER.sub("{searchTerms}", t)
    if "{searchTerms}" in urllib.parse.urlsplit(t).netloc:
        return None, "term_in_host"
    return t, ""


def search_actions(ld_texts: list[str]) -> list[tuple[str, str]]:
    """Every SearchAction target in the page's JSON-LD: [(urlTemplate, declared term name)]. Walks the whole
    structure (WebSite.potentialAction, @graph, lists); target may be a string, an EntryPoint, or a list of them."""
    out: list[tuple[str, str]] = []

    def walk(o, depth=0):
        if depth > 12:
            return
        if isinstance(o, list):
            for x in o[:200]:
                walk(x, depth + 1)
        elif isinstance(o, dict):
            ty = o.get("@type")
            tys = ty if isinstance(ty, list) else [ty]
            if any(isinstance(x, str) and x.rsplit("/", 1)[-1] == "SearchAction" for x in tys):
                qi = o.get("query-input")
                qi = " ".join(str(x) for x in qi) if isinstance(qi, list) else qi
                nm = re.search(r"name\s*=\s*([\w-]+)", qi) if isinstance(qi, str) else None
                tg = o.get("target")
                for e in (tg if isinstance(tg, list) else [tg]):
                    u = e.get("urlTemplate") if isinstance(e, dict) else e
                    m = e.get("httpMethod") if isinstance(e, dict) else None
                    if isinstance(u, str) and not (isinstance(m, str) and m.upper() != "GET"):
                        out.append((u, nm.group(1) if nm else ""))
            for v in o.values():
                if isinstance(v, (list, dict)):
                    walk(v, depth + 1)

    for t in ld_texts:
        try:
            walk(json.loads(t))
        except Exception:  # noqa: BLE001 - JSON-LD in the wild is often broken; skip that block
            continue
    return out


def opensearch_template(xml: str) -> tuple[str | None, str]:
    """The Url template of an OpenSearch description: the text/html (or untyped) GET one. -> (template, reason)."""
    import xml.etree.ElementTree as ET
    if "<!DOCTYPE" in xml[:2000].upper() or "<!ENTITY" in xml.upper():
        return None, "xml_dtd"                              # no entity expansion from a stranger's file
    try:
        root = ET.fromstring(xml)
    except Exception:  # noqa: BLE001
        return None, "xml_bad"
    best = None
    for el in root.iter():
        if el.tag.rsplit("}", 1)[-1] != "Url":
            continue
        ty = (el.get("type") or "").lower()
        if (el.get("method") or "get").lower() != "get":
            continue
        if ty and "html" not in ty:
            continue
        if el.get("template") and (best is None or ty):    # prefer the typed text/html one
            best = el.get("template")
            if ty:
                break
    return (best, "") if best else (None, "no_get_html_url")


class Pace:
    """At most `rate` requests a second overall, shared by every worker."""
    def __init__(self, rate: float):
        import threading
        self.gap, self.next, self.lock = 1.0 / rate, 0.0, threading.Lock()

    def wait(self):
        with self.lock:
            now = time.monotonic()
            t = max(now, self.next)
            self.next = t + self.gap
        if t > now:
            time.sleep(t - now)


def search_one(site: str, pace: "Pace") -> dict:
    """One site: robots.txt, the home page, and (only if the page had no usable SearchAction) the OpenSearch
    description file. Every GET passes through pace; robots.txt is obeyed for the home page and the description."""
    out = {"site": site, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    base = f"https://{site}/"

    def get(url, limit=MAX_BYTES):
        pace.wait()
        return _get(url, limit)

    try:
        rp = urllib.robotparser.RobotFileParser()
        try:
            _s, _u, _t, txt = get(base + "robots.txt", 300_000)
            rp.parse(txt.splitlines())
        except urllib.error.HTTPError as e:
            rp.parse([] if e.code >= 400 else ["User-agent: *", "Disallow: /"])
        except Exception:  # noqa: BLE001
            rp.parse([])
        if not rp.can_fetch(UA, base):
            return dict(out, status="robots")
        try:
            _status, final, ctype, body = get(base)
        except urllib.error.HTTPError as e:
            return dict(out, status=f"http{e.code}")
        except Exception:  # noqa: BLE001
            _status, final, ctype, body = get(f"https://www.{site}/")
        if "html" not in ctype.lower() or "\x00" in body[:2000]:
            return dict(out, status="not_html", final=final)
        p = Links()
        try:
            p.feed(body)
        except Exception:  # noqa: BLE001
            pass
        out.update(status="ok", final=final)
        acts = search_actions(p.ldjson)
        out["searchaction"] = [list(x) for x in acts[:3]]
        out["opensearch_link"] = p.opensearch
        reasons = []
        for u, nm in acts:
            t, why = validate_template(u, site, final, nm)
            if t:
                return dict(out, template=t, source="searchaction")
            reasons.append("sa:" + why)
        if p.opensearch:
            d = urllib.parse.urljoin(final, p.opensearch)
            if urllib.parse.urlsplit(d).scheme != "https" or host(d) != host(final):
                reasons.append("os:desc_other_host")
            elif not rp.can_fetch(UA, d):
                reasons.append("os:desc_robots")
            else:
                try:
                    _s2, _u2, _t2, xml = get(d, 200_000)
                    raw, why = opensearch_template(xml)
                    out["opensearch_template"] = raw
                    if raw:
                        t, why = validate_template(raw, site, final)
                        if t:
                            return dict(out, template=t, source="opensearch")
                    reasons.append("os:" + why)
                except urllib.error.HTTPError as e:
                    reasons.append(f"os:http{e.code}")
                except Exception as e:  # noqa: BLE001
                    reasons.append("os:" + type(e).__name__)
        return dict(out, reject=reasons[:4])
    except Exception as e:  # noqa: BLE001
        return dict(out, status=type(e).__name__)


def _search_bucket(r: dict) -> str:
    if r.get("template"):
        return "template:" + r["source"]
    s = r["status"]
    return s if s in ("ok", "robots", "not_html") or s.startswith("http") else "error"


def cmd_search(a):
    sites = targets(a.targets)[: a.limit or None]
    done = set()
    try:
        done = {json.loads(l)["site"] for l in open(a.out, encoding="utf-8")}
    except FileNotFoundError:
        pass
    todo = [s for s in sites if s not in done]
    print(f"{len(sites)} sites, {len(done)} already done, {len(todo)} to fetch at <= {a.rate}/s", file=sys.stderr, flush=True)
    pace, t0, n, st = Pace(a.rate), time.time(), 0, collections.Counter()
    with ThreadPoolExecutor(a.workers) as ex, open(a.out, "a", encoding="utf-8") as f:
        for r in ex.map(lambda s: search_one(s, pace), todo):
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
            n += 1
            st[_search_bucket(r)] += 1
            if n % 500 == 0:
                f.flush()
                print(f"{n}/{len(todo)} {round(time.time() - t0)} s {dict(st)}", file=sys.stderr, flush=True)
    print(f"done {n} {round(time.time() - t0)} s {dict(st)}", file=sys.stderr, flush=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("fetch"); p.add_argument("targets"); p.add_argument("out")
    p.add_argument("--workers", type=int, default=32); p.add_argument("--limit", type=int, default=0)
    p = sub.add_parser("pick"); p.add_argument("fetched"); p.add_argument("out"); p.add_argument("--max", type=int, default=6)
    p = sub.add_parser("cczip"); p.add_argument("gaps"); p.add_argument("out"); p.add_argument("--cluster", required=True)
    p.add_argument("--coll", default="CC-MAIN-2026-43"); p.add_argument("--workers", type=int, default=16)
    p = sub.add_parser("cc"); p.add_argument("gaps"); p.add_argument("out")
    p.add_argument("--coll", default="CC-MAIN-2026-43"); p.add_argument("--workers", type=int, default=3)
    p = sub.add_parser("sitemap"); p.add_argument("gaps"); p.add_argument("out"); p.add_argument("--workers", type=int, default=16)
    p = sub.add_parser("search"); p.add_argument("targets"); p.add_argument("out")
    p.add_argument("--workers", type=int, default=16); p.add_argument("--limit", type=int, default=0)
    p.add_argument("--rate", type=float, default=10.0)
    p = sub.add_parser("report"); p.add_argument("sitenav"); p.add_argument("--sample", type=int, default=50)
    a = ap.parse_args()
    {"fetch": cmd_fetch, "pick": cmd_pick, "report": cmd_report, "cc": cmd_cc, "cczip": cmd_cczip, "sitemap": cmd_sitemap, "search": cmd_search}[a.cmd](a)


if __name__ == "__main__":
    main()
