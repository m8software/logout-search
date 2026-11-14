"""search_api's exact lookups from one read-only SQLite file (T4, ADR-063).

SEARCH_LOOKUP_BACKEND=sqlite routes every lookup (places, suggestions, typo correction, cards,
facts, accounts, nav, kinds, stocks, the tier-4 fuzzy fallbacks) here; text and vector lists
stay where they are. The file is built by tools/sqlite_lookups/export.py; typo correction reads
a Tantivy index of the suggestion keys built beside it.

SqliteLookups.execute(SQL, params) answers a search_api SQL constant with the rows Postgres
returns (same columns, same order rules). Postgres-only parts are reproduced:
  = ANY, @>, DISTINCT ON      IN, a word table, row_number()
  en_US text order            build-side rank columns (wiki_titles.tord, wiki_links.vord,
                              stock_tickers.tord): SQLite compares bytes
  float4 columns              stored float32-rounded; handed back as the shortest decimal,
                              as psycopg does
  NULL order                  Postgres puts NULL first under DESC: spelled out
  pg_trgm similarity()        trigrams below (validated against Postgres, 0/40,000 mismatches)
                              over FTS5 trigram indexes of pg_trgm-padded text, prefix-filtered
Only this module imports sqlite3/tantivy, so the Postgres default needs neither.
"""
from __future__ import annotations

import collections
import json
import math
import re
import sqlite3
import struct
import threading
import unicodedata

CELL, NLON, SEQ_BITS = 0.5, 720, 20
SEP = "\x01"


# ------------------------------------------------------------------ pg_trgm
def pg_lower(s: str) -> str:
    """lower() one character at a time, like glibc towlower: "İ" -> "i", not "i" + a dot."""
    return "".join(ch.lower()[:1] if ch.isupper() else ch for ch in s)


_ASCII_WORD = re.compile(r"[a-z0-9]+")


def words(s: str) -> list[str]:
    # Plain ASCII has no combining marks and lowers as glibc does: one pass in C. The fuzzy fallback compares
    # thousands of titles a query and the loop below was half of each comparison (a 37-letter title: 10.8 µs,
    # this way 2.3, 2026-10-08).
    if s.isascii():
        return _ASCII_WORD.findall(s.lower())
    out, cur = [], []
    for ch in pg_lower(s):
        # glibc's iswalnum (what pg_trgm asks) also counts combining marks as letters
        if ch.isalnum() or (ch > "\x7f" and unicodedata.category(ch)[0] == "M"):
            cur.append(ch)
        elif cur:
            out.append("".join(cur)); cur = []
    if cur:
        out.append("".join(cur))
    return out


def trigrams(s: str) -> set[str]:
    out = set()
    for w in words(s):
        p = "  " + w + " "
        for i in range(len(p) - 2):
            out.add(p[i:i + 3])
    return out


def f32(x: float) -> float:
    return struct.unpack("f", struct.pack("f", x))[0]


def sim_counts(shared: int, na: int, nb: int) -> float:
    if na + nb - shared <= 0:
        return 0.0
    return f32(f32(shared) / f32(na + nb - shared))


def similarity(a: str, b: str) -> float:
    ta, tb = trigrams(a), trigrams(b)
    return sim_counts(len(ta & tb), len(ta), len(tb))


def padded(s: str) -> str:
    """What the FTS5 trigram indexes store: each pg_trgm-padded word, SEP between them."""
    return SEP.join("  " + w + " " for w in words(s))


def phrase(t: str) -> str:
    return '"' + t.replace('"', '""') + '"'


def r32(x):
    """A stored float4 back as psycopg hands it to Python: the shortest decimal ("0.7")."""
    if x is None:
        return None
    import numpy as np
    return float(str(np.float32(x)))


def city_key(city: str) -> str:
    """PLACE_TOWN_SQL's regexp_replace(lower(city), '[^a-z0-9]', '', 'g')."""
    return re.sub(r"[^a-z0-9]", "", pg_lower(city))


def d_km(lat, lon, hlat, hlon):
    # PLACES_SQL's formula, the same operations in double precision
    return 111.32 * math.sqrt(math.pow(lat - hlat, 2) + math.pow((lon - hlon) * math.cos(math.radians(hlat)), 2))


class NoPort(LookupError):
    """This SQL is not a lookup (or has no SQLite port): the router decides what that means."""


class Rows(list):
    def fetchone(self):
        return self[0] if self else None

    def fetchall(self):
        return list(self)


# Tables whose presence the SQLite file decides (search_api.tables() asks the file, not
# Postgres): a file built without the Jev-derived kinds tables turns the whole kinds tier off.
OWNED = frozenset("places suggest_keys suggest_top suggest_phrases suggest_items wiki_aliases wiki_sites wiki_links "
                  "wiki_next wiki_facts host_nav nav_sites kind_docs kind_members kind_keys kind_vecs stock_tickers "
                  "stock_keys card_images site_quality".split())
