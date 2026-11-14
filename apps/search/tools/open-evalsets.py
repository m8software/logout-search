#!/usr/bin/env python3
# LEGACY, Postgres-bound (SEARCH-POSTGRES-EXIT.md phase 3): it reads wiki_docs.aliases (raw redirect titles), wiki_links (Wikidata ids) and suggest_items from Postgres; a release keeps only normalised name keys, so it cannot be ported without a new release table. Delete at phase 7.
"""Answer-known eval sets from open data (ADR-064/065): no Jev, no LLM, no browsing history.

    tools/open-evalsets.py [--host deploy] [--out tools/evalsets/open] [--scale 1.0]

Every label is a fact somebody already wrote down in an open dataset:

  wiki-redirect  Wikipedia redirect -> its target article        ("big apple" style)    CC BY-SA 4.0
  site-nav       Wikidata official website (P856) -> its domain  ("<name> website")    CC0
  account        Wikidata X/Instagram/Facebook/TikTok/LinkedIn/YouTube ids -> the URL  CC0
  place          OpenStreetMap named feature near the box's home -> that feature     ODbL 1.0
  place-town     the same feature asked as "<name> <town>"                             ODbL 1.0
  typo-*         a generated misspelling of a wiki-redirect / site-nav / place query,
                 same answer, same licence as its source

The data comes from testbox's serving database (read-only, `ssh testbox`), which holds the
public tables built from the Wikipedia dump (wiki_docs.aliases = redirect titles), the
Wikidata dump (wiki_links) and OSM extracts (places). Popularity strata use only
suggest_items.pop (Wikipedia pageviews) and Wikidata sitelinks — never the history- or
Jev-derived columns (suggest_items.hist/demand, *history_visits, site_quality, kind_members).

Sampling is deterministic (md5 of a fixed seed and the entity), so a rerun on the same
data writes the same files. The split is by ENTITY, the same key in every set (the
normalised Wikipedia title when the thing has an article, else the Wikidata QID, else the
OSM id): Barack Obama is test in the redirect, site, account and typo sets alike, so a
model trained on dev never saw a test entity under another template.

Ambiguous names (one name, several entities) are not scored; they are written to
ambiguous-<set>.tsv with how many entities share the name.

Output (TSV, header row; tools/eval-queries.py reads it): query, category, expected,
set, stratum, split, entity.
"""
from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import unquote, urlsplit

SEED = "402"
_TOKEN = re.compile(r"[a-z0-9]+")

HOME = (40.592, -105.871)        # testbox's SEARCH_HOME: /search measures places from here
PLACE_R_KM = 38.0               # inside the engine's 40 km place radius, with a margin
PLACE_SAME_KM = 0.5             # rows of one name this close are one place (OSM + listing)

# Hosts that are nobody's own site: a path on them is an account, not a domain answer.
SHARED_HOSTS = {"facebook.com", "twitter.com", "x.com", "instagram.com", "youtube.com", "linkedin.com",
                "wikipedia.org", "wikidata.org", "github.com", "google.com", "tiktok.com", "soundcloud.com",
                "bandcamp.com", "blogspot.com", "wordpress.com", "tumblr.com", "medium.com", "myspace.com",
                "archive.org", "weebly.com", "wixsite.com", "squarespace.com", "jimdo.com", "angelfire.com",
                "geocities.com", "tripod.com", "about.me", "linktr.ee", "vk.com", "t.me", "spotify.com",
                "apple.com", "amazon.com", "imdb.com"}
# The engine's own refusals (search_api._ACCOUNT_NOT_A_NAME): a name starting like this is
# read as "how to use instagram", not an account, so it would test the parser, not ranking.
NOT_A_NAME = re.compile(
    r"^(how|what|why|when|where|is|can|does|do|my|download|delete|deactivate|"
    r"login|log in|log into|sign in|sign up|signup|create|open|reset|change|recover|hack|unblock|install|"
    r"update|private|desktop|web|app|dark mode|contact|customer|support|careers?|jobs?|stock|shares?)\b")
# The word people type for each platform (one the engine's account parser knows).
ACCOUNT_WORD = {"x": "twitter", "instagram": "instagram", "facebook": "facebook", "tiktok": "tiktok",
                "linkedin": "linkedin", "youtube": "youtube"}
