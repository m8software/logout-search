#!/usr/bin/env python3
"""Input preparation for the release's web Tantivy indexes: sites, pages, anchors (SEARCH-RELEASE-FORMAT
rules 1, 3, 5; the builder is `tantivy-structure build --kind sites|pages|anchors`).

    web_input.py map KIND REGISTRY      stdin  ident TAB col...   ->  stdout  doc_id TAB col...
    web_input.py rows KIND REGISTRY     sites|pages rows made from the registry alone (no crawl text)
    web_input.py anchors AGG.tsv.gz [PHRASES.sql]   per-domain anchor documents from anchors.tsv.gz

KIND and the columns every index is built from (tab separated, COPY text escapes: \\t \\n \\\\ and \\N):

    sites    ident = the host string (registry type 2 `path`)      head  body
               head = domain words (dots/dashes as spaces) + title + h1
               body = domain words + title + description + h1 + page_words(body)      (SITES_SQL)
    pages    ident = the page's url (registry type 3)               body
               body = title + description + h1 + page_words(body)                      (PAGES_SQL)
    anchors  ident = the site's host (registry type 2: an anchor document IS its site, same doc_id)  body
               body = each anchor text repeated floor(log2(sources)) times (1..8), anchors in
               alphabetical order, block-list words removed (importer/anchors.py cmd_load, ANCHORS_SQL)

`map` replaces the ident by the registry's doc_id (rule 1). It refuses an ident the registry lacks
and an ident that appears twice: the registry is built from the same documents, so either is a bug
upstream, never something to skip. The registry's type shift is read from its meta table.
"""
import gzip
import math
import os
import re
import sqlite3
import sys

TYPE = {"sites": 2, "anchors": 2, "pages": 3}
COLS = {"sites": 2, "pages": 1, "anchors": 1}


def esc(s):
    """One value as a COPY text field (what Postgres COPY ... TO STDOUT writes)."""
    if s is None:
        return "\\N"
    return s.replace("\\", "\\\\").replace("\t", "\\t").replace("\n", "\\n").replace("\r", "\\r")


def unesc(f):
    if "\\" not in f:
        return f
    out, i = [], 0
    while i < len(f):
        c = f[i]
        if c == "\\" and i + 1 < len(f):
            i += 1
            out.append({"t": "\t", "n": "\n", "r": "\r"}.get(f[i], f[i]))
        else:
            out.append(c)
        i += 1
    return "".join(out)


def registry_rows(path, typ):
    """(doc_id, ident, title) of one registry type, in doc_id order. The ident is the registry's
    own: sites `path`; pages hosts.origin + path, or `alt` for a url with no scheme://host form."""
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    shift = int(dict(db.execute("SELECT key, value FROM meta"))["type_shift"])
    sql = ("SELECT r.doc_id, r.path, r.title FROM registry r WHERE r.doc_id >> ? = 2 ORDER BY r.doc_id" if typ == 2 else
           "SELECT r.doc_id, coalesce(h.origin || r.path, r.alt), r.title FROM registry r "
           "LEFT JOIN hosts h ON h.id = r.host_id WHERE r.doc_id >> ? = 3 ORDER BY r.doc_id")
    return shift, db.execute(sql, (shift,))


def cmd_map(kind, registry):
    typ = TYPE[kind]
    shift, rows = registry_rows(registry, typ)
    ids = {}
    for d, ident, _t in rows:
        if ident is not None:
            ids[ident] = d
    seen = set()
    out = sys.stdout.buffer
    n = 0
    for raw in sys.stdin.buffer:
        line = raw.decode("utf-8", "surrogateescape").rstrip("\n")
        ident, _, rest = line.partition("\t")
        if len(rest.split("\t")) != COLS[kind]:
            sys.exit(f"web_input map {kind}: line {n + 1} has {len(rest.split(chr(9)))} columns after the ident, want {COLS[kind]}")
        ident = unesc(ident)
        d = ids.get(ident)
        if d is None:
            sys.exit(f"web_input map {kind}: {ident!r} (line {n + 1}) is not in the registry — refusing")
        if ident in seen:
            sys.exit(f"web_input map {kind}: {ident!r} appears twice (line {n + 1}) — refusing")
        seen.add(ident)
        out.write(f"{d}\t{rest}\n".encode("utf-8", "surrogateescape"))
        n += 1
    print(f"map {kind}: {n} rows of {len(ids)} registry {kind} (type shift {shift})", file=sys.stderr)


_SPLIT = re.compile(r"[.\-_/]+")


def cmd_rows(kind, registry):
    """Rows built from what the registry holds: the host or url words and the crawled title.
    A stand-in for the crawl export when only a registry is at hand (no description, h1, page words)."""
    shift, rows = registry_rows(registry, TYPE[kind])
    out = sys.stdout.buffer
    for _d, ident, title in rows:
        if ident is None:
            continue
        title = title or ""
        if kind == "sites":
            words = " ".join(w for w in (_SPLIT.sub(" ", ident), title) if w.strip())
            line = f"{esc(ident)}\t{esc(words)}\t{esc(words)}\n"
        else:
            url = re.sub(r"^[a-z][a-z0-9+.-]*://", "", ident)
            words = " ".join(w for w in (title, _SPLIT.sub(" ", url)) if w.strip())
            line = f"{esc(ident)}\t{esc(words)}\n"
        out.write(line.encode("utf-8", "surrogateescape"))


def block_re(phrases_sql):
    m = re.search(r"ARRAY\[(.*?)\], '\|'", open(phrases_sql, encoding="utf-8").read(), re.S)
    words = re.findall(r"'([^']*)'", m.group(1)) if m else []
    if len(words) < 40:
        sys.exit(f"no word list (>= 40 entries) in {phrases_sql} — refusing to build anchor text unfiltered")
    return re.compile(r"\b(" + "|".join(re.escape(w) for w in words) + r")\b", re.I)


def anchor_body(pairs):
    """[(anchor, sources)] of one domain -> the anchor document's text (importer/anchors.py cmd_load)."""
    return " ".join((a + " . ") * max(1, min(8, int(math.floor(math.log(s) / math.log(2)))))
                    for a, s in sorted(pairs) if s >= 1)


def cmd_anchors(agg, phrases=None):
    here = os.path.dirname(os.path.abspath(__file__))
    phrases = phrases or os.environ.get("PHRASES_SQL") or os.path.join(here, "..", "..", "serve", "phrases.sql")
    block = block_re(phrases)
    per = {}
    with (gzip.open(agg, "rt", encoding="utf-8", errors="replace") if agg.endswith(".gz") else open(agg, encoding="utf-8")) as f:
        for line in f:
            p = line.rstrip("\n").split("\t")
            if len(p) == 3 and p[2].isdigit():
                per.setdefault(p[0], []).append((p[1], int(p[2])))
    out = sys.stdout.buffer
    kept = 0
    for dom in sorted(per):
        body = block.sub(" ", anchor_body(per[dom]))
        if re.search(r"\w", body):
            out.write(f"{esc(dom)}\t{esc(body)}\n".encode("utf-8"))
            kept += 1
    print(f"anchors: {len(per)} domains, {kept} with text left after the block list", file=sys.stderr)


if __name__ == "__main__":
    a = sys.argv[1:]
    if len(a) >= 3 and a[0] == "map" and a[1] in TYPE:
        cmd_map(a[1], a[2])
    elif len(a) == 3 and a[0] == "rows" and a[1] in ("sites", "pages"):
        cmd_rows(a[1], a[2])
    elif len(a) in (2, 3) and a[0] == "anchors":
        cmd_anchors(*a[1:])
    else:
        sys.exit(__doc__)
