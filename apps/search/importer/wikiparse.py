"""English Wikipedia dump -> searchable articles.

Streams `enwiki-*-pages-articles-multistream.xml.bz2` (never stored
uncompressed), keeps namespace-0 articles and redirects, and converts each
article's wikitext to: title, section headings, the first ~4 KB of plain text,
and infobox fields (specs, dates, makers — what spec searches need). Redirect
titles become aliases of their target, which is how "750ti"-style queries find
the right article.

Wikitext -> text is CPU-bound, so pages are parsed in a process pool while the
main process reads the XML and writes to Postgres.
"""

from __future__ import annotations

import bz2
import os
import re
import time
import xml.etree.ElementTree as ET
from multiprocessing import Pool

import mwparserfromhell

LEAD_CHARS = 4000
WIKITEXT_CAP = 20_000          # lead + infobox sit at the top; 60 KB cost 37 ms/article (7M → ~72 CPU-h)
INFOBOX_CHARS = 1500
_NS = "{http://www.mediawiki.org/xml/export-0.11/}"
_WS = re.compile(r"\s+")
_DAB = re.compile(r"\{\{\s*(disambiguation|disambig|dab|hndis|geodis|set index article|surname|given name)\b", re.I)
_DROP_SECTIONS = re.compile(r"^(references|external links|see also|further reading|notes|bibliography|sources|citations)$", re.I)


def log(msg: str) -> None:
    print(f"[wiki] {msg}", flush=True)


def _plain(node) -> str:
    return _WS.sub(" ", node.strip_code(normalize=True, collapse=True)).strip()


# Templates that carry text in the article as Wikipedia shows it. Removing them with the rest dropped that text:
# "(April 12, 1861{{snd}}May 26, 1865" read "1861May 26", "Nordstrom.{{As of|2024|lc=y}}, Nordstrom Rack" read
# "Nordstrom., Nordstrom Rack". These are rendered as text first; every other template is still removed.
# The engine renders the same set on text it reads (serve/api/card_text.py, _render_template); keep the two in step.
_DASHES = {"snd": " \u2013 ", "spnd": " \u2013 ", "sndash": " \u2013 ", "spaced ndash": " \u2013 ", "spaced en dash": " \u2013 ",
           "spaced en dash space": " \u2013 ", "ndash": "\u2013", "en dash": "\u2013", "nsndns": "\u2013", "mdash": "\u2014",
           "em dash": "\u2014", "spaced mdash": " \u2014 ", "spaced em dash": " \u2014 ", "snds": " \u2013 ", "nbsp": " ",
           "spaces": " ", "sp": " "}
_MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November",
           "December")


def _month(x: str) -> str | None:
    x = x.strip()
    if x.isdigit() and 1 <= int(x) <= 12:
        return _MONTHS[int(x) - 1]
    return next((m for m in _MONTHS if x.lower() in (m.lower(), m[:3].lower())), None) if x else None


def _date(pos: list[str], us: bool) -> str:
    y = pos[0] if pos else ""
    if not re.fullmatch(r"\d{3,4}", y):
        return ""
    mo = _month(pos[1]) if len(pos) > 1 else None
    d = pos[2] if len(pos) > 2 and pos[2].isdigit() else None
    if mo and d:
        return f"{mo} {int(d)}, {y}" if us else f"{int(d)} {mo} {y}"
    return f"{mo} {y}" if mo else y


def _template_text(t) -> str | None:
    """The text a simple template shows in the article, or None when it is one to remove."""
    name = _WS.sub(" ", str(t.name)).strip().lower()
    if name in _DASHES:
        return _DASHES[name]
    pos = [_plain(p.value) for p in t.params if not p.showkey]
    named = {str(p.name).strip().lower(): _plain(p.value) for p in t.params if p.showkey}
    if name in ("as of", "asof"):
        when = _date(pos, named.get("df", "").startswith("us") or named.get("df") == "mdy")
        if not when:
            return None
        word = "Since" if named.get("since") in ("y", "yes") else "As of"
        if named.get("lc") in ("y", "yes"):
            word = word.lower()
        return " " + (named.get("alt") or f"{word} {when}")
    if name in ("circa", "c.") and pos and pos[0]:
        return "c. " + pos[0]
    if (name == "lang" or name.startswith("lang-")) and pos:
        return pos[-1] if name == "lang" else pos[0]
    if name in ("nowrap", "nobr", "small", "big", "nobold", "abbr", "convert") and pos:
        return " ".join(pos[:2]) if name == "convert" and len(pos) >= 2 else pos[0]
    if name == "start date" and pos:
        return _date(pos, False) or None
    if name == "nihongo" and len(pos) >= 2 and pos[0]:
        extra = ", ".join(x for x in pos[1:3] if x)
        return f"{pos[0]} ({extra})" if extra else pos[0]
    return None


