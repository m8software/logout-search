"""Products by name, maker, model number or GTIN, read from product tables (SQLite + FTS5) built offline by
apps/search/tools/product-table/ (build_cc0.py, build_odbl.py). Pure: sqlite3 and the standard library only, no psycopg,
so the engine and its tests import the very code that runs (follow-up, 2026-11-10).

A search "toyota camry", "samsung galaxy s24", "cheerios", "tylenol extra strength", "bosch dishwasher", a model number or a
GTIN is recognised as "about product P by maker M". The engine uses the answer to offer M's own site (search_api.py,
SEARCH_PRODUCTS). Nothing here is shown by itself.

Schema (both files): products(id, name, maker, maker_site, category, ids, source, url) and products_fts(name, maker, ids)
(FTS5, content=products, content_rowid=id). ids holds space-separated key:value pairs: gtin: ndc: model: wd: years: and
brand: / alias: (values may hold spaces). Columns are never renamed (the table builder's promise).

Recognition (one FTS query per table per search; 1 to 6 words only). The query's words are ANDed, so every word of the
search must sit in one row's name, maker or ids: "apple pie recipe" finds nothing because no product holds "recipe".
The hits (best bm25 first, at most LIMIT) are then judged in Python, strongest reading first:
  id             a GTIN (8-14 digits, leading zeros do not count) or a model number (holds a digit, 4+ letters and digits) of
                 the row's ids is in the search. Specific by nature.
  maker+product  the first word(s) of the row's maker (or one of its brand: names) are in the search, the rest of the search
                 is in the row's name or ids, and that rest is specific (below).
  name           no maker in the search: every word is in the row's name and covers at least 60% of it, or starts or ends
                 a name of five words or fewer.
Specific: a word that is not an English dictionary word (typo-words.txt), or holds a digit; or, for maker+product, a
dictionary word only when the maker is unambiguous (one maker among the rows read). A bare dictionary word never recognises
a product by itself ("tide", "dove", "corona"). A maker with no product words ("toyota") recognises nothing.
Ambiguity: the rows of the reading are grouped by maker; a second maker with at least half the rows of the first leaves the
search unrecognised ("apple pie", ten makers).
"""
from __future__ import annotations

import os
import re
import sqlite3
import unicodedata

LIMIT = 40
MAX_WORDS = 6
NAME_COVER = 0.6
_TOKEN = re.compile(r"[a-z0-9]+")
_MAKER_NOISE = frozenset("""inc llc ltd co corp corporation company the sales group brands brand foods food usa us of and
international industries holdings plc gmbh sa ag lp limited enterprises products distribution manufacturing
laboratories labs pharmaceuticals pharma""".split())
_PAIR = re.compile(r"(\w+):((?:(?!\s\w+:).)*)")
CREDIT = {"wd": "Wikidata (CC0)", "fdc": "USDA FoodData Central (public domain)", "ndc": "FDA NDC Directory (public domain)",
          "epa": "EPA fueleconomy.gov (public domain)", "es": "ENERGY STAR (public domain)",
          "off": "Open Food Facts contributors (ODbL 1.0)"}


def plain(s: str) -> str:
    return unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()


def tokens(s: str) -> list[str]:
    return _TOKEN.findall(plain(s))


def licence_of(path: str) -> str:
    """The licence of a table file, from its name: products-odbl*.sqlite is ODbL 1.0, anything else CC0 1.0."""
    return "ODbL-1.0" if "odbl" in os.path.basename(path).lower() else "CC0-1.0"


