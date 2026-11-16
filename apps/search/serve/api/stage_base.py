"""stage_base: code moved out of search_api.py. Byte-identical to what it was there; the names it reads come from
search_api (imported at the end of that file's definitions) and the stdlib."""
from __future__ import annotations

from stage_parts import PartStale
from stage_parts import _pcheck
import contextvars
import datetime as dt
import gov_state
import json
import os
from stage_parts import part_run
import psycopg
import re
import threading
import time
import unicodedata
from urllib.parse import urlsplit
from search_api import (
    ANCHORS_SQL,
    CE_EXACT,
    CE_TOP,
    CHAIN_HOURS_SQL,
    GOV_CODE_STATES,
    GOV_SAME_WORDS,
    LOCAL_TOWN_SQL,
    LOOKUP_BACKEND,
    NAMED_TOWN_SQL,
    NAV_SCORE,
    NOMIC_DIR,
    OFFICIAL_FIRST,
    PLACE_EXACT_KM,
    RANKER_PATH,
    RELEASE_DIR,
    SEM_BACKEND,
    SEM_RELEASE,
    STATE_NAME,
    TABLES_TTL_S,
    TOWN_SQL,
    US_STATES,
    WIKI_BACKEND,
    WIKI_EXACT_SCORE,
    WX_NEAREST_MIN_POP,
    WX_NEAREST_SQL,
    _CC_SLD,
    _CrossEncoder,
    _EXEC,
    _GENERIC,
    _NEAR,
    _NEVER,
    _STORED_SQL,
    _TOKEN,
    _TOWN_TYPE,
    _compute_cache,
    _conn,
    _domain_of,
    _drop,
    _drop_pool,
    _news_rows,
    _press_release_title,
    _rows,
    _search,
    _staged,
    _stages,
    _trace,
    _wiki,
    ce_text,
    chain_hours,
    cross_encoder,
    domain_query,
    embed,
    exact_name_top,
    generation,
    gov_states,
    home_point,
    is_generic,
    is_local_gov,
    kind_vectors,
    local_map,
    lookups,
    names_task,
    norm_key,
    not_the_task,
    official_inner_page,
    official_ok,
    parts_for,
    place_keys,
    place_radius_km,
    query_names_domain,
    same_page,
    sem_ivf,
    sites_intent,
    stage,
    stored,
    tables,
    tight,
    town_query,
    trace_rows,
    web_text,
    wiki_text,
)


# Words that do not change which SITE is meant ("brave search", "costco website").
_NAV_NOISE = frozenset("the a www http https com net org site website official login signin sign in "
                       "home homepage page search app online".split())

def nav_keys(q: str) -> list[str]:
    """Candidate first labels of a domain the query could be naming.

    "wells fargo" → wellsfargo / wells-fargo; "brave search" → brave. Only the
    WHOLE remaining query is joined: "apple pie recipe" must not become apple.com,
    so a query with more than three meaningful words names no site.
    """
    core = [t for t in _TOKEN.findall(q.lower()) if t not in _NAV_NOISE]
    if not core or len(core) > 3:
        return []
    keys = {"".join(core), "-".join(core)}
    return sorted(k for k in keys if len(k) >= 2)

NAV_SQL = """
SELECT d.url, d.title, d.description, s.tranco_rank, coalesce(s.history_visits, 0), d.domain
FROM docs d LEFT JOIN serve_sites s USING (domain)
WHERE d.error IS NULL AND split_part(d.domain, '.', 1) = ANY(%(keys)s)
ORDER BY (d.domain !~ %(generic)s), s.tranco_rank NULLS LAST, s.cc_hc_pos NULLS LAST LIMIT 3
"""

# Every Tranco domain and subdomain host by name ("typing in the
# name of each of the top million domains and subdomains from tranco should have its
# domain as the top result"). 63% of them have no parsed homepage; those answer with a
# plain link to the site. nav_sites is built by serve-prep.sql, blocked domains excluded.
NAV_SITES_SQL = "SELECT host, rank FROM nav_sites WHERE key = ANY(%(keys)s) ORDER BY rank LIMIT 5"

DOMAIN_DOC_SQL = """
SELECT d.url, d.title, d.description, s.tranco_rank FROM docs d LEFT JOIN serve_sites s USING (domain)
WHERE d.error IS NULL AND d.domain = %(d)s LIMIT 1
"""

DOMAIN_NAV_SQL = "SELECT rank FROM nav_sites WHERE host = %(d)s ORDER BY rank LIMIT 1"

def bare_site(host: str, rank) -> tuple:
    """A Tranco site we hold no homepage for: a plain link, titled by its name."""
    return (f"https://{host}/", host, "", rank, 0, host)

def is_bare(c: tuple) -> bool:
    return c[1] == c[5] and not c[2]

def nav_order(cands: list[tuple]) -> list[tuple]:
    """(url, title, desc, rank, visits, domain) -> generic endings first, then Tranco rank,
    over homepages AND homepage-less Tranco sites alike. Preferring a site we hold a
    homepage for was tried and lost: nbcnews -> nbcnews.to (a mirror we had a page for)
    over nbcnews.com (none), meraki -> meraki.net over meraki.com (test)."""
    return sorted(cands, key=lambda c: (not is_generic(c[5]), c[3] if c[3] is not None else 10 ** 9))

# Subdomain services by name (host_nav, built by serve-prep from Tranco WITH
# subdomains): "youtube tv" -> tv.youtube.com.
HOST_NAV_SQL = """
SELECT url, title, description, rank FROM host_nav WHERE key = ANY(%(keys)s)
ORDER BY rank LIMIT 2
"""

WIKI_EXACT_SQL = """
SELECT DISTINCT w.title, w.lead
FROM wiki_aliases a JOIN wiki_docs w USING (page_id)
WHERE a.norm = %(nq)s LIMIT 3
"""

# Infobox kinds whose subject IS the website's owner: for these the official site is
# what a bare-name query wants, so it goes above the article. For anything else (a
# phone, a film, a person) the article stays first and the site follows it.
# Sports teams' infoboxes are named per league — "infobox mlb" (Yankees), "infobox nfl team",
# "infobox basketball club", "ice hockey team" — and none said "sports team", so no pro
# team's own site outranked its article ("yankees" put mlb.com/yankees last, 2026-09-25).
# A season, a biography, a match or a competition page is not the organisation.
_ORG_BOX = re.compile(r"^(?!.*\b(season|biography|match|competition|tournament|championship)\b)"
                      r"(?=.*(company|website|organi[sz]ation|airline|bank|retail|agency|software|brand|"
                      r"service|channel|network|university|college|school|club|restaurant|store|"
                      r"magazine|newspaper|government|political party|non-profit|charity|"
                      r"\bteam\b|\bmlb\b|\bnhl\b|\bnba\b|\bnfl\b|\bmls\b|minor league|\bleague\b))")

# "x official site", "x website", "x homepage": the person asks for the SITE of x (rc5 plan W6, measured 2026-10-07 on
# the site-nav dev half: 92 of 750 queries had the article and its official site in the release and missed only because
# the lookup was asked with these words still attached). Only a TRAILING ask is stripped; "web site design" is untouched.
_SITE_ASK = re.compile(r"^(.{2,}?)\s+(?:(?:official\s+)?(?:web\s?site|site|home\s?page)|official)\s*$")

def site_ask(q: str) -> str | None:
    """The name before a trailing "official site" / "website" / "homepage" / "official", else None."""
    m = _SITE_ASK.match(" ".join(q.lower().split()))
    name = m.group(1).strip() if m else ""
    # "official site" names nothing: the name must hold a word that is not itself such a word
    return name if any(t not in _NAV_NOISE for t in _TOKEN.findall(name)) else None

_PLACE_BOX = re.compile(r"\b(country|settlement|city|town|village|municipality|region|province|state|islands?|county|"
                        r"district|division|river|mountain|lake|continent|territory|prefecture|canton|commune|"
                        r"neighbou?rhood|borough|protected area|park|place)\b")

_PERSON_BOX = re.compile(r"\b(person|people|biography|artist|officeholder|writer|scientist|royalty|sportsperson|player|"
                         r"actor|comedian|politician|personality|criminal|saint|clergy|economist|philosopher|academic|"
                         r"architect|chef|model|wrestler|boxer|golfer|cyclist|swimmer|skier|driver|astronaut|engineer|"
                         r"presenter|journalist|author|musician|band)\b")

def box_kind(box: str | None) -> str:
    """"org", "place", "person" (a person or a band) or "other", from the article's infobox type or the Wikidata
    verdict the importer stored ('wikidata organization', 'wikidata place', 'wikidata person'). An organisation
    type wins: "infobox state agency" is an agency, "infobox football club" a club."""
    b = (box or "").lower()
    if not b:
        return "other"
    if _ORG_BOX.search(b):
        return "org"
    if _PLACE_BOX.search(b):
        return "place"
    return "person" if _PERSON_BOX.search(b) else "other"

def official_deep(url: str) -> bool:
    """A page, not a site: movies.disney.com/smith is the film Smith!'s "official site",
    and "smith" was answered with it, sure. Such a link may still be shown, never as a
    popular (confident) answer."""
    path = urlsplit(url).path.strip("/")
    return path.count("/") >= 1 or (len(path) > 0 and _domain_of(url).count(".") >= 2)

# How far above the best name match (NAV_SCORE) a leading official site sits. Under 0 it is no longer first among
# the sites of one name, and no longer pinned (official_top asks for NAV_SCORE or more). sweeps it.
OFFICIAL_LEAD = float(os.environ.get("SEARCH_OFFICIAL_LEAD", "0.5"))

def official_score(box: str | None, i: int, url: str = "", own_popular: bool = False, own: bool = False,
                   asked: bool = False) -> float:
    """Where an article's official site ranks. An organisation's own site on a generic
    ending is the best evidence there is for a bare-name query: above every name
    match. On a foreign country ending (aldi.de for "aldi") it stays tier 1 but below
    the best generic-ending name match. Anything else: just below the article.
    own: the query is this article's own title. asked: the query asks for the site ("x official site")."""
    kind = box_kind(box)
    if asked:
        lead = True                                     # the person asked for the site: it leads, whatever x is
    elif kind == "place" and OFFICIAL_FIRST != "all":
        lead = False                                    # a place: the article first, even with a popular site (italia.it)
    elif kind == "org" or own_popular:
        # own_popular: the article's own title IS the query and its site is popular — Gmail
        # (typed "email service", not an organisation) must still put its site first.
        lead = True
    else:
        # a person or a band asked by its own name: the site first, popular or not; only the first article of that
        # name (a namesake's site must not lead). Anything else (a phone, a film): the article first.
        lead = own and i == 0 and (kind == "person" or OFFICIAL_FIRST == "all")
    if lead and not asked and official_inner_page(url):
        lead = False                                    # a page of someone's site is not "the site"
    if lead:
        host = (urlsplit(url).hostname or "") if url else ""
        # The country-ending rule is about a foreign branch of a brand: aldi.de for "aldi" stays under the best
        # generic-ending name match. It is wrong for the thing the query names by its own title when that thing is no
        # organisation: a country's or a city's site IS on a country ending. Measured 2026-10-07, own-title dev half:
        # "france" led with france.tv, "london" with london.edu, "iceland" with iceland.co.uk (a supermarket), each a
        # mere name match on a generic ending, over service-public.fr, london.gov.uk and the country's own site.
        if not url or is_generic(host) or asked or (own and i == 0 and kind != "org"):
            return NAV_SCORE + OFFICIAL_LEAD - 0.1 * i
        return NAV_SCORE - 1.5 - 0.1 * i
    return WIKI_EXACT_SCORE - 0.5 - 0.1 * i

# A site we hold no fetched homepage for was shown by its address: "chess" -> "https://www.chess.com/", no description
# (rc6, 2026-10-08: 516 of the top 1,000 plain domains by own rank have no title, facebook.com, apple.com,
# google.com and amazon.com among them; 1,727,008 of 1,944,913 sites in all). The release already knows what most of
# them are: the article whose official site is that exact front page. It names the site the way the article's own
# name does everywhere else, "<Article> — official site", with the lead's first sentence under it.
# Only a front page takes a name: stricter than official_deep and official_inner_page, so "Google Keep"
# (keep.google.com) names keep.google.com and never google.com. official_ok is not asked: it keeps a profile on
# facebook.com from being someone's official site, and facebook.com's own front page is Facebook's.
# Which article: an organisation before anything else, then the most sitelinks (bbc.com is claimed by BBC, a BBC
# News channel and two people; fifa.com by FIFA and "Association football"). 25,588 of the 660,610 untitled sites
# with a claim have more than one, 223 of 467 in the top 1,000.
# A claim alone is not enough, Wikipedia lists front pages loosely: wixstatic.com is "Amy Serrano", gmail.com
# "I Hate Being Single", merriam-webster.com "Samoyed dog", sony.com "Core International" (1 sitelink). So the
# article must be tied to the address: its name fits it (name_fits), or the address or its first label is one of
# the article's own names (the redirects "Weather.gov", "GOP", "DFB"). Only when no claim is tied that way does an
# organisation with SITE_NAME_SITELINKS sitelinks or more name it (almoqawma.com: Hamas; werder.de: SV Werder
# Bremen). Tied claims go first because the best-linked claim is often a neighbour: businessinsider.com is also
# claimed by SourceForge, lionsgate.com by Summit Entertainment, dfb.de by the Germany national football team.
# Measured on the untitled sites: 462 of 516 in the top 1,000 get a name (361 by the name, 66 by a redirect, 35 by
# sitelinks), 14,171 of 16,050 in the top 25,000, 515,573 of 1,727,008 in all; any claim at all would name 467,
# 14,324 and 660,610, the difference being mostly people, places and works on an address their name is not in.
# Not used: anchor phrases (24,566 more sites, 8 in the top 1,000; lower-cased link text such as "video" and
# "register here", and the file has no index by site) and suggest_items names (tiktok.com: "TikTok For Business").
SITE_NAME_SITELINKS = 5

_NAME_QUALIFIER = re.compile(r"\s*\([^()]*\)$")                 # "Amazon (company)" -> "Amazon"

_SENTENCE_END = re.compile(r"(?<=[a-z0-9)\"”]{3})\.(?=\s+[A-Z\"“(]|\s*$)")   # not "Inc. is", "St. Mary", "U.S. agency"

def front_host(url: str) -> str | None:
    """The host, without www., when the address is a site's front page; else None."""
    try:
        u = urlsplit(url or "")
    except ValueError:
        return None
    if u.scheme not in ("http", "https") or u.path not in ("", "/") or u.query or u.fragment:
        return None
    return _domain_of(url) or None

def name_fits(title: str, host: str) -> bool:
    """Does the article's name fit the site's address? The name without its qualifier ("Wiley (publisher)",
    "Bayside, Wisconsin", "The Lancet") starts or ends a label of the host or the label starts or ends it (wiley.com,
    baysidewi.gov, thelancet.com, springer.com for "Springer Science+Business Media", navalny.com for "Alexei
    Navalny"), or the label is the name's initials (fbi.gov, pnas.org, lww.com). Not a word in the middle:
    defense.gov is not the "Assistant to the Secretary of Defense for Public Affairs"."""
    t = re.sub(r"^the\s+", "", _NAME_QUALIFIER.sub("", title or "").split(",")[0], flags=re.I)
    fold = lambda s: re.sub(r"[^a-z0-9]", "", unaccent(s.lower()))
    name = fold(t)
    ini = "".join(w[0] for w in re.split(r"[^\w]+", t) if w and (w[0].isupper() or w[0].isdigit())).lower()
    for label in host.split(".")[:-1] or [host]:
        for x in {fold(label), fold(label)[3:] if label.startswith("the") else fold(label)} - {"www", ""}:
            if len(name) >= 3 and len(x) >= 3 and (x.startswith(name) or x.endswith(name)):
                return True
            if len(x) >= 4 and (name.startswith(x) or name.endswith(x)):
                return True
            if len(ini) >= 2 and len(x) >= 2 and (x == ini or (len(x) >= 3 and x in ini) or (len(ini) >= 3 and x.startswith(ini))):
                return True
    return False

def first_sentence(lead: str | None) -> str:
    """The lead's first sentence, 300 characters at most: a site's one-line description."""
    text = " ".join((lead or "").split())
    if text.startswith(("{", "|", "[[")):       # the infobox, not the lead (115 of the 515,573: "{{Infobox political party | name ...")
        return ""
    m = _SENTENCE_END.search(text, 20)
    return (text[:m.end()] if m else text)[:300]

def site_name(host: str, claims) -> tuple[str, str] | None:
    """(name, description) for a site from the articles that claim its front page as their official site,
    [(title, infobox kind, sitelinks, lead, short description, the address is one of the article's names)];
    None when no claim is good enough."""
    order = sorted(claims, key=lambda c: (box_kind(c[1]) != "org", -(c[2] or 0), c[0] or ""))
    tied = [c for c in order if c[0] and (c[5] or name_fits(c[0], host))]
    known = [c for c in order if c[0] and box_kind(c[1]) == "org" and (c[2] or 0) >= SITE_NAME_SITELINKS]
    for title, _box, _links, lead, descr, _aka in (tied or known)[:1]:
        return _NAME_QUALIFIER.sub("", title) or title, first_sentence(lead) or descr or ""
    return None

def shown_by_address(r: dict) -> bool:
    """The result's title is its own address or host: nothing better was known."""
    t = re.sub(r"^https?://", "", (r.get("title") or "").strip().lower()).rstrip("/")
    return (t[4:] if t.startswith("www.") else t) == same_page(r["url"])

def name_front_pages(res: list[dict]) -> list[dict]:
    """Give the front pages shown by their address a name, and those shown without a description one (site_name).
    Run on the finished list, after every rule and both rankers, so it cannot move a result (web_rerank, which
    runs later, reads a named result by its address). A site with a fetched homepage keeps its own title and
    description: only what is missing is filled."""
    lk = lookups()
    if lk is None or lk.name != "release":
        return res
    out = []
    for r in res:
        host = front_host(r["url"]) if r.get("source") == "site" else None
        bare = bool(host) and shown_by_address(r)
        if host and (bare or not r.get("content")):
            titled, claims = lk.site_claims(host)
            got = site_name(host, claims)
            if got and bare and not titled:
                r = dict(r, title=f"{got[0]} — official site", named=True)      # named: web_rerank reads the address
            if got and got[1] and not r.get("content"):
                r = dict(r, content=got[1])
        out.append(r)
    return out

# Searches a language model wrote for each site (doc2query, d2q_docs): "newspaper of
# record new york" -> nytimes.com. Off unless ?d2q=1 until it is measured (2026-09-29).
D2Q_SQL = ANCHORS_SQL.replace("anchor_docs_bm25", "d2q_docs_bm25").replace("FROM anchor_docs", "FROM d2q_docs")

EXACT_ANCHOR_MIN, EXACT_ANCHOR_LEAD = 10, 3.0

SEM_PROBES = max(1, min(int(os.environ.get("SEARCH_SEM_PROBES", "200")), 2600))

# Meaning search over web pages: the release's vectors/pages-ivf* folder, the pages' 1-bit nomic codes
# (release/pages_vec.py), compared with the query's code as Wikipedia's are. Its hits join the fusion as one list at
# SEM_PAGES_W; a page more than SEM_PAGES_MAX_BITS of the 768 bits away is no match. Needs SEARCH_SEM_BACKEND=ivf (the
# encoder). SEARCH_SEM_PAGES_IVF=<folder>; unset, the release's vectors/pages-ivf* if there is one. ?drop=sempages: off.
SEM_PAGES_W = float(os.environ.get("SEARCH_SEM_PAGES_W", "0.5"))

SEM_PAGES_MAX_BITS = int(os.environ.get("SEARCH_SEM_PAGES_MAX_BITS", "230"))

SEM_PAGES_PROBES = max(1, int(os.environ.get("SEARCH_SEM_PAGES_PROBES", "100")))

_sem_pages_dir: str | None | bool = False

def sem_pages_dir() -> str | None:
    global _sem_pages_dir
    if _sem_pages_dir is False:
        d = os.environ.get("SEARCH_SEM_PAGES_IVF") or ""
        if not d:
            import glob as _glob
            found = sorted(_glob.glob(os.path.join(RELEASE_DIR, "vectors", "pages-ivf*")))
            d = found[-1] if found else ""
        _sem_pages_dir = d if d and os.path.isfile(os.path.join(d, "ivf.json")) else None
        if _sem_pages_dir:
            print(f"page meaning search: {_sem_pages_dir}", flush=True)
    return _sem_pages_dir

_qcode_cache: dict = {}

_qcode_busy: dict = {}                 # query -> Event while one thread embeds it

_qcode_lock = threading.Lock()

def _qcode_compute(si, enc, q: str):
    """The nomic code of q: from the compute cache (SEARCH_COMPUTE_CACHE) when this model already coded this
    exact text, else from the model. The namespace names the model file's sha256 and the encoder's prefix and token
    limit, so a new model or setting never reads an old code."""
    cc = _compute_cache()
    if cc is None:
        return si.code_of(enc(q))
    import compute_cache as _cc
    ns = f"qcode|{_cc.file_sha256(os.path.join(NOMIC_DIR, 'model.onnx'))}|{si.NOMIC_PREFIX}|{si.NOMIC_TOKENS}|{enc.sess.get_providers()[0]}"
    v = cc.get_many(ns, [q])[0]
    if v is not None:
        return si.np.frombuffer(v, dtype=si.np.uint8).copy()
    c = si.code_of(enc(q))
    cc.put_many(ns, [q], [c.tobytes()])
    return c

def query_code(q: str):
    """The query's 96-byte code, once per query text: the Wikipedia and the pages lists share it. The two lists start
    together, so a second caller waits for the first one's embedding instead of making its own (2026-11-03: every
    search embedded twice, ~150 CPU ms each, on the public server's four busy cores)."""
    si, _ix, enc = sem_ivf()
    while True:
        with _qcode_lock:
            c = _qcode_cache.get(q)
            if c is not None:
                return c
            ev = _qcode_busy.get(q)
            if ev is None:
                ev = _qcode_busy[q] = threading.Event()
                break
        with stage("embed_wait"):
            ev.wait(30)                # the first caller finished (or failed): look again
    try:
        with stage("embed"):
            c = _qcode_compute(si, enc, q)
        with _qcode_lock:
            if len(_qcode_cache) > 512:
                _qcode_cache.clear()
            _qcode_cache[q] = c
        return c
    finally:
        with _qcode_lock:
            _qcode_busy.pop(q, None)
        ev.set()

def sem_page_rows(q: str, k: int) -> list[tuple]:
    """(url, title, "", similarity) of the pages whose meaning is nearest the query's; [] without the folder."""
    path = sem_pages_dir()
    if not path or SEM_BACKEND != "ivf" or "sempages" in _drop.get():
        return []
    web = web_text()
    if not hasattr(web, "_page_url"):                   # checked first: the scan below is wasted when its hits cannot be read (CORE-A-08)
        return []
    si = sem_ivf()[0]
    pix = si.load_pages(path, SEM_RELEASE)
    code = query_code(q)
    with stage("sem-pages"):
        hits = pix.search(code, k, SEM_PAGES_PROBES)
    lk = web._lk()
    out = []
    for d, dist in hits:
        if dist > SEM_PAGES_MAX_BITS:
            continue
        url = web._page_url(lk, d)
        if url:
            out.append((url, lk.title(d) or url, "", 1 - dist / 768.0))
    return out

def wiki_rows(q: str, k: int, stored: frozenset = frozenset()) -> list[tuple]:
    """(title, lead, bm25) rows of the Wikipedia text list, from whichever backend serves it."""
    return wiki_text().search(q, k, stored)

RRF_K = int(os.environ.get("SEARCH_RRF_K", "60"))            # swept by tools/sq-sweep.py

SEM_WIKI_SQL = """
SELECT w.title, w.lead, 1 - (v.v <=> %(e)s::halfvec) AS sim
FROM wiki_vec v JOIN wiki_docs w ON w.page_id = v.id
ORDER BY v.v <=> %(e)s::halfvec LIMIT %(k)s
"""

