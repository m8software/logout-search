"""Official websites that live only in Wikidata (P856) — the gap wikisites.py cannot see.

Many articles write {{Official URL}} with no argument: the site is in Wikidata. One
SPARQL query to QLever's public Wikidata endpoint (it answers million-row queries;
query.wikidata.org times out) returns every English Wikipedia article whose item has
an official website — 834,366 on 2026-09-24 — plus whether the item is an
organisation, which decides where search-api ranks the site. Run AFTER wiki-sites:
this only fills articles the wikitext pass found nothing for.
"""

from __future__ import annotations

import time
import urllib.parse
import urllib.request

from wikisites import normalize

ENDPOINT = "https://qlever.dev/api/wikidata"
USER_AGENT = "logout-search-import/1 (personal search index)"

# instance-of values that make the article's subject the site's owner (a company, a
# website, an airline …) — same idea as search-api's _ORG_BOX for infobox kinds.
ORG_TYPES = """Q4830453 Q783794 Q891723 Q6881511 Q35127 Q43229 Q46970 Q22687 Q3918 Q163740 Q507619
Q1331793 Q11032 Q41298 Q2085381 Q15265344 Q327333 Q7278 Q476028 Q12973014 Q1058914 Q167037
Q18127 Q2001305 Q1664720 Q7075 Q31855 Q902104 Q4671277 Q11691 Q210167 Q3152824
Q3220391 Q193424 Q1668024 Q620615 Q166142""".split()
# (last row, 2026-09-24: social networking service, web service, service on internet,
#  mobile app, application — "instagram" must put instagram.com first, not under the article)

# (rc4, 2026-10-06): the list above was matched EXACTLY against the item's instance-of, so an item typed
# "dot-com company" (GOG.com), "video streaming service" (Prime Video), "cable channel" (The Weather Channel) or
# "Wikimedia content project" (Wikipedia) was no organisation, its site got none of the article's pageviews or
# sitelinks in own_rank.py, and a namesake led (gog.com at own rank 24,498). The same types are now also reached
# through subclass-of. That alone is wrong: "sovereign state" and "municipality" are subclasses of organisation,
# and the official site of Italy or Cadiz is a tourism or city site that borrows the place's fame (own-rank audit
# 2026-10-04). So an item that is a person, a place or a band (NOT_SITE_TYPES, also through subclass-of) is never an
# organisation by the walk. An exact match still counts as before, so no article loses what it had.
# Measured on QLever 2026-10-06, 784,777 articles with an official website: 134,798 organisations by the exact
# list, 307,601 by the walk without the excluded kinds (173,269 gained), 308,067 with the exact matches kept.
NOT_SITE_TYPES = "Q5 Q56061 Q1048835 Q486972 Q6256 Q15642541 Q215380 Q11074464".split()
# human, administrative territorial entity, political territorial entity, human settlement, country,
# human-geographic territorial entity, musical group, pornography website.
# The last one (42 articles through subclass-of, 2026-10-07): an adult site the block lists do not carry is in the
# release without a junk flag (motherless.xxx, rc4). Credited with its article's fame it went from own rank
# 1,652,633 to 143 (rc5 step 0, first run): "popular", first for its name, in suggestions. Its article does not lift it.

QUERY = """PREFIX wdt: <http://www.wikidata.org/prop/direct/>
PREFIX wd: <http://www.wikidata.org/entity/>
PREFIX schema: <http://schema.org/>
SELECT ?article ?site (MAX(?isorg) AS ?org) (MAX(?iswalk) AS ?walk) (MAX(?isnot) AS ?notsite) WHERE {
  ?item wdt:P856 ?site .
  ?article schema:about ?item ; schema:isPartOf <https://en.wikipedia.org/> .
  OPTIONAL { ?item wdt:P31 ?t . VALUES ?t { %(org)s } BIND(1 AS ?isorg) }
  OPTIONAL { ?item wdt:P31/wdt:P279* ?u . VALUES ?u { %(org)s } BIND(1 AS ?iswalk) }
  OPTIONAL { ?item wdt:P31/wdt:P279* ?n . VALUES ?n { %(no)s } BIND(1 AS ?isnot) }
} GROUP BY ?article ?site""" % {"org": " ".join("wd:" + q for q in ORG_TYPES), "no": " ".join("wd:" + q for q in NOT_SITE_TYPES)}


def log(msg: str) -> None:
    print(f"[wikidata-sites] {msg}", flush=True)