def parse_ids(ids: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for k, v in _PAIR.findall(ids or ""):
        out.setdefault(k, []).append(v.strip())
    return out


def _gtin_forms(w: str) -> list[str]:
    d = w.lstrip("0")
    return [("0" * z) + d for z in range(0, 14 - len(d) + 1)] if w.isdigit() and 8 <= len(d) <= 14 else []


def fts_query(words: list[str]) -> str:
    """Every word quoted and ANDed; a GTIN-like word also as its zero-padded forms (ids hold 12/13/14-digit forms)."""
    parts = []
    for w in words:
        w = w.replace('"', "")
        forms = _gtin_forms(w)
        parts.append("(" + " OR ".join(f'"{f}"' for f in forms) + ")" if forms else f'"{w}"')
    return " AND ".join(parts)


def _norm_gtin(w: str) -> str:
    return w.lstrip("0")


class Row:
    __slots__ = ("id", "name", "maker", "maker_site", "category", "ids", "source", "url", "table", "name_t", "maker_core",
                 "aliases", "idv", "bm25")

    def __init__(self, r, table: int):
        self.id, self.name, self.maker, self.maker_site, self.category, self.ids, self.source, self.url = r[:8]
        self.bm25 = r[8] if len(r) > 8 else 0.0
        self.table = table
        self.name_t = tokens(self.name)
        self.idv = parse_ids(self.ids)
        core = lambda s: [t for t in tokens(s) if t not in _MAKER_NOISE] or tokens(s)   # noqa: E731
        self.maker_core = core(self.maker)
        self.aliases = [core(b) for b in self.idv.get("brand", []) if b]

    def maker_key(self) -> str:
        return " ".join(self.maker_core[:2])


def _maker_in(qset: set[str], core: list[str]) -> list[str]:
    """The longest prefix of the maker's core words that is in the search (the first word is enough: 'samsung' of 'samsung
    electronics'); [] when none, or when it is one short word."""
    best: list[str] = []
    for i in range(1, len(core) + 1):
        if all(t in qset for t in core[:i]):
            best = core[:i]
        else:
            break
    return best if best and len(best[0]) >= 3 else []


class Products:
    """One or more product tables, opened read-only. lookup(q) -> dict | None."""

    def __init__(self, paths: list[str], is_word=None):
        self.paths = [p for p in paths if p]
        self.dbs: list[sqlite3.Connection] = []
        self.licences: list[str] = []
        for p in self.paths:
            db = sqlite3.connect(f"file:{p}?mode=ro", uri=True, check_same_thread=False)
            db.execute("SELECT id, name, maker, maker_site, category, ids, source, url FROM products LIMIT 1").fetchall()
            self.dbs.append(db)
            self.licences.append(licence_of(p))
        self.is_word = is_word or (lambda w: False)
        self.queries = 0
        # maker key -> its site, read once from the rows that have one: a hit whose own rows have none takes the site another row with the very same maker string has. 14k makers, no per-search query.
        self.sites: dict[str, str] = {}
        for db in self.dbs:
            for maker, site, n in db.execute("SELECT maker, maker_site, count(*) FROM products WHERE maker_site != '' AND maker != '' "
                                             "GROUP BY maker, maker_site"):
                k = " ".join(tokens(maker))             # the whole maker string, never a prefix: "General Mills" is not another
                if k and (k not in self.sites or n > self.sites[k][1]):
                    self.sites[k] = (site, n)
        self.sites = {k: v[0] for k, v in self.sites.items()}

    def _rows(self, q: str) -> list[Row]:
        words = q.split()
        match = fts_query(words)
        out: list[Row] = []
        for ti, db in enumerate(self.dbs):
            self.queries += 1
            try:
                for r in db.execute(
                        "SELECT p.id, p.name, p.maker, p.maker_site, p.category, p.ids, p.source, p.url, bm25(products_fts, 10.0, 4.0, 1.0) "
                        "FROM products_fts f JOIN products p ON p.id = f.rowid WHERE products_fts MATCH ? "
                        "ORDER BY bm25(products_fts, 10.0, 4.0, 1.0) LIMIT ?", (match, LIMIT)):
                    out.append(Row(r, ti))
            except sqlite3.Error:
                continue
        return sorted(out, key=lambda r: r.bm25)

    def word(self, w: str) -> bool:
        """An English dictionary word, a plural of one included ("helicopters")."""
        return self.is_word(w) or (w.endswith("s") and self.is_word(w[:-1])) or (w.endswith("es") and self.is_word(w[:-2]))

    def specific(self, words: list[str]) -> bool:
        return any(any(c.isdigit() for c in w) or not self.word(w) for w in words)

    def lookup(self, q: str) -> dict | None:
        q = " ".join(plain(q).split())
        n = len(q.split())
        if not self.dbs or n < 1 or n > MAX_WORDS or len(q) < 3:
            return None
        qt = tokens(q)
        if not qt:
            return None
        qset = set(qt)
        rows = self._rows(q)
        if not rows:
            return None
        gt = {_norm_gtin(t) for t in qt if t.isdigit() and 8 <= len(_norm_gtin(t)) <= 14}
        reading: list[tuple[Row, str, list[str]]] = []          # (row, kind, maker words found)
        for r in rows:
            makers = [m for m in [_maker_in(qset, r.maker_core)] + [_maker_in(qset, a) for a in r.aliases] if m]
            mk = makers[0] if makers else []
            if gt and any(_norm_gtin(g) in gt for g in r.idv.get("gtin", []) + r.idv.get("ndc", [])):
                reading.append((r, "id", mk))
                continue
            if any(_model_in(qset, m) for m in r.idv.get("model", [])):
                reading.append((r, "id", mk))
                continue
            rest = [t for t in qt if t not in mk]
            known = set(r.name_t) | {t for v in r.idv.get("alias", []) for t in tokens(v)}
            if mk and rest and all(t in known for t in rest):
                reading.append((r, "maker+product", mk))
            elif not mk and qset <= set(r.name_t) and _name_reading(qt, r.name_t):
                reading.append((r, "name", []))
        if not reading:
            return None
        if any(_maker_in(qset, r.maker_core) and all(t in _maker_in(qset, r.maker_core) for t in qt) for r in rows):
            return None                                            # the search is a maker's name ("toshiba"): not a product
        kinds = [k for _r, k, _m in reading]
        if "id" in kinds:
            reading = [t for t in reading if t[1] == "id"]
            kind = "id"
        else:
            kind = "maker+product" if "maker+product" in kinds else "name"
            reading = [t for t in reading if t[1] == kind]
        groups: dict[str, list[tuple[Row, str, list[str]]]] = {}
        for t in reading:
            groups.setdefault(t[0].maker_key(), []).append(t)
        order = sorted(groups.values(), key=lambda g: -len(g))
        if kind != "id" and len(order) > 1 and len(order[1]) * 2 >= len(order[0]):
            return None                                            # two makers share the reading: unrecognised
        top = order[0]
        r0, _k, mk = top[0]
        if kind == "name":
            if len(qt) == 1 and not self.specific(qt):
                return None
        elif kind == "maker+product":
            rest = [t for t in qt if t not in mk]
            if not self.specific(rest) and len(order) > 1:         # a category word, and a maker that is not alone
                return None
            if len(mk) < len(r0.maker_core) and all(self.word(t) for t in mk):
                return None                                        # "lemon tree" is not "Tree of Life": a dictionary-word start of a longer maker name
        site = next((x[0].maker_site for x in top if x[0].maker_site), "") or self.sites.get(" ".join(tokens(r0.maker)), "")
        best = next((x[0] for x in top if x[0].maker_site), r0)
        return {"kind": kind, "maker": r0.maker, "maker_site": site or "", "name": best.name, "category": best.category,
                "source": best.source, "licence": self.licences[best.table], "credit": CREDIT.get(best.source, best.source),
                "url": best.url or "", "rows": len(reading), "table": self.paths[best.table]}


def _name_reading(qt: list[str], name_t: list[str]) -> bool:
    """The search is the product's name: it covers 60% of the name's words, or it starts or ends a name of 5 words or fewer
    ("cheerios" of "Cheerios Cereal" and "Honey Nut Cheerios")."""
    if len(set(qt)) / max(len(set(name_t)), 1) >= NAME_COVER:
        return True
    return len(name_t) <= 5 and (name_t[:len(qt)] == qt or name_t[-len(qt):] == qt)


def _model_in(qset: set[str], model: str) -> bool:
    t = tokens(model)
    flat = "".join(t)
    return bool(t) and len(flat) >= 4 and any(c.isdigit() for c in flat) and any(c.isalpha() for c in flat) and all(x in qset for x in t)


# ---------------------------------------------------------------------------------------------------------------------
# SEAM: the sitemap pages table (branch feat/sitemap-index, tools/sitemap-keep.py at 76e9b904). Its engine module does
# not exist yet; this function reads the table that tool writes (site_pages(id, host, path, lastmod, title), site_hosts(host,
# n, pages), site_pages_fts(site, words, title) with site = "s<n>"). When the branch's own lookup lands, replace the body.
def site_pages(path: str, host: str, words: list[str], limit: int = 3) -> list[tuple[str, str]]:
    """[(url, title)] of the pages of `host` that hold every word in their address words or title, shallowest first."""
    if not path or not words:
        return []
    try:
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
        row = db.execute("SELECT n FROM site_hosts WHERE host = ?", (host,)).fetchone()
        if not row:
            return []
        clause = " AND ".join('({words title} : "%s")' % w.replace('"', "") for w in words)
        q = f"site : s{row[0]} AND {clause}"
        rows = db.execute("SELECT p.host, p.path, coalesce(p.title, '') FROM site_pages_fts f JOIN site_pages p ON p.id = f.rowid "
                          "WHERE site_pages_fts MATCH ? LIMIT 50", (q,)).fetchall()
    except sqlite3.Error:
        return []
    rows.sort(key=lambda r: (r[1].count("/"), len(r[1])))
    return [(f"https://{h}{p}", t) for h, p, t in rows[:limit]]