def parse_article(item: tuple[int, int, str, str]) -> tuple | None:
    """(page_id, rev_id, title, wikitext) -> row, or None for a disambiguation page."""
    page_id, rev_id, title, text = item
    if _DAB.search(text[:5000]):
        return None
    try:
        code = mwparserfromhell.parse(text[:WIKITEXT_CAP])
    except Exception:
        return (page_id, rev_id, title, "", "", "")
    infobox = []
    for t in code.filter_templates(recursive=False):
        name = str(t.name).strip().lower()
        if name.startswith("infobox"):
            for p in t.params:
                k = str(p.name).strip()
                v = _plain(p.value)
                if v and not k.startswith(("image", "caption", "alt", "logo", "map")):
                    infobox.append(f"{k}: {v}")
            break
    headings = []
    for h in code.filter_headings():
        ht = _plain(h.title)
        if ht and not _DROP_SECTIONS.match(ht):
            headings.append(ht)
    # Render the templates that carry text (dashes, "As of", circa, ...), then remove the rest, tables, refs and files
    # before taking the lead text.
    for t in code.filter_templates(recursive=False):
        try:
            text = _template_text(t)
            if text is None:
                code.remove(t)
            else:
                code.replace(t, text)
        except ValueError:
            pass
    for tag in code.filter_tags(recursive=False):
        if str(tag.tag).lower() in ("ref", "table", "gallery", "math", "references"):
            try:
                code.remove(tag)
            except ValueError:
                pass
    # File/Image links keep their caption text through strip_code ("thumb|Albedo change …").
    for wl in code.filter_wikilinks(recursive=False):
        if str(wl.title).strip().lower().startswith(("file:", "image:", "category:")):
            try:
                code.remove(wl)
            except ValueError:
                pass
    body = re.sub(r"([.!?] )(as of|since) ", lambda m: m.group(1) + m.group(2).capitalize() + " ", _plain(code))  # {{As of|lc=y}} after a full stop
    return (page_id, rev_id, title, " | ".join(headings)[:1000], body[:LEAD_CHARS],
            " ; ".join(infobox)[:INFOBOX_CHARS])


def _pages(path: str):
    """Yield (kind, page_id, rev_id, title, text_or_target) for namespace-0 pages.

    A truncated file (a partial download) ends with an EOFError from bz2 — every
    complete stream before it is still valid, so stop quietly there.
    """
    f = bz2.open(path, "rb")
    try:
        for ev, el in ET.iterparse(f, events=("end",)):
            if el.tag != _NS + "page":
                continue
            ns = el.findtext(_NS + "ns")
            if ns == "0":
                title = el.findtext(_NS + "title") or ""
                pid = int(el.findtext(_NS + "id") or 0)
                rev = el.find(_NS + "revision")
                rid = int(rev.findtext(_NS + "id") or 0) if rev is not None else 0
                red = el.find(_NS + "redirect")
                if red is not None:
                    yield ("redirect", pid, rid, title, red.get("title") or "")
                else:
                    txt = rev.findtext(_NS + "text") if rev is not None else ""
                    yield ("article", pid, rid, title, txt or "")
            el.clear()
    except (EOFError, ET.ParseError) as e:
        log(f"stopped at end of readable data ({type(e).__name__}) — expected for a partial file")


def _low_priority() -> None:
    # indexhost runs the radio recorder at load ~6.4/8 — this batch job must yield to it.
    # (Windows has no os.nice; buildbox's task is started at below-normal priority instead.)
    if hasattr(os, "nice"):
        os.nice(15)


