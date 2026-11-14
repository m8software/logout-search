"""Every outbound link the English Wikipedia editors chose, per article (2026-11-09, owner: "use the Wikipedia links
as much as possible"). wikisites.py keeps one thing per article, its official website; this pass keeps the rest:

  external   a bullet in the "External links" section (the links an editor thinks a reader should follow)
  further    a bullet in "Further reading" / "Bibliography" / "Sources" style sections
  citation   a url of a <ref> / {{cite ...}} in the body (what the article's facts were taken from)

Regex over the raw wikitext of the same dump as wiki_docs / wiki_sites, no full parse. Standalone (no database):

    wikiext.py extract DUMP.xml.bz2 OUT.tsv.gz [--procs N] [--limit N]    -> title, kind, url, label, domain,
                                                                              section, sentence, cite_title
    wikiext.py contexts OUT.tsv.gz PAGES.sqlite [--tmp DIR] [--keep N]      -> per url: citing articles, best contexts
    wikiext.py sqlite  OUT.tsv.gz WIKILINKS.sqlite [--ext N] [--cite N]    -> the engine's file (SEARCH_WIKILINKS)

What is dropped: archive.org / archive.today / webcitation wrappers (the original url is kept when it is inside the
wrapper), a url that sits under {{dead link}} or url-status=dead|usurped|unfit, non-http schemes, Wikimedia's own
sites, and obvious junk (bare IPs, localhost, search-result and share-button urls). `archive-url=` is never read.

Context columns (2026-11-10): `section` is the heading the link sits under; `sentence` is the prose sentence a
<ref> is attached to (markup, templates and nested refs stripped, about 300 characters; empty for External links /
Further reading, for a ref inside a table or infobox, and for a link written in the prose itself); `cite_title` is the
cite template's title= when there is one. A named ref reused with <ref name=x/> adds its own row, with the sentence of
the place that reuses it, so one url can have several rows (one per distinct sentence); a url with no sentence anywhere
keeps its one row. The text of a sentence is Wikipedia's (CC BY-SA 4.0).

The domain trust signal is the number of DISTINCT articles that link a registered domain (any kind). Wikipedia's
text is CC BY-SA 4.0; the links are facts, the compilation is derived from it (docs/legal/SEARCH-DATA-LICENSES.md).
"""

from __future__ import annotations

import bisect
import gzip
import html
import re
import sqlite3
import sys
import time
from urllib.parse import unquote, urlsplit

try:                      # the importer image has both; a bare checkout runs the tests with the fallback
    from wikisites import _registrable
except Exception:         # pragma: no cover
    _registrable = None

KINDS = ("external", "further", "citation")

_COMMENT = re.compile(r"<!--.*?-->", re.S)
_HEADING = re.compile(r"^(={2,6})\s*(.*?)\s*\1\s*$", re.M)
_REF = re.compile(r"<ref\b[^>]*?(?<!/)>(.*?)</ref\s*>", re.S | re.I)
_REF_SELF = re.compile(r"<ref\b[^>]*/>", re.I)
# a url the editors wrote: scheme, then up to whitespace / bracket / pipe / brace / angle / quote
_URL = r"(?:https?:)?//[^\s\[\]|{}<>\"']+"
_BRACKET = re.compile(r"\[\s*(" + _URL + r")(?:[ \t]+([^\]\n]*))?\]", re.I)
_BARE = re.compile(r"(?<![\w/=\[|])(https?://[^\s\[\]|{}<>\"']+)", re.I)
_CITE = re.compile(r"\{\{\s*(?:cite|citation|cita)\b[^{}]*(?:\{\{[^{}]*\}\}[^{}]*)*\}\}", re.I | re.S)
_PARAM = re.compile(r"\|\s*([a-z][a-z0-9 _-]*?)\s*=\s*((?:[^|{}]|\{\{[^{}]*\}\})*)", re.I)
_URLTPL = re.compile(r"\{\{\s*url\s*\|\s*(?:1\s*=\s*)?([^|{}]+)(?:\|\s*(?:2\s*=\s*)?([^|{}]*))?", re.I)
_OFFICIAL = re.compile(r"\{\{\s*official\b", re.I)
_DEAD = re.compile(r"\{\{\s*(?:dead[ _-]*link|dl|broken[ _-]*link|webarchive-needed|linkrot|404)\b", re.I)
_DEAD_STATUS = re.compile(r"\|\s*(?:url-status|dead-url|deadurl)\s*=\s*(?:dead|yes|y|true|usurped|unfit)\b", re.I)

