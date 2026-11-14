"""What kind of site/app a site says it is, in its own markup (2026-09-29).

Three self-descriptions on the homepage we already serve, re-read from its Common Crawl
WARC capture (never from the site):
  - schema.org JSON-LD / microdata: SoftwareApplication, WebApplication,
    MobileApplication, VideoGame nodes (applicationCategory, applicationSubCategory,
    genre, name, description), and the specific @type of the page's top-level entities;
  - <meta property="og:type">, <meta name="application-name">;
  - <link rel="manifest"> — recorded only: Common Crawl rarely captures the manifest
    itself (23 of 345,941 cc_pages rows), so its "categories" are not read.

Web Data Commons publishes no SoftwareApplication/WebApplication/MobileApplication
class subset (checked 2021-12 .. 2024-12), hence the re-read.

    appmeta.py extract --in captures.tsv --out raw.jsonl.gz [--procs 2 --workers 16 --rate 100]
        on AWS (CC_ACCESS=s3): captures.tsv = domain, url, warc_filename, offset, length;
        one JSON line of raw fields per page
    appmeta.py phrases --in raw.jsonl.gz --out appmeta.tsv.gz
        at home: domain, url, phrase, name, description — one row per (site, kind phrase)
    appmeta.py test FILE.html...   raw fields + phrases of local files (no network)
"""

from __future__ import annotations

import gzip
import html
import json
import re
import sys
import time
from html.parser import HTMLParser

_WS = re.compile(r"\s+")
APP_TYPES = {"SoftwareApplication", "WebApplication", "MobileApplication", "VideoGame"}


def log(msg: str) -> None:
    print(f"[appmeta] {msg}", flush=True)


def clean(s, n: int = 300) -> str:
    if not isinstance(s, str):
        return ""
    return _WS.sub(" ", html.unescape(s).replace("\x00", "")).strip()[:n]


def last_seg(t: str) -> str:
    """'https://schema.org/WebApplication' / 'schema:WebApplication' -> 'WebApplication'."""
    t = t.strip().rstrip("/")
    return re.split(r"[/:#]", t)[-1] if re.search(r"[/:#]", t) else t


def _types(node: dict) -> list[str]:
    t = node.get("@type") or node.get("type") or []
    if isinstance(t, str):
        t = [t]
    return [last_seg(x) for x in t if isinstance(x, str) and x.strip()]


def _vals(v) -> list[str]:
    """A property value (string, list, {"@id"/"name": …}) -> its strings."""
    if isinstance(v, str):
        return [v]
    if isinstance(v, list):
        return [s for x in v for s in _vals(x)]
    if isinstance(v, dict):
        for k in ("name", "@id", "@value"):
            if isinstance(v.get(k), str):
                return [v[k]]
    return []