def rrf(lists: list[list[dict]], scale: float = 100.0, weights: list[float] | None = None) -> list[dict]:
    """Reciprocal-rank fusion of ranked result lists: one entry per page, scored
    scale * RRF_K/2 * sum(w / (RRF_K + rank)) — a page first in two lists ~98, first
    in one ~49, so the fused tier stays far below the exact tiers (900+). w is the list's
    weight, 1 unless given."""
    fused: dict[str, dict] = {}
    for n, lst in enumerate(lists):
        w = weights[n] if weights else 1.0
        for rank, r in enumerate(lst, 1):
            key = same_page(r["url"])
            f = fused.setdefault(key, dict(r, score=0.0))
            f["score"] += w * scale * (RRF_K / 2) / (RRF_K + rank)
    for f in fused.values():
        f["score"] = round(f["score"], 2)
    return sorted(fused.values(), key=lambda r: r["score"], reverse=True)

# ---------------------------------------------------------------------------
# Subreddits (2026-09-24): "r/homelab", "homelab subreddit", "subreddit homelab" name a
# community -> tier 1; "homelab reddit" and "<topic> subreddit/reddit" search the
# directory (name + title + description, weighted by members) as its own fused list.
# ---------------------------------------------------------------------------
_R_SLASH = re.compile(r"^/?r/([a-z0-9_]{2,21})/?$")

_SUB_WORDS = re.compile(r"\b(subreddits?|sub reddit|reddit)\b")

SUB_POPULAR = 10_000

def subreddit_query(q: str) -> tuple[str | None, str | None]:
    """-> (exact subreddit key or None, topic text or None).
    "r/homelab" / "homelab subreddit" -> ("homelab", "homelab"); "3d printing reddit" ->
    ("3dprinting", "3d printing"); anything without reddit/subreddit/r/ -> (None, None)."""
    t = q.lower().strip()
    m = _R_SLASH.match(t)
    if m:
        return norm_key(m.group(1)), m.group(1)
    if not _SUB_WORDS.search(t):
        return None, None
    rest = " ".join(_SUB_WORDS.sub(" ", t).split())
    if not rest or len(rest.split()) > 4:
        return None, (rest or None)
    return norm_key(rest), rest

SUB_EXACT_SQL = "SELECT name, title, description, subscribers FROM subreddits WHERE key = %(key)s"

SUB_SEARCH_SQL = """
SELECT name, title, description, subscribers,
       -(search_text <@> to_bm25query(%(q)s, 'subreddits_bm25')) AS bm25
FROM subreddits
ORDER BY search_text <@> to_bm25query(%(q)s, 'subreddits_bm25') LIMIT 30
"""

def _subreddit(name, title, desc, subs, score, popular=False) -> dict:
    r = {"url": f"https://www.reddit.com/r/{name}/", "title": f"r/{name} — {title or name}",
         "content": f"{int(subs):,} members. {(desc or '')[:260]}", "source": "subreddit", "score": round(score, 2)}
    if popular:
        r["popular"] = True
    return r

# ---------------------------------------------------------------------------
# YouTube channels (2026-09-24, Wikidata P2397): "mkbhd youtube", "@mrbeast", "nike
# youtube channel" name a channel -> tier 1; "woodworking youtube channels" searches
# the directory as its own fused list. "youtube <x>" names a channel only when x is not
# a YouTube product ("youtube music" is music.youtube.com, not a channel called Music).
# ---------------------------------------------------------------------------
_YT_HANDLE = re.compile(r"^@([a-z0-9._-]{3,30})$")

_YT_WORDS = re.compile(r"\b(youtube channels?|youtubers?|youtube|yt channel)\b")

_YT_TAIL = re.compile(r"^(.+?)\s+(?:on\s+)?(?:youtube channel|youtube|yt channel)$")

_YT_HEAD = re.compile(r"^(?:youtube channel|youtube)\s+(?!channels?\b)(.+)$")

_YT_LIST = re.compile(r"\b(youtube channels|youtubers?)\b")

_YT_PRODUCTS = frozenset("music tv kids premium studio shorts red gaming originals downloader download "
                         "to mp3 mp3 converter login sign in account app video videos "
                         "live search history subscriptions".split())

YT_POPULAR = 100_000

def youtube_query(q: str) -> tuple[str | None, str | None]:
    """-> (exact channel key or None, topic text or None).
    "mkbhd youtube" / "@mkbhd" / "youtube channel mkbhd" -> ("mkbhd", "mkbhd");
    "cooking youtube channels" / "woodworking youtubers" -> (None, topic);
    "youtube music" -> (None, None); no youtube word -> (None, None)."""
    t = " ".join(q.lower().split())
    m = _YT_HANDLE.match(t)
    if m:
        return norm_key(m.group(1)), m.group(1)
    if not _YT_WORDS.search(t):
        return None, None
    tail, head = _YT_TAIL.match(t), _YT_HEAD.match(t)
    if head and not tail and head.group(1).split()[0] in _YT_PRODUCTS:
        return None, None                                  # youtube music / youtube tv / youtube to mp3
    named = (tail or head).group(1) if (tail or head) else None
    if named and len(named.split()) <= 4:
        return norm_key(named), named
    if named:
        return None, named
    if not _YT_LIST.search(t):                              # "how to upload to youtube": not about channels
        return None, None
    return None, " ".join(_YT_WORDS.sub(" ", t).split()) or None

YT_EXACT_SQL = """
SELECT channel_id, handle, name, description, followers, sitelinks FROM youtube_channels
WHERE key = %(key)s OR hkey = %(key)s OR wkey = %(key)s OR skey = %(key)s
ORDER BY (skey = %(key)s AND %(key)s NOT IN (coalesce(key, ''), coalesce(hkey, ''), coalesce(wkey, ''))),
         followers DESC NULLS LAST, sitelinks DESC LIMIT 2
"""

YT_SEARCH_SQL = """
SELECT channel_id, handle, name, description, followers, sitelinks,
       -(search_text <@> to_bm25query(%(q)s, 'youtube_channels_bm25')) AS bm25
FROM youtube_channels
ORDER BY search_text <@> to_bm25query(%(q)s, 'youtube_channels_bm25') LIMIT 30
"""

# A bare query that IS a big channel's name ("linus tech tips" — its article is a
# redirect to Linus Media Group, so the channel was nowhere, 2026-09-24).
# 100,000 since 2026-10-11 (the owner: "100,000+ should show for its bare name"); it was 1,000,000.
YT_BARE_MIN = int(os.environ.get("SEARCH_YT_BARE_MIN", "100000"))

YT_BARE_SQL = """
SELECT channel_id, handle, name, description, followers, sitelinks FROM youtube_channels
WHERE (key = %(key)s OR hkey = %(key)s OR wkey = %(key)s) AND followers >= %(min)s
ORDER BY followers DESC LIMIT 1
"""

# A bare name leads with the channel only when the channel IS the thing: a creator.
# "prime video" is a streaming service with a channel — primevideo.com is the answer.
_YT_CREATOR = re.compile(r"youtub|vlogg|internet personality|web series|streamer|content creator|podcast", re.I)

def bare_channel_score(key: str, out: list[dict], words: int = 2, handle: str | None = None,
                       desc: str = "YouTuber") -> float:
    """Where a bare-name channel goes: first, unless a site or article is literally named
    the query (nike -> nike.com, music -> the Music article), then just under the article.
    One plain word is too ambiguous to lead with ("train", "quick" are 1M+ channels) unless
    it is the channel's own handle (mkbhd, mrbeast). A brand's or company's channel never
    leads (see _YT_CREATOR)."""
    if not _YT_CREATOR.search(desc or "") or (words < 2 and norm_key(handle or "") != key):
        return WIKI_EXACT_SCORE - 0.5
    for r in out:
        if r["score"] < WIKI_EXACT_SCORE - 3:
            continue
        name = r.get("title", "").split(" — ")[0]
        label = _domain_of(r.get("url", "")).split(".")[0]
        if (r.get("source") == "site" and not r.get("official") and label == key) or \
                (r.get("source") in ("wikipedia",) and norm_key(name) == key) or \
                (r.get("official") and norm_key(name) == key):
            return WIKI_EXACT_SCORE - 0.5
    return NAV_SCORE + 0.75

def bare_channel_row(r: dict, desc: str | None, out: list[dict]) -> dict | None:
    """The bare-name channel as it joins the list, or None. A creator's channel is the thing asked for and joins as
    it is. Anything else's (a company's, a band's) is one more of the thing's own accounts once its article has
    brought some: it is counted with them (SEARCH_PROFILE_CAP), and left out when they already hold a YouTube
    account. At 100,000 followers "sega" otherwise listed two Sega channels and lost sega.com from the first five
    (head-brand, rc8, 2026-10-11)."""
    if _YT_CREATOR.search(desc or ""):
        return r
    profs = [p for p in out if p.get("profile")]
    if any(_domain_of(p.get("url", "")) == "youtube.com" for p in profs):
        return None
    if profs:
        r["profile"], r["of"] = True, profs[0].get("of")
    return r

def youtube_url(channel_id, handle) -> str:
    return f"https://www.youtube.com/@{handle}" if handle else f"https://www.youtube.com/channel/{channel_id}"

def _youtube(channel_id, handle, name, desc, followers, sitelinks, score) -> dict:
    head = f"{int(followers):,} subscribers. " if followers else ""
    r = {"url": youtube_url(channel_id, handle), "title": f"{name} — YouTube" + (f" (@{handle})" if handle else ""),
         "content": head + (desc or "")[:260], "source": "youtube", "score": round(score, 2)}
    if (followers or 0) >= YT_POPULAR or (followers is None and (sitelinks or 0) >= 10):
        r["popular"] = True
    return r

# ---------------------------------------------------------------------------
# Accounts from Wikidata (wiki_links, 2026-09-24): "<name> instagram", "<name> on x",
# "<name> spotify", "<film> imdb" -> that account at tier 1. Only the TAIL form: a
# leading platform word is usually a product ("instagram reels", "github actions").
# "x" alone is never a platform word ("malcolm x", "spacex"): "on x" / "x account" are.
# ---------------------------------------------------------------------------
_ACCOUNT_WORDS = {
    "x": ("twitter", "tweets", "twitter account", "on x", "x account", "x profile"),
    "instagram": ("instagram", "insta", "ig", "instagram account", "instagram page"),
    "facebook": ("facebook", "fb", "facebook page", "facebook account"),
    "tiktok": ("tiktok", "tik tok", "tiktok account"),
    "linkedin": ("linkedin", "linkedin page", "linkedin profile"),
    "spotify": ("spotify",),
    "github": ("github", "git hub", "github repo"),
    "twitch": ("twitch", "twitch channel", "twitch stream"),
    "imdb": ("imdb",),
    "mastodon": ("mastodon",),
    "bluesky": ("bluesky", "bsky"),
    "blog": ("blog",),
    "gutenberg": ("gutenberg", "project gutenberg"),
}

_ACCOUNT_TAIL = [(p, re.compile(r"^(.+?)\s+(?:on\s+)?(?:%s)$" % "|".join(
    re.escape(w) for w in sorted(ws, key=len, reverse=True)))) for p, ws in _ACCOUNT_WORDS.items()]

# "download instagram", "delete facebook", "how to use github": the platform, not an account.
# Articles and adjectives are NOT here: The Office, Best Buy, Top Gun, New York Times.
_ACCOUNT_NOT_A_NAME = re.compile(
    r"^(how|what|why|when|where|is|can|does|do|my|download|delete|deactivate|"
    r"login|log in|log into|sign in|sign up|signup|create|open|reset|change|recover|hack|unblock|install|"
    r"update|private|desktop|web|app|dark mode|contact|customer|support|careers?|jobs?|stock|shares?)\b")

# A book by Wikidata's ebook ID, a book matched from the catalog (importer/gutenberg.py), an author.
GUTENBERG = ["gutenberg", "gutenberg-catalog", "gutenberg-author"]

def the_keys(key: str) -> list[str]:
    """"newyorktimes" also matches "The New York Times" (Wikidata keeps the article)."""
    return [key] if key.startswith("the") else [key, "the" + key]

def account_query(q: str) -> tuple[list[str], str] | None:
    """"nike instagram" -> (["instagram"], "nike"); "elon musk on x" -> (["x"], "elonmusk");
    "linkedin" platforms cover both company pages and people. None when the query names
    no account."""
    t = " ".join(q.lower().split())
    for platform, rx in _ACCOUNT_TAIL:
        m = rx.match(t)
        if m:
            name = m.group(1)
            if _ACCOUNT_NOT_A_NAME.match(name) or len(name.split()) > 5:
                return None
            key = norm_key(name)
            if len(key) < 2:
                return None
            if platform == "gutenberg":
                # the book ("moby dick gutenberg") or the author's shelf ("mark twain gutenberg");
                # "project gutenberg" alone is the site
                return None if key == "project" else (GUTENBERG, key)
            return (["linkedin", "linkedin-in"] if platform == "linkedin" else [platform]), key
    return None

# One account per entity: an item often lists several (The New York Times: @nytimes,
# @nytgraphics, @nytchinese …) — the handle matching the query wins, else the shortest,
# which is the main account far more often than not.
ACCOUNT_SQL = """
SELECT platform, url, name, description, value, sitelinks FROM (
    SELECT DISTINCT ON (qid) platform, url, name, description, value, sitelinks,
           (skey = %(key)s AND %(key)s NOT IN (coalesce(key, ''), coalesce(hkey, ''), coalesce(wkey, ''))) AS short_only
    FROM wiki_links
    WHERE platform = ANY(%(p)s) AND (key = ANY(%(keys)s) OR hkey = %(key)s OR wkey = ANY(%(keys)s) OR skey = %(key)s)
    ORDER BY qid, (hkey = %(key)s) DESC, length(value)) x
ORDER BY short_only, sitelinks DESC LIMIT 2
"""

# Official sites of things English Wikipedia has no article about: a bare exact name,
# ranked just under an article and never above a domain-name match.
# The floor: such a site is shown only when its Wikidata item has this many sitelinks (pages about it in any
# Wikimedia project; 0 for an item no project has a page for). Read by all three lookups (this SQL,
# lookups_sqlite, lookups_release.link_accounts).: 149 dev answers of the open sets sit under 2.
# rc9: default 1 (was 2); the floor at 0 stays rejected (arm-486-floor1.md). SEARCH_LINK_SITE_MIN_SITELINKS=2 restores rc8.
LINK_SITE_MIN_SITELINKS = int(os.environ.get("SEARCH_LINK_SITE_MIN_SITELINKS", "1"))

LINK_SITE_SQL = f"""
SELECT url, name, description, sitelinks FROM wiki_links
WHERE platform = 'site' AND (key = %(key)s OR wkey = %(key)s) AND coalesce(sitelinks, 0) >= {LINK_SITE_MIN_SITELINKS}
ORDER BY sitelinks DESC NULLS LAST LIMIT 1
"""

# (2026-10-30): chains and utilities by the name people use (brand-sites.tsv: name-suggestion-index's names, Wikidata's
# official sites). Asked only where the link tier found nothing, with the same conditions. SEARCH_BRAND_SITES=0 turns it off;
# measured 2026-10-30 (no set moves on test, 2 site-nav gains on dev); the file absent is the same as off.
BRAND_SITES = os.environ.get("SEARCH_BRAND_SITES", "1") == "1"

_brand_sites = None

def brand_sites() -> dict:
    global _brand_sites
    if _brand_sites is None:
        _brand_sites = {}
        try:
            with open(os.environ.get("SEARCH_BRAND_SITES_FILE") or os.path.join(RELEASE_DIR, "brand-sites.tsv"), encoding="utf-8") as f:
                for line in f:
                    k, url, name = line.rstrip("\n").split("\t")
                    _brand_sites[k] = (url, name)
        except OSError:
            pass
        print(f"brand names with an official site: {len(_brand_sites)}", flush=True)
    return _brand_sites

# The guard for a floor under LINK_SITE_TRUSTED, on by default in rc9; SEARCH_LINK_SITE_LOW_GUARD=0 turns it off. A site the tier
# shows for an item with fewer sitelinks than this ("low": 0 or 1, 1.43M items nobody or one project wrote a page
# about) is marked link_low where the tier appends it, and then:
#   - brand_site_first() never lifts it above the places ("da vi nails": davinails.com 940.5 over the map row 940.0);
#   - link_low_yields() takes it out when an article carries the name ("robert lambert" -> robertlambert.com, a
#     zero-sitelink namesake of four people with articles).
# All three lookups hand the tier the item's sitelinks as the row's fourth column; none of them changes.
LINK_SITE_TRUSTED = 2

LINK_SITE_LOW_GUARD = os.environ.get("SEARCH_LINK_SITE_LOW_GUARD", "1").strip() == "1"

_TITLE_QUALIFIER = re.compile(r"\s*(?:\(.*\)|,.*)$")

def link_low(sitelinks) -> bool:
    """The link tier's row is one only a floor under LINK_SITE_TRUSTED admits, and the guard is on."""
    return LINK_SITE_LOW_GUARD and (sitelinks or 0) < LINK_SITE_TRUSTED

def link_low_yields(out: list[dict], lists: list[list[dict]], key: str) -> None:
    """A link_low site is not shown when an article in the fused lists carries the name the tier matched (key):
    the title itself, or the title before its "(…)" or ", …" qualifier. Either the article is the thing asked for
    and was not in the exact tier ("médecins sans frontières": norm_key drops the accented letters, the alias
    lookup misses, and an archive page about MSF stood above the article), or the name is shared by things with
    articles and the site is a namesake's ("robert lambert", "patrick murphy official site" -> a University of
    Arizona home page, "cañada website" -> la-canyada.com with Cañada, Alicante in the list). floor-0 dev, rc8:
    takes out those three namesakes and one right answer of 106 ("frail website": the band, an article about a
    song)."""
    if not any(r.get("link_low") for r in out):
        return
    for lst in lists:
        for r in lst:
            if r.get("source") != "wikipedia":
                continue
            title = r["title"].rsplit(" — Wikipedia", 1)[0]
            if key in (norm_key(title), norm_key(_TITLE_QUALIFIER.sub("", title))):
                out[:] = [x for x in out if not x.get("link_low")]
                return

# Pages that describe the thing an exact name refers to (Wikidata "described at URL":
# catalogues, databases, reference entries) — one more fused list, never tier 1.
DESCRIBED_SQL = """
SELECT url, name, description FROM wiki_links
WHERE platform = 'described' AND (key = %(key)s OR wkey = %(key)s)
ORDER BY sitelinks DESC LIMIT 3
"""

def _described(url, name, desc) -> dict:
    host = _domain_of(url)
    return {"url": url, "title": f"{name} — {host}", "content": (desc or "")[:300],
            "source": "described", "score": 0.0}

def profiles_follow_article(res: list[dict]) -> list[dict]:
    """An article's own accounts sit right under it: the rerankers moved IMDb above
    the Paul Rudd article (2026-09-25). Profiles whose article is absent stay put."""
    arts = {same_page(r["url"]): i for i, r in enumerate(res) if not r.get("profile")}
    moving = sorted((r for r in res if r.get("profile") and r.get("of") in arts), key=lambda r: -r["score"])
    if not moving:
        return res
    rest = [r for r in res if not (r.get("profile") and r.get("of") in arts)]
    out = []
    for r in rest:
        out.append(r)
        out.extend(p for p in moving if p["of"] == same_page(r["url"]))
    return out

_ANCHOR_NONWORD = re.compile(r"[^\w&'+.-]+", re.U)

def anchor_key(q: str) -> str:
    """A query as anchors.norm_anchor stores anchor text: "Wayback  Machine!" -> "wayback machine"."""
    return re.sub(r"\s+", " ", _ANCHOR_NONWORD.sub(" ", (q or "").lower())).strip(" .-'&+")

def exact_anchor(rows) -> dict | None:
    """EXACT_ANCHOR_SQL rows -> the site a phrase names: used by EXACT_ANCHOR_MIN+ sites and
    EXACT_ANCHOR_LEAD times as many as for the next site; else None."""
    if not rows:
        return None
    top = rows[0]
    second = rows[1][1] if len(rows) > 1 else 0
    if top[1] < EXACT_ANCHOR_MIN or top[1] < EXACT_ANCHOR_LEAD * max(second, 1):
        return None
    r = _site(top[2], top[3], top[4], NAV_SCORE - 2, True)
    r["anchor_named"] = True
    return r

def anchor_list(rows) -> list[dict]:
    """ANCHORS_SQL rows -> site results, one per URL, best first (their own fused list)."""
    out, seen = [], set()
    for url, title, desc, bm25 in rows:
        if url not in seen:
            seen.add(url)
            out.append(_site(url, title, desc, float(bm25)))
    return out

# The one a bare surname means ("trump is the president and the results
# should reflect that"): a one-word query that ends the name of an article searched
# FAMOUS_RATIO times more than anything actually named that word — Donald Trump (434k
# searches) over $Trump (6.9k), Taylor Swift over Swift (bird). "washington" (George vs
# the state, 5.7x) and "jordan" (3.9x) stay what they are named. From search demand, so
# it follows who is being searched now; no list of people.
FAMOUS_RATIO = 10.0

FAMOUS_MIN = 20000.0

FAMOUS_SQL = """
SELECT i.kind, i.name, i.wiki, coalesce(i.demand, 0), k.pos FROM suggest_keys k JOIN suggest_items i USING (id)
WHERE k.key = %(k)s AND k.pos IN (0, 1) AND i.kind IN ('wiki', 'site', 'youtube')
ORDER BY i.demand DESC NULLS LAST LIMIT 40
"""

def org_first(out: list[dict], exact_titles: list[str], title: str, lead_of=None) -> None:
    """Of several articles named exactly the query ("Chase", "Chase Bank", "Chase (video
    game)"), the organisation's is the one the card and the first article slot get. One not
    named exactly ("cvs" -> CVS Pharmacy) is added in that slot."""
    if title in exact_titles:
        exact_titles.remove(title)
    elif lead_of is None:
        return
    exact_titles.insert(0, title)
    for r in out:
        if r.get("source") == "wikipedia" and r["title"].rsplit(" — Wikipedia", 1)[0] == title:
            r["score"] = round(WIKI_EXACT_SCORE + 0.5, 2)
            return
    if lead_of is not None:
        out.append(_wiki(title, lead_of(title), WIKI_EXACT_SCORE + 0.5))

# A title that equals the query only once its leading symbol is dropped ("$Trump" for "trump", "#MeToo" for "metoo"):
# the exact lookup keys on letters and digits. When a famous namesake takes the query, such a title and its official
# site go to the end; with nothing else named so, it stays (a search for "metoo" still finds #MeToo).
SIGILS = "$#@¥€£!+"

def untyped_sigil(title: str, q: str) -> bool:
    return bool(title) and title[0] in SIGILS and title[0] not in (q or "")

def famous_namesake(key: str, rows) -> str | None:
    """FAMOUS_SQL rows for a one-word key -> the article that word means, or None."""
    named = max((d for kind, name, wiki, d, pos in rows if pos == 0), default=0.0)
    for kind, name, wiki, d, pos in rows:
        if pos != 1 or kind != "wiki" or not wiki:
            continue
        words = re.sub(r"\s*\(.*\)$", "", name).split()
        if not words or norm_key(words[-1]) != key:
            continue                                  # "Trump family": not a name ending in it
        if not 2 <= len(words) <= 4 or not all(w[:1].isupper() for w in words):
            continue                                  # "Wealth of Elon Musk": not a person's name
        return wiki if d >= FAMOUS_MIN and d >= FAMOUS_RATIO * max(named, 1.0) else None
    return None

# A major-league team leads for its nickname ("if a major sports team is a
# possible result it should probably be the top result"): "devils" -> New Jersey Devils over
# D'Evils (a Jay-Z song), "giants" / "rangers" -> the most searched of the teams with that
# name. From the article's infobox kind — team-only, unlike Wikidata's league (P118), which
# players carry too (10,246 MLB items) and NHL teams lack. A basketball club must also be in
# the NBA (European clubs share the infobox); half the NFL (Giants, Jets, Rams) uses the
# generic gridiron box, shared with the CFL — those need the NFL as league. Works off-season.
MAJOR_TEAM_BOXES = ["infobox mlb", "infobox nfl team", "infobox nhl team"]