_EXT_SECTION = re.compile(r"^(external links?|external sources?|links)$", re.I)
_FURTHER_SECTION = re.compile(r"^(further reading|bibliography|selected bibliography|general references|"
                              r"works cited|reading list|additional reading|suggested reading)$", re.I)

_ARCHIVE_HOSTS = ("web.archive.org", "archive.org", "archive.today", "archive.is", "archive.ph", "archive.fo",
                  "archive.li", "archive.md", "archive.vn", "webcitation.org", "ghostarchive.org", "perma.cc",
                  "wayback.archive-it.org", "webarchive.org.uk", "webarchive.nationalarchives.gov.uk",
                  "arquivo.pt", "freezepage.com", "timetravel.mementoweb.org", "archive-it.org", "cachedview.nl",
                  "waybackmachine.org")
_WAYBACK = re.compile(r"^https?://(?:web\.)?archive\.org/(?:web/)?(?:\d{1,14}[a-z_]*/)(https?:/{1,2}.+)$", re.I)
_ARCHIVE_TODAY = re.compile(r"^https?://archive\.(?:today|is|ph|fo|li|md|vn)/(?:\d{8,14}/|o/\w+/)(https?:/{1,2}.+)$", re.I)
# Wikimedia's own sites and shorteners: not "somewhere else on the web"
_SKIP_DOMAINS = frozenset((
    "wikipedia.org", "wikidata.org", "wikimedia.org", "wikibooks.org", "wikiquote.org", "wikisource.org",
    "wikinews.org", "wikiversity.org", "wikivoyage.org", "wiktionary.org", "mediawiki.org", "wikimediafoundation.org",
    "toolforge.org", "wmflabs.org", "t.co", "bit.ly", "goo.gl", "tinyurl.com", "ow.ly", "is.gd", "fb.me", "youtu.be",
    "lnkd.in", "dlvr.it", "buff.ly", "tiny.cc", "rb.gy", "shorturl.at", "cutt.ly"))
_JUNK_PATH = re.compile(r"(?:^|[/?&#])(?:share|sharer|intent/tweet|login|signin|sign_in|search|results|cgi-bin/search|"
                        r"redirect|out|go|click|track|utm_|print|feed|rss)(?:$|[/?&=#.])", re.I)
_JUNK_HOST = re.compile(r"^(?:localhost|\d{1,3}(?:\.\d{1,3}){3}|\[.*\]|(?:books|scholar|news|translate|webcache|docs|"
                        r"drive|maps|play|groups|sites)\.google\.[a-z.]+|google\.[a-z.]+|www\.google\.[a-z.]+)$")
_TRAIL = ".,;:!?)'\"”’"


def _registrable_fallback(host: str) -> str:
    p = host.split(".")
    if len(p) >= 3 and p[-2] in ("co", "com", "org", "ac", "gov", "net", "edu") and len(p[-1]) == 2:
        return ".".join(p[-3:])
    return ".".join(p[-2:])


def registrable(host: str) -> str:
    if _registrable is not None:
        try:
            return _registrable(host)
        except Exception:
            pass
    return _registrable_fallback(host)


def unwrap(url: str) -> str | None:
    """A web.archive.org / archive.today snapshot -> the page it holds; any other archive wrapper -> None."""
    m = _WAYBACK.match(url) or _ARCHIVE_TODAY.match(url)
    if m:
        inner = m.group(1)
        inner = re.sub(r"^(https?):/(?!/)", r"\1://", inner, flags=re.I)
        return inner
    return url