class _Page(HTMLParser):
    """JSON-LD blocks, microdata item types and app props, og:type, application-name,
    og:site_name, description, title, manifest href."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.ld: list[str] = []
        self._in_ld = self._in_title = False
        self._buf: list[str] = []
        self.md_top: list[str] = []       # itemscope without itemprop: a top-level item
        self.md_all: list[str] = []
        self.md_props: dict[str, list[str]] = {}
        self._md_want = ""                # itemprop whose text comes next
        self._buf_md: list[str] = []
        self.meta: dict[str, str] = {}
        self.title = ""
        self.manifest = ""

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "script" and "ld+json" in a.get("type", "").lower():
            self._in_ld, self._buf = True, []
        elif tag == "title" and not self.title:
            self._in_title = True
        elif tag == "meta":
            key = (a.get("property") or a.get("name") or "").lower().strip()
            if key in ("og:type", "application-name", "og:site_name", "description", "og:description",
                       "apple-mobile-web-app-title") and key not in self.meta:
                self.meta[key] = a.get("content", "")
        elif tag == "link" and "manifest" in a.get("rel", "").lower().split() and not self.manifest:
            self.manifest = a.get("href", "")[:300]
        if "itemscope" in a and a.get("itemtype"):
            ts = [last_seg(t) for t in a["itemtype"].split() if "schema.org" in t]
            self.md_all += ts
            if "itemprop" not in a:
                self.md_top += ts
        prop = a.get("itemprop", "")
        if prop in ("applicationCategory", "applicationSubCategory", "genre"):
            v = a.get("content") or a.get("href") or ""
            if v:
                self.md_props.setdefault(prop, []).append(v)
            elif tag != "meta":
                self._md_want, self._buf_md = prop, []

    def handle_endtag(self, tag):
        if tag == "script" and self._in_ld:
            self._in_ld = False
            self.ld.append("".join(self._buf))
        elif tag == "title":
            self._in_title = False
        if self._md_want and tag not in ("br", "img"):
            txt = clean("".join(self._buf_md), 300)
            if txt:
                self.md_props.setdefault(self._md_want, []).append(txt)
            self._md_want = ""

    def handle_data(self, data):
        if self._in_ld:
            self._buf.append(data)
        elif self._in_title:
            self.title += data
        elif self._md_want:
            self._buf_md.append(data)


def _load_ld(text: str):
    t = text.strip()
    t = re.sub(r"^\s*(<!--|//\s*<!\[CDATA\[|<!\[CDATA\[)", "", t)
    t = re.sub(r"(-->|//\s*\]\]>|\]\]>)\s*$", "", t).strip()
    try:
        return json.loads(t, strict=False)
    except ValueError:
        # Several objects back to back, or a trailing semicolon: common hand-written slips.
        t2 = t.rstrip(";")
        try:
            return json.loads("[" + re.sub(r"}\s*{", "},{", t2) + "]", strict=False)
        except ValueError:
            return None


def _walk(node, top: bool, out_top: list, out_apps: list, depth: int = 0) -> None:
    if depth > 12:
        return
    if isinstance(node, list):
        for x in node:
            _walk(x, top, out_top, out_apps, depth + 1)
        return
    if not isinstance(node, dict):
        return
    ts = _types(node)
    if top:
        out_top.extend(ts)
    if APP_TYPES.intersection(ts):
        out_apps.append({
            "types": ts,
            "category": [clean(v, 120) for v in _vals(node.get("applicationCategory"))],
            "subcategory": [clean(v, 120) for v in _vals(node.get("applicationSubCategory"))],
            "genre": [clean(v, 120) for v in _vals(node.get("genre"))],
            "name": clean(" ".join(_vals(node.get("name"))[:1]), 200),
            "description": clean(" ".join(_vals(node.get("description"))[:1]), 300),
        })
    elif top and ("WebSite" in ts) and node.get("genre"):
        out_apps.append({"types": ts, "category": [], "subcategory": [],
                         "genre": [clean(v, 120) for v in _vals(node.get("genre"))],
                         "name": clean(" ".join(_vals(node.get("name"))[:1]), 200),
                         "description": clean(" ".join(_vals(node.get("description"))[:1]), 300)})
    for k, v in node.items():
        if k == "@graph":
            _walk(v, top, out_top, out_apps, depth + 1)       # @graph members are top level
        elif isinstance(v, (dict, list)) and k != "@context":
            _walk(v, False, out_top, out_apps, depth + 1)


def extract_html(text: str) -> dict:
    """HTML -> raw self-description fields (JSON-serialisable)."""
    p = _Page()
    try:
        p.feed(text)
    except Exception:  # malformed HTML: keep whatever was parsed before the error
        pass
    top: list[str] = []
    apps: list[dict] = []
    ld_bad = 0
    for block in p.ld:
        data = _load_ld(block)
        if data is None:
            ld_bad += 1
            continue
        _walk(data, True, top, apps)
    md_app = APP_TYPES.intersection(p.md_all)
    return {
        "ld_top": sorted(set(top)), "ld_apps": apps, "ld_blocks": len(p.ld), "ld_bad": ld_bad,
        "md_top": sorted(set(p.md_top)),
        "md_app": {k: [clean(v, 120) for v in vs][:5] for k, vs in p.md_props.items()} if md_app else {},
        "md_app_types": sorted(md_app),
        "og_type": clean(p.meta.get("og:type", ""), 60),
        "app_name": clean(p.meta.get("application-name") or p.meta.get("apple-mobile-web-app-title") or "", 200),
        "site_name": clean(p.meta.get("og:site_name", ""), 200),
        "title": clean(p.title, 200),
        "description": clean(p.meta.get("description") or p.meta.get("og:description") or "", 300),
        "manifest": p.manifest,
    }


def _decode(http_head: bytes, body: bytes) -> str:
    m = re.search(rb"charset=([\w-]+)", http_head, re.I) or re.search(rb'<meta[^>]+charset=["\']?([\w-]+)', body[:4096], re.I)
    cs = m.group(1).decode("ascii", "replace") if m else "utf-8"
    try:
        return body.decode(cs, "replace")
    except LookupError:
        return body.decode("utf-8", "replace")


def extract_record(raw: bytes) -> dict:
    rec = gzip.decompress(raw)
    parts = rec.split(b"\r\n\r\n", 2)
    if len(parts) < 3:
        raise ValueError("record has no HTTP body")
    return extract_html(_decode(parts[1], parts[2]))


# ---- phrases ---------------------------------------------------------------------------

# Types that say nothing about what kind of site this is (structure, parts, the generic
# organisation/page/site types), never made phrases.
GENERIC = {
    "Thing", "WebSite", "WebPage", "Organization", "Corporation", "Person", "Brand", "Place",
    "CollectionPage", "AboutPage", "ItemPage", "FAQPage", "ProfilePage", "SearchResultsPage", "ContactPage",
    "CheckoutPage", "QAPage", "MedicalWebPage", "RealEstateListing", "WebPageElement", "WPHeader", "WPFooter",
    "WPSideBar", "WPAdBlock", "SiteNavigationElement", "Table",
    "BreadcrumbList", "ListItem", "ItemList", "HowToStep", "HowToSection", "OfferCatalog",
    "ImageObject", "VideoObject", "AudioObject", "MediaObject", "ImageGallery", "Photograph",
    "SearchAction", "EntryPoint", "ReadAction", "WatchAction", "ViewAction", "Action", "PropertyValueSpecification",
    "PostalAddress", "ContactPoint", "GeoCoordinates", "GeoShape", "Country", "State", "City",
    "AdministrativeArea", "Language", "OpeningHoursSpecification", "PropertyValue", "MonetaryAmount",
    "QuantitativeValue", "PriceSpecification", "UnitPriceSpecification", "DefinedTerm", "DefinedTermSet",
    "Offer", "AggregateOffer", "AggregateRating", "Rating", "Review", "Question", "Answer", "Comment",
    "Article", "NewsArticle", "BlogPosting", "SocialMediaPosting", "Report", "TechArticle", "ScholarlyArticle",
    "CreativeWork", "CreativeWorkSeries", "Intangible", "StructuredValue", "Service", "Product", "ProductGroup",
    "Event", "HowTo", "Dataset", "SpeakableSpecification", "Duration", "Schedule", "Audience", "Role",
    "OrganizationRole", "EmployeeRole", "Occupation", "JobPosting", "Invoice", "Order", "Demand",
    "Collection", "Episode", "Clip", "Trip", "LocationFeatureSpecification", "NutritionInformation",
    "Recipe", "Course", "Book", "Movie", "MusicRecording", "MusicAlbum", "MusicGroup", "TVSeries", "PodcastEpisode",
    "SoftwareSourceCode", "DataCatalog", "DataDownload", "Menu", "MenuItem", "MenuSection", "EducationalOccupationalCredential",
    "ProgramMembership", "MemberProgram", "MemberProgramTier", "ShippingDeliveryTime", "OfferShippingDetails",
    "MerchantReturnPolicy", "DeliveryTimeSettings", "ShippingRateSettings", "DefinedRegion", "Distance", "Mass",
    "Energy", "Enumeration", "Periodical", "PublicationIssue", "PublicationVolume", "Claim", "ClaimReview",
    "Game", "WebContent", "Guide", "Specialty", "Thesis", "Map", "Chapter", "CorrectionComment",
    "InteractionCounter", "Seat", "Ticket", "Reservation", "Rating", "EndorsementRating", "EmployerAggregateRating",
}
# The special cases the markup's own names read badly for.
OVERRIDE = {
    "GameApplication": "game", "VideoGame": "video game", "NGO": "non-profit",
    "NewsMediaOrganization": "news site", "OnlineStore": "online store", "OnlineBusiness": "online business",
    "WebApplication": "web application", "MobileApplication": "mobile app",
    "SoftwareApplication": "software application", "SportsOrganization": "sports organization",
    "Blog": "blog", "LocalBusiness": "local business", "EducationalOrganization": "educational organization",
    "CollegeOrUniversity": "college or university", "GovernmentOrganization": "government organization",
    "MedicalOrganization": "medical organization", "FinancialService": "financial service",
    "ProfessionalService": "professional service", "Store": "store",
}
GENERIC_OG = {"website", "article", "object", "webpage", "web page", "page", "homepage", "home", "site",
              "og:website", "blog.article", "website:website", "web", "html", "text/html", "summary", "none",
              "null", "undefined", "profile", "product", "product.group", "video.other"}
# The same generic types however they are spelled ("Website", "WEBSITE", og:type "company").
GENERIC_PHRASES = {"website", "web site", "webpage", "web page", "page", "home page", "homepage", "home",
                   "company", "corporation", "organization", "organisation", "thing", "site", "person", "brand",
                   "item page", "collection page", "about page", "contact page", "article", "object"}
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_BAD_PHRASE = re.compile(r"https?:|www\.|[<>{}=@|]|^\W*$|^[\d\W]+$")


def readable_type(t: str) -> str:
    t = last_seg(t)
    if t in OVERRIDE:
        return OVERRIDE[t]
    return _CAMEL.sub(" ", t).lower().strip()


def readable_value(v: str) -> list[str]:
    """applicationCategory/genre value -> phrases. 'BusinessApplication' or
    'https://schema.org/BusinessApplication' -> 'business application'; free text
    'Finance, Productivity' -> 'finance', 'productivity'."""
    v = clean(v, 200)
    if not v:
        return []
    if re.fullmatch(r"(https?://)?(www\.)?schema\.org/\w+/?", v, re.I) or re.fullmatch(r"[A-Z][a-z]+(?:[A-Z][a-z]+)+", v):
        return [readable_type(v)]
    out = []
    for part in re.split(r"\s*[,;|/]\s*|\s+>\s+", v):
        part = part.strip(" .-_\"'").lower()
        part = part.replace("_", " ")
        if 2 <= len(part) <= 60 and not _BAD_PHRASE.search(part) and len(part.split()) <= 6:
            out.append(part)
    return out


def readable_og(v: str) -> str:
    v = v.strip().lower()
    if not v or v in GENERIC_OG or _BAD_PHRASE.search(v) or len(v) > 40:
        return ""
    if "." in v:                                   # business.business -> business; video.movie -> movie
        a, b = v.split(".", 1)
        v = b if b and b != "other" else a
        if v in GENERIC_OG:
            return ""
    return v.replace("_", " ").replace(":", " ").strip()


def phrases_of(raw: dict) -> list[tuple[str, str, str]]:
    """Raw fields -> [(phrase, name, description)], one per distinct phrase."""
    site_name = raw.get("app_name") or raw.get("site_name") or raw.get("title") or ""
    site_desc = raw.get("description") or ""
    out: dict[str, tuple[str, str]] = {}

    def add(phrase: str, name: str = "", desc: str = "") -> None:
        phrase = phrase.strip()
        if phrase and phrase not in out and phrase not in GENERIC_PHRASES:
            out[phrase] = (clean(name or site_name, 200), clean(desc or site_desc, 300))

    for app in raw.get("ld_apps", []):
        n, d = app.get("name", ""), app.get("description", "")
        for key in ("category", "subcategory", "genre"):
            for v in app.get(key, []):
                for ph in readable_value(v):
                    add(ph, n, d)
        for t in app.get("types", []):
            if t not in GENERIC:
                add(readable_type(t), n, d)
    md = raw.get("md_app") or {}
    mn = mdsc = ""                                 # microdata names are anyone's: the site's own is used
    for key in ("applicationCategory", "applicationSubCategory", "genre"):
        for v in md.get(key, []):
            for ph in readable_value(v):
                add(ph, mn, mdsc)
    for t in raw.get("md_app_types", []):
        add(readable_type(t), mn, mdsc)
    for t in list(raw.get("ld_top", [])) + list(raw.get("md_top", [])):
        if t not in GENERIC and re.fullmatch(r"[A-Z][A-Za-z]+", t):
            add(readable_type(t))
    og = readable_og(raw.get("og_type", ""))
    if og:
        add(og)
    return [(p, n, d) for p, (n, d) in out.items()]


def norm_domain(d: str) -> str:
    d = d.strip().lower()
    return d[4:] if d.startswith("www.") else d


def cmd_phrases(src: str, dst: str) -> None:
    import collections
    rows = sites = with_rows = 0
    top = collections.Counter()
    with gzip.open(src, "rt", encoding="utf-8") as f, gzip.open(dst, "wt", encoding="utf-8") as o:
        for line in f:
            r = json.loads(line)
            if r.get("error"):
                continue
            sites += 1
            ph = phrases_of(r)
            if ph:
                with_rows += 1
            for p, n, d in ph:
                o.write("\t".join(clean(x, 300).replace("\t", " ") for x in (norm_domain(r["domain"]), r["url"], p, n, d))
                        + "\n")
                rows += 1
                top[p] += 1
    log(f"{rows:,} rows for {with_rows:,} of {sites:,} parsed homepages")
    for p, c in top.most_common(40):
        log(f"  {c:7,}  {p}")


# ---- fetch -----------------------------------------------------------------------------

def _read_captures(path: str) -> list[tuple]:
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            p = line.rstrip("\n").split("\t")
            if len(p) == 5 and p[3].isdigit() and p[4].isdigit():
                rows.append((p[0], p[1], p[2], int(p[3]), int(p[4])))
    return rows


def _extract_part(rows, workers: int, rate: float, out: str, part: int):
    from concurrent.futures import ThreadPoolExecutor

    import ccfetch                                    # CC_ACCESS=s3 must be set by now
    throttle = ccfetch._Throttle(rate)

    def work(r):
        domain, url, wf, off, ln = r
        try:
            raw = ccfetch._get_s3(wf, off, ln, throttle)
            return r, extract_record(raw), None
        except (ccfetch.SlowDown, RuntimeError):
            return r, None, "SKIP"
        except Exception as e:
            return r, None, f"{type(e).__name__}: {e}"[:200]

    done = failed = skipped = n_app = 0
    t0 = time.time()
    with gzip.open(out, "wt", encoding="utf-8") as o, ThreadPoolExecutor(max_workers=workers) as pool:
        for r, page, err in pool.map(work, rows):
            if err == "SKIP":
                skipped += 1
                continue
            rec = {"domain": r[0], "url": r[1]}
            if err:
                failed += 1
                rec["error"] = err
            else:
                done += 1
                rec.update(page)
                if page["ld_apps"] or page["md_app_types"]:
                    n_app += 1
                if done <= 6 or r[0] in ("bitwarden.com", "1password.com", "notion.so", "duolingo.com"):
                    log(f"[part {part}] {r[0]}: {phrases_of(rec)[:8]}")
            o.write(json.dumps(rec, ensure_ascii=False) + "\n")
            k = done + failed + skipped
            if k % 5000 == 0:
                log(f"[part {part}] {k:,}/{len(rows):,} ({failed:,} failed, {skipped:,} throttled-skipped, "
                    f"{n_app:,} with an app node), {k / (time.time() - t0):.1f}/s")
    log(f"[part {part}] done: {done:,} parsed, {failed:,} failed, {skipped:,} throttled-skipped, {n_app:,} with an app node")
    return done, failed, skipped, n_app


def cmd_extract(src: str, dst: str, procs: int, workers: int, rate: float) -> None:
    import multiprocessing
    import os
    rows = _read_captures(src)
    log(f"{len(rows):,} captures, {procs} x {workers} workers, at most {rate}/s from s3://commoncrawl")
    parts = [rows[i::procs] for i in range(procs)]
    outs = [f"{dst}.part{i}" for i in range(procs)]
    t0 = time.time()
    with multiprocessing.get_context("fork").Pool(procs) as pool:
        res = pool.starmap(_extract_part, [(p, workers, rate / procs, o, i + 1) for i, (p, o) in enumerate(zip(parts, outs))])
    with open(dst, "wb") as o:                        # gzip members concatenate into one valid file
        for part in outs:
            with open(part, "rb") as f:
                o.write(f.read())
            os.remove(part)
    done, failed, skipped, n_app = (sum(x) for x in zip(*res))
    log(f"extract: {done:,} parsed, {failed:,} failed, {skipped:,} throttled-skipped of {len(rows):,}; "
        f"{n_app:,} with an app node; {(time.time() - t0) / 60:.1f} min")


def main(argv: list[str]) -> None:
    import argparse
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("extract")
    e.add_argument("--in", dest="src", required=True); e.add_argument("--out", required=True)
    e.add_argument("--procs", type=int, default=2); e.add_argument("--workers", type=int, default=16)
    e.add_argument("--rate", type=float, default=100)
    p = sub.add_parser("phrases")
    p.add_argument("--in", dest="src", required=True); p.add_argument("--out", required=True)
    t = sub.add_parser("test")
    t.add_argument("files", nargs="+")
    a = ap.parse_args(argv)
    if a.cmd == "extract":
        cmd_extract(a.src, a.out, a.procs, a.workers, a.rate)
    elif a.cmd == "phrases":
        cmd_phrases(a.src, a.out)
    else:
        for fn in a.files:
            raw = extract_html(open(fn, encoding="utf-8", errors="replace").read())
            print(fn, json.dumps(raw, ensure_ascii=False))
            for row in phrases_of(raw):
                print("   ", row)


if __name__ == "__main__":
    main(sys.argv[1:])