def title_of(article_iri: str) -> str | None:
    """<https://en.wikipedia.org/wiki/Costco> -> 'Costco' (wiki_docs.title form)."""
    s = article_iri.strip("<>")
    prefix = "https://en.wikipedia.org/wiki/"
    if not s.startswith(prefix):
        return None
    return urllib.parse.unquote(s[len(prefix):]).replace("_", " ")


def _flag(parts: list[str], i: int) -> bool:
    return len(parts) > i and parts[i].strip('"').split("^^")[0].strip('"') == "1"


def _front(url: str) -> bool:
    """https://www.gog.com/ is a front page; https://www.amazon.co.jp/gp/video/storefront is not."""
    rest = url.split("://", 1)[-1]
    return rest.partition("/")[2].strip("/") == "" and "?" not in rest and "#" not in rest


def _own_front(url: str, domain: str) -> bool:
    """The front page of the registrable domain itself: https://www.chess.com/ for chess.com. Not a deep link
    (chess.com/news/view/...), not a host under it (translations.telegram.org for telegram.org)."""
    if not url or not domain or not _front(url):
        return False
    host = url.split("://", 1)[-1].partition("/")[0].partition("?")[0].partition("#")[0].lower()
    return host in (domain, "www." + domain)


def better_row(cur_url: str, cur_domain: str, cur_box: str, url: str, domain: str, org: bool) -> tuple[str, str, str] | None:
    """The (url, domain, box) an existing wiki_sites row should have instead, or None to leave it., read in
    the rc4 input 2026-10-07: Chess.com's row was a news page on chess.com picked from its infobox, Telegram's was
    translations.telegram.org, Prime Video's an amazon.co.jp storefront. own_rank.official_key credits an article's
    fame only to a front page, so chess.com, telegram.org and primevideo.com got none. A row is replaced only when
    Wikidata names the front page of a domain itself AND the row is not already such a front page: an infobox that
    names a site's front page always wins over Wikidata. An infobox row keeps its box (the infobox type is what says
    the article is an organisation); a row this pass wrote takes Wikidata's verdict."""
    ours = (cur_box or "").startswith("wikidata") or not cur_box        # a row an earlier run of this pass wrote
    if not _own_front(url, domain) or url == cur_url:
        return None
    if _own_front(cur_url or "", cur_domain or "") and not ours:        # an infobox front page always wins
        return None
    # ours: the earlier run's choice has no authority (it took google.be for "Google Search"); today's _pick decides
    box = ("wikidata organization" if org else "wikidata") if ours else cur_box
    return url, domain, box


def _pick(site: tuple) -> tuple:
    """Which of an item's official sites stands for the article; the greater key wins. The domain's own front page
    (telegram.org before desktop.telegram.org), then any front page, then https. 5,549 items list front pages on
    several DOMAINS ("Google Search": google.be, google.ca, google.com and twelve more; QLever's order made
    google.be the site and gave it the article's fame, own rank 39 against google.com's 65, rc5 step 0, 2026-10-07).
    For those: a generic top-level domain before a country one (the index is English; a two-letter ending is a
    country's), then the shorter address, then byte order, so the choice never depends on the order of the answer."""
    url, dom = site[0], site[1]
    return (_own_front(url, dom), _front(url), url.startswith("https"), len(dom.rsplit(".", 1)[-1]) != 2,
            -len(url), tuple(-b for b in url.encode("utf-8")))


def parse_tsv(lines) -> dict[str, tuple[str, str, bool]]:
    """QLever TSV -> {title: (url, domain, is_org)}; one site per article: a front page before a deep link
    (own_rank.official_key credits only a front page; Prime Video lists primevideo.com and an amazon.co.jp
    storefront), then https before http, then the first seen. is_org: the item's type is in ORG_TYPES, or reaches
    one through subclass-of and is no person, place or band (columns ?org ?walk ?notsite; a TSV with only ?org
    reads as before)."""
    best: dict[str, tuple[str, str, bool]] = {}
    for line in lines:
        parts = line.rstrip("\n").split("\t")
        if len(parts) < 2 or parts[0].startswith("?"):
            continue
        title = title_of(parts[0])
        got = normalize(parts[1].strip("<>"), title) if title else None
        if not got:
            continue
        org = _flag(parts, 2) or (_flag(parts, 3) and not _flag(parts, 4))
        cur = best.get(title)
        if cur is None or _pick(got) > _pick(cur):
            best[title] = (got[0], got[1], org or (cur[2] if cur else False))
    return best


