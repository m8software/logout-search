"""Wikipedia's own classification of articles, for "what kind of site" queries.

"password manager" or "video sharing site" should find sites of that kind. Wikipedia
already sorts the articles: Bitwarden is in [[Category:Password managers]], YouTube is
linked from "List of video hosting services". One streaming pass over the same dump as
wiki_docs writes two files (regex over the raw wikitext, no full parse):

  wiki-cats.tsv.gz       article_title \t category        explicit [[Category:…]] tags
  wiki-listlinks.tsv.gz  list_title \t article_title \t position
                         links in the body of "List of" / "Lists of" / "Comparison of"
                         pages, in order of first appearance

Titles use the dump's form (spaces, first letter upper) — the same as wiki_docs.title.
Link targets are NOT redirect-resolved: join them through wiki_aliases (norm) or
wiki_redirects_stage to reach the article a redirect points at.

Maintenance and tracking categories ("Articles with …", "Use dmy dates", "CS1 …",
"1970 births") are dropped.

--tree is a separate pass over the category pages (namespace 14) instead of the articles:

  wiki-cattree.tsv.gz    child_category \t parent_category   a category page's own tags

so "Password managers" reaches its parent "Security software". Both sides drop the article
pass's junk, hidden categories (__HIDDENCAT__), and the container/tracking parents above them.
"""

from __future__ import annotations

import bz2
import gzip
import html
import os
import re
import time
import xml.etree.ElementTree as ET
from multiprocessing import Pool

from wikiparse import _NS, _low_priority, _pages, log

_COMMENT = re.compile(r"<!--.*?-->", re.S)
_NOWIKI = re.compile(r"<nowiki>.*?</nowiki>", re.S | re.I)
_REF = re.compile(r"<ref[^>/]*/>|<ref[^>]*>.*?</ref>", re.S | re.I)
_CAT = re.compile(r"\[\[\s*category\s*:\s*([^\]|\n]+)(?:\|[^\]]*)?\]\]", re.I)
_LINK = re.compile(r"\[\[([^\[\]|\n]+)(?:\|[^\[\]]*)?\]\]")
_WS = re.compile(r"[\s_]+")
_LIST_TITLE = re.compile(r"^(List of|Lists of|Comparison of) ")
# Everything after these headings is navigation, not list members.
_TAIL = re.compile(r"^==+\s*(see also|references|external links|notes|further reading|"
                   r"bibliography|sources|citations|footnotes)\s*==+\s*$", re.I | re.M)
_JUNK_CAT = re.compile(
    r"articles|pages|wikipedia|wikidata|\bcs1\b|^use |\bstubs?\b|short description|webarchive|^all |"
    r"dead external links|^\d{4}s? (births|deaths)$|^\d+(st|nd|rd|th)-century (births|deaths)$|"
    r"^living people$|^year of (birth|death)|redirects|templates|infobox|coordinates|"
    r"unsourced|official website|commons category|lacking|needing|^engvar|with hcards|"
    r"disambiguation|good articles|featured articles", re.I)
# Category-tree only: meta categories that group categories rather than topics.
_JUNK_TREE = re.compile(
    r"hidden categor|tracking categor|container categor|commons category|maintenance categor|"
    r"^categories\b|categories (named|by|for|that|which|requiring|with)|"
    r"^(set|topic|eponymous) categor|diffus|user categor|wikiproject|categori[sz]ation|"
    r"(?<!grammatical) categories$|"
    r"^main topic classifications$|^fundamental categories$|^contents$", re.I)
# Hidden categories: the magic word, or (usually) a template that emits it.
_HIDDENCAT = re.compile(
    r"__HIDDENCAT__|\{\{\s*(hidden[ _]?category|hiddencat|tracking[ _]category|maintenance[ _]category|"
    r"monthly[ _]clean-?up[ _]category|(wikipedia|possibly empty|empty)[ _]category[^}]*\bhidden\s*=\s*(yes|y|true|1))",
    re.I)