MAJOR_TEAM_SQL = """
SELECT i.wiki, i.name, coalesce(max(i.demand), 0) AS d
FROM suggest_keys k JOIN suggest_items i USING (id)
JOIN wiki_docs w ON w.title = i.wiki JOIN wiki_sites ws USING (page_id)
WHERE k.key = %(k)s AND k.pos IN (0, 1) AND i.kind = 'wiki'
  AND (ws.box = ANY(%(boxes)s)
       OR (ws.box IN ('infobox gridiron football team', 'infobox american football team')
           AND EXISTS (SELECT 1 FROM wiki_facts f WHERE f.title = i.wiki
               AND f.fact = 'league' AND f.value = 'National Football League'))
       OR (ws.box = 'infobox basketball club' AND EXISTS (SELECT 1 FROM wiki_facts f WHERE f.title = i.wiki
               AND f.fact = 'league' AND f.value = 'National Basketball Association')))
GROUP BY i.wiki, i.name ORDER BY d DESC LIMIT 5
"""

# A maker's product asked by its bare word ("keep" showed no Google Keep, "drive" no Google Drive,
# "teams" no Microsoft Teams). The article is "<Maker> <word>" and its own site says both: the maker is a label of
# the host and the word is another label or a path segment (keep.google.com, google.com/maps, tesla.com/modely).
# Read from the names and official sites already held: no list of makers. The 400-product sweep that day: 314 sites
# found by the full name, 115 by the bare word. Shown under the things named exactly the word; it leads only when
# famous_namesake says the word means it ("maps", "translate").
PRODUCT_SQL = """
SELECT i.wiki, i.name, ws.url, coalesce(max(i.demand), 0) AS d
FROM suggest_keys k JOIN suggest_items i USING (id)
JOIN wiki_docs w ON w.title = i.wiki JOIN wiki_sites ws USING (page_id)
WHERE k.key = %(k)s AND k.pos = 1 AND i.kind = 'wiki' AND ws.url IS NOT NULL
GROUP BY i.wiki, i.name, ws.url ORDER BY d DESC LIMIT 60
"""

PRODUCT_TOP = 2                               # "maps": Google Maps and Apple Maps; "mail": Yahoo Mail and Proton Mail

PRODUCT_SCORE = WIKI_EXACT_SCORE - 2.5        # under the articles named exactly the word, over a demoted name match

PRODUCT_TIER_WIDE = os.environ.get("SEARCH_PRODUCT_TIER_WIDE", "0").strip() == "1"

PRODUCT_MAKER_MIN = int(os.environ.get("SEARCH_PRODUCT_MAKER_MIN", "4"))

_OFFICIAL_SUFFIX = " — official site"

def maker_in_host(maker: str, url: str, wide: bool = False, min_len: int | None = None) -> bool:
    """Is the maker's name (letters and digits) a label of the url's host? wide: or a part of the host with its dots
    and dashes removed ("suzuki" in globalsuzuki.com, "nissan" in nissan-global.com), when it has min_len letters."""
    host = _domain_of(url)
    labels = [norm_key(x) for x in host.split(".")[:-1]]
    if maker and maker in labels:
        return True
    return bool(wide and maker and len(maker) >= (PRODUCT_MAKER_MIN if min_len is None else min_len)
                and maker in norm_key(host.rsplit(".", 1)[0]))

def product_tail(key: str, title: str, url: str, wide: bool = True) -> bool:
    """Is `key` the bare tail of the multi-word title ("777x" of "Boeing 777X") with the title's head (the maker) in the
    host of url? The qualifier ("(car)") is not part of the name."""
    words = re.sub(r"\s*\(.*\)$", "", title or "").split()
    return any(norm_key(" ".join(words[i:])) == key and norm_key(" ".join(words[:i])) != key
               and maker_in_host(norm_key(" ".join(words[:i])), url, wide)
               for i in range(1, len(words)))

def product_site_lead(key: str, out: list[dict], mode: str = "1") -> None:
    """SEARCH_PRODUCT_SITE_FIRST: lift the product's own official row to the front of the exact tier. The row is the
    first official site row (title "<Article> — official site", an exact-tier or product-tier score) whose article title
    ends in the query and holds the maker in its host. It goes just above the best Wikipedia article of the exact tier
    (mode 1), or above the unpopular domain-named sites that stand over it as well (mode 2), pinned as site_over_article
    pins a team's site. Anything else above it (a popular site, a place, an account, another official site) stops it."""
    if mode == "0" or not key:
        return
    best = None
    for r in out:
        if (r.get("source") == "site" and (r.get("official") or r.get("product")) and r["title"].endswith(_OFFICIAL_SUFFIX)
                and WIKI_EXACT_SCORE - 3 <= r["score"] < WIKI_EXACT_SCORE and not r.get("famous")
                and (r.get("product_table") or product_tail(key, r["title"][:-len(_OFFICIAL_SUFFIX)], r["url"], wide=True))
                and (best is None or r["score"] > best["score"])):
            best = r
    if best is None:
        return
    above = [r for r in out if r is not best and r["score"] > best["score"]]
    lead = best["score"]
    for r in above:
        named = (r.get("source") == "site" and not r.get("official") and not r.get("popular")
                 and norm_key(_domain_of(r["url"]).split(".")[0]) == key)
        if r.get("source") == "wikipedia" or (mode == "2" and named):
            lead = max(lead, r["score"])
        else:
            return                      # something else is above it: a popular site, a place, an account, a namesake
    if lead > best["score"]:
        best["score"] = round(lead + 0.5, 2)
        best["famous"] = True
        best["product_lead"] = True

# Wikipedia opens a discontinued thing's article in the past tense ("Google Video was a free video hosting service",
# "Stadia was a cloud gaming service"): its site is dead or someone else's. Without this, "video" led with
# video.google.com and "desktop" with desktop.google.com.
_WAS = re.compile(r"^[^.]{0,160}?\b(?:was|were)\s+(?:an?|the|one)\b")

def discontinued(lead: str | None) -> bool:
    return bool(_WAS.match(lead or ""))

# Product tables (2026-11-10 follow-up). SEARCH_PRODUCTS=<file>[:<file>] names SQLite product tables
# (product_table.py: CC0 file, ODbL file); empty is off. A search recognised as "product P by maker M" gets M's own site as a
# candidate under the product tier's score (product_site_lead places it with SEARCH_PRODUCT_SITE_FIRST) and, when
# SEARCH_SITE_PAGES names the sitemap pages table, the best page of M's site holding the product's words, through the
# reranker. ?pt=0 turns it off for one request, ?pt=1 on (eval A/B). Rows with no maker_site show nothing.
PRODUCTS = [x for x in os.environ.get("SEARCH_PRODUCTS", "").split(":") if x.strip()]

SITE_PAGES = os.environ.get("SEARCH_SITE_PAGES", "").strip()

_products = None

_GOV_HOST = re.compile(r"\.(gov|mil)(\.[a-z]{2})?$|\.gov\.[a-z]{2}$")

def products_table():
    """The product tables, opened once; None when none is named or one cannot be opened (logged, search goes on)."""
    global _products
    if _products is None:
        _products = False
        if PRODUCTS:
            try:
                import product_table
                _products = product_table.Products(PRODUCTS, lambda w: w in typo_words())
                print(f"product tables: {PRODUCTS}", flush=True)
            except Exception as e:              # a missing or malformed file must not take the search down
                print(f"product tables unusable ({type(e).__name__}: {e}): {PRODUCTS}", flush=True)
    return _products or None

def product_leads_list(out: list[dict]) -> bool:
    """A government or place result already leads: a product link would be noise ("ford county courthouse")."""
    if not out:
        return False
    top = max(out, key=lambda r: r.get("score", 0.0))
    return top.get("source") == "place" or bool(_GOV_HOST.search(_domain_of(top.get("url", ""))))

def product_candidates(hit: dict, q: str, out: list[dict], pages: str = "") -> None:
    """Add the recognised maker's own site (when no row of that site is in the list) and, with `pages`, the best page of it
    for the product's words. Rows carry product_* fields so the page can credit the table (the ODbL one must be)."""
    site = (hit.get("maker_site") or "").strip()
    if not site:
        return
    if "://" not in site:
        site = "https://" + site
    if not official_ok(site):
        return
    dom = _domain_of(site)
    meta = {"product_kind": hit["kind"], "product_name": hit["name"], "product_maker": hit["maker"],
            "product_source": hit["source"], "product_licence": hit["licence"], "product_credit": hit["credit"]}
    if not any(_domain_of(r["url"]) == dom for r in out):
        o = _site(site, f"{hit['maker']} — official site", f"{hit['name']} · {hit['category']}"[:300], PRODUCT_SCORE - 0.05)
        o["product"] = True
        o["product_table"] = True
        o.update(meta)
        out.append(o)
    if pages:
        import product_table
        mk = set(product_table.tokens(hit["maker"]))
        words = [w for w in product_table.tokens(q) if w not in mk]
        for url, title in product_table.site_pages(pages, dom, words, 2):
            if not any(r["url"] == url for r in out):
                slug = re.sub(r"\.(html?|php|aspx?)$", "", urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1])
                o = _site(url, title or f"{dom} — {' '.join(re.split(r'[-_]+', slug)).strip() or url}", "", 95.0)
                o["product_page"] = True
                o.update(meta)
                out.append(o)

def maker_products(key: str, rows, lead_of=None, wide: bool = False) -> list[tuple[str, str]]:
    """PRODUCT_SQL rows (most searched first) -> [(article title, official url)] of the products the bare word names:
    at most PRODUCT_TOP, one per article, none whose article (lead_of) says it is discontinued."""
    out: list[tuple[str, str]] = []
    for wiki, name, url, _d in rows:
        words = re.sub(r"\s*\(.*\)$", "", name or "").split()
        if not wiki or not url or len(words) < 2 or any(wiki == t for t, _u in out) or not official_ok(url):
            continue
        u = urlsplit(url)
        labels = [norm_key(x) for x in _domain_of(url).split(".")[:-1]]
        segs = [norm_key(x) for x in u.path.split("/") if x]
        for i in range(1, len(words)):
            maker, rest = norm_key(" ".join(words[:i])), norm_key(" ".join(words[i:]))
            if rest == key and maker_in_host(maker, url, wide) and maker != key and (key in labels or key in segs):
                if lead_of is None or not discontinued(lead_of(wiki)):
                    out.append((wiki, url))
                break
        if len(out) == PRODUCT_TOP:
            break
    return out

def product_results(products: list[tuple[str, str]], out: list[dict], famous: str | None, lead_of) -> None:
    """Add each product's own site (once: a site an earlier tier already answered with is left as it is). The one
    famous_namesake lifted gets its site over its article, as a team does (site_over_article)."""
    have = {r["url"].rstrip("/") for r in out}
    for i, (title, url) in enumerate(products):
        if url.rstrip("/") not in have:
            o = _site(url, f"{title} — official site", (lead_of(title) or "")[:300], round(PRODUCT_SCORE - 0.1 * i, 2))
            o["product"] = True
            if title == famous:
                # only the famous one is "the official site" to the tiers that pin one first (official_top,
                # brand_site_first): as official, "video" led with Google Video and "desktop" with Google Desktop
                o["official"] = True
                o["of_kind"] = "org"
            out.append(o)
        if title == famous:
            site_over_article(out, title)

def site_over_article(out: list[dict], title: str) -> None:
    """A team's own site stays the top result, its lifted article (and card) right under it
    ("yankees" -> mlb.com/yankees first); the learned ranker may move
    neither."""
    site = next((r for r in out if r.get("official") and r["title"].startswith(title + " — ")), None)
    if site is not None:
        site["score"] = NAV_SCORE + 1.5
        site["famous"] = True

# A site named after the query with a common add-on word in its domain (
# "if people are searching meadowlands they 80/20 want the site for the actual meadowlands"):
# playmeadowlands.com, whose homepage calls itself "Meadowlands Racing & Entertainment". Two
# signals: the domain is the query plus an add-on (play-, visit-, -online …) AND the site's
# own title begins with the query. Only when no domain matches the query outright.
SELF_NAMED_PREFIX = ("play", "visit", "go", "get", "my", "the", "shop", "try", "join", "ask", "discover",
                     "explore", "experience", "official", "we", "i", "e", "hello", "meet", "see", "enjoy")

SELF_NAMED_SUFFIX = ("online", "hq", "app", "official", "usa", "us", "inc", "co", "group", "live", "tv",
                     "nj", "ny", "home", "site", "web", "world", "now")

SELF_NAMED_SQL = """
SELECT d.url, d.title, d.description, d.domain, s.tranco_rank FROM docs d LEFT JOIN serve_sites s USING (domain)
WHERE d.error IS NULL AND split_part(d.domain, '.', 1) LIKE %(like)s
ORDER BY s.tranco_rank NULLS LAST LIMIT 30
"""

_TITLE_SPLIT = re.compile(r"\s*(?:[|–—:·•]|\s-\s|::)\s*")

def self_named(q: str, rows) -> tuple | None:
    """SELF_NAMED_SQL rows -> the (url, title, desc) of the best-ranked site named after the
    query, or None."""
    key = norm_key(q)
    words = [w for w in re.split(r"[^\w]+", q.lower()) if w]
    for url, title, desc, domain, _rank in rows:
        label = norm_key(domain.split(".")[0])
        if key not in label or label == key:
            continue
        head, _, tail = label.partition(key)
        if not ((head in SELF_NAMED_PREFIX and not tail) or (tail in SELF_NAMED_SUFFIX and not head)):
            continue
        first = _TITLE_SPLIT.split((title or "").strip(), 1)[0]
        tw = [w for w in re.split(r"[^\w]+", first.lower()) if w]
        tw = tw[1:] if tw[:1] == ["the"] and words[:1] != ["the"] else tw
        if tw[:len(words)] == words:
            return url, title, desc
    return None

def lift_famous(out: list[dict], exact_titles: list[str], famous: str, lead_of) -> None:
    """Put the famous namesake's article first (score over the named site) and first among
    the exact titles (its accounts and card follow). "trump" already matched Donald
    Trump's article as an exact alias, at 899 under $Trump — lift it, never add it twice."""
    url = _wiki(famous, "", 0.0)["url"]
    hit = next((r for r in out if r.get("source") == "wikipedia" and r["url"] == url), None)
    if hit is None:
        hit = _wiki(famous, lead_of(famous), 0.0)
        out.append(hit)
    hit["score"] = NAV_SCORE + 1
    hit["famous"] = True
    if famous in exact_titles:
        exact_titles.remove(famous)
    exact_titles.insert(0, famous)

# ---------------------------------------------------------------------------
# Kinds of site (2026-09-28). "password manager", "video sharing site", "online code
# editor" name a KIND of site, not a site. Before this the Wikipedia article about the kind
# won (tier 2: "Password manager"), or a tiny domain that spells the words (tier 1:
# photo-editor.one, audiobook.com, recipes.net): 34% of 258 hand-written descriptive queries
# had a real site of that kind first (Jev, testbox). kind_docs (importer kinds.py) holds, per
# site, the kinds Wikidata, Wikipedia categories and list pages, the Open Directory and
# awesome lists give it; kind_members maps a normalised kind name to the sites carrying it.
#   - The query IS a kind name (kind_key in kind_members, >= KIND_MIN_MEMBERS sites) and is
#     not a question: the best-known sites of that kind go above the article (KIND_SCORE),
#     and unpopular domain-name matches go below them. Never `confident`: a popularity list
#     does not skip the web on its own.
#   - Otherwise the kinds BM25 list is one more list in the fusion.
# ---------------------------------------------------------------------------
KIND_FILLER = frozenset("a an the best top good great free cheap online site sites website websites "
                        "webpage app apps application applications service services platform platforms tool tools "
                        "software program programs provider providers company companies store stores for to with "
                        "of and or my".split())

_KIND_QUESTION = re.compile(r"^(how|what|whats|why|when|who|whom|which|where|is|are|was|were|does|do|did|can|should|"
                            r"history|definition|define|meaning|examples?|types?|list of|comparison)\b"
                            r"|\b(history of|vs|versus|invented|examples of|meaning of|near me)\b")

KIND_SCORE = WIKI_EXACT_SCORE + 4      # above the article about the kind, below named sites

KIND_UNDER_ARTICLE = WIKI_EXACT_SCORE - 0.1   # no site word: right under the article

KIND_TOP = 3

KIND_MIN_MEMBERS = 2      # a kind name needs this many sites to be looked up at all

KIND_MIN_GOOD = 2         # ... and this many judged good to go above the article

# Popularity: the SITES_SQL Tranco / Common Crawl terms plus Wikidata sitelinks (how many
# Wikipedias write about the site) — the best-known sites of a kind lead. Old (Jev-built) tables
# only; the open tables use KIND_POP_OPEN (no Tranco: D2, docs/legal/SEARCH-DATA-LICENSES.md).
KIND_POP = ("(0.15 * greatest(0, 14 - ln(coalesce(k.tranco_rank, 2000000)))"
            " + 0.10 * greatest(0, 18 - ln(coalesce(k.cc_hc_pos, 100000000)))"
            " + 0.08 * ln(1 + coalesce(k.sitelinks, 0)))")

# Jev's verdict (kind_members.jev, from the deleted utility/search-kind-members.py; ADR-065) decides the order first:
# a site judged a real one of the kind (>= 0.7) beats an unjudged one, which beats a doubtful
# one; below 0.3 it is dropped. Popularity orders within each band.
KIND_JEV_BAND = "10 * max(CASE WHEN m.jev >= 0.7 THEN 2 WHEN m.jev IS NULL THEN 1 WHEN m.jev >= 0.4 THEN 0.5 ELSE 0 END)"

KIND_KEY_SQL = f"""
SELECT k.url, k.title, k.descr, k.domain,
       {KIND_JEV_BAND}
       + {KIND_POP} * (1 + 0.5 * ln(sum(m.votes))) @LEAD@ @HOME@
       + %(exact_w)s * max(CASE WHEN m.key = %(exact)s THEN 1 ELSE 0 END) AS score
FROM kind_members m JOIN kind_docs k USING (domain)
WHERE m.key = ANY(%(keys)s) AND coalesce(m.jev, 0.5) >= 0.3
GROUP BY k.url, k.title, k.descr, k.domain, k.tranco_rank, k.cc_hc_pos, k.sitelinks @HOMEG@
ORDER BY score DESC LIMIT %(k)s
"""

# The open tables (importer/kinds_open.py, ADR-065, 2026-10-01) carry kind_members.conf — a
# membership confidence from open evidence (direct Wikidata statement, independent source
# families) — instead of Jev's verdict. A site is lifted only when conf >= KIND_LIFT_CONF: the
# lift is an explicit predicate, so popularity can never carry an unsupported site over the
# line (in the Jev formula an unjudged, popular, exact-name site could reach KIND_GOOD).
# Popularity is Common Crawl host rank + Wikidata sitelinks + Wikipedia pageviews.
KIND_LIFT_CONF = float(os.environ.get("SEARCH_KIND_LIFT_CONF", "0.7"))

# The open lead is coarse (own Wikidata class 0.5, named in the first sentence 0.3, agreement 0.2):
# weighted 1.5, not the Jev verdict's 4, so popularity still orders sites with the same lead
# (v1 used 2; changed with KIND_POP_OPEN after the v1 eval).
KIND_LEAD_W_OPEN = float(os.environ.get("SEARCH_KIND_LEAD_W_OPEN", "1.5"))

KIND_CONF_W = float(os.environ.get("SEARCH_KIND_CONF_W", "3"))

# Wikipedia pageviews lead (how many people look the site up), Common Crawl host rank only nudges:
# serve_sites has no rank for many well-known sites (nordvpn.com, italki.com), so a missing rank
# counts as 1M — not as last. v1 (0.04 pageviews, 0.12 rank, missing = 100M) put ipvanish.com
# over nordvpn.com and protonvpn.com (2026-10-02).
KIND_POP_OPEN = ("(0.25 * ln(1 + coalesce(k.pageviews, 0))"
                 " + 0.05 * greatest(0, 18 - ln(coalesce(k.cc_hc_pos, 1000000)))"
                 " + 0.08 * ln(1 + coalesce(k.sitelinks, 0)))")

# A query that is nothing but news words ("latest news", "breaking news", "news today") asks for news sites. The kind
# names it matches word for word are thin: "latest news" has 72 members and its list was allkpop.com,
# coinmarketcap.com, scmp.com; nytimes.com, reuters.com and the BBC are under "daily newspaper", "news agency" and
# "news channel". Such a query reads those.
NEWS_KINDS = ("daily newspaper", "news agency", "news channel", "cable news channel", "news magazine", "breaking news")

KIND_KEY_SQL_OPEN = """
SELECT k.url, k.title, k.descr, k.domain,
       CASE WHEN max(m.conf) >= @LIFT@ THEN @GOOD@ ELSE 0 END
       + least(@GOOD@ - 0.1, @CONFW@ * max(m.conf) + @POP@ * (1 + 0.5 * ln(greatest(1, max(m.votes)))) @LEAD@ @HOME@
               + %(exact_w)s * max(CASE WHEN m.key = %(exact)s THEN 1 ELSE 0 END)) AS score
FROM kind_members m JOIN kind_docs k USING (domain)
WHERE m.key = ANY(%(keys)s) AND m.conf >= 0.3
GROUP BY k.url, k.title, k.descr, k.domain, k.cc_hc_pos, k.sitelinks, k.pageviews @HOMEG@
ORDER BY score DESC LIMIT %(k)s
"""

# A kind name that contains every word of the query ("online chess" -> "chess playing"):
# the shortest few with enough members.
KIND_KEYS_SQL = """
SELECT key FROM kind_keys WHERE (key = %(key)s OR words @> %(w)s) AND n >= %(min)s
ORDER BY key = %(key)s DESC, array_length(words, 1), n DESC LIMIT 3
"""

# Meaning match (kind_vecs, tools/embed-kinds.py): a query whose words name no kind finds
# the nearest kind names by embedding ("photo editor" ~ "raster graphics editor"). Only kind
# names with >= 2 liftable sites are embedded (Jev-approved in the old tables, conf >=
# KIND_LIFT_CONF in the open ones), and only those sites are ever lifted.
KIND_SIM = float(os.environ.get("SEARCH_KIND_SIM", "0.86"))

KIND_KEY_EXACT_SQL = "SELECT key FROM kind_keys WHERE key = %(k)s AND n >= %(m)s"

# Jev's "first choice" verdict (kind_members.lead, from the deleted utility/search-kind-leaders.py) orders the
# approved sites of a kind; it never admits a site (the Jev-approval band still decides that).
# Older tables have no lead column: the term is left out, as before (2026-09-30).
KIND_LEAD_W = float(os.environ.get("SEARCH_KIND_LEAD_W", "4"))

# A site of the kind the query names exactly beats one that only a broader kind (containment
# or meaning match) brought in: "internet radio streaming" pulled Prime Video in through a
# general streaming kind (2026-09-30, r6 dev eval).
KIND_EXACT_W = float(os.environ.get("SEARCH_KIND_EXACT_W", "5"))

# Home country (2026-09-30): among a kind's approved sites, those whose owner is in the
# box's country (kind_docs.country, Wikidata P17) move up — "supermarket chain" from a US box.
# SEARCH_COUNTRY is the Wikidata English label; empty = no preference.
KIND_COUNTRY = os.environ.get("SEARCH_COUNTRY", "United States")

KIND_HOME_W = float(os.environ.get("SEARCH_KIND_HOME_W", "1"))   # a tie-breaker: 3 put fitbit over toggl

_kind_cols = (None, frozenset())

def kind_cols(c) -> frozenset:
    """Which optional kind columns the tables this connection reads have (its search_path:
    an eval schema in front of public must not mix the two)."""
    global _kind_cols
    gen = generation()
    if _kind_cols[0] != gen:
        cols = frozenset(f"{t}.{c}" for t, c in c.execute(
            "SELECT table_name, column_name FROM information_schema.columns "
            "WHERE (table_name, column_name) IN (('kind_members', 'lead'), ('kind_docs', 'country'), "
            "('kind_members', 'conf')) AND table_schema = ("
            "SELECT n.nspname FROM pg_class r JOIN pg_namespace n ON n.oid = r.relnamespace "
            "WHERE r.oid = to_regclass('kind_members'))"))
        _kind_cols = (gen, cols)
    return _kind_cols[1]

def kind_open(c) -> bool:
    """The open tables (kind_members.conf) rather than the old Jev-built ones."""
    return "kind_members.conf" in kind_cols(c)

def kind_bm25_sql(c) -> str:
    """KIND_SQL with the popularity of the tables in use (no Tranco on the open tables)."""
    return KIND_SQL.replace(KIND_POP, KIND_POP_OPEN) if kind_open(c) else KIND_SQL