def clean_url(raw: str) -> tuple[str, str] | None:
    """A url as an editor wrote it -> (url, registrable domain), or None when it is not worth keeping."""
    u = html.unescape(raw).strip()
    if u.startswith("//"):
        u = "https:" + u
    if not re.match(r"^https?://", u, re.I):
        return None
    for _ in range(2):                                    # a snapshot of a snapshot is rare; twice is plenty
        w = unwrap(u)
        if w is None:
            return None
        u = w
    u = u.rstrip(_TRAIL)
    if len(u) > 300 or "{{" in u or "%7B%7B" in u or " " in u:
        return None
    try:
        sp = urlsplit(u)
        host = (sp.hostname or "").lower().rstrip(".")
    except ValueError:
        return None
    if not host or "." not in host or _JUNK_HOST.match(host):
        return None
    if any(host == a or host.endswith("." + a) for a in _ARCHIVE_HOSTS):
        return None                                       # a wrapper whose original is not in the url
    dom = registrable(host)
    if not dom or dom in _SKIP_DOMAINS:
        return None
    path_q = unquote((sp.path or "") + ("?" + sp.query if sp.query else ""))
    if _JUNK_PATH.search(path_q) and len(path_q) > 1:
        return None
    return u, dom


def _label(text: str) -> str:
    t = re.sub(r"\[\[(?:[^|\]]*\|)?([^\]]*)\]\]", r"\1", text or "")
    t = re.sub(r"\{\{[^{}]*\}\}", "", t)
    t = re.sub(r"<[^>]+>", "", t)
    t = re.sub(r"'{2,}", "", t)
    t = html.unescape(t)
    t = re.sub(r"\s+", " ", t).strip(" \t-–—:|")
    if re.fullmatch(r"[\d\s,.–—-]+|\[?\d+\]?|(?:pp?|page)\.?\s*[\d–-]+", t, re.I):   # "39", "47–50": a page, not a title
        return ""
    return t[:140]


def _sections(text: str):
    """[(heading, body)] with the lead as ("", body); level-2 and deeper headings both start a section."""
    out, last, name, raw = [], 0, "", ""
    for m in _HEADING.finditer(text):
        out.append((name, text[last:m.start()], raw))
        raw = _label(m.group(2))
        name, last = raw.lower(), m.end()
    out.append((name, text[last:], raw))
    return out


def _links_in(chunk: str):
    """(raw url, label, site, cite title) in a stretch of wikitext: cite templates' url=, [url label], {{URL|…}}, bare urls."""
    seen = set()
    for m in _CITE.finditer(chunk):
        tpl = m.group(0)
        if _DEAD_STATUS.search(tpl):
            continue
        params = {k.strip().lower().replace(" ", "-"): v.strip() for k, v in _PARAM.findall(tpl)}
        url = params.get("url") or params.get("chapter-url") or params.get("contribution-url")
        if not url:
            continue
        label = params.get("title") or params.get("chapter") or params.get("work") or params.get("website") or ""
        site = params.get("website") or params.get("work") or params.get("publisher") or ""
        yield url, _label(label), _label(site), _label(params.get("title") or "")
        seen.add(url)
    rest = _CITE.sub(" ", chunk)
    for m in _URLTPL.finditer(rest):
        if _OFFICIAL.search(rest[max(0, m.start() - 40):m.start()]):
            continue
        yield m.group(1).strip(), _label(m.group(2) or ""), "", ""
    for m in _BRACKET.finditer(rest):
        yield m.group(1), _label(m.group(2) or ""), "", ""
    for m in _BARE.finditer(_BRACKET.sub(" ", rest)):
        yield m.group(1), "", "", ""