def cmd_wiki(conn, path: str, dump: str, procs: int, limit: int | None) -> None:
    _low_priority()
    conn.execute("TRUNCATE wiki_articles_stage, wiki_redirects_stage")
    t0 = time.time()
    arts = reds = dabs = 0
    abatch, rbatch = [], []

    def flush():
        with conn.transaction():
            if abatch:
                with conn.cursor().copy("COPY wiki_articles_stage (page_id, rev_id, title, headings, lead, infobox) "
                                        "FROM STDIN") as cp:
                    for r in abatch:
                        cp.write_row(tuple(v.replace("\x00", "") if isinstance(v, str) else v for v in r))
            if rbatch:
                with conn.cursor().copy("COPY wiki_redirects_stage (title, target) FROM STDIN") as cp:
                    for r in rbatch:
                        cp.write_row(r)
        abatch.clear()
        rbatch.clear()

    def articles():
        nonlocal reds
        n = 0
        for kind, pid, rid, title, x in _pages(path):
            if kind == "redirect":
                reds += 1
                rbatch.append((title, x))
                continue
            n += 1
            if limit and n > limit:
                return
            yield (pid, rid, title, x)

    with Pool(procs, initializer=_low_priority) as pool:
        for row in pool.imap(parse_article, articles(), chunksize=64):
            if row is None:
                dabs += 1
                continue
            arts += 1
            abatch.append(row)
            if len(abatch) >= 2000:
                flush()
            if arts % 100_000 == 0:
                log(f"{arts:,} articles, {reds:,} redirects, {dabs:,} disambiguation skipped, "
                    f"{arts / (time.time() - t0):.0f}/s")
    flush()
    log(f"parsed {arts:,} articles, {reds:,} redirects, skipped {dabs:,} disambiguation pages "
        f"in {(time.time() - t0) / 60:.1f} min — building the searchable table")

    _build_wiki_docs(conn, dump, t0)


def _build_wiki_docs(conn, dump: str, t0: float) -> None:
    # Swap in the new table in one transaction: aliases = redirect titles grouped by target.
    with conn.transaction():
        conn.execute("DROP TABLE IF EXISTS wiki_docs_new")
        conn.execute(
            "CREATE TABLE wiki_docs_new AS "
            "SELECT a.page_id, a.rev_id, a.title, a.headings, a.lead, a.infobox, %s::text AS dump, "
            "       coalesce(r.aliases, '') AS aliases "
            "FROM wiki_articles_stage a LEFT JOIN ("
            "   SELECT target, string_agg(title, ' | ') AS aliases FROM wiki_redirects_stage GROUP BY target"
            ") r ON r.target = a.title", (dump,))
        conn.execute(
            "ALTER TABLE wiki_docs_new ADD COLUMN search_text text GENERATED ALWAYS AS ("
            " title || ' ' || aliases || ' ' || coalesce(infobox, '') || ' ' || coalesce(headings, '') || ' ' ||"
            " coalesce(lead, '')) STORED")
        conn.execute("ALTER TABLE wiki_docs_new ADD PRIMARY KEY (page_id)")
        conn.execute("DROP TABLE IF EXISTS wiki_docs")
        conn.execute("ALTER TABLE wiki_docs_new RENAME TO wiki_docs")
        conn.execute("CREATE INDEX wiki_docs_bm25 ON wiki_docs USING bm25 (search_text) WITH (text_config = 'english')")
        conn.execute("CREATE INDEX wiki_docs_title_trgm ON wiki_docs USING gin (title gin_trgm_ops)")
        # Exact title / redirect-name lookup for search-api: "louis ck" finds
        # "Louis C.K.", "corpus callosum" its article, "750ti" the redirect's
        # target. Key = lowercase letters and digits only (the API normalises the
        # query the same way).
        conn.execute("DROP TABLE IF EXISTS wiki_aliases")
        conn.execute(
            "CREATE TABLE wiki_aliases AS "
            "SELECT DISTINCT lower(regexp_replace(t, '[^[:alnum:]]', '', 'g')) AS norm, page_id "
            "FROM (SELECT page_id, title AS t FROM wiki_docs "
            "      UNION ALL SELECT page_id, unnest(string_to_array(aliases, ' | ')) FROM wiki_docs "
            "      WHERE aliases <> '') x "
            "WHERE t <> ''")
        conn.execute("CREATE INDEX wiki_aliases_norm ON wiki_aliases (norm)")
    n = conn.execute("SELECT count(*), count(*) FILTER (WHERE aliases <> '') FROM wiki_docs").fetchone()
    log(f"wiki_docs ready: {n[0]:,} articles, {n[1]:,} with aliases, total {(time.time() - t0) / 60:.1f} min")


