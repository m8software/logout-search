"""Answer pages from open dumps (2026-10-11) — the pages Common Crawl cannot give us.

The sites people ask questions of shut CCBot out (wikihow, healthline, consumerreports name it;
stackoverflow shuts out every crawler), so an answer-page corpus cannot be crawled. Some of it is
published as a file under a licence that allows a product we sell (docs/legal/SEARCH-DATA-SOURCES.md:
commercial use, and combinable with CC BY-SA):

    stackexchange   se_docs (stackexchange.py, the network's data dump)      CC BY-SA 2.5 / 3.0 / 4.0
    wikibooks       enwikibooks pages-articles, with its Cookbook            CC BY-SA 4.0
    wikivoyage      enwikivoyage pages-articles                              CC BY-SA 4.0
    medlineplus     MedlinePlus health topic XML (summaries only)            public domain (US government)

NOT here, and why: wikiHow and iFixit are CC BY-NC-SA (non-commercial); the A.D.A.M. encyclopedia and
the drug monographs on MedlinePlus are copyrighted, so only the health topic summaries are read.

Every source becomes rows of page_docs, the table the crawled inner pages are in, with crawl
'open:<source>:<dump>'. From there nothing is new: the release ships a page as its title, its address
and its sorted words (ADR-059), and the address is the attribution each licence asks for. A site with
'open:' rows is an answer site to the release cut (sqlite_lookups/build.py s_answer).

    open-pages --source stackexchange [--min-score 3] [--per-site 20000]
    open-pages --source wikibooks|wikivoyage --file <pages-articles.xml.bz2> [--procs 4]
    open-pages --source medlineplus [--file <mplus_topics.xml>]
"""

from __future__ import annotations

import bz2
import html
import io
import os
import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile

USER_AGENT = "logout-search-import/1 (personal search index)"
MEDLINE_INDEX = "https://medlineplus.gov/xml.html"
# Wikimedia projects read from a pages-articles dump: host, and the namespaces that hold what a person
# searches for, by NAME (the Cookbook's number is the wiki's own; the dump's header says which it is).
WIKIS = {
    "wikibooks": ("en.wikibooks.org", ("", "Cookbook")),
    "wikivoyage": ("en.wikivoyage.org", ("",)),
}
SOURCES = ("stackexchange", *WIKIS, "medlineplus")
MIN_BODY = 200          # a stub or a bare list of links answers nothing
BODY_CHARS = 4000
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")
_MEDLINE_ZIP = re.compile(r'https://medlineplus\.gov/xml/mplus_topics_compressed_(\d{4}-\d{2}-\d{2})\.zip')


def log(msg: str) -> None:
    print(f"[open-pages] {msg}", flush=True)


def crawl_tag(source: str, dump: str) -> str:
    return f"open:{source}:{dump}"


def _text(markup: str | None) -> str:
    return _WS.sub(" ", html.unescape(_TAG.sub(" ", markup or ""))).strip()


# --- Stack Exchange ---------------------------------------------------------------------------------

def se_domain(site: str) -> str:
    """cooking.stackexchange.com -> stackexchange.com; superuser.com stays (page_docs.domain is the
    registrable domain, as for a crawled page on a subdomain)."""
    return "stackexchange.com" if site.endswith(".stackexchange.com") else site


def se_budget(rows, min_score: int, per_site: int):
    """(site, qid, title, tags, score, question, answer) rows -> the ones kept: score >= min_score, and a
    site's per_site best by score (the lower question id on a tie, so a rerun keeps the same pages)."""
    by_site: dict[str, list] = {}
    for r in rows:
        if r[4] >= min_score and (r[2] or "").strip():
            by_site.setdefault(r[0], []).append(r)
    for site in sorted(by_site):
        yield from sorted(by_site[site], key=lambda r: (-r[4], r[1]))[:per_site]


