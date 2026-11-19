"""The kinds tier's lookups from the release's kinds.sqlite (own licence file; D2, 2026-10-04): what search_api asks
Postgres about kinds of site ("password manager"), answered from the open tables without Postgres.

Attached by lookups_release.ReleaseLookups as schema `kd` when <release>/kinds.sqlite exists (role `kinds`, stamped like the
other per-licence files); a release without it behaves as before: the kinds tables are not announced and the tier is off.
File layout and what is deliberately not in it: apps/search/release/kinds_build.py. The statements answered here are the
ones search_api's kinds tier issues, matched by their exact text (the constants are search_api's own):

  information_schema probe (kind_cols)   -> the columns the open tables have: conf, lead, country  (so kind_open() is true)
  KIND_KEYS_SQL                          kind names containing every query word
  KIND_KEY_EXACT_SQL                     one kind name
  KIND_VECS_SQL                          (name, unit vector): near_kinds' meaning match; rows only when the file has vectors
  kind_key_sql(c)  (KIND_KEY_SQL_OPEN)   the sites of some kinds, scored by conf / votes / lead / popularity / home country
  kind_bm25_sql(c) (KIND_SQL, open pop)  the fused "kinds BM25" list, over the kind labels

A site's url, title and description come from the registry and lookup.sqlite (what exact_anchor does), its Common Crawl
host rank from signals.bin. The legacy formula (the old judged column of kind_members) is NOT implemented: a statement that names it is
refused (NoPort), never answered with the open data.
"""
from __future__ import annotations

import math
import re

import numpy as np

from lookups_sqlite import NoPort

ATTACH_AS = "kd"
KIND_TABLES = frozenset({"kind_docs", "kind_members", "kind_keys"})        # announced when the file is attached
KEY_SQL_HEAD = "SELECT k.url, k.title, k.descr, k.domain,"
_WORD = re.compile(r"\w+")
OR = " OR "            # a BM25 query is any of its words


