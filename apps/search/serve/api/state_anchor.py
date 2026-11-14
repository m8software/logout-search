"""What a state's own site calls its pages, as a way to find them.

The NJ gate's misses were mostly pages the engine never put among its candidates: "nj state parks",
"nj unemployment", "nj property tax relief". Each of those pages is linked from the rest of its own site under exactly
that name ("NJ State Parks", "Unemployment", "Property Tax Relief"). How many of the site's pages link to it was tried
first and did not separate them (2026-11-03: menus give every listed page the same count); the link's words do.

    state_anchor.py build LINKS.tsv PAGES.tsv OUT.sqlite   importer/statecrawl.py's links.tsv (from, to, text) and
                                                           pages.tsv: one row per (phrase, page) for every page the
                                                           crawl fetched, with how many other pages of the site link
                                                           to it under that phrase.

A phrase is matched by its words, not its letters: lower case, the state's name and a few stop words out, a plural's
final s off, order ignored ("Parks, State" = "state parks"). The engine asks `best` for the query's words; this module
is the one place that turns text into a key, for the build and the engine alike.
"""
from __future__ import annotations

import os
import re
import sqlite3
import sys
from urllib.parse import urlsplit

_TOK = re.compile(r"[a-z0-9]+")
STOP = frozenset("nj new jersey the of and a an to for in on my your s".split())
# a link that names no page: navigation chrome, not what the site calls the page
GENERIC = frozenset({"home", "here", "click here", "read more", "learn more", "more", "skip to content",
                     "skip to main content", "back", "next", "previous", "top", "back to top", "menu", "search",
                     "contact", "contact us", "about", "about us"})


def _stem(t: str) -> str:
    return t[:-1] if len(t) > 3 and t.endswith("s") and not t.endswith("ss") else t


def key_words(text: str) -> frozenset:
    return frozenset(_stem(t) for t in _TOK.findall((text or "").lower()) if t not in STOP)


def key_of(words) -> str:
    return " ".join(sorted(words))


def host_path(url: str) -> str:
    s = urlsplit(url)
    return (s.hostname or "").lower().removeprefix("www.") + re.sub(r"/index\.\w+$", "", s.path).rstrip("/")


def _unesc(v: str) -> str:
    return "" if v == r"\N" else v.replace("\\\\", "\\")


def build(links: str, pages: str, out: str) -> int:
    page = {}                                     # host_path -> (url, title) of a page the crawl fetched whole
    with open(pages, encoding="utf-8", errors="replace") as f:
        for line in f:
            c = line.rstrip("\n").split("\t")
            if len(c) < 10 or c[3] != "200" or c[9] != r"\N":
                continue
            page.setdefault(host_path(_unesc(c[2])), (_unesc(c[2]), _unesc(c[4])))
    src: dict[tuple[str, str], set] = {}
    phrase: dict[tuple[str, str], str] = {}
    with open(links, encoding="utf-8", errors="replace") as f:
        for line in f:
            c = line.rstrip("\n").split("\t")
            if len(c) < 3:
                continue
            text = " ".join(_unesc(c[2]).lower().split())
            if not text or text in GENERIC or len(text) > 80:
                continue
            a, b = host_path(_unesc(c[0])), host_path(_unesc(c[1]))
            if a == b or b not in page:
                continue
            k = key_of(key_words(text))
            if not k:
                continue
            src.setdefault((k, b), set()).add(a)
            phrase.setdefault((k, b), text)
    tmp = out + ".partial"
    if os.path.exists(tmp):
        os.remove(tmp)
    db = sqlite3.connect(tmp)
    db.execute("CREATE TABLE a (key TEXT NOT NULL, hp TEXT NOT NULL, url TEXT NOT NULL, title TEXT, phrase TEXT, "
               "sources INTEGER NOT NULL)")
    db.executemany("INSERT INTO a VALUES (?,?,?,?,?,?)",
                   ((k, b, page[b][0], page[b][1], phrase[(k, b)], len(s)) for (k, b), s in src.items()))
    db.execute("CREATE INDEX a_key ON a (key)")
    db.execute("CREATE INDEX a_hp ON a (hp)")
    db.commit()
    n = db.execute("SELECT count(*), count(DISTINCT hp) FROM a").fetchone()
    db.close()
    os.replace(tmp, out)
    print(f"state anchors: {n[0]:,} (phrase, page) rows for {n[1]:,} pages from {len(page):,} fetched pages",
          file=sys.stderr)
    return 0


def _jaccard(q: frozenset, k: frozenset) -> float:
    return len(q & k) / len(q | k) if q and k else 0.0


def best(db: sqlite3.Connection, text: str) -> tuple[float, int, str, str, str] | None:
    """(jaccard, sources, url, title, phrase) of the page whose site names it closest to `text`: a phrase with the same
    words, or (three words or more) all but one of them. None when nothing matches."""
    q = key_words(text)
    if not q:
        return None
    keys = {key_of(q)}
    if len(q) >= 3:
        keys |= {key_of(q - {w}) for w in q}
    rows = db.execute(f"SELECT key, sources, url, title, phrase, hp FROM a WHERE key IN ({','.join('?' * len(keys))})",
                      sorted(keys)).fetchall()
    per: dict[str, list] = {}
    for k, n, url, title, ph, hp in rows:
        j = _jaccard(q, frozenset(k.split()))
        cur = per.get(hp)
        if cur is None or j > cur[0]:
            per[hp] = [j, n, url, title, ph]
        elif j == cur[0]:
            cur[1] += n
    if not per:
        return None
    return tuple(max(per.values(), key=lambda r: (r[0], r[1], -len(r[2]))))


def match_of(db: sqlite3.Connection, text: str, urls: list[str]) -> tuple[float, int]:
    """How closely the site names any of `urls` the way `text` asks: (jaccard, sources) of their best phrase."""
    q = key_words(text)
    hps = [host_path(u) for u in urls]
    if not q or not hps:
        return 0.0, 0
    rows = db.execute(f"SELECT key, sources FROM a WHERE hp IN ({','.join('?' * len(hps))})", hps).fetchall()
    return max(((_jaccard(q, frozenset(k.split())), n) for k, n in rows), default=(0.0, 0))


if __name__ == "__main__":
    if len(sys.argv) == 5 and sys.argv[1] == "build":
        sys.exit(build(*sys.argv[2:]))
    sys.exit(__doc__)
