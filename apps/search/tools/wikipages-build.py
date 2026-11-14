#!/usr/bin/env python3
"""wikipages-build.py PAGES.sqlite OUT.sqlite [--min-articles N] [--need title|sentence|either|both] [--top-per-site N]
     [--tmp DIR] [--detail full|column|none]

The shippable cut of the cited-pages table (2026-11-10): one row per page,
    p(id, h, url, domain, title, sent, art, n)  url without its scheme (h = 1 when it is http://); art = the Wikipedia
                                                article the sentence is from (the credit line)
    fts  FTS5 (title, sent) over p, content='p'  (porter, unicode61, diacritics folded)
Per page the sentence is picked from the stored contexts by pick_sentence() (below), not by length.
Rows are merged by lower-cased host + path (http/https and case variants of one address); n = the largest article count.

Run niced, 2 CPUs / 4 GB, TMPDIR on the NAS. Rows are staged in OUT.stage.sqlite, then cut with SQL and copied in order.
"""
import argparse
import json
import os
import re
import sqlite3
import sys
import time

TITLE_MAX = 120
SENT_MIN, SENT_MAX = 60, 220
_W = lambda s: set(re.findall(r"[a-z0-9]{3,}", s.lower()))
# a sentence that leans on the one before it, or a list item / caption written as a fragment
_CONNECT = re.compile(r"^(and|but|or|nor|which|who|whom|whose|that|however|thus|then|also|so|consequently|therefore|"
                      r"moreover|furthermore|meanwhile|instead|still|yet|nevertheless|likewise|similarly|additionally|"
                      r"this|these|those|such|it|they|he|she|his|her|their)\b", re.I)
_LISTISH = re.compile(r"^\W*(?:\d{4}(?:\s*[–-]\s*(?:\d{2,4}|present))?|\d{1,2}\s+\w+\s+\d{4})\s*[:–—-]")
UPSERT = ("INSERT INTO s VALUES (?,?,?,?,?,?,?) ON CONFLICT(k) DO UPDATE SET n = max(n, excluded.n), "
          "title = CASE WHEN s.title = '' THEN excluded.title ELSE s.title END, "
          "art = CASE WHEN s.sent = '' THEN excluded.art ELSE s.art END, "
          "sent = CASE WHEN s.sent = '' THEN excluded.sent ELSE s.sent END")
_URL_SCHEME = re.compile(r"^https?://", re.I)


def complete(s: str) -> bool:
    """A whole sentence a reader can follow alone: 60-220 characters, starts with a capital (or a digit), ends with a
    full stop, no leaning-on connective, not a dated list item, no cut-off tail."""
    s = s.strip()
    if not (SENT_MIN <= len(s) <= SENT_MAX):
        return False
    if not (s[0].isupper() or s[0].isdigit() or s[0] in '"“'):
        return False
    if s[-1] not in '.!?"”':
        return False
    if _CONNECT.match(s) or _LISTISH.match(s):
        return False
    if any(c in s for c in '"“”'):             # a quotation is somebody else's words, not Wikipedia's (licence note)
        return False
    return True


def pick_sentence(title: str, contexts: list) -> tuple[str, str]:
    """contexts = [[article, section, sentence], ...]. Among complete sentences: most words shared between the page's
    cite title and the citing article's title or the sentence; then the sentence most like the others (the common
    wording); then the shorter. ("", "") when none is complete; else (sentence, title of the citing article)."""
    cs = [c for c in contexts if complete(c[2])]
    if not cs:
        return "", ""
    t = _W(title)
    ws = {id(c): _W(c[2]) for c in contexts}

    def centrality(c):
        w = ws[id(c)]
        return sum(len(w & x) / max(1, len(w | x)) for k, x in ws.items() if k != id(c))
    best = max(cs, key=lambda c: (len(t & _W(c[0])) + len(t & ws[id(c)]), round(centrality(c), 2), -len(c[2])))
    return re.sub(r"\s+", " ", best[2]).strip(), best[0]