def kind_key_sql(c) -> str:
    """KIND_KEY_SQL with the optional terms whose columns this database has (older tables
    have neither kind_members.lead nor kind_docs.country: the terms are left out). The open
    tables (kind_members.conf) get KIND_KEY_SQL_OPEN."""
    cols = kind_cols(c)
    if "kind_members.conf" in cols:
        sql = (KIND_KEY_SQL_OPEN.replace("@LIFT@", str(KIND_LIFT_CONF)).replace("@GOOD@", str(KIND_GOOD))
               .replace("@CONFW@", str(KIND_CONF_W)).replace("@POP@", KIND_POP_OPEN))
    else:
        sql = KIND_KEY_SQL
    lead_w = KIND_LEAD_W_OPEN if "kind_members.conf" in cols else KIND_LEAD_W
    sql = sql.replace("@LEAD@", f"+ {lead_w} * max(coalesce(m.lead, 0.5))"
                      if "kind_members.lead" in cols else "")
    home = "kind_docs.country" in cols and bool(KIND_COUNTRY)
    esc = KIND_COUNTRY.replace("'", "''")
    sql = sql.replace("@HOME@", f"+ CASE WHEN k.country = '{esc}' THEN {KIND_HOME_W} ELSE 0 END" if home else "")
    sql = sql.replace("@HOMEG@", ", k.country" if home else "")
    if "kindjev" in _drop.get():
        # eval only: no Jev verdicts — every member in the top band, no floor, no lead term
        sql = (sql.replace(KIND_JEV_BAND, "10 * 2").replace(" AND coalesce(m.jev, 0.5) >= 0.3", "")
                  .replace(f"+ {KIND_LEAD_W} * max(coalesce(m.lead, 0.5))", ""))
    return sql

def near_kinds(c, kq: str, n: int = 3) -> list[str]:
    keys, m = kind_vectors(c)
    if m is None or not len(keys):
        return []
    e = embed(kq, prefix="")
    if e is None:
        return []
    import numpy as np
    sims = m @ np.asarray(e, dtype=np.float32)
    top = np.argsort(-sims)[:n]
    return [keys[i] for i in top if sims[i] >= KIND_SIM]

# Meaning search over sites (kind_site_vec): a kind-shaped query that names no kind we know
# ("app for splitting bills with roommates") is compared with every approved site's title,
# kinds and description. One more fused list — never lifted, never `confident`.
# Since 2026-09-30 the vectors also carry doc2query's model-written queries, and cover sites
# that are in no kind (d2q-only): those show their own crawled homepage (docs).
KIND_SITE_SIM = float(os.environ.get("SEARCH_KIND_SITE_SIM", "0.72"))

KIND_SITE_SQL = """
SELECT coalesce(k.url, d.url), coalesce(k.title, nullif(d.title, ''), v.domain), coalesce(k.descr, d.description, ''),
       v.domain, v.sim
FROM (SELECT domain, 1 - (v <=> %(e)s::halfvec) AS sim FROM kind_site_vec
      ORDER BY v <=> %(e)s::halfvec LIMIT 8) v
LEFT JOIN kind_docs k ON k.domain = v.domain
LEFT JOIN LATERAL (SELECT url, title, description FROM docs WHERE domain = v.domain AND error IS NULL
                   ORDER BY fetched_at DESC LIMIT 1) d ON k.domain IS NULL
WHERE k.domain IS NOT NULL OR d.url IS NOT NULL
ORDER BY v.sim DESC
"""

def _kind_site_rows(q: str) -> list[tuple]:
    e = embed(q)
    if e is None:
        return []
    vec = "[" + ",".join(f"{x:.5f}" for x in e) + "]"
    return [r for r in _rows(KIND_SITE_SQL, {"e": vec}, "SET hnsw.ef_search = 40") if r[4] >= KIND_SITE_SIM]

KIND_GOOD = 20.0          # a member Jev judged a real site of the kind (>= 0.7): only these are lifted

KIND_SQL = f"""
SELECT url, title, descr, domain, bm25 * (1 + pop) AS score FROM (
  SELECT k.url, k.title, k.descr, k.domain, {KIND_POP} AS pop,
         -(k.search_text <@> to_bm25query(%(q)s, 'kind_docs_bm25')) AS bm25
  FROM kind_docs k
  ORDER BY k.search_text <@> to_bm25query(%(q)s, 'kind_docs_bm25') LIMIT 200) x
ORDER BY score DESC LIMIT %(k)s
"""

_NOT_PLURAL = frozenset("news series species gps dns aws ios macos sms bus lens chess glass".split())

def _singular(w: str) -> str:
    if w in _NOT_PLURAL:
        return w
    if len(w) > 4 and w.endswith("ies"):
        return w[:-3] + "y"
    if len(w) > 3 and w.endswith("s") and not w.endswith(("ss", "us", "is")):
        return w[:-1]
    return w

def kind_key(text: str) -> str:
    """A kind name, normalised: "Password Managers" / "password manager app" ->
    "password manager". Must match importer/kinds.py kind_key() — test_search_kinds.py."""
    words = [_singular(w) for w in _TOKEN.findall(text.lower()) if w not in KIND_FILLER]
    return " ".join(words)

def kind_query(q: str) -> str | None:
    """The kind a query names, or None for a question / an empty remainder."""
    t = q.lower().strip()
    if _KIND_QUESTION.search(t) or domain_query(t):
        return None
    k = kind_key(t)
    return k if 2 <= len(k) <= 60 else None

# Words that say "a site / app / service" — not articles or adjectives ("the learning" is
# not a request for learning sites, 2026-09-28 regression eval).
KIND_SITE_WORDS = frozenset("online site sites website websites webpage app apps application applications service "
                            "services platform platforms tool tools software program programs provider providers "
                            "store stores".split())

def kind_base(q: str, has_article: bool) -> float:
    """Where the lifted sites of a kind start. "cloud computing", "video game": a topic that is
    also a kind keeps its article first unless the query asks for a site ("... app",
    "online ...") — 2026-09-28 (linode.com had topped "cloud computing")."""
    site_asked = (any(w in KIND_SITE_WORDS for w in _TOKEN.findall(q.lower()))
                  or sites_intent(q) >= SITES_INTENT_MIN)
    return KIND_SCORE if site_asked or not has_article else KIND_UNDER_ARTICLE

# A task names the tool that does it ("merge pdf files online" -> "pdf merger", "convert video
# to mp3" -> "video converter" / "file converter"). Only candidate kind names: a lift still
# needs >= 2 Jev-approved sites of that kind, so a wrong rewrite finds nothing (2026-09-30).
TASK_TOOL = {"merge": "merger", "combine": "merger", "convert": "converter", "edit": "editor",
             "record": "recorder", "track": "tracker", "compress": "compressor", "translate": "translator",
             "shorten": "shortener", "download": "downloader", "generate": "generator", "make": "maker",
             "create": "maker", "scan": "scanner", "check": "checker", "plan": "planner",
             "calculate": "calculator", "play": "player", "manage": "manager", "draw": "drawing",
             "sign": "signature", "host": "hosting", "share": "sharing", "store": "storage"}

TASK_GENERIC = {"converter": "file converter", "compressor": "file compressor"}

_TASK_SKIP = KIND_FILLER | frozenset("file files my your a the online free into in on".split())

def task_kinds(q: str) -> list[str]:
    """Candidate tool kind names for a task-shaped query; [] when it does not start with a task verb."""
    words = _TOKEN.findall(q.lower())
    if not words or words[0] not in TASK_TOOL:
        return []
    noun = TASK_TOOL[words[0]]
    objs = [w for w in words[1:] if w not in _TASK_SKIP]
    out = []
    if objs:
        out.append(kind_key(" ".join(objs[:2]) + " " + noun))
        out += [kind_key(o + " " + noun) for o in objs[:2]]
    if noun in TASK_GENERIC:
        out.append(TASK_GENERIC[noun])
    return list(dict.fromkeys(k for k in out if k))

def kind_worded(q: str) -> bool:
    """The query says it wants a site ("... app", "online ...") or names a kind in 2+ words.
    A bare topic word ("chess", "email") is not lifted: its article stays first."""
    words = _TOKEN.findall(q.lower())
    return any(w in KIND_SITE_WORDS for w in words) or len([w for w in words if w not in KIND_FILLER]) >= 2

# The kinds tier recommends: "the best-known sites of this kind". Two things it got wrong (2026-10-09: "cnn
# breaking news" led with dailystormer.in, then bbc.com and allkpop.com, and cnn.com was 4th at 98).
# 1. Nothing asked what a site IS. The list holds the members with conf >= KIND_LIFT_CONF, and conf is highest (0.85)
#    for a site whose own Wikidata class is the kind: The Daily Stormer is classed a news website, the BBC (0.75) and
#    CNN (0.55, never lifted) are broadcasters. UT1's hate list does not hold that domain, and the category data
#    cannot tell: nytimes.com, facebook.com and whitehouse.gov are in "fake news websites" too. The article's own
#    first sentence can ("... was an American neo-Nazi commentary and message board website"): of the 108,135
#    liftable sites of rc6, 43 are described that way (infowars.com, stormfront.org, thegatewaypundit.com,
#    naturalnews.com, patriotfront.us). They are still found by their name; they are not recommended.
# 2. The site the query names was ignored: "breaking news" came from the meaning match, which does not see "cnn".
#    A member of the kind whose domain label is a word of the query leads the list, liftable or not.
_NOT_RECOMMENDED = re.compile(r"\b(?:neo-?nazi|white[- ]supremac|white[- ]nationalis|holocaust[- ]deni|hate group|"
                              r"fake[- ]news (?:web)?site|conspiracy[- ]theor)", re.I)

KIND_NAMED_POOL = 60          # members read to find the one the query names (the list itself stays KIND_TOP)

def not_recommended(claims) -> bool:
    """site_claims rows of a site -> an article about it opens by calling it a hate, hoax or conspiracy site."""
    return any(_NOT_RECOMMENDED.search(first_sentence(c[3]) + " " + (c[4] or "")) for c in claims)

def kind_unfit(url: str) -> bool:
    lk = lookups()
    if lk is None or lk.name != "release":
        return False
    return not_recommended(lk.site_claims(_domain_of(url))[1])

def kind_named_first(q: str, good: list[tuple], rows: list[tuple]) -> list[tuple]:
    """(url, title, descr, domain, score) rows: the member whose domain label is a word of the query goes first
    ("cnn breaking news" -> cnn.com, a member at 0.55). Filler words name no site ("best", "online")."""
    words = {w for w in _TOKEN.findall(q.lower()) if len(w) >= 3 and w not in KIND_FILLER}
    named = next((r for r in rows if r[3].split(".")[0] in words), None)
    if named is None or (good and good[0][3] == named[3]):
        return good
    return [named] + [r for r in good if r[3] != named[3]]

def _kind(url, title, descr, score, popular=False) -> dict:
    r = _site(url, title or url, descr or "", score, popular)
    r["kind"] = True
    return r

# "Top <kind>" card (2026-09-30, approved mockup): when a kind's sites are lifted to the top,
# the page shows them together in one box (up to 5) instead of as the first rows. Titled with
# the query's own words — kind keys are normalised ("window text editor") and read badly.
KIND_CARD_N = 5

def _plural(w: str) -> str:
    if w in _NOT_PLURAL or w.endswith("s"):
        return w
    if w.endswith("y") and len(w) > 2 and w[-2] not in "aeiou":
        return w[:-1] + "ies"
    return w + ("es" if w.endswith(("x", "ch", "sh")) else "s")

_CARD_HEAD = KIND_SITE_WORDS | frozenset("company companies".split())

_CARD_AS_TYPED = frozenset("for with to of that which who".split())

def kind_card_title(q: str) -> str:
    """"password manager" -> "Password managers"; "video sharing site" -> "Video sharing sites"
    (the site word stays when it ends the query: "Video sharings" is not a name)."""
    toks = _TOKEN.findall(q.lower())
    words = [w for w in toks if w not in KIND_FILLER]
    if not words or task_kinds(q) or _CARD_AS_TYPED & set(toks):
        t = q.strip()[:60]                  # "app for splitting bills with roommates"
        return t[:1].upper() + t[1:]
    if toks[-1] in _CARD_HEAD:
        words.append(toks[-1])
    words[-1] = _plural(words[-1])
    t = " ".join(words)[:60]
    return t[:1].upper() + t[1:]

def kind_card(q: str, rows) -> dict | None:
    sites = [{"url": url, "title": title or dom, "domain": dom} for url, title, _d, dom, _s in rows]
    return {"title": kind_card_title(q), "sites": sites} if len(sites) >= KIND_MIN_GOOD else None

def demote_name_matches(q: str, out: list[dict], keep: set[str]) -> None:
    """A kind query ("audiobook app"): a domain-name match (audiobook.com, and cnet.com for
    "cloud storage service") goes under the best-known sites of that kind — unless the query
    is exactly that popular site's name ("chess" -> chess.com)."""
    for r in out:
        if (r.get("source") == "site" and r["score"] > KIND_SCORE and not exact_name_top(q, r)
                and not r.get("official") and _domain_of(r["url"]) not in keep):
            r["score"] = round(KIND_UNDER_ARTICLE - KIND_TOP - 1 - (NAV_SCORE - min(r["score"], NAV_SCORE)) / 100, 2)

# Stack Exchange Q&A (se_docs): keyword tier, beside homepages. A well-voted question
# gets a small lift; the link back to the question is the CC BY-SA attribution.
SE_SQL = """
SELECT site, qid, title, answer, -(search_text <@> to_bm25query(%(q)s, 'se_docs_bm25')) AS bm25, score
FROM se_docs
ORDER BY search_text <@> to_bm25query(%(q)s, 'se_docs_bm25') LIMIT %(k)s
"""

# ---------------------------------------------------------------------------
# Places (OpenStreetMap): "aldi", "harbor freight", "restaurants", "gym near me"
# answered with the nearest matches to a TOWN-LEVEL home point (SEARCH_HOME="lat,lon"
# in the search-api environment; unset = no place results). Category words map to OSM
# kinds; anything else is tried as a place name or brand.
# ---------------------------------------------------------------------------
PLACE_CATEGORY_SCORE, PLACE_NAME_SCORE = 950.0, 940.0

# "wells fargo": branches just under the bank's article and site. Places start at WIKI_EXACT_SCORE minus this, so a
# negative value is an order above the article (-40: the place tier, 940; -105: above the named sites). sweeps it.
# rc9 default -40 (was 20). SEARCH_PLACE_UNDER_THING=20 restores rc8.
PLACE_UNDER_THING = float(os.environ.get("SEARCH_PLACE_UNDER_THING", "-40"))

NEAR_FUZZY_KM = 10.0         # a loosely matched name beyond this is dropped when the query says "near me"

PLACE_LEAD = 5               # local intent: nearby places above every named-site tier (NAV_SCORE + 5)

# A category with no place of its own kind nearby asks again with these (OSM-only data has no
# cuisine=pizza kind: "pizza" then means any restaurant, as before 2026-10-02).
PLACE_KIND_FALLBACK = {"pizza": ["amenity=restaurant"]}

def possessive_variant(name: str) -> str | None:
    """"wendys" -> "wendy's", "dunkin donuts" -> None: the name a brand is stored under when its last word
    was typed without the apostrophe (OSM names are "Wendy's", "McDonald's"; the trigram test is 0.5, under the
    0.6 the name match needs, so a bare "wendys" found no branch at all, owner 2026-10-11). None when the
    name already has one or is too short to guess."""
    words = name.split()
    if not words or "'" in name or "\u2019" in name:
        return None
    last = words[-1]
    if len(last) < 5 or not last.endswith("s") or last.endswith("ss"):
        return None
    return " ".join(words[:-1] + [last[:-1] + "'s"])

# Distance in km (equirectangular — exact enough inside a county-sized radius).
PLACES_SQL = """
SELECT osm, name, kind, street, city, hours, website, phone, d_km, lat, lon FROM (
    SELECT p.*, 111.32 * sqrt(power(p.lat - %(lat)s, 2)
                              + power((p.lon - %(lon)s) * cos(radians(%(lat)s)), 2)) AS d_km
    FROM places p
    WHERE p.lat BETWEEN %(lat)s - %(dlat)s AND %(lat)s + %(dlat)s
      AND p.lon BETWEEN %(lon)s - %(dlon)s AND %(lon)s + %(dlon)s
      AND {match}) x
WHERE d_km <= %(r)s ORDER BY d_km LIMIT 3
"""

PLACE_MATCH_KINDS = ("(p.kind = ANY(%(kinds)s) OR (%(hint)s <> '' AND p.kind = ANY(%(hint_kinds)s) "
                     "AND lower(p.name) LIKE %(hint)s))")

# A category OSM never tags as a kind: "pizza" is also a restaurant with "pizz" in its name
# (an Overture pizza place at an OSM restaurant's address is dropped as its duplicate, and the
# OSM row says only "restaurant": Rockville found 1 pizza place, 2026-10-02).
PLACE_NAME_HINT = {"pizza": ("%pizz%", ["amenity=restaurant", "amenity=fast_food"])}

# The first group is what the indexes answer (name, brand, trigram — run with
# PLACE_NAME_SETUP): without it the similarity test ran on every place in the 40 km box,
# 98k rows over 45k heap blocks, 332 ms p50 on indexhost for the 77% of queries shaped like
# a name (2026-09-26); with it 30 ms. The second group is the exact test, unchanged.
PLACE_MATCH_NAME = ("(lower(p.name) = %(name)s OR (p.brand <> '' AND lower(p.brand) = %(name)s) "
                    "OR lower(p.name) %% %(name)s) "
                    "AND (lower(p.name) = %(name)s OR (p.brand <> '' AND lower(p.brand) = %(name)s) "
                    "OR similarity(lower(p.name), %(name)s) >= 0.6)")

PLACE_NAME_SETUP = "SET pg_trgm.similarity_threshold = 0.6"

# ---------------------------------------------------------------------------
# Opening hours: which to show, how old they are, whether to trust them (2026-10-02).
# Sources, kept apart (docs/legal/PLACES-AND-HOURS-LICENSING.md): OpenStreetMap's
# opening_hours (+ hours_checked: the newest check_date / survey:date tag; edited: the
# element's last edit) and hours_web (the business's own schema.org hours, from Common Crawl,
# with the crawl date). The newer plausible one wins; one older than HOURS_STALE_DAYS is shown
# as "may have changed". Never "open now" from a stale or unreadable value (that's the page's).
# ---------------------------------------------------------------------------
HOURS_STALE_DAYS = int(os.environ.get("SEARCH_HOURS_STALE_DAYS", "548"))      # ~18 months

HOURS_INFO_SQL = ("SELECT p.osm, p.hours_checked, p.edited, coalesce(w.hours, ''), coalesce(w.crawled, ''), "
                  "coalesce(w.url, '') FROM places p LEFT JOIN hours_web w ON w.osm = p.osm WHERE p.osm = ANY(%(ids)s)")

# "24/7" is rare for these and was common in stale OSM data (1,531 CVS: most-common value "24/7")
SUSPECT_ALWAYS_OPEN = frozenset({"amenity=dentist", "healthcare=dentist", "amenity=bank", "amenity=library",
                                 "amenity=post_office", "shop=hardware", "shop=doityourself", "shop=pet",
                                 "amenity=cafe", "amenity=restaurant", "cuisine=pizza", "amenity=pharmacy",
                                 "amenity=marketplace", "amenity=veterinary", "shop=laundry",
                                 "amenity=bar", "amenity=pub"})

ALWAYS_OPEN_IF_STATED = frozenset({"amenity=pharmacy", "amenity=veterinary", "shop=laundry"})

# Kinds that really keep late hours (07:00-02:00 is a normal drive-thru, bar or hotel desk). For any
# other kind a day that opens in the morning and "closes" 01:00-07:00 is a 5 PM written as 05:00
# ("Mo,We,Th 08:00-05:00", a dentist; web crawl 2026-10-03: ~900 such rows outside these kinds).
LATE_HOURS_KINDS = frozenset({"amenity=fast_food", "amenity=restaurant", "cuisine=pizza", "amenity=bar", "amenity=pub",
                              "amenity=cafe", "amenity=fuel", "amenity=hospital", "tourism=hotel", "amenity=nightclub",
                              "amenity=casino", "amenity=ice_cream", "leisure=fitness_centre", "shop=convenience",
                              "shop=supermarket", "amenity=pharmacy"})

_SPAN = re.compile(r"(\d{2}):(\d{2})-(\d{2}):(\d{2})")

def pm_misread(hours: str, kind: str) -> bool:
    """Opens 06:00-13:00 and closes 01:00-07:00 on the same line, for a kind that does not keep late hours."""
    if kind in LATE_HOURS_KINDS:
        return False
    for m in _SPAN.finditer(hours):
        a, b = int(m[1]) * 60 + int(m[2]), int(m[3]) * 60 + int(m[4])
        if b < a and 6 * 60 <= a <= 13 * 60 and 60 <= b <= 7 * 60:
            return True
    return False

def hours_plausible(hours: str, kind: str, src: str) -> bool:
    h = " ".join((hours or "").split())
    if not h:
        return False
    if re.fullmatch(r"(?:(?:Mo|Tu|We|Th|Fr|Sa|Su)[-,A-Za-z]* )?(?:off|closed)", h, re.I):
        return False                                           # "closed" every day: not a business's hours
    if h in ("24/7", "Mo-Su 00:00-24:00", "00:00-24:00") and kind in SUSPECT_ALWAYS_OPEN:
        # a business's own site saying 24/7 is believed where round-the-clock places exist
        # (emergency vets, 24-hour pharmacies); for a restaurant, bank or bar it was a site error
        # (crawl 2026-10-03: 106 restaurants, 34 banks, 34 bars "24/7")
        if src != "web" or kind not in ALWAYS_OPEN_IF_STATED:
            return False
    if pm_misread(h, kind):
        return False
    # "opens" left empty on a site's form reads as midnight: "Mo 00:00-20:00" (crawl, 2026-10-02)
    if re.search(r"(?:^|[ ,])00:00-(1[2-9]|2[0-3]):\d{2}", h):
        return False
    return True

def place_hours_info(ids: list[str]) -> dict:
    """osm -> (hours_checked, edited, web_hours, web_crawled, web_url); {} when the table or
    backend has none of it (older databases, the SQLite lookup file)."""
    if not ids:
        return {}
    try:
        return {r[0]: r[1:] for r in _rows(HOURS_INFO_SQL, {"ids": ids})}
    except Exception:
        return {}

def _days_since(iso: str) -> int | None:
    try:
        return (dt.date.today() - dt.date.fromisoformat(iso[:10])).days
    except (TypeError, ValueError):
        return None

def choose_hours(osm_hours: str, kind: str, info: tuple | None) -> tuple[str, str, str]:
    """-> (hours, as_of, source) — source "web" (the business's site), "osm" (checked on the
    ground) or "osm-edited" (only the map edit date); ("", "", "") when none is believable."""
    checked, edited, web, crawled, _url = info or ("", "", "", "", "")
    cands = []
    if web and hours_plausible(web, kind, "web"):
        cands.append((crawled, web, "web"))
    if osm_hours and hours_plausible(osm_hours, kind, "osm"):
        cands.append((checked, osm_hours, "osm") if checked else (edited, osm_hours, "osm-edited"))
    if not cands:
        return "", "", ""
    # newest evidence wins; on a tie the business's own word
    as_of, hours, src = max(cands, key=lambda c: (c[0] or "", c[2] == "web"))
    return hours, as_of, src

def with_hours(r: dict, osm_hours: str, kind: str, info: tuple | None) -> dict:
    """The place result with the chosen hours: content says them (list-only clients), and
    hours / hours_asof / hours_src / hours_stale let the page say how sure it is."""
    hours, as_of, src = choose_hours(osm_hours, kind, info)
    # "Mo-Th 09:00-17:00, Fr 09:00-18:00": rules split by commas (common in OSM) read as one
    # malformed rule by the page; the standard separator is "; "
    hours = re.sub(r",\s+(?=(?:Mo|Tu|We|Th|Fr|Sa|Su|PH)\b)", "; ", hours)
    if osm_hours and hours != osm_hours:                       # the row's own text named the OSM value
        r["content"] = " · ".join(x for x in r["content"].split(" · ") if x != osm_hours)
        if hours:
            what, _, rest = r["content"].partition(" · ")
            r["content"] = " · ".join(x for x in (what, hours, rest) if x)
    elif hours and not osm_hours:
        what, _, rest = r["content"].partition(" · ")
        r["content"] = " · ".join(x for x in (what, hours, rest) if x)
    if hours:
        age = _days_since(as_of)
        r.update(hours=hours, hours_asof=as_of, hours_src=src,
                 hours_stale=bool(age is None or age > HOURS_STALE_DAYS))
    return r

