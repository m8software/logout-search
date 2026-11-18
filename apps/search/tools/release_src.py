"""Eval and ops tools read a RELEASE FOLDER, not Postgres (SEARCH-POSTGRES-EXIT.md phase 3).

    import release_src
    r = release_src.open_release("/opt/logout-release/current")      # a release folder, or BASE with `current`
    r.demand_for_keys(["gillette", "gilletteshaving"], "pop")        # eval-demand.py
    r.top_domains(30000, 400, rnd)                                   # yardstick.py / search-ranker.py samples

It opens lookup.sqlite (names, cards, suggest_*, sites, youtube), registry.sqlite (doc_id -> title/key/url),
places-osm.sqlite and signals.bin READ-ONLY, with the same classes the engine uses (lookups_release.Signals and
Registry): nothing about the file layout is re-implemented here. No psycopg, no network, no server.

Every function returns what the Postgres query it replaces returned (same column order, same population);
the sampling ones draw with the caller's random.Random instead of ORDER BY random(), so the same seed gives the
same sample on the same release. Things a release never ships (subreddits, page text ADR-059, the legacy
kinds tier ADR-065) have no function here: the tool says so and skips that kind.
"""
from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import sys
import zlib

_API = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "serve", "api")
if _API not in sys.path:
    sys.path.insert(0, _API)

import lookups_release as lr  # noqa: E402  (pure sqlite3/numpy: no psycopg)
import release_root  # noqa: E402

BOX_RE = re.compile(r"company|website|organi|airline|bank|retail|software|service")      # yardstick/search-ranker nav-brand
TEAM_RE = re.compile(r"sports team|football club|baseball team|basketball team|hockey team")
HAS_DIGIT_OR_PUNCT = re.compile(r"[(),:0-9]")
REGISTRY_FIELDS = ("wiki", "site", "page", "place", "account", "youtube", "stock", "link")