SITE_TEMPLATES = ("{}", "{} official site", "{} website")
QWERTY = ["qwertyuiop", "asdfghjkl", "zxcvbnm"]


def norm_key(s: str) -> str:
    """search_api.norm_key: ASCII letters and digits, lowercase."""
    return "".join(_TOKEN.findall(s.lower()))


def h(*parts) -> str:
    return hashlib.md5((SEED + ":" + ":".join(map(str, parts))).encode()).hexdigest()


def split_of(entity: str) -> str:
    return "test" if int(h("split", entity)[:8], 16) % 2 else "dev"


def lit(xs) -> str:
    return "ARRAY[" + ",".join("'" + x.replace("'", "''") + "'" for x in xs) + "]::text[]" if xs else "ARRAY[]::text[]"


class DB:
    def __init__(self, host: str):
        self.host = host
        print("open-evalsets: LEGACY Postgres-bound (SEARCH-POSTGRES-EXIT.md phase 3: the release keeps no raw redirect "
              "titles or Wikidata ids); removed or re-sourced at phase 7", file=sys.stderr)

    def rows(self, sql: str) -> list[list[str]]:
        """Read-only psql on the serving box; the SQL travels on stdin."""
        cmd = ["ssh", "-o", "BatchMode=yes", self.host,
               "docker exec -i search-serve-postgres psql -U search -d search -v ON_ERROR_STOP=1 "
               "-At -F $'\\t' -c 'SET default_transaction_read_only = on' -f -"]
        r = subprocess.run(cmd, input=sql, capture_output=True, text=True, timeout=1800)
        if r.returncode:
            sys.exit(f"psql on {self.host} failed: {r.stderr.strip()[:500]}")
        out = r.stdout.splitlines()
        if out and out[0] == "SET":
            out = out[1:]
        return [line.split("\t") for line in out if line]


def clean(col: str) -> str:
    return f"translate(coalesce({col}, ''), E'\\t\\n\\r', '   ')"


def bucket(v: float, edges: list[tuple[float, str]]) -> str:
    for lo, name in edges:
        if v >= lo:
            return name
    return edges[-1][1]


def words_bucket(s: str) -> str:
    n = len(s.split())
    return "1" if n == 1 else "2" if n == 2 else "3+"


def take(cands: list[tuple], per: int, key_of) -> list[tuple]:
    """Up to `per` candidates per stratum, md5-ordered (deterministic), one per entity."""
    by = defaultdict(list)
    for c in cands:
        by[key_of(c)].append(c)
    out = []
    for k in sorted(by):
        seen = set()
        for c in sorted(by[k], key=lambda c: h("take", c[0], c[1])):
            if c[0] in seen:
                continue
            seen.add(c[0])
            out.append(c)
            if len(seen) >= per:
                break
    return out


def lev(a: str, b: str) -> int:
    if abs(len(a) - len(b)) > 3:
        return 9
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


# ---------------------------------------------------------------- Wikipedia redirects
def wiki_redirects(db: DB, per: int, ambiguous: list) -> list[dict]:
    n_pick = per * 12
    rows = db.rows(f"""
WITH s AS (
    SELECT wiki AS title, pop, CASE WHEN pop >= 0.6 THEN 'high' WHEN pop >= 0.45 THEN 'mid' ELSE 'low' END AS b
    FROM suggest_items WHERE kind = 'wiki' AND wiki IS NOT NULL),
pick AS (
    SELECT title, pop, b FROM (
        SELECT s.*, row_number() OVER (PARTITION BY b ORDER BY md5('{SEED}:' || title)) AS rn FROM s) x
    WHERE rn <= {n_pick})
SELECT p.b, d.page_id, {clean('d.title')}, {clean('d.aliases')} FROM pick p JOIN wiki_docs d ON d.title = p.title
WHERE d.aliases <> ''
UNION ALL
SELECT 'unranked', d.page_id, {clean('d.title')}, {clean('d.aliases')} FROM wiki_docs d
WHERE md5('{SEED}:' || d.title) < '04' AND d.aliases <> ''
  AND NOT EXISTS (SELECT 1 FROM suggest_items s WHERE s.wiki = d.title);
""")
    cands = []
    for b, page_id, title, aliases in rows:
        tk = norm_key(title)
        base = re.sub(r"\s*\(.*\)$", "", title)
        for a in aliases.split(" | "):
            a = " ".join(a.split())
            k = norm_key(a)
            if (not a or len(k) < 3 or k == tk or k == norm_key(base) and base != title
                    or re.search(r"[(),:/#\[\]{}|]", a) or len(a.split()) > 6 or a.isdigit()):
                continue
            # a redirect a few letters off the title is a misspelling redirect: its own stratum
            form = "spelling" if lev(k, tk) <= 2 else "alias"
            cands.append((tk, a, b, int(page_id), title, k, form))
    # the alias must name THIS article and nothing else (no other title, no other redirect)
    pages = defaultdict(set)
    for norm, pid in db.rows(f"SELECT norm, page_id FROM wiki_aliases WHERE norm = ANY({lit(sorted({c[5] for c in cands}))});"):
        pages[norm].add(int(pid))
    ok = []
    for c in cands:
        got = pages.get(c[5], set())
        if got == {c[3]}:
            ok.append(c)
        elif len(got) > 1:
            ambiguous.append((c[1], "wiki-redirect", len(got)))
    picked = take(ok, per, key_of=lambda c: (c[2], words_bucket(c[1])))
    return [{"query": a.lower(), "category": "wiki", "expected": title, "set": "wiki-redirect",
             "stratum": f"pop:{b},len:{words_bucket(a)},form:{form}", "entity": tk}
            for tk, a, b, _pid, title, _k, form in picked]