_REF_ANY = re.compile(r"<ref\b[^>]*?/>|<ref\b[^>]*?(?<!/)>.*?</ref\s*>", re.S | re.I)
_REF_NAME = re.compile(r"""<ref\b[^>]*?\bname\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'/>]+))""", re.I)
_MARK0 = 0xF0000                                         # one private-use character per ref, to find its place in the prose
_MARKS = re.compile("[\U000F0000-\U000FFFFD]")
_TOKENS = re.compile(r"^\{\||^\|\}|\{\{|\}\}", re.M)
_REFLIST = re.compile(r"\{\{\s*(?:reflist|refbegin|notelist|references|ref list|refs)\b", re.I)
_REFERENCES_TAG = re.compile(r"<references\b[^>]*?(?:/>|>.*?</references\s*>)", re.S | re.I)
_ABBR = frozenset(("mr mrs ms dr st jr sr vs inc ltd co corp no nos etc approx ca c cf al gen col lt sgt capt cmdr adm "
                   "prof rev hon gov sen rep pres mt ft dept est vol vols ed eds fig pp p ch fl b d r ave blvd rd mrs "
                   "jan feb mar apr jun jul aug sep sept oct nov dec").split())
_SENT_END = re.compile(r"[.!?](?:[\"'”’)\]]|[\U000F0000-\U000FFFFD])*\s+(?=[\"'“‘(\[]?[A-Z0-9\U000F0000-\U000FFFFD])")
_FILELINK = re.compile(r"\[\[(?:File|Image|Category|Media):[^\[\]]*(?:\[\[[^\]]*\]\][^\[\]]*)*\]\]", re.I)
_EXTLINK_TXT = re.compile(r"\[(?:https?:)?//[^\s\]]+[ \t]+([^\]\n]*)\]", re.I)
_EXTLINK_BARE = re.compile(r"\[(?:https?:)?//[^\s\]]+\]", re.I)
_SENT_MAX = 300


def _prose_spans(t: str):
    """[(start, end, kind)] of top-level multi-line templates and tables ('np': infobox, navbox, table: no prose) and of
    reference-list templates ('defs': refs defined there have no place in the text)."""
    out, stack = [], []
    for m in _TOKENS.finditer(t):
        tok = m.group(0)
        if tok in ("{{", "{|"):
            stack.append((tok, m.start()))
        elif stack and stack[-1][0] == ("{{" if tok == "}}" else "{|"):
            op, st = stack.pop()
            if not stack:
                body = t[st:m.end()]
                if op == "{{" and _REFLIST.match(body):
                    out.append((st, m.end(), "defs"))
                elif "\n" in body and len(body) < (30000 if op == "{{" else 200000):
                    out.append((st, m.end(), "np"))
    for m in _REFERENCES_TAG.finditer(t):
        out.append((m.start(), m.end(), "defs"))
    out.sort()
    return out


def _clean_prose(s: str) -> str:
    for _ in range(6):
        n = re.sub(r"\{\{[^{}]*\}\}", "", s)
        if n == s:
            break
        s = n
    s = s.replace("{{", "").replace("}}", "")
    s = _FILELINK.sub("", s)
    s = re.sub(r"\[\[(?:[^|\]]*\|)?([^\]]*)\]\]", r"\1", s)
    s = _EXTLINK_TXT.sub(r"\1", s)
    s = _EXTLINK_BARE.sub("", s)
    s = re.sub(r"https?://\S+", "", s)
    s = re.sub(r"<br\s*/?>", " ", s, flags=re.I)
    s = re.sub(r"<(math|gallery|timeline|score|syntaxhighlight|source|pre|nowiki)\b[^>]*>.*?</\1\s*>", "", s, flags=re.S | re.I)
    s = re.sub(r"<[^>]+>", "", s)
    s = re.sub(r"'{2,}", "", s)
    s = html.unescape(s)
    return s


def _split_sentences(s: str):
    """[(start, end)] of the sentences of a cleaned paragraph; no split after an abbreviation or an initial."""
    bounds, last = [], 0
    for m in _SENT_END.finditer(s):
        w = re.search(r"([A-Za-z]+)$", s[:m.start()])
        if s[m.start()] == "." and w and (w.group(1).lower() in _ABBR or (len(w.group(1)) == 1 and w.group(1).isupper())):
            continue
        bounds.append((last, m.end() - len(m.group(0)) + len(m.group(0).rstrip())))
        last = m.end()
    bounds.append((last, len(s)))
    return bounds