# Link prefixes that are not articles: namespaces and the interwiki projects.
_NOT_ARTICLE = frozenset((
    "file", "image", "media", "category", "template", "t", "wikipedia", "wp", "project", "help",
    "portal", "user", "talk", "user talk", "draft", "module", "special", "mediawiki", "timedtext",
    "book", "education program", "gadget", "topic", "wikt", "wiktionary", "commons", "c", "s",
    "wikisource", "d", "wikidata", "q", "b", "wikibooks", "n", "wikinews", "v", "wikiversity",
    "voy", "wikivoyage", "species", "wikispecies", "meta", "m", "mw", "foundation", "wmf",
    "phab", "outreach", "incubator", "w", "iarchive", "doi", "rfc", "google",
))
_LANG = re.compile(r"^[a-z]{2,3}(?:-[a-z]+)*$")   # interlanguage: [[fr:…]], [[zh-yue:…]]


def _title(t: str) -> str:
    t = _WS.sub(" ", html.unescape(t)).strip()
    return t[:1].upper() + t[1:] if t else ""


def _cats(text: str) -> list[str]:
    out, seen = [], set()
    for m in _CAT.finditer(text):
        c = _title(m.group(1))
        if c and c not in seen and not _JUNK_CAT.search(c):
            seen.add(c)
            out.append(c)
    return out