# ---------------------------------------------------------------------------
# Split mode: parse on one machine (buildbox — Windows, 12 threads), load on another.
# The files are PostgreSQL COPY text format, gzip-compressed.
# ---------------------------------------------------------------------------
def _copy_field(v) -> str:
    if v is None:
        return "\\N"
    return (str(v).replace("\x00", "").replace("\\", "\\\\").replace("\t", "\\t")
            .replace("\n", "\\n").replace("\r", "\\r"))


def parse_to_files(path: str, outdir: str, procs: int, limit: int | None) -> None:
    import gzip
    _low_priority()
    os.makedirs(outdir, exist_ok=True)
    t0 = time.time()
    arts = reds = dabs = 0
    rfile = gzip.open(os.path.join(outdir, "redirects.tsv.gz"), "wt", encoding="utf-8", compresslevel=5)
    afile = gzip.open(os.path.join(outdir, "articles.tsv.gz"), "wt", encoding="utf-8", compresslevel=5)

    def articles():
        nonlocal reds
        n = 0
        for kind, pid, rid, title, x in _pages(path):
            if kind == "redirect":
                reds += 1
                rfile.write(_copy_field(title) + "\t" + _copy_field(x) + "\n")
                continue
            n += 1
            if limit and n > limit:
                return
            yield (pid, rid, title, x)

    with Pool(procs, initializer=_low_priority) as pool:
        for row in pool.imap(parse_article, articles(), chunksize=64):
            if row is None:
                dabs += 1
                continue
            arts += 1
            afile.write("\t".join(_copy_field(v) for v in row) + "\n")
            if arts % 100_000 == 0:
                log(f"{arts:,} articles, {reds:,} redirects, {dabs:,} disambiguation skipped, "
                    f"{arts / (time.time() - t0):.0f}/s")
    afile.close()
    rfile.close()
    with open(os.path.join(outdir, "DONE"), "w") as f:
        f.write(f"articles={arts} redirects={reds} disambiguation={dabs} seconds={time.time() - t0:.0f}\n")
    log(f"parsed {arts:,} articles, {reds:,} redirects, skipped {dabs:,} disambiguation pages "
        f"in {(time.time() - t0) / 60:.1f} min -> {outdir}")


def load_from_files(conn, articles_path: str, redirects_path: str, dump: str) -> None:
    import gzip
    t0 = time.time()
    conn.execute("TRUNCATE wiki_articles_stage, wiki_redirects_stage")
    for path, table, cols in ((articles_path, "wiki_articles_stage", "page_id, rev_id, title, headings, lead, infobox"),
                              (redirects_path, "wiki_redirects_stage", "title, target")):
        with conn.transaction(), gzip.open(path, "rb") as f:
            with conn.cursor().copy(f"COPY {table} ({cols}) FROM STDIN") as cp:
                while chunk := f.read(1 << 20):
                    cp.write(chunk)
        log(f"loaded {path} into {table}")
    _build_wiki_docs(conn, dump, t0)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Parse a Wikipedia dump into COPY files (split mode).")
    ap.add_argument("cmd", choices=["parse"])
    ap.add_argument("dump_file")
    ap.add_argument("outdir")
    ap.add_argument("--procs", type=int, default=max(1, (os.cpu_count() or 2) - 2))
    ap.add_argument("--limit", type=int)
    a = ap.parse_args()
    parse_to_files(a.dump_file, a.outdir, a.procs, a.limit)
