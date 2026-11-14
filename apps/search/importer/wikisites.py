"""Official website per Wikipedia article (2026-09-24).

Site queries ("costco", "wells fargo", "spirit airlines") mostly land on the right
company's Wikipedia article, while the company's own homepage may never have been
fetched. The article already names the site: the infobox `website` / `url` field
({{URL|costco.com}}, [https://… name], a bare domain) or, failing that, an
{{Official website|…}} template in External links. This pass pulls just that —
regex over the raw wikitext, no full parse — from the same dump as wiki_docs, so
page ids line up.

Articles whose website lives only in Wikidata ({{Official URL}} with no argument)
yield nothing here.
"""

from __future__ import annotations

import re
import time
from multiprocessing import Pool
from urllib.parse import urlsplit

import tldextract

from wikiparse import _low_priority, _pages, log

_extract = tldextract.TLDExtract(suffix_list_urls=())   # bundled suffix list: no network

_INFOBOX = re.compile(r"\{\{\s*(infobox[^|\n}]*)", re.I)
_PARAM = re.compile(r"^\s*\|\s*(?:website|url|homepage|web_site)\s*=[ \t]*(.*)$", re.I | re.M)
_OFFICIAL = re.compile(r"\{\{\s*official(?:[ _]+(?:website|site|url|homepage))?\s*\|([^{}]*)\}\}", re.I)
_URLTPL = re.compile(r"\{\{\s*url\s*\|\s*([^|{}]+)", re.I)
_BRACKET = re.compile(r"\[\s*(https?://[^\s\]|]+)", re.I)
_BARE = re.compile(r"^(https?://[^\s|{}\[\]<]+|(?:www\.)?[a-z0-9-]+(?:\.[a-z0-9-]+)+(?:/[^\s|{}\[\]<]*)?)", re.I)
_COMMENT = re.compile(r"<!--.*?-->", re.S)

# Not an organisation's own site even when an infobox says so.
_NOT_OWN = ("archive.org", "wikipedia.org", "wikidata.org", "wikimedia.org", "facebook.com",
            "instagram.com", "twitter.com", "x.com", "linkedin.com",
            # short / alternate hosts of the same (2026-09-24: fb.com/YRMPH was an "official site")
            "fb.com", "fb.me", "instagr.am", "t.co", "youtu.be", "lnkd.in", "linktr.ee", "threads.net")
# google.com was here and dropped every Google product (Drive, Maps, Gmail live under it).
# sites.google.com pages are rejected as user sites below.
_NOT_OWN_HOSTS = ("sites.google.com",)


def _registrable(host: str) -> str:
    r = _extract(host)
    return f"{r.domain}.{r.suffix}".lower() if r.domain and r.suffix else ""


def _is_subject(dom: str, subject: str | None) -> bool:
    """The article is about this very site: "Instagram" -> instagram.com, "X (social
    network)" -> x.com. Then a normally-rejected host (a social network) IS the answer."""
    if not subject:
        return False
    label = dom.split(".")[0]
    key = re.sub(r"[^a-z0-9]", "", re.sub(r"\s*\(.*\)$", "", subject.lower()))
    return bool(label) and key == label


def normalize(candidate: str, subject: str | None = None) -> tuple[str, str] | None:
    """A written-down website -> (url, registrable domain), or None if it is not one."""
    c = candidate.strip().strip("'\"").rstrip(".,;")
    if not c or "{{" in c or " " in c:
        return None
    if not re.match(r"https?://", c, re.I):
        c = "https://" + c
    try:
        host = (urlsplit(c).hostname or "").lower()
    except ValueError:   # "[" in the value reads as a broken IPv6 literal (crashed the 2026-09-24 run)
        return None
    dom = _registrable(host)
    if not dom or host in _NOT_OWN_HOSTS:
        return None
    if any(dom == n or dom.endswith("." + n) for n in _NOT_OWN) and not _is_subject(dom, subject):
        return None
    return c, dom