def _sentence_for(line: str, marker: str) -> str:
    """The sentence of a cleaned paragraph that holds `marker`; ~300 characters around the marker; '' for a fragment."""
    i = line.find(marker)
    if i < 0:
        return ""
    spans = _split_sentences(line)
    k = next((n for n, (a, b) in enumerate(spans) if a <= i <= b + 1), len(spans) - 1)
    a, b = spans[k]
    seg = line[a:b + (1 if b < len(line) and _MARKS.match(line[b]) else 0)]
    if len(_MARKS.sub("", seg).split()) < 4 and k > 0 and i - a <= 1:        # "Sentence. <ref>": the ref is the last one's
        a, b = spans[k - 1]
        seg = line[a:b]
        i = len(seg)
    else:
        i -= a
    if len(seg) > _SENT_MAX:
        start = max(0, min(i - 200, len(seg) - _SENT_MAX))
        seg = seg[start:start + _SENT_MAX]
        if start:
            seg = seg.split(" ", 1)[-1]
        if len(seg) == _SENT_MAX and " " in seg:
            seg = seg.rsplit(" ", 1)[0]
    seg = re.sub(r"\s+", " ", _MARKS.sub("", seg)).strip(" \t*#:;")
    if len(seg.split()) < 4 or not re.search(r"[A-Za-z]{3}", seg):
        return ""
    return seg


def extract_contexts(text: str) -> list[tuple[str, str, str, str, str, str, str]]:
    """Article wikitext -> [(kind, url, label, domain, section, sentence, cite_title)] in page order (links of the
    sections first, then the references). A url keeps one row per distinct citing sentence, or one row when it has none."""
    text = _COMMENT.sub("", text)
    rows, seen, seen_ctx = [], set(), set()

    def add(kind, raw, label, site="", section="", sentence="", title=""):
        got = clean_url(raw)
        if not got:
            return
        url, dom = got
        key = re.sub(r"^https?://(www\.)?", "", url.lower()).rstrip("/")
        if sentence:
            if (key, sentence) in seen_ctx:
                return
            seen_ctx.add((key, sentence))
        elif key in seen:
            return
        seen.add(key)
        rows.append([kind, url, label or site, dom, section, sentence, title, key])

    # references: the ones in the text, with their place (marker characters), and the named bodies a <ref name=x/> reuses
    refs = [(m, i) for i, m in enumerate(_REF_ANY.finditer(text))]
    defs = {}
    for m, _i in refs:
        if not m.group(0).endswith("/>"):
            nm = _REF_NAME.match(m.group(0))
            if nm:
                defs.setdefault((nm.group(1) or nm.group(2) or nm.group(3) or "").strip().lower(), m.group(0))
    pieces, last = [], 0
    for m, i in refs:
        pieces.append(text[last:m.start()])
        pieces.append(chr(_MARK0 + i) if _MARK0 + i <= 0xFFFFD else "")
        last = m.end()
    pieces.append(text[last:])
    t2 = "".join(pieces)
    spans = _prose_spans(t2)
    span_starts = [x[0] for x in spans]
    heads = [(m.start(), _label(m.group(2))) for m in _HEADING.finditer(t2)]
    head_pos = [h[0] for h in heads]
    # cleaned paragraphs of t2 (markers kept), each cleaned once
    para_clean = {}
    pos = 0
    for ln in t2.split("\n"):
        if _MARKS.search(ln):
            para_clean[pos] = ln
        pos += len(ln) + 1
    para_starts = sorted(para_clean)

    def place(i):
        """(section heading, sentence) for ref number i."""
        mk = chr(_MARK0 + i)
        p = t2.find(mk)
        if p < 0:
            return "", ""
        h = bisect.bisect_right(head_pos, p) - 1
        sec = heads[h][1] if h >= 0 else ""
        # a ref inside a table / infobox / reference list has no sentence
        q = bisect.bisect_right(span_starts, p) - 1
        if q >= 0 and spans[q][1] > p:
            return sec, ""
        k = bisect.bisect_right(para_starts, p) - 1
        if k < 0:
            return sec, ""
        ln = para_clean[para_starts[k]]
        if re.match(r"\s*(?:\||!|\{\||\{\{|=)", ln):
            return sec, ""
        cleaned = _clean_prose(ln)
        return sec, _sentence_for(cleaned, mk)

    # body of the page without refs: External links, Further reading and links written in the prose
    body_text = _REF_ANY.sub(" ", text)
    lines_alive = []
    for ln in body_text.split("\n"):
        if _DEAD.search(ln) or _DEAD_STATUS.search(ln):
            continue
        lines_alive.append(ln)
    body_text = "\n".join(lines_alive)
    for name, chunk, raw in _sections(body_text):
        if _EXT_SECTION.match(name):
            kind = "external"
        elif _FURTHER_SECTION.match(name):
            kind = "further"
        else:
            kind = "citation"
        for u, label, site, _t in _links_in(chunk):
            add(kind, u, label, site, raw)
    for m, i in refs:
        body = m.group(0)
        if body.endswith("/>"):
            nm = _REF_NAME.match(body)
            body = defs.get((nm.group(1) or nm.group(2) or nm.group(3) or "").strip().lower(), "") if nm else ""
        if not body or _DEAD.search(body) or _DEAD_STATUS.search(body):
            continue
        links = list(_links_in(re.sub(r"^<ref\b[^>]*>|</ref\s*>$", "", body, flags=re.I)))
        if not links:
            continue
        sec, sent = place(i)
        for u, label, site, ctitle in links:
            add("citation", u, label, site, sec, sent, ctitle)
    have = {r[7] for r in rows if r[5]}
    return [tuple(r[:7]) for r in rows if r[5] or r[7] not in have]


