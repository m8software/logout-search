"""What KIND of thing an article's subject is, from its infobox.

"password manager" or "video hosting service" should find sites and apps of that kind.
Wikipedia's infoboxes say so directly: {{Infobox website | type = Video hosting service}},
{{Infobox software | genre = Password manager}}, {{Infobox company | industry = … |
services = … | products = …}}. One streaming pass over the same dump as wiki_docs
(regex + a brace scanner over the raw wikitext, no full parse) writes:

  kinds-extra/wikiinfobox-raw.tsv.gz   article_title \t infobox_name \t field \t value

one row per list item. Only the first infobox whose name is about websites, software,
apps, online services or companies counts (see _BOX). Fields: type, genre, industry,
services, products, category, and platform for software boxes.

Values are cleaned: refs, comments and HTML dropped; [[link|label]] -> label;
{{ubl|a|b}} / {{hlist}} / {{plainlist}} / <br> / bullets split into items. Commas and
semicolons split only inside those lists (or between wikilinks) so "Video hosting
service" stays one item. Items outside 3..80 chars are dropped; duplicates are dropped
per (article, field), case-insensitively.

Titles use the dump's form — the same as wiki_docs.title and wikicats.py.

Run on indexhost from /mnt/nas/logout/search (the image has wikiparse.py at /app):
  sudo -n docker run -d --name wikiinfobox --user 1000:1000 --memory 6g --cpus 4 \\
    --entrypoint python -v $PWD/wikiinfobox-job/wikiinfobox.py:/app/wikiinfobox.py:ro \\
    -v $PWD/wikipedia:/wikipedia:ro -v $PWD/import:/out logout/search-importer:1 \\
    -u /app/wikiinfobox.py /wikipedia/enwiki-20260901-pages-articles-multistream.xml.bz2 \\
    /out/kinds-extra --procs 4
"""

from __future__ import annotations

import gzip
import html
import os
import re
import time
from multiprocessing import Pool

from wikiparse import _low_priority, _pages, log

FIELDS = frozenset(("type", "genre", "industry", "services", "products", "category", "platform"))
_START = re.compile(r"\{\{\s*infobox[ _]", re.I)
_BOX = re.compile(r"\b(website|software|video game|company|online service|web browser|browser|"
                  r"programming language|operating system|search engine|social network(ing)?( service)?)\b")
_COMMENT = re.compile(r"<!--.*?(?:-->|$)", re.S)
_NOWIKI = re.compile(r"<nowiki>.*?</nowiki>", re.S | re.I)
_REF = re.compile(r"<ref[^>/]*/>|<ref[^>]*>.*?</ref>|<ref[^>]*>", re.S | re.I)
_BR = re.compile(r"<\s*br\s*/?\s*>|<\s*/\s*br\s*>", re.I)
_TAG = re.compile(r"</?[a-z][^>]*>", re.I)
_LINK = re.compile(r"\[\[([^\[\]|]*)(?:\|([^\[\]]*))?\]\]")
_EXT = re.compile(r"\[(?:https?:)?//[^\s\]]+\s*([^\]]*)\]")
_QUOTES = re.compile(r"'{2,}")
_WS = re.compile(r"\s+")
_HEADING = re.compile(r"^==[^=\n].*==\s*$", re.M)
_PAREN = re.compile(r"^\(.*\)$")   # "(Full list)", "(more)"
_TPL = re.compile(r"\{\{([^{}]*)\}\}")
SEP = "\x1f"
# Templates whose arguments are the list items.
_LIST_TPL = frozenset(("ubl", "ubil", "unbulleted list", "unbulleted indent list", "plainlist", "plain list",
                       "flatlist", "flat list", "hlist", "bulleted list", "bulletedlist", "blist", "bull list",
                       "collapsible list", "cslist", "comma separated list", "enum", "ordered list", "unordered list",
                       "startflatlist", "startplainlist", "endflatlist", "endplainlist", "nowrap begin", "nowrap end"))
