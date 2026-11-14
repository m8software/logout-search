"""search_api's exact lookups on lookup.sqlite (T4) — what the downloadable engine would run.

LiteConn stands in for a psycopg connection: execute(SQL, params) is dispatched on the
search_api SQL constant and answered from SQLite with the same rows (same columns, same
order rules). Postgres-only syntax (= ANY, DISTINCT ON, regexp, pg_trgm %, NULLS FIRST on
DESC) is reproduced here, in SQL where SQLite can, in Python where it can't.

Typo correction has two engines: "fts" (FTS5 trigram index, pg_trgm similarity) and "tv"
(Tantivy fuzzy term query on the distinct keys, then the same similarity).
"""
import collections, contextlib, math, re, sqlite3
import trgm
import search_api as s

CELL, NLON, SEQ_BITS = 0.5, 720, 20
TYPO_SEQ_BITS = 23


def d_km(lat, lon, hlat, hlon):
    # PLACES_SQL's formula, same operations in double precision
    return 111.32 * math.sqrt(math.pow(lat - hlat, 2) + math.pow((lon - hlon) * math.cos(math.radians(hlat)), 2))


def r32(x):
    """A stored float4 back as psycopg hands it to Python: the shortest decimal ("0.7")."""
    if x is None:
        return None
    import numpy as np
    return float(str(np.float32(x)))


def item32(r):
    # suggest_items columns pop, hist, demand (positions 6, 7, 8 of _I2) are Postgres reals
    return r[:6] + (r32(r[6]), r32(r[7]), r32(r[8])) + r[9:]


class Rows(list):
    def fetchone(self):
        return self[0] if self else None

    def fetchall(self):
        return list(self)