def _place(osm, name, kind, street, city, hours, website, phone, d_km, score=0.0, lat=None, lon=None) -> dict:
    where = ", ".join(x for x in (street, city) if x)
    miles = float(d_km) * 0.621371
    title = f"{name} — {where + ' — ' if where else ''}{miles:.1f} mi"
    what = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", kind.split("=", 1)[-1]).replace("_", " ").lower()
    detail = " · ".join(x for x in (what, hours, phone) if x)
    # A place opens on a map ("physical locations should be clickable and
    # open in default maps"): its OpenStreetMap page, or for a Web Data Commons business
    # (no OSM page) OpenStreetMap at its point; the launcher turns lat/lon into geo:.
    # Overture (ov:, 2026-10-02) has no OSM page either.
    if not osm.startswith(("node/", "way/", "relation/")) and lat is not None:
        url = f"https://www.openstreetmap.org/?mlat={lat:.5f}&mlon={lon:.5f}#map=17/{lat:.5f}/{lon:.5f}"
    else:
        url = f"https://www.openstreetmap.org/{osm}"
    r = {"url": url, "title": title, "content": detail[:300], "source": "place", "score": round(score, 2), "pid": osm}
    if lat is not None and lon is not None:
        r["lat"], r["lon"] = round(float(lat), 5), round(float(lon), 5)
    # the business's own site (2026-10-10): where the hours are when we hold none ("Hours on their site")
    if website and re.match(r"https?://[^\s]+$", website) and len(website) <= 300:
        r["site"] = website
    return r

def chain_fill(results: list[dict], names: dict) -> None:
    """In a list of places ("pharmacy", "walgreens near me"): a branch with no hours of its own gets its chain's
    usual hours, marked hours_src "chain" and stale (the page shows them as typical and claims no open/closed).
    names: pid -> the place's name. A branch that has hours keeps them; a name with no agreeing chain gets nothing."""
    usual: dict = {}
    for r in results:
        name = names.get(r.get("pid"))
        if r.get("source") != "place" or r.get("hours") or not name:
            continue
        keys = tuple(place_keys(name))
        if keys not in usual:
            try:
                usual[keys] = chain_hours([h for (h,) in _rows(CHAIN_HOURS_SQL, {"keys": list(keys)})])
            except Exception:                                   # no hours_web in this database
                usual[keys] = None
        if usual[keys]:
            r.update(hours=usual[keys], hours_src="chain", hours_asof="", hours_stale=True)

def town_reading_far(rows: list[tuple], near_km: float | None, stated: bool) -> bool:
    """"<name> <town>" is not what was asked (2026-10-11): "walmart center" from New Jersey answered with the
    Walmart Supercenter of Center, Texas, above the Walmart Garden Center 6 miles off. The town reading is dropped
    when a place nearby is named by the whole query (near_km: the nearest such), every place it found is farther than
    a named place is looked for (PLACE_EXACT_KM), and the query names no state. "wawa orlando" and "sun spa cedar
    mills" asked from far away keep theirs: nothing nearby is called that. rows: place_town()'s, d_km last."""
    return (not stated and near_km is not None and near_km <= PLACE_EXACT_KM
            and bool(rows) and min(r[10] for r in rows) > PLACE_EXACT_KM)

def place_site(pl: dict | None, out: list[dict]) -> dict | None:
    """A named place's own site ("parx casino" -> parxcasino.com, not crawled) as a first
    answer, unless a result is already on it."""
    if not pl or not pl["own_site"]:
        return None
    host = same_page(pl["own_site"])
    if any(same_page(r["url"]).split("/")[0] == host for r in out):
        return None
    r = _site(pl["own_site"], f"{pl['name']} — official site",
              ", ".join(x for x in (pl["street"], pl["city"]) if x), NAV_SCORE)
    r["official"] = True
    return r

FUZZY_SIM = float(os.environ.get("SEARCH_FUZZY_SIM", "0.45"))   # the tier-4 fallback's pg_trgm threshold (both lists below)

FUZZY_SITE_SQL = """
SELECT d.url, d.title, d.description, similarity(split_part(d.domain, '.', 1), %(key)s) AS sim
FROM docs d
WHERE d.error IS NULL AND split_part(d.domain, '.', 1) %% %(key)s
ORDER BY sim DESC LIMIT 3
"""

FUZZY_WIKI_SQL = """
SELECT title, lead, similarity(title, %(q)s) AS sim FROM wiki_docs
WHERE title %% %(q)s ORDER BY sim DESC LIMIT 2
"""

# _search's own stretches (2026-10-08): a misspelt query logged search:10431 with 0.6 s of named stages
# inside it. With the fuzzy fallback timed, a 14.3 s request had 12.8 s in fuzzy_wiki. setup, exact (the named
# tiers, names included), lists_wait (collecting the lists started in setup), fuzzy_site and fuzzy_wiki
# (fuzzy_read: their trigram lists) now add up, with ranker and ce, to search.
def stage_since(name: str, t: float) -> float:
    """Adds the time since t to stage `name` and returns the time now, to start the next stretch from: for
    stretches of _search too long to indent under stage()."""
    st, now = _stages.get(), time.monotonic()
    if st is not None:
        st[name] = st.get(name, 0.0) + (now - t) * 1000
    return now

def _tr(step: str, rows: list[dict] | None = None, **info) -> None:
    tr = _trace.get()
    if tr is not None:
        tr.append(dict(info, step=step, **(trace_rows(rows) if rows is not None else {})))

def _tl(name: str, lst: list[dict]) -> list[dict]:
    """One of the lists the fusion blends, noted under its name when a trace is on."""
    _tr("list", lst, name=name)
    return lst

TOWN_TITLE_SQL = "SELECT title FROM wiki_docs WHERE title = ANY(%(t)s)"

TOWN_WORD_MIN_POP = 5000

def town_titles(name: str, kind: str, county: str, state: str) -> list[str]:
    """Wikipedia's titles for a US municipality, most specific first: "Marsh Township,
    Marlow County, New Jersey", "Stone Township, New Jersey", "Paris, Lamar County,
    Texas", "Twin Falls, New Jersey"."""
    st = STATE_NAME.get(state, "")
    if not st:
        return []
    out = []
    if kind == "township":
        out += [f"{name} Township, {county}, {st}", f"{name} Township, {st}"]
    if county:
        out.append(f"{name}, {county}, {st}")
    out.append(f"{name}, {st}")
    return [t for t in out if ", ," not in t]

def town_article(q: str, c) -> str | None:
    tq = town_query(q)
    if not tq:
        return None
    row = _news_rows(TOWN_SQL, {"k": tq[0], "s": tq[1]})
    bare = _TOWN_TYPE.sub("", tq[0]).strip()
    if not row and bare and bare != tq[0]:          # "marsh twp" is no town name; "ocean city" is
        row = _news_rows(TOWN_SQL, {"k": bare, "s": tq[1]})
    if not row or (tq[2] and (row[0][3] or 0) < TOWN_WORD_MIN_POP):
        return None
    titles = town_titles(row[0][0], row[0][1] or "", row[0][2] or "", tq[1])
    have = {r[0] for r in c.execute(TOWN_TITLE_SQL, {"t": titles})}
    return next((t for t in titles if t in have), None)

NAMED_TOWN_MIN_POP = 10000

_stored = (_NEVER, frozenset())

def stored_text(c) -> frozenset:
    """Tables that still carry a stored search_text column; asked once per TABLES_TTL_S."""
    global _stored
    if time.monotonic() - _stored[0] > TABLES_TTL_S:
        _stored = (time.monotonic(), frozenset(r[0] for r in c.execute(_STORED_SQL)))
    return _stored[1]

# An exact title match far less searched than the first one is a namesake, not a second
# meaning (2026-10-01: "att" put the AT-T tractor, 268 searches/month, and Melanie and Martina
# Grant via their album "A Twin Thing", 1,604, beside AT&T's 17,970). Real ambiguities are
# close: Mercury the element and the planet ~1:1, Python the language and the snake ~7:1.
NAMESAKE_RATIO = 10

NAMESAKE_SCORE = 1.0                # below every result: the end of the list

WIKI_DEMAND_SQL = "SELECT wiki, max(demand) FROM suggest_items WHERE kind = 'wiki' AND wiki = ANY(%(t)s) GROUP BY wiki"

def namesakes(titles: list[str], demand: dict, sited: set[str] = frozenset()) -> set[str]:
    """Exact matches after the first with under 1/NAMESAKE_RATIO of its demand. Nothing when
    the first has no demand figure (no basis to judge). An article with a popular official site
    never is one: "apple" -> Apple Inc., "jaguar" -> Jaguar Cars, "chase" -> Chase Bank are each
    searched less as articles than the fruit, the cat and the word, and are what most mean."""
    lead = demand.get(titles[0]) if titles else None
    if not lead:
        return set()
    return {t for t in titles[1:] if t not in sited and (demand.get(t) or 0) * NAMESAKE_RATIO < lead}

def spaced_as_asked(rows: list, q: str) -> list:
    """Exact-title rows with the titles written in the query's own words first (2026-11-09). The exact lookup keys
    on letters and digits only, so "west ham" finds both "Westham" (a Sussex parish) and "West Ham": the parish led,
    with its council's site above everything and its card. A title whose words are the query's words, in order, is
    the one that was typed; the joined or split spelling follows. Order is otherwise kept, and nothing moves when no
    title is written as asked ("wellsfargo", a typo'd spacing)."""
    asked = _TOKEN.findall(q.lower())
    as_asked = [_TOKEN.findall(re.sub(r"\s*\([^)]*\)\s*$", "", r[0]).lower()) == asked for r in rows]
    if not any(as_asked) or all(as_asked):
        return list(rows)
    return [r for r, a in zip(rows, as_asked) if a] + [r for r, a in zip(rows, as_asked) if not a]

def _site(url: str, title: str, desc: str, score: float, popular: bool = False) -> dict:
    r = {"url": url, "title": title or url, "content": (desc or "")[:300],
         "source": "site", "score": round(float(score), 2)}
    if popular:
        r["popular"] = True
    return r

# A site found by one word of a name (2026-11-06, "west ham united" -> unitedutilities.com on "united",
# westpoint.edu on "west", accesskent.com from the kind list): when the query is an entity's name (an exact
# article title), a site from the word lists must hold two of the article title's words side by side ("West Ham
# News"), or them joined in its host ("westham…"). The title, not the query: a typo ("beyonce knowels", answered
# as Beyoncé) pairs nothing. A one-word query or title, and one-letter words ("F.C."), have nothing to pair.
_WORDS = re.compile(r"[a-z0-9]+")

def _plain_words(s: str) -> list[str]:
    return _WORDS.findall(unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower())

def name_pairs(name: str) -> list[str]:
    """A name's neighbouring word pairs ("west ham", "ham united"), its "(…)" and one-letter words dropped."""
    w = [x for x in _plain_words(re.sub(r"\s*\([^)]*\)\s*$", "", name or "")) if len(x) > 1]
    return [f"{a} {b}" for a, b in zip(w, w[1:])]

def holds_a_pair(r: dict, pairs: list[str]) -> bool:
    host = (urlsplit(r.get("url") or "").hostname or "").lower()
    text = " " + " ".join(_plain_words(f"{r.get('title') or ''} {r.get('content') or ''}")) + " "
    return any(f" {p} " in text or p.replace(" ", "") in host for p in pairs)

def named_only(lst: list[dict], pairs: list[str]) -> list[dict]:
    """A word list's sites that hold a pair of the name's words (pairs empty: the list as it is)."""
    return [r for r in lst if holds_a_pair(r, pairs)] if pairs else lst

# One site's home page under other names ("west ham united": whufc.com, whufc.com/en and cdn.whufc.com/en were
# three results): a language root and a CDN or asset host are the same entry as the site's home page. The language
# codes are listed (not any two letters), so a section such as /tv stays its own page.
_LANG_ROOT = re.compile(r"^/(?:en|es|fr|de|it|pt|nl|ja|zh|ko|ru|pl|sv|da|no|fi|tr|ar|he|hi|id|th|vi|cs|el|hu|ro|uk|"
                        r"intl|global|home|index\.html?)(?:[-_][a-z]{2})?/?$", re.I)

_ASSET_HOST = re.compile(r"^(?:cdn|static|assets|img|images)\d*\.(?=[^.]+\.[^.]+)", re.I)

def result_key(r: dict) -> str:
    """What makes two results the same entry: the page — but a place is itself, not its map link. A place with no
    OpenStreetMap page of its own (the gap-fill file: Overture, All the Places) links to the map at its point, and
    every such link is one page to same_page(): three Starbucks in a row were kept as one (2026-10-10)."""
    if r.get("source") == "place" and r.get("pid"):
        return "place:" + r["pid"]
    return same_page(r["url"])

def home_key(r: dict) -> str:
    """result_key(), with a site's language root and its CDN host read as its home page."""
    key = result_key(r)
    if key.startswith("place:"):
        return key
    host, _, path = key.partition("/")
    host = _ASSET_HOST.sub("", host)
    return host if _LANG_ROOT.match("/" + path) else host + ("/" + path if path else "")

def one_home_page(res: list[dict]) -> list[dict]:
    """A site's home page once, the first of its names kept where it stands. Run after the rerankers, so it
    only drops a later copy: merged before them, the copy kept could be the one the rerankers like less, and the
    site lost the first row ("brow gel": benefitcosmetics.com/en-au first became maybelline.com, 2026-11-04)."""
    seen: set[str] = set()
    out = []
    for r in res:
        key = home_key(r)
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out

# "Wants sites of a kind" (2026-09-30): the intent model's sixth label, Jev's answer to "is the
# best answer a short list of real websites or apps of a certain kind?" (password manager,
# video sharing site). A query that says so without a site word ("tollywood ott", "discord
# bots") gets its kind's sites above the article, like "... app" does. 0.0 on older models.
SITES_INTENT_MIN = float(os.environ.get("SEARCH_SITES_INTENT_MIN", "0.7"))

def demote_weak_names(out: list[dict]) -> None:
    """When the query is exactly a Wikipedia title, an unpopular domain-name match that
    is NOT that article's own official site goes below the article: "fdr" meant
    Franklin D. Roosevelt, not fdr.com.br; "get out" the film, not getout.com
    (2026-09-24 eval). Popular sites and the article's official site keep tier 1."""
    if not any(r.get("source") == "wikipedia" and r["score"] >= WIKI_EXACT_SCORE - 3 for r in out):
        return
    official = {_domain_of(r["url"]) for r in out if r.get("official") and not r.get("place")}
    # a place's own site: neither its popularity nor being the article's official domain keeps it above the
    # article ("cadiz" -> the article, then cadiz.es; owner 2026-10-07)
    place = {_domain_of(r["url"]) for r in out if r.get("official") and r.get("place")}
    for r in out:
        # every domain-name match, not just the first three: a country ending starts a point
        # lower, and monopoly.ru (996.7) kept "monopoly" from the article (2026-09-26)
        if (r.get("source") == "site" and r["score"] > WIKI_EXACT_SCORE + 50 and not r.get("official") and not r.get("stock")
                and (_domain_of(r["url"]) in place
                     or (not r.get("popular") and _domain_of(r["url"]) not in official))):
            r["score"] = round(WIKI_EXACT_SCORE - 5 - (NAV_SCORE - r["score"]), 2)

def named_place_leads(q: str, pl: dict | None, r: dict) -> bool:
    """A one-off place nearby asked by its own name leads a site that is merely named like it (2026-10-08: of
    1,300 real places near home asked by name the place was first for 60%; "vics" led with vics.or.jp over Vic's 21 mi
    away, "ikko" with ikko.com, and "gold cafeteria" with gold.org, lifted as its "brand"). Not a blanket rule: a
    chain keeps its brand's site ("rita's", "mavis discount tire"), and so do a popular site, an article's official
    site and the place's own website. Nearby is the radius the name list reads; a place found by its town
    ("wawa twin falls") or its brand tag is not named by the whole query and changes nothing. With exact_name_first():
    place first for 59.6% -> 75.5% of 661 (dev half) and 60.4% -> 76.5% of 639 (test half); no row of head-brand,
    site-nav, wiki-redirect or place-town changed."""
    if not pl or pl.get("chain") or r.get("source") != "site" or r.get("popular") or r.get("official"):
        return False
    if norm_key(pl.get("name") or "") not in place_keys(q) or (pl.get("miles") or 0) > place_radius_km() * 0.621371:
        return False
    return not pl.get("website") or _domain_of(pl["website"]) != _domain_of(r["url"])

def name_sites_under_place(q: str, pl: dict | None, out: list[dict]) -> None:
    """The name-match sites named_place_leads() says yield go under the place tier, in their order, the way
    demote_weak_names() puts them under an article. A typed domain (NAV_SCORE + 1) is asked for and stays."""
    for r in out:
        if PLACE_NAME_SCORE < r["score"] <= NAV_SCORE and named_place_leads(q, pl, r):
            r["score"] = round(PLACE_NAME_SCORE - 5 - (NAV_SCORE - r["score"]), 2)

def exact_name_first(rows: list[tuple], q: str) -> list[tuple]:
    """PLACES_SQL rows (nearest first) with the places named exactly q ahead of the loose matches: "don's pizza" led
    with Donato's Pizza 5.6 mi away over Don's Pizza at 20, "federico's" with Federico's Pizza (2026-10-08:
    another place led 53 of the 1,300 named places). Each group keeps its nearest-first order."""
    keys = place_keys(q)
    return sorted(rows, key=lambda r: norm_key(r[1] or "") not in keys)

def brand_site_first(q: str, out: list[dict], pl: dict | None = None) -> None:
    """A query naming a place brand ("mavis discount tire") put the nearest OSM location
    first and the brand's own site at 2-4 (eval 2026-09-24: mavis.com 0.94 under a
    node at 0.49; kindercare.com 0.88 under 0.42; speedway.com 0.77 under 0.49). When a
    place leads and a site whose first label IS a query word (or the words run together)
    is on a generic ending and at most one path segment deep, it goes just above the
    places. costco.com.au for "costco gasoline" stays put (foreign ending)."""
    if not out:
        return
    top = max(out, key=lambda r: r["score"])
    if top.get("source") != "place":
        return
    # "7-eleven near me" asks for a location: the places stay first. (Its words also matched
    # near.org's first label, which went above every 7-Eleven — local yardstick, 2026-10-02.)
    if _NEAR.search(q.lower()):
        return
    # "xina <town>": the town's words name no brand (sites named after the town's words led 2026-10-07) — only the name's do
    # and asked with its town it is a location that is wanted: only the whole name is a brand ("wawa twin falls" ->
    # wawa.com), not one word of it ("ko sushi twin falls" -> sushi.com)
    words = _TOKEN.findall((top.get("town_name") or q).lower())
    # a brand leads its name ("mavis discount tire", "costco gasoline", "speedway"): the first word or the whole name run
    # together, never a later word ("old time photos" is not time.gov, "radio technology museum" not museum.tv)
    names = {"".join(words)} if top.get("town_name") else set(words[:1]) | {"".join(words)}
    # ... and the first word alone only with evidence that the site is the place's brand ("<town> mvc" -> the
    # site named after the town, "<name>'s taqueria" -> <name>.edu, "go fresh expess" -> go.dev; 31 of the 32 such lifts in the dev halves
    # of place and typo-place were misses): the leading place is called just that ("speedway gas" -> Speedway), a
    # listed place gives that site as its own website, or the place named exactly is a chain around here. The whole
    # name run together needs none.
    chain = bool(pl and pl.get("chain"))
    place_name = norm_key((top.get("title") or "").split(" — ")[0])
    place_sites = {_domain_of(r["site"]) for r in out if r.get("source") == "place" and r.get("site")}
    if pl and pl.get("website"):
        place_sites.add(_domain_of(pl["website"]))
    best = None
    for r in out:
        if r.get("source") != "site" or r["score"] >= top["score"]:
            continue
        # nor a site the link tier shows only under a lowered floor: its item is no known brand, and the
        # place named by the query stays first ("da vi nails" -> the salon 6.4 mi away, davinails.com under it)
        if r.get("link_low"):
            continue
        host = _domain_of(r["url"])
        path = urlsplit(r["url"]).path.strip("/")
        # the SITE is named after the brand, not a host under someone else's site: deli.yango.com is not a brand called
        # "deli" (rc5 acceptance, 2026-10-08: inner pages brought such hosts in and 21 named places lost first place to
        # arcade.hsn.com, dental.nova.edu, courts.delaware.gov ...)
        if host.count(".") != 1:
            continue
        # nor is the first word of a one-off place's own name: "gold cafeteria" 5.5 mi away is not gold.org, "perk cafe"
        # not perk.com (2026-10-08: 165 of the 520 named places that were not first lost to such a site)
        if named_place_leads(q, pl, r):
            continue
        if (host.split(".")[0] not in ("".join(words), place_name) and not chain
                and not any(h == host or h.endswith("." + host) for h in place_sites)):
            continue
        if (host.split(".")[0] in names and is_generic(host) and path.count("/") == 0 and len(path) <= 5
                and (best is None or r["score"] > best["score"])):
            best = r
    if best is not None:
        best["score"] = round(top["score"] + 0.5, 2)
        best["brand_site"] = True      # the learned ranker put the places back on top: it keeps this

def official_subdomain_yields(out: list[dict]) -> None:
    """An article's official site on a SUBDOMAIN of a site the query names outranked that
    site: plus.google.com (Google's infobox) over google.com for "google". It now
    sits just below the name match."""
    names = [r for r in out if r.get("source") == "site" and not r.get("official") and r["score"] >= NAV_SCORE - 3]
    for o in out:
        if not o.get("official"):
            continue
        oh = _domain_of(o["url"])
        for r in names:
            d = _domain_of(r["url"])
            if oh != d and oh.endswith("." + d):
                o["score"] = min(o["score"], round(r["score"] - 0.1, 2))

def name_sans_ending(url: str) -> str:
    """A site's name without its ending: ebay.co.uk and ebay.de are both "ebay",
    maps.google.de is "maps.google". The path stays, so an inner page only matches the
    same inner page."""
    host = _domain_of(url)
    m = _CC_SLD.search(host)
    return (host[:m.start()] if m else host.rpartition(".")[0]) + urlsplit(url).path.rstrip("/")

def is_country_ending(host: str) -> bool:
    """ebay.de, ebay.co.uk, google.com.ai: a country code or a country second level. The
    two-letter endings the nav tier counts as generic (.io, .co, .ai, .tv, .me) are not."""
    return not is_generic(host) and (bool(_CC_SLD.search(host)) or len(host.rpartition(".")[2]) == 2)

def other_endings_last(res: list[dict]) -> list[dict]:
    """One site, one row on page one (2026-10-08): 233 of 1,372 brand and product
    queries (17%) showed the same name on two or more endings in the first five ("ebay" ->
    ebay.com, ebay.co.uk, ebay.de; "google maps" -> maps.google.com, .co.uk, .de). Under a
    popular or official .com the country endings of the same name go to the end of the
    list, so they show only when the page would otherwise be short. Nothing else is
    folded, because the same name on another ending is as often another site: sending
    every later ending down lost 4 more of 1,000 head-brand answers from the first five
    ("ms" -> ms.now under ms.gov, "plaid" -> plaid.cymru, "obsidian" -> obsidian.md under
    obsidian.net, "aldi" -> aldi.com under the official aldi.de) and under an unpopular
    .com the country ending was the site asked for ("banca etica website" ->
    bancaetica.it). An article's official site never moves, and places are not looked at."""
    coms, kept, later = set(), [], []
    for r in res:
        host = "" if r.get("source") == "place" else _domain_of(r["url"])
        if host.endswith(".com") and (r.get("popular") or r.get("official")):
            coms.add(name_sans_ending(r["url"]))
        if host and is_country_ending(host) and not r.get("official") and name_sans_ending(r["url"]) in coms:
            later.append(r)
        else:
            kept.append(r)
    return kept + later

def is_untitled(r: dict) -> bool:
    """A site row with nothing to read: its host as its title and no description (a Tranco
    site we hold no homepage for, or a homepage that gave neither)."""
    host = _domain_of(r.get("url", ""))
    return (r.get("source") == "site" and not (r.get("content") or "").strip()
            and (r.get("title") or "").strip().lower() in ("", host, "www." + host, r["url"].lower()))