def extract_links(text: str) -> list[tuple[str, str, str, str]]:
    """Article wikitext -> [(kind, url, label, domain)], first mention of a url wins, order of the page kept."""
    out, seen = [], set()
    for kind, url, label, dom, _sec, _sent, _t in extract_contexts(text):
        if url not in seen:
            seen.add(url)
            out.append((kind, url, label, dom))
    return out


def _tsv(v: str) -> str:
    return re.sub(r"[\t\r\n]+", " ", v)


def _work(item):
    title, text = item
    try:
        return title, extract_contexts(text)
    except Exception:             # one odd article must not stop a 7M-article pass
        return title, []


def cmd_extract(path: str, out: str, procs: int, limit: int | None) -> None:
    from multiprocessing import Pool
    from wikiparse import _low_priority, _pages, log
    _low_priority()
    t0 = time.time()
    n_art = n_with = n_rows = logged = 0

    def articles():
        nonlocal n_art
        for kind, _pid, _rid, title, text in _pages(path):
            if kind == "article":
                n_art += 1
                if limit and n_art > limit:
                    return
                yield title, text

    with gzip.open(out, "wt", encoding="utf-8", compresslevel=3) as fh, Pool(procs, initializer=_low_priority) as pool:
        for title, links in pool.imap(_work, articles(), chunksize=64):
            if links:
                n_with += 1
            for kind, url, label, dom, sec, sent, ctitle in links:
                fh.write("\t".join((title, kind, url, _tsv(label), dom, _tsv(sec), _tsv(sent), _tsv(ctitle))) + "\n")
                n_rows += 1
            if n_art - logged >= 250_000:
                logged = n_art
                log(f"{n_art:,} articles, {n_with:,} with links, {n_rows:,} links, {(time.time() - t0) / 60:.1f} min")
    log(f"done: {n_art:,} articles, {n_with:,} with links, {n_rows:,} links in {(time.time() - t0) / 60:.1f} min")