class KindsRelease:
    def __init__(self, lk):
        self.lk, self.s = lk, lk.s
        d = lk.db
        self.cols = frozenset(f"{t}.{r[1]}" for t in ("kind_members", "kind_docs") for r in d.execute(f"PRAGMA {ATTACH_AS}.table_info({t})"))
        for need in ("kind_members.conf", "kind_members.lead", "kind_docs.country"):
            if need not in self.cols:
                raise RuntimeError(f"release {lk.root}: kinds.sqlite has no {need} - not the open kinds tier, refusing")
        self.n_vecs = d.execute(f"SELECT count(*) FROM {ATTACH_AS}.kind_vecs").fetchone()[0]
        self._outlet_kids = None       # ids of the kind names news_outlet asks about, found on its first call
        s = self.s
        self.h = {
            s.KIND_KEYS_SQL: self.kind_keys,
            s.KIND_KEY_EXACT_SQL: lambda p: [(r[0],) for r in lk.db.execute(
                f"SELECT key FROM {ATTACH_AS}.kind_keys WHERE key = ? AND n >= ?", (p["k"], p["m"]))],
            s.KIND_VECS_SQL: self.kind_vecs,
            s.KIND_SQL.replace(s.KIND_POP, s.KIND_POP_OPEN): self.kind_bm25,
        }

    def tables(self) -> frozenset:
        """The tables search_api's table list shows for this file; kind_vecs only when it has vectors."""
        return KIND_TABLES | ({"kind_vecs"} if self.n_vecs else set())

    def handle(self, sql: str, params=None):
        """Rows for a kinds statement, or None when the statement is not a kinds one."""
        p = params or {}
        h = self.h.get(sql)
        if h is not None:
            return h(p)
        if sql.startswith("SELECT table_name, column_name FROM information_schema.columns"):
            return [tuple(c.split(".")) for c in sorted(self.cols) if c in ("kind_members.conf", "kind_members.lead", "kind_docs.country")]
        if sql.lstrip().startswith(KEY_SQL_HEAD) and "kind_members" in sql:
            if "m.conf" not in sql or "m.jev" in sql:
                raise NoPort("kinds: the legacy kind_members formula has no release port: " + " ".join(sql.split())[:100])
            return self.kind_key(p)
        return None

    def news_outlet(self, doc_id) -> bool:
        """Is this site a news outlet by the kinds data? A member (conf >= OUTLET_MIN_CONF) of a kind named for news
        (newspapers and news channels included). Not the catch-all "news media" (google.com, apple.com, amazon.com and espn.com are in it at 0.5),
        nor the names that say what a site is accused of ("fake news", "hoax news", "news satire")."""
        s = self.s
        if self._outlet_kids is None:
            # the kind names the answer below can turn on, found once. kind_members is keyed (kid, doc_id): asked by
            # doc_id alone it was read whole, 72 ms a site on indexhost (2026-10-11); asked by these kids it is its key
            self._outlet_kids = [i for i, key in self.lk.db.execute(f"SELECT id, key FROM {ATTACH_AS}.kind_keys")
                                 if key == "news media" or s.outlet_kind(key) or s.outlet_medium(key)]
        kids = self._outlet_kids
        rows = self.lk.db.execute(
            f"SELECT k.key, m.conf FROM {ATTACH_AS}.kind_members m JOIN {ATTACH_AS}.kind_keys k ON k.id = m.kid "
            f"WHERE m.kid IN ({','.join('?' * len(kids))}) AND m.doc_id = ? AND m.conf >= ?",
            (*kids, doc_id, s.OUTLET_MIN_CONF)).fetchall() if kids else []
        if any(s.outlet_kind(key) for key, _c in rows):
            return True
        # ... or in the catch-all and, by its own class, a channel or a magazine: espn.com (cable channel 0.85), si.com
        # (sport magazine). apple.com, google.com and nba.com are in the catch-all alone (2026-10-10).
        return (any(key == "news media" for key, _c in rows)
                and any(c >= s.KIND_LIFT_CONF and s.outlet_medium(key) for key, c in rows))

    # ------------------------------------------------------------------ names
    def _kid(self, key):
        r = self.lk.db.execute(f"SELECT id FROM {ATTACH_AS}.kind_keys WHERE key = ?", (key,)).fetchone()
        return r[0] if r else None

    def kind_keys(self, p):
        """KIND_KEYS_SQL: key = the query's name, or every query word among a name's words; n >= min; the exact name first,
        then fewer words, then more sites; three."""
        key, words, mn = p["key"], sorted(set(p["w"])), p["min"]
        db, a = self.lk.db, ATTACH_AS
        cand = {r[0] for r in db.execute(
            f"SELECT kid FROM {a}.kind_key_words WHERE word IN ({','.join('?' * len(words))}) GROUP BY kid HAVING count(*) = ?",
            (*words, len(words)))} if words else set()
        kid = self._kid(key)
        if kid is not None:
            cand.add(kid)
        if not cand:
            return []
        rows = db.execute(f"SELECT key, nwords, n FROM {a}.kind_keys WHERE id IN ({','.join('?' * len(cand))}) AND n >= ?",
                          (*sorted(cand), mn)).fetchall()
        rows.sort(key=lambda r: (r[0] != key, r[1], -r[2], r[0]))
        return [(r[0],) for r in rows[:3]]

    def kind_vecs(self, _p):
        return [(k, np.frombuffer(v, dtype=np.float16).astype(np.float32)) for k, v in self.lk.db.execute(
            f"SELECT k.key, v.v FROM {ATTACH_AS}.kind_vecs v JOIN {ATTACH_AS}.kind_keys k ON k.id = v.kid ORDER BY k.id")]

    # ------------------------------------------------------------------ sites
    def _pop(self, d, pageviews, sitelinks):
        """KIND_POP_OPEN: Wikipedia pageviews lead; the Common Crawl host rank (signals.bin) only nudges, a missing one
        counts as 1M; Wikidata sitelinks; and our own site rank (KIND_RANK_W_OPEN)."""
        cc = self.lk.S(d, "cc_hc") or 1000000
        own = self.lk.S(d, "tranco")             # the release's site rank is our own (G3 D2); None when unranked
        return (0.25 * math.log(1 + (pageviews or 0)) + 0.05 * max(0.0, 18 - math.log(cc)) + 0.08 * math.log(1 + (sitelinks or 0))
                + (self.s.KIND_RANK_W_OPEN * max(0.0, 14 - math.log(own)) if own else 0.0))

    def _site(self, d):
        """(url, title, description, domain) of a site doc_id, or None when the registry no longer knows it."""
        t, host, url, title = self.lk.reg(d)
        if t is None:
            return None
        return url, title or host, self.lk.site(d)[1] or "", host

    def kind_key(self, p):
        """KIND_KEY_SQL_OPEN (search_api): per site over the members of the asked kinds with conf >= 0.3,
        GOOD if its best conf >= KIND_LIFT_CONF, plus min(GOOD - 0.1, conf, popularity, votes, lead, home, exact terms)."""
        s, keys = self.s, [k for k in dict.fromkeys(p["keys"])]
        db, a = self.lk.db, ATTACH_AS
        kids = {r[0]: r[1] for r in db.execute(f"SELECT key, id FROM {a}.kind_keys WHERE key IN ({','.join('?' * len(keys))})", keys)} if keys else {}
        if not kids:
            return []
        ex = kids.get(p["exact"], -1)
        rows = db.execute(
            f"SELECT m.doc_id, max(m.conf), max(m.votes), max(coalesce(m.lead, 0.5)), max(m.kid = ?), d.country, d.pageviews, d.sitelinks "
            f"FROM {a}.kind_members m JOIN {a}.kind_docs d USING (doc_id) WHERE m.kid IN ({','.join('?' * len(kids))}) AND m.conf >= 0.3 "
            f"GROUP BY m.doc_id", (ex, *kids.values())).fetchall()
        home = bool(s.KIND_COUNTRY)
        out = []
        for d, conf, votes, lead, exact, country, pv, sl in rows:
            site = self._site(d)
            if site is None:
                continue
            inner = (s.KIND_CONF_W * conf + self._pop(d, pv, sl) * (1 + 0.5 * math.log(max(1.0, votes))) + s.KIND_LEAD_W_OPEN * lead
                     + (s.KIND_HOME_W if home and country == s.KIND_COUNTRY else 0) + p["exact_w"] * exact)
            score = (s.KIND_GOOD if conf >= s.KIND_LIFT_CONF else 0) + min(s.KIND_GOOD - 0.1, inner)
            out.append((*site, score, d))
        out.sort(key=lambda r: (-r[4], r[5]))
        return [r[:5] for r in out[:p["k"]]]

    def kind_bm25(self, p):
        """KIND_SQL: the 200 best BM25 matches of the query's words over the kind labels, times (1 + popularity), best k.
        FTS5's BM25, not pg_textsearch's: same family (k1 1.2, b 0.75), not the same numbers."""
        words = list(dict.fromkeys(_WORD.findall((p["q"] or "").lower())))
        if not words:
            return []
        q = OR.join('"' + w + '"' for w in words)
        a = ATTACH_AS
        hits = self.lk.db.execute(
            f"SELECT kind_docs_fts.rowid, -bm25(kind_docs_fts), d.pageviews, d.sitelinks FROM {a}.kind_docs_fts "
            f"JOIN {a}.kind_docs d ON d.doc_id = kind_docs_fts.rowid WHERE kind_docs_fts MATCH ? ORDER BY bm25(kind_docs_fts) LIMIT 200", (q,)).fetchall()
        out = []
        for d, bm, pv, sl in hits:
            site = self._site(d)
            if site is not None:
                out.append((*site, bm * (1 + self._pop(d, pv, sl)), d))
        out.sort(key=lambda r: (-r[4], r[5]))
        return [r[:5] for r in out[:p["k"]]]