# ---------------------------------------------------------------- Wikidata entity keys
def entity_keys(db: DB, qids: set[str]) -> dict[str, str]:
    """qid -> normalised Wikipedia title (the split key every set shares), else the qid."""
    out = {}
    for qid, title in db.rows(f"""
SELECT DISTINCT ON (qid) qid, {clean('wiki_title')} FROM wiki_links
WHERE qid = ANY({lit(sorted(qids))}) AND wiki_title IS NOT NULL AND wiki_title <> '' ORDER BY qid, wiki_title;"""):
        out[qid] = norm_key(title)
    return {q: out.get(q) or q for q in qids}


def name_owners(db: DB, keys: set[str]) -> dict[str, int]:
    """How many Wikidata entities are known by each name key, anywhere in wiki_links."""
    return {k: int(n) for k, n in db.rows(
        f"SELECT key, count(DISTINCT qid) FROM wiki_links WHERE key = ANY({lit(sorted(keys))}) GROUP BY key;")}


SITELINK_EDGES = [(50, "50+"), (20, "20-49"), (5, "5-19"), (0, "1-4")]


QLEVER = "https://qlever.dev/api/wikidata"
P856_QUERY = """PREFIX wdt: <http://www.wikidata.org/prop/direct/> PREFIX wikibase: <http://wikiba.se/ontology#>
PREFIX schema: <http://schema.org/> PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
SELECT ?item ?sl ?article ?label ?site WHERE {
  ?item wdt:P856 ?site . ?item wikibase:sitelinks ?sl .
  ?article schema:about ?item ; schema:isPartOf <https://en.wikipedia.org/> .
  ?item rdfs:label ?label . FILTER(LANG(?label) = "en") }"""


def p856_rows(cache: Path) -> list[tuple[str, int, str, str, str]]:
    """Wikidata official websites (P856) of everything with an English article, from QLever
    (one request, cached: rerun from the cache for the same snapshot)."""
    if not cache.exists():
        import time
        import urllib.parse
        import urllib.request
        body = urllib.parse.urlencode({"query": P856_QUERY}).encode()
        for attempt in range(5):
            req = urllib.request.Request(QLEVER, data=body, headers={
                "Accept": "text/tab-separated-values", "User-Agent": "logout-search-evalsets/1"})
            try:
                with urllib.request.urlopen(req, timeout=900) as r:
                    cache.write_bytes(r.read())
                break
            except urllib.error.HTTPError as e:
                if e.code not in (429, 503):
                    raise
                time.sleep(int(e.headers.get("Retry-After") or 60 * (attempt + 1)))
        else:
            sys.exit("qlever.dev kept answering 429/503 - try later")
    out = []
    with open(cache, encoding="utf-8") as f:
        next(f)
        for line in f:
            item, sl, art, label, site = line.rstrip("\n").split("\t")
            label = label.rsplit("@", 1)[0][1:-1].replace('\\"', '"')   # "name"@en, quotes escaped
            title = unquote(art.strip("<>").rsplit("/wiki/", 1)[-1]).replace("_", " ")
            out.append((item.strip("<>").rsplit("/", 1)[-1], int(sl), title, label, site.strip("<>")))
    return out