def url_from_value(value: str) -> tuple[str, str] | None:
    v = _COMMENT.sub("", value).strip()
    for rx in (_URLTPL, _BRACKET, _BARE):
        m = rx.search(v) if rx is not _BARE else rx.match(v)
        if m:
            return normalize(m.group(1))
    return None


def _official_arg(args: str) -> str:
    """{{Official website|https://x|name=…}} or {{Official website|url=https://x}}."""
    parts = [p.strip() for p in args.split("|")]
    for p in parts:
        if p.lower().startswith("url="):
            return p[4:]
    return next((p for p in parts if p and "=" not in p), "")


def extract_site(text: str) -> tuple[str, str, str] | None:
    """Article wikitext -> (url, domain, infobox kind) or None."""
    box = _INFOBOX.search(text[:20_000])
    kind = re.sub(r"\s+", " ", box.group(1)).strip().lower() if box else ""
    if box:
        for m in _PARAM.finditer(text, box.start(), min(len(text), box.start() + 20_000)):
            got = url_from_value(m.group(1))
            if got:
                return got[0], got[1], kind
    for m in _OFFICIAL.finditer(text):
        got = url_from_value(_official_arg(m.group(1)))
        if got:
            return got[0], got[1], kind
    return None


def _work(item):
    pid, text = item
    try:
        got = extract_site(text)
    except Exception:   # one odd article must not stop a 7M-article pass
        return None
    return (pid, *got) if got else None


def cmd_wiki_sites(conn, path: str, procs: int) -> None:
    _low_priority()
    t0 = time.time()
    conn.execute("DROP TABLE IF EXISTS wiki_sites_new")
    conn.execute("CREATE UNLOGGED TABLE wiki_sites_new (page_id integer, url text, domain text, box text)")
    n_art = n_site = logged = 0
    batch = []

    def articles():
        nonlocal n_art
        for kind, pid, _rid, _title, text in _pages(path):
            if kind == "article":
                n_art += 1
                yield pid, text

    def flush():
        with conn.cursor().copy("COPY wiki_sites_new (page_id, url, domain, box) FROM STDIN") as cp:
            for r in batch:
                cp.write_row(r)
        batch.clear()

    with Pool(procs, initializer=_low_priority) as pool:
        for row in pool.imap(_work, articles(), chunksize=256):
            if row:
                n_site += 1
                batch.append(row)
                if len(batch) >= 5000:
                    flush()
            # n_art is counted by the feeder, a chunk ahead of the results: it is almost never an exact multiple
            # here, so "n_art % 500_000 == 0" never printed
            if n_art - logged >= 500_000:
                logged = n_art
                log(f"{n_art:,} articles, {n_site:,} with a website, {(time.time() - t0) / 60:.1f} min")
    if batch:
        flush()
    # Rank and history come along for search-api's "confident"; blocked sites never ship.
    with conn.transaction():
        conn.execute("DROP TABLE IF EXISTS wiki_sites")
        conn.execute(
            "CREATE TABLE wiki_sites AS "
            "SELECT DISTINCT ON (w.page_id) w.page_id, w.url, w.domain, w.box, "
            "       s.tranco_rank, coalesce(s.history_visits, 0) AS history_visits "
            "FROM wiki_sites_new w LEFT JOIN sites s ON s.domain = w.domain "
            "WHERE s.blocked_by IS NULL ORDER BY w.page_id")
        conn.execute("ALTER TABLE wiki_sites ADD PRIMARY KEY (page_id)")
        # search-api's organisation lookup goes site -> article ("cvs" -> cvs.com -> CVS Pharmacy)
        conn.execute("CREATE INDEX wiki_sites_domain ON wiki_sites (domain)")
        conn.execute("DROP TABLE wiki_sites_new")
    n = conn.execute("SELECT count(*), count(tranco_rank) FROM wiki_sites").fetchone()
    log(f"wiki_sites ready: {n[0]:,} articles with an official site ({n[1]:,} of those sites in Tranco), "
        f"from {n_art:,} articles in {(time.time() - t0) / 60:.1f} min")