def untitled_last(q: str, res: list[dict]) -> list[dict]:
    """A name and nothing else is a weak answer (2026-10-08): 387 of 1,372 brand and
    product queries (28%) had an untitled row in the first five ("keep" -> keep.pt, "drive"
    -> drive.ai, "chrome" -> chrome.dev beside the article and the product's own site).
    When the page has a named answer with a title, the untitled name matches follow every
    other row of the page. Four stay where they are: a popular site (half of the top
    1,000 have no fetched homepage: google.com, facebook.com, apple.com, amazon.com), an
    article's official site, the domain that was typed ("aldi.de"), and the generic ending
    of an official site that is on a country ending ("aldi" -> aldi.com beside the official
    aldi.de, "shiv sena" -> shivsena.org beside shivsena.in: both left the first five
    without this). With no named answer on the page the untitled name match is the best
    guess and stays first: sending it down regardless lost 26 first places of 1,505 on
    site-nav ("jbpm website" -> jbpm.org under squarespace.com, "belfond" -> belfond.fr).
    An untitled row that is not a name match was ranked on other evidence and stays too:
    moving those took 4 of 372 product answers out of the first five ("motorrad" ->
    bmw-motorrad.com, "chem" -> lgchem.com). Places are not looked at."""
    if domain_query(q):
        return res
    abroad = {name_sans_ending(r["url"]) for r in res if r.get("official") and r.get("source") != "place"
              and is_country_ending(_domain_of(r["url"]))}
    weak = [r for r in res if is_untitled(r) and query_names_domain(q, _domain_of(r["url"]))
            and not r.get("popular") and not r.get("official")
            and not (is_generic(_domain_of(r["url"])) and name_sans_ending(r["url"]) in abroad)]
    # A front page that only got its name from name_front_pages() (`named`) is not the titled answer that
    # sends the others down: merged, "diego torres" lost diegotorres.com from 1st to 10th under the newly named
    # diegotorres.com.ar (combined run, 2026-10-09). It is not weak either: it has a name now.
    if not weak or not any(r["score"] >= WIKI_EXACT_SCORE - 3 and r.get("source") != "place" and not r.get("named")
                           and not any(r is w for w in weak) for r in res):
        return res
    return [r for r in res if not any(r is w for w in weak)] + weak

# Stock and ticker searches (2026-09-30, ADR-061): "netflix stock", "nflx share price",
# "$nflx". Answered with three links, never a price we fetch: the Google Finance quote (live for
# NYSE/Nasdaq trades and the fewest trackers of 17 quote pages measured), the company's official
# site, and its SEC filings. Reasoning and measurements: docs/internal/search-jev/12-stock-queries.md.
# stock_tickers / stock_keys come from importer/stocks.py (SEC ticker list + Wikidata).
STOCK_SCORE = NAV_SCORE + 2             # above the company's own homepage match

_STOCK_WORDS = ("stock price today", "share price today", "stock price", "share price", "stock quote", "stock news",
                "stock today", "stock forecast", "stocks", "stock", "shares", "share", "quote", "ticker", "price target",
                # earnings wording (answer eval 2026-10-04: "nvidia earnings date", "apple earnings report"): the
                # company's card — quote page, investor-relations site, SEC filings — is the answer we have
                "earnings date", "earnings report", "earnings call", "earnings release", "earnings results",
                "earnings today", "earnings")

_STOCK_SUFFIX = re.compile(r"\b(inc|incorporated|corp|corporation|co|company|companies|ltd|limited|plc|llc|lp|"
                           r"sa|nv|ag|se|class [a-c]|common stock|ordinary shares|the)\b")

# words that name a category of stocks, not one company, though each is also a ticker or a company
# key ("ai stocks" is not C3.ai, "gold stocks" not Barrick, "tech stocks" not Bio-Techne) — $AI still works
_STOCK_GENERIC = frozenset("ai,best,top,tech,gold,silver,oil,car,cars,crypto,lithium,live,weed,growth,dividend,"
                           "penny,meme,bank,banks,energy,solar,space,chip,chips,ev,value,new,hot,cheap,good,"
                           "blue chip,small cap,large cap,mid cap,defense,uranium,biotech,pharma,reit,cannabis,"
                           "chicken,beef,vegetable,bone,rolling".split(","))

_GOOGLE_EX = {"Nasdaq": "NASDAQ", "NYSE": "NYSE", "CBOE": "BATS", "OTC": "OTCMKTS"}

STOCK_SQL = """
SELECT t.ticker, t.exchange, coalesce(t.label, t.name), t.cik, t.site, to_jsonb(t) ->> 'ir'
FROM stock_keys k JOIN stock_tickers t USING (ticker)
WHERE k.key = %(k)s
ORDER BY t.ticker LIKE '%%-' || %(cls)s DESC, t.exchange = 'OTC', coalesce(t.sitelinks, 0) DESC, position('-' in t.ticker) > 0, length(t.ticker), t.ticker
LIMIT 1
"""

def stock_key(text: str) -> str:
    """Must match importer/stocks.py stock_key()."""
    t = text.lower().replace("&", " and ").replace(".com", " ").replace("'", "").replace("\u2019", "")
    t = re.sub(r"[^a-z0-9]+", " ", t)
    w = _STOCK_SUFFIX.sub(" ", t).split()
    while w and w[-1] == "and":
        w.pop()
    while w and w[0] == "and":
        w.pop(0)
    return " ".join(w)

def stock_query(q: str) -> str | None:
    """The company or ticker a stock search names ("netflix stock" -> "netflix", "$nflx" -> "nflx"),
    or None: the query must END in stock / shares / quote / ticker, or be a $TICKER. A bare ticker
    ("nflx") is not taken: too many are words or brands ("meta", "f", "t")."""
    t = " ".join(q.lower().split())
    m = re.fullmatch(r"\$([a-z]{1,5}(?:[.-][a-z])?)", t)
    if m:
        return m.group(1)
    t = re.sub(r"^(nasdaq|nyse)\s*:\s*", "", t)
    for w in _STOCK_WORDS:                 # suffix only: "quote of the day" is not Dayforce (DAY)
        m = re.fullmatch(rf"(.+?)\s+{w}", t)
        if m:
            k = stock_key(m.group(1))
            return k if k and k not in _STOCK_GENERIC else None
    return None

# A bare ticker ("nflx", "vti", 2026-10-01): answered only when the word IS a ticker and nothing
# else — not the company's own name ("ibm" is IBM's site) and not an English word ("cat", "low",
# "now", "spy": stock_tickers.word, marked at import from a dictionary). Wikipedia titles were
# the wrong test: "qqq" and "voo" have articles about the funds, and "low"'s exact match was
# "Speedrunning". "$spy" or "spy stock" still work. Funds (ETFs, mutual funds) come from SEC's
# fund list, which has no exchange: their quote link is Google Finance's search.
_BARE_TICKER = re.compile(r"[a-z]{2,5}")

STOCK_BARE_SQL = """
SELECT t.ticker, t.exchange, coalesce(t.label, t.name), t.cik, t.site, to_jsonb(t) ->> 'ir',
       coalesce((to_jsonb(t) ->> 'word')::boolean, false)
FROM stock_tickers t WHERE t.ticker = upper(%(k)s)
"""

def named_site(q: str, out: list[dict]) -> bool:
    """A popular site whose name is exactly the word ("cvs" -> cvs.com, CVS Health's ticker
    too): that site is what the word means, not the stock."""
    t = " ".join(q.lower().split())
    return any(r.get("source") == "site" and r.get("popular") and not r.get("stock")
               and (urlsplit(r["url"]).hostname or "").removeprefix("www.").split(".")[0] == t for r in out)

def bare_ticker_ok(q: str, row) -> bool:
    t = " ".join(q.lower().split())
    if not row or not _BARE_TICKER.fullmatch(t):
        return False
    if len(row) > 6 and row[6]:
        return False                                   # an English word: "cat", "low", "spy"
    name = "" if row[1] == "FUND" else (row[2] or "")       # a fund's "name" is its ticker
    return not (name and (stock_key(name) == t or stock_key(name).replace(" ", "") == t))   # "ibm"

def stock_results(row) -> list[dict]:
    ticker, exchange, name, cik, site, ir = (tuple(row) + (None,))[:6]   # ir: absent in older tables
    if exchange == "FUND":
        out = [_site(f"https://www.google.com/finance?q={ticker}", f"{ticker} fund quote — Google Finance",
                     f"{ticker}: price, chart and holdings.", STOCK_SCORE)]
    else:
        g = f"{ticker.replace('-', '.')}:{_GOOGLE_EX.get(exchange, exchange.upper())}"
        out = [_site(f"https://www.google.com/finance/quote/{g}", f"{name} ({ticker}) stock quote — Google Finance",
                     f"{exchange}: {ticker}. Live price for NYSE/Nasdaq trades, chart and news.", STOCK_SCORE)]
    if ir:                                  # found in Common Crawl (importer/irlinks.py)
        out.append(_site(ir, f"{name} — investor relations", "Stock information, earnings, reports and events.",
                         STOCK_SCORE - 0.1))
    elif site:
        out.append(_site(site, f"{name} — official website", "The company's own site; investor relations is linked from it.",
                         STOCK_SCORE - 0.1))
    sec = f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik:010d}&type=&dateb=&owner=include&count=40"
    if exchange == "FUND":
        out.append(_site(sec, f"{ticker} fund filings — SEC EDGAR", "Prospectus, annual and semi-annual reports, holdings.",
                         STOCK_SCORE - 0.2))
    else:
        out.append(_site(sec, f"{name} filings (10-K, 10-Q, 8-K) — SEC EDGAR",
                         "Annual and quarterly reports, earnings releases (8-K).", STOCK_SCORE - 0.2))
    for r in out:
        r["stock"] = True
    return out

# The article the query names exactly (its title or a redirect) and that the hand rules put first. Measured 2026-10-07
# on the release engine, wiki-redirect dev half: with the Jev-free ranker.json that ships, the learned ranker moved a
# reference link (899.5) above that article for 99 of 1,201 queries (86.2% hit@1); the Jev-fitted weights the rc4
# acceptance image carried did not (94.4%). SEARCH_PIN_EXACT_ARTICLE=0 lets the ranker move it again.
PIN_EXACT_ARTICLE = os.environ.get("SEARCH_PIN_EXACT_ARTICLE", "1").strip() != "0"

def official_top(r: dict) -> bool:
    """The official site of the article the query names, put first by the hand rules (official_score's tier 1). The
    learned ranker moved london.edu over london.gov.uk and france.tv over service-public.fr (2026-10-07): the site
    the article itself names is better evidence than the ranker's features."""
    # Not for an organisation, and only for a front page on the open web ("wix" -> firegiant.com/wixtoolset/ and an
    # .onion address were pinned by a looser rule): there the ranker is right more often than the stored link.
    # Pinned for every kind, head-brand lost 14 of 504 dev queries to wrong official links the ranker used to pass
    # over ("lg" -> lgcorp.com, "fulham f.c." -> a premierleague.com news page, "craigslist" -> a statista page);
    # for a company the name-match site is the real one, for a place it is an impostor (london.edu).
    u = urlsplit(r.get("url") or "")
    return (bool(r.get("official")) and r.get("source") == "site" and r.get("score", 0) >= NAV_SCORE
            and r.get("of_kind") != "org" and u.path.strip("/") == "" and not (u.hostname or "").endswith(".onion"))

def exact_article_top(r: dict) -> bool:
    return (PIN_EXACT_ARTICLE and r.get("source") == "wikipedia" and not r.get("namesake")
            and r.get("score", 0) >= WIKI_EXACT_SCORE - 3)

def keep_top(q: str, r: dict) -> bool:
    """The hand-ranked first result the learned ranker may not move: a named channel /
    subreddit / account / brand site, a popular site named exactly by the query, or a place
    (2026-10-02: the ranker never saw a nearby place in training and put "Dentist — Wikipedia"
    over the clinic 1.3 mi away; local hit@1 0.733 -> back to the hand order's 0.850)."""
    return (names_platform_page(r) or exact_name_top(q, r) or bool(r.get("famous")) or bool(r.get("kind_top"))
            or bool(r.get("stock")) or r.get("source") == "place" or exact_article_top(r) or official_top(r))

def root_beats_inner_pages(out: list[dict]) -> None:
    """When an article's official site is present, other tier-1 results on the same
    domain go: facebook.com/notesandclips/ and microsoft.com/ja-jp were the captured
    "homepages" and outranked the official roots (2026-09-24)."""
    officials = {_domain_of(r["url"]): r for r in out if r.get("official")}
    keep = []
    for r in out:
        o = officials.get(_domain_of(r["url"]))
        if o is not None and not r.get("official") and r.get("source") == "site" and r["score"] >= NAV_SCORE - 3:
            if official_deep(o["url"]) and not official_deep(r["url"]):
                # the "official" link is a page (github.com/blog/589-…, picked out of an infobox) and the name match
                # is the site's front page: the front page is shown, with the official result's place (rc5 plan W1)
                o["url"] = r["url"]
            # The official root takes the inner page's place AND its rank: meshtastic.org
            # (a tier-1 name match) must not fall under the article just because the
            # article is not typed as an organisation.
            o["score"] = max(o["score"], r["score"])
            o["popular"] = o.get("popular") or r.get("popular")
            continue
        keep.append(r)
    out[:] = keep

FEATURES = ["bias", "src_site", "src_wiki", "src_place", "src_subreddit", "tier_nav", "tier_wiki_exact",
            "tier_place", "tier_fused", "fused_score", "position", "popular", "official", "generic_tld",
            "root_path", "path_depth", "label_eq_query", "label_in_query", "title_overlap", "title_starts",
            "query_words", "is_wikipedia_url",
            # 2026-09-24: kinds added after the first model — YouTube channels,
            # Wikidata accounts, described-at pages. A new name makes an old ranker.json
            # stale (ignored) until it is retrained with them.
            "src_youtube", "src_account", "src_described"]

def features(q: str, r: dict, pos: int) -> list[float]:
    import math as _m
    u = urlsplit(r.get("url", ""))
    host = (u.hostname or "").lower()
    host = host[4:] if host.startswith("www.") else host
    label = host.split(".")[0] if host else ""
    qk = norm_key(q)
    qt = set(_TOKEN.findall(q.lower()))
    tt = set(_TOKEN.findall((r.get("title") or "").lower()))
    sc = float(r.get("score", 0))
    src = r.get("source", "")
    path = u.path.rstrip("/")
    return [1.0,
            float(src == "site"), float(src == "wikipedia"), float(src == "place"), float(src == "subreddit"),
            float(sc >= NAV_SCORE - 3), float(WIKI_EXACT_SCORE - 3 <= sc < WIKI_EXACT_SCORE + 1),
            float(PLACE_NAME_SCORE - 5 <= sc < PLACE_CATEGORY_SCORE + 1), float(sc < 200),
            _m.log1p(sc) if sc < 200 else 0.0, _m.log1p(pos),
            float(bool(r.get("popular"))), float(bool(r.get("official"))), float(bool(_GENERIC.search(host))),
            float(path in ("", "/")), float(path.count("/")),
            float(bool(qk) and label == qk), float(bool(label) and len(label) > 2 and label in qk),
            len(qt & tt) / max(1, len(qt)), float((r.get("title") or "").lower().startswith(q.lower().strip())),
            float(len(qt)), float("wikipedia.org" in host),
            float(src == "youtube"), float(src == "account"), float(src == "described")]

# The rules added for government and local queries, as features the model can weigh instead of hand
# order. A model trained with them lists FEATURES + EXTRA_FEATURES; one trained without them (the shipped
# ranker.json) still matches FEATURES alone and is read as before, so adding a name here never turns ranking off.
EXTRA_FEATURES = ["gov_site", "gov_query", "gov_same_state", "gov_other_state", "local_site", "path_service",
                  "path_news", "src_pages_sem"]

_GOV_PATH_SERVICE = re.compile(r"(^|[/_.-])(apply|renew\w*|replace\w*|licen[cs]\w*|permits?|regist\w*|forms?|faqs?|"
                               r"how-?to|eligib\w*|requirements?|fees?|appointments?|benefits|certificates?|records|pay\w*|"
                               r"claims?|reservations?|file|filing)([/_.-]|$)")

_GOV_PATH_NEWS = re.compile(r"(^|/)(news|newsroom|press|press-?releases?|media|events?|calendar|meetings?|minutes|agendas?|"
                            r"archives?|blog|\d{4}(/\d{1,2}){0,2})(/|$)")

_feat_ctx: dict = {}

def _query_ctx(q: str) -> tuple:
    """(state the query names or the searcher's, local sites) once per query and searcher's point, for extra_features."""
    key = (q, home_point(), _drop.get())
    c = _feat_ctx.get(key)
    if c is None:
        gs = gov_query_state(q)
        gq = gs or home_gov_state(q)
        hl = None if gs else home_local(q)
        c = (gq[0] if gq else None, frozenset(hl[0]) if hl else frozenset())
        if len(_feat_ctx) > 2048:
            _feat_ctx.clear()
        _feat_ctx[key] = c
    return c

def extra_features(q: str, r: dict) -> list[float]:
    host = (urlsplit(r.get("url", "")).hostname or "").lower()
    path = urlsplit(r.get("url", "")).path.lower()
    doms = gov_states()
    st = gov_state.host_state(host, doms) if doms else None
    qst, local = _query_ctx(q)
    return [float(st is not None), float(qst is not None), float(bool(st) and st == qst),
            float(bool(st) and bool(qst) and st != qst),
            float(bool(local) and gov_state.host_domain(host, doms) in local),
            float(bool(_GOV_PATH_SERVICE.search(path))), float(bool(_GOV_PATH_NEWS.search(path))),
            float(bool(r.get("sem_page")))]

def model_features(m: dict, q: str, r: dict, pos: int) -> list[float]:
    """The features a model was trained with: FEATURES, or FEATURES + EXTRA_FEATURES."""
    f = features(q, r, pos)
    return f + extra_features(q, r) if len(m["features"]) > len(FEATURES) else f

_RANKER = None

def _ranker():
    global _RANKER
    if _RANKER is None:
        try:
            with open(RANKER_PATH) as f:
                m = json.load(f)
            _RANKER = m if m.get("features") in (FEATURES, FEATURES + EXTRA_FEATURES) else False   # stale model: ignore it
        except (OSError, ValueError):
            _RANKER = False
    return _RANKER

def names_platform_page(r: dict) -> bool:
    """A query that named a YouTube channel or subreddit ("@mrbeast", "r/homelab") keeps
    that answer first: the model has no feature for those sources and put mrbeast.store
    over the channel the query asked for (2026-09-24)."""
    return ((r.get("source") in ("youtube", "subreddit", "account") and r["score"] >= NAV_SCORE)
            or bool(r.get("brand_site")))

def promoted(hand_top: dict, top: dict) -> bool:
    """The learned ranker put a different page first. rerank returns copies (for the why
    tooltip), so pages are compared, not objects — comparing objects marked every reranked
    query as promoted, and none could be confident (2026-09-26)."""
    return same_page(top["url"]) != same_page(hand_top["url"])

def rerank(q: str, results: list[dict]) -> list[dict]:
    """Reorder by the learned model when ranker.json exists and matches FEATURES."""
    m = _ranker()
    if not m or not results:
        return results
    import math as _m
    w = m["weights"]
    scored = []
    for pos, r in enumerate(results):
        z = sum(a * b for a, b in zip(w, model_features(m, q, r, pos)))
        p = 1 / (1 + _m.exp(-z))
        # the model's belief and the hand order it started from ride along for the page's
        # "why this result" tooltip (2026-09-26); a copy, so no cached dict carries them
        scored.append((p, -pos, dict(r, lr=round(p, 3), pos0=pos + 1)))
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return [r for _, _, r in scored]

# The Jev-free models score a Wikipedia article at their floor (-7.9) unless the query is close to its title or
# worded as a question: their constructed training rows hold an article as the answer only for its own title
# (yardstick "wiki-topic"). At the floor the score says nothing — "illinois foid card": Firearm Owner's
# Identification -7.86, Firearms regulation in France -7.97 — and the article fell under whatever site scored
# higher ("NJ firearms id card": Gun laws in New Jersey from 1st to 6th, Marlin Firearms first; 2026-10-11).
# Such a row keeps the place the ranker gave it; the others are reordered around it. 30 informational queries
# on public: 43 of 99 scored articles at the floor, first result changed for 7 (6 better, 1 no better).
# -99 switches it off; a retrained model that scores articles on any wording makes it unneeded.
CE_WIKI_FLOOR = float(os.environ.get("SEARCH_CE_WIKI_FLOOR", "-7"))

CE_ALT_DIR = os.environ.get("SEARCH_CE_ALT_DIR", "")

_ce_alt = None

_ce_alt_lock = threading.Lock()

def ce_alt():
    """?drop=cealt (eval only): the reranker in SEARCH_CE_ALT_DIR, on the CPU. Fails loudly —
    a silent fall-back to the default model would measure nothing."""
    global _ce_alt
    if _ce_alt is None:
        with _ce_alt_lock:
            if _ce_alt is None:
                if not CE_ALT_DIR:
                    raise RuntimeError("drop=cealt needs SEARCH_CE_ALT_DIR")
                _ce_alt = _CrossEncoder(CE_ALT_DIR)
                print(f"cross-encoder (alt): {CE_ALT_DIR} on cpu", flush=True)
    return _ce_alt

def ce_rerank(q: str, results: list[dict], scorer=None) -> list[dict]:
    """Exact-tier results first in their hand order, then the first CE_TOP of the rest
    reordered by the cross-encoder, then the remainder as they were. A Wikipedia article
    scored at the model's floor stays where it was (CE_WIKI_FLOOR)."""
    scorer = scorer or cross_encoder()
    if not scorer:
        return results
    top = [r for r in results if r["score"] >= CE_EXACT]
    rest = [r for r in results if r["score"] < CE_EXACT]
    cand, tail = rest[:CE_TOP], rest[CE_TOP:]
    if len(cand) < 2:
        return results
    sc = scorer(q, [ce_text(r) for r in cand])
    # A query naming a state and a service ("alaska child support payment") is answered by that state's page,
    # not by whatever article shares its words: the floor's hold is for articles that ARE the answer and the
    # model does not know ("python programming language"). Measured on the 2,000 government queries of rc10
    # (tools/gov-eval, 2026-10-26): in 80 of the 142 queries that lost first place, a floor-held article sat
    # first ("Child Support Agency (Australia)", "REAL ID Act", "Same-sex marriage in Alabama") with the state's
    # page right under it; no held article outranked a page for any other reason..
    hold_wiki = "cewiki" not in _drop.get() and not state_service_query(q)
    held = {i for i, r in enumerate(cand) if r.get("source") == "wikipedia" and sc[i] <= CE_WIKI_FLOOR} if hold_wiki else set()
    free = iter(sorted((i for i in range(len(cand)) if i not in held), key=lambda i: -sc[i]))
    order = [i if i in held else next(free) for i in range(len(cand))]
    return top + [dict(cand[i], ce=round(float(sc[i]), 2)) for i in order] + tail

# ---------------------------------------------------------------------------
# Typo correction (2026-09-24). A misspelt name matched nothing exact and fell to
# weak keyword / semantic hits ("decathhlon" -> Red herring; typos scored 0.39 on the
# Jev eval vs 0.80 overall). When no exact tier answered, the closest suggest key by
# trigram similarity ("decathlon") is searched instead and its results are marked
# corrected — never `confident`: a guessed spelling must not skip the web.
# ---------------------------------------------------------------------------
TYPO_MIN = int(os.environ.get("SEARCH_TYPO_MIN", "5"))        # swept by tools/sq-sweep.py, with the three below

# Tuned 2026-09-24 on 300 synthetic typos of popular names with known originals
# (right / wrong / untouched):
#   trigram 0.5, spelling only                     234 / 29 / 37
#   trigram 0.4 + popularity 0.5                   253 / 34 / 13  ("costoc" -> Costco, not Costock)
#   trigram 0.25 + popularity 0.5 + <= 1-2 edits   280 / 18 /  2
#   … + 100 candidates, popularity 1.0              280 / 22 /  1, and the ones people
#     type right: the 30 closest spellings never held "amazon" for "amzaon", and at
#     0.5 "costoc" stayed the village Costock.
# The edit limit is what separates a typo from a different phrase that merely looks
# similar: "bike across america" was "corrected" to Race Across America (3 edits),
# "cheap flights to denver" to Cheapflights (8). 1 edit under 9 letters, 2 from 9.
TYPO_SIM = float(os.environ.get("SEARCH_TYPO_SIM", "0.25"))