def own_domain(url: str) -> str | None:
    """The domain an official-site URL names, or None when it is a page on a shared host
    (a Facebook page, a blogspot path) or deep inside someone else's site."""
    u = urlsplit(url if "://" in url else "https://" + url)
    host = (u.hostname or "").lower()
    if not host or any(host == x or host.endswith("." + x) for x in SHARED_HOSTS):
        return None
    path = u.path.strip("/").lower()
    if path and not re.fullmatch(r"[a-z]{2}(-[a-z]{2})?|index\.(html?|php)|home", path):
        return None
    return host[4:] if host.startswith("www.") else host


def sl_bucket(sl: int) -> str:
    return "50+" if sl >= 50 else "20-49" if sl >= 20 else "5-19" if sl >= 5 else "1-4"


def site_nav(db: DB, per: int, ambiguous: list, cache: Path) -> list[dict]:
    """Official sites: things with an English article (Wikidata P856 via QLever — not the
    engine's own wiki_sites table, which comes from infoboxes), plus a smaller stratum of
    things with no article (wiki_links platform=site, which the engine also serves)."""
    doms, meta = defaultdict(set), {}
    for qid, sl, title, label, site in p856_rows(cache):
        d = own_domain(site)
        if d:
            doms[qid].add(d)
            meta[qid] = (label, sl, norm_key(label), "yes", norm_key(title))
    rows = db.rows(f"""
SELECT qid, name, url, sitelinks FROM (
    SELECT qid, {clean('name')} AS name, {clean('url')} AS url, sitelinks,
           dense_rank() OVER (PARTITION BY sitelinks >= 5 ORDER BY md5('{SEED}:' || qid)) AS rn
    FROM wiki_links WHERE platform = 'site' AND key IS NOT NULL AND length(key) >= 3) x
WHERE rn <= {per * 6};
""")
    for qid, name, url, sl in rows:
        d = own_domain(url)
        if d and qid not in meta or (qid in meta and meta[qid][3] == "no" and d):
            doms[qid].add(d)
            meta[qid] = (name, int(sl), norm_key(name), "no", qid)
    # a name is ambiguous when more than one entity answers to it: another item in the P856
    # list, or an English title/redirect that is a different article
    by_key = defaultdict(set)
    for qid, m in meta.items():
        by_key[m[2]].add(qid)
    pre = []
    for qid, (name, sl, key, art, ent) in meta.items():
        if len(key) < 3 or len(name.split()) > 5 or NOT_A_NAME.match(name.lower()) or len(by_key[key]) > 1:
            if len(by_key[key]) > 1:
                ambiguous.append((name.lower(), "site-nav", len(by_key[key])))
            continue
        pre.append((ent, name, f"article:{art},sitelinks:{sl_bucket(sl)}", qid, key))
    # sample before the (expensive) alias check: 3x the stratum size is plenty
    pre = take(pre, per * 3, key_of=lambda c: c[2])
    keys = sorted({c[4] for c in pre})
    pages = defaultdict(set)
    for norm, title in db.rows(f"SELECT a.norm, {clean('d.title')} FROM wiki_aliases a JOIN wiki_docs d USING (page_id) "
                               f"WHERE a.norm = ANY({lit(keys)});"):
        pages[norm].add(norm_key(title))
    cands = []
    for ent, name, st, qid, key in pre:
        other = pages.get(key, set()) - {ent}
        if other:
            ambiguous.append((name.lower(), "site-nav", 1 + len(other)))
            continue
        cands.append((ent, name, st, qid, "|".join(sorted(doms[qid]))))
    picked = take(cands, per, key_of=lambda c: c[2])
    out = []
    for ent, name, st, qid, ds in picked:
        t = int(h("tmpl", qid)[:4], 16) % len(SITE_TEMPLATES)
        tmpl = ["bare", "official", "website"][t]
        out.append({"query": SITE_TEMPLATES[t].format(name).lower(), "category": "site", "expected": ds,
                    "set": "site-nav", "stratum": f"{st},template:{tmpl}", "entity": ent})
    return out