class Lite:
    def __init__(self, path, typo="fts", tv_path=None, cache_mb=64):
        self.db = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True, check_same_thread=False)
        self.db.execute(f"PRAGMA cache_size = -{cache_mb * 1000}")
        self.db.execute("PRAGMA temp_store = MEMORY")
        self.kind_id = dict((k, i) for i, k in self.db.execute("SELECT id, kind FROM place_kinds"))
        self.kind_of = {i: k for k, i in self.kind_id.items()}
        self.typo = typo
        import os
        self.prefix = os.environ.get("T4_PLACE_PREFIX") == "1"
        self._df = {}
        self.db.execute("CREATE VIRTUAL TABLE temp.pn_vocab USING fts5vocab(main, place_names, row)")
        self.tv = None
        if typo == "tv":
            import tantivy
            self.tv = tantivy.Index.open(tv_path)
            self.tv_searcher = self.tv.searcher()
            self.tv_schema = self.tv.schema
        self.tables = frozenset(r[0] for r in self.db.execute("SELECT name FROM sqlite_schema WHERE type IN ('table', 'view')"))
        P = s.PLACES_SQL
        self.h = {
            s._TABLES_SQL: lambda p: [(t,) for t in s._TABLE_NAMES if t in self.tables or (t == "wiki_docs" and "wiki_titles" in self.tables)],
            s._STORED_SQL: lambda p: [],
            s._GEN_SQL: lambda p: [(1,)],
            s.SUGGEST_V2_SQL: lambda p: [(True,)],
            P.format(match=s.PLACE_MATCH_KINDS): self.places_kinds,
            P.format(match=s.PLACE_MATCH_NAME): self.places_name,
            s.PLACE_EXACT_SQL: self.place_exact,
            s.CHAIN_HOURS_SQL: self.chain_hours,
            s.TYPO_SQL: self.typo_rows,
            s.SUGGEST_TOP2_SQL: self.suggest_top2,
            s.SUGGEST_SCAN2_SQL: self.suggest_scan2,
            s.SUGGEST_EXACT2_SQL: self.suggest_exact2,
            s.SUGGEST_PHRASES_SQL: lambda p: self.q("SELECT phrases, counts FROM suggest_phrases WHERE prefix = ?", p["p"], js=(0, 1)),
            s.WIKI_EXACT_SITE_SQL: self.wiki_exact_site,
            s.WIKI_EXACT_SQL: self.wiki_exact,
            s.FACTS_SQL: self.facts,
            s.FACT_TITLES_SQL: self.fact_titles,
            s.PROFILES_SQL: self.profiles,
            s.ACCOUNT_SQL: self.account,
            s.NAV_SITES_SQL: lambda p: self.q(f"SELECT host, rank FROM nav_sites WHERE key IN ({self.qm(p['keys'])}) ORDER BY rank LIMIT 5", *p["keys"]),
            s.HOST_NAV_SQL: lambda p: self.q(f"SELECT url, title, description, rank FROM host_nav WHERE key IN ({self.qm(p['keys'])}) ORDER BY rank LIMIT 2", *p["keys"]),
            s.DOMAIN_NAV_SQL: lambda p: self.q("SELECT rank FROM nav_sites WHERE host = ? ORDER BY rank LIMIT 1", p["d"]),
            s.NAV_SQL: self.nav,
            s.KIND_KEYS_SQL: self.kind_keys,
            s.CARD_DESCR_SQL: lambda p: self.q("SELECT descr FROM suggest_items WHERE wiki = ? AND descr IS NOT NULL ORDER BY demand DESC LIMIT 1", p["t"]),
            s.RELATED_SQL: self.related,
            s.PERSON_SQL: lambda p: self.q("SELECT 1 FROM wiki_facts WHERE title = ? AND fact = 'born' LIMIT 1", p["t"]),
            s.FAMOUS_SQL: lambda p: self.q32(3, "SELECT i.kind, i.name, i.wiki, coalesce(i.demand, 0), k.pos FROM suggest_keys k "
                                           "JOIN suggest_items i USING (id) WHERE k.key = ? AND k.pos IN (0, 1) "
                                           "AND i.kind IN ('wiki', 'site', 'youtube') ORDER BY i.demand DESC LIMIT 40", p["k"]),
            s.ORG_SQL: lambda p: self.q("SELECT n.rank, w.title FROM wiki_aliases a JOIN wiki_sites ws USING (page_id) "
                                        "JOIN wiki_titles w USING (page_id) JOIN nav_sites n ON n.key = ? AND n.domain = ws.domain "
                                        "WHERE a.norm = ? ORDER BY n.rank LIMIT 1", p["k"], p["k"]),
            s.STOCK_SQL: self.stock,
            s.YT_EXACT_SQL: self.yt_exact,
            s.SUB_EXACT_SQL: lambda p: self.q("SELECT name, title, description, subscribers FROM subreddits WHERE key = ?", p["key"]),
            s.WIKI_DEMAND_SQL: lambda p: self.q32(1, f"SELECT wiki, max(demand) FROM suggest_items WHERE kind = 'wiki' AND wiki IN ({self.qm(p['t'])}) GROUP BY wiki", *p["t"]),
        }

    # -- plumbing
    @staticmethod
    def qm(xs):
        return ",".join("?" * len(xs)) or "NULL"

    def q(self, sql, *args, js=()):
        rows = self.db.execute(sql, args).fetchall()
        if js:
            import json
            rows = [tuple(json.loads(v) if i in js and v is not None else v for i, v in enumerate(r)) for r in rows]
        return Rows(rows)

    def q32(self, col, sql, *args):
        return Rows([r[:col] + (r32(r[col]),) + r[col + 1:] for r in self.db.execute(sql, args)])

    def execute(self, sql, params=None):
        if sql.startswith(("SET ", "RESET")):
            return Rows()
        if sql.startswith("SELECT table_name, column_name FROM information_schema.columns"):
            return Rows([("kind_members", "lead"), ("kind_docs", "country")])
        h = self.h.get(sql)
        if h is None:
            if sql.startswith("\nSELECT k.url, k.title, k.descr, k.domain"):
                return Rows(self.kind_key(sql, params))
            raise KeyError("no SQLite port for: " + sql.strip()[:90])
        return Rows(h(params or {}))

    @contextlib.contextmanager
    def conn(self):
        yield self

    # -- places
    def _place_rows(self, ids, hlat, hlon, r):
        out = []
        ids = list(ids)
        for i in range(0, len(ids), 30000):
            chunk = ids[i:i + 30000]
            for (osm, name, kid, brand, lat, lon, street, city, hours, website, phone, brand_lower) in self.db.execute(
                    f"SELECT osm, name, kind_id, brand, lat, lon, street, city, hours, website, phone, brand_lower "
                    f"FROM places WHERE id IN ({self.qm(chunk)})", chunk):
                if lat is None:
                    continue
                d = d_km(lat, lon, hlat, hlon)
                if d <= r:
                    out.append((osm, name, self.kind_of[kid], street, city, hours, website, phone, d, lat, lon, brand_lower))
        return out

    def _cells(self, lat, lon, dlat, dlon):
        la0, la1 = int((lat - dlat + 90) // CELL), int((lat + dlat + 90) // CELL)
        lo0, lo1 = int((lon - dlon + 180) // CELL), int((lon + dlon + 180) // CELL)
        return [((la * NLON + lo0) << SEQ_BITS, ((la * NLON + lo1 + 1) << SEQ_BITS) - 1) for la in range(la0, la1 + 1)]

    def df(self, g):
        """How many place names hold trigram g (fts5vocab; global, a rarity proxy)."""
        if g not in self._df:
            r = self.db.execute("SELECT doc FROM temp.pn_vocab WHERE term = ?", (g,)).fetchone()
            self._df[g] = r[0] if r else 0
        return self._df[g]

    def places_kinds(self, p):
        lat, lon, e = p["lat"], p["lon"], 1e-4
        ids = []
        for k in p["kinds"]:
            kid = self.kind_id.get(k)
            if kid is None:
                continue
            ids += [i for (i,) in self.db.execute(
                "SELECT id FROM places_geo WHERE lat0 <= ? AND lat1 >= ? AND lon0 <= ? AND lon1 >= ? AND k0 <= ? AND k1 >= ?",
                (lat + p["dlat"] + e, lat - p["dlat"] - e, lon + p["dlon"] + e, lon - p["dlon"] - e, kid, kid))]
        rows = sorted(self._place_rows(ids, lat, lon, p["r"]), key=lambda x: x[8])[:3]
        return [r[:11] for r in rows]

    def places_name(self, p):
        name, lat, lon = p["name"], p["lat"], p["lon"]
        A = trgm.trigrams(name)
        need = 0.6 * len(A) - 1e-6
        cells = self._cells(lat, lon, p["dlat"], p["dlon"])
        if self.prefix:
            # prefix filter: a name sharing >= need of A's trigrams holds at least one of A's
            # len(A) - ceil(need) + 1 rarest; read only those doclists, then check each
            # candidate's similarity exactly (_place_rows + the test below)
            m = len(A) - math.ceil(need) + 1
            df = sorted((self.df(g), g) for g in A)
            ids = set()
            for _, g in df[:m]:
                for lo, hi in cells:
                    ids.update(i for (i,) in self.db.execute(
                        "SELECT rowid FROM place_names WHERE place_names MATCH ? AND rowid BETWEEN ? AND ?", (trgm.fts_phrase(g), lo, hi)))
        else:
            cnt = collections.Counter()
            for g in A:
                for lo, hi in cells:
                    cnt.update(i for (i,) in self.db.execute(
                        "SELECT rowid FROM place_names WHERE place_names MATCH ? AND rowid BETWEEN ? AND ?", (trgm.fts_phrase(g), lo, hi)))
            ids = {i for i, c in cnt.items() if c >= need}
        # an exact brand match needs no shared name trigram
        ids |= {i for (i,) in self.db.execute("SELECT id FROM places WHERE brand_lower = ? AND brand_lower <> ''", (name,))}
        out = []
        for r in self._place_rows(ids, lat, lon, p["r"]):
            if trgm.pg_lower(r[1] or "") == name or (r[11] and r[11] == name) or trgm.similarity(r[1] or "", name) >= 0.6:
                out.append(r)
        out.sort(key=lambda x: x[8])
        return [r[:11] for r in out[:3]]

    def place_exact(self, p):
        keys, hlat, hlon = p["keys"], p["lat"], p["lon"]
        ids = [i for (i,) in self.db.execute(f"SELECT id FROM places WHERE name_key IN ({self.qm(keys)}) "
                                             f"UNION SELECT id FROM places WHERE brand_key IN ({self.qm(keys)}) AND brand_key <> ''",
                                             keys + keys)]
        rows = sorted(self._place_rows(ids, hlat, hlon, p["r"]), key=lambda x: x[8])[:40]
        # PLACE_EXACT_SQL column order: osm, name, kind, street, city, hours, website, phone, lat, lon, d_km
        return [(r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[9], r[10], r[8]) for r in rows]

    def chain_hours(self, p):
        k = p["keys"]
        return self.q(f"SELECT hours FROM places WHERE name_key IN ({self.qm(k)}) AND osm GLOB 'wdc:*' AND hours <> '' LIMIT 400", *k)

    # -- suggestions
    _I2 = "i.id, i.kind, i.name, i.url, i.domain, i.wiki, i.pop, i.hist, i.demand, i.descr"

    def suggest_top2(self, p):
        import json
        row = self.db.execute("SELECT ids, pos FROM suggest_top WHERE prefix = ?", (p["p"],)).fetchone()
        if not row:
            return []
        ids, pos = json.loads(row[0]), json.loads(row[1])
        got = {r[0]: r for r in self.db.execute(f"SELECT {self._I2} FROM suggest_items i WHERE id IN ({self.qm(ids)})", ids)}
        return [item32(got[i]) + (ps,) for i, ps in zip(ids, pos) if i in got]

    def suggest_scan2(self, p):
        pre = p["like"][:-1]
        return [item32(r) for r in self.db.execute(
            f"SELECT {self._I2}, k.pos FROM (SELECT id, pos FROM (SELECT id, min(pos) AS pos, row_number() OVER "
            f"(PARTITION BY kind ORDER BY max(score) - min(pos) DESC) AS rn FROM suggest_keys "
            f"WHERE key >= ? AND key < ? || char(1114111) GROUP BY id, kind) x WHERE rn <= 12) k JOIN suggest_items i USING (id)",
            (pre, pre)).fetchall()]

    def suggest_exact2(self, p):
        return [item32(r) for r in self.db.execute(f"SELECT {self._I2}, k.pos FROM suggest_keys k JOIN suggest_items i USING (id) WHERE k.key = ?",
                                                   (p["p"],)).fetchall()]

    # -- typo correction (TYPO_SQL: name, similarity, score, key; best 100 rows by similarity)
    def typo_rows(self, p):
        t = p["t"]
        A = trgm.trigrams(t)
        na = len(A)
        if self.typo == "tv":
            sims = self._typo_tv(t, A)
        else:
            cnt = collections.Counter()
            for g in A:
                cnt.update(i for (i,) in self.db.execute("SELECT rowid FROM typo_trgm WHERE typo_trgm MATCH ?", (trgm.fts_phrase(g),)))
            sims = []
            for kid, sh in cnt.items():
                sm = trgm.sim_counts(sh, na, kid >> TYPO_SEQ_BITS)
                if sm >= s.TYPO_SIM:
                    sims.append((sm, kid))
            sims.sort(reverse=True)
            sims = sims[:200]
            keys = dict(self.db.execute(f"SELECT kid, key FROM typo_keys WHERE kid IN ({self.qm([k for _, k in sims])})",
                                        [k for _, k in sims]))
            sims = [(sm, keys[k]) for sm, k in sims]
        sims = [(sm, key) for sm, key in sims if key != t]
        out = []
        for sm, key in sims:
            for name, score in self.db.execute("SELECT i.name, i.score FROM suggest_keys k JOIN suggest_items i USING (id) WHERE k.key = ?", (key,)):
                out.append((name, sm, r32(score), key))
            if len(out) >= 100:
                break
        return out[:100]

    def _typo_tv(self, t, A):
        import tantivy
        q = tantivy.Query.fuzzy_term_query(self.tv_schema, "key", t, distance=s.max_edits(t), transposition_cost_one=True)
        res = self.tv_searcher.search(q, 2000).hits
        out = []
        for _sc, addr in res:
            key = self.tv_searcher.doc(addr)["key"][0]
            sm = trgm.sim_counts(len(A & trgm.trigrams(key)), len(A), len(trgm.trigrams(key)))
            if sm >= s.TYPO_SIM:
                out.append((sm, key))
        out.sort(key=lambda x: (-x[0], x[1]))
        return out

    # -- wiki, cards, accounts
    _OWN = re.compile(r"[^\w]|_")

    def _own(self, title, nq):
        return trgm.pg_lower(self._OWN.sub("", title)) == nq

    def wiki_exact_site(self, p):
        nq = p["nq"]
        rows = self.db.execute("SELECT DISTINCT w.title, ws.url, ws.box, ws.tranco_rank, ws.history_visits FROM wiki_aliases a "
                               "JOIN wiki_titles w USING (page_id) LEFT JOIN wiki_sites ws USING (page_id) WHERE a.norm = ?", (nq,)).fetchall()
        rows = [r for r in rows if not (r[0] or "").startswith(".") and not re.match(r"^https?://(www\.)?(nic|registry)\.", r[1] or "")]
        rows = [(t, None, u, b, tr, hv, self._own(t, nq)) for t, u, b, tr, hv in rows]
        rows.sort(key=lambda r: (not r[6], r[4] is None, r[4] or 0))
        return rows[:3]

    def wiki_exact(self, p):
        return [(t, None) for (t,) in self.db.execute("SELECT DISTINCT w.title FROM wiki_aliases a JOIN wiki_titles w USING (page_id) "
                                                      "WHERE a.norm = ? LIMIT 3", (p["nq"],))]

    def facts(self, p):
        k, f = p["keys"], p["facts"]
        return self.q32(6, f"SELECT w.title, f.fact, f.value, f.unit, f.precision, f.at, coalesce(max(si.demand), 0) AS d "
                      f"FROM wiki_aliases a JOIN wiki_titles w USING (page_id) "
                      f"JOIN wiki_facts f ON f.title = w.title AND f.fact IN ({self.qm(f)}) "
                      f"LEFT JOIN suggest_items si ON si.wiki = w.title WHERE a.norm IN ({self.qm(k)}) "
                      f"GROUP BY w.title, f.fact, f.value, f.unit, f.precision, f.at ORDER BY d DESC, w.title LIMIT 40", *f, *k)

    def fact_titles(self, p):
        k = p["keys"]
        return self.q(f"SELECT DISTINCT w.title FROM wiki_aliases a JOIN wiki_titles w USING (page_id) WHERE a.norm IN ({self.qm(k)}) LIMIT 3", *k)

    def profiles(self, p):
        pl = p["p"]
        return self.q(f"SELECT platform, url, name, description, value, sitelinks FROM (SELECT *, row_number() OVER "
                      f"(PARTITION BY platform ORDER BY length(value), value) AS rn FROM wiki_links "
                      f"WHERE wiki_title = ? AND platform IN ({self.qm(pl)})) WHERE rn = 1 ORDER BY platform", p["t"], *pl)

    def account(self, p):
        key, keys, pl = p["key"], p["keys"], p["p"]
        # Postgres sorts NULL first under DESC and last under ASC: spelled out with CASE
        sql = (f"SELECT platform, url, name, description, value, sitelinks FROM ("
               f"SELECT *, CASE WHEN so IS NULL THEN 2 WHEN so THEN 1 ELSE 0 END AS so_o, row_number() OVER (PARTITION BY qid ORDER BY "
               f"CASE WHEN hkey IS NULL THEN 2 WHEN hkey = ? THEN 1 ELSE 0 END DESC, length(value)) AS rn FROM ("
               f"SELECT platform, qid, url, name, description, value, sitelinks, hkey, "
               f"(skey = ? AND ? NOT IN (coalesce(key, ''), coalesce(hkey, ''), coalesce(wkey, ''))) AS so FROM wiki_links "
               f"WHERE platform IN ({self.qm(pl)}) AND (key IN ({self.qm(keys)}) OR hkey = ? OR wkey IN ({self.qm(keys)}) OR skey = ?))) "
               f"WHERE rn = 1 ORDER BY so_o, sitelinks IS NULL DESC, sitelinks DESC LIMIT 2")
        return self.q(sql, key, key, key, *pl, *keys, key, *keys, key)

    def related(self, p):
        return [(t, None) for (t,) in self.db.execute(
            "SELECT n.next_title FROM wiki_next n JOIN wiki_titles w ON w.title = n.next_title WHERE n.title = ? "
            "ORDER BY n.clicks DESC, n.next_title LIMIT ?", (p["t"], p["n"]))]

    def nav(self, p):
        keys = p["keys"]
        gen = re.compile(p["generic"])
        rows = self.db.execute(f"SELECT d.url, d.title, d.description, s.tranco_rank, coalesce(s.history_visits, 0), d.domain, s.cc_hc_pos "
                               f"FROM docs_meta d LEFT JOIN serve_sites s USING (domain) WHERE d.label IN ({self.qm(keys)})", keys).fetchall()
        rows.sort(key=lambda r: (not gen.search(r[5]), r[3] is None, r[3] or 0, r[6] is None, r[6] or 0))
        return [r[:6] for r in rows[:3]]

    def kind_keys(self, p):
        key, w, mn = p["key"], sorted(set(p["w"])), p["min"]
        cands = {r[0] for r in self.db.execute(f"SELECT key FROM kind_key_words WHERE word IN ({self.qm(w)}) GROUP BY key "
                                               f"HAVING count(*) = ?", (*w, len(w)))} | {key}
        rows = self.db.execute(f"SELECT key, nwords, n FROM kind_keys WHERE key IN ({self.qm(list(cands))}) AND n >= ?",
                               (*cands, mn)).fetchall()
        rows.sort(key=lambda r: (r[0] != key, r[1], -(r[2] or 0)))
        return [(r[0],) for r in rows[:3]]

    def kind_key(self, sql, p):
        keys = p["keys"]
        rows = self.db.execute(
            f"SELECT k.url, k.title, k.descr, k.domain, k.tranco_rank, k.cc_hc_pos, k.sitelinks, k.country, "
            f"max(CASE WHEN m.jev >= 0.7 THEN 2 WHEN m.jev IS NULL THEN 1 WHEN m.jev >= 0.4 THEN 0.5 ELSE 0 END), "
            f"sum(m.votes), max(coalesce(m.lead, 0.5)), max(CASE WHEN m.key = ? THEN 1 ELSE 0 END) "
            f"FROM kind_members m JOIN kind_docs k USING (domain) WHERE m.key IN ({self.qm(keys)}) AND coalesce(m.jev, 0.5) >= 0.3 "
            f"GROUP BY k.url, k.title, k.descr, k.domain, k.tranco_rank, k.cc_hc_pos, k.sitelinks, k.country",
            (p["exact"], *keys)).fetchall()
        out = []
        for url, title, descr, dom, tr, cc, sl, country, band, votes, lead, ex in rows:
            pop = (0.15 * max(0, 14 - math.log(tr or 2000000)) + 0.10 * max(0, 18 - math.log(cc or 100000000))
                   + 0.08 * math.log(1 + (sl or 0)))
            sc = 10 * band + pop * (1 + 0.5 * math.log(votes)) + s.KIND_LEAD_W * lead
            sc += (s.KIND_HOME_W if s.KIND_COUNTRY and country == s.KIND_COUNTRY else 0) + p["exact_w"] * ex
            out.append((url, title, descr, dom, sc))
        out.sort(key=lambda r: -r[4])
        return out[:p["k"]]

    def stock(self, p):
        cls = p["cls"]
        rows = self.db.execute("SELECT t.ticker, t.exchange, coalesce(t.label, t.name), t.cik, t.site, t.ir, t.sitelinks "
                               "FROM stock_keys k JOIN stock_tickers t USING (ticker) WHERE k.key = ?", (p["k"],)).fetchall()
        # LIKE in Postgres is case-sensitive: endswith, not SQLite's LIKE
        rows.sort(key=lambda r: (not r[0].endswith("-" + cls), r[1] == "OTC", -(r[6] or 0), "-" in r[0], len(r[0]), r[0]))
        return [r[:6] for r in rows[:1]]

    def yt_exact(self, p):
        key = p["key"]
        rows = self.db.execute("SELECT channel_id, handle, name, description, followers, sitelinks, key, hkey, wkey, skey "
                               "FROM youtube_channels WHERE key = ? OR hkey = ? OR wkey = ? OR skey = ?", (key,) * 4).fetchall()

        def short_only(r):
            if r[9] is None:
                return None if key not in (r[6] or "", r[7] or "", r[8] or "") else False
            return r[9] == key and key not in (r[6] or "", r[7] or "", r[8] or "")
        so = lambda v: 2 if v is None else (1 if v else 0)
        rows.sort(key=lambda r: (so(short_only(r)), r[4] is None, -(r[4] or 0), r[5] is not None, -(r[5] or 0)))
        return [r[:6] for r in rows[:2]]