# Templates that just wrap their first positional argument.
_WRAP_TPL = frozenset(("nowrap", "nobr", "small", "smaller", "big", "lang", "nobold", "noitalic", "abbr",
                       "not a typo", "sic", "vanchor", "anchor", "longitem", "keypress", "tooltip", "em",
                       "strong", "title case", "lc", "uc", "lcfirst", "ucfirst", "resize", "font", "code",
                       "wikt-lang", "transl", "ill", "interlanguage link", "ill-wd", "linktext"))


def _match_close(text: str, i: int) -> int:
    """Index just past the }} that closes the {{ at i, or -1."""
    depth, n = 0, len(text)
    while i < n:
        j = text.find("{{", i)
        k = text.find("}}", i)
        if k < 0:
            return -1
        if 0 <= j < k:
            depth += 1
            i = j + 2
        else:
            depth -= 1
            i = k + 2
            if depth == 0:
                return i
    return -1


def _split_top(body: str) -> list[str]:
    """Split on | outside {{…}} and [[…]]."""
    parts, depth_t, depth_l, start, i, n = [], 0, 0, 0, 0, len(body)
    while i < n:
        two = body[i:i + 2]
        if two == "{{":
            depth_t += 1
            i += 2
        elif two == "}}":
            depth_t -= 1
            i += 2
        elif two == "[[":
            depth_l += 1
            i += 2
        elif two == "]]":
            depth_l -= 1
            i += 2
        elif body[i] == "|" and depth_t <= 0 and depth_l <= 0:
            parts.append(body[start:i])
            start = i + 1
            i += 1
        else:
            i += 1
    parts.append(body[start:])
    return parts


def _find_box(text: str) -> tuple[str, str] | None:
    """(infobox name, body without braces) of the first matching infobox in the lead.

    A matching box after the first section heading describes something else — FIFA's
    FIFA+ streaming box — not the article's subject.
    """
    h = _HEADING.search(text)
    lead_end = h.start() if h else len(text)
    for m in _START.finditer(text, 0, lead_end):
        s = m.start()
        e = _match_close(text, s)
        if e < 0:
            return None
        body = text[s + 2:e - 2]
        name = _WS.sub(" ", body.split("|", 1)[0].replace("_", " ")).strip().lower()
        if _BOX.search(name[len("infobox"):]):
            return name, body
    return None


def _expand(m: re.Match) -> str:
    """One innermost template -> its text (list templates -> SEP-joined items)."""
    args = m.group(1).split("|")
    name = args[0].strip().lower().replace("_", " ")
    pos = [a for a in args[1:] if not re.match(r"^\s*[\w -]+\s*=", a)]
    if name in _LIST_TPL:
        return SEP + SEP.join(pos) + SEP
    if name in _WRAP_TPL:
        if name in ("lang", "wikt-lang", "transl") and len(pos) >= 2:
            return pos[1]
        return pos[0] if pos else ""
    return ""   # citations, dates, URLs, flags, icons — not kinds


def _links(v: str) -> str:
    def rep(m: re.Match) -> str:
        target, label = m.group(1), m.group(2)
        if re.match(r"^\s*:?\s*(file|image|category|media)\s*:", target, re.I):
            return ""
        return (label if label is not None and label.strip() else target.split("#")[0]).strip()
    return _LINK.sub(rep, v)