# ---------------------------------------------------------------- Wikidata accounts
def norm_url(u: str) -> str:
    s = urlsplit(u if "://" in u else "https://" + u)
    host = (s.hostname or "").lower()
    host = host[4:] if host.startswith("www.") else host
    return (host + s.path.rstrip("/")).lower()


ACCT_EDGES = [(30, "30+"), (10, "10-29"), (0, "1-9")]


def accounts(db: DB, per: int, ambiguous: list) -> list[dict]:
    n_pick = per * 6
    rows = db.rows(f"""
WITH s AS (
    SELECT CASE WHEN platform = 'linkedin-in' THEN 'linkedin' ELSE platform END AS p, qid,
           {clean('name')} AS name, {clean('url')} AS url, value, sitelinks, key,
           CASE WHEN sitelinks >= 30 THEN '30+' WHEN sitelinks >= 10 THEN '10-29' ELSE '1-9' END AS b
    FROM wiki_links
    WHERE platform IN ('x', 'instagram', 'facebook', 'tiktok', 'linkedin', 'linkedin-in', 'youtube')
      AND key IS NOT NULL AND length(key) >= 3)
SELECT p, qid, name, url, value, sitelinks, key, b FROM (
    SELECT s.*, dense_rank() OVER (PARTITION BY p, b ORDER BY md5('{SEED}:' || qid)) AS rn FROM s) x
WHERE rn <= {n_pick};
""")
    urls = defaultdict(set)
    meta = {}
    yt_ids = set()
    for p, qid, name, url, value, sl, key, b in rows:
        if p == "youtube":
            yt_ids.add(value)
            urls[(p, qid)].add(value)                    # channel id; resolved below
        else:
            urls[(p, qid)].add(norm_url(url))
        meta[(p, qid)] = (name, int(sl), key, b)
    # YouTube is answered from youtube_channels (url = @handle, else /channel/<id>): only
    # channels the engine holds are answerable.
    handles = {cid: hd for cid, hd in db.rows(
        f"SELECT channel_id, coalesce(handle, '') FROM youtube_channels WHERE channel_id = ANY({lit(sorted(yt_ids))});")}
    owners = name_owners(db, {m[2] for m in meta.values()})
    ents = entity_keys(db, {q for _, q in meta})
    cands = []
    for (p, qid), (name, sl, key, b) in meta.items():
        if len(name.split()) > 5 or NOT_A_NAME.match(name.lower()):
            continue
        if owners.get(key, 1) > 1:
            ambiguous.append((f"{name.lower()} {ACCOUNT_WORD[p]}", "account", owners[key]))
            continue
        exp = urls[(p, qid)]
        if p == "youtube":
            got = set()
            for cid in exp:
                if cid in handles:
                    got.add(f"youtube.com/channel/{cid}".lower())
                    if handles[cid]:
                        got.add(f"youtube.com/@{handles[cid]}".lower())
            if not got:
                continue
            exp = got
        cands.append((ents[qid], name, f"{p}|{b}", qid, "|".join(sorted(exp)), p, b))
    picked = take(cands, per, key_of=lambda c: c[2])
    return [{"query": f"{name} {ACCOUNT_WORD[p]}".lower(), "category": "account", "expected": exp,
             "set": "account", "stratum": f"platform:{p},sitelinks:{b}", "entity": ent}
            for ent, name, _s, _q, exp, p, b in picked]


# ---------------------------------------------------------------- OSM places
def kind_group(kind: str) -> str:
    k, _, v = kind.partition("=")
    if kind in ("amenity=restaurant", "amenity=fast_food", "amenity=cafe", "amenity=bar", "amenity=pub",
                "amenity=ice_cream"):
        return "food"
    if k == "shop":
        return "shop"
    if k in ("leisure", "tourism", "natural", "historic"):
        return "leisure"
    if k in ("amenity", "healthcare", "office", "craft"):
        return "service"
    return "other"