def cmd_sqlite(src: str, dest: str, per_ext: int, per_cite: int, min_trust: int) -> None:
    """The engine's file: links(title, kind, pos, url, label, domain) capped per article, trust(domain, n).

    Per article: the first `per_ext` external/further links and the `per_cite` best citations, one per domain,
    best = domains cited by the most distinct articles (trust). Trust counts every kind, every article."""
    import collections
    t0 = time.time()
    trust = collections.Counter()
    last_title, doms = None, set()
    with gzip.open(src, "rt", encoding="utf-8") as fh:                       # pass 1: distinct articles per domain
        for ln in fh:
            f = ln.rstrip("\n").split("\t")
            if len(f) < 5:
                continue
            if f[0] != last_title:
                trust.update(doms)
                last_title, doms = f[0], set()
            doms.add(f[4])
    trust.update(doms)
    print(f"[wikiext] {len(trust):,} domains, pass 1 {time.time() - t0:.0f}s", file=sys.stderr, flush=True)
    db = sqlite3.connect(dest)
    db.executescript("PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;"
                     "DROP TABLE IF EXISTS links; DROP TABLE IF EXISTS trust;"
                     "CREATE TABLE links (title TEXT NOT NULL, kind TEXT NOT NULL, pos INTEGER NOT NULL, url TEXT NOT NULL,"
                     " label TEXT NOT NULL, domain TEXT NOT NULL);"
                     "CREATE TABLE trust (domain TEXT PRIMARY KEY, n INTEGER NOT NULL) WITHOUT ROWID;")

    def flush(title, rows):
        ext = [r for r in rows if r[0] in ("external", "further")]
        cite = [r for r in rows if r[0] == "citation"]
        keep, per_dom = [], set()
        for r in ext:                                                         # page order, one per domain
            if len(keep) < per_ext and r[3] not in per_dom:
                per_dom.add(r[3]); keep.append(r)
        cdom = set()
        for r in sorted(cite, key=lambda r: -trust[r[3]]):
            if trust[r[3]] < min_trust or r[3] in cdom or len([k for k in keep if k[0] == "citation"]) >= per_cite:
                continue
            cdom.add(r[3]); keep.append(r)
        db.executemany("INSERT INTO links VALUES (?,?,?,?,?,?)",
                       [(title, k, i, u, lb, d) for i, (k, u, lb, d) in enumerate(keep)])

    last_title, rows = None, []
    with gzip.open(src, "rt", encoding="utf-8") as fh:                       # pass 2
        for ln in fh:
            f = ln.rstrip("\n").split("\t")
            if len(f) < 5:
                continue
            if f[0] != last_title and last_title is not None:
                flush(last_title, rows)
                rows = []
            last_title = f[0]
            rows.append((f[1], f[2], f[3], f[4]))
    if rows:
        flush(last_title, rows)
    db.executemany("INSERT INTO trust VALUES (?,?)", [(d, n) for d, n in trust.items() if n >= 2])
    db.executescript("CREATE INDEX links_title ON links(title);")
    db.commit()
    n = db.execute("SELECT count(*), count(DISTINCT title) FROM links").fetchone()
    print(f"[wikiext] {dest}: {n[0]:,} links for {n[1]:,} articles in {time.time() - t0:.0f}s", file=sys.stderr, flush=True)


