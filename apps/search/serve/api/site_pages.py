"""The pages of a site, from the site's own sitemap (2026-11-10).

A search that is a site's name plus more words ("nike air max 90", "sony wh-1000xm5", "kia sportage features") is
answered from a table of that site's page addresses and titles, harvested once from its sitemap
(tools/sitemap-harvest.py, tools/sitemap-keep.py): SEARCH_SITE_PAGES=<site-pages.sqlite>. Unset = off, and this module is
never imported. Nothing is fetched while searching and nothing about a search leaves the engine.

Table (tools/sitemap-keep.py build): site_pages(id, host_id, path, lastmod, title) with lastmod as yyyymm or 0;
site_hosts(host_id, host, pages); an FTS5 table site_pages_fts(site, words, title) with rowid = site_pages.id, where `site` is
the token "s<host_id>" and `words` the words of the address path.

What it does: look_up(q, hint_hosts) reads q as a name + leftover words (name first or last, as site_slots.splits does),
accepts the name only when it is the name of a site the engine already shows (hint_hosts = the first rows' hosts), and
returns the best few pages of that site whose address or title holds every leftover word. The caller offers them as
candidates; the rerankers decide the order. Not for a search with no site, and not for a leftover that is one slot's
phrase ("nike login": feat/intent-slots' slot row answers that from the home page's own links).
"""
from __future__ import annotations

import re
import sqlite3
import threading
import unicodedata
import urllib.parse

try:                      # feat/intent-slots: one reading of "name + words" for both, when that module is present
    import site_slots as _S
except ImportError:       # noqa: SIM105
    _S = None

_WORD = re.compile(r"[a-z0-9]+")
_SLD = frozenset(("com", "co", "net", "org", "gov", "edu", "ac", "or", "ne", "go", "gob", "nom", "mil"))
# leftover words that name nothing: "nike official site" is the site, not a page of it
_FILLER = frozenset(("official", "site", "website", "web", "online", "homepage", "home", "page", "www", "com", "the", "and", "of", "for", "in"))
MAX_NAME_WORDS, MAX_REST_WORDS = 3, 5


def words(s: str) -> list[str]:
    return _WORD.findall(unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower())


def reg_domain(host: str) -> str:
    if _S is not None:
        return _S.reg_domain(host)
    p = (host or "").lower().strip(".").split(".")
    if len(p) >= 3 and p[-2] in _SLD and len(p[-1]) == 2:
        return ".".join(p[-3:])
    return ".".join(p[-2:])


def host_of(url: str) -> str:
    try:
        h = (urllib.parse.urlsplit(url or "").hostname or "").lower()
    except ValueError:
        return ""
    return h[4:] if h.startswith("www.") else h


def splits(q: str):
    """(name_words, rest_words) for every way to read q as a name and leftover words, the name first or last, the longest
    name first. site_slots.splits when that module is present (it yields a third item, where the name sits)."""
    if _S is not None:
        for name_w, rest_w, _where in _S.splits(q, MAX_NAME_WORDS, MAX_REST_WORDS):
            yield name_w, rest_w
        return
    w = (q or "").split()
    n = len(w)
    for k in range(min(MAX_NAME_WORDS, n - 1), 0, -1):
        if n - k <= MAX_REST_WORDS:
            yield w[:k], w[k:]
            yield w[n - k:], w[:n - k]


def _variants(w: str) -> list[str]:
    out = [w]
    if len(w) > 2 and not w.endswith("s"):
        out.append(w + "s")
    elif len(w) > 3 and w.endswith("s"):
        out.append(w[:-1])
    return out


def fts_query(site_tokens: list[str], rest: list[str]) -> str:
    """site:(s1 OR s2) AND each leftover word (or its plural / singular) in the address words or the title."""
    sites = " OR ".join(f'"{t}"' for t in site_tokens)
    parts = []
    for w in rest:
        parts.append('{words title} : (' + " OR ".join(f'"{v}"' for v in _variants(w)) + ")")
    return f"site : ({sites}) AND " + " AND ".join(parts)


class SitePages:
    """The table, read-only. Hosts are loaded once (about 13,000); a lookup is one FTS query on a thread's own connection."""

    def __init__(self, path: str):
        self.path = path
        self._tl = threading.local()
        db = self._db()
        self.by_reg: dict[str, list[tuple[str, int, int]]] = {}
        for host, n, pages in db.execute("SELECT host, host_id, pages FROM site_hosts"):
            self.by_reg.setdefault(reg_domain(host), []).append((host, n, pages))
        self.rows = db.execute("SELECT count(*) FROM site_pages").fetchone()[0]

    def _db(self) -> sqlite3.Connection:
        c = getattr(self._tl, "c", None)
        if c is None:
            c = sqlite3.connect(f"file:{self.path}?mode=ro&immutable=1", uri=True)
            self._tl.c = c
        return c

    def __len__(self) -> int:
        return len(self.by_reg)

    def site_for(self, q: str, hint_hosts: list[str]):
        """(registered domain, [(host, n, pages)...], rest words) when q is the name of a site in the table that the engine
        already shows (hint_hosts), else None. The longest name wins; a name of one letter never does."""
        hints = []
        for h in hint_hosts:
            rd = reg_domain(host_of(h) or h)
            if rd and rd not in hints:
                hints.append(rd)
        for name_w, rest_w in splits(q):
            nk = "".join(words(" ".join(name_w)))
            rest = [w for x in rest_w for w in words(x)]
            if len(nk) < 2 or not [w for w in rest if w not in _FILLER and (len(w) > 1 or w.isdigit())]:
                continue
            for rd in hints:
                if "".join(words(rd.split(".")[0])) == nk and rd in self.by_reg:
                    if _S is not None and _S.slot_of_query_words(rest):
                        return None                      # a slot's phrase ("login", "returns"): the slot row's job
                    return rd, self.by_reg[rd], [w for w in rest if w not in _FILLER]
        return None

    def look_up(self, q: str, hint_hosts: list[str], limit: int = 3) -> list[dict]:
        """[{url, title, lastmod, host}] best first, at most `limit`; [] when q is not a site's name + words or nothing
        on the site holds every leftover word."""
        found = self.site_for(q, hint_hosts)
        if not found:
            return []
        rd, hosts, rest = found
        hosts = sorted(hosts, key=lambda h: (h[0] != rd, -h[2]))[:3]
        sql = ("SELECT h.host, p.path, p.title, p.lastmod FROM site_pages_fts f JOIN site_pages p ON p.id = f.rowid "
               "JOIN site_hosts h ON h.host_id = p.host_id WHERE site_pages_fts MATCH ? ORDER BY bm25(site_pages_fts, 0.0, 1.0, 2.0), length(p.path), p.id LIMIT ?")
        try:
            got = self._db().execute(sql, (fts_query([f"s{n}" for _h, n, _p in hosts], rest), limit)).fetchall()
        except sqlite3.Error:
            return []                                    # a lookup that failed says nothing: never break a search
        out = []
        for host, path, title, lastmod in got:
            out.append({"url": f"https://{host}{path}", "title": title or "", "lastmod": str(lastmod or ""), "host": host})
        return out


def row_title(r: dict) -> str:
    """The title shown: the sitemap's own when it has one, else the address words, capitalised, under the host."""
    if r.get("title"):
        return r["title"]
    path = urllib.parse.unquote(urllib.parse.urlsplit(r["url"]).path)
    w = [x for x in re.split(r"[-_/+.\s]+", path) if x and not x.isdigit()]
    return (" ".join(w).strip().title() or r["host"]) + " — " + r["host"]