def se_stream(conn, min_score: int, per_site: int):
    """The same rows as se_budget(), read one site at a time so 1.5 million questions never sit in memory at
    once (the all-at-once read was killed at 4 GB, 2026-10-17). Same order inside a site.

    `conn` MUST NOT be the connection the COPY in write_pages() runs on: a query sent on a connection that is
    in the middle of a COPY waits forever (the first version did that and hung for 4 h, 2026-10-17)."""
    sites = [r[0] for r in conn.execute("SELECT DISTINCT site FROM se_docs ORDER BY site").fetchall()]
    total = 0
    for site in sites:
        rows = conn.execute(
            "SELECT site, qid, title, tags, score, question, answer FROM se_docs "
            "WHERE site = %s AND score >= %s AND btrim(coalesce(title, ''), E' \\t\\r\\n') <> '' "
            "ORDER BY score DESC, qid LIMIT %s", (site, min_score, per_site)).fetchall()
        total += len(rows)
        log(f"stackexchange: {site}: {len(rows):,} questions (total {total:,})")
        yield from rows


def se_page(row) -> tuple:
    """-> (url, domain, title, description, body). The address is the question's canonical short form."""
    site, qid, title, tags, _score, question, answer = row
    body = " ".join(p for p in (tags, question, answer) if p)
    return (f"https://{site}/questions/{qid}", se_domain(site), title.strip(), None, body)


# --- Wikimedia projects -----------------------------------------------------------------------------

def wiki_url(host: str, title: str) -> str:
    return f"https://{host}/wiki/" + urllib.parse.quote(title.replace(" ", "_"), safe="/:(),'!*-._~")


def wiki_pages(stream, names: tuple[str, ...]):
    """A pages-articles XML stream -> (page_id, rev_id, title, wikitext) for the pages of the namespaces
    called `names` that are not redirects. Tags are matched without the export schema's version."""
    keep: set[str] = set()
    for _ev, el in ET.iterparse(stream, events=("end",)):
        tag = el.tag.rsplit("}", 1)[-1]
        if tag == "namespace":
            if (el.text or "") in names:
                keep.add(el.get("key"))
        elif tag == "page":
            f = {c.tag.rsplit("}", 1)[-1]: c for c in el}
            rev = f.get("revision")
            if (f["ns"].text if "ns" in f else None) in keep and "redirect" not in f and rev is not None:
                r = {c.tag.rsplit("}", 1)[-1]: c for c in rev}
                text = r["text"].text if "text" in r else None
                if text:
                    yield (int(f["id"].text or 0), int(r["id"].text or 0) if "id" in r else 0,
                           f["title"].text or "", text)
            el.clear()


def wiki_page(host: str, parsed: tuple | None) -> tuple | None:
    """wikiparse.parse_article's row -> a page row, or None for a stub or the project's front page."""
    if parsed is None:
        return None
    _pid, _rev, title, headings, body, _infobox = parsed
    if len(body) < MIN_BODY or title == "Main Page":
        return None
    return (wiki_url(host, title), host.split(".", 1)[1], title, None, f"{headings} {body}".strip())


# --- MedlinePlus ------------------------------------------------------------------------------------

def medline_topics(stream):
    """The health topic XML -> page rows for the English topics. Only what NLM lists as public domain is
    read: the topic's own summary, its other names and its groups. The <site> links under a topic lead to
    other publishers and to the copyrighted encyclopedia, and are not read."""
    for _ev, el in ET.iterparse(stream, events=("end",)):
        if el.tag != "health-topic":
            continue
        if el.get("language") == "English" and el.get("url") and el.get("title"):
            names = [c.text or "" for c in el if c.tag in ("also-called", "see-reference", "group")]
            body = " ".join([*names, _text(el.findtext("full-summary"))]).strip()[:BODY_CHARS]
            if len(body) >= MIN_BODY:
                yield (el.get("url"), "medlineplus.gov", el.get("title"), el.get("meta-desc") or None, body)
        el.clear()