def places(db: DB, per: int, ambiguous: list) -> tuple[list[dict], list[dict]]:
    lat, lon = HOME
    dlat = 40 / 111.32
    dlon = dlat / max(0.1, __import__("math").cos(__import__("math").radians(lat)))
    rows = db.rows(f"""
WITH box AS (
    SELECT osm, {clean('name')} AS name, kind, brand, lat, lon, {clean('city')} AS city,
           regexp_replace(lower(name), '[^a-z0-9]', '', 'g') AS k,
           111.32 * sqrt(power(lat - {lat}, 2) + power((lon - {lon}) * cos(radians({lat})), 2)) AS d
    FROM places WHERE lat BETWEEN {lat} - {dlat} AND {lat} + {dlat} AND lon BETWEEN {lon} - {dlon} AND {lon} + {dlon}),
g AS (
    SELECT k, count(*) AS n,
           111.32 * sqrt(power(max(lat) - min(lat), 2) + power((max(lon) - min(lon)) * cos(radians({lat})), 2)) AS spread
    FROM box WHERE d <= 40 GROUP BY k),
br AS (SELECT DISTINCT regexp_replace(lower(brand), '[^a-z0-9]', '', 'g') AS k FROM box WHERE brand <> '' AND d <= 40)
SELECT b.osm, b.name, b.kind, b.city, b.lat, b.lon, g.n, g.spread FROM box b JOIN g USING (k)
WHERE b.osm NOT LIKE 'wdc:%' AND b.d <= {PLACE_R_KM} AND coalesce(b.brand, '') = '' AND length(b.k) >= 4
  AND NOT EXISTS (SELECT 1 FROM br WHERE br.k = b.k);
""")
    cands = []
    for osm, name, kind, city, la, lo, n, spread in rows:
        name = " ".join(name.split())
        if float(spread) > PLACE_SAME_KM:
            ambiguous.append((name.lower(), "place", int(n)))
            continue
        if len(name.split()) > 4 or NOT_A_NAME.match(name.lower()) or "|" in name or "@" in name:
            continue                                       # the engine's place parser takes <= 4 words
        cands.append((osm, name, f"{kind_group(kind)}|{words_bucket(name)}", city, float(la), float(lo), kind))
    picked = take(cands, per, key_of=lambda c: c[2])
    bare, town = [], []
    for osm, name, s, city, la, lo, kind in picked:
        g, w = s.split("|")
        exp = f"{osm}@{la:.5f},{lo:.5f}@{name}"     # id, point, name: a business-listing twin counts too
        bare.append({"query": name.lower(), "category": "place", "expected": exp, "set": "place",
                     "stratum": f"kind:{g},len:{w}", "entity": osm, "_name": name})
        if city and norm_key(city) not in norm_key(name):
            town.append({"query": f"{name} {city}".lower(), "category": "place", "expected": exp,
                         "set": "place-town", "stratum": f"kind:{g},len:{w}", "entity": osm, "_name": name})
    return bare, town


# ---------------------------------------------------------------- typos
def misspell(q: str, salt: str) -> str | None:
    """One human-looking slip in the longest word of 5+ letters: drop, swap, double or a
    neighbouring key — deterministic per query."""
    words = q.split()
    idx = max(range(len(words)), key=lambda i: (len(words[i]), -i))
    w = words[idx]
    if len(w) < 5 or not w.isalpha() or not w.isascii():
        return None
    r = int(h("typo", salt, q)[:8], 16)
    i = 1 + r % (len(w) - 2)                             # never the first letter: people rarely slip there
    op = (r >> 8) % 4
    if op == 0:
        t = w[:i] + w[i + 1:]
    elif op == 1:
        t = w[:i] + w[i + 1] + w[i] + w[i + 2:] if i + 1 < len(w) else w[:i] + w[i + 1:]
    elif op == 2:
        t = w[:i] + w[i] + w[i:]
    else:
        row = next((rw for rw in QWERTY if w[i] in rw), None)
        if not row:
            return None
        j = row.index(w[i])
        nb = [row[x] for x in (j - 1, j + 1) if 0 <= x < len(row)]
        t = w[:i] + nb[(r >> 12) % len(nb)] + w[i + 1:]
    if t == w:
        return None
    words[idx] = t
    return " ".join(words)