def _link_target(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith(":"):          # [[:Category:X]] / [[:fr:X]] — a link to a non-article
        return ""
    raw = raw.split("#", 1)[0]
    if ":" in raw:
        pre = raw.split(":", 1)[0].strip()
        if pre.lower() in _NOT_ARTICLE or _LANG.match(pre):
            return ""
    return _title(raw)


def parse_page(item: tuple[str, str]) -> tuple[str, list[str], list[str] | None]:
    """(title, wikitext) -> (title, categories, list links or None)."""
    title, text = item
    text = _NOWIKI.sub("", _COMMENT.sub("", text))
    cats = _cats(text)
    links = None
    if _LIST_TITLE.match(title):
        body = _REF.sub("", text)
        m = _TAIL.search(body)
        if m:
            body = body[:m.start()]
        links, seen = [], {title}
        for lm in _LINK.finditer(body):
            t = _link_target(lm.group(1))
            if t and t not in seen:
                seen.add(t)
                links.append(t)
    return title, cats, links


def _clean(s: str) -> str:
    return s.replace("\t", " ").replace("\n", " ").replace("\r", " ")


def _category_pages(path: str):
    """Yield (name, wikitext) for namespace-14 pages, "Category:" stripped. wikiparse._pages
    yields namespace 0 only and its callers depend on that, so this is its own loop."""
    f = bz2.open(path, "rb")
    try:
        for _ev, el in ET.iterparse(f, events=("end",)):
            if el.tag != _NS + "page":
                continue
            if el.findtext(_NS + "ns") == "14" and el.find(_NS + "redirect") is None:
                title = el.findtext(_NS + "title") or ""
                rev = el.find(_NS + "revision")
                txt = rev.findtext(_NS + "text") if rev is not None else ""
                yield _title(title.split(":", 1)[1] if ":" in title else title), txt or ""
            el.clear()
    except (EOFError, ET.ParseError) as e:
        log(f"stopped at end of readable data ({type(e).__name__}) — expected for a partial file")


def run_tree(path: str, outdir: str, limit: int | None) -> None:
    # XML parsing is the whole cost and is one stream, so no worker pool here.
    _low_priority()
    os.makedirs(outdir, exist_ok=True)
    t0 = time.time()
    n = 0
    edges: list[tuple[str, str]] = []
    hidden: set[str] = set()
    for child, text in _category_pages(path):
        n += 1
        if limit and n > limit:
            break
        text = _NOWIKI.sub("", _COMMENT.sub("", text))
        if _HIDDENCAT.search(text):
            hidden.add(child)
            continue
        if not child or _JUNK_CAT.search(child) or _JUNK_TREE.search(child):
            continue
        for p in _cats(text):
            if p != child and not _JUNK_TREE.search(p):
                edges.append((child, p))
        if n % 250_000 == 0:
            log(f"{n:,} category pages, {len(edges):,} edges, {len(hidden):,} hidden, "
                f"{n / (time.time() - t0):.0f}/s")
    # A parent can be a hidden category that appears later in the dump: filter at the end.
    tmp = os.path.join(outdir, "wiki-cattree.tsv.gz.part")
    rows = 0
    children: set[str] = set()
    with gzip.open(tmp, "wt", encoding="utf-8", compresslevel=5) as f:
        for c, p in edges:
            if p in hidden:
                continue
            f.write(f"{_clean(c)}\t{_clean(p)}\n")
            rows += 1
            children.add(c)
    os.replace(tmp, os.path.join(outdir, "wiki-cattree.tsv.gz"))
    secs = time.time() - t0
    with open(os.path.join(outdir, "wiki-cattree.DONE"), "w") as f:
        f.write(f"category_pages={n} hidden={len(hidden)} rows={rows} children={len(children)} "
                f"seconds={secs:.0f} dump={os.path.basename(path)}\n")
    log(f"{n:,} category pages ({len(hidden):,} hidden) -> {rows:,} edges from {len(children):,} "
        f"categories in {secs / 60:.1f} min -> {outdir}")


def run(path: str, outdir: str, procs: int, limit: int | None) -> None:
    _low_priority()
    os.makedirs(outdir, exist_ok=True)
    t0 = time.time()
    arts = cat_rows = lists = link_rows = 0
    cats_tmp = os.path.join(outdir, "wiki-cats.tsv.gz.part")
    links_tmp = os.path.join(outdir, "wiki-listlinks.tsv.gz.part")
    cf = gzip.open(cats_tmp, "wt", encoding="utf-8", compresslevel=5)
    lf = gzip.open(links_tmp, "wt", encoding="utf-8", compresslevel=5)

    def articles():
        n = 0
        for kind, _pid, _rid, title, text in _pages(path):
            if kind != "article":
                continue
            n += 1
            if limit and n > limit:
                return
            yield (title, text)

    with Pool(procs, initializer=_low_priority) as pool:
        for title, cats, links in pool.imap(parse_page, articles(), chunksize=256):
            arts += 1
            t = _clean(title)
            for c in cats:
                cf.write(f"{t}\t{_clean(c)}\n")
            cat_rows += len(cats)
            if links is not None:
                lists += 1
                for i, lk in enumerate(links, 1):
                    lf.write(f"{t}\t{_clean(lk)}\t{i}\n")
                link_rows += len(links)
            if arts % 250_000 == 0:
                log(f"{arts:,} articles, {cat_rows:,} category rows, {lists:,} lists / "
                    f"{link_rows:,} links, {arts / (time.time() - t0):.0f}/s")
    cf.close()
    lf.close()
    os.replace(cats_tmp, os.path.join(outdir, "wiki-cats.tsv.gz"))
    os.replace(links_tmp, os.path.join(outdir, "wiki-listlinks.tsv.gz"))
    secs = time.time() - t0
    with open(os.path.join(outdir, "wiki-cats.DONE"), "w") as f:
        f.write(f"articles={arts} category_rows={cat_rows} lists={lists} link_rows={link_rows} "
                f"seconds={secs:.0f} dump={os.path.basename(path)}\n")
    log(f"{arts:,} articles -> {cat_rows:,} category rows; {lists:,} list pages -> {link_rows:,} links "
        f"in {secs / 60:.1f} min -> {outdir}")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Wikipedia categories + list-page links -> TSV files.")
    ap.add_argument("dump_file")
    ap.add_argument("outdir")
    ap.add_argument("--procs", type=int, default=4)
    ap.add_argument("--limit", type=int, help="stop after this many articles (a test run)")
    ap.add_argument("--tree", action="store_true",
                    help="category pages -> wiki-cattree.tsv.gz (child \\t parent) instead")
    a = ap.parse_args()
    if a.tree:
        run_tree(a.dump_file, a.outdir, a.limit)
    else:
        run(a.dump_file, a.outdir, a.procs, a.limit)