TYPO_POP = float(os.environ.get("SEARCH_TYPO_POP", "1.0"))

TYPO_SQL = """
SELECT i.name, similarity(k.key, %(t)s) AS sim, i.score, k.key
FROM suggest_keys k JOIN suggest_items i USING (id)
WHERE k.key %% %(t)s AND k.key <> %(t)s
ORDER BY similarity(k.key, %(t)s) DESC LIMIT 100
"""

def edits(a: str, b: str) -> int:
    """Damerau-Levenshtein distance (optimal string alignment): a swap of two
    neighbouring letters is one edit, as a typist makes it."""
    d = [[0] * (len(b) + 1) for _ in range(len(a) + 1)]
    for i in range(len(a) + 1):
        d[i][0] = i
    for j in range(len(b) + 1):
        d[0][j] = j
    for i in range(1, len(a) + 1):
        for j in range(1, len(b) + 1):
            d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + (a[i - 1] != b[j - 1]))
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                d[i][j] = min(d[i][j], d[i - 2][j - 2] + 1)
    return d[len(a)][len(b)]

# the edit limit: 1 edit under this many letters, 2 from it (the tuning note above TYPO_SIM)
TYPO_EDITS2_LEN = int(os.environ.get("SEARCH_TYPO_EDITS2_LEN", "9"))

def max_edits(t: str) -> int:
    return 1 if len(t) < TYPO_EDITS2_LEN else 2

def pick_correction(cands: list[tuple], t: str = "", sim_min: float = None, pop_w: float = None) -> str | None:
    """cands: (name, similarity, popularity score, key). The best spelling match weighted
    by how well known the name is, among keys at most max_edits(t) edits from the typed
    text t; None when nothing qualifies."""
    sim_min = TYPO_SIM if sim_min is None else sim_min
    pop_w = TYPO_POP if pop_w is None else pop_w
    ok = [c for c in cands if c[1] >= sim_min and (not t or len(c) < 4 or edits(t, c[3]) <= max_edits(t))]
    if not ok:
        return None
    return max(ok, key=lambda c: c[1] + pop_w * (c[2] or 0.0))[0]

def needs_correction(q: str, results: list[dict]) -> bool:
    """Only when nothing matched exactly (no tier 1/2 result) and the query is long enough
    for a spelling to be wrong in a way trigrams can see."""
    # A lifted kind is not an exact match: "miccrosoft whiteboard" lifted the kind
    # "whiteboard" (miro.com) and so was never corrected to Microsoft Whiteboard (2026-09-30).
    # Nor is a channel that only sits under an article (bare_channel_score): "röyksopp" matched its channel's name,
    # so the spelling without the accent was never tried and royksopp.com and the article were lost (2026-10-11).
    return len(tight(q)) >= TYPO_MIN and not any(
        r["score"] >= WIKI_EXACT_SCORE - 3 and not r.get("kind")
        and not (r.get("source") == "youtube" and r["score"] < WIKI_EXACT_SCORE) for r in results)

def correct_query(q: str) -> str | None:
    t = tight(q)
    with _conn() as c:
        if "suggest_keys" not in tables(c):
            return None
        c.execute(f"SET pg_trgm.similarity_threshold = {TYPO_SIM}")
        best = pick_correction(list(c.execute(TYPO_SQL, {"t": t})), t)
    if not best:
        return None
    name = re.sub(r"\s*\(.*\)$", "", best).strip()
    return name if tight(name) != t else None

# Second attempt (2026-10-07). The index above holds the suggestion keys only: 3.2 M of the 16 M
# article aliases and none of the long-tail site names, so a slip in "air hostess" or in a small
# company's name found nothing to be corrected to. Every spelling ONE slip away (a letter dropped,
# added, replaced, or swapped with its neighbour: about 75 per letter) is looked up in the names the
# exact tiers answer, and the best known one is searched. Two rules keep it off queries spelt right:
#   - only a word Wikipedia hardly uses is respelt. Without it "driving directions" became the
#     article "driving direction" and "jeremy howard" another man's site. Of 494 misspelt words in the
#     typo sets 90% are in fewer than 20 articles; of 1,683 words in the other sets 11% are.
#   - a query with accents is only folded ("salvador dalí" -> "salvador dali"): name keys drop every
#     non-ASCII letter, so a slip next to one lands on unrelated names.
# Release lookups and the release's Wikipedia text only. SEARCH_TYPO_WIDE=0 turns it off.
TYPO_WIDE = os.environ.get("SEARCH_TYPO_WIDE", "1") != "0"

# rc9 default 3000 (was 20 stage two). SEARCH_TYPO_WORD_DF=20 restores rc8.
TYPO_WORD_DF = int(os.environ.get("SEARCH_TYPO_WORD_DF", "3000"))

# a typed word is a word in its own right, not a slip (respells_known_words). Fitted 2026-10-11 on the dev halves
# of typo-site, typo-wiki and typo-place (rc8, 383 word-for-word corrections: none refused); test halves: 1 of 419
# refused, a correction that was wrong anyway ("claes cove").
TYPO_KNOWN_DF = int(os.environ.get("SEARCH_TYPO_KNOWN_DF", "1000"))

TYPO_KNOWN_RATIO = float(os.environ.get("SEARCH_TYPO_KNOWN_RATIO", "0.5"))

TYPO_NEAR_SQL = "-- release only: which of %(keys)s are names (lookups_release.near_names)"

# (2026-10-13): two classes of name the exact tiers answer are in neither correction vocabulary, so a slip in
# one found nothing to be corrected to — 60 of the 110 misspelt dev queries the recall audit filed as never undone
# (tools/evalsets/open/arm-487-typo.md): official sites of things with no English article ("chebahciy", the link
# tier, LINK_SITE_SQL) and places nearby ("ocena club", the place tier). SEARCH_TYPO_NAMES names the classes
# correct_wide() may also ask: "links", "places" or both. rc9 default: both; SEARCH_TYPO_NAMES=0 (or empty) is neither. They are asked only when the first
# vocabulary held nothing one slip away, so a query it corrects today is corrected the same.
TYPO_NAME_CLASSES = ("links", "places")

def typo_names_setting(v: str) -> frozenset:
    """"links, places" -> the classes named in it; anything else ("", "0", "1") is none."""
    return frozenset(x for x in (y.strip().lower() for y in (v or "").split(",")) if x in TYPO_NAME_CLASSES)

TYPO_NAMES = typo_names_setting(os.environ.get("SEARCH_TYPO_NAMES", "links,places"))

# (2026-10-30): the words of English (typo-words.txt, Open English WordNet) as evidence on the typo path.
# SEARCH_TYPO_WORDS names the uses, default fill as measured 2026-10-30 (guard moved no typo set); the file absent is none.
#   guard  a dictionary word is no slip: correct_wide() leaves it alone, and respells_known_words() counts it as a word
#          in its own right however rarely Wikipedia uses it (the ratio still holds: far rarer than the other word is a slip)
#   fill   a last attempt, word by word: a rare word that is no dictionary word becomes the best-known dictionary word
#          one slip away ("go fresh expess", "busiiness banking"). Two words at least, one of them left as typed:
#          alone, "ozera" would become the article "opera". The floor in _search_corrected() still decides.
TYPO_WORD_USES = ("guard", "fill")

def typo_words_setting(v: str) -> frozenset:
    """"guard, fill" -> the uses named in it; anything else ("", "0", "1") is none."""
    return frozenset(x for x in (y.strip().lower() for y in (v or "").split(",")) if x in TYPO_WORD_USES)

TYPO_WORDS = typo_words_setting(os.environ.get("SEARCH_TYPO_WORDS", "fill"))

_typo_words = None

def typo_words() -> frozenset:
    global _typo_words
    if _typo_words is None:
        try:
            with open(os.environ.get("SEARCH_TYPO_WORDS_FILE") or os.path.join(RELEASE_DIR, "typo-words.txt"), encoding="utf-8") as f:
                _typo_words = frozenset(f.read().split())
        except OSError:
            _typo_words = frozenset()
        print(f"typo path dictionary words: {len(_typo_words)}", flush=True)
    return _typo_words

def dictionary_word(w: str) -> bool:
    return "guard" in TYPO_WORDS and w in typo_words()

TYPO_NEAR_MORE_SQL = "-- release only: which of %(keys)s are link-only sites or places nearby (lookups_release.near_more)"

_EDIT_LETTERS = "abcdefghijklmnopqrstuvwxyz0123456789"

def one_edit(w: str) -> set[str]:
    """Every spelling one slip from the word w."""
    out = set()
    for i in range(len(w) + 1):
        out.update(w[:i] + ch + w[i:] for ch in _EDIT_LETTERS)
        if i < len(w) and w[i].isalnum():
            out.add(w[:i] + w[i + 1:])
            out.update(w[:i] + ch + w[i + 1:] for ch in _EDIT_LETTERS)
            if i + 1 < len(w) and w[i + 1].isalnum():
                out.add(w[:i] + w[i + 1] + w[i] + w[i + 2:])
    out.discard(w)
    return out

def unaccent(s: str) -> str:
    return "".join(ch for ch in unicodedata.normalize("NFKD", s) if not unicodedata.combining(ch))

def respells_known_words(q: str, fixed: str) -> bool:
    """The correction only changes words that are words in their own right: "nba news" -> "NBC News", "nba scores" ->
    "NBA Store", "nhl scores" -> "Hi Scores" (2026-10-10: the first index compares whole strings, so real words
    one letter from a better-known name were "corrected"). A word is one in its own right when Wikipedia uses it
    plenty (TYPO_KNOWN_DF articles) and at least TYPO_KNOWN_RATIO as often as the word it would become: "nba" beside
    "nbc". A slip is far rarer than what it stands for ("marne" 6,644 articles, "marine" 133,806; "univerity" 31),
    and refusing every word in 20 articles cost typo-site 2.2 points of hit@1 on rc8. Two spellings the index holds
    as one term ("languags", "language") are no respelling. Only when the two line up word for word: a join or a
    split ("face book" -> "facebook") is judged as before."""
    if not (LOOKUP_BACKEND == "release" and WIKI_BACKEND == "tantivy-release"):       # doc_freq is the release index's
        return False
    a, b = _TOKEN.findall(q.lower()), _TOKEN.findall(fixed.lower())
    changed = [(x, y) for x, y in zip(a, b) if x != y]
    if len(a) != len(b) or not changed:
        return False
    wt = wiki_text()
    for x, y in changed:
        df = wt.doc_freq(x)
        if (wt.terms(x) == wt.terms(y) or (df < TYPO_KNOWN_DF and not dictionary_word(x))
                or df < TYPO_KNOWN_RATIO * wt.doc_freq(y)):
            return False
    return True

def more_names(c, q: str, keys: list[str]) -> list[tuple]:
    """(key, score) for the keys that name a link-only official site or a place nearby: the classes
    SEARCH_TYPO_NAMES allows. Places only where the place tier would read the respelt name: a home point, and a
    name of at most four words (place_query). The typed name's key goes along: a name of either class is no slip."""
    here = home_point() if "places" in TYPO_NAMES and len(q.split()) <= 4 else None
    p = {"keys": keys, "typed": norm_key(q), "links": "links" in TYPO_NAMES, "places": here is not None, "r": place_radius_km()}
    if here is not None:
        p["lat"], p["lon"] = here
    with stage("typo_names"):
        return list(c.execute(TYPO_NEAR_MORE_SQL, p))

def correct_wide(q: str) -> str | None:
    """The best-known name one slip from q, spelt with q's own spaces; None when there is none."""
    if not TYPO_WIDE or LOOKUP_BACKEND != "release" or WIKI_BACKEND != "tantivy-release":
        return None
    words = q.lower().split()
    plain = unaccent(" ".join(words))
    if plain != " ".join(words):
        near = {norm_key(plain): plain}
    else:
        wt, near = wiki_text(), {}
        for i, w in enumerate(words):
            if wt.doc_freq(w) < TYPO_WORD_DF and not dictionary_word(w):
                for v in one_edit(w):
                    fixed = " ".join(words[:i] + [v] + words[i + 1:])
                    near.setdefault(norm_key(fixed), " ".join(fixed.split()))
    near.pop(norm_key(q), None)
    if not near:
        return None
    with _conn() as c:
        rows = list(c.execute(TYPO_NEAR_SQL, {"keys": list(near)}))
        if not rows and TYPO_NAMES:
            rows = more_names(c, q, list(near))
    return near[max(rows, key=lambda r: r[1])[0]] if rows else None

def correct_words(q: str) -> str | None:
    """Word by word: each rare word that is no dictionary word becomes the best-known dictionary word one slip away
    ("fill" in SEARCH_TYPO_WORDS). None when nothing changes or no word would be left as typed."""
    if "fill" not in TYPO_WORDS or LOOKUP_BACKEND != "release" or WIKI_BACKEND != "tantivy-release" or not typo_words():
        return None
    words, known, wt = q.lower().split(), typo_words(), wiki_text()
    out = list(words)
    for i, w in enumerate(words):
        if w.isalpha() and w not in known and wt.doc_freq(w) < TYPO_WORD_DF:
            near = one_edit(w) & known
            if near:
                out[i] = max(sorted(near), key=wt.doc_freq)
    changed = sum(a != b for a, b in zip(words, out))
    return " ".join(out) if changed and changed < len(words) else None

# A misspelt name runs to six words ("byrd polar and climate ersearch center", Jev typo
# set); a longer sentence ("how to fix a squeaky door hinge") is never within two edits of
# one, and its trigram scan was the slowest step (67 ms cold). At 4 words "national
# fund for heealth insurance" lost its correction.
TYPO_MAX_WORDS = 6

def typo_shape(q: str) -> bool:
    return len(tight(q)) >= TYPO_MIN and len(q.split()) <= TYPO_MAX_WORDS

def base_part(q: str, k: int, semantic: bool, learned: bool, correct: bool, ce: bool = True) -> list[dict]:
    """The base stage (parts stage "base" phase 2): the ranked candidates of one search, before the profile cap,
    the junk drop and the hands-off check that search() applies after the cache. Through the parts store when
    SEARCH_PARTS has "base"; its key holds the retrieval and ranking code, every setting and data file it reads, and the
    device each model really runs on."""
    if parts_for("base") is None:
        if _pcheck.get():
            raise PartStale("base")
        return _search_retry(q, k, semantic, learned, correct, ce)
    return part_run("base", [q, k, semantic, learned, correct, ce], lambda: _search_retry(q, k, semantic, learned, correct, ce))

def _search_retry(q: str, k: int, semantic: bool, learned: bool, correct: bool, ce: bool = True) -> list[dict]:
    try:
        return _search_corrected(q, k, semantic, learned, correct, ce)
    except psycopg.OperationalError:
        _drop_pool()                          # a serve swap terminated the pooled connections
        return _search_corrected(q, k, semantic, learned, correct, ce)

# A news outlet asked with news words leads with its own site (owner 2026-10-09: "cnn latest news", "cnn news
# today" and "cnn world news" led with a Wikipedia article about a CNN programme, cnn.com second; "fox news today"
# led with foxsports.com/mlb, "nytimes latest news" with babylonbee.com). The words say what is wanted FROM the
# outlet, not another site: with them taken off the end or the start, what is left names the outlet. It leads only
# when (a) what is left answers on its own with a named site (the exact tiers, NAV_SCORE), and (b) that site is a
# news outlet by the kinds data, or what is left still says "news" ("abc news today", "sky news live": no kinds row
# for abcnews.go.com or news.sky.com). So "tesla latest news" and "bitcoin news today" are untouched: a search for
# news ABOUT a company is not a search for its site. Tried longest name first, so "fox news today" is Fox News
# before it is fox.com, and "usa today latest news" is USA Today.
_OUTLET_TAIL = frozenset("news latest today live breaking headlines headline world update updates now top stories "
                         "story tonight daily newest recent current".split())

OUTLET_SCORE = NAV_SCORE - 2          # a named site: the article about a programme (900) goes under it

OUTLET_TRIES = 4

def news_only(q: str) -> bool:
    """Nothing but news words, "news" or "headlines" among them: "latest news", "breaking news today"."""
    toks = _TOKEN.findall(q.lower())
    return bool(toks) and all(t in _OUTLET_TAIL for t in toks) and any(t in ("news", "headlines") for t in toks)

def outlet_names(q: str) -> list[str]:
    """What the query could be naming once news words are taken off its end, then its start, longest first:
    "fox news today" -> ["fox news", "fox"]; "cnn latest news" -> ["cnn latest", "cnn"]; "latest bbc news" ->
    ["latest bbc", "bbc"]. [] when the query has no such word, or is nothing but such words."""
    toks = _TOKEN.findall(q.lower())
    if not 2 <= len(toks) <= 7 or not any(t in _OUTLET_TAIL for t in toks):
        return []
    out, lo, hi = [], 0, len(toks)
    while hi - lo > 1 and toks[hi - 1] in _OUTLET_TAIL:
        hi -= 1
        out.append(" ".join(toks[lo:hi]))
    while hi - lo > 1 and toks[lo] in _OUTLET_TAIL:
        lo += 1
        out.append(" ".join(toks[lo:hi]))
    return [n for n in dict.fromkeys(out) if any(t not in _OUTLET_TAIL and t not in _NAV_NOISE for t in n.split())][:OUTLET_TRIES]

def is_news_outlet(url: str, name: str) -> bool:
    if any(t in ("news", "newspaper") for t in name.split()):
        return True
    lk = lookups()
    if lk is None or lk.name != "release" or lk.kinds is None:
        return False
    import lookups_release as R
    return any(lk.kinds.news_outlet(d) for d in lk.names1(_domain_of(url), R.N_DOMAIN)[:1])

def outlet_first(q: str, res: list[dict], k: int) -> list[dict]:
    """res with the outlet the query names in front, or res unchanged."""
    # a well-known or official site already answers by name ("cnn news", "bbc news"). A site that is merely called
    # the whole query does not stop the outlet: "npr live" is NPR, not nprlive.com
    if (res and res[0].get("source") == "site" and res[0]["score"] >= NAV_SCORE - 2
            and (res[0].get("popular") or res[0].get("official"))):
        return res
    names = outlet_names(q)
    # "guardian latest news": the paper is "the guardian" (theguardian.com); "guardian" alone is guardian.co.uk, an
    # address with no kinds row. A one-word name is also tried with its article.
    names = [n2 for n in names for n2 in ((n, "the " + n) if " " not in n else (n,))][:OUTLET_TRIES + 2]
    for name in names:
        # what the name alone leads with, learned order included: by score alone "cbc" is cbc.edu, not cbc.ca.
        # NAV_SCORE - 3 is the whole name tier ("sky news": news.sky.com at 997.9)
        named = [r for r in _search(name, 5, False, True, False)[:3]
                 if r.get("source") == "site" and r["score"] >= NAV_SCORE - 3 and (r.get("popular") or r.get("official"))]
        top = named[0] if named else None
        # the outlet may be the second site of that name: "msnbc" is msnbc.com (no kinds row) and then ms.now, its
        # official site and a cable news channel
        if top and any(is_news_outlet(r["url"], name) for r in named):
            lead = dict(top, score=OUTLET_SCORE, outlet=True)
            same = same_page(lead["url"])
            return ([lead] + [r for r in res if same_page(r["url"]) != same])[:k]
    return res

def _search_corrected(q: str, k: int, semantic: bool, learned: bool, correct: bool, ce: bool = True) -> list[dict]:
    # The correction lookup starts alongside the search; its answer is used only when
    # nothing matched exactly.
    fix = (_EXEC.submit(contextvars.copy_context().run, _staged, "typo", correct_query, q)
           if correct and typo_shape(q) else None)
    with stage("search"):
        res = _search(q, k, semantic, learned, ce)
    with stage("outlet"):
        res = outlet_first(q, res, k)
    if fix is not None and needs_correction(q, res):
        # the correction found a name; over a lifted kind it must be a named SITE, so a
        # real kind query ("luggage brand site") is not traded for a near-miss name
        floor = NAV_SCORE - 2 if res and res[0].get("kind") else WIKI_EXACT_SCORE - 3
        # a known name one slip away first (typo dev halves: wiki 82.7% against 79.4% the other way
        # round), then the index's spelling, which reaches two slips and parts of names
        for name, attempt in (("typo_wide", lambda: correct_wide(q)), ("typo_wait", fix.result), ("typo_words", lambda: correct_words(q))):
            with stage(name):
                fixed = attempt()
                if fixed and (respells_known_words(q, fixed) or corrects_state_away(q, fixed)):
                    fixed = None                           #: every word it changes is a word in its own right;
                                                           # the state a resident named stays
            if fixed:
                with stage("search_fixed"):
                    res2 = _search(fixed, k, semantic, learned, ce)
                if res2 and res2[0]["score"] >= floor:
                    return [dict(r, corrected=fixed) for r in res2]
    return res

def exact_names(q: str, keys: list[str], dq: str | None) -> list[tuple]:
    """Every (name, names.kind) the exact-match tiers of _search() can ask for this query, so a
    release backend answers them all with one names lookup (lookups_release.prefetch)."""
    import lookups_release as R
    nq = norm_key(q)
    out = [(dq, R.N_DOMAIN)] if dq else []
    for k in keys:
        out += [(k, R.N_SITE_LABEL), (k, R.N_HOST), (k.replace("-", ""), R.N_HOST_NAV)]
    yk = youtube_query(q)[0]
    if yk:
        out.append((yk, R.N_YOUTUBE))
    acct = account_query(q)
    if acct:
        out += [(n, R.N_ACCOUNT) for n in set(the_keys(acct[1]) + [acct[1]])]
    if nq:
        out += [(nq, R.N_ALIAS), (nq, R.N_HOST), (nq, R.N_ACCOUNT), (nq, R.N_SELF)]
    sk = stock_query(q)
    if sk:
        out.append((sk, R.N_STOCK))
    if re.fullmatch(r"[a-z]{2,5}", " ".join(q.lower().split())):
        out.append((" ".join(q.lower().split()), R.N_TICKER))
    return out

GOV_PAGES_GIVEN = int(os.environ.get("SEARCH_GOV_PAGES_GIVEN", "12"))   # of the pool, to the reranker

_GOV_SERVICE_WORD = re.compile(r"\b(licen[cs]es?|licensing|permits?|renew\w*|registration|register|apply|applications?|"
                               r"requirements?|certificates?|records?|benefits?|claims?|tax(es)?|refunds?|courts?|"
                               r"tickets?|fines?|citations?|reservations?|camping|parks?|inspections?|dmv|bmv|mvd|"
                               r"real id|id card|plates?|title|vital|unclaimed|medicaid|snap|ebt|jury|notary|voter|"
                               r"vot(e|ing)|elections?|statutes?|laws?|regulations?|hunting|fishing|"
                               r"child support|unemployment)\b")

_code_evidence: dict[str, bool] = {}

def code_is_state(state: str, rest: str) -> bool:
    """Whether the rest of a query beside an ambiguous code is a state task (GOV_CODE_* above). Cached per rest;
    False when the backend has no government page words or anything fails."""
    key = " ".join(rest.lower().split())
    if key not in _code_evidence:
        ok = False
        try:
            web = web_text()
            if hasattr(web, "gov_share"):
                with stage("gov_code"):
                    if _GOV_SERVICE_WORD.search(key):
                        share, spread = web.gov_share(key)
                        ok = spread >= GOV_CODE_STATES and share >= web.gov_base()
        except Exception as e:                       # never let the evidence break a search
            print(f"gov_code: {type(e).__name__}: {e}", flush=True)
        if len(_code_evidence) > 50000:
            _code_evidence.clear()
        _code_evidence[key] = ok
    return _code_evidence[key]

def gov_query_state(q: str) -> tuple[str, str, bool] | None:
    """(state, the other words, weak) when the query names one state and the release knows whose sites are whose."""
    if "govstate" in _drop.get() or not gov_states():
        return None
    return gov_state.query_state(q, US_STATES, None if "govcode" in _drop.get() else code_is_state)