def cmd_contexts(src: str, dest: str, tmp: str | None, keep: int) -> None:
    """Per URL: distinct citing articles, number of contexts and the best `keep` contexts, into pages(url, domain, kind,
    title, n_articles, n_contexts, n_sentences, contexts JSON [[article, section, sentence], ...]).

    The extract file is in article order, so the rows are sorted by url (LC_ALL=C sort, nice, 2 threads, spill files in
    `tmp`) and read back in groups. Best = longest sentence up to the cap, one per article, a cite title preferred."""
    import json
    import os
    import subprocess
    import threading
    t0 = time.time()
    env = dict(os.environ, LC_ALL="C")
    cmd = ["nice", "-n", "19", "sort", "-t", "\t", "-k1,1", "-S", "3G", "--parallel=2"] + (["-T", tmp] if tmp else [])
    sp = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env)

    def feed():
        with gzip.open(src, "rt", encoding="utf-8") as fh:
            for ln in fh:
                f = ln.rstrip("\n").split("\t")
                if len(f) < 8:
                    continue
                # url, article, kind, label, domain, section, sentence, cite title
                sp.stdin.write(("\t".join((f[2], f[0], f[1], f[3], f[4], f[5], f[6], f[7])) + "\n").encode("utf-8"))
        sp.stdin.close()

    th = threading.Thread(target=feed)
    th.start()
    db = sqlite3.connect(dest)
    db.executescript("PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF; DROP TABLE IF EXISTS pages;"
                     "CREATE TABLE pages (url TEXT PRIMARY KEY, domain TEXT NOT NULL, kind TEXT NOT NULL, title TEXT NOT NULL,"
                     " n_articles INTEGER NOT NULL, n_contexts INTEGER NOT NULL, n_sentences INTEGER NOT NULL,"
                     " contexts TEXT NOT NULL) WITHOUT ROWID;")
    batch, n = [], 0

    def flush(url, dom, arts, kinds, titles, labels, ctx, n_ctx, n_sent):
        best, used = [], set()
        for sc, art, sec, sent in sorted(ctx, key=lambda c: (-c[0], c[1])):
            if art in used:
                continue
            used.add(art)
            best.append([art, sec, sent])
            if len(best) >= keep:
                break
        title = (titles.most_common(1) or labels.most_common(1) or [("", 0)])[0][0]
        kind = "citation" if kinds.get("citation") else sorted(kinds)[0]
        batch.append((url, dom, kind, title, len(arts), n_ctx, n_sent, json.dumps(best, ensure_ascii=False, separators=(",", ":"))))

    import collections
    cur, state = None, None
    for raw in sp.stdout:
        f = raw.decode("utf-8").rstrip("\n").split("\t")
        if len(f) < 8:
            continue
        url, art, kind, label, dom, sec, sent, ctitle = f[:8]
        if url != cur:
            if cur is not None:
                flush(cur, *state)
                if len(batch) >= 50000:
                    db.executemany("INSERT OR REPLACE INTO pages VALUES (?,?,?,?,?,?,?,?)", batch)
                    n += len(batch)
                    batch.clear()
                    if n % 1_000_000 < 50000:
                        print(f"[wikiext] {n:,} urls, {(time.time() - t0) / 60:.1f} min", file=sys.stderr, flush=True)
            cur = url
            state = [dom, set(), collections.Counter(), collections.Counter(), collections.Counter(), [], 0, 0]
        st = state
        st[1].add(art)
        st[2][kind] += 1
        if ctitle:
            st[3][ctitle] += 1
        if label:
            st[4][label] += 1
        st[6] += 1
        if sent:
            st[7] += 1
            st[5].append((min(len(sent), _SENT_MAX) + (20 if ctitle else 0), art, sec, sent))
            if len(st[5]) > 64:
                st[5] = sorted(st[5], key=lambda c: (-c[0], c[1]))[:32]
    if cur is not None:
        flush(cur, *state)
    th.join()
    sp.wait()
    db.executemany("INSERT OR REPLACE INTO pages VALUES (?,?,?,?,?,?,?,?)", batch)
    db.commit()
    c = db.execute("SELECT count(*), sum(n_sentences > 0), sum(title != '') FROM pages").fetchone()
    print(f"[wikiext] {dest}: {c[0]:,} urls, {c[1]:,} with a sentence, {c[2]:,} with a title in {(time.time() - t0) / 60:.1f} min",
          file=sys.stderr, flush=True)


def main(argv: list[str]) -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("extract")
    e.add_argument("dump"); e.add_argument("out")
    e.add_argument("--procs", type=int, default=2); e.add_argument("--limit", type=int)
    c = sub.add_parser("contexts")
    c.add_argument("src"); c.add_argument("dest"); c.add_argument("--tmp"); c.add_argument("--keep", type=int, default=8)
    s = sub.add_parser("sqlite")
    s.add_argument("src"); s.add_argument("dest")
    s.add_argument("--ext", type=int, default=12); s.add_argument("--cite", type=int, default=12)
    s.add_argument("--min-trust", type=int, default=5)
    a = ap.parse_args(argv)
    if a.cmd == "extract":
        cmd_extract(a.dump, a.out, a.procs, a.limit)
    elif a.cmd == "contexts":
        cmd_contexts(a.src, a.dest, a.tmp, a.keep)
    else:
        cmd_sqlite(a.src, a.dest, a.ext, a.cite, a.min_trust)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