def typos(db: DB, sources: dict[str, list[dict]], per_set: int) -> list[dict]:
    cands = []
    for name, rows in sources.items():
        for r in sorted(rows, key=lambda r: h("typo-pick", r["query"]))[: per_set * 2]:
            base = r.get("_name", "").lower() or r["query"]
            if r["set"] == "site-nav":
                base = r["query"].replace(" official site", "").replace(" website", "")
            t = misspell(base, name)
            if t and norm_key(t) != norm_key(base):
                cands.append((name, r, t))
    # a "typo" that is itself a real name (a redirect, a Wikidata name, a nearby place)
    # is not a typo test
    keys = sorted({norm_key(t) for _, _, t in cands})
    real = {k for (k,) in db.rows(f"SELECT DISTINCT norm FROM wiki_aliases WHERE norm = ANY({lit(keys)});")}
    real |= {k for (k,) in db.rows(f"SELECT DISTINCT key FROM wiki_links WHERE key = ANY({lit(keys)});")}
    real |= {k for (k,) in db.rows(
        f"SELECT DISTINCT regexp_replace(lower(name), '[^a-z0-9]', '', 'g') FROM places "
        f"WHERE regexp_replace(lower(name), '[^a-z0-9]', '', 'g') = ANY({lit(keys)});")}
    out, n = [], Counter()
    for name, r, t in cands:
        if norm_key(t) in real or n[name] >= per_set:
            continue
        n[name] += 1
        out.append(dict(r, query=t, set=f"typo-{name}", stratum=r["stratum"].split(",")[0]))
    return out


LICENCE = {"wiki-redirect": "CC BY-SA 4.0 (Wikipedia redirects)", "site-nav": "CC0 (Wikidata P856)",
           "account": "CC0 (Wikidata P2002/P2003/P2013/P7085/P4264/P6634/P2397)",
           "place": "ODbL 1.0 (OpenStreetMap contributors)", "place-town": "ODbL 1.0 (OpenStreetMap contributors)",
           "typo-wiki": "CC BY-SA 4.0 (Wikipedia redirects)", "typo-site": "CC0 (Wikidata P856)",
           "typo-place": "ODbL 1.0 (OpenStreetMap contributors)"}
COLS = ("query", "category", "expected", "set", "stratum", "split", "entity")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="deploy", help="ssh host of the serving box (read-only queries)")
    ap.add_argument("--out", default=str(Path(__file__).parent / "evalsets" / "open"))
    ap.add_argument("--p856", default=str(Path.home() / ".cache" / "logout" / "p856-enwiki.tsv"),
                    help="QLever P856 download (fetched once if missing; keep it to reproduce a run)")
    ap.add_argument("--scale", type=float, default=1.0, help="multiply every per-stratum size (training pools: 10+)")
    a = ap.parse_args()
    db = DB(a.host)
    s = a.scale
    amb: list = []
    sets: dict[str, list[dict]] = {}
    print("wiki-redirect …", file=sys.stderr)
    sets["wiki-redirect"] = wiki_redirects(db, int(200 * s), amb)
    print("site-nav …", file=sys.stderr)
    sets["site-nav"] = site_nav(db, int(250 * s), amb, Path(a.p856))
    print("account …", file=sys.stderr)
    sets["account"] = accounts(db, int(100 * s), amb)
    print("place …", file=sys.stderr)
    sets["place"], sets["place-town"] = places(db, int(150 * s), amb)
    print("typos …", file=sys.stderr)
    for r in typos(db, {"wiki": sets["wiki-redirect"], "site": sets["site-nav"], "place": sets["place"]}, int(500 * s)):
        sets.setdefault(r["set"], []).append(r)

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    seen = set()
    for name, rows in sets.items():
        kept = []
        for r in rows:
            if r["query"] in seen:                       # one query, one label across all sets
                continue
            seen.add(r["query"])
            r["split"] = split_of(r["entity"])
            kept.append(r)
        with open(out / f"{name}.tsv", "w", encoding="utf-8") as f:
            f.write("\t".join(COLS) + "\n")
            for r in sorted(kept, key=lambda r: (r["split"], r["stratum"], r["query"])):
                f.write("\t".join(str(r[c]) for c in COLS) + "\n")
        sp = Counter(r["split"] for r in kept)
        print(f"{name:<14} {len(kept):>6}  dev {sp['dev']:>5}  test {sp['test']:>5}  {LICENCE[name]}", file=sys.stderr)
    # one file per source, so each keeps its own licence
    for st in sorted({x[1] for x in amb}):
        with open(out / f"ambiguous-{st}.tsv", "w", encoding="utf-8") as f:
            f.write("query\tset\tentities\n")
            for q, s_, n in sorted({x for x in amb if x[1] == st}):
                f.write(f"{q}\t{s_}\t{n}\n")
    print(f"ambiguous (not scored): {len(set(amb))}", file=sys.stderr)


if __name__ == "__main__":
    main()