def clean_value(raw: str) -> list[str]:
    v = _REF.sub("", _NOWIKI.sub("", _COMMENT.sub("", raw)))
    # A plain comma list of links ("[[Internet]], [[Software]]") is a list, too.
    linklist = bool(re.fullmatch(r"\s*\[\[[^\[\]]+\]\](\s*[,;]\s*(and\s+)?\[\[[^\[\]]+\]\])+\s*", v))
    v = _links(v)           # before templates: a [[a|b]] inside {{ubl|…}} would split on its |
    for _ in range(4):      # innermost templates first; nesting in infobox values is shallow
        v2 = _TPL.sub(_expand, v)
        if v2 == v:
            break
        v = v2
    v = v.replace("{{", "").replace("}}", "")
    v = _EXT.sub(lambda m: m.group(1), v)
    has_list = SEP in v or bool(_BR.search(v)) or bool(re.search(r"(^|\n)\s*[*#]", v)) or linklist
    v = _BR.sub(SEP, v)
    v = re.sub(r"(^|\n)\s*[*#:]+", SEP, v)
    v = v.replace("\n", SEP if has_list else " ")
    v = _TAG.sub("", _QUOTES.sub("", v))
    v = html.unescape(v).replace(" ", " ")
    if has_list:
        v = re.sub(r"[,;]", SEP, v)
    out, seen = [], set()
    for it in v.split(SEP):
        it = _WS.sub(" ", it).strip(" \t.,;:-–—•·/")
        it = re.sub(r"^(and|or)\s+", "", it, flags=re.I)
        k = it.lower()
        if 3 <= len(it) <= 80 and k not in seen and not _PAREN.match(it):
            seen.add(k)
            out.append(it)
    return out


def parse_page(item: tuple[str, str]) -> tuple[str, str, list[tuple[str, str]]] | None:
    """(title, wikitext) -> (title, infobox name, [(field, value)]) or None."""
    title, text = item
    if not _START.search(text):
        return None
    box = _find_box(text)
    if box is None:
        return None
    name, body = box
    software = "software" in name
    rows = []
    for p in _split_top(body)[1:]:
        if "=" not in p:
            continue
        k, v = p.split("=", 1)
        k = k.strip().lower()
        if k not in FIELDS or (k == "platform" and not software):
            continue
        for item_ in clean_value(v):
            rows.append((k, item_))
    return title, name, rows


def _clean(s: str) -> str:
    return s.replace("\t", " ").replace("\n", " ").replace("\r", " ")


def run(path: str, outdir: str, procs: int, limit: int | None) -> None:
    _low_priority()
    os.makedirs(outdir, exist_ok=True)
    t0 = time.time()
    arts = boxes = covered = rows = 0
    tmp = os.path.join(outdir, "wikiinfobox-raw.tsv.gz.part")
    f = gzip.open(tmp, "wt", encoding="utf-8", compresslevel=5)

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
        for res in pool.imap(parse_page, articles(), chunksize=256):
            arts += 1
            if res is not None:
                title, name, items = res
                boxes += 1
                if items:
                    covered += 1
                t, nm = _clean(title), _clean(name)
                for k, v in items:
                    f.write(f"{t}\t{nm}\t{k}\t{_clean(v)}\n")
                rows += len(items)
            if arts % 250_000 == 0:
                log(f"{arts:,} articles, {boxes:,} matching infoboxes, {covered:,} with values, "
                    f"{rows:,} rows, {arts / (time.time() - t0):.0f}/s")
    f.close()
    os.replace(tmp, os.path.join(outdir, "wikiinfobox-raw.tsv.gz"))
    secs = time.time() - t0
    with open(os.path.join(outdir, "wikiinfobox.DONE"), "w") as df:
        df.write(f"articles={arts} infoboxes={boxes} covered={covered} rows={rows} "
                 f"seconds={secs:.0f} dump={os.path.basename(path)}\n")
    log(f"{arts:,} articles -> {boxes:,} matching infoboxes, {covered:,} with values, {rows:,} rows "
        f"in {secs / 60:.1f} min -> {outdir}")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Wikipedia infobox type/genre/industry fields -> TSV.")
    ap.add_argument("dump_file")
    ap.add_argument("outdir")
    ap.add_argument("--procs", type=int, default=4)
    ap.add_argument("--limit", type=int, help="stop after this many articles (a test run)")
    a = ap.parse_args()
    run(a.dump_file, a.outdir, a.procs, a.limit)