# A lookup that reaches Postgres while SQLite serves lookups is a bug, never a quiet fall back:
# any SQL naming one of these tables must be answered here (text/vector SQL uses <@> / <=>).
_GUARD = re.compile(r"\b(places|suggest_\w+|wiki_aliases|wiki_sites|wiki_facts|wiki_links|wiki_next|nav_sites|"
                    r"host_nav|site_quality|kind_keys|kind_members|kind_vecs|stock_\w+|card_images)\b")


class SqliteLookups:
    name = "sqlite"

    def __init__(self, path: str, s, typo_index: str):
        self.path, self.s = path, s
        self._local = threading.local()
        db = self.db
        self.tables = frozenset(r[0] for r in db.execute("SELECT name FROM sqlite_schema WHERE type = 'table'"))
        self.kind_id = dict((k, i) for i, k in db.execute("SELECT id, kind FROM place_kinds"))
        self.kind_of = {i: k for k, i in self.kind_id.items()}
        self.kind_cols = frozenset(f"kind_members.{c[1]}" for c in db.execute("PRAGMA table_info(kind_members)")) \
            | frozenset(f"kind_docs.{c[1]}" for c in db.execute("PRAGMA table_info(kind_docs)"))
        import tantivy
        self.tv = tantivy
        idx = tantivy.Index.open(typo_index)
        self.tv_searcher = idx.searcher()
        self.tv_schema = idx.schema
        self.typo_keys = self.tv_searcher.num_docs
        P = s.PLACES_SQL
        self.h = {
            s.SUGGEST_V2_SQL: lambda p: [(True,)],
            P.format(match=s.PLACE_MATCH_KINDS): self.places_kinds,
            P.format(match=s.PLACE_MATCH_NAME): self.places_name,
            s.PLACE_EXACT_SQL: self.place_exact,
            s.PLACE_TOWN_SQL: self.place_town,
            s.CHAIN_HOURS_SQL: lambda p: self.q(f"SELECT hours FROM places WHERE name_key IN ({qm(p['keys'])}) "
                                                f"AND osm GLOB 'wdc:*' AND hours <> '' LIMIT 400", *p["keys"]),
            s.TYPO_SQL: self.typo,
            s.SUGGEST_TOP2_SQL: self.suggest_top2,
            s.SUGGEST_SCAN2_SQL: self.suggest_scan2,
            s.SUGGEST_EXACT2_SQL: lambda p: [self.item32(r) for r in self.db.execute(
                f"SELECT {self._I2}, k.pos FROM suggest_keys k JOIN suggest_items i USING (id) WHERE k.key = ?", (p["p"],))],
            s.SUGGEST_PHRASES_SQL: lambda p: [(json.loads(a), json.loads(b)) for a, b in self.db.execute(
                "SELECT phrases, counts FROM suggest_phrases WHERE prefix = ?", (p["p"],))],
            s.WIKI_EXACT_SITE_SQL: self.wiki_exact_site,
            s.WIKI_EXACT_SQL: lambda p: self.q("SELECT DISTINCT w.title, l.lead FROM wiki_aliases a JOIN wiki_titles w USING (page_id) "
                                               "LEFT JOIN wiki_leads l USING (page_id) WHERE a.norm = ? LIMIT 3", p["nq"]),
            s.FAMOUS_LEAD_SQL: lambda p: self.q("SELECT l.lead FROM wiki_titles w JOIN wiki_leads l USING (page_id) "
                                                "WHERE w.title = ? LIMIT 1", p["t"]),
            s.TOWN_TITLE_SQL: lambda p: self.q(f"SELECT title FROM wiki_titles WHERE title IN ({qm(p['t'])})", *p["t"]),
            s.FACTS_SQL: self.facts,
            s.FACT_TITLES_SQL: lambda p: self.q(f"SELECT DISTINCT w.title FROM wiki_aliases a JOIN wiki_titles w USING (page_id) "
                                                f"WHERE a.norm IN ({qm(p['keys'])}) LIMIT 3", *p["keys"]),
            s.PERSON_SQL: lambda p: self.q("SELECT 1 FROM wiki_facts WHERE title = ? AND fact = 'born' LIMIT 1", p["t"]),
            s.PROFILES_SQL: self.profiles,
            s.ACCOUNT_SQL: self.account,
            s.LINK_SITE_SQL: lambda p: self.q("SELECT url, name, description, sitelinks FROM wiki_links WHERE platform = 'site' "
                                              "AND (key = ? OR wkey = ?) AND coalesce(sitelinks, 0) >= ? ORDER BY sitelinks DESC, vord LIMIT 1",
                                              p["key"], p["key"], s.LINK_SITE_MIN_SITELINKS),
            s.DESCRIBED_SQL: lambda p: self.q("SELECT url, name, description FROM wiki_links WHERE platform = 'described' "
                                              "AND (key = ? OR wkey = ?) ORDER BY sitelinks IS NULL DESC, sitelinks DESC LIMIT 3",
                                              p["key"], p["key"]),
            s.RELATED_SQL: lambda p: self.q("SELECT n.next_title, l.lead FROM wiki_next n JOIN wiki_titles w ON w.title = n.next_title "
                                            "LEFT JOIN wiki_leads l ON l.page_id = w.page_id WHERE n.title = ? "
                                            "ORDER BY n.clicks IS NULL DESC, n.clicks DESC, w.tord LIMIT ?", p["t"], p["n"]),
            s.CARD_SITE_SQL: lambda p: self.q("SELECT ws.url FROM wiki_sites ws JOIN wiki_titles w USING (page_id) "
                                              "WHERE w.title = ? AND ws.url IS NOT NULL LIMIT 1", p["t"]),
            s.CARD_DESCR_SQL: lambda p: self.q("SELECT descr FROM suggest_items WHERE wiki = ? AND descr IS NOT NULL "
                                               "ORDER BY demand DESC LIMIT 1", p["t"]),
            s.WIKI_DEMAND_SQL: lambda p: [(w, r32(d)) for w, d in self.db.execute(
                f"SELECT wiki, max(demand) FROM suggest_items WHERE kind = 'wiki' AND wiki IN ({qm(p['t'])}) GROUP BY wiki", p["t"])],
            s.FAMOUS_SQL: lambda p: [(k, n, w, r32(d), ps) for k, n, w, d, ps in self.db.execute(
                "SELECT i.kind, i.name, i.wiki, coalesce(i.demand, 0), k.pos FROM suggest_keys k JOIN suggest_items i USING (id) "
                "WHERE k.key = ? AND k.pos IN (0, 1) AND i.kind IN ('wiki', 'site', 'youtube') ORDER BY i.demand DESC LIMIT 40",
                (p["k"],))],
            s.ORG_SQL: lambda p: self.q("SELECT n.rank, w.title FROM wiki_aliases a JOIN wiki_sites ws USING (page_id) "
                                        "JOIN wiki_titles w USING (page_id) JOIN nav_sites n ON n.key = ? AND n.domain = ws.domain "
                                        "WHERE a.norm = ? ORDER BY n.rank IS NULL, n.rank LIMIT 1", p["k"], p["k"]),
            s.ORG_BY_SITE_SQL: self.org_by_site,
            s.MAJOR_TEAM_SQL: lambda p: self.team(p, with_box=False),
            s.PRODUCT_SQL: lambda p: [(w, n, u, r32(d)) for w, n, u, d in self.db.execute(
                "SELECT i.wiki, i.name, ws.url, coalesce(max(i.demand), 0) AS d FROM suggest_keys k JOIN suggest_items i USING (id) "
                "JOIN wiki_titles w ON w.title = i.wiki JOIN wiki_sites ws ON ws.page_id = w.page_id "
                "WHERE k.key = ? AND k.pos = 1 AND i.kind = 'wiki' AND ws.url IS NOT NULL "
                "GROUP BY i.wiki, i.name, ws.url ORDER BY d DESC LIMIT 60", (p["k"],))],
            s.BROWSER_TEAM_SQL: lambda p: self.team(p, with_box=True),
            s.NAV_SITES_SQL: lambda p: self.q(f"SELECT host, rank FROM nav_sites WHERE key IN ({qm(p['keys'])}) "
                                              f"ORDER BY rank IS NULL, rank LIMIT 5", *p["keys"]),
            s.HOST_NAV_SQL: lambda p: self.q(f"SELECT url, title, description, rank FROM host_nav WHERE key IN ({qm(p['keys'])}) "
                                             f"ORDER BY rank IS NULL, rank LIMIT 2", *p["keys"]),
            s.DOMAIN_NAV_SQL: lambda p: self.q("SELECT rank FROM nav_sites WHERE host = ? ORDER BY rank IS NULL, rank LIMIT 1", p["d"]),
            s.DOMAIN_DOC_SQL: lambda p: self.q("SELECT d.url, d.title, d.description, s.tranco_rank FROM docs_meta d "
                                               "LEFT JOIN serve_sites s USING (domain) WHERE d.domain = ? LIMIT 1", p["d"]),
            s.NAV_SQL: self.nav,
            s.SELF_NAMED_SQL: self.self_named,
            s.FUZZY_SITE_SQL: self.fuzzy_site,
            s.FUZZY_WIKI_SQL: self.fuzzy_wiki,
            s.KIND_KEYS_SQL: self.kind_keys,
            s.KIND_KEY_EXACT_SQL: lambda p: self.q("SELECT key FROM kind_keys WHERE key = ? AND n >= ?", p["k"], p["m"]),
            s.KIND_VECS_SQL: lambda p: [(k, list(struct.unpack(f"{len(v) // 4}f", v))) for k, v in
                                        self.db.execute("SELECT key, v FROM kind_vecs")],
            s.JUNK_SQL: lambda p: self.q("SELECT domain FROM site_junk"),
            s.STOCK_SQL: self.stock,
            s.STOCK_BARE_SQL: lambda p: [r[:6] + (bool(r[6]),) for r in self.db.execute(
                "SELECT ticker, exchange, coalesce(label, name), cik, site, ir, word FROM stock_tickers WHERE ticker = ?",
                (p["k"].upper(),))],
            s.YT_EXACT_SQL: self.yt_exact,
            s.YT_BARE_SQL: lambda p: self.q("SELECT channel_id, handle, name, description, followers, sitelinks FROM youtube_channels "
                                            "WHERE (key = ? OR hkey = ? OR wkey = ?) AND followers >= ? ORDER BY followers DESC LIMIT 1",
                                            p["key"], p["key"], p["key"], p["min"]),
            s.SUB_EXACT_SQL: lambda p: self.q("SELECT name, title, description, subscribers FROM subreddits WHERE key = ?", p["key"]),
            s.CARD_IMAGE_SQL: lambda p: self.q("SELECT img, w, h, author, licence, licence_url, file FROM card_images WHERE title = ?",
                                               p["t"]),
        }

    # -- plumbing
    @property
    def db(self):
        c = getattr(self._local, "db", None)
        if c is None:
            # one read-only connection per thread: search_api runs its lists side by side
            c = sqlite3.connect(f"file:{self.path}?mode=ro&immutable=1", uri=True, check_same_thread=False)
            c.execute("PRAGMA cache_size = -32000")
            c.execute("CREATE VIRTUAL TABLE IF NOT EXISTS temp.pn_vocab USING fts5vocab(main, place_names, row)")
            if "wiki_trgm_data" in {r[0] for r in c.execute("SELECT name FROM sqlite_schema")}:
                c.execute("CREATE VIRTUAL TABLE IF NOT EXISTS temp.wt_vocab USING fts5vocab(main, wiki_trgm, row)")
                c.execute("CREATE VIRTUAL TABLE IF NOT EXISTS temp.lt_vocab USING fts5vocab(main, label_trgm, row)")
            self._local.db = c
            self._local.df = {}
        return c

    def q(self, sql, *args):
        return Rows(self.db.execute(sql, args).fetchall())

    def present(self, pg_tables: frozenset) -> frozenset:
        """search_api.tables(): the file decides the lookup tables, Postgres the rest."""
        mine = {t for t in OWNED if t in self.tables or (t == "site_quality" and "site_junk" in self.tables)}
        return frozenset((pg_tables - OWNED) | mine)

    def handles(self, sql: str) -> bool:
        return sql in self.h

    def execute(self, sql: str, params=None) -> Rows:
        h = self.h.get(sql)
        if h is not None:
            return Rows(h(params or {}))
        if sql.startswith("SELECT table_name, column_name FROM information_schema.columns"):
            return Rows([tuple(c.split(".")) for c in self.kind_cols
                         if c in ("kind_members.lead", "kind_docs.country")])
        if sql.lstrip().startswith("SELECT k.url, k.title, k.descr, k.domain,") and "kind_members" in sql:
            return Rows(self.kind_key(params))
        raise NoPort("lookup SQL with no SQLite port: " + " ".join(sql.split())[:100])

    def df(self, vocab: str, g: str) -> int:
        key = (vocab, g)
        self.db                                  # this thread's connection (and its df cache)
        cache = self._local.df
        if key not in cache:
            r = self.db.execute(f"SELECT doc FROM temp.{vocab} WHERE term = ?", (g,)).fetchone()
            cache[key] = r[0] if r else 0
        return cache[key]

    def candidates(self, fts: str, vocab: str, A: set, need: float, ranges=((None, None),)) -> set:
        """Rowids holding >= need of A's trigrams must hold one of A's |A| - ceil(need) + 1
        rarest: read only those doclists (the caller checks similarity exactly)."""
        m = len(A) - math.ceil(need) + 1
        rare = sorted((self.df(vocab, g), g) for g in A)[:max(m, 0)]
        ids = set()
        for _, g in rare:
            for lo, hi in ranges:
                if lo is None:
                    ids.update(i for (i,) in self.db.execute(f"SELECT rowid FROM {fts} WHERE {fts} MATCH ?", (phrase(g),)))
                else:
                    ids.update(i for (i,) in self.db.execute(
                        f"SELECT rowid FROM {fts} WHERE {fts} MATCH ? AND rowid BETWEEN ? AND ?", (phrase(g), lo, hi)))
        return ids

    # -- places
    def _place_rows(self, ids, hlat, hlon, r):
        out, ids = [], list(ids)
        for i in range(0, len(ids), 30000):
            chunk = ids[i:i + 30000]
            for (osm, name, kid, lat, lon, street, city, hours, website, phone, brand_lower) in self.db.execute(
                    f"SELECT osm, name, kind_id, lat, lon, street, city, hours, website, phone, brand_lower "
                    f"FROM places WHERE id IN ({qm(chunk)})", chunk):
                if lat is None:
                    continue
                d = d_km(lat, lon, hlat, hlon)
                if d <= r:
                    out.append((osm, name, self.kind_of[kid], street, city, hours, website, phone, d, lat, lon, brand_lower))
        return out

    def places_kinds(self, p):
        lat, lon, e = p["lat"], p["lon"], 1e-4
        ids = []
        for k in p["kinds"]:
            kid = self.kind_id.get(k)
            if kid is not None:
                ids += [i for (i,) in self.db.execute(
                    "SELECT id FROM places_geo WHERE lat0 <= ? AND lat1 >= ? AND lon0 <= ? AND lon1 >= ? AND k0 <= ? AND k1 >= ?",
                    (lat + p["dlat"] + e, lat - p["dlat"] - e, lon + p["dlon"] + e, lon - p["dlon"] - e, kid, kid))]
        return [r[:11] for r in sorted(self._place_rows(ids, lat, lon, p["r"]), key=lambda x: x[8])[:3]]

    def places_name(self, p):
        name, lat, lon = p["name"], p["lat"], p["lon"]
        A = trigrams(name)
        la0, la1 = int((lat - p["dlat"] + 90) // CELL), int((lat + p["dlat"] + 90) // CELL)
        lo0, lo1 = int((lon - p["dlon"] + 180) // CELL), int((lon + p["dlon"] + 180) // CELL)
        ranges = [((la * NLON + lo0) << SEQ_BITS, ((la * NLON + lo1 + 1) << SEQ_BITS) - 1) for la in range(la0, la1 + 1)]
        ids = self.candidates("place_names", "pn_vocab", A, 0.6 * len(A) - 1e-6, ranges) if A else set()
        ids |= {i for (i,) in self.db.execute("SELECT id FROM places WHERE brand_lower = ? AND brand_lower <> ''", (name,))}
        out = [r for r in self._place_rows(ids, lat, lon, p["r"])
               if pg_lower(r[1] or "") == name or (r[11] and r[11] == name) or similarity(r[1] or "", name) >= 0.6]
        out.sort(key=lambda x: x[8])
        return [r[:11] for r in out[:3]]

    def place_exact(self, p):
        keys = p["keys"]
        ids = [i for (i,) in self.db.execute(f"SELECT id FROM places WHERE name_key IN ({qm(keys)}) UNION "
                                             f"SELECT id FROM places WHERE brand_key IN ({qm(keys)}) AND brand_key <> ''", keys + keys)]
        rows = sorted(self._place_rows(ids, p["lat"], p["lon"], p["r"]), key=lambda x: x[8])[:40]
        return [(r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[9], r[10], r[8]) for r in rows]

    def place_town(self, p):
        keys, towns = p["keys"], set(p["towns"])
        ids = [i for i, city in self.db.execute(
            f"SELECT id, city FROM places WHERE name_key IN ({qm(keys)}) UNION "
            f"SELECT id, city FROM places WHERE brand_key IN ({qm(keys)}) AND brand_key <> ''", keys + keys)
            if city and city_key(city) in towns]
        rows = sorted(self._place_rows(ids, p["lat"], p["lon"], math.inf), key=lambda x: x[8])[:40]
        return [(r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[9], r[10], r[8]) for r in rows]

    # -- suggestions
    _I2 = "i.id, i.kind, i.name, i.url, i.domain, i.wiki, i.pop, i.hist, i.demand, i.descr"

    @staticmethod
    def item32(r):
        return r[:6] + (r32(r[6]), r32(r[7]), r32(r[8])) + r[9:]

    def suggest_top2(self, p):
        row = self.db.execute("SELECT ids, pos FROM suggest_top WHERE prefix = ?", (p["p"],)).fetchone()
        if not row:
            return []
        ids, pos = json.loads(row[0]), json.loads(row[1])
        got = {r[0]: r for r in self.db.execute(f"SELECT {self._I2} FROM suggest_items i WHERE id IN ({qm(ids)})", ids)}
        return [self.item32(got[i]) + (ps,) for i, ps in zip(ids, pos) if i in got]

    def suggest_scan2(self, p):
        pre = p["like"][:-1]
        return [self.item32(r) for r in self.db.execute(
            f"SELECT {self._I2}, k.pos FROM (SELECT id, pos FROM (SELECT id, min(pos) AS pos, row_number() OVER "
            f"(PARTITION BY kind ORDER BY max(score) - min(pos) DESC) AS rn FROM suggest_keys "
            f"WHERE key >= ? AND key < ? || char(1114111) GROUP BY id, kind) x WHERE rn <= 12) k JOIN suggest_items i USING (id)",
            (pre, pre))]

    # -- typo correction: Tantivy fuzzy terms (edit distance max_edits), then pg_trgm similarity
    def typo(self, p):
        s, t = self.s, p["t"]
        A = trigrams(t)
        tv = self.tv
        q = tv.Query.fuzzy_term_query(self.tv_schema, "key", t, distance=s.max_edits(t), transposition_cost_one=True)
        sims = []
        for _sc, addr in self.tv_searcher.search(q, 2000).hits:
            key = self.tv_searcher.doc(addr)["key"][0]
            if key == t:
                continue
            B = trigrams(key)
            sm = sim_counts(len(A & B), len(A), len(B))
            if sm >= s.TYPO_SIM:
                sims.append((sm, key))
        sims.sort(key=lambda x: (-x[0], x[1]))
        out = []
        for sm, key in sims:
            for name, score in self.db.execute("SELECT i.name, i.score FROM suggest_keys k JOIN suggest_items i USING (id) "
                                               "WHERE k.key = ?", (key,)):
                out.append((name, sm, r32(score), key))
            if len(out) >= 100:
                break
        return out[:100]

    # -- wiki, cards, accounts
    _NONALNUM = re.compile(r"[\W_]")

    def wiki_exact_site(self, p):
        nq = p["nq"]
        rows = self.db.execute("SELECT DISTINCT w.title, l.lead, ws.url, ws.box, ws.tranco_rank, ws.history_visits FROM wiki_aliases a "
                               "JOIN wiki_titles w USING (page_id) LEFT JOIN wiki_leads l USING (page_id) "
                               "LEFT JOIN wiki_sites ws USING (page_id) WHERE a.norm = ?", (nq,)).fetchall()
        rows = [r for r in rows if not (r[0] or "").startswith(".") and not re.match(r"^https?://(www\.)?(nic|registry)\.", r[2] or "")]
        rows = [r + (pg_lower(self._NONALNUM.sub("", r[0])) == nq,) for r in rows]
        rows.sort(key=lambda r: (not r[6], r[4] is None, r[4] or 0))
        return rows[:3]

    def facts(self, p):
        k, f = p["keys"], p["facts"]
        rows = self.db.execute(
            f"SELECT w.title, f.fact, f.value, f.unit, f.precision, f.at, coalesce(max(si.demand), 0) AS d, min(w.tord) AS o "
            f"FROM wiki_aliases a JOIN wiki_titles w USING (page_id) JOIN wiki_facts f ON f.title = w.title AND f.fact IN ({qm(f)}) "
            f"LEFT JOIN suggest_items si ON si.wiki = w.title WHERE a.norm IN ({qm(k)}) "
            f"GROUP BY w.title, f.fact, f.value, f.unit, f.precision, f.at ORDER BY d DESC, o LIMIT 40", (*f, *k)).fetchall()
        return [r[:6] + (r32(r[6]),) for r in rows]

    def profiles(self, p):
        pl = p["p"]
        return self.q(f"SELECT platform, url, name, description, value, sitelinks FROM (SELECT *, row_number() OVER "
                      f"(PARTITION BY platform ORDER BY length(value), vord) AS rn FROM wiki_links "
                      f"WHERE wiki_title = ? AND platform IN ({qm(pl)})) WHERE rn = 1 ORDER BY platform", p["t"], *pl)

    def account(self, p):
        key, keys, pl = p["key"], p["keys"], p["p"]
        sql = (f"SELECT platform, url, name, description, value, sitelinks FROM ("
               f"SELECT *, CASE WHEN so IS NULL THEN 2 WHEN so THEN 1 ELSE 0 END AS so_o, row_number() OVER (PARTITION BY qid ORDER BY "
               f"CASE WHEN hkey IS NULL THEN 2 WHEN hkey = ? THEN 1 ELSE 0 END DESC, length(value), vord) AS rn FROM ("
               f"SELECT platform, qid, url, name, description, value, sitelinks, hkey, vord, "
               f"(skey = ? AND ? NOT IN (coalesce(key, ''), coalesce(hkey, ''), coalesce(wkey, ''))) AS so FROM wiki_links "
               f"WHERE platform IN ({qm(pl)}) AND (key IN ({qm(keys)}) OR hkey = ? OR wkey IN ({qm(keys)}) OR skey = ?))) "
               f"WHERE rn = 1 ORDER BY so_o, sitelinks IS NULL DESC, sitelinks DESC LIMIT 2")
        return self.q(sql, key, key, key, *pl, *keys, key, *keys, key)

    def org_by_site(self, p):
        k = p["k"]
        rows = self.db.execute("SELECT n.rank, w.title FROM nav_sites n JOIN wiki_sites ws ON ws.domain = n.domain "
                               "JOIN wiki_titles w USING (page_id) WHERE n.key = ? AND n.rank <= ?", (k, p["top"])).fetchall()
        rows = [r for r in rows if re.sub(r"[^a-z0-9]", "", pg_lower(r[1] or "")).startswith(k)]
        rows.sort(key=lambda r: (r[0], len(r[1])))
        return rows[:5]

    def team(self, p, with_box: bool):
        boxes = p.get("boxes") or ["infobox nfl team"]
        rows = self.db.execute(
            "SELECT i.wiki, i.name, ws.box, coalesce(max(i.demand), 0) AS d FROM suggest_keys k JOIN suggest_items i USING (id) "
            "JOIN wiki_titles w ON w.title = i.wiki JOIN wiki_sites ws ON ws.page_id = w.page_id "
            "WHERE k.key = ? AND k.pos IN (0, 1) AND i.kind = 'wiki' AND (ws.box IN (" + qm(boxes) + ") "
            "OR (ws.box IN ('infobox gridiron football team', 'infobox american football team') AND EXISTS (SELECT 1 FROM wiki_facts f "
            "WHERE f.title = i.wiki AND f.fact = 'league' AND f.value = 'National Football League')) "
            "OR (ws.box = 'infobox basketball club' AND EXISTS (SELECT 1 FROM wiki_facts f WHERE f.title = i.wiki "
            "AND f.fact = 'league' AND f.value = 'National Basketball Association'))) "
            "GROUP BY i.wiki, i.name" + (", ws.box" if with_box else "") + " ORDER BY d DESC LIMIT 5", (p["k"], *boxes)).fetchall()
        return [(w, n, b, r32(d)) if with_box else (w, n, r32(d)) for w, n, b, d in rows]

    def nav(self, p):
        keys, gen = p["keys"], re.compile(p["generic"])
        rows = self.db.execute(f"SELECT d.url, d.title, d.description, s.tranco_rank, coalesce(s.history_visits, 0), d.domain, s.cc_hc_pos "
                               f"FROM docs_meta d LEFT JOIN serve_sites s USING (domain) WHERE d.label IN ({qm(keys)})", keys).fetchall()
        rows.sort(key=lambda r: (not gen.search(r[5]), r[3] is None, r[3] or 0, r[6] is None, r[6] or 0))
        return [r[:6] for r in rows[:3]]

    def self_named(self, p):
        sub = p["like"].strip("%")
        if len(sub) < 3:
            return []
        rows = self.db.execute("SELECT d.url, d.title, d.description, d.domain, s.tranco_rank FROM label_sub f "
                               "JOIN docs_meta d ON d.rowid = f.rowid LEFT JOIN serve_sites s USING (domain) "
                               "WHERE label_sub MATCH ?", (phrase(sub),)).fetchall()
        rows = [r for r in rows if sub in r[3].split(".", 1)[0]]
        rows.sort(key=lambda r: (r[4] is None, r[4] or 0))
        return rows[:30]

    def fuzzy_site(self, p):
        key, th = p["key"], self.s.FUZZY_SIM
        A = trigrams(key)
        if not A:
            return []
        ids = list(self.candidates("label_trgm", "lt_vocab", A, th * len(A) - 1e-6))
        out = []
        for i in range(0, len(ids), 30000):
            chunk = ids[i:i + 30000]
            for url, title, desc, label in self.db.execute(
                    f"SELECT url, title, description, label FROM docs_meta WHERE rowid IN ({qm(chunk)})", chunk):
                sm = similarity(label, key)
                if sm >= th:
                    out.append((url, title, desc, sm))
        out.sort(key=lambda r: -r[3])
        return out[:3]

    def fuzzy_wiki(self, p):
        q, th = p["q"], self.s.FUZZY_SIM
        A = trigrams(q)
        if not A:
            return []
        ids = list(self.candidates("wiki_trgm", "wt_vocab", A, th * len(A) - 1e-6))
        best = []
        for i in range(0, len(ids), 30000):
            chunk = ids[i:i + 30000]
            for pid, title in self.db.execute(f"SELECT page_id, title FROM wiki_titles WHERE page_id IN ({qm(chunk)})", chunk):
                sm = similarity(title, q)
                if sm >= th:
                    best.append((sm, pid, title))
        best.sort(key=lambda r: -r[0])
        out = []
        for sm, pid, title in best[:2]:
            lead = self.db.execute("SELECT lead FROM wiki_leads WHERE page_id = ?", (pid,)).fetchone()
            out.append((title, lead[0] if lead else None, sm))
        return out

    # -- kinds (absent from a Jev-free file: search_api never asks then)
    def kind_keys(self, p):
        key, w, mn = p["key"], sorted(set(p["w"])), p["min"]
        cands = {r[0] for r in self.db.execute(f"SELECT key FROM kind_key_words WHERE word IN ({qm(w)}) GROUP BY key "
                                               f"HAVING count(*) = ?", (*w, len(w)))} | {key}
        rows = self.db.execute(f"SELECT key, nwords, n FROM kind_keys WHERE key IN ({qm(list(cands))}) AND n >= ?",
                               (*cands, mn)).fetchall()
        rows.sort(key=lambda r: (r[0] != key, r[1], -(r[2] or 0)))
        return [(r[0],) for r in rows[:3]]

    def kind_key(self, p):
        s, keys = self.s, p["keys"]
        lead = "kind_members.lead" in self.kind_cols
        home = "kind_docs.country" in self.kind_cols and bool(s.KIND_COUNTRY)
        rows = self.db.execute(
            f"SELECT k.url, k.title, k.descr, k.domain, k.tranco_rank, k.cc_hc_pos, k.sitelinks, {'k.country' if home else 'NULL'}, "
            f"max(CASE WHEN m.jev >= 0.7 THEN 2 WHEN m.jev IS NULL THEN 1 WHEN m.jev >= 0.4 THEN 0.5 ELSE 0 END), "
            f"sum(m.votes), {'max(coalesce(m.lead, 0.5))' if lead else '0'}, max(CASE WHEN m.key = ? THEN 1 ELSE 0 END) "
            f"FROM kind_members m JOIN kind_docs k USING (domain) WHERE m.key IN ({qm(keys)}) AND coalesce(m.jev, 0.5) >= 0.3 "
            f"GROUP BY k.url, k.title, k.descr, k.domain, k.tranco_rank, k.cc_hc_pos, k.sitelinks{', k.country' if home else ''}",
            (p["exact"], *keys)).fetchall()
        out = []
        for url, title, descr, dom, tr, cc, sl, country, band, votes, ld, ex in rows:
            pop = (0.15 * max(0, 14 - math.log(tr or 2000000)) + 0.10 * max(0, 18 - math.log(cc or 100000000))
                   + 0.08 * math.log(1 + (sl or 0)))
            sc = 10 * band + pop * (1 + 0.5 * math.log(votes)) + (s.KIND_LEAD_W * ld if lead else 0)
            sc += (s.KIND_HOME_W if home and country == s.KIND_COUNTRY else 0) + p["exact_w"] * ex
            out.append((url, title, descr, dom, sc))
        out.sort(key=lambda r: -r[4])
        return out[:p["k"]]

    # -- stocks, channels
    def stock(self, p):
        cls = p["cls"]
        rows = self.db.execute("SELECT t.ticker, t.exchange, coalesce(t.label, t.name), t.cik, t.site, t.ir, t.sitelinks, t.tord "
                               "FROM stock_keys k JOIN stock_tickers t USING (ticker) WHERE k.key = ?", (p["k"],)).fetchall()
        # Postgres's LIKE is case-sensitive: endswith, not SQLite's LIKE
        rows.sort(key=lambda r: (not r[0].endswith("-" + cls), r[1] == "OTC", -(r[6] or 0), "-" in r[0], len(r[0]), r[7]))
        return [r[:6] for r in rows[:1]]

    def yt_exact(self, p):
        key = p["key"]
        rows = self.db.execute("SELECT channel_id, handle, name, description, followers, sitelinks, key, hkey, wkey, skey "
                               "FROM youtube_channels WHERE key = ? OR hkey = ? OR wkey = ? OR skey = ?", (key,) * 4).fetchall()

        def short_only(r):
            rest = key not in (r[6] or "", r[7] or "", r[8] or "")
            if r[9] is None:
                return None if rest else False
            return r[9] == key and rest
        so = lambda v: 2 if v is None else (1 if v else 0)
        rows.sort(key=lambda r: (so(short_only(r)), r[4] is None, -(r[4] or 0), r[5] is not None, -(r[5] or 0)))
        return [r[:6] for r in rows[:2]]


def qm(xs) -> str:
    return ",".join("?" * len(xs)) or "NULL"


class LookupRouter:
    """A pooled Postgres connection whose lookups go to SQLite. execute() is the only call
    search_api makes on a connection besides SET/RESET, which go to Postgres."""

    def __init__(self, pg, lookups: SqliteLookups, tables_sql: str, text_tables: frozenset = frozenset(),
                 fixed: dict | None = None, missing=None):
        """pg=None: no Postgres at all (SEARCH_POSTGRES=off). The table list is then the lookups'
        plus text_tables (the lists the release text backends serve); `fixed` answers the few
        server-state statements (search_text columns, snapshot id); SET/RESET do nothing; any other
        statement without a port answers no rows after missing(sql) records it (raises NoPort when
        missing is not given)."""
        self.pg, self.lk, self.tables_sql = pg, lookups, tables_sql
        self.text_tables, self.fixed, self.missing = text_tables, fixed or {}, missing

    def execute(self, sql, params=None):
        if sql == self.tables_sql:
            pg = frozenset(r[0] for r in self.pg.execute(sql)) if self.pg is not None else self.text_tables
            return Rows([(t,) for t in sorted(self.lk.present(pg))])
        if self.pg is None:
            if sql in self.fixed:
                return Rows(self.fixed[sql])
            if sql.lstrip().upper().startswith(("SET ", "RESET")):
                return Rows([])
            try:
                return self.lk.execute(sql, params)
            except NoPort:
                if self.missing is None:
                    raise
                # an optional list the release cannot answer yet is empty, never a failed search
                # (rc3 acceptance: YT_SEARCH_SQL turned 143 channel queries into HTTP 503); missing()
                # logs it once and records it in NO_POSTGRES_MISSES
                self.missing(sql)
                return Rows([])
        try:
            return self.lk.execute(sql, params)
        except NoPort:
            if _GUARD.search(sql) and "<@>" not in sql and "<=>" not in sql:
                raise
        return self.pg.execute(sql, params) if params is not None else self.pg.execute(sql)