class ReleaseSrc:
    def __init__(self, root: str):
        self.root = release_root.resolve(root).rstrip("/")
        for need in ("lookup.sqlite", "registry.sqlite", "signals.bin"):
            if not os.path.exists(f"{self.root}/{need}"):
                raise SystemExit(f"{self.root} is not a release folder: no {need}")
        ro = "?mode=ro&immutable=1"
        self.db = sqlite3.connect(f"file:{self.root}/lookup.sqlite{ro}", uri=True)
        self.db.execute(f"ATTACH 'file:{self.root}/registry.sqlite{ro}' AS r")
        self.has_places = os.path.exists(f"{self.root}/places-osm.sqlite")
        if self.has_places:
            self.db.execute(f"ATTACH 'file:{self.root}/places-osm.sqlite{ro}' AS p")
        self.sig = lr.Signals(self.root)
        self.reg = lr.Registry(self.db)
        self.shift = self.reg.shift
        self.dict = dict(self.db.execute("SELECT id, s FROM dict"))
        self.pdict = dict(self.db.execute("SELECT id, s FROM p.dict")) if self.has_places else {}
        self.release_id = dict(self.db.execute("SELECT key, value FROM meta")).get("built_release")

    # ------------------------------------------------------------------ small helpers
    def S(self, d: int, field: str, ints: bool = True):
        return self.sig.get(d, field, ints)

    def card(self, d: int) -> dict:
        row = self.db.execute("SELECT c FROM cards WHERE doc_id = ?", (d,)).fetchone()
        if not row:
            return {}
        return json.loads(zlib.decompress(row[0]) if isinstance(row[0], bytes) else row[0])

    def title(self, d: int):
        return self.reg.row(d)[3]

    def names(self, kind: int, prefix: str | None = None, name: str | None = None) -> list[tuple[str, int, int]]:
        """(name, doc_id, flags) rows of one names.kind, by exact name or by prefix."""
        if name is not None:
            return self.db.execute("SELECT name, doc_id, flags FROM names WHERE name = ? AND kind = ?", (name, kind)).fetchall()
        hi = prefix + "\U0010ffff"
        return self.db.execute("SELECT name, doc_id, flags FROM names WHERE name >= ? AND name < ? AND kind = ?",
                               (prefix, hi, kind)).fetchall()

    def _docs_of_type(self, t: int) -> list[int]:
        lo, hi = t << self.shift, (t + 1) << self.shift
        return [d for (d,) in self.db.execute("SELECT doc_id FROM r.registry WHERE doc_id >= ? AND doc_id < ? ORDER BY doc_id", (lo, hi))]

    # ------------------------------------------------------------------ eval-demand.py
    def demand_for_keys(self, keys: list[str], signal: str) -> float | None:
        """max over the suggestion items any key reaches. signal 'pop' = suggest_items.pop; 'searches' = the
        clickstream demand, log-scaled: least(1, log(1 + coalesce(demand, 0)) / 6) (Postgres log() is base 10).
        None = no key reaches an item."""
        best = None
        for k in keys:
            for (d,) in self.db.execute("SELECT DISTINCT doc_id FROM suggest_keys WHERE key = ?", (k,)):
                if signal == "pop":
                    v = self.S(d, "pop", False)
                else:
                    v = min(1.0, math.log10(1 + (self.S(d, "demand", False) or 0.0)) / 6)
                if v is not None and (best is None or v > best):
                    best = v
        return best

    # eval-queries.py's is-the-answer-indexed check ships its own stdlib-only snippet (it runs on the box, where
    # this module is not installed): PRESENT_RELEASE there reads the same names / sites tables.

    # ------------------------------------------------------------------ eval-completion.py baseline
    def suggest_baseline(self, prefixes: list[str]) -> dict[str, list[dict]]:
        """The old BASELINE_SQL: per typed prefix the top YouTube channel (>= 1M followers) then up to three
        sites by Tranco (<= 50000), each {url, title, source}."""
        out: dict[str, list[dict]] = {}
        for p in prefixes:
            squeezed = p.replace(" ", "")
            rows: list[dict] = []
            yts = {}
            for _n, d, f in self.names(lr.N_YOUTUBE, prefix=squeezed):
                if f & lr.F_KEY:
                    yts[d] = self.S(d, "followers")
            yts = {d: v for d, v in yts.items() if v is not None and v >= 1_000_000}
            if yts:
                d = max(yts, key=lambda x: (yts[x], -x))
                handle = self.db.execute("SELECT handle FROM youtube WHERE doc_id = ?", (d,)).fetchone()
                _t, key, _u, title = self.reg.row(d)
                rows.append({"url": "https://www.youtube.com/@" + ((handle[0] if handle and handle[0] else None) or key),
                             "title": title, "source": "youtube"})
            sites = []
            for _n, d, _f in self.names(lr.N_DOMAIN, prefix=squeezed):
                rk = self.S(d, "tranco")
                home = self.db.execute("SELECT home FROM sites WHERE doc_id = ?", (d,)).fetchone()
                if rk is not None and rk <= 50000 and home and home[0]:
                    sites.append((rk, d))
            for _rk, d in sorted(sites)[:3]:
                _t, _k, url, title = self.reg.row(d)
                rows.append({"url": url, "title": title, "source": "site"})
            out[p] = rows
        return out

    # ------------------------------------------------------------------ yardstick.py / search-ranker.py populations
    def _official(self):
        """(wiki doc, domain, box, site doc) for every article with an official site, from names kind 10 + cards."""
        seen = set()
        for dom, d, _f in self.db.execute("SELECT name, doc_id, flags FROM names WHERE kind = ?", (lr.N_OFFICIAL,)):
            if d in seen:                    # one official site per article
                continue
            seen.add(d)
            o = self.card(d).get("o")
            box = self.dict.get(o[1]) if o and o[1] else None
            sdoc = o[2] if o and len(o) > 2 else None
            yield d, dom, box, sdoc

    def brands(self, limit: int, rnd, box_re=BOX_RE, tranco_below: int = 50000) -> list[tuple[str, str, int]]:
        """(title, official domain, articles sharing that domain) of company-like articles with a top site."""
        cands, shared = [], {}
        for d, dom, box, sdoc in self._official():
            shared[dom] = shared.get(dom, 0) + 1
            rk = self.S(sdoc, "tranco") if sdoc else None
            if rk is not None and rk < tranco_below and box and box_re.search(box):
                cands.append((self.title(d), dom, d))
        rnd.shuffle(cands)
        return [(t, dom, shared[dom]) for t, dom, _d in cands[:limit]]

    def teams(self, limit: int, rnd) -> list[str]:
        """Titles of sports-team articles with an official site under Tranco 200000 and no digit in the title."""
        cands = []
        for d, _dom, box, sdoc in self._official():
            rk = self.S(sdoc, "tranco") if sdoc else None
            t = self.title(d) or ""
            if box and TEAM_RE.search(box) and rk is not None and rk < 200000 and not re.search(r"[0-9]", t):
                cands.append(t)
        rnd.shuffle(cands)
        return cands[:limit]

    def top_domains(self, below: int, limit: int, rnd) -> list[str]:
        """Served sites ranked under `below` by Tranco."""
        out = []
        for d in self._docs_of_type(lr.SITE):
            rk = self.S(d, "tranco")
            if rk is not None and rk < below:
                out.append(self.reg.row(d)[1])
        rnd.shuffle(out)
        return out[:limit]

    def wiki_topics(self, limit: int, rnd, min_pop: float | None = None) -> list[str]:
        """Titles of articles with no official site, 4..40 characters, none of ( ) , : or a digit. With
        min_pop: the suggestion popularity (search-ranker's 'topic' kind: pop >= 0.55, 4..30 characters)."""
        have_site = {d for d, _dom, _b, _s in self._official()}
        out = []
        for d in self._docs_of_type(lr.WIKI):
            if d in have_site:
                continue
            t = self.title(d) or ""
            hi = 40 if min_pop is None else 30
            if not (4 <= len(t) <= hi) or HAS_DIGIT_OR_PUNCT.search(t):
                continue
            if min_pop is not None and not ((self.S(d, "pop", False) or 0) >= min_pop):
                continue
            out.append(t)
        rnd.shuffle(out)
        return out[:limit]

    def youtube_big(self, followers: int, limit: int, rnd) -> list[tuple[str, str]]:
        """(name, 'channel_id handle ...' of every channel of the same entity) of channels with >= `followers`."""
        by_wiki: dict = {}
        rows = []
        for d, handle, wiki in self.db.execute("SELECT doc_id, handle, wiki FROM youtube"):
            _t, key, _u, title = self.reg.row(d)
            rows.append((d, key, handle, wiki, title))
            if wiki is not None:
                by_wiki.setdefault(wiki, []).append((key, handle))
        big = [r for r in rows if (self.S(r[0], "followers") or 0) >= followers]
        rnd.shuffle(big)
        out = []
        for _d, key, handle, wiki, title in big[:limit]:
            same = by_wiki.get(wiki, [(key, handle)])
            ids = " ".join(f"{k.lower()} {(h or '').lower()}" for k, h in same)
            out.append((title, ids))
        return out

    def accounts(self, platforms: tuple, min_sitelinks: int, limit: int, rnd) -> list[tuple[str, str, str]]:
        """(entity name, platform, the entity's account urls on that platform, space-separated), one row per
        (entity, platform), for entities with >= min_sitelinks (any account of the group counts, like max())."""
        groups: dict = {}
        for d in self._docs_of_type(lr.ACCOUNT):
            plat, _handle, url, q = self.reg.account(d)
            if plat not in platforms:
                continue
            g = groups.setdefault((q if q is not None else -d, plat), {"urls": [], "sl": 0, "doc": d})
            g["urls"].append(url)
            g["sl"] = max(g["sl"], self.S(d, "sitelinks") or 0)
        out = []
        for (q, plat), g in sorted(groups.items()):
            if g["sl"] < min_sitelinks:
                continue
            ent = self.db.execute("SELECT name FROM entities WHERE id = ?", (q,)).fetchone() if q >= 0 else None
            name = ent[0] if ent and ent[0] else self.reg.row(g["doc"])[3]
            if name:
                out.append((name, plat, " ".join(g["urls"])))
        rnd.shuffle(out)
        return out[:limit]

    # ------------------------------------------------------------------ places (local_yardstick.py, yardstick.py)
    def _kind(self, kid):
        return self.pdict.get(kid)

    def _need_places(self):
        if not self.has_places:
            raise SystemExit(f"{self.root} has no places-osm.sqlite")

    def place_cells(self, min_count: int) -> list[tuple[float, float, int]]:
        """1-degree cells (round(lat), round(lon)) holding >= min_count OSM places: the OSM file holds nothing else."""
        self._need_places()
        cells: dict = {}
        for la, lo in self.db.execute("SELECT lat, lon FROM p.places WHERE lat IS NOT NULL"):
            k = (float(round(la)), float(round(lo)))
            cells[k] = cells.get(k, 0) + 1
        return [(a, b, n) for (a, b), n in cells.items() if n >= min_count]

    def home_points(self, la: float, lo: float, kinds: tuple, limit: int = 200) -> list[tuple[float, float]]:
        """Lat/lon of places of those kinds inside the cell (+- 0.5 degree)."""
        self._need_places()
        ids = [i for i, s in self.pdict.items() if s in kinds]
        if not ids:
            return []
        q = ",".join("?" * len(ids))
        return self.db.execute(f"SELECT lat, lon FROM p.places WHERE kind IN ({q}) AND lat BETWEEN ? AND ? AND lon BETWEEN ? AND ? "
                               "ORDER BY pid LIMIT ?", (*ids, la - 0.5, la + 0.5, lo - 0.5, lo + 0.5, limit)).fetchall()

    def places_in_box(self, lat0: float, lat1: float, lon0: float, lon1: float) -> list[tuple[str, str, str, float, float]]:
        """(osm, kind, lower(name), lat, lon) inside the box, without schema= kinds."""
        self._need_places()
        out = []
        for doc, name, kid, la, lo in self.db.execute(
                "SELECT doc_id, name, kind, lat, lon FROM p.places WHERE lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?",
                (lat0, lat1, lon0, lon1)):
            kind = self._kind(kid) or ""
            if kind.startswith("schema="):
                continue
            out.append((self.reg.row(doc)[1], kind, (name or "").lower(), la, lo))
        return out

    def places_by_osm(self, osm_ids: list[str]) -> dict[str, tuple[str, str, float, float]]:
        """osm id -> (kind, lower(name), lat, lon). The osm id is the registry key (path) of the place doc."""
        self._need_places()
        want = set(osm_ids)
        lo, hi = lr.PLACE << self.shift, (lr.PLACE + 1) << self.shift
        doc_osm = {d: path for d, path in self.db.execute(
            "SELECT doc_id, path FROM r.registry WHERE doc_id >= ? AND doc_id < ?", (lo, hi)) if path in want}
        out = {}
        docs = sorted(doc_osm)
        for i in range(0, len(docs), 500):
            chunk = docs[i:i + 500]
            q = ",".join("?" * len(chunk))
            for doc, name, kid, la, lo_ in self.db.execute(
                    f"SELECT doc_id, name, kind, lat, lon FROM p.places WHERE doc_id IN ({q})", chunk):
                out[doc_osm[doc]] = (self._kind(kid) or "", (name or "").lower(), float(la), float(lo_))
        return out

    def local_brands(self, min_places: int, limit: int, rnd) -> list[tuple[str, str]]:
        """(lower(name), most common website host) of branded places with >= min_places locations."""
        self._need_places()
        counts: dict = {}
        sites: dict = {}
        for name, website in self.db.execute("SELECT lower(name), website FROM p.places WHERE brand <> ''"):
            counts[name] = counts.get(name, 0) + 1
            m = re.match(r"^https?://(?:www\.)?([^/]+).*$", website or "")
            if m:
                sites.setdefault(name, {})
                sites[name][m.group(1)] = sites[name].get(m.group(1), 0) + 1
        keep = sorted(n for n, c in counts.items() if c >= min_places)
        rnd.shuffle(keep)
        out = []
        for n in keep[:limit]:
            s = sites.get(n) or {}
            out.append((n, max(sorted(s), key=lambda h: s[h]) if s else ""))
        return out


def open_release(root: str) -> ReleaseSrc:
    return ReleaseSrc(root)


def release_arg(ap) -> None:
    """--release DIR on a tool's parser (default: $SEARCH_RELEASE_DIR; unset = the legacy Postgres path)."""
    ap.add_argument("--release", default=os.environ.get("SEARCH_RELEASE_DIR", ""),
                    help="read this release folder (or BASE with `current`) instead of Postgres; default $SEARCH_RELEASE_DIR. "
                         "Unset = the LEGACY Postgres path, deleted at phase 7 of SEARCH-POSTGRES-EXIT.md")