# A query that names no state is about the searcher's own state when they have a location set (near=lat,lon from the
# launcher or the public page's settings, per request; else the box's SEARCH_HOME) and its words are a state task by
# the code rule's evidence above: "fishing license", "dmv near me", "jury duty". The state is the nearest Census
# town's (wx_towns, the weather's table). Never when the query names a state, a county or a town of 10,000 people or
# more ("seattle parking ticket"), or asks for the nation's ("irs", "federal"); the state is not lifted over the first
# result (state_page_first is for a state the query named), its pages join the list and other states' answer last.
# ?drop=govhome: off.
_HOME_NEAR = re.compile(r"\b(near me|nearby|near|closest|nearest|in my (?:state|area|county|town)|my state|local)\b")

_HOME_NOT = re.compile(r"\b(county|counties|city of|township|borough|parish|federal|national|nationwide|irs|usa|us|u\.s\.?|"
                       r"america|american|united states|"
                       # the nation's tasks, which no state does: a state page for them is wrong wherever the searcher is
                       r"passports?|copyrights?|trademarks?|patents?|social security|ssn|medicare|visas?|immigration|"
                       r"citizenship|naturali[sz]ation|green card|fafsa|student loans?|usps|postal|post office|"
                       r"veterans? (?:affairs|benefits)|va benefits)\b")

_point_states: dict = {}

def point_state(pt) -> str | None:
    """The US state of a point: its nearest Census town of WX_NEAREST_MIN_POP people or more within ~40 km; None at
    sea, abroad, or without the weather's towns. Cached per ~1 km."""
    key = (round(pt[0], 2), round(pt[1], 2))
    if key not in _point_states:
        st = None
        try:
            rows = _news_rows(WX_NEAREST_SQL, {"p": WX_NEAREST_MIN_POP, "lat": pt[0], "lon": pt[1]})
            st = rows[0][1] if rows and rows[0][1] in set(US_STATES.values()) else None
        except Exception as e:                       # never let the lookup break a search
            print(f"point_state: {type(e).__name__}: {e}", flush=True)
        if len(_point_states) > 10000:
            _point_states.clear()
        _point_states[key] = st
    return _point_states[key]

def names_place(q: str) -> bool:
    """A state's name, or a town of NAMED_TOWN_MIN_POP or more, in the query's words (one to three in a row)."""
    words = re.findall(r"[a-z0-9]+", q.lower())
    names = {k for k in US_STATES if len(k) > 2}
    grams = {" ".join(words[i:i + n]) for n in (1, 2, 3) for i in range(len(words) - n + 1)}
    if grams & names:
        return True
    keys = sorted({norm_key(g) for g in grams if len(norm_key(g)) >= 4})
    try:
        return bool(keys) and bool(_news_rows(NAMED_TOWN_SQL, {"k": keys, "p": NAMED_TOWN_MIN_POP}))
    except Exception as e:                           # no towns to read: say a place is named, so no state is assumed
        print(f"names_place: {type(e).__name__}: {e}", flush=True)
        return True

def home_gov_state(q: str) -> tuple[str, str, bool] | None:
    """(the searcher's state, the query without "near me", False) for a state task asked without a state, by a
    searcher whose location is set; None otherwise (see above)."""
    pt = home_point()
    if not pt or "govhome" in _drop.get() or "govstate" in _drop.get() or not gov_states():
        return None
    low = " ".join(q.lower().split())
    if _HOME_NOT.search(low):
        return None
    rest = " ".join(_HOME_NEAR.sub(" ", low).split())
    words = rest.split()
    # a state code at either end, even one the state rule refused ("in jury duty", "co parenting classes"), may name
    # another state: the searcher's own is never assumed over it
    if (not rest or words[0] in US_STATES or words[-1] in US_STATES
            or gov_state.query_state(q, US_STATES) is not None or names_place(rest)):
        return None
    state = point_state(pt)
    if not state or not code_is_state(state, rest):
        return None
    return state, rest, False

# Local beyond the state: a searcher with a location set who asks for something their town or county does
# ("trash pickup schedule", "library hours", "parking ticket", "building permit") gets that town's and county's own
# pages, found by the site word the release gives each government page (gov_state.site_token). The town is the nearest
# Census town of WX_NEAREST_MIN_POP or more; its sites and its county's come from gov-states.json's "local" map (the
# .gov registry's City and County rows). Never when the query names a state or another town, or asks for the nation's.
# ?drop=govlocal: off.
_LOCAL_TASK = re.compile(r"\b(trash|garbage|recycling|recycle|bulk (?:item|trash|pickup)|yard waste|leaf (?:pickup|collection)|"
                         r"brush pickup|dump|transfer station|landfill|library|libraries|property tax(?:es)?|tax bill|"
                         r"tax assessor|assessor|parking (?:ticket|permit|citation)s?|building permits?|zoning|code enforcement|"
                         r"water bill|sewer|town hall|city hall|borough hall|municipal court|police (?:report|department)|"
                         r"non.?emergency|animal control|dog licen[cs]e|pet licen[cs]e|recreation|parks and rec|"
                         r"pothole|snow removal|street sweeping|council meeting|town council|city council|"
                         r"town clerk|city clerk|county clerk|voter registration|polling place|sheriff|"
                         r"marriage licen[cs]e|birth certificate|death certificate|health department)\b")

_point_towns: dict = {}

def point_town(pt) -> tuple[str, str, str] | None:
    """(name, state, county) of a point's nearest Census town of WX_NEAREST_MIN_POP or more; cached per ~1 km."""
    key = (round(pt[0], 2), round(pt[1], 2))
    if key not in _point_towns:
        row = None
        try:
            rows = _news_rows(LOCAL_TOWN_SQL, {"p": WX_NEAREST_MIN_POP, "lat": pt[0], "lon": pt[1]})
            row = tuple(rows[0]) if rows else None
        except Exception as e:                       # never let the lookup break a search
            print(f"point_town: {type(e).__name__}: {e}", flush=True)
        if len(_point_towns) > 10000:
            _point_towns.clear()
        _point_towns[key] = row
    return _point_towns[key]

def local_domains(town: tuple[str, str, str]) -> list[str]:
    """The town's .gov sites, then its county's."""
    name, state, county = town
    m = local_map()
    cty = re.sub(r"\s+(county|parish|borough|census area|municipality)$", "", (county or "").strip(), flags=re.I)
    out = list(m["city"].get(gov_state.local_key(state, name), []))
    out += [d for d in m["county"].get(gov_state.local_key(state, cty), []) if d not in out] if cty else []
    return out

def home_local(q: str) -> tuple[list[str], str] | None:
    """(the searcher's town and county sites, the query without "near me") for a local task asked without a place, by a
    searcher whose location is set; None otherwise (see above)."""
    pt = home_point()
    if not pt or "govlocal" in _drop.get() or not gov_states():
        return None
    low = " ".join(q.lower().split())
    if not _LOCAL_TASK.search(low) or re.search(r"\b(federal|national|nationwide|irs|usa|united states)\b", low):
        return None
    rest = " ".join(_HOME_NEAR.sub(" ", low).split())
    words = rest.split()
    if (not rest or words[0] in US_STATES or words[-1] in US_STATES
            or gov_state.query_state(q, US_STATES) is not None or names_place(rest)):
        return None
    town = point_town(pt)
    doms = local_domains(town) if town else []
    return (doms, rest) if doms else None

def local_page_first(hl, res: list[dict]) -> list[dict]:
    """The searcher's own town's or county's page first, for a local task, when one in the first eight carries at least
    half the query's words in its title or address and the first result is a plain site row of somebody else's (never
    over a place, an article, a popular or an official site)."""
    if not hl or len(res) < 2:
        return res
    doms = set(hl[0])
    words = [w for w in re.findall(r"[a-z0-9]+", hl[1]) if len(w) > 1 and not w.isdigit()]
    top = res[0]
    if not words or top.get("popular") or top.get("official") or top.get("source") not in (None, "site"):
        return res
    gs = gov_states()
    if gov_state.host_domain(_domain_of(top.get("url") or ""), gs) in doms:
        return res
    for i, r in enumerate(res[1:8], 1):
        if r.get("source") not in (None, "site") or gov_state.host_domain(_domain_of(r.get("url") or ""), gs) not in doms:
            continue
        named = re.sub(r"[\W_]+", " ", ((r.get("title") or "") + " " + urlsplit(r.get("url") or "").path).lower())
        if sum(w in named.split() for w in words) * 2 >= len(words):
            return [r] + res[:i] + res[i + 1:]
    return res

def state_service_query(q: str) -> bool:
    """The query names one US state (not a weak code such as "al" or "ms" with too few words) and asks for something
    beside it: the shape of a resident's service query. gov-states.json absent or the state rule dropped: False."""
    gs = gov_query_state(q)
    return bool(gs) and not gs[2]

def corrects_state_away(q: str, fixed: str) -> bool:
    """A resident's service query keeps its state through a spelling correction. "nj fishing license" was corrected to
    the article "Fishing license" (rc11 acceptance, 2026-10-28): the correction found a name, the state's own
    page was first without it. True when q named a state (not a weak code) and `fixed` no longer names that state."""
    gs = gov_query_state(q)
    if not gs or gs[2]:
        return False
    after = gov_query_state(fixed)
    return not after or after[0] != gs[0]

def _task_words_hit(words, toks, endings: bool = False) -> int:
    def has(w):
        w = w[:-1] if len(w) > 4 and w.endswith("s") else w      # "tolls" reaches "tolling"
        if endings:
            w = w[:-1] if len(w) > 4 and w.endswith("e") else w  # "license" reaches "licensing"
        return any(t.startswith(w) or (len(t) >= 4 and w.startswith(t)) for t in toks)
    return sum(has(w) or any(has(x) for x in GOV_SAME_WORDS.get(w, ())) for w in words)

def best_state_page(state: str, words: list[str], res: list[dict]) -> list[dict]:
    """The state's own page is first: is it the page for the task? One for one audience, or short of the query's words,
    gives way to one of the state's pages further down that is for everyone and carries more of them, the page nearest
    the site's top among equals."""
    doms = gov_states()

    def rate(r):
        toks = set(re.findall(r"[a-z0-9]+", ((r.get("title") or "") + " " + urlsplit(r["url"]).path).lower()))
        return _task_words_hit(words, toks), not not_the_task(r["url"], r.get("title") or "", words)

    hits0, plain0 = rate(res[0])
    if plain0 and hits0 >= len(words):
        return res
    best = None
    for i in range(1, len(res)):
        r = res[i]
        if r.get("source") != "site" or is_local_gov(_domain_of(r.get("url") or "")) \
                or gov_state.host_state(_domain_of(r.get("url") or ""), doms) != state:
            continue
        hits, plain = rate(r)
        if not plain or hits < max(1, (len(words) + 1) // 2) or (plain0 and hits <= hits0):
            continue
        key = (hits, -urlsplit(r["url"]).path.rstrip("/").count("/"), -i)
        if best is None or key > best[0]:
            best = (key, i)
    if best is None:
        return res
    i = best[1]
    return [dict(res[i], state_first=True)] + res[:i] + res[i + 1:]

def state_page_first(gs, res: list[dict]) -> list[dict]:
    """A resident who named their state gets that state's page first when the first result is somebody else's and one
    of the state's pages in the first five carries at least half the other words of the query in its title or address.
    Clef 27B's reading of rc11's 2,000 government queries (2026-10-28): 229 first results were a national site for a
    state's task (irs.gov for "virginia state tax refund status", medicaid.gov, unclaimed.org, rockthevote.org,
    llcuniversity.com) with the state's own page under it. Never for a weak code; never over a popular or an
    official site or one the query names (njtransit.com stays first for "nj transit"); a page of the state that
    carries fewer than half the words is not lifted, a front door least of all."""
    if not gs or gs[2] or len(res) < 2:
        return res
    state, rest = gs[0], gs[1]
    words = [w for w in re.findall(r"[a-z0-9]+", rest.lower()) if len(w) > 1 and not w.isdigit()]
    if not words:
        return res
    doms = gov_states()
    top = res[0]
    if (top.get("source") == "wikipedia" and re.match(r"(?i)list of\b", top.get("title") or "")
            and "govfront" not in _drop.get()):
        # a list article ("List of Colorado state parks") gives way to the state agency the query names
        # (cpw.state.co.us, "Colorado Parks and Wildlife"): the article lists the parks, the agency runs them
        for i in range(1, min(5, len(res))):
            r = res[i]
            if (front_host(r.get("url") or "") and gov_state.host_state(_domain_of(r["url"]), doms) == state
                    and names_task(r.get("title") or "", rest)):
                return [dict(r, state_first=True)] + res[:i] + res[i + 1:]
        return res
    # only a site row is displaced: an article, an account or a place first was put there by its own tier
    # ("2012 new york liberty" -> the season's article, "new york army national guard facebook" -> its page;
    # sq-eval test halves, 2026-10-28: four such first places were lost before this), and a number is no word
    # of the task ("ohio state highway 75" lifted a county's highway plans for "75")
    if top.get("popular") or top.get("official") or top.get("source") not in (None, "site"):
        return res
    top_host = _domain_of(top.get("url") or "")
    top_ours = gov_state.host_state(top_host, doms) == state
    top_town = top_ours and is_local_gov(top_host)        # a town's page for a state's task: the state's own goes over it
    if query_names_domain(rest, top_host) or "".join(words) in top_host.split(".")[0].replace("-", ""):      # "nj transit" -> njtransit.com
        return res
    if top_ours and not top_town:
        return best_state_page(state, words, res)
    need = max(1, (len(words) + 1) // 2)                  # at least half the words: "tax refund" of "state tax refund status"
    best = first = None
    for i in range(1, min(8 if top_town else 5, len(res))):
        r = res[i]
        if r.get("source") == "place" or gov_state.host_state(_domain_of(r.get("url") or ""), doms) != state:
            continue
        if top_town and is_local_gov(_domain_of(r.get("url") or "")):
            continue
        toks = set(re.findall(r"[a-z0-9]+", ((r.get("title") or "") + " " + urlsplit(r["url"]).path).lower()))
        hits = sum(any(t.startswith(w) or (len(t) >= 4 and w.startswith(t)) for t in toks) for w in words)
        if hits >= need and first is None:
            first = i
        wider = _task_words_hit(words, toks, endings=True)                       # "gun" reaches "firearms", "license" "licensing"
        if wider >= need and not _press_release_title(r.get("title") or "") and (best is None or (wider, -i) > best[0]):
            best = ((wider, -i), i)
    if first is None:
        return res
    # The first such page, unless it is a press release and another page qualifies ("nj gun license": a lawsuit's
    # release over the State Police's licensing bureau). Applying this to every lift cost help@1 0.5 points.
    i = best[1] if best is not None and _press_release_title(res[first].get("title") or "") else first
    return [dict(res[i], state_first=True)] + res[:i] + res[i + 1:]

# The state's page by what its own site calls it (state_anchor.py): "nj state parks" -> the page the parks
# site links to as "NJ State Parks", which the engine did not have among its candidates at all. Only a close match
# (STATE_ANCHOR_MIN of the words, both ways) linked from STATE_ANCHOR_SOURCES pages, never over a popular or official
# site or one the query names, and not when one of the first three is a page its site calls that as closely from as
# many pages ("nj unemployment": a contact page linked once as "Unemployment" does not keep the section linked twice).
# SEARCH_STATE_ANCHORS unset or missing: off. ?drop=stateanchor: off.
STATE_ANCHORS = os.environ.get("SEARCH_STATE_ANCHORS", "")

STATE_ANCHOR_MIN = float(os.environ.get("SEARCH_STATE_ANCHOR_MIN", "0.67"))

STATE_ANCHOR_SOURCES = int(os.environ.get("SEARCH_STATE_ANCHOR_SOURCES", "2"))

_state_anchor_db = threading.local()

def _state_anchors():
    if not STATE_ANCHORS or not os.path.exists(STATE_ANCHORS):
        return None
    db = getattr(_state_anchor_db, "c", None)
    if db is None:
        import sqlite3
        db = _state_anchor_db.c = sqlite3.connect(f"file:{STATE_ANCHORS}?mode=ro", uri=True, check_same_thread=False)
    return db

def state_anchor_first(gs, res: list[dict]) -> list[dict]:
    if not gs or gs[2] or not res or "stateanchor" in _drop.get():
        return res
    db = _state_anchors()
    if db is None:
        return res
    import state_anchor
    top = res[0]
    if top.get("popular") or top.get("official") or top.get("source") not in (None, "site") \
            or query_names_domain(gs[1], _domain_of(top.get("url") or "")):
        return res
    hit = state_anchor.best(db, gs[1])
    if not hit:
        return res
    j, n, url, title, _phrase = hit
    if j < STATE_ANCHOR_MIN or n < STATE_ANCHOR_SOURCES \
            or gov_state.host_state(_domain_of(url), gov_states()) != gs[0] \
            or state_anchor.match_of(db, gs[1], [r.get("url") or "" for r in res[:3]]) >= (j, n):
        return res
    hp = state_anchor.host_path(url)
    same = [r for r in res if state_anchor.host_path(r.get("url") or "") == hp]
    row = dict(same[0]) if same else {"url": url, "title": title or url, "content": "", "source": "site", "score": 0.0}
    return [dict(row, state_anchor=True)] + [r for r in res if state_anchor.host_path(r.get("url") or "") != hp]

# The searcher's own state, lifted: a state task asked without a state ("fishing license" from a point in New
# Jersey) found that state's pages but left them at five to ten under the articles. The best of them in the first ten
# that carries at least half the query's words moves up to SEARCH_HOME_RANK (1: over the first result, but never over
# a place, an account, a popular or an official site; 2: under it). gov-eval's home wording on rc11, 2026-10-29 (1,000
# queries, vs drop=govhome at 0.0/0.3): rank 1 help@1 19.8% help@5 24.1%, rank 2 4.0% / 24.0%; the open sets and the
# named-state set unchanged at either. ?drop=govhome: off with the rest of the home rule.
HOME_RANK = max(1, int(os.environ.get("SEARCH_HOME_RANK", "1")))

_HOME_GENERIC = frozenset("""a an the to for of in on my me how get new online state apply application applications applying
    license licenses licence licences licensing permit permits renew renewal register registration requirements status
    copy copies pay payment payments form forms file filing request replace replacement become check lookup search
    """.split())

def home_page_up(gq, res: list[dict]) -> list[dict]:
    if not gq or len(res) < 2:
        return res
    state, rest = gq[0], gq[1]
    words = [w for w in re.findall(r"[a-z0-9]+", rest.lower()) if len(w) > 1 and not w.isdigit()]
    if not words:
        return res
    doms = gov_states()
    top = res[0]
    to = HOME_RANK - 1
    if to == 0 and (top.get("popular") or top.get("official") or top.get("source") in ("place", "account")):
        to = 1
    if any(gov_state.host_state(_domain_of(r.get("url") or ""), doms) == state for r in res[:to + 1]):
        return res                                        # the state is already there
    need = max(1, (len(words) + 1) // 2)
    for i in range(to + 1, min(10, len(res))):
        r = res[i]
        if r.get("source") == "place" or gov_state.host_state(_domain_of(r.get("url") or ""), doms) != state:
            continue
        path = urlsplit(r["url"]).path.lower()
        if _GOV_PATH_NEWS.search(path):                   # a press release or a meeting's minutes is not the service
            continue
        host = (urlsplit(r["url"]).hostname or "").lower()
        toks = set(re.findall(r"[a-z0-9]+", ((r.get("title") or "") + " " + path + " " + host).lower()))
        # the word or the word with a short ending ("renewal", "licenses"), never a longer one: "renew" is not
        # "renewables" (rc11, 2026-10-29: "renew passport" from Newark lifted cleanenergy.nj.gov/.../other-renewables)
        hit = [any((t.startswith(w) and len(t) - len(w) <= 3) or (len(t) >= 4 and w.startswith(t) and len(w) - len(t) <= 3)
                   for t in toks) for w in words]
        # and a word that names the task, not only generic ones: "medicaid application" is not any state board's
        # application page (a reading of 40 rc11 lifts, 2026-10-29: 22 were such pages). One such word is enough:
        # requiring them all lost 32 of 157 good lifts ("jury duty" -> "Jury | Colorado Judicial Branch").
        named = [h for w, h in zip(words, hit) if w not in _HOME_GENERIC]
        if sum(hit) >= need and (not named or any(named)):
            return res[:to] + [dict(r, home_up=True)] + res[to:i] + res[i + 1:]
    return res

def other_states_last(gs, res: list[dict]) -> list[dict]:
    """Another state's government site answers last when the query named a state (never for a `weak` one)."""
    if not gs or gs[2]:
        return res
    doms = gov_states()
    return gov_state.first_and_last(
        gs[0], res, lambda r: None if r.get("source") == "place" else gov_state.host_state(_domain_of(r.get("url") or ""), doms))

SEM_IVF_DOCS_SQL = "SELECT page_id, title, lead FROM wiki_docs WHERE page_id = ANY(%(ids)s)"

def _sem_rows(q: str) -> list[tuple] | None:
    if SEM_BACKEND == "ivf":
        si, ix, enc = sem_ivf()
        code = query_code(q)
        with stage("sem-ivf"):
            hits = ix.search(code, 5, SEM_PROBES)
        if not hits:
            return []
        with stage("sem-docs"):                                # RT-02: the part of sem that had no stage name
            docs = {r[0]: r[1:] for r in _rows(SEM_IVF_DOCS_SQL, {"ids": [p for p, _d in hits]})}
        # (title, lead, sim) as the Postgres form returns them; sim = share of the 768 bits that agree
        return [(docs[p][0], docs[p][1], 1 - d / 768.0) for p, d in hits if p in docs]
    with stage("embed"):
        e = embed(q)
    if e is None:
        return None
    vec = "[" + ",".join(f"{x:.5f}" for x in e) + "]"
    return _rows(SEM_WIKI_SQL, {"e": vec, "k": 5}, "SET hnsw.ef_search = 40")

# A site's own pages (2026-11-10; site_pages.py): a search that is a site's name plus more words ("nike air max 90",
# "kia sportage features") is looked up in a table of that site's page addresses and titles, taken once from its sitemap
# (tools/sitemap-harvest.py + tools/sitemap-keep.py). SEARCH_SITE_PAGES=<site-pages.sqlite> turns it on; unset = off and the
# module is never imported. The best SEARCH_SITE_PAGES_N pages join the fusion as one more list (weight SEARCH_SITE_PAGES_W),
# so the rankers and the cross-encoder see them like any other candidate. Only for a name the engine itself already shows as a
# site among its named rows, and never for a leftover that is one slot's phrase (feat/intent-slots' slot row).
SITE_PAGES_PATH = os.environ.get("SEARCH_SITE_PAGES", "").strip()

SITE_PAGES_N = max(1, int(os.environ.get("SEARCH_SITE_PAGES_N", "3")))

SITE_PAGES_W = float(os.environ.get("SEARCH_SITE_PAGES_W", "1.0"))

_site_pages_state: list = []

_site_pages_lock = threading.Lock()

def site_pages_table():
    """The table, opened once; None when unset or unreadable (the feature stays off, and says so once)."""
    if not SITE_PAGES_PATH:
        return None
    with _site_pages_lock:
        if not _site_pages_state:
            try:
                import site_pages
                t = site_pages.SitePages(SITE_PAGES_PATH)
                print(f"site pages {SITE_PAGES_PATH}: {len(t)} sites, {t.rows} pages", flush=True)
            except Exception as e:  # noqa: BLE001 - an optional extra: never fail the engine for it
                print(f"site pages {SITE_PAGES_PATH}: not read ({type(e).__name__}: {e})", flush=True)
                t = None
            _site_pages_state.append(t)
    return _site_pages_state[0]

def site_page_rows(q: str, out: list[dict]) -> list[dict]:
    """Candidate rows from the named site's own pages for "name + words"; [] when off, or when no named site row of the
    first six matches the name, or when no page of the site holds every leftover word. `out`: the rows named so far."""
    t = site_pages_table()
    if t is None or not out:
        return []
    top = sorted((r for r in out if r.get("source") == "site" and r.get("url")), key=lambda r: r["score"], reverse=True)[:6]
    if not top:
        return []
    try:
        import site_pages
        got = t.look_up(q, [r["url"] for r in top], SITE_PAGES_N)
        return [dict(_site(g["url"], site_pages.row_title(g), "", 1.0), site_page=True) for g in got]
    except Exception as e:  # noqa: BLE001
        print(f"site pages: {type(e).__name__}", flush=True)
        return []