def medline_latest(index_html: str) -> tuple[str, str]:
    """NLM's XML page -> (url, date) of the newest compressed health topic file."""
    found = sorted({(m.group(1), m.group(0)) for m in _MEDLINE_ZIP.finditer(index_html)})
    if not found:
        raise SystemExit(f"{MEDLINE_INDEX}: no mplus_topics_compressed_<date>.zip link — the page changed")
    return found[-1][1], found[-1][0]


def _get(url: str) -> bytes:
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": USER_AGENT}), timeout=120) as r:
        return r.read()


# --- page_docs --------------------------------------------------------------------------------------

def write_pages(conn, source: str, dump: str, pages) -> int:
    """Replace the source's rows (any earlier dump of it) with `pages`, in one transaction. A page a crawl
    already fetched keeps its crawled row: the open row is not written over it."""
    crawl, n = crawl_tag(source, dump), 0
    with conn.transaction():
        conn.execute("DELETE FROM page_docs WHERE crawl LIKE %s", (crawl_tag(source, "") + "%",))
        conn.execute("CREATE TEMP TABLE open_pages_in (url text, domain text, title text, description text, "
                     "body text) ON COMMIT DROP")
        with conn.cursor().copy("COPY open_pages_in (url, domain, title, description, body) FROM STDIN") as cp:
            for p in pages:
                cp.write_row(tuple(v.replace("\x00", "") if isinstance(v, str) else v for v in p))
                n += 1
        kept = conn.execute(
            "INSERT INTO page_docs (url, domain, crawl, http_status, title, description, lang, body) "
            "SELECT DISTINCT ON (url) url, domain, %s, 200, title, description, 'en', body FROM open_pages_in "
            "ORDER BY url ON CONFLICT (url) DO NOTHING", (crawl,)).rowcount
    log(f"{crawl}: {kept:,} pages written of {n:,} read")
    return kept


def cmd_open_pages(conn, source: str, file: str | None = None, min_score: int = 3, per_site: int = 20000,
                   procs: int = 4, dump: str | None = None) -> int:
    t0 = time.time()
    if source == "stackexchange":
        if conn.execute("SELECT to_regclass('se_docs')").fetchone()[0] is None:
            raise SystemExit("no se_docs table — run `search_import.py stackexchange` first")
        import stackexchange
        total = conn.execute("SELECT count(*) FROM se_docs").fetchone()[0]
        log(f"se_docs: {total:,} questions; keeping score >= {min_score}, the best {per_site:,} a site")
        import psycopg
        with psycopg.connect(autocommit=True) as reader:      # a second connection: see se_stream()
            n = write_pages(conn, source, dump or stackexchange.DUMP_TAG,
                            map(se_page, se_stream(reader, min_score, per_site)))
    elif source in WIKIS:
        if not file:
            raise SystemExit(f"--file <en{source}-…-pages-articles.xml.bz2> is required for {source}")
        from multiprocessing import Pool
        import wikiparse
        host, names = WIKIS[source]
        tag = dump or (re.search(r"-(\d{8}|latest)-", os.path.basename(file)) or [None, "unknown"])[1]
        with bz2.open(file, "rb") as f, Pool(procs) as pool:
            parsed = pool.imap(wikiparse.parse_article, wiki_pages(f, names), chunksize=64)
            n = write_pages(conn, source, tag, (p for p in (wiki_page(host, a) for a in parsed) if p))
    elif source == "medlineplus":
        if file:
            day = dump or (re.search(r"\d{4}-\d{2}-\d{2}", os.path.basename(file)) or ["unknown"])[0]
            with open(file, "rb") as f:
                n = write_pages(conn, source, day, medline_topics(f))
        else:
            url, day = medline_latest(_get(MEDLINE_INDEX).decode("utf-8", "replace"))
            with zipfile.ZipFile(io.BytesIO(_get(url))) as z:
                with z.open(next(m for m in z.namelist() if m.endswith(".xml"))) as f:
                    n = write_pages(conn, source, dump or day, medline_topics(f))
    else:
        raise SystemExit(f"unknown source {source!r}: one of {', '.join(SOURCES)}")
    log(f"{source}: {n:,} pages in {(time.time() - t0) / 60:.1f} min")
    return n
