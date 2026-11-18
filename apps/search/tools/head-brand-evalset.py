#!/usr/bin/env python3
"""Head-brand eval set (step 2): "type a well-known site's name, get that site". No Tranco,
no database, no history: Wikidata (CC0) through QLever and Wikipedia pageviews (CC0) through the Wikimedia REST API.

    tools/head-brand-evalset.py [--out tools/evalsets/open] [--n 1000] [--pool 5000] [--month 2026-09]

Why it exists: the open sets sample the long tail of official sites and passed rc4, while own rank sent "chess"
to chess.az and ranked gog.com 24,498. The 635-name probe that showed it is Tranco-derived (dev measurement only).
This set is the permanent one.

A row is an English Wikipedia article about an organisation, a website or a service that names its own site:

  site      the item's official website (P856) is a FRONT PAGE (open-evalsets.own_domain's path rule)
  name      the article title, without a trailing "(...)", names the host: its letters and digits equal the host's
            first label ("Reddit" -> reddit.com, "Telegram (software)" -> telegram.org) or the whole host
            ("GOG.com" -> gog.com, "Chess.com" -> chess.com)
  kind      the item is an organisation by importer/wikidata_sites.py's rule (ORG_TYPES, exact or through
            subclass-of) and no person, place or band (NOT_SITE_TYPES). Without it the most viewed "brands" were
            73 film sites ("spider-man: brand new day" -> a .movie site), and "Cadiz" -> cadiz.es is a name too:
            for those the article is the answer
  head      the --pool most-linked candidates (Wikidata sitelinks) get their pageviews of --month; the --n most
            viewed are the set

LIMIT: the kind rule is the one the own-rank recipe credits. The set shows whether the brands the recipe means
to credit end up first; it cannot show a brand the type rule misses (that takes a set from another source, e.g.
the Tranco-derived 635-name probe, which is dev-only). A row whose site is on the engine's block lists (adult
sites are among the most viewed articles) scores zero in every arm.

Query: the title without "(...)", lowercased; for a title that is the host itself ("GOG.com") the first label
("gog"), which is what people type. Stratum name:shared marks a query that is also the exact title of ANOTHER
article ("chess", "telegram"): there the site is one right answer, not the only one. Score both strata, report
them apart. Names two rows share go to ambiguous-head-brand.tsv.

Split and columns are open-evalsets.py's (seed 402, by normalised article title): an entity is on the same side
in every set. Output: head-brand.tsv (query, category, expected, set, stratum, split, entity).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

SEED = "402"                                    # open-evalsets.SEED: the same split in every set
QLEVER = "https://qlever.dev/api/wikidata"
PAGEVIEWS = "https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/en.wikipedia/all-access/user"
# Wikimedia throttles a client that names no contact (429 on the second request, 2026-10-06): the public site is ours
USER_AGENT = "logout-search-evalsets/1 (https://search.logout.cloud)"


def importer_types(name: str) -> list[str]:
    """ORG_TYPES / NOT_SITE_TYPES as importer/wikidata_sites.py writes them (read as text: importing it needs the
    importer's dependencies). One list, kept there."""
    src = (Path(__file__).resolve().parent.parent / "importer" / "wikidata_sites.py").read_text(encoding="utf-8")
    m = re.search(r'^%s = "{1,3}([^"]+)"{1,3}\.split\(\)' % name, src, re.M)
    if not m:
        sys.exit(f"importer/wikidata_sites.py has no {name} list")
    return m.group(1).split()


ORG_TYPES, NOT_SITE_TYPES = importer_types("ORG_TYPES"), importer_types("NOT_SITE_TYPES")
COLS = ("query", "category", "expected", "set", "stratum", "split", "entity")
_TOKEN = re.compile(r"[a-z0-9]+")

CANDIDATE_QUERY = """PREFIX wdt: <http://www.wikidata.org/prop/direct/>
PREFIX wd: <http://www.wikidata.org/entity/>
PREFIX schema: <http://schema.org/>
PREFIX wikibase: <http://wikiba.se/ontology#>
SELECT ?article ?site (MAX(?sl) AS ?links) (MAX(?isorg) AS ?org) (MAX(?isnot) AS ?notsite) WHERE {
  ?item wdt:P856 ?site ; wikibase:sitelinks ?sl .
  ?article schema:about ?item ; schema:isPartOf <https://en.wikipedia.org/> .
  OPTIONAL { ?item wdt:P31/wdt:P279* ?u . VALUES ?u { %s } BIND(1 AS ?isorg) }
  OPTIONAL { ?item wdt:P31/wdt:P279* ?n . VALUES ?n { %s } BIND(1 AS ?isnot) }
} GROUP BY ?article ?site""" % (" ".join("wd:" + q for q in ORG_TYPES), " ".join("wd:" + q for q in NOT_SITE_TYPES))


def log(msg: str) -> None:
    print(f"[head-brand] {msg}", file=sys.stderr, flush=True)


def norm_key(s: str) -> str:
    """search_api.norm_key: ASCII letters and digits, lowercase."""
    return "".join(_TOKEN.findall(s.lower()))


def split_of(entity: str) -> str:
    """open-evalsets.split_of, verbatim."""
    return "test" if int(hashlib.md5(f"{SEED}:split:{entity}".encode()).hexdigest()[:8], 16) % 2 else "dev"


def front_host(url: str) -> str | None:
    """The host a front-page URL names (www. dropped), or None for a deep link. open-evalsets.own_domain's path
    rule, without its shared-host list: here the article IS the host's own (wikipedia.org for "Wikipedia")."""
    u = urllib.parse.urlsplit(url if "://" in url else "https://" + url)
    host = (u.hostname or "").lower()
    path = u.path.strip("/").lower()
    if not host or "." not in host or u.query or u.fragment:
        return None
    if path and not re.fullmatch(r"[a-z]{2}(-[a-z]{2})?|index\.(html?|php)|home", path):
        return None
    return host[4:] if host.startswith("www.") else host


def plain_title(title: str) -> str:
    return re.sub(r"\s*\([^()]*\)$", "", title).strip()


def name_query(title: str, host: str) -> tuple[str, str] | None:
    """(query, template) when the title names the host, else None. "Reddit"/reddit.com -> ("reddit", "title");
    "GOG.com"/gog.com -> ("gog", "label"); "Italy"/italia.it -> None."""
    plain = plain_title(title)
    key, label = norm_key(plain), host.split(".")[0]
    if len(key) < 2:
        return None
    if key == norm_key(label):
        return plain.lower(), "title"
    if key == norm_key(host):
        return label, "label"
    return None


def _flag(v: str) -> bool:
    return v.strip('"').split("^^")[0].strip('"') == "1"


def candidates(lines) -> tuple[list[tuple[str, str, str, str, int]], list[tuple[str, int]]]:
    """QLever TSV (article, site, links, org, notsite) -> ([(title, hosts, query, template, sitelinks)], ambiguous).
    One row per article. hosts: every front page of its item that the title names, "|"-joined in byte order
    (wikipedia.org and wikipedia.com are both Wikipedia's; a result on either is a hit). The query comes from the
    shortest of them (then the first in byte order). A query two articles share is ambiguous and dropped."""
    per: dict[str, dict] = {}
    for line in lines:
        f = line.rstrip("\n").split("\t")
        if len(f) < 5 or f[0].startswith("?"):
            continue
        art = f[0].strip("<>")
        if "/wiki/" not in art:
            continue
        title = urllib.parse.unquote(art.rsplit("/wiki/", 1)[-1]).replace("_", " ")
        e = per.setdefault(title, {"org": False, "no": False, "sl": 0, "hosts": {}})
        e["org"] = e["org"] or _flag(f[3])
        e["no"] = e["no"] or _flag(f[4])
        try:
            e["sl"] = max(e["sl"], int(f[2].strip('"').split("^^")[0].strip('"') or 0))
        except ValueError:
            pass
        host = front_host(f[1].strip("<>"))
        got = name_query(title, host) if host else None
        if got:
            e["hosts"][host] = got
    by_query = defaultdict(list)
    for title, e in per.items():
        if not e["org"] or e["no"] or not e["hosts"]:
            continue
        first = min(e["hosts"], key=lambda h: (len(h), h.encode("utf-8")))
        q, tmpl = e["hosts"][first]
        by_query[q].append((title, "|".join(sorted(e["hosts"], key=lambda h: h.encode("utf-8"))), q, tmpl, e["sl"]))
    rows, ambiguous = [], []
    for q, group in by_query.items():
        if len(group) > 1:
            ambiguous.append((q, len(group)))
        else:
            rows.append(group[0])
    rows.sort(key=lambda r: (-r[4], r[0].encode("utf-8")))
    return rows, sorted(ambiguous)


def _get(url: str, data: bytes | None = None, accept: str = "application/json", timeout: int = 900) -> bytes:
    for attempt in range(5):
        req = urllib.request.Request(url, data=data, headers={"User-Agent": USER_AGENT, "Accept": accept})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return b""
            if e.code not in (429, 503):
                raise
            time.sleep(int(e.headers.get("Retry-After") or 30 * (attempt + 1)))
    sys.exit(f"{url.split('?')[0]} kept answering 429/503 - try later")


def qlever(query: str, cache: Path) -> list[str]:
    if not cache.exists():
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_bytes(_get(QLEVER, urllib.parse.urlencode({"query": query}).encode(), "text/tab-separated-values"))
    return cache.read_text(encoding="utf-8").splitlines()


def month_views(title: str, month: str) -> int:
    """User pageviews of the English article in YYYY-MM; 0 when the API has no row."""
    y, m = month.split("-")
    last = {"02": "28", "04": "30", "06": "30", "09": "30", "11": "30"}.get(m, "31")
    body = _get(f"{PAGEVIEWS}/{urllib.parse.quote(title.replace(' ', '_'), safe='')}/monthly/{y}{m}0100/{y}{m}{last}00", timeout=60)
    return sum(i.get("views", 0) for i in json.loads(body).get("items", [])) if body else 0


def pageviews(titles: list[str], month: str, cache: Path, workers: int = 4) -> dict[str, int]:
    have = {}
    if cache.exists():
        for line in cache.read_text(encoding="utf-8").splitlines():
            t, v = line.rsplit("\t", 1)
            have[t] = int(v)
    need = [t for t in titles if t not in have]
    if need:
        log(f"pageviews {month}: {len(need):,} articles to fetch ({len(have):,} cached)")
        with ThreadPoolExecutor(workers) as ex, open(cache, "a", encoding="utf-8") as o:
            for i, (t, v) in enumerate(zip(need, ex.map(lambda t: month_views(t, month), need)), 1):
                have[t] = v
                o.write(f"{t}\t{v}\n")
                if i % 500 == 0:
                    o.flush()                     # a stopped run keeps what it fetched
                    log(f"pageviews: {i:,} of {len(need):,}")
    return {t: have[t] for t in titles}


def other_titles(queries: dict[str, str], cache: Path) -> set[str]:
    """The queries that are also the exact title of an article other than their own ({query: own title})."""
    def cap(q: str) -> str:
        return (q[:1].upper() + q[1:]).replace(" ", "_")
    ask = sorted(q for q, own in queries.items() if cap(q).replace("_", " ") != own)
    if not ask:
        return set()
    iris = " ".join(f"<https://en.wikipedia.org/wiki/{urllib.parse.quote(cap(q), safe='_')}>" for q in ask)
    # the answer belongs to this list of names: a cache keyed by anything less answered for another head (2026-10-06:
    # "chess" read as name:only from the list of an earlier run)
    cache = cache.with_name(f"{cache.stem}-{hashlib.md5(iris.encode()).hexdigest()[:12]}{cache.suffix}")
    lines = qlever("PREFIX schema: <http://schema.org/>\nSELECT ?a WHERE { VALUES ?a { %s } ?a schema:about ?i ; "
                   "schema:isPartOf <https://en.wikipedia.org/> }" % iris, cache)
    found = {urllib.parse.unquote(ln.strip().strip("<>").rsplit("/wiki/", 1)[-1]) for ln in lines if "/wiki/" in ln}
    return {q for q in ask if cap(q) in found}


def build(rows, views: dict[str, int], shared: set[str], n: int) -> list[dict]:
    head = sorted(rows, key=lambda r: (-views.get(r[0], 0), r[0].encode("utf-8")))[:n]
    out = []
    for i, (title, host, q, tmpl, _sl) in enumerate(head):
        band = "1-100" if i < 100 else "101-300" if i < 300 else "301+"
        ent = norm_key(title)
        out.append({"query": q, "category": "site", "expected": host, "set": "head-brand",
                    "stratum": f"views-rank:{band},template:{tmpl},name:{'shared' if q in shared else 'only'}",
                    "split": split_of(ent), "entity": ent})
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", default=str(Path(__file__).parent / "evalsets" / "open"))
    ap.add_argument("--n", type=int, default=1000, help="rows in the set (the most viewed of the pool)")
    ap.add_argument("--pool", type=int, default=5000, help="most-linked candidates whose pageviews are fetched")
    ap.add_argument("--month", default="2026-09", help="pageview month, YYYY-MM")
    ap.add_argument("--cache", default=str(Path.home() / ".cache" / "logout"), help="QLever and pageview answers (delete to refresh)")
    a = ap.parse_args()
    cache, out = Path(a.cache), Path(a.out)
    rows, ambiguous = candidates(qlever(CANDIDATE_QUERY, cache / "head-brand-candidates-org.tsv"))
    log(f"{len(rows):,} articles name their own front page; {len(ambiguous):,} names shared by two of them")
    pool = rows[:a.pool]
    views = pageviews([r[0] for r in pool], a.month, cache / f"head-brand-pageviews-{a.month}.tsv")
    head = sorted(pool, key=lambda r: (-views[r[0]], r[0].encode("utf-8")))[:a.n]
    shared = other_titles({r[2]: r[0] for r in head}, cache / "head-brand-titles.tsv")
    res = build(pool, views, shared, a.n)
    out.mkdir(parents=True, exist_ok=True)
    with open(out / "head-brand.tsv", "w", encoding="utf-8") as f:
        f.write("\t".join(COLS) + "\n")
        for r in res:
            f.write("\t".join(str(r[c]) for c in COLS) + "\n")
    with open(out / "ambiguous-head-brand.tsv", "w", encoding="utf-8") as f:
        f.write("query\tset\tentities\n")
        for q, k in ambiguous:
            f.write(f"{q}\thead-brand\t{k}\n")
    by = defaultdict(int)
    for r in res:
        by[r["split"], r["stratum"].split(",")[2]] += 1
    log(f"wrote {len(res):,} rows: " + ", ".join(f"{s} {n_} {c}" for (s, n_), c in sorted(by.items())))


if __name__ == "__main__":
    main()