def cmd_wikidata_sites(conn) -> None:
    t0 = time.time()
    body = urllib.parse.urlencode({"query": QUERY}).encode()
    req = urllib.request.Request(ENDPOINT, data=body, headers={
        "User-Agent": USER_AGENT, "Accept": "text/tab-separated-values"})
    with urllib.request.urlopen(req, timeout=900) as r:
        lines = r.read().decode("utf-8", "replace").splitlines()
    sites = parse_tsv(lines)
    log(f"{len(lines) - 1:,} rows from QLever -> {len(sites):,} articles with a usable site "
        f"({sum(v[2] for v in sites.values()):,} organisations) in {time.time() - t0:.0f}s")
    conn.execute("CREATE TEMP TABLE wd (title text, url text, domain text, org boolean)")
    with conn.cursor().copy("COPY wd (title, url, domain, org) FROM STDIN") as cp:
        for title, (url, dom, org) in sites.items():
            cp.write_row((title, url, dom, org))
    with conn.transaction():
        n = conn.execute(
            "INSERT INTO wiki_sites (page_id, url, domain, box, tranco_rank, history_visits) "
            "SELECT DISTINCT ON (w.page_id) w.page_id, wd.url, wd.domain, "
            "       CASE WHEN wd.org THEN 'wikidata organization' ELSE 'wikidata' END, "
            "       s.tranco_rank, coalesce(s.history_visits, 0) "
            "FROM wd JOIN wiki_docs w ON w.title = wd.title "
            "LEFT JOIN sites s ON s.domain = wd.domain "
            "WHERE s.blocked_by IS NULL "
            "  AND NOT EXISTS (SELECT 1 FROM wiki_sites x WHERE x.page_id = w.page_id) "
            "ORDER BY w.page_id").rowcount
        # Rows this pass added earlier as plain 'wikidata' become organisations when the
        # type list grows (Thingiverse, Instagram-like services).
        up = conn.execute(
            "UPDATE wiki_sites ws SET box = 'wikidata organization' FROM wd JOIN wiki_docs w ON w.title = wd.title "
            "WHERE ws.page_id = w.page_id AND ws.box = 'wikidata' AND wd.org").rowcount
        # ...and the other way when the exclusions grow (an adult site, 2026-10-07): a row this pass wrote takes
        # today's verdict, it does not keep one an earlier run gave it
        down = conn.execute(
            "UPDATE wiki_sites ws SET box = 'wikidata' FROM wd JOIN wiki_docs w ON w.title = wd.title "
            "WHERE ws.page_id = w.page_id AND ws.box = 'wikidata organization' AND NOT wd.org").rowcount
        # Rows that name a deep link or a host under the site, when Wikidata names the site's front page (better_row)
        have = conn.execute(
            "SELECT ws.page_id, ws.url, ws.domain, ws.box, wd.url, wd.domain, wd.org "
            "FROM wd JOIN wiki_docs w ON w.title = wd.title JOIN wiki_sites ws ON ws.page_id = w.page_id "
            "LEFT JOIN sites s ON s.domain = wd.domain "
            "WHERE s.blocked_by IS NULL AND ws.url IS DISTINCT FROM wd.url").fetchall()
        fixes = [(pid, *new) for pid, cu, cd, cb, u, d, o in have if (new := better_row(cu, cd, cb, u, d, bool(o)))]
        conn.execute("CREATE TEMP TABLE wd_fix (page_id integer PRIMARY KEY, url text, domain text, box text) ON COMMIT DROP")
        with conn.cursor().copy("COPY wd_fix (page_id, url, domain, box) FROM STDIN") as cp:
            for row in {f[0]: f for f in fixes}.values():
                cp.write_row(row)
        fixed = conn.execute(
            "UPDATE wiki_sites ws SET url = f.url, domain = f.domain, box = f.box, "
            "       tranco_rank = s.tranco_rank, history_visits = coalesce(s.history_visits, 0) "
            "FROM wd_fix f LEFT JOIN sites s ON s.domain = f.domain WHERE ws.page_id = f.page_id").rowcount
    log(f"{up:,} earlier Wikidata rows are organisations now, {down:,} no longer; {fixed:,} rows now name the site's front page "
        f"(of {len(have):,} where Wikidata names another address)")
    total = conn.execute("SELECT count(*) FROM wiki_sites").fetchone()[0]
    log(f"added {n:,} Wikidata-only official sites; wiki_sites now {total:,} in {(time.time() - t0) / 60:.1f} min")