def key_of(url: str):
    u = _URL_SCHEME.sub("", url)
    host, sep, rest = u.partition("/")
    host = host.rsplit("@", 1)[-1].lower()
    return 1 if url[:5].lower() == "http:" else 0, host + sep + rest


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src"); ap.add_argument("dest")
    ap.add_argument("--min-articles", type=int, default=2)
    ap.add_argument("--need", choices=("title", "sentence", "either", "both"), default="either")
    ap.add_argument("--top-per-site", type=int, default=0)
    ap.add_argument("--tmp")
    ap.add_argument("--detail", choices=("full", "column", "none"), default="full")
    a = ap.parse_args()
    if a.tmp:
        os.environ["SQLITE_TMPDIR"] = a.tmp
    t0 = time.time()
    stage_path = a.dest + ".stage"
    for p in (stage_path, a.dest):
        if os.path.exists(p):
            os.remove(p)
    st = sqlite3.connect(stage_path)
    st.executescript("PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF; PRAGMA temp_store=FILE;"
                     "CREATE TABLE s (k TEXT PRIMARY KEY, h INTEGER, domain TEXT, title TEXT, sent TEXT, art TEXT, n INTEGER) WITHOUT ROWID;")
    src = sqlite3.connect(f"file:{a.src}?mode=ro", uri=True)
    seen = kept = 0
    batch = []
    for url, dom, title, na, ns, ctx in src.execute(
            "SELECT url, domain, title, n_articles, n_sentences, contexts FROM pages WHERE n_articles >= ?", (a.min_articles,)):
        seen += 1
        title = re.sub(r"\s+", " ", title).strip()[:TITLE_MAX]
        sent = art = ""
        if ns:
            try:
                sent, art = pick_sentence(title, json.loads(ctx))
            except ValueError:
                sent = art = ""
        ok = {"title": bool(title), "sentence": bool(sent), "either": bool(title or sent), "both": bool(title and sent)}[a.need]
        if not ok:
            continue
        h, k = key_of(url)
        batch.append((k, h, dom, title, sent, art, na))
        kept += 1
        if len(batch) >= 100_000:
            # a duplicate key keeps the row with the larger n (and its https flag when the counts tie)
            st.executemany(UPSERT, batch)
            batch.clear()
            if seen % 1_000_000 < 100_000:
                print(f"[build] {seen:,} read, {kept:,} kept, {(time.time() - t0) / 60:.1f} min", file=sys.stderr, flush=True)
    if batch:
        st.executemany(UPSERT, batch)
    st.commit()
    nstage = st.execute("SELECT count(*) FROM s").fetchone()[0]
    print(f"[build] staged {nstage:,} rows from {seen:,} read ({kept:,} passed) in {(time.time() - t0) / 60:.1f} min",
          file=sys.stderr, flush=True)
    out = sqlite3.connect(a.dest)
    out.executescript("PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF; PRAGMA temp_store=FILE;"
                      "CREATE TABLE p (id INTEGER PRIMARY KEY, h INTEGER NOT NULL, url TEXT NOT NULL, domain TEXT NOT NULL,"
                      " title TEXT NOT NULL, sent TEXT NOT NULL, art TEXT NOT NULL, n INTEGER NOT NULL);")
    out.execute("ATTACH ? AS st", (stage_path,))
    if a.top_per_site:
        sel = ("SELECT h, k, domain, title, sent, art, n FROM (SELECT *, row_number() OVER (PARTITION BY domain ORDER BY n DESC, "
               "length(sent) DESC, k) AS r FROM st.s) WHERE r <= %d ORDER BY domain, n DESC" % a.top_per_site)
    else:
        sel = "SELECT h, k, domain, title, sent, art, n FROM st.s ORDER BY domain, n DESC"
    out.execute("INSERT INTO p (h, url, domain, title, sent, art, n) " + sel)
    out.commit()
    out.execute("DETACH st")
    n = out.execute("SELECT count(*) FROM p").fetchone()[0]
    print(f"[build] p: {n:,} rows, {(time.time() - t0) / 60:.1f} min; indexing", file=sys.stderr, flush=True)
    det = "" if a.detail == "full" else f", detail={a.detail}"
    out.executescript("CREATE VIRTUAL TABLE fts USING fts5(title, sent, content='p', content_rowid='id', "
                      f"tokenize='porter unicode61 remove_diacritics 2'{det});"
                      "INSERT INTO fts(fts, rank) VALUES('automerge', 8);")
    out.execute("INSERT INTO fts(rowid, title, sent) SELECT id, title, sent FROM p")
    out.execute("INSERT INTO fts(fts) VALUES('optimize')")
    out.commit()
    out.execute("VACUUM") if n < 3_000_000 else None
    size = os.path.getsize(a.dest)
    os.remove(stage_path)
    print(f"[build] {a.dest}: {n:,} rows, {size / 1e9:.2f} GB, {(time.time() - t0) / 60:.1f} min", file=sys.stderr, flush=True)


if __name__ == "__main__":
    main()
