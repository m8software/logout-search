"""Local search API for SearXNG — the serving half of apps/search.

GET /search?q=...&k=10  -> {"results": [{"url", "title", "content", "source", "score"}],
                            "confident": bool}   (see CONFIDENT_RANK)
GET /healthz            -> 200 "ok" when the database answers

SearXNG's json_engine reads `results`, `url`, `title`, `content`. On a box that
also runs SearXNG it has no host port (only the SearXNG network reaches it). Where
it runs on another LAN host (indexhost, 2026-09-24: the personal server had no RAM
for the Wikipedia index) it gets a LAN port, and SEARCH_API_ALLOW_FROM limits it
to the SearXNG host; everything else gets 403. No token either way. Stdlib HTTP server + a small
pool of Postgres connections; a serve swap terminates them, so a broken one is dropped
and the search retried once on fresh connections.
"""

from __future__ import annotations

import contextlib
import contextvars
import datetime as dt
import base64
import ipaddress
import functools
import json
import math
import os
# One thread per matrix call (2026-11-03): numpy's OpenBLAS had no limit, so each of a search's ~20 parallel lists could
# start four more threads on the public server's four cores, which mostly spin. Set before numpy is first imported;
# the environment still wins, for an A/B.
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
import re
import sys
import threading
import time
import unicodedata
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlsplit

import psycopg

import gov_state

# "Never fetched" for the (fetched_at, value) caches below. time.monotonic() counts from the
# MACHINE's boot, so a 0.0 stamp looked fresh to a service started within a TTL of booting:
# tables() kept its empty start value and every search answered "200, zero results" for the
# first minute after a reboot (repro/407).
_NEVER = float("-inf")

PORT = int(os.environ.get("SEARCH_API_PORT", "8090"))
BIND = os.environ.get("SEARCH_API_BIND", "0.0.0.0")   # a home machine binds 127.0.0.1 (installer/install.py engine_env); servers keep every interface
# Comma-separated networks allowed to query; empty = any (the no-host-port case).
# Loopback is always allowed so the container healthcheck works.
def _allow_list(raw: str) -> list:
    """Parse SEARCH_API_ALLOW_FROM. A malformed entry is skipped with a warning, never fatal:
    "172.16.0.48/28fdd0:0:0:3::/64" (two subnets run together) crash-looped search-api and
    took Loggedout search down for ~3 min (2026-09-24). The valid entries still apply —
    skipping one can only allow LESS."""
    nets = []
    for n in raw.split(","):
        n = n.strip()
        if not n:
            continue
        try:
            nets.append(ipaddress.ip_network(n, strict=False))
        except ValueError:
            print(f"SEARCH_API_ALLOW_FROM: ignoring malformed entry {n!r}", flush=True)
    return nets


ALLOW_FROM = _allow_list(os.environ.get("SEARCH_API_ALLOW_FROM", ""))


def client_allowed(ip: str) -> bool:
    if not ALLOW_FROM:
        return True
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return a.is_loopback or any(a in n for n in ALLOW_FROM)
MAX_K = 20
# BM25 below this is a weak match (slice test 2026-09-23: right answers 11-25,
# wrong ones 5-10); popularity only boosts results that already match strongly.
STRONG_BM25 = float(os.environ.get("SEARCH_STRONG_BM25", "10"))

# ---------------------------------------------------------------------------
# Ranking, in tiers (measured against the buildbox query set):
#   1. the query names a site      "costco", "wells fargo"   → that domain first
#   2. the query names an article  "corpus callosum", "louis ck", "750ti" (redirect)
#   3. keyword match, title/domain weighted 2x over body text, popularity only
#      after a strong match
#   4. typo fallback               "craigsk" → craigslist, only when 1-3 are weak
# Tier scores are far apart on purpose, so the SearXNG ordering is the tier order.
# ---------------------------------------------------------------------------
NAV_SCORE, WIKI_EXACT_SCORE = 1000.0, 900.0

# "Confident" = local search alone answers this query, so a caller could skip the web
# engines. Measured on the buildbox query set (2026-09-24): the only tier that is right when
# it fires is a domain-name match on a POPULAR site — alibaba.com / brave.com /
# disneyplus.com (Tranco 313-809) were right; btc.bw, aldi.com.au, gg.ca,
# you.boutique (70k-614k) were not. Exact Wikipedia titles are never confident:
# "carpet", "gym", "restaurants" match an article but meant shopping or a place.
CONFIDENT_RANK = int(os.environ.get("SEARCH_CONFIDENT_RANK", "10000"))
# The same line on OUR rank (G3 D2, importer/own_rank.py). 10000 was fitted on Tranco's order; our rank orders the same
# brands lower (gog.com 24,498, optimum.com 18,096, primevideo.com 19,108), so at 10000 they lost first place to a
# namesake or to their own article (rc4 acceptance, hand v3 -2 queries). Swept on the dev halves, 2026-10-06
# (docs/internal/audits/2026-10-06-rc4.md): 25000 changes no top result on 2,445 open dev queries and wins the three
# back; 40000 already lets a site pass an article (sat.gob.mx 27,132, cts.com.tw 27,435). The window is narrow.
CONFIDENT_RANK_OWN = int(os.environ.get("SEARCH_CONFIDENT_RANK_OWN", "25000"))


def confident_rank_for(own: bool) -> int:
    """The popularity line for the rank in use: an explicit SEARCH_CONFIDENT_RANK always wins; else our own rank's line
    when the ranks ARE our own (a release that carries site.own_rank, or SEARCH_SITE_RANK=own), else Tranco's."""
    if "SEARCH_CONFIDENT_RANK" in os.environ:
        return CONFIDENT_RANK
    return CONFIDENT_RANK_OWN if own else CONFIDENT_RANK
# Which site-popularity rank the Postgres engine reads (G3 D2, importer/own_rank.py): "tranco" (default, what the
# serving database carries today) or "own" (own_rank / nav_sites_own / host_nav_own, built by serve-prep.sql).
SITE_RANK = os.environ.get("SEARCH_SITE_RANK", "tranco").strip().lower()
if SITE_RANK not in ("tranco", "own"):
    raise SystemExit(f"SEARCH_SITE_RANK={SITE_RANK!r}: use 'tranco' or 'own'")

# Words that do not change which SITE is meant ("brave search", "costco website").
_NAV_NOISE = frozenset("the a www http https com net org site website official login signin sign in "
                       "home homepage page search app online".split())
_TOKEN = re.compile(r"[a-z0-9]+")


def norm_key(s: str) -> str:
    """Letters and digits only, lowercase — the same key the build side stores."""
    return "".join(_TOKEN.findall(s.lower()))


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
# A typed domain ("costco.co.uk", "drive.google.com", "https://www.bbc.com/") is that site.
_DOMAIN_Q = re.compile(r"^(?:https?://)?(?:www\.)?((?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,})/?$")
DOMAIN_DOC_SQL = """
SELECT d.url, d.title, d.description, s.tranco_rank FROM docs d LEFT JOIN serve_sites s USING (domain)
WHERE d.error IS NULL AND d.domain = %(d)s LIMIT 1
"""
DOMAIN_NAV_SQL = "SELECT rank FROM nav_sites WHERE host = %(d)s ORDER BY rank LIMIT 1"


def domain_query(q: str) -> str | None:
    m = _DOMAIN_Q.match(q.strip().lower())
    return m.group(1) if m else None


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


# Domain endings a US-based user means by a bare name. A foreign country version
# (costco.co.uk, aldi.de, webarchive.org.uk) must not beat the .com — it did whenever
# the main site's homepage was missing from the index (2026-09-24 probe).
GENERIC_TLD = r"\.(com|org|net|us|gov|edu|io|co|app|dev|ai|tv|me|info|mil)$"
_GENERIC = re.compile(GENERIC_TLD)
# A country's second level ("google.com.ai", "google.com.co", "bbc.co.uk") ends in a
# "generic" label but IS a country version: google.com.ai came first for "google".
_CC_SLD = re.compile(r"\.(com|co|net|org|gov|edu|ac|or|ne|go)\.[a-z]{2}$")


def is_generic(host: str) -> bool:
    return bool(_GENERIC.search(host)) and not _CC_SLD.search(host)

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

# Same lookup plus the article's official website (wiki_sites, built from the same
# dump). "costco" -> Costco's article -> costco.com, even when costco.com's homepage
# was never fetched.
WIKI_EXACT_SITE_SQL = """
SELECT title, lead, url, box, tranco_rank, history_visits, own_title FROM (
    SELECT DISTINCT w.title, w.lead, ws.url, ws.box, ws.tranco_rank, ws.history_visits,
           lower(regexp_replace(w.title, '[^[:alnum:]]', '', 'g')) = %(nq)s AS own_title
    FROM wiki_aliases a JOIN wiki_docs w USING (page_id)
    LEFT JOIN wiki_sites ws USING (page_id)
    WHERE a.norm = %(nq)s
      -- not the domain-ending articles (".amazon", ".gmail" via Google Registry): their
      -- registry sites are noise for a brand query
      AND w.title NOT LIKE '.%%' AND coalesce(ws.url, '') !~ '^https?://(www\\.)?(nic|registry)\\.') x
-- The article whose OWN title is the query first ("facebook": Facebook, not the
-- "Face book" redirect), then the one whose site is most popular.
ORDER BY own_title DESC, tranco_rank NULLS LAST LIMIT 3
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


# Hosts that are never an "official site" answer, whatever Wikipedia lists: a social
# profile or link hub. "youth reform movement" -> https://www.fb.com/YRMPH was marked
# sure (2026-09-24 gate check); fb.com / youtu.be slipped past facebook.com / youtube.com.
_SOCIAL_HOSTS = frozenset("""facebook.com fb.com fb.me m.facebook.com instagram.com instagr.am twitter.com x.com t.co
youtube.com youtu.be linkedin.com lnkd.in tiktok.com linktr.ee threads.net""".split())


# Press-release wires are nobody's official site (Home Depot's article linked a
# prnewswire.com release, which then outranked homedepot.com, 2026-09-25).
_WIRE_HOSTS = frozenset("prnewswire.com businesswire.com globenewswire.com prweb.com newswire.com "
                        "accesswire.com einpresswire.com".split())


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


# Which of an article and its official site leads when the query is the article's own name. Owner, 2026-10-07 (final):
# "site first then article on everything ... always showing wikipedia as first result really doesn't make us much of a
# search engine" - so the SITE, whatever the thing is and whatever the site's rank ("snoop dogg" -> snoopdogg.com,
# "italy" -> italia.it), with the article directly under it; on the page the article is the card anyway.
# "people-orgs" keeps the article first for a place (a country's site is a tourism board): the option not taken.
OFFICIAL_FIRST = os.environ.get("SEARCH_OFFICIAL_FIRST", "all").strip().lower()
if OFFICIAL_FIRST not in ("people-orgs", "all"):
    raise SystemExit(f"SEARCH_OFFICIAL_FIRST={OFFICIAL_FIRST!r}: use 'people-orgs' or 'all'")
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


def official_ok(url: str) -> bool:
    return _domain_of(url) not in _SOCIAL_HOSTS and _domain_of(url) not in _WIRE_HOSTS


def official_deep(url: str) -> bool:
    """A page, not a site: movies.disney.com/smith is the film Smith!'s "official site",
    and "smith" was answered with it, sure. Such a link may still be shown, never as a
    popular (confident) answer."""
    path = urlsplit(url).path.strip("/")
    return path.count("/") >= 1 or (len(path) > 0 and _domain_of(url).count(".") >= 2)


_LOCALE_SEG = re.compile(r"[a-z]{2}(?:[-_][a-z]{2,4})?$", re.I)


def official_inner_page(url: str) -> bool:
    """The stored official link is a page deep inside a site, not anyone's home: defense.gouv.fr/terre/presentation/
    organisation-des-forces/... for a regiment, msccruises.de/unsere-kreuzfahrten/kreuzfahrtschiffe/msc-virtuosa for a
    ship (rc5 acceptance, 2026-10-08: "13 dble" and "imo 9803625" led with those over the article the alias names).
    Three or more path segments, language segments not counted. A home one or two levels down still leads:
    mlb.com/yankees, workspace.google.com/products/gmail, ikea.com/us/en."""
    segs = [x for x in urlsplit(url or "").path.split("/") if x and not _LOCALE_SEG.match(x)]
    return len(segs) >= 3


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

# Weight of the head fields (domain words + title + h1) against the whole page in the
# site keyword list — BM25F-style field weighting (Robertson, Zaragoza & Taylor
# 2004, https://doi.org/10.1145/1031171.1031181). 2 was a guess; ?hw= sweeps it.
HEAD_WEIGHT = float(os.environ.get("SEARCH_HEAD_WEIGHT", "2"))
_head_w: contextvars.ContextVar = contextvars.ContextVar("head_w", default=None)
_no_anchors: contextvars.ContextVar = contextvars.ContextVar("no_anchors", default=False)   # eval A/B only (anc=0)
_pages_fuse: contextvars.ContextVar = contextvars.ContextVar("pages_fuse", default=None)   # eval A/B only (pk=, pw=)
# Eval only (off=page_docs,wiki_links): search as if these tables were not in the snapshot —
# what leaving one off a customer box would cost (docs/internal/SEARCH-BOX-PLAN.md).
_off: contextvars.ContextVar = contextvars.ContextVar("off", default=frozenset())
_d2q: contextvars.ContextVar = contextvars.ContextVar("d2q", default=False)                 # eval A/B only (d2q=1)
_no_intent: contextvars.ContextVar = contextvars.ContextVar("no_intent", default=False)     # eval A/B only (intent=0)
_no_webrr: contextvars.ContextVar = contextvars.ContextVar("no_webrr", default=False)       # eval A/B only (webrr=0)
# Which page-text index the site and page lists match against — eval A/B only (# body=bow|none): "bow" is each page's words sorted (no sentences), "none" drops the
# page text and keeps title, description and h1. Tables built by tools/body-variants.sql.
BODY_VARIANTS = ("full", "bow", "none")
_body: contextvars.ContextVar = contextvars.ContextVar("body", default="full")
# Drop-cost evals (ADR-065, 2026-10-01; docs/internal/audits/2026-10-01-drop-costs.md):
# ?drop=a,b searches as if a Jev-derived (or Tranco) input were gone, to measure what each
# removal costs. Ignored unless SEARCH_DROP_EVAL=1 (a throwaway eval container): on any
# other box a query string must not be able to switch the junk filter off.
#   cealt    the reranker runs the model in SEARCH_CE_ALT_DIR (e.g. the untrained base)
#   intent   the web-intent model answers 0 (regex intent only)
#   kindjev  kinds ignore kind_members.jev / .lead: every member trusted, no first-choice order
#   junk     no junk filter;  junkut1  only site_quality rows UT1 flagged (source ut1 / jev+ut1)
#   tranco   Tranco rank replaced by the Common Crawl host rank, mapped onto Tranco's scale
#   cewiki   the reranker may sink a Wikipedia article it scores at its floor (CE_WIKI_FLOOR off)
DROP_EVAL = os.environ.get("SEARCH_DROP_EVAL", "0") == "1"
#   govstate a state named in the query does not order government pages
#   govfront no agency front page for a query naming its business, no list article giving way to it
DROP_SWITCHES = frozenset({"cealt", "cewiki", "intent", "kindjev", "junk", "junkut1", "tranco", "govstate",
                           "govhome", "govlocal", "sempages", "stateanchor", "govfront"})
_drop: contextvars.ContextVar = contextvars.ContextVar("drop", default=frozenset())


def parse_drop(qs: dict) -> frozenset:
    if not DROP_EVAL:
        return frozenset()
    return frozenset(x.strip() for x in (qs.get("drop") or [""])[0].split(",") if x.strip() in DROP_SWITCHES)


def head_weight() -> float:
    v = _head_w.get()
    return HEAD_WEIGHT if v is None else v


HEAD_EXPR = ("(regexp_replace(d.domain, '[.-]+', ' ', 'g') || ' ' || coalesce(d.title, '') || ' ' "
             "|| coalesce(d.h1, ''))")

SITES_SQL = f"""
WITH head AS (
    SELECT d.domain, -({HEAD_EXPR} <@> to_bm25query(%(q)s, 'docs_head_bm25')) AS b
    FROM docs d WHERE d.error IS NULL
    ORDER BY {HEAD_EXPR} <@> to_bm25query(%(q)s, 'docs_head_bm25') LIMIT 50),
full_ AS (
    SELECT d.domain, -(@docs_text@ <@> to_bm25query(%(q)s, 'docs_bm25')) AS b
    FROM docs d WHERE d.error IS NULL
    ORDER BY @docs_text@ <@> to_bm25query(%(q)s, 'docs_bm25') LIMIT 50),
hits AS (
    SELECT domain, %(hw)s * coalesce(max(hb), 0) + coalesce(max(fb), 0) AS bm25
    FROM (SELECT domain, b AS hb, NULL::float8 AS fb FROM head
          UNION ALL SELECT domain, NULL, b FROM full_) u
    GROUP BY domain)
SELECT d.url, d.title, d.description, h.bm25,
       CASE WHEN h.bm25 >= %(strong)s
            THEN h.bm25 * (1 + 0.15 * greatest(0, 14 - ln(coalesce(s.tranco_rank, 2000000)))
                             + 0.10 * greatest(0, 18 - ln(coalesce(s.cc_hc_pos, 100000000))))
            ELSE h.bm25 END AS score
FROM hits h JOIN docs d ON d.domain = h.domain AND d.error IS NULL
LEFT JOIN serve_sites s ON s.domain = h.domain
ORDER BY score DESC LIMIT %(k)s
"""

# What other sites call a site when they link to it (Common Crawl anchor text,
# anchor_docs): "the paper of record" -> nytimes.com. One more fused list; each anchor
# already weighs by how many sites use it (anchors.cmd_load).
ANCHORS_SQL = """
SELECT d.url, d.title, d.description, a.bm25
       * (1 + 0.15 * greatest(0, 14 - ln(coalesce(s.tranco_rank, 2000000)))
            + 0.10 * greatest(0, 18 - ln(coalesce(s.cc_hc_pos, 100000000)))) AS bm25 FROM (
    SELECT domain, -(body <@> to_bm25query(%(q)s, 'anchor_docs_bm25')) AS bm25
    FROM anchor_docs ORDER BY body <@> to_bm25query(%(q)s, 'anchor_docs_bm25') LIMIT 20) a
JOIN docs d ON d.domain = a.domain AND d.error IS NULL
LEFT JOIN serve_sites s ON s.domain = a.domain
ORDER BY 4 DESC LIMIT %(k)s
"""
# Searches a language model wrote for each site (doc2query, d2q_docs): "newspaper of
# record new york" -> nytimes.com. Off unless ?d2q=1 until it is measured (2026-09-29).
D2Q_SQL = ANCHORS_SQL.replace("anchor_docs_bm25", "d2q_docs_bm25").replace("FROM anchor_docs", "FROM d2q_docs")
# The query IS what many sites call one site when they link to it ("wayback machine": 150
# sites say it of archive.org, 14 of the next) — as good as its name (2026-09-26). The
# weighting above keeps a one-word match on an obscure site (papermag.com for "the paper
# of record") under the known sites, as SITES_SQL does.
EXACT_ANCHOR_SQL = """
SELECT a.domain, a.sources, d.url, d.title, d.description FROM anchors a
JOIN docs d ON d.domain = a.domain AND d.error IS NULL
WHERE a.anchor = %(q)s ORDER BY a.sources DESC LIMIT 2"""
EXACT_ANCHOR_MIN, EXACT_ANCHOR_LEAD = 10, 3.0

WIKI_SQL = """
SELECT title, lead, -(@wiki_text@ <@> to_bm25query(%(q)s, 'wiki_docs_bm25')) AS bm25
FROM wiki_docs
ORDER BY @wiki_text@ <@> to_bm25query(%(q)s, 'wiki_docs_bm25') LIMIT %(k)s
"""

# ---------------------------------------------------------------------------
# Retrieval interface (SEARCH-TANTIVY-PLAN phase 5, ADR-063): each text list sits behind
# one small object, so the downloadable engine can swap Postgres for Tantivy one list at
# a time. Only the Wikipedia text list is ported so far; Postgres stays the default.
#   SEARCH_WIKI_BACKEND=postgres | tantivy | tantivy-release     SEARCH_WIKI_INDEX=<Tantivy index directory>
#   tantivy-release: the release layout (text/wiki, nothing stored: doc_id only), titles and leads from
#   the release lookups (SEARCH_LOOKUP_BACKEND=release); SEARCH_WIKI_INDEX defaults to <release>/text/wiki.
# Every backend returns the rows WIKI_SQL returns: (title, lead, bm25), best first.
# ---------------------------------------------------------------------------
WIKI_BACKEND = os.environ.get("SEARCH_WIKI_BACKEND", "postgres").strip().lower()
WIKI_INDEX = os.environ.get("SEARCH_WIKI_INDEX", "/data/wiki-tantivy")

# Postgres's english.stop (127 words), so the Tantivy analyzer drops what to_bm25query
# drops: simple tokenizer + lowercase + these + the English (Snowball) stemmer.
PG_ENGLISH_STOP = (
    "i me my myself we our ours ourselves you your yours yourself yourselves he him his himself she "
    "her hers herself it its itself they them their theirs themselves what which who whom this that "
    "these those am is are was were be been being have has had having do does did doing a an the and "
    "but if or because as until while of at by for with about against between into through during "
    "before after above below to from up down in out on off over under again further then once here "
    "there when where why how all any both each few more most other some such no nor not only own "
    "same so than too very s t can will just don should now").split()

def _ort_lean(opts) -> None:
    """Every onnxruntime session: no arena / memory pattern; SEARCH_ORT_PREPACK=0 skips repacked weights
    (sem_ivf._ort_lean has the measurements)."""
    opts.enable_cpu_mem_arena = False
    opts.enable_mem_pattern = False
    if os.environ.get("SEARCH_ORT_PREPACK", "1") == "0":
        opts.add_session_config_entry("session.disable_prepacking", "1")
    _ort_spin(opts)


def _ort_spin(opts) -> None:
    """onnxruntime's pool threads spin between runs by default; with five sessions and ~20 lists on four cores the
    spinning takes cores from the lists (2026-11-03). SEARCH_ORT_SPIN=1 brings it back, for an A/B."""
    if os.environ.get("SEARCH_ORT_SPIN", "0") != "1":
        opts.add_session_config_entry("session.intra_op.allow_spinning", "0")

TANTIVY_ANALYZER = "en_pg"
TANTIVY_ANALYZERS = ("en_pg", "en_pg2")
PG_TOKEN_RE = r"[^\W_]+(?:[.@][^\W_]+)+|[^\W_]+"
ANALYZER_FILE = "logout-analyzer.txt"   # which analyzer a site/page index was built with


def _release_root(path: str) -> str:
    """release_root.resolve: BASE / BASE/current -> the release folder `current` names now, pinned for the process."""
    try:
        import release_root
    except ImportError:                                  # loaded by file path, api/ not on sys.path
        import importlib.util
        spec = importlib.util.spec_from_file_location("release_root", os.path.join(os.path.dirname(os.path.abspath(__file__)), "release_root.py"))
        release_root = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(release_root)
    return release_root.resolve(path)


def _open_index(tv, path: str):
    """tantivy.Index.open that also works on a read-only release folder (tv_open.py)."""
    try:
        import tv_open
    except ImportError:                                  # loaded by file path, api/ not on sys.path
        import importlib.util
        spec = importlib.util.spec_from_file_location("tv_open", os.path.join(os.path.dirname(os.path.abspath(__file__)), "tv_open.py"))
        tv_open = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(tv_open)
    return tv_open.open_index(tv, path)


class PgWikiText:
    """The Wikipedia text list as it always was: pg_textsearch BM25 over wiki_docs."""
    name = "postgres"

    def search(self, q: str, k: int, stored: frozenset = frozenset()) -> list[tuple]:
        return _rows(with_text(WIKI_SQL, stored), {"q": q, "k": k})


class TantivyWikiText:
    """The same list from a Tantivy index (built by the export side: fields page_id, title,
    body = title+aliases+infobox+headings+lead, snippet = lead[:300] as bytes). BM25 with
    the same k1/b and IDF as pg_textsearch: scores agree to ~0.2% on the same article
    (testbox, 2026-10-01), so STRONG_BM25 keeps its meaning. The snippet is all _wiki() shows
    of the lead, so no Postgres round trip is needed. tantivy is imported here only, so
    the Postgres default needs no tantivy installed."""
    name = "tantivy"

    def __init__(self, path: str):
        import tantivy
        self.tv = tantivy
        self.index = _open_index(tantivy, path)
        self.analyzer = self.build_analyzer(tantivy)
        self.index.register_tokenizer(TANTIVY_ANALYZER, self.analyzer)
        self.index.reload()
        self.searcher = self.index.searcher()
        self.schema = self.index.schema
        self.num_docs = self.searcher.num_docs

    @staticmethod
    def build_analyzer(tantivy, name: str = TANTIVY_ANALYZER):
        """en_pg: simple tokenizer (splits at every non-letter/digit). en_pg2: also keeps
        dotted and @ tokens whole (golf.nl, fable5.1, a@b.org), as Postgres's parser does
        for hosts, files, versions and e-mail addresses."""
        tok = tantivy.Tokenizer.regex(PG_TOKEN_RE) if name == "en_pg2" else tantivy.Tokenizer.simple()
        return (tantivy.TextAnalyzerBuilder(tok)
                .filter(tantivy.Filter.lowercase())
                .filter(tantivy.Filter.custom_stopword(PG_ENGLISH_STOP))
                .filter(tantivy.Filter.stemmer("english"))
                .build())

    def terms(self, q: str) -> list[str]:
        seen: list[str] = []
        for t in self.analyzer.analyze(q):
            if t not in seen:
                seen.append(t)
        return seen

    def search(self, q: str, k: int, stored: frozenset = frozenset()) -> list[tuple]:
        tv = self.tv
        terms = self.terms(q)
        if not terms:                                    # only stopwords: nothing to match
            return []
        with stage("wiki_tv"):
            query = tv.Query.boolean_query([(tv.Occur.Should, tv.Query.term_query(self.schema, "body", t))
                                            for t in terms])
            out = []
            for score, addr in self.searcher.search(query, k).hits:
                d = self.searcher.doc(addr)
                out.append((d["title"][0], bytes(d["snippet"][0]).decode("utf-8", "replace"), float(score)))
            return out


class TantivyReleaseWikiText:
    """The Wikipedia text list from the release's index (SEARCH-RELEASE-FORMAT rule 3: Tantivy
    stores nothing displayable). The index holds searchable text and a fast `doc_id` field (the
    registry's id); the title and the lead snippet come from the release lookups, one registry
    row and one card per hit, so the rows are the ones TantivyWikiText returns. Needs
    SEARCH_LOOKUP_BACKEND=release. The analyzer is the index's own (logout-analyzer.txt, else en_pg)."""
    name = "tantivy-release"

    def __init__(self, path: str):
        import tantivy
        self.tv = tantivy
        self.ix = _TvIndex(tantivy, path)
        self.num_docs = self.ix.searcher.num_docs

    def terms(self, q: str) -> list[str]:
        seen: list[str] = []
        for t in self.ix.analyzer.analyze(q):
            if t not in seen:
                seen.append(t)
        return seen

    def doc_freq(self, word: str) -> int:
        """Articles whose text has the word (its commonest term); a stopword counts as every article."""
        return max((self.ix.searcher.doc_freq("body", t) for t in self.terms(word)), default=self.num_docs)

    def search(self, q: str, k: int, stored: frozenset = frozenset()) -> list[tuple]:
        tv = self.tv
        terms = self.terms(q)
        if not terms:                                    # only stopwords: nothing to match
            return []
        lk = lookups()
        if lk is None or lk.name != "release":
            raise RuntimeError("SEARCH_WIKI_BACKEND=tantivy-release needs SEARCH_LOOKUP_BACKEND=release")
        with stage("wiki_tv"):
            query = tv.Query.boolean_query([(tv.Occur.Should, tv.Query.term_query(self.ix.schema, "body", t))
                                            for t in terms])
            hits = self.ix.searcher.search(query, k).hits
            ids = self.ix.searcher.fast_field_values("doc_id", [a for _s, a in hits]) if hits else []
        out = []
        for (score, _a), d in zip(hits, ids):
            title = lk.title(d)
            if title:                                    # a doc_id the registry no longer knows is skipped
                out.append((title, (lk.card(d).get("l") or "")[:300], float(score)))
        return out


_wiki_text = None
_wiki_text_lock = threading.Lock()


# Wikipedia meaning search (T3b, ADR-063): Postgres (wiki_vec, bge-small, HNSW) or the release's
# IVF folder over nomic 1-bit codes (sem_ivf.py) — no database, low memory.
#   SEARCH_SEM_BACKEND=postgres | ivf    SEARCH_SEM_IVF=<vectors/ivf2600>    SEARCH_NOMIC_DIR=<model>
#   SEARCH_SEM_PROBES=200 (lists compared per search; 0.935 of the exact top 10 on testbox)
SEM_BACKEND = os.environ.get("SEARCH_SEM_BACKEND", "postgres").strip().lower()
SEM_IVF = _release_root(os.environ.get("SEARCH_SEM_IVF", "/data/vectors/ivf2600"))
NOMIC_DIR = os.environ.get("SEARCH_NOMIC_DIR", "/models/nomic-embed-text-v1.5")
SEM_PROBES = max(1, min(int(os.environ.get("SEARCH_SEM_PROBES", "200")), 2600))
SEM_RELEASE = os.environ.get("SEARCH_RELEASE_ID") or None


def sem_ivf():
    """The IVF index + nomic encoder, loaded once; None under the Postgres backend. A configured IVF
    folder or model that will not load is an error, never a quiet fall back to wiki_vec."""
    if SEM_BACKEND == "postgres":
        return None
    if SEM_BACKEND != "ivf":
        raise ValueError(f"SEARCH_SEM_BACKEND={SEM_BACKEND!r}: want postgres or ivf")
    import sem_ivf as _si
    ix, enc = _si.load(SEM_IVF, NOMIC_DIR, SEM_RELEASE)
    return _si, ix, enc


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
            c = si.code_of(enc(q))
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
    si = sem_ivf()[0]
    pix = si.load_pages(path, SEM_RELEASE)
    code = query_code(q)
    with stage("sem-pages"):
        hits = pix.search(code, k, SEM_PAGES_PROBES)
    web = web_text()
    if not hasattr(web, "_page_url"):
        return []
    lk = web._lk()
    out = []
    for d, dist in hits:
        if dist > SEM_PAGES_MAX_BITS:
            continue
        url = web._page_url(lk, d)
        if url:
            out.append((url, lk.title(d) or url, "", 1 - dist / 768.0))
    return out


def wiki_text():
    """The configured Wikipedia text backend, opened once. A Tantivy index that will not
    open is an error, never a quiet fall back to Postgres (an A/B would compare A with A)."""
    global _wiki_text
    if _wiki_text is None:
        with _wiki_text_lock:
            if _wiki_text is None:
                if WIKI_BACKEND == "tantivy":
                    b = TantivyWikiText(WIKI_INDEX)
                    print(f"wiki text: tantivy index {WIKI_INDEX}, {b.num_docs} articles", flush=True)
                elif WIKI_BACKEND == "tantivy-release":
                    path = os.environ.get("SEARCH_WIKI_INDEX") or os.path.join(RELEASE_DIR, "text", "wiki")
                    b = TantivyReleaseWikiText(path)
                    print(f"wiki text: tantivy-release index {path}, {b.num_docs} articles", flush=True)
                elif WIKI_BACKEND == "postgres":
                    b = PgWikiText()
                else:
                    raise ValueError(f"SEARCH_WIKI_BACKEND={WIKI_BACKEND!r}: want postgres, tantivy or tantivy-release")
                _wiki_text = b
    return _wiki_text


def wiki_rows(q: str, k: int, stored: frozenset = frozenset()) -> list[tuple]:
    """(title, lead, bm25) rows of the Wikipedia text list, from whichever backend serves it."""
    return wiki_text().search(q, k, stored)


# The site, page and anchor lists (T2 step 2): one backend serves all four of them —
#   sites(q, k, hw, stored, bv)  -> SITES_SQL rows        (url, title, description, bm25, score)
#   pages(q, k, stored, bv)      -> PAGES_SQL rows        (url, title, description, bm25)
#   anchors(q, k)                -> ANCHORS_SQL rows      (url, title, description, weighted bm25)
#   exact_anchor(key)            -> EXACT_ANCHOR_SQL rows (domain, sources, url, title, description)
#   SEARCH_SITES_BACKEND=postgres | tantivy     SEARCH_SITES_INDEX=<dir with sites/ pages/ anchor_docs/ anchors/>
# The Tantivy indexes come from tools/sites-tantivy.py, page text only as page_words (ADR-059).
#   SEARCH_WEB_BACKEND=tantivy-release (the release's text/sites/{sites,pages,anchors}: doc_id only, titles, urls and
#   descriptions from the release lookups; SEARCH_SITES_INDEX defaults to <release>/text/sites) also selects the backend;
#   it wins over SEARCH_SITES_BACKEND when both are set.
SITES_BACKEND = (os.environ.get("SEARCH_WEB_BACKEND") or os.environ.get("SEARCH_SITES_BACKEND") or "postgres").strip().lower()
SITES_INDEX = _release_root(os.environ.get("SEARCH_SITES_INDEX", "/data/sites-tantivy"))
SITES_HEAD_N = 50          # SITES_SQL takes 50 from each of the head and full indexes
ANCHOR_DOCS_N = 20         # ANCHORS_SQL weighs the best 20 anchor documents


def popularity(tranco: int | None, cc_hc_pos: int | None) -> float:
    """SITES_SQL / ANCHORS_SQL's popularity factor (1 for an unknown site)."""
    return (1 + 0.15 * max(0.0, 14 - math.log(tranco or 2_000_000))
            + 0.10 * max(0.0, 18 - math.log(cc_hc_pos or 100_000_000)))


class PgWebText:
    """The site, page and anchor lists as they always were: pg_textsearch over docs,
    page_docs, anchor_docs and anchors."""
    name = "postgres"

    def sites(self, q, k, hw, stored=frozenset(), bv="full"):
        return _rows(with_text(_body_sql(SITES_SQL, "docs", bv), stored),
                     {"q": q, "k": k, "strong": STRONG_BM25, "hw": hw})

    def pages(self, q, k, stored=frozenset(), bv="full"):
        return _rows(with_text(_body_sql(PAGES_SQL, "page_docs", bv), stored), {"q": q, "k": k})

    def anchors(self, q, k):
        return _rows(ANCHORS_SQL, {"q": q, "k": k})

    def exact_anchor(self, key):
        return _rows(EXACT_ANCHOR_SQL, {"q": key})


class _TvIndex:
    """One Tantivy index, opened once; its searcher is shared by every request thread."""

    def __init__(self, tv, path: str):
        self.tv = tv
        self.index = _open_index(tv, path)
        try:                                # written by tools/sites-tantivy.py; absent = en_pg
            name = open(os.path.join(path, ANALYZER_FILE)).read().strip() or TANTIVY_ANALYZER
        except FileNotFoundError:
            name = TANTIVY_ANALYZER
        if name not in TANTIVY_ANALYZERS:
            raise ValueError(f"{path}: unknown analyzer {name!r}")
        self.analyzer_name = name
        self.analyzer = TantivyWikiText.build_analyzer(tv, name)
        self.index.register_tokenizer(name, self.analyzer)
        self.index.reload()
        self.searcher = self.index.searcher()
        self.schema = self.index.schema

    def bm25(self, field: str, terms: list[str], k: int, exclude: str | None = None) -> list:
        """[(score, stored doc)] for the terms OR-ed on one field, best first; `exclude`
        names an int field whose value 1 drops the document (WHERE error IS NULL)."""
        tv = self.tv
        parts = [(tv.Occur.Should, tv.Query.term_query(self.schema, field, t)) for t in terms]
        if exclude:
            parts.append((tv.Occur.MustNot, tv.Query.term_query(self.schema, exclude, 1)))
        return [(float(s), self.searcher.doc(a)) for s, a in self.searcher.search(tv.Query.boolean_query(parts), k).hits]


def _txt(d, field: str) -> str:
    return bytes(d[field][0]).decode("utf-8", "replace")


class TantivyWebText:
    """The same four lists from Tantivy indexes. BM25 statistics include the rows Postgres
    indexes but filters out (fetch errors), so document counts and lengths match its index;
    the popularity factor and head/full combination are SITES_SQL's, in Python."""
    name = "tantivy"

    def __init__(self, root: str):
        import tantivy
        self.tv = tantivy
        self.ix = {kind: _TvIndex(tantivy, os.path.join(root, kind))
                   for kind in ("sites", "pages", "anchor_docs", "anchors")}
        self.num_docs = {kind: ix.searcher.num_docs for kind, ix in self.ix.items()}
        self.analyzers = {kind: ix.analyzer_name for kind, ix in self.ix.items() if kind != "anchors"}

    def terms(self, q: str, kind: str = "sites") -> list[str]:
        """The query analysed the way index `kind` analysed its text."""
        seen: list[str] = []
        for t in self.ix[kind].analyzer.analyze(q):
            if t not in seen:
                seen.append(t)
        return seen

    def sites(self, q, k, hw, stored=frozenset(), bv="full"):
        terms = self.terms(q)
        if not terms:
            return []
        with stage("sites_tv"):
            ix = self.ix["sites"]
            hits: dict[str, list] = {}           # domain -> [head bm25, full bm25, doc]
            for slot, field in ((0, "head"), (1, "body")):
                for score, d in ix.bm25(field, terms, SITES_HEAD_N, exclude="err"):
                    h = hits.setdefault(d["domain"][0], [0.0, 0.0, d])
                    h[slot] = max(h[slot], score)
            rows = []
            for hb, fb, d in hits.values():
                bm25 = hw * hb + fb
                score = bm25 * popularity(d["tranco"][0], d["cchc"][0]) if bm25 >= STRONG_BM25 else bm25
                rows.append((_txt(d, "url"), _txt(d, "title"), _txt(d, "desc"), bm25, score))
            rows.sort(key=lambda r: r[4], reverse=True)
            return rows[:k]

    def pages(self, q, k, stored=frozenset(), bv="full"):
        terms = self.terms(q, "pages")
        if not terms:
            return []
        with stage("pages_tv"):
            return [(_txt(d, "url"), _txt(d, "title"), _txt(d, "desc"), score)
                    for score, d in self.ix["pages"].bm25("body", terms, k, exclude="err")]

    def anchors(self, q, k):
        terms = self.terms(q, "anchor_docs")
        if not terms:
            return []
        with stage("anchors_tv"):
            rows = [(_txt(d, "url"), _txt(d, "title"), _txt(d, "desc"),
                     score * popularity(d["tranco"][0], d["cchc"][0]))
                    for score, d in self.ix["anchor_docs"].bm25("body", terms, ANCHOR_DOCS_N)
                    if not d["nodoc"][0]]          # no fetched page: ANCHORS_SQL's join drops it
            rows.sort(key=lambda r: r[3], reverse=True)
            return rows[:k]

    def exact_anchor(self, key):
        tv = self.tv
        with stage("exact_anchor_tv"):
            ax = self.ix["anchors"]
            res = ax.searcher.search(tv.Query.term_query(ax.schema, "anchor", key), 2,
                                     order_by_field="sources", order=tv.Order.Desc)
            out = []
            sx = self.ix["sites"]
            for _s, a in res.hits:
                d = ax.searcher.doc(a)
                domain = d["domain"][0]
                site = sx.searcher.search(tv.Query.boolean_query([
                    (tv.Occur.Must, tv.Query.term_query(sx.schema, "domain", domain)),
                    (tv.Occur.MustNot, tv.Query.term_query(sx.schema, "err", 1))]), 1).hits
                if site:
                    s = sx.searcher.doc(site[0][1])
                    out.append((domain, d["sources"][0], _txt(s, "url"), _txt(s, "title"), _txt(s, "desc")))
            return out


class TantivyReleaseWebText:
    """The site, page and anchor lists from the release's own indexes (SEARCH-RELEASE-FORMAT, the web text
    indexes): text/sites/sites (head + body), pages (body), anchors (body), each holding only the registry's
    `doc_id`. A hit becomes a row through the release lookups, like TantivyReleaseWikiText: url and title from
    the registry (a page's url is its host origin + path), a site's description from lookup.sqlite, the
    popularity factor from signals.bin (tranco, cc_hc). Needs SEARCH_LOOKUP_BACKEND=release.
    What the release does not carry, said out loud rather than answered empty:
      - exact_anchor(): from anchor-phrases.sqlite (its own licence file); a release without it raises.
      - a page's description: pages ship words only (ADR-059), so a page row has an empty description.
      - the indexes hold only fetched sites/pages (error IS NULL), so no `err` / `nodoc` filter is needed."""
    name = "tantivy-release"
    KINDS = {"sites": "sites", "pages": "pages", "anchor_docs": "anchors"}      # list name -> folder under text/sites

    def __init__(self, root: str):
        import tantivy
        self.tv = tantivy
        self.ix = {kind: _TvIndex(tantivy, os.path.join(root, folder)) for kind, folder in self.KINDS.items()}
        self.num_docs = {kind: ix.searcher.num_docs for kind, ix in self.ix.items()}
        self.analyzers = {kind: ix.analyzer_name for kind, ix in self.ix.items()}

    @staticmethod
    def _lk():
        lk = lookups()
        if lk is None or lk.name != "release":
            raise RuntimeError("SEARCH_WEB_BACKEND=tantivy-release needs SEARCH_LOOKUP_BACKEND=release")
        return lk

    def terms(self, q: str, kind: str = "sites") -> list[str]:
        seen: list[str] = []
        for t in self.ix[kind].analyzer.analyze(q):
            if t not in seen:
                seen.append(t)
        return seen

    def _hits(self, kind: str, field: str, terms: list[str], k: int) -> list[tuple[float, int]]:
        """[(bm25, doc_id)] for the terms OR-ed on one field, best first."""
        ix, tv = self.ix[kind], self.tv
        res = ix.searcher.search(tv.Query.boolean_query(
            [(tv.Occur.Should, tv.Query.term_query(ix.schema, field, t)) for t in terms]), k).hits
        ids = ix.searcher.fast_field_values("doc_id", [a for _s, a in res]) if res else []
        return [(float(sc), int(d)) for (sc, _a), d in zip(res, ids)]

    @staticmethod
    def _site_row(lk, d: int):
        """(url, title, description, tranco, cc_hc) of a site doc_id, or None when the registry no longer knows it."""
        t, host, url, title = lk.reg(d)
        if t is None:
            return None
        return url, title or host, lk.site(d)[1] or "", lk.S(d, "tranco"), lk.S(d, "cc_hc")

    def sites(self, q, k, hw, stored=frozenset(), bv="full"):
        terms = self.terms(q)
        if not terms:
            return []
        lk = self._lk()
        with stage("sites_tv"):
            hits: dict[int, list] = {}                    # doc_id -> [head bm25, full bm25]
            for slot, field in ((0, "head"), (1, "body")):
                for score, d in self._hits("sites", field, terms, SITES_HEAD_N):
                    h = hits.setdefault(d, [0.0, 0.0])
                    h[slot] = max(h[slot], score)
            rows = []
            for d, (hb, fb) in hits.items():
                r = self._site_row(lk, d)
                if r is None:                             # a doc_id the registry no longer knows is skipped
                    continue
                bm25 = hw * hb + fb
                score = bm25 * popularity(r[3], r[4]) if bm25 >= STRONG_BM25 else bm25
                rows.append((r[0], r[1], r[2], bm25, score))
            rows.sort(key=lambda r: r[4], reverse=True)
            return rows[:k]

    def _page_url(self, lk, d: int):
        row = lk.db.execute("SELECT h.origin, r.path, r.alt FROM r.registry r LEFT JOIN r.hosts h ON h.id = r.host_id "
                            "WHERE r.doc_id = ?", (d,)).fetchone()
        if row is None:
            return None
        origin, path, alt = row
        return alt or ((origin or "") + (path or ""))

    def pages(self, q, k, stored=frozenset(), bv="full"):
        terms = self.terms(q, "pages")
        if not terms:
            return []
        lk = self._lk()
        with stage("pages_tv"):
            out = []
            for score, d in self._hits("pages", "body", terms, k):
                url = self._page_url(lk, d)
                if url:                                   # unknown to the registry: skipped
                    cr = lk.page_credits(d) if hasattr(lk, "page_credits") else None
                    # a Stack Exchange page carries its credits as a fifth element (se_credit)
                    out.append((url, lk.title(d) or url, "", score) + ((cr,) if cr else ()))
            return out

    def gov_state_word(self, state: str) -> str | None:
        """The analysed form of gov_state.state_token(state) when the pages index carries it (a release built with
        gov-states.json), else None: an older release's pages have no state word, and the state is then read
        from each hit's address. Asked of the index once per state."""
        cache = self.__dict__.setdefault("_gov_word", {})
        if state not in cache:
            ix, tv = self.ix["pages"], self.tv
            tt = self.terms(gov_state.state_token(state), "pages")
            cache[state] = (tt[0] if tt and ix.searcher.search(tv.Query.term_query(ix.schema, "body", tt[0]), 1).hits else None)
        return cache[state]

    def gov_share(self, rest: str) -> tuple[float, int]:
        """(share of the pages holding every word of `rest` that are government pages, number of states whose
        government pages hold them all), from the state words of a release built with gov-states.json; (0.0, 0)
        on an older release. A few term intersections, 1 to 6 ms on rc11's 718,213 pages."""
        terms = self.terms(rest, "pages")
        ix, tv = self.ix["pages"], self.tv
        words = [w for w in (self.gov_state_word(st) for st in sorted(set(gov_states().values()))) if w]
        if not terms or not words:
            return 0.0, 0
        must = [(tv.Occur.Must, tv.Query.term_query(ix.schema, "body", t)) for t in terms]
        total = ix.searcher.search(tv.Query.boolean_query(must), 1, count=True).count
        if not total:
            return 0.0, 0
        anygov = tv.Query.boolean_query([(tv.Occur.Should, tv.Query.term_query(ix.schema, "body", w)) for w in words])
        gov = ix.searcher.search(tv.Query.boolean_query(must + [(tv.Occur.Must, anygov)]), 1, count=True).count
        spread = 0
        for w in words:
            if ix.searcher.search(tv.Query.boolean_query(must + [(tv.Occur.Must, tv.Query.term_query(ix.schema, "body", w))]), 1).hits:
                spread += 1
                if spread >= GOV_CODE_STATES:
                    break
        return gov / total, spread

    def gov_base(self) -> float:
        """The share of all pages that are government pages: 12.7 % on rc11, 55.9 % on rc12 after the link crawl
       . Counted once."""
        cache = self.__dict__.setdefault("_gov_base", [])
        if not cache:
            ix, tv = self.ix["pages"], self.tv
            words = [w for w in (self.gov_state_word(st) for st in sorted(set(gov_states().values()))) if w]
            anygov = tv.Query.boolean_query([(tv.Occur.Should, tv.Query.term_query(ix.schema, "body", w)) for w in words])
            n = self.num_docs["pages"]
            cache.append(ix.searcher.search(anygov, 1, count=True).count / n if words and n else 0.0)
        return cache[0]

    def gov_pages(self, state: str, rest: str, weak: bool, k: int):
        """PAGES rows on `state`'s government sites for the query's other words. A release built with
        gov-states.json marks every government page's words with its state (gov_state.state_token): that word is
        then a Must term and the GOV_PAGES_POOL best hits are the state's own. Without it (an older release) the
        GOV_PAGES_DEPTH best pages are read and the state's kept by address. Either way: first the pages holding
        every word, then (not for a `weak` state, gov_state.py) pages holding some. Of up to GOV_PAGES_POOL such
        pages the ones whose title or address carries more of the words come first: the body is a bag of the
        page's words, and a page that mentions licences outscored the page that is about them."""
        terms = self.terms(rest, "pages")
        doms = gov_states()
        if not terms or not doms:
            return []
        lk = self._lk()
        ix, tv = self.ix["pages"], self.tv
        hint = [(tv.Occur.Should, tv.Query.term_query(ix.schema, "body", t))
                for t in self.terms(f"{STATE_NAME.get(state, '')} {state}", "pages") if t not in terms]
        word = self.gov_state_word(state)
        if word:
            hint.append((tv.Occur.Must, tv.Query.term_query(ix.schema, "body", word)))
        # a town's page must not push the state's out of the pool: read three pools' worth, the tier sort below picks
        pool_size = GOV_PAGES_POOL * GOV_PAGES_READ
        depth = pool_size if word else GOV_PAGES_DEPTH
        with stage("gov_pages_tv"):
            pool, seen = [], set()
            for occur in (tv.Occur.Must,) if weak else (tv.Occur.Must, tv.Occur.Should):
                def clause(t):
                    same = [x for w in GOV_SAME_WORDS.get(t, ()) for x in self.terms(w, "pages")]
                    q = tv.Query.term_query(ix.schema, "body", t)
                    return q if not same else tv.Query.boolean_query(
                        [(tv.Occur.Should, q)] + [(tv.Occur.Should, tv.Query.term_query(ix.schema, "body", x)) for x in same])
                res = ix.searcher.search(tv.Query.boolean_query([(occur, clause(t)) for t in terms] + hint), depth).hits
                ids = ix.searcher.fast_field_values("doc_id", [a for _s, a in res]) if res else []
                for (score, _a), d in zip(res, ids):
                    url = self._page_url(lk, int(d))
                    if not url or url in seen or gov_state.host_state(urlsplit(url).hostname or "", doms) != state:
                        continue
                    seen.add(url)
                    title = lk.title(int(d)) or url
                    named = set(self.terms(title + " " + re.sub(r"[\W_]+", " ", urlsplit(url).path), "pages"))
                    # every-word pages before some-word pages, the state's own before a town's or county's in each
                    # ("nj gun license" answered with Maplewood's police, 2026-11-01), then words in the title or
                    # address, then bm25
                    tier = (2 if occur is tv.Occur.Must else 0) + (0 if is_local_gov(urlsplit(url).hostname or "") else 1)
                    hit = sum(t in named or any(x in named for w in GOV_SAME_WORDS.get(t, ()) for x in self.terms(w, "pages"))
                              for t in terms)
                    pool.append((tier, hit - not_the_task(url, title, terms), float(score), url, title))
                    if len(pool) >= pool_size:
                        break
                if len(pool) >= pool_size:
                    break
            pool.sort(key=lambda r: r[:3], reverse=True)
            out = [(url, title, "", score) for _m, _n, score, url, title in pool[:k]]
            front = None if "govfront" in _drop.get() else agency_front(lk, rest, [(r[3], r[4]) for r in pool])
            if front:
                out = [(front[0], front[1], "", pool[0][2])] + [r for r in out if same_page(r[0]) != same_page(front[0])]
            return out[:k]

    def local_pages(self, domains: list[str], rest: str, k: int):
        """PAGES rows on these government sites (a town's and its county's) for the query's other words:
        the site words (gov_state.site_token) are a Must group, every word first, then some; title and address words
        before bm25, as gov_pages. [] on a release without site words."""
        terms = self.terms(rest, "pages")
        if not terms or not domains:
            return []
        ix, tv = self.ix["pages"], self.tv
        sites = []
        for dmn in domains:
            tt = self.terms(gov_state.site_token(dmn), "pages")
            if tt:
                sites.append((tv.Occur.Should, tv.Query.term_query(ix.schema, "body", tt[0])))
        if not sites:
            return []
        lk = self._lk()
        site_q = (tv.Occur.Must, tv.Query.boolean_query(sites))
        with stage("local_pages_tv"):
            pool, seen = [], set()
            for occur in (tv.Occur.Must, tv.Occur.Should):
                res = ix.searcher.search(tv.Query.boolean_query(
                    [(occur, tv.Query.term_query(ix.schema, "body", t)) for t in terms] + [site_q]), GOV_PAGES_POOL).hits
                ids = ix.searcher.fast_field_values("doc_id", [a for _s, a in res]) if res else []
                for (score, _a), d in zip(res, ids):
                    url = self._page_url(lk, int(d))
                    if not url or url in seen:
                        continue
                    seen.add(url)
                    title = lk.title(int(d)) or url
                    named = set(self.terms(title + " " + re.sub(r"[\W_]+", " ", urlsplit(url).path), "pages"))
                    pool.append((occur is tv.Occur.Must, sum(t in named for t in terms), float(score), url, title))
                if len(pool) >= GOV_PAGES_POOL:
                    break
            pool.sort(key=lambda r: r[:3], reverse=True)
            return [(url, title, "", score) for _m, _n, score, url, title in pool[:k]]

    def anchors(self, q, k):
        terms = self.terms(q, "anchor_docs")
        if not terms:
            return []
        lk = self._lk()
        with stage("anchors_tv"):
            rows = []
            for score, d in self._hits("anchor_docs", "body", terms, ANCHOR_DOCS_N):
                r = self._site_row(lk, d)                 # an anchor document is its site's doc_id
                if r is not None:
                    rows.append((r[0], r[1], r[2], score * popularity(r[3], r[4])))
            rows.sort(key=lambda r: r[3], reverse=True)
            return rows[:k]

    def exact_anchor(self, key):
        """EXACT_ANCHOR_SQL's rows (domain, sources, url, title, description) from anchor-phrases.sqlite: the
        phrase's sites by linking sites, url/title/description through the registry and lookup.sqlite."""
        lk = self._lk()
        with stage("exact_anchor_tv"):
            hits = lk.anchor_phrase(key)
            if hits is None:
                raise RuntimeError("SEARCH_WEB_BACKEND=tantivy-release has no exact-anchor lookup: the release does not carry "
                                   "anchor-phrases.sqlite (SEARCH-RELEASE-FORMAT). Refusing rather than answering empty; "
                                   "serve this list from SEARCH_SITES_BACKEND=tantivy or postgres")
            out = []
            for d, sources in hits:
                r = self._site_row(lk, d)
                if r is not None:                 # a site the registry no longer knows is skipped
                    out.append((lk.reg(d)[1], sources, r[0], r[1], r[2]))
            return out


_web_text = None


def web_text():
    """The configured site/page/anchor backend, opened once; a Tantivy index that will not
    open is an error, never a quiet fall back to Postgres."""
    global _web_text
    if _web_text is None:
        with _wiki_text_lock:
            if _web_text is None:
                if SITES_BACKEND == "tantivy":
                    b = TantivyWebText(SITES_INDEX)
                    print(f"site/page/anchor lists: tantivy {SITES_INDEX} {b.num_docs} {b.analyzers}", flush=True)
                elif SITES_BACKEND == "tantivy-release":
                    path = os.environ.get("SEARCH_SITES_INDEX") or os.path.join(RELEASE_DIR, "text", "sites")
                    b = TantivyReleaseWebText(path)
                    print(f"site/page/anchor lists: tantivy-release {path} {b.num_docs} {b.analyzers}", flush=True)
                elif SITES_BACKEND == "postgres":
                    b = PgWebText()
                else:
                    raise ValueError(f"SEARCH_WEB_BACKEND/SEARCH_SITES_BACKEND={SITES_BACKEND!r}: want postgres, tantivy or tantivy-release")
                _web_text = b
    return _web_text

# ---------------------------------------------------------------------------
# Semantic tier: the query is embedded on CPU with the model tools/embed.py used
# for the documents (bge-small-en-v1.5, ONNX, baked into the image) and matched against
# <name>_vec by cosine (HNSW). Its list is fused with the keyword lists by reciprocal
# rank (RRF), so no score scales are mixed. Missing model or table = the tier is off.
# ---------------------------------------------------------------------------
MODEL_DIR = os.environ.get("SEARCH_MODEL_DIR", "/models/bge-small-en-v1.5")
QUERY_PREFIX = "Represent this sentence for searching relevant passages: "   # bge v1.5 retrieval
RRF_K = int(os.environ.get("SEARCH_RRF_K", "60"))            # swept by tools/sq-sweep.py
# The pages list in the fusion (rc9). rc9 grew page_docs from 82,602 to 587,427 pages
# (Stack Exchange, Wikibooks, Wikivoyage, MedlinePlus): a topic query now gets five page hits
# that tie the article at ~49, the learned ranker leaves the article below the cross-encoder's
# eight and it never comes back ("what is copper" -> Copper (color); judged dev right@1
# 70.4% on rc8 -> 68.0%). The pages list's first PAGES_FUSE_K hits enter at weight PAGES_FUSE_W;
# a state's government pages are a list of their own at full weight. Measured on rc9 (judged dev
# 510, /search right@1): k=5 68.0%, k=2 70.2% (rc8 70.4%); a weight under 1 reaches 71.0% but loses 5 of the
# 43 government questions, so the default is k=2 at full weight. SEARCH_PAGES_FUSE_K=5 restores rc9's first build.
PAGES_FUSE_K = int(os.environ.get("SEARCH_PAGES_FUSE_K", "2"))
PAGES_FUSE_W = float(os.environ.get("SEARCH_PAGES_FUSE_W", "1"))
_embedder = None


class _Embedder:
    def __init__(self, model_dir: str):
        import numpy as np
        import onnxruntime as ort
        from tokenizers import Tokenizer
        ort.disable_telemetry_events()   # privacy: no usage events, no persisted device id
        self.np = np
        self.tok = Tokenizer.from_file(os.path.join(model_dir, "tokenizer.json"))
        self.tok.enable_truncation(max_length=128)
        opts = ort.SessionOptions()
        _ort_lean(opts)
        opts.intra_op_num_threads = 2
        self.sess = ort.InferenceSession(os.path.join(model_dir, "model.onnx"), opts,
                                         providers=["CPUExecutionProvider"])
        self.inputs = {i.name for i in self.sess.get_inputs()}

    def __call__(self, text: str, prefix: str = QUERY_PREFIX) -> list[float]:
        np = self.np
        e = self.tok.encode(prefix + text)
        feed = {"input_ids": np.array([e.ids], dtype=np.int64),
                "attention_mask": np.array([e.attention_mask], dtype=np.int64)}
        if "token_type_ids" in self.inputs:
            feed["token_type_ids"] = np.zeros_like(feed["input_ids"])
        cls = self.sess.run(None, feed)[0][0, 0]            # CLS pooling, as bge does
        return (cls / np.linalg.norm(cls)).tolist()

    def batch(self, texts: list[str], prefix: str = ""):
        """Unit vectors for several texts in one run (padded), same pooling as __call__."""
        np = self.np
        enc = [self.tok.encode(prefix + t) for t in texts]
        width = max(len(e.ids) for e in enc)
        ids = np.array([e.ids + [0] * (width - len(e.ids)) for e in enc], dtype=np.int64)
        mask = np.array([e.attention_mask + [0] * (width - len(e.ids)) for e in enc], dtype=np.int64)
        feed = {"input_ids": ids, "attention_mask": mask}
        if "token_type_ids" in self.inputs:
            feed["token_type_ids"] = np.zeros_like(ids)
        cls = self.sess.run(None, feed)[0][:, 0]
        return cls / np.linalg.norm(cls, axis=1, keepdims=True)


def embed(text: str, prefix: str = QUERY_PREFIX) -> list[float] | None:
    global _embedder
    if _embedder is None:
        try:
            _embedder = _Embedder(MODEL_DIR)
        except Exception:
            _embedder = False                               # no model in this image: tier off
    return _embedder(text, prefix) if _embedder else None


# News headlines by meaning (2026-09-30): one vector per item, cached by URL so a headline is
# embedded once however many searches match it (float16, bounded). 2026-11-04: 30,000 (~23 MB) so the
# background fill (news_vec_fill) can hold the whole NEWS_HOURS window (~20,000 headlines on public): a first
# news lookup for a popular name took 140-700 ms embedding its headlines, a repeat 41 ms.
_NEWS_VEC_MAX = int(os.environ.get("SEARCH_NEWS_VEC_MAX", "30000"))
_news_vecs: OrderedDict = OrderedDict()
_news_vec_lock = threading.Lock()


def news_vectors(items: list[tuple[str, str]]) -> dict | None:
    """[(url, "headline. summary")] -> {url: unit vector}; None when the model is off."""
    if embed("warm") is None:
        return None
    np = _embedder.np
    with _news_vec_lock:
        missing = [(u, t) for u, t in items if u not in _news_vecs]
    if missing:
        vs = _embedder.batch([t[:400] for _u, t in missing]).astype(np.float16)
        with _news_vec_lock:
            for (u, _t), v in zip(missing, vs):
                _news_vecs[u] = v
            while len(_news_vecs) > _NEWS_VEC_MAX:
                _news_vecs.popitem(last=False)
    with _news_vec_lock:
        return {u: _news_vecs[u] for u, _t in items if u in _news_vecs}


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


# Pages inside the top sites (page_docs, ccpages.py): a keyword list of their own in
# the fusion, so a site's help page can answer a how-to query.
PAGES_SQL = """
SELECT url, title, description, -(@pages_text@ <@> to_bm25query(%(q)s, 'page_docs_bm25')) AS bm25
FROM page_docs WHERE error IS NULL
ORDER BY @pages_text@ <@> to_bm25query(%(q)s, 'page_docs_bm25') LIMIT %(k)s
"""


def _body_sql(sql: str, table: str, v: str) -> str:
    """SITES_SQL / PAGES_SQL against body variant v's table, which has no error column
    (failed fetches were left out when it was built)."""
    if v == "full":
        return sql
    # a variant table always has its own search_text column: resolve the stored-text
    # placeholders (with_text) here, so the rewrites below see the column name
    sql = sql.replace("@docs_text@", "d.search_text").replace("@pages_text@", "search_text")
    alt = f"{table}_body_{v}"
    return (sql.replace(f"'{table}_bm25'", f"'{alt}_bm25'")
               .replace(f"FROM {table} d WHERE d.error IS NULL\n    ORDER BY d.search_text",
                        f"FROM {alt} d\n    ORDER BY d.search_text")
               .replace(f"FROM {table} WHERE error IS NULL", f"FROM {alt}"))

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
ACCOUNT_POPULAR = 15          # Wikidata sitelinks: known in 15+ language editions
ACCOUNT_LABEL = {"x": "X", "instagram": "Instagram", "facebook": "Facebook", "tiktok": "TikTok",
                 "linkedin": "LinkedIn", "linkedin-in": "LinkedIn", "spotify": "Spotify", "github": "GitHub",
                 "twitch": "Twitch", "imdb": "IMDb", "mastodon": "Mastodon", "bluesky": "Bluesky", "blog": "blog",
                 "rottentomatoes": "Rotten Tomatoes", "letterboxd": "Letterboxd", "tmdb-person": "TMDB",
                 "tmdb-movie": "TMDB", "tmdb-tv": "TMDB", "metacritic": "Metacritic", "applemusic": "Apple Music",
                 "genius": "Genius", "discogs": "Discogs", "allmusic": "AllMusic", "soundcloud": "SoundCloud",
                 "goodreads": "Goodreads", "gutenberg": "Project Gutenberg", "gutenberg-catalog": "Project Gutenberg",
                 "gutenberg-author": "Project Gutenberg",
                 "steam": "Steam", "espn-nfl": "ESPN", "nba": "NBA.com",
                 "basketball-ref": "Basketball Reference", "mlb": "MLB.com", "baseball-ref": "Baseball Reference",
                 "hockey-ref": "Hockey Reference", "transfermarkt": "Transfermarkt", "fbref": "FBref",
                 "youtube": "YouTube", "bioguide": "Congress", "govtrack": "GovTrack",
                 "ballotpedia": "Ballotpedia", "opensecrets": "OpenSecrets", "votesmart": "Vote Smart",
                 "fec": "FEC", "cspan": "C-SPAN", "ukparliament": "UK Parliament"}


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


# The accounts of the thing an exact name refers to, under its article (
# "paul rudd" gave "a bunch of pauls wikipedia pages" where the web gives Wikipedia,
# IMDb, Instagram). Keyed on the article, so a namesake never lends its accounts. One
# per platform (the shortest handle is the main account far more often than not): the
# first PROFILE_REF_N reference sites in PROFILE_REF order — which of them an item has
# says what it is (an actor has a TMDB person ID, a band a Discogs one) — then the first
# PROFILE_SOCIAL_N accounts, at most PROFILES_N in all. The official site already comes
# with the article. Project Gutenberg leads: where a book or its author has a page there,
# the text itself is free to read, and behind three film and music sites it would be cut.
PROFILE_REF = ["gutenberg", "gutenberg-catalog", "gutenberg-author",
               "imdb", "rottentomatoes", "tmdb-person", "letterboxd", "tmdb-movie", "tmdb-tv", "metacritic",
               "spotify", "applemusic", "genius", "allmusic", "discogs", "goodreads", "steam",
               "espn-nfl", "nba", "basketball-ref", "mlb", "baseball-ref", "hockey-ref", "transfermarkt", "fbref"]
PROFILE_SOCIAL = ["instagram", "x", "youtube", "facebook", "tiktok", "twitch", "soundcloud", "bluesky", "github"]
# The platform's own home page, for the card whose article IS the platform (platform_home; owner 2026-11-10: "YouTube
# card should have YouTube.com be the official link, same across all the sites that get a social media spot").
# Hand list, one line per platform, keyed on the Wikipedia article title (lower case): the Wikidata website (P856) is
# in wiki_sites but official_ok drops every _SOCIAL_HOSTS host, and a profile-path row (instagram.com/instagram) is
# not a home page. The PROFILE_SOCIAL platforms plus the other big ones.
PLATFORM_HOME = {
    "youtube": "https://www.youtube.com/", "facebook": "https://www.facebook.com/", "instagram": "https://www.instagram.com/",
    "twitter": "https://x.com/", "x (social network)": "https://x.com/", "tiktok": "https://www.tiktok.com/",
    "twitch": "https://www.twitch.tv/", "soundcloud": "https://soundcloud.com/", "bluesky": "https://bsky.app/",
    "github": "https://github.com/", "reddit": "https://www.reddit.com/", "reddit (website)": "https://www.reddit.com/", "linkedin": "https://www.linkedin.com/",
    "pinterest": "https://www.pinterest.com/", "snapchat": "https://www.snapchat.com/", "whatsapp": "https://www.whatsapp.com/",
    "telegram (software)": "https://telegram.org/", "threads (social network)": "https://www.threads.com/",
    "tumblr": "https://www.tumblr.com/", "discord": "https://discord.com/", "mastodon (social network)": "https://joinmastodon.org/",
    "mastodon (software)": "https://joinmastodon.org/", "telegram": "https://telegram.org/",
    "threads": "https://www.threads.com/", "mastodon": "https://joinmastodon.org/",
}
# Public office first: someone who holds or runs for office (or is a public figure the
# record covers) is looked up there before any film or music site.
PROFILE_PUBLIC = ["bioguide", "ballotpedia", "govtrack", "opensecrets", "votesmart", "fec", "ukparliament", "cspan"]
PROFILE_PLATFORMS = PROFILE_PUBLIC + PROFILE_REF + PROFILE_SOCIAL
PROFILE_REF_N, PROFILE_SOCIAL_N, PROFILES_N = 3, 3, 7

# A named thing's accounts in its card, not in the list (the owner's choice 2026-10-17: "fold them into the card").
# When the answer has a card, the accounts the card carries as links (its profile rows) leave the list: the list keeps
# sites and the article. No card: nothing leaves, so no account is lost. An account the query asks for ("nike instagram")
# is never a profile row and stays. With the switch on, Project Gutenberg goes last among the reference sites (a card
# row is short; it never takes the place of an official, public-office or other reference link).
#   SEARCH_ACCOUNTS_IN_CARD=0   turns the fold off; and per request ?aic=1|0 (A/B of two layouts on one engine).
#   Default ON since rc9 (owner, 2026-10-20): list-first right@1 +5.1 on the judged dev set, 0 open-set loss.
PROFILE_REF_GLAST = [p for p in PROFILE_REF if not p.startswith("gutenberg")] + [p for p in PROFILE_REF if p.startswith("gutenberg")]
ACCOUNTS_IN_CARD = os.environ.get("SEARCH_ACCOUNTS_IN_CARD", "1").strip().lower() not in ("0", "off", "false", "no")
_accounts_in_card: contextvars.ContextVar = contextvars.ContextVar("accounts_in_card", default=ACCOUNTS_IN_CARD)   # ?aic=


def parse_aic(v: str, default: bool) -> bool:
    """?aic= as written: "1" -> True, "0" -> False, anything else -> the default."""
    return {"1": True, "0": False}.get((v or "").strip(), default)


def fold_accounts(res: list[dict], card: dict | None) -> list[dict]:
    """The list without the profile rows the card carries as links. No card, or a card without links: res itself."""
    if not card or not card.get("links"):
        return res
    key = same_page(card.get("url") or "")
    carried = {l.get("url") for l in card["links"]}
    kept = [r for r in res if not (r.get("profile") and r.get("of") == key and r.get("url") in carried)]
    return kept if len(kept) != len(res) else res
PROFILE_PUBLIC_N, PROFILE_REF_WITH_PUBLIC = 3, 1
PROFILES_SQL = """
SELECT DISTINCT ON (platform) platform, url, name, description, value, sitelinks
FROM wiki_links WHERE wiki_title = %(t)s AND platform = ANY(%(p)s)
ORDER BY platform, length(value), value
"""


def pick_profiles(rows) -> list[tuple]:
    """One PROFILES_SQL row per platform -> the ones to show, in display order."""
    by = {r[0]: r for r in rows}
    pub = [by[p] for p in PROFILE_PUBLIC if p in by]
    ref = [by[p] for p in (PROFILE_REF_GLAST if _accounts_in_card.get() else PROFILE_REF) if p in by]
    soc = [by[p] for p in PROFILE_SOCIAL if p in by]
    n_ref = PROFILE_REF_WITH_PUBLIC if pub else PROFILE_REF_N
    picked = pub[:PROFILE_PUBLIC_N] + ref[:n_ref] + soc[:PROFILE_SOCIAL_N]
    extra = [r for r in pub[PROFILE_PUBLIC_N:] + ref[n_ref:] + soc[PROFILE_SOCIAL_N:]]
    return (picked + extra)[:PROFILES_N]


def profile_results(rows, top_score: float, of_url: str | None = None) -> list[dict]:
    """Rows of PROFILES_SQL -> account results just under the article (top_score), in
    order; of_url is the article's URL (profiles_follow_article keeps them under it)."""
    out = []
    for i, row in enumerate(pick_profiles(rows)):
        r = _account(*row, top_score - 0.1 * (i + 1))
        r["profile"] = True
        if of_url:
            r["of"] = same_page(of_url)
        out.append(r)
    return out


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


# How many of a named thing's own accounts keep their place under the article (the owner chose 2 on
# 2026-10-11, so that is the default; SEARCH_PROFILE_CAP=off is every answer as before). On rc8 with 2, accounts
# take 35.3% of head-brand's top-5 slots (48.6% without), 28 queries of 1,000 have three or more accounts in
# results 2-5 (690), and no query loses its expected site from the top 5. On the 1,000 head-brand queries (rc6,
# 2026-10-08) accounts took 2,398 of 4,995 top-5 slots (48%) and in 676 queries three or more of results 2-5
# were accounts: "starbucks" was its site, the article, then Instagram, X, YouTube, Facebook. With a cap the
# first N stay (pick_profiles order) and the rest go to the END of the same k results: still there, and
# card_of still finds them for the card's links. 0 keeps none under the article.
#   SEARCH_PROFILE_CAP=N | off   and per request ?pcap=N | off (A/B of two layouts on one engine)
# Only the appended block (profile: true) moves: an account the query asks for ("nike instagram", @handle,
# r/name) is never flagged profile, so it stays where it ranked.


def parse_profile_cap(v: str, default: int | None = None) -> int | None:
    """A cap as written: "2" -> 2, "off" -> None (no cap), anything else -> default."""
    v = (v or "").strip().lower()
    if v == "off":
        return None
    return int(v) if v.isdigit() and len(v) < 4 else default


PROFILE_CAP = parse_profile_cap(os.environ.get("SEARCH_PROFILE_CAP", "").strip() or "2", -1)
if PROFILE_CAP == -1:
    raise SystemExit(f"SEARCH_PROFILE_CAP={os.environ.get('SEARCH_PROFILE_CAP')!r}: use a number or 'off'")
_profile_cap: contextvars.ContextVar = contextvars.ContextVar("profile_cap", default=PROFILE_CAP)   # ?pcap=


def cap_profiles(res: list[dict], n: int | None) -> list[dict]:
    """At most n of each article's own accounts stay where they are; the others move to the end, in their
    order. n None: res itself, untouched."""
    if n is None:
        return res
    seen: dict = {}
    keep, late = [], []
    for r in res:
        if r.get("profile"):
            seen[r.get("of")] = seen.get(r.get("of"), 0) + 1
            if seen[r.get("of")] > n:
                late.append(r)
                continue
        keep.append(r)
    return keep + late if late else res


# What readers of the exact article open next (Wikipedia clickstream "link" rows, CC0,
# wiki_next): "paul rudd" -> Ant-Man, Clueless. A tier of its own just under the exact
# answer and its accounts, flagged related (the start page shows them apart).
RELATED_N = 4
RELATED_SCORE = WIKI_EXACT_SCORE - 10
RELATED_SQL = """
SELECT n.next_title, w.lead FROM wiki_next n JOIN wiki_docs w ON w.title = n.next_title
WHERE n.title = %(t)s ORDER BY n.clicks DESC, n.next_title LIMIT %(n)s
"""


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
FAMOUS_LEAD_SQL = "SELECT lead FROM wiki_docs WHERE title = %(t)s LIMIT 1"


# An organisation the query names exactly (2026-09-25: "chase", "wells fargo", "sofi"): an
# article with that exact name whose own site is the site the query names. Its site's rank
# is how people actually reach it — Chase Bank's article has 16k views a month against a
# namesake actress's 283k death spike, but chase.com is the 3,292nd site on the web.
ORG_SQL = """SELECT n.rank, w.title FROM wiki_aliases a JOIN wiki_sites s USING (page_id)
JOIN wiki_docs w USING (page_id)
JOIN nav_sites n ON n.key = %(k)s AND n.domain = s.domain WHERE a.norm = %(k)s ORDER BY n.rank LIMIT 1"""
# No article is named just the query ("cvs"; the article is "CVS Pharmacy"): the top site
# the query names, and the article whose own site that is and whose title starts with the
# query — not every article that lists the site (MinuteClinic).
ORG_BY_SITE_SQL = """SELECT n.rank, w.title FROM nav_sites n JOIN wiki_sites s ON s.domain = n.domain
JOIN wiki_docs w USING (page_id)
WHERE n.key = %(k)s AND n.rank <= %(top)s
  AND regexp_replace(lower(w.title), '[^a-z0-9]', '', 'g') LIKE %(k)s || '%%'
ORDER BY n.rank, length(w.title) LIMIT 5"""
# ... and the title is the query plus a company word at most: "CVS Pharmacy", "Shell plc" —
# not "Sun Microsystems" for "sun" (sun.com is Oracle's) nor "Apple M1".
_ORG_WORD = re.compile(r"^(?:pharmacy|plc|inc|corporation|corp|company|co|group|bank|stores?|restaurants?|"
                       r"airlines?|holdings|ltd|llc|international|brands|foods|markets?|supermarkets?)$")


def org_title_ok(title: str, nq: str) -> bool:
    words = re.sub(r"\(.*?\)", "", title).lower().replace(".", "").replace(",", "").split()
    for i in range(1, len(words) + 1):
        if norm_key(" ".join(words[:i])) == nq:
            return all(_ORG_WORD.match(w) for w in words[i:])
    return False


def org_lookup(c, nq: str) -> tuple:
    """(rank, article title) of the organisation the query names, or (None, None)."""
    hit = c.execute(pop_sql(ORG_SQL), {"k": nq}).fetchone()
    if hit:
        return hit
    for rank, title in c.execute(pop_sql(ORG_BY_SITE_SQL), {"k": nq, "top": CONFIDENT_RANK}).fetchall():
        if org_title_ok(title, nq):
            return rank, title
    return None, None


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


def org_blocks_namesake(org_rank, famous: str | None = None, org_lead: str | None = None) -> bool:
    """A famous namesake does not take a query that names a top site's organisation ("chase" is Chase Bank, not Daveigh
    Chase) -- unless the organisation is the person's own: its article's lead names them (2026-11-14: "trump" gave the
    Trump Organization's card, "privately owned by Donald Trump", and the $Trump meme coin's site first, once trump.com
    counted as a top site). Measured that day: no demand figure for either company's article, and their sites' ranks
    2,470 (chase.com) and 2,506 (trump.com), so neither tells the two apart; the lead does."""
    if org_rank is None or org_rank > CONFIDENT_RANK:
        return False
    if famous and org_lead:
        name = re.sub(r"\s*\(.*\)$", "", famous)
        if name and name.lower() in org_lead.lower():
            return False
    return True


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


def major_team(key: str, rows) -> str | None:
    """MAJOR_TEAM_SQL rows -> the team article the typed nickname names: the query must be
    the END of the team's name ("devils" / "new jersey devils", not "new jersey"), most
    searched first."""
    for wiki, name, _d in rows:
        words = re.sub(r"\s*\(.*\)$", "", name).split()
        if any(norm_key(" ".join(words[i:])) == key for i in range(len(words))):
            return wiki
    return None


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

# Product rows (follow-up, 2026-11-10; both off by default so an eval arm can measure each alone).
#   SEARCH_PRODUCT_SITE_FIRST  1: "777x" -> boeing.com/commercial/777x, the article "Boeing 777X" right under it. The
#                                 query is the bare tail of a multi-word article title and the site is that article's own
#                                 official row with the maker's name in its host. Only the article's rank changes.
#                              2: also above an UNPOPULAR domain named like the query ("chrome" -> google.com/chrome
#                                 over chrome.com). A popular name-match site (windows.net) keeps its place.
#   SEARCH_PRODUCT_TIER_WIDE   1: the product tier also answers a 2-3 word query ("3 series", "galaxy note"), and the
#                                 maker may be a part of a host label ("suzuki" in globalsuzuki.com) when it is at least
#                                 SEARCH_PRODUCT_MAKER_MIN letters, so "vw" never pairs by substring.
# Measured offline on the 211 dev rows of the bare-product set (cached top-10 lists re-ordered, 2026-11-10): the swap
# alone 30 -> 70 first places with no other open set changing; without the maker-in-host guard it also lost 2
# wiki-redirect rows ("l40sx", "wylbur") and, as a blanket swap of every article above its site, 68 of them.
PRODUCT_SITE_FIRST = os.environ.get("SEARCH_PRODUCT_SITE_FIRST", "0").strip()
if PRODUCT_SITE_FIRST not in ("0", "1", "2"):
    raise SystemExit(f"SEARCH_PRODUCT_SITE_FIRST={PRODUCT_SITE_FIRST!r}: use 0, 1 or 2")
PRODUCT_TIER_WIDE = os.environ.get("SEARCH_PRODUCT_TIER_WIDE", "0").strip() == "1"
PRODUCT_MAKER_MIN = int(os.environ.get("SEARCH_PRODUCT_MAKER_MIN", "4"))
_OFFICIAL_SUFFIX = " — official site"
_WIKI_SUFFIX = " — Wikipedia"


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
_pt: contextvars.ContextVar = contextvars.ContextVar("pt", default=None)     # eval A/B only (pt=0 / 1)
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
# Two things the ordering lacked (2026-10-10, rc6):
# - a site's votes were SUMMED over the kind names a query matched, and one Wikidata class comes under several names
#   ("breaking news", "latest news", "news update", "news report", "current event", "commentary"): a site of that one
#   class got 5 or 6 votes, bbc.com 1. The most votes under any one name is the count of independent sources.
# - no site rank: the Tranco term left with Tranco and nothing took its place (the Common Crawl position is missing
#   for most sites and "only nudges"). Our own rank now has that term's weight. Over the 11,095 kinds with a list the
#   median own rank of the three listed sites goes from 94,151 to 68,843.
KIND_RANK_W_OPEN = float(os.environ.get("SEARCH_KIND_RANK_W_OPEN", "0.15"))
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
_kvec = (None, None, None)          # (generation, keys, unit-row matrix)
_kvec_lock = threading.Lock()


KIND_VECS_SQL = "SELECT key, v FROM kind_vecs"
KIND_KEY_EXACT_SQL = "SELECT key FROM kind_keys WHERE key = %(k)s AND n >= %(m)s"


def kind_vectors(c):
    global _kvec
    gen = generation()
    if _kvec[0] != gen or _kvec[1] is None:
        with _kvec_lock:
            if _kvec[0] != gen or _kvec[1] is None:
                try:
                    import numpy as np
                    rows = c.execute(KIND_VECS_SQL).fetchall()
                    _kvec = (gen, [r[0] for r in rows], np.array([r[1] for r in rows], dtype=np.float32))
                except Exception as e:
                    print(f"kind vectors: off ({type(e).__name__}: {e})", flush=True)
                    _kvec = (gen, [], None)
    return _kvec[1], _kvec[2]


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

def related_results(rows) -> list[dict]:
    out = []
    for i, (title, lead) in enumerate(rows):
        r = _wiki(title, lead, RELATED_SCORE - i)
        r["related"] = True
        out.append(r)
    return out


# The card over an exact answer (start page, 2026-09-25): what the thing is, from its
# Wikidata description (suggest_items.descr), its lead, and its accounts and related
# articles as links — built from the results already found, one lookup more.
CARD_DESCR_SQL = "SELECT descr FROM suggest_items WHERE wiki = %(t)s AND descr IS NOT NULL ORDER BY demand DESC NULLS LAST LIMIT 1"
# Its own stored official site (wiki_sites, the table WIKI_EXACT_SITE_SQL joins), for the card whose result list holds no
# official row of it ("claude": claude.ai is a plain site row and the exact-title path found another article).
CARD_SITE_SQL = ("SELECT ws.url FROM wiki_sites ws JOIN wiki_docs w USING (page_id) "
                 "WHERE w.title = %(t)s AND ws.url IS NOT NULL LIMIT 1")
# Its picture (2026-09-29, importer/card_images.py): free-licensed Commons files only, stored
# as small WebP and sent inline, with the credit the licence asks for.
CARD_IMAGE_SQL = "SELECT img, w, h, author, licence, licence_url, file FROM card_images WHERE title = %(t)s"


def card_image(row) -> dict | None:
    """A card_images row -> the card's picture: a data: URI, its size, and the credit
    (author, licence + deed, the file's Commons page)."""
    if not row:
        return None
    img, w, h, author, licence, licence_url, file = row
    out = {"src": "data:image/webp;base64," + base64.b64encode(bytes(img)).decode("ascii"), "w": w, "h": h,
           "licence": licence, "source": "https://commons.wikimedia.org/wiki/File:" + quote(file.replace(" ", "_"))}
    if author:
        out["author"] = author
    if licence_url:
        out["licence_url"] = licence_url
    return out


CARD_WITHIN = 3
# What the lead keeps of stripped pronunciation markup: "Roblox (, ) is", "The Eiffel Tower ( ; )
# is" (card UI audit, 2026-09-30).
_EMPTY_PARENS = re.compile(r"\s*\(\s*[,;:\s]*\)")


def clean_lead(text: str) -> str:
    """A lead for the card without empty brackets and the spacing they leave."""
    return re.sub(r"\s+([,;.])", r"\1", _EMPTY_PARENS.sub("", text or "")).strip()


def _label(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower().removeprefix("www.")
    return host.split(".")[0]


def card_article(res: list[dict]) -> dict | None:
    """The exact-answer article the card is about: the first one in the top CARD_WITHIN,
    not counting the top site's sisters: on other endings (starbucks.com.cn, starbucks.co.jp
    pushed Starbucks' article to 4th, 2026-09-25) and under its own name (plus.cnn.com and
    cnnespanol.cnn.com pushed CNN's to 4th: "cnn" had no card, 2026-10-10)."""
    top = _label(res[0]["url"]) if res and res[0].get("source") == "site" else None
    under = "." + _domain_of(res[0]["url"]) if top else None
    sister = lambda r: r.get("source") == "site" and (_label(r["url"]) == top or _domain_of(r["url"]).endswith(under))  # noqa: E731
    # nearby branches are the Nearby box, not rows: "Ikea" with an area put three IKEA stores between the official site
    # and the article, which then sat 5th and the card was lost (2026-11-06)
    window = [r for i, r in enumerate(res) if not (i and top and sister(r)) and r.get("source") != "place"]
    return next((r for r in window[:CARD_WITHIN] if r.get("source") == "wikipedia"
                 and (r["score"] >= WIKI_EXACT_SCORE or r.get("respelled"))), None)


# A misspelled name gets its card (2026-11-06, owner: "on misspellings we should be hitting the correct card"):
# "sarah j mass" is not respelled (every word is a word), so no exact article answers it, yet the list leads with
# "Sarah J. Maas — Wikipedia" and sarahjmaas.com. The article counts as the card's when its title is the query
# word for word with each word at most one letter off (a swap counts once), and the name's own site (its words
# joined, "sarahjmaas") is among the first six rows (the engine's order: sarahjmaas.com sat 4th). "mass" alone is
# one word against three: never.
_RW = re.compile(r"[a-z0-9]+")


def _name_words(s: str) -> list[str]:
    return _RW.findall(unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower())


def _one_off(a: str, b: str) -> bool:
    """a and b equal, or one edit apart (a letter changed, added, dropped, or two neighbours swapped)."""
    if a == b:
        return True
    if abs(len(a) - len(b)) > 1 or min(len(a), len(b)) < 3:
        return False
    if len(a) == len(b):
        d = [i for i in range(len(a)) if a[i] != b[i]]
        return len(d) == 1 or (len(d) == 2 and d[1] == d[0] + 1 and a[d[0]] == b[d[1]] and a[d[1]] == b[d[0]])
    s, t = (a, b) if len(a) < len(b) else (b, a)
    return any(t[:i] + t[i + 1:] == s for i in range(len(t)))


def respells_name(q: str, title: str) -> bool:
    """title (its "(…)" dropped) is q word for word, each word at most one edit off, at least one differing."""
    qw, tw = _name_words(q), _name_words(re.sub(r"\s*\([^)]*\)\s*$", "", title))
    return len(qw) >= 2 and len(qw) == len(tw) and qw != tw and all(_one_off(a, b) for a, b in zip(qw, tw))


def mark_respelled(q: str, res: list[dict]) -> None:
    """Marks the first or second row as the card's article when it respells q and the name's own site is in the
    first six rows (card_article then takes it)."""
    for r in res[:2]:
        if r.get("source") != "wikipedia" or r["score"] >= WIKI_EXACT_SCORE:
            continue
        title = r["title"].rsplit(" — Wikipedia", 1)[0]
        if not respells_name(q, title):
            continue
        joined = "".join(_name_words(re.sub(r"\s*\([^)]*\)\s*$", "", title)))
        if any(x.get("source") == "site" and _label(x["url"]) == joined for x in res[:6]):
            r["respelled"] = True
        return


# The site tree under the card's official site (2026-11-05, owner: "a site tree type of thing that has links to where
# important parts of the site might be useful"): a few of the site's own sections (Tickets, Shop, Menu, Locations,
# News, Support, Contact ...), picked from its home page's navigation by tools/site-nav.py at build time. Nothing is
# fetched at search time. SEARCH_SITENAV: that tool's sitenav.jsonl; unset or missing, cards have no tree.
SITENAV_PATH = os.environ.get("SEARCH_SITENAV", "").strip()
SITENAV_MAX = 6
_sitenav: dict[str, list[dict]] | None = None
_sitenav_lock = threading.Lock()


def _host_key(url: str) -> str:
    h = (urlsplit(url or "").hostname or "").lower()
    return h[4:] if h.startswith("www.") else h


def sitenav() -> dict[str, list[dict]]:
    """{site host (no www.): [{title, url}, ...]}, read once."""
    global _sitenav
    if _sitenav is None:
        with _sitenav_lock:
            if _sitenav is None:
                got: dict[str, list[dict]] = {}
                try:
                    with open(SITENAV_PATH, encoding="utf-8") as f:
                        for line in f:
                            r = json.loads(line)
                            links = [{"title": x["text"], "url": x["url"], **({"section": x["section"]} if x.get("section") else {})}
                                     for x in r.get("links") or []
                                     if x.get("text") and str(x.get("url", "")).startswith("https://")]
                            if links:
                                got[r["site"]] = links[:SITENAV_MAX]
                except (OSError, ValueError, KeyError) as e:
                    if SITENAV_PATH:
                        print(f"site tree {SITENAV_PATH}: not read ({type(e).__name__})", flush=True)
                _sitenav = got
                if got:
                    print(f"site tree {SITENAV_PATH}: {len(got)} sites", flush=True)
    return _sitenav


# "nike returns", "chase login", "sony wh-1000xm5" (site_slots.py): a search that is a site's name plus
# words is answered by lookup. SEARCH_SITE_SLOTS=<site-slots.jsonl> (tools/site-slots.py build) turns on the slot row: the
# page of the site's own tree that the leftover words ask for (login, returns, support, pricing ...), under the exact rows.
# SEARCH_SITE_BRIDGE=<site-search.jsonl> (tools/site-slots.py search-table) turns on the "search on <site>" row (bridge: true)
# when no slot matched. Both unset = off. Neither is in result_store.NOT_RESULTS: they are in the results fingerprint.
SITE_SLOTS_PATH = os.environ.get("SEARCH_SITE_SLOTS", "").strip()
SITE_BRIDGE_PATH = os.environ.get("SEARCH_SITE_BRIDGE", "").strip()
SITE_SLOTS_LEAD = os.environ.get("SEARCH_SITE_SLOTS_LEAD", "").strip().lower() in ("1", "on", "true", "yes")   # the slot row first, not under the entity's own row
_site_tables: dict[str, object] = {}
_site_tables_lock = threading.Lock()


def _site_table(kind: str):
    """The slot table or the site-search table, read once; None when unset or unreadable (the feature stays off).
    site_slots is imported here, so with both settings unset the module is never loaded."""
    path = SITE_SLOTS_PATH if kind == "slots" else SITE_BRIDGE_PATH
    if not path:
        return None
    with _site_tables_lock:
        if kind not in _site_tables:
            import site_slots
            try:
                t = (site_slots.load_slots if kind == "slots" else site_slots.load_search)(path)
                print(f"site {kind} {path}: {len(t)} sites", flush=True)
            except (OSError, ValueError) as e:
                print(f"site {kind} {path}: not read ({type(e).__name__})", flush=True)
                t = None
            _site_tables[kind] = t
    return _site_tables[kind]


def _official_hosts_of_name(name_key: str) -> list[str]:
    """The official sites of the articles a name is an alias of (tier 2's own lookup, WIKI_EXACT_SITE_SQL): the engine's
    answer to "which site does this name mean" for names whose domain is not the name. [] without the table or on error."""
    try:
        with _conn() as c:
            if "wiki_sites" not in tables(c):
                return []
            rows = c.execute(pop_sql(WIKI_EXACT_SITE_SQL), {"nq": name_key}).fetchall()
        return [_host_key(r[2]) for r in rows if r[2]]
    except Exception:  # noqa: BLE001 - extra: never fail the answer
        return []


def site_intent_rows(q: str, res: list[dict], card: dict | None) -> list[dict]:
    """The shown list with the slot row, else the bridge row, when the settings are on and the search is a name + words."""
    if not (SITE_SLOTS_PATH or SITE_BRIDGE_PATH) or not res:
        return res
    import site_slots
    out = site_slots.apply_slots(q, res, card, _site_table("slots"), _official_hosts_of_name, SITE_SLOTS_LEAD)
    if out is res:
        out = site_slots.apply_bridge(q, res, card, _site_table("bridge"), _official_hosts_of_name)
    return out


# Every link Wikidata records for a topic (2026-11-06, owner: "If it's legal and ok license wise to use the stuff from
# Wikidata we should be using all of it"; Wikidata is CC0, and these are links only). tools/card-links.py builds
# SEARCH_CARDLINKS (sqlite: links(title, p, url), fields(p, label, rank)); read per card, by its English Wikipedia
# title. Accounts and fan communities join the card's marks; the rest go under "More links".
CARDLINKS_PATH = os.environ.get("SEARCH_CARDLINKS", "").strip()
CARDLINKS_MAIN = frozenset(("Reddit", "Fandom", "X", "Instagram", "Facebook", "YouTube", "TikTok", "Threads", "Bluesky",
                            "Mastodon", "LinkedIn", "GitHub"))
CARDLINKS_MORE = 16
# a film or music catalogue only for what it catalogues (account_fits): its label as one of those platforms
_CARDLINK_KIND = {**{k: "imdb" for k in ("IMDb", "Letterboxd", "TMDB", "Trakt", "Rotten Tomatoes", "Metacritic", "TheTVDB",
                                         "LUMIERE", "MovieLens", "FilmAffinity")},
                  **{k: "discogs" for k in ("MusicBrainz", "MusicBrainz release group", "Discogs", "AllMusic", "Songkick",
                                            "Genius", "Spotify", "Apple Music", "SoundCloud", "Last.fm", "Bandcamp", "Deezer")}}
_cardlinks_db = threading.local()


def card_links(title: str) -> list[tuple[str, str]]:
    """[(label, url)] for the topic, in the fields' order (most used first); [] without the file."""
    if not CARDLINKS_PATH or not title:
        return []
    import sqlite3
    db = getattr(_cardlinks_db, "db", None)
    if db is None:
        try:
            db = sqlite3.connect(f"file:{CARDLINKS_PATH}?mode=ro", uri=True, check_same_thread=False)
            _cardlinks_db.db = db
        except sqlite3.Error:
            return []
    try:
        return db.execute("SELECT f.label, l.url FROM links l JOIN fields f ON f.p = l.p WHERE l.title = ? ORDER BY f.rank",
                          (title,)).fetchall()
    except sqlite3.Error:
        return []


def site_tree(url: str) -> list[dict]:
    """The section links of the site at url (its own host, or its www./bare twin), [] if none."""
    return sitenav().get(_host_key(url), []) if SITENAV_PATH else []


# The card gate (2026-11-05, owner: "only show things that are relevant underneath with high certainty"): under a
# card, a result after the first is shown only when a small model of the engine's own scores says it is likely
# relevant. Fitted on 2026-11-05 to 11.5k pairs of the stored searches (positions 2-5) that clef-flash and Qwen3-14B
# both labelled sure (both < 0.2 or both > 0.8); the exclude.tsv names left out; split by search. Held out, at
# 0.5: 19% of those rows kept, their mean label 60% (all rows: 27%); 57% of card searches keep nothing under the
# first. Features as served: reranker (ce), learned ranker (buildbox), the row's score, the share of query words in
# the title and in the host, article / site, position; ce and buildbox absent count as such. Off by default
# (SEARCH_CARD_GATE=0); ?cg=<threshold> per request for an A/B. The first row and places are never gated.
CARD_GATE = float(os.environ.get("SEARCH_CARD_GATE", "0") or 0)
_card_gate: contextvars.ContextVar = contextvars.ContextVar("card_gate", default=CARD_GATE)
_CG_W = (-3.4722, 0.8172, 0.3326, 0.6411, 1.6392, 0.2992, 0.7690, -0.6049, -0.0491, -0.4546, 0.5260)
_CG_MU = (0.0000, -2.8688, 0.0739, 91.1752, 0.2927, 0.0555, 0.2752, 0.7219, 2.5821, 0.3541, 0.5306)
_CG_SD = (1.0000, 3.2312, 0.1256, 199.0217, 0.3229, 0.1785, 0.4466, 0.4480, 1.1108, 0.4783, 0.4991)
_CG_STOP = frozenset("the a an of and in on for to de la le el s".split())


def _cg_toks(s: str) -> set[str]:
    return {t for t in re.findall(r"[\w']+", (s or "").lower()) if t not in _CG_STOP}


def card_gate_p(q: str, r: dict, pos: int) -> float:
    """How likely the gate's model finds row pos (0-based) relevant to q."""
    qt = _cg_toks(q)
    host = (urlsplit(r.get("url") or "").hostname or "")
    ce, lr = r.get("ce"), r.get("lr")
    x = [1.0, float(ce or 0.0), float(lr or 0.0), float(r.get("score") or 0.0),
         len(qt & _cg_toks(r.get("title") or "")) / max(len(qt), 1),
         len(qt & _cg_toks(re.sub(r"[./-]", " ", host))) / max(len(qt), 1),
         float(r.get("source") == "wikipedia"), float(r.get("source") == "site"), float(pos),
         float(ce is None), float(lr is None)]
    z = _CG_W[0] + sum(w * (v - m) / d for w, v, m, d in zip(_CG_W[1:], x[1:], _CG_MU[1:], _CG_SD[1:]))
    return 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, z))))


def card_gate(q: str, res: list[dict], th: float | None = None) -> list[dict]:
    """Under a card: the first row, places, and the rows the gate's model is sure of."""
    th = _card_gate.get() if th is None else th
    if th <= 0 or len(res) < 2:
        return res
    return res[:1] + [r for i, r in enumerate(res[1:], 1) if r.get("source") == "place" or card_gate_p(q, r, i) >= th]


# Accounts a card shows (2026-11-06): music and film catalogues only for what they catalogue. "IKEA" had Discogs and
# SoundCloud, "West Ham United" IMDb. Judged from the entity's Wikidata short description; none known, all stay.
_MUSIC_PLATFORMS = frozenset(("discogs", "soundcloud", "spotify", "applemusic", "allmusic", "genius"))
_FILM_PLATFORMS = frozenset(("imdb", "letterboxd", "rottentomatoes", "metacritic", "tmdb-person", "tmdb-movie", "tmdb-tv"))
_MUSIC_WORDS = re.compile(r"\b(singer|songwriter|band|musician|rapper|composer|dj|music|musical|record label|album|song|"
                          r"orchestra|choir|duo|trio|quartet|vocalist|guitarist|drummer|pianist|producer)\b", re.I)
_FILM_WORDS = re.compile(r"\b(actor|actress|film|films|movie|director|television|tv|series|sitcom|comedian|presenter|"
                         r"screenwriter|producer|studio|animator|animated|voice|documentary|show)\b", re.I)


def account_fits(platform: str, what: str) -> bool:
    if not what:
        return True
    if platform in _MUSIC_PLATFORMS:
        return bool(_MUSIC_WORDS.search(what))
    if platform in _FILM_PLATFORMS:
        return bool(_FILM_WORDS.search(what))
    return True


_PLATFORM_IS = re.compile(r"\b(social (media|network)|video[- ]sharing|video platform|messaging|instant messag|microblog|"
                          r"streaming (service|platform)|online (platform|service|community)|photo[- ]sharing|"
                          r"image[- ]sharing|code hosting|hosting service|social news|networking (service|site|platform)|"
                          r"voip|chat (app|platform)|mobile app)", re.I)
_PLATFORM_NOT = re.compile(r"\b(film|novel|album|band|song|newspaper|magazine|letter|concept|"
                           r"television|tv series|play|poem|disambiguation)\b", re.I)


def platform_home(title: str, descr: str | None, lead: str | None) -> list[dict]:
    """The card's official site when the article IS a social platform (PLATFORM_HOME): the article title is on the
    list, and its Wikidata description or lead's first 200 characters call it a platform and not a film, a newspaper,
    a letter. The card carries no Wikidata id, so the title and this wording stand in for it."""
    url = PLATFORM_HOME.get((title or "").strip().lower())
    if not url:
        return []
    for text in (descr, (lead or "")[:200]):
        if text and _PLATFORM_IS.search(text) and not _PLATFORM_NOT.search(text):
            return [{"title": "Official site", "url": url}]
    return []


def card_of(res: list[dict], descr: str | None = None, related: list[dict] | None = None,
            image: dict | None = None, site: str | None = None) -> dict | None:
    """An exact article among the first CARD_WITHIN results -> {title, url, description,
    lead, links: its official site then its accounts, related: readers' next articles,
    image: its picture + credit (card_image)}; else None. ("taylor swift" leads with her official site, the article second.)"""
    top = card_article(res)
    if top is None:
        return None
    title = top["title"].rsplit(" — Wikipedia", 1)[0]
    key = same_page(top["url"])
    # its official site wherever it ranked: donaldjtrump.com sat 4th and never reached the card
    official = [{"title": "Official site", "url": r["url"]} for r in res
                if r.get("official") and r["title"].startswith(title + " — ")][:1]
    if not official and site and official_ok(site) and not official_inner_page(site):
        official = [{"title": "Official site", "url": site}]       # the article's own stored site (CARD_SITE_SQL)
    if not official:
        official = platform_home(title, descr, top.get("content"))     # a platform whose own site is a social host
    mine = [r for r in res if r.get("profile") and r.get("of") == key]
    # what it is, in one line (2026-11-06): its Wikidata short description, which every account row of it carries
    what = next((r["content"].strip() for r in mine if (r.get("content") or "").strip()), "")
    links = official + [{"title": ACCOUNT_LABEL.get(r.get("platform", ""), r["title"].rsplit(" on ", 1)[-1].split(" (@")[0]),
                         "url": r["url"]} for r in mine if account_fits(r.get("platform", ""), what)]
    # related: looked up for the card (the top-8 cut and the rerankers keep only some of
    # them in res — "paul rudd" showed 1 of 4); else whichever made it into res
    if related is None:
        related = [r for r in res if r.get("related")]
    related = [{"title": r["title"].rsplit(" — Wikipedia", 1)[0], "url": r["url"]} for r in related]
    more = []
    if CARDLINKS_PATH:                                   # Wikidata's other links for it (card_links)
        have = {_host_key(l["url"]) for l in links} | {l["title"] for l in links}
        for label, url in card_links(title):
            h = _host_key(url)
            kind = _CARDLINK_KIND.get(label, "")
            # a film or music catalogue only for what it catalogues; unknown (no description) = left out
            if h in have or label in have or (kind and not (descr or what)) or not account_fits(kind, descr or what):
                continue
            have |= {h, label}
            if label in CARDLINKS_MAIN:
                links.append({"title": label, "url": url})
            elif len(more) < CARDLINKS_MORE:
                more.append({"title": label, "url": url})
    card = {"title": title, "url": top["url"], "lead": clean_lead(top.get("content", "")), "links": links, "related": related}
    if more:
        card["more_links"] = more
    if official and (tree := site_tree(official[0]["url"])):
        card["nav"] = tree
    if descr or what:
        card["description"] = (descr or what)[:120]
    if image:
        card["image"] = image
    return card


# Wikipedia in the result list ("6 or 7 in a row as the top results").
WIKI_MIX_MAX = 2                # other articles kept in the list once the card has its own
WIKI_MIX_SLOTS = (1, 4)         # 0-based places among the rest: one early, one in the middle


# The first row of the /instant list (audit 2026-10-20 #1): the ranked list's first result, when it is an article
# the models put first (score under WIKI_EXACT_SCORE), used to fall to the second row behind a website, and the card's
# own article left the list; a list-only client (launcher, start page, API) saw the right first result for 30.4% of
# the judged questions against 71.0% on the ranked list. SEARCH_INSTANT_LIST_FIRST=1 keeps the served first result
# first; the card's article comes back as the first row flagged in_card, so a page that shows the card can hide it.
#   SEARCH_INSTANT_LIST_FIRST=0   turns it off; and per request ?ilf=1|0 (A/B of both layouts on one engine).
#   Default ON since rc9 (owner, 2026-10-20): judged dev list-first right@1 35.5 -> 70.4 with accounts in the card,
#   open sets dev pooled list hit@1 65.7 -> 85.4 (the /search list's 85.4); /search itself is not touched.
INSTANT_LIST_FIRST = os.environ.get("SEARCH_INSTANT_LIST_FIRST", "1").strip().lower() not in ("0", "off", "false", "no")
_instant_list_first: contextvars.ContextVar = contextvars.ContextVar("instant_list_first", default=INSTANT_LIST_FIRST)


# The list under the card, wider and cleaner (2026-11-07, owner: "what were looking for at this point is increasing the
# breadth of quality results, i think we pretty much nail whats on the card" / "just make it as good as it can get").
# SEARCH_BREADTH (default "") or per request ?bl= (eval A/B), letters combined:
#   L  the topic's community and reference pages from Wikidata (SEARCH_CARDLINKS: its subreddit, fan wiki,
#      Britannica, Letterboxd, Transfermarkt ...) join the list as rows, placed by the reranker against the rows there
#   S  the same rows, but the subreddit and fan wiki take fixed places (2nd and 3rd under the first row)
#   J  junk out: a row that shares no word of the search (title, address or text), or repeats a site already shown
#      (its home under another path, a cdn./static. host), unless it is the card's own site
#   D  one row per site (the card's own site excepted; Wikipedia articles are not capped, see below)
#   F  a floor: from the 4th row on, a row the reranker scores under BREADTH_FLOOR is left out (a shorter good list)
#   W  the links the card's Wikipedia article itself lists under "External links" / "Further reading"
#      (SEARCH_WIKILINKS, importer/wikiext.py): up to BREADTH_WIKI of them join the list, scored by the reranker
#      against the rows there like L's, with the editors' own link text as the row title; off unless the letter is set
#   C  the same for the article's best citations (the sources its facts came from: BREADTH_CITE of them, the domains
#      the most Wikipedia articles cite first); a candidate must score BREADTH_WIKI_MIN or more
BREADTH = os.environ.get("SEARCH_BREADTH", "").strip().upper()
# Fitted 2026-11-07 on mix-300's base lists, Clef-judged rows from the 4th on, scored by st1 per channel (ce-st1-pc):
# -2.7 removes half the junk rows (529 of 1,055) and 43 of 351 relevant ones (92% of what it removes is junk); -2.5
# removes 78% of the junk but 125 relevant rows. A floor belongs to one reranker's scale: refit it when the model changes.
BREADTH_FLOOR = float(os.environ.get("SEARCH_BREADTH_FLOOR", "-2.7"))
_breadth_floor: contextvars.ContextVar = contextvars.ContextVar("breadth_floor", default=BREADTH_FLOOR)
BREADTH_LINKS = 3
_breadth: contextvars.ContextVar = contextvars.ContextVar("breadth", default=BREADTH)
_BREADTH_SKIP = frozenset(("Official site", "X", "Instagram", "Facebook", "TikTok", "Threads", "LinkedIn", "YouTube",
                           "Pinterest", "Giphy", "Bluesky", "Mastodon", "GitHub", "Amazon", "Vikidia"))
_BREADTH_STOP = frozenset(("the", "and", "for", "with", "how", "what", "who", "when", "where", "why", "near", "best",
                           "from", "into", "your", "you", "are", "was", "does", "can", "of", "in", "on", "to", "a",
                           "an", "is", "it", "my", "me", "do", "vs", "man", "men", "new", "one", "out", "get", "all"))
_BREADTH_UNCAPPED = frozenset(("wikipedia", "stackexchange", "gov", "account", "youtube", "subreddit"))
_BREADTH_QUESTION = re.compile(r"^(how|what|why|when|where|can|should|does|do|is)\b", re.I)


def _site_of(url: str) -> str:
    h = (urlsplit(url or "").hostname or "").lower()
    for pre in ("www.", "m.", "cdn.", "static.", "assets.", "en."):
        if h.startswith(pre) and h.count(".") >= 2:
            h = h[len(pre):]
    p = h.split(".")
    return ".".join(p[-3:]) if len(p) >= 3 and p[-2] in ("co", "com", "org", "ac", "gov", "net", "edu") and len(p[-1]) == 2 else ".".join(p[-2:])


def _uncapped_row(r: dict, st: str, is_gov: bool = False) -> bool:
    """Wikipedia, Stack Exchange Q&A, channel/account rows, and (on a government search) .gov/.mil portals hold distinct
    pages or agencies under one root domain: D does not cap them at one row per root domain, and J leaves them."""
    return r.get("source") in _BREADTH_UNCAPPED or (is_gov and st.endswith((".gov", ".mil")))


def _link_rows(card: dict) -> list[dict]:
    """The card's Wikidata community/reference links as list rows, with a title a reader recognises."""
    name = card.get("title") or ""
    rows = []
    for l in (card.get("links") or []) + (card.get("more_links") or []):
        label, url = l.get("title") or "", l.get("url") or ""
        if label in _BREADTH_SKIP or not url.startswith("https://"):
            continue
        if label == "Reddit":
            title = "r/" + url.rstrip("/").rsplit("/", 1)[-1] + " — Reddit"
        elif label == "Fandom":
            title = f"{name} Wiki — Fandom"
        else:
            title = f"{name} — {label}"
        rows.append({"url": url, "title": title, "content": card.get("description") or "", "source": "site",
                     "score": 0.0, "wikidata_link": label})
    return rows


def _scores(q: str, rows: list[dict], scorer=None):
    scorer = scorer if scorer is not None else cross_encoder()
    if not scorer or not rows:
        return None
    return [float(v) for v in scorer(q, [ce_text(r) for r in rows])]


# SEARCH_SCORE_ALL=1 (or ?sa=1, eval A/B; default off): every row of the shown list that has no reranker score gets one
# before the list is cleaned (breadth_list), so the F floor reads a score for every row and never a guess. Without it a
# third of the rows of mix-300's lists (789 of 2,434, 25% relevant) carry none: ce_rerank scores only the 8 candidates
# under the exact tiers; the rows behind them, and the exact tiers themselves, keep the hand score.
# Left unscored on purpose: a row at or above CE_EXACT (an exact article, a named or official site, an account, a place:
# the tier is why it is there and the model is never allowed to move it), a place or stock answer, the list's first row
# (the answer; F never drops it) and an offline curated row (a larger model already chose it). The rows of a web_only
# search are scored too: ce_rerank leaves that whole list unscored because the model was not fitted to reorder those
# wordings, but the score only decides what F cuts, never the order, and on mix-300 the filled score is 41%
# relevant above -2.7 and 5% below it for exactly those rows.
SCORE_ALL = os.environ.get("SEARCH_SCORE_ALL", "").strip() == "1"
# SEARCH_SCORE_ALL_TOP=N (2026-11-14): with SCORE_ALL, only the list's first N rows are scored and kept; the rest go. A page
# shows about 8 rows, and scoring every row was the costliest step of a new search on public (median 142 ms, slowest tenth
# 1.8 s; new searches held fell from 4 to 2 a second). Simulated on the judged lists of 2026-11-13 (floor 0, first 8 shown):
# N=10 -> everyday-100 pass 69 -> 71, relevant rows 426 -> 402, rows sent to the model -42%; mix-300 pass 185 -> 187,
# relevant 950 -> 900, model rows -44%. 0 = every row, as before.
SCORE_ALL_TOP = int(os.environ.get("SEARCH_SCORE_ALL_TOP", "0") or 0)
_score_all: contextvars.ContextVar = contextvars.ContextVar("score_all", default=SCORE_ALL)


def score_unscored(q: str, res: list[dict], scorer=None) -> list[dict]:
    """res with a reranker score (ce) on every row that should have one and has none. One batch; the extra pairs are
    the rows returned without ce, and a search with nothing to score asks the model nothing."""
    if len(res) < 2:
        return res
    todo = [i for i, r in enumerate(res)
            if i > 0 and r.get("ce") is None and r.get("score", 0) < CE_EXACT and not r.get("curated")
            and r.get("source") not in ("place", "stock") and not r.get("stock")]
    if not todo:
        return res
    sc = _scores(q, [res[i] for i in todo], scorer)
    if sc is None:
        return res
    out = list(res)
    for i, v in zip(todo, sc):
        out[i] = dict(res[i], ce=round(v, 2))
    return out


def _scores_reuse(q: str, rows: list[dict], scorer=None):
    """Like _scores, but a row that already has a ce keeps it: the model is asked about the rows without one only."""
    need = [i for i, r in enumerate(rows) if not isinstance(r.get("ce"), (int, float)) and (r.get("score") or 0) < CE_EXACT]
    sc = _scores(q, [rows[i] for i in need], scorer) if need else []
    if sc is None:
        return None
    out = [float(r["ce"]) if isinstance(r.get("ce"), (int, float)) else 0.0 for r in rows]
    for i, v in zip(need, sc):
        out[i] = v
    return out


# The card's Wikipedia article's own outbound links (importer/wikiext.py, 2026-11-09). SEARCH_WIKILINKS is a sqlite file:
# links(title, kind, pos, url, label, domain) and trust(domain, n = distinct articles citing it). Read per card by its
# English Wikipedia title, only when the W or C breadth letter is on.
WIKILINKS_PATH = os.environ.get("SEARCH_WIKILINKS", "").strip()
BREADTH_WIKI = int(os.environ.get("SEARCH_BREADTH_WIKI", "3"))
BREADTH_CITE = int(os.environ.get("SEARCH_BREADTH_CITE", "3"))
BREADTH_WIKI_MIN = float(os.environ.get("SEARCH_BREADTH_WIKI_MIN", "-1.5"))
# A first run (2026-11-09, sq-eval place set) let candidates land above an expected answer (kiehl's 4th -> 7th,
# tornado 4th -> 6th): they only go from this 0-based place of the rows after the first on (default: behind three rows).
BREADTH_WIKI_FROM = int(os.environ.get("SEARCH_BREADTH_WIKI_FROM", "3"))
_wikilinks_db = threading.local()
_GENERIC_LABEL = re.compile(r"^(official\s+(web\s*)?site|official page|website|web site|home\s*page|homepage|home|link|"
                            r"profile|page|site|here|online|web|pdf|full text|text|source|archived|archive|"
                            r"main page|main site)$", re.I)
_WIKI_SKIP_DOMAINS = frozenset(("facebook.com", "twitter.com", "x.com", "instagram.com", "linkedin.com", "youtube.com",
                                "tiktok.com", "threads.net", "pinterest.com", "doi.org", "jstor.org", "worldcat.org",
                                "handle.net", "ssrn.com", "semanticscholar.org", "archive.org", "googleusercontent.com",
                                "imdb.com", "wikimedia.org", "amazon.com", "goodreads.com"))


def wiki_links(title: str) -> list[tuple[str, str, str, str, int]]:
    """[(kind, url, label, domain, trust)] the article lists, page order within a kind; [] without the file."""
    if not WIKILINKS_PATH or not title:
        return []
    import sqlite3
    db = getattr(_wikilinks_db, "db", None)
    if db is None:
        try:
            db = sqlite3.connect(f"file:{WIKILINKS_PATH}?mode=ro", uri=True, check_same_thread=False)
            _wikilinks_db.db = db
        except sqlite3.Error:
            return []
    try:
        return db.execute("SELECT l.kind, l.url, l.label, l.domain, coalesce(t.n, 0) FROM links l "
                          "LEFT JOIN trust t ON t.domain = l.domain WHERE l.title = ? ORDER BY l.kind, l.pos",
                          (title,)).fetchall()
    except sqlite3.Error:
        return []


def _wiki_rows(card: dict, flags: str, have: set) -> tuple[list[dict], list[dict]]:
    """(W rows, C rows): the card's article's links as list rows. The row title is the editors' link text (never the
    card's name: a candidate must earn its place by what it says); a generic text ("official website") becomes the
    site's name, and a citation without a usable text is left out."""
    ext, cite, seen = [], [], set(have)
    for kind, url, label, dom, trust in wiki_links(card.get("title") or ""):
        if dom in _WIKI_SKIP_DOMAINS or not url.startswith(("https://", "http://")):
            continue
        st = _site_of(url)
        if st in seen:
            continue
        host = (urlsplit(url).hostname or "").removeprefix("www.")
        if kind == "citation":
            if not label or _GENERIC_LABEL.match(label) or len(label) < 6:
                continue
            title = label
        else:
            title = host if (not label or _GENERIC_LABEL.match(label)) else label
        seen.add(st)
        row = {"url": url, "title": title, "content": "", "source": "site", "score": 0.0,
               "wikipedia_link": kind, "trust": trust}
        (cite if kind == "citation" else ext).append(row)
    if "W" not in flags:
        ext = []
    if "C" not in flags:
        cite = []
    return ext, cite


def _merge_wiki(q: str, rest: list[dict], sc, card: dict, flags: str, have: set, scorer=None):
    """W / C: score the article's links with the list's rows and put the best of each kind where their score falls;
    a candidate under BREADTH_WIKI_MIN is not added. Returns (rest, scores or None)."""
    ext, cite = _wiki_rows(card, flags, have)
    cands = ext + cite
    if not cands:
        return rest, sc
    allsc = _scores(q, rest + cands, scorer)
    if allsc is None:
        return rest, sc
    rs, cs = allsc[:len(rest)], allsc[len(rest):]
    take = []
    for pool, n, off in ((ext, BREADTH_WIKI, 0), (cite, BREADTH_CITE, len(ext))):
        ranked = sorted(((cs[off + i], i) for i in range(len(pool)) if cs[off + i] >= BREADTH_WIKI_MIN), reverse=True)
        take += [(v, pool[i]) for v, i in ranked[:n]]
    merged = list(zip(rs, rest))
    for v, row in sorted(take, key=lambda t: -t[0]):
        pos = max(next((k for k, (rv, _r) in enumerate(merged) if v > rv), len(merged)), min(BREADTH_WIKI_FROM, len(merged)))
        merged.insert(pos, (v, dict(row, ce=round(v, 2))))
    return [r for _v, r in merged], [v for v, _r in merged]


def breadth_list(q: str, res: list[dict], card: dict | None, flags: str, scorer=None) -> list[dict]:
    """The shown list after the Wikipedia mix, shaped by the SEARCH_BREADTH flags (above). Places and a stock
    answer stay on top as they are; the first row stays first."""
    if not flags or not res:
        return res
    n = 0
    while n < len(res) and (res[n].get("source") == "place" or res[n].get("stock")):
        n += 1
    head, rows = res[:n], res[n:]
    # places under a site or an article ("taco bell": the official site, then its three branches) are no list rows
    # either: D kept one row of openstreetmap.org and the page, which needs two places for its Nearby box, drew none
    # (owner 2026-11-13, "why no near me"). They sit out the shaping and go back after the row they followed.
    mid, kept = {}, []
    for r in rows:
        if r.get("source") == "place":
            mid.setdefault(len(kept) - 1, []).append(r)
        else:
            kept.append(r)
    rows = kept
    if not rows:
        return res
    official = {_site_of(l["url"]) for l in (card or {}).get("links", []) if l.get("title") == "Official site"}
    first, rest = rows[0], rows[1:]
    # the search's words, and the card's name ("taylor swfit" is answered as Taylor Swift: her name's words count)
    words = {w for w in re.findall(r"[a-z0-9]+", ((q or "") + " " + ((card or {}).get("title") or "")).lower())
             if len(w) >= 3 and w not in _BREADTH_STOP}

    def has_word(r: dict) -> bool:
        text = " ".join((r.get("title") or "", r.get("url") or "", (r.get("content") or "")[:300])).lower()
        flat = re.sub(r"[^a-z0-9]", "", text)              # "spiderman" is in "Spider-Man", "westham" in "West Ham"
        return any(w in text or w in flat for w in words)
    is_gov = any(r.get("source") == "gov" or _site_of(r.get("url") or "").endswith((".gov", ".mil")) for r in rows[:3])
    if "J" in flags:
        # when no row beyond the first shares a word and there is no card, or the search is a question / government task,
        # the query words are not the results' vocabulary: J's word check stays out. With a card (or >=2 matching rows),
        # J stays on even when more than half of rest is junk ("spiderman" had 8 junk rows of 13, 2026-11-08)
        word_ok = (bool(words) and not is_gov and not _BREADTH_QUESTION.match((q or "").strip())
                   and (card is not None or sum(1 for r in rows if has_word(r)) >= 2))
        seen, out = {_site_of(first["url"])}, []
        for r in rest:
            st = _site_of(r["url"])
            if word_ok and not _uncapped_row(r, st, is_gov) and not has_word(r):
                continue
            path = urlsplit(r["url"]).path.strip("/").lower()
            if st in seen and st not in official and path in ("", "en", "en-us", "en-gb", "home", "index.html"):
                continue
            seen.add(st); out.append(r)
        rest = out
    if "D" in flags:
        # one row per site (the card's own site excepted). Wikipedia, Stack Exchange Q&A, government (.gov/.mil) and
        # channel/account rows are NOT capped: Clef 27B judged a second state agency page (courts.oregon.gov after
        # oregon.gov/boli), a second Stack Exchange question or a second article better than what replaced them
        # (2026-11-08, mix-300: government 2.26 -> 3.46, howto 1.64 -> 1.80, people 2.06 -> 2.50)
        out, per = [], {_site_of(first["url"]): 1}
        for r in rest:
            st = _site_of(r["url"])
            if not _uncapped_row(r, st, is_gov) and per.get(st, 0) >= 1 and st not in official:
                continue
            per[st] = per.get(st, 0) + 1; out.append(r)
        rest = out
    links = []
    if card and ("L" in flags or "S" in flags):
        have = {same_page(r["url"]) for r in rows} | {_site_of(r["url"]) for r in rows}
        links = [l for l in _link_rows(card) if same_page(l["url"]) not in have and _site_of(l["url"]) not in have]
    sc = None
    if "L" in flags and links:
        sc = _scores(q, rest + links, scorer)
        if sc is not None:
            rs, ls = sc[:len(rest)], sc[len(rest):]
            best = sorted(zip(ls, range(len(links))), reverse=True)[:BREADTH_LINKS]
            merged = list(zip(rs, rest))
            for v, i in best:
                pos = next((k for k, (rv, _r) in enumerate(merged) if v > rv), len(merged))
                merged.insert(pos, (v, dict(links[i], ce=round(v, 2))))
            rest = [r for _v, r in merged]
            sc = [v for v, _r in merged]
    elif "S" in flags and links:
        for k, l in enumerate([l for l in links if l["wikidata_link"] in ("Reddit", "Fandom")][:2]):
            rest.insert(min(k, len(rest)), l)
    if card and ("W" in flags or "C" in flags) and WIKILINKS_PATH:
        have = {_site_of(r["url"]) for r in rows} | {_site_of(l["url"]) for l in links} | official
        rest, got = _merge_wiki(q, rest, sc, card, flags, have, scorer)
        if got is not None:
            sc = got
    if "F" in flags:
        if sc is None or len(sc) != len(rest):
            sc = _scores_reuse(q, rest, scorer) if _score_all.get() else _scores(q, rest, scorer)
        if sc is not None:
            floor = _breadth_floor.get()
            pin = _score_all.get()                         # with SCORE_ALL an exact-tier row is never under the floor
            rest = [r for k, (r, v) in enumerate(zip(rest, sc))
                    if k < 2 or v >= floor or r.get("wikidata_link") or (pin and (r.get("score") or 0) >= CE_EXACT)]
    out = [first] + rest
    if mid:
        left = {r["url"] for r in out}
        follow = {}
        for k in sorted(mid):                              # after the row it followed, or the nearest one above still shown
            at = next(kept[i]["url"] for i in range(k, -1, -1) if kept[i]["url"] in left or i == 0)
            follow.setdefault(at, []).extend(mid[k])
        out = [x for r in out for x in [r] + follow.pop(r["url"], [])]
    return head + out


# Stack Exchange how-to index (2026-11-10, owner decisions, ADR-081): SEARCH_SE_INDEX=<sqlite> (tools/se-howto.py cut)
# gives how-to and question searches candidate rows from 45 sites' questions, each with a snippet of its best answer.
# Off unless the path is set; per request ?se=0|1 (eval A/B). A candidate goes through the reranker with the rows already
# in the list and is added only when its score clears SEARCH_SE_MIN (default the breadth floor: "ring size" must not
# bring ring flashes); at most SEARCH_SE_ROWS are added. A row the release already lists (same page) is not repeated: it
# gets the snippet and the licence fields. A row carries, besides url / title: content (the snippet), snippet (the same,
# an excerpt), the credit fields of se_credit (author, answer_author and their profile links), answer_url, licence,
# licence_url, excerpt: true. The page must show the question link, both authors, the licence mark and "excerpt" with it.
SE_INDEX_PATH = os.environ.get("SEARCH_SE_INDEX", "").strip()
SE_INDEX_ROWS = int(os.environ.get("SEARCH_SE_ROWS", "3"))
SE_INDEX_MIN = float(os.environ.get("SEARCH_SE_MIN", str(BREADTH_FLOOR)))
SE_INDEX_POOL = int(os.environ.get("SEARCH_SE_POOL", "20"))      # candidates read from the index and scored (each is one reranker pair)
# SEARCH_SE_REUSE=1: a row of the shown list that already carries a reranker score keeps it, and the model is
# asked about the candidates and the unscored rows only. Off = every row is scored again with the candidates.
SE_INDEX_REUSE = os.environ.get("SEARCH_SE_REUSE", "").strip() == "1"
_se_on: contextvars.ContextVar = contextvars.ContextVar("se_index_on", default=True)
_se_ix_db = threading.local()
_SE_STOP = frozenset(("the", "a", "an", "of", "and", "in", "on", "for", "to", "is", "are", "was", "how", "do", "does", "what",
                      "who", "why", "when", "where", "can", "i", "my", "me", "you", "with", "at", "by", "from", "it", "this",
                      "that", "be", "or", "as", "get", "your"))
_SE_LICENCES = ((("2011-04-08",), "2.5"), (("2018-05-02",), "3.0"))      # answers before the date: that version; later: 4.0


def se_ix_licence(date: str) -> tuple[str, str]:
    """(name, deed url) of the CC BY-SA version an answer written on `date` (YYYY-MM-DD) carries: 2.5 before 2011-04-08,
    3.0 before 2018-05-02, 4.0 after (the network's public terms as we read them; the date is stored per answer)."""
    d = (date or "")[:10]
    v = "4.0"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", d):
        for (cut,), ver in _SE_LICENCES:
            if d < cut:
                v = ver
                break
    return f"CC BY-SA {v}", f"https://creativecommons.org/licenses/by-sa/{v}/"


def se_ix_lookup(q: str, n: int | None = None) -> list[tuple]:
    """The index's best rows for q: bm25 with the title weighted most; AND of the content words, OR when AND gives
    fewer than 5. [] without the file or without a content word."""
    import sqlite3
    if not SE_INDEX_PATH:
        return []
    n = SE_INDEX_POOL if n is None else n
    words = [w for w in re.findall(r"[a-z0-9]+", (q or "").lower()) if w not in _SE_STOP]
    if not words:
        return []
    db = getattr(_se_ix_db, "db", None)
    if db is None:
        try:
            db = sqlite3.connect(f"file:{SE_INDEX_PATH}?mode=ro", uri=True, check_same_thread=False)
            _se_ix_db.db = db
        except sqlite3.Error:
            return []
    rows: list[tuple] = []
    for op in (" AND ", " OR "):
        try:
            rows = db.execute("SELECT s.site, s.qid, s.title, s.score, s.acc, s.asker, s.answerer, s.aid, s.adate, s.snip "
                              "FROM fts JOIN s ON s.id = fts.rowid WHERE fts MATCH ? ORDER BY bm25(fts, 6.0, 2.0, 1.0, 1.0) LIMIT ?",
                              (op.join(f'"{w}"' for w in words[:12]), n)).fetchall()
        except sqlite3.Error:
            rows = []
        if len(rows) >= 5:
            break
    return rows


def _se_ix_credit(packed: str) -> tuple:
    """'<user id>|<name>' -> (user id or None, name or None)."""
    uid, _, name = (packed or "").partition("|")
    return (int(uid) if uid.isdigit() else None), (name.strip() or None)


def se_ix_row(r: tuple) -> dict:
    """One index row -> a result row with its snippet, credits and licence fields."""
    site, qid, title, score, acc, asker, answerer, aid, adate, snip = r
    url = f"https://{site}/questions/{qid}"
    lic, lic_url = se_ix_licence(adate)
    row = {"url": url, "title": f"{title} \u2014 {site}", "content": snip or "", "source": "stackexchange", "score": 0.0,
           "se_index": True, "accepted": bool(acc), "votes": score, "answer_url": f"https://{site}/a/{aid}" if aid else None,
           "licence": lic, "licence_url": lic_url, "excerpt": True}
    if snip:
        row["snippet"] = snip
    row.update(se_credit(url, {1: _se_ix_credit(asker), 2: _se_ix_credit(answerer)}))
    return row


def se_howto(q: str, res: list[dict], scorer=None) -> list[dict]:
    """The shown list with index rows for a how-to or question search (intent_of, the engine's own signal). Returns the
    list as it was when the index is off, the search is another kind, or no candidate clears the floor."""
    if not SE_INDEX_PATH or not _se_on.get() or not res or intent_of(q, res) != "howto":
        return res
    cands = [se_ix_row(r) for r in se_ix_lookup(q)]
    if not cands:
        return res
    have = {same_page(r["url"]): r for r in res}
    out = list(res)                                       # rows may be shared with the result cache: copy before changing one
    for k, old in enumerate(out):
        c = next((c for c in cands if same_page(c["url"]) == same_page(old["url"])), None)
        if c is not None and (old.get("source") == "stackexchange" or "author" in old or "answer_author" in old):
            # a row the release lists already (a release page row is source "site" with its credits): snippet + licence on it
            upd = {f: c[f] for f in ("content", "snippet", "answer_url", "licence", "licence_url", "excerpt", "accepted",
                                     "votes", "se_index") if f in c}
            upd.update({f: c[f] for f in ("author", "author_url", "answer_author", "answer_author_url") if f in c and f not in old})
            out[k] = dict(old, **upd)
    new = [c for c in cands if same_page(c["url"]) not in have]
    if not new or SE_INDEX_ROWS <= 0:
        return out
    if SE_INDEX_REUSE:                                    # the rows' own scores stand; only what has none is asked
        todo = [k for k, r in enumerate(out) if not isinstance(r.get("ce"), (int, float))]
        sc = _scores(q, [out[k] for k in todo] + new, scorer)
        if sc is not None:
            rs = [float(r["ce"]) if isinstance(r.get("ce"), (int, float)) else 0.0 for r in out]
            for k, v in zip(todo, sc):
                rs[k] = v
            sc = rs + sc[len(todo):]
    else:
        sc = _scores(q, out + new, scorer)
    if sc is None:
        return out
    rs, ns = sc[:len(out)], sc[len(out):]
    take = sorted(((v, i) for i, v in enumerate(ns) if v >= SE_INDEX_MIN), reverse=True)[:SE_INDEX_ROWS]
    if not take:
        return out
    n_head = 0
    while n_head < len(out) and (out[n_head].get("source") == "place" or out[n_head].get("stock")):
        n_head += 1
    merged = list(zip(rs, out))
    for v, i in take:                                     # best first; the first row after the places stays first
        pos = next((k for k, (rv, _r) in enumerate(merged) if k > n_head and v > rv), len(merged))
        merged.insert(pos, (v, dict(new[i], ce=round(v, 2))))
    return [r for _v, r in merged]


_SEASON = re.compile(r"^((?:1[89]|20)\d\d)[–-](?:\d{2}|\d{4})\b.*\bseason\b", re.I)


def old_season(r: dict, year: int | None = None) -> bool:
    """A "1964–65 West Ham United F.C. season" article more than a year old (it took the list's second row for
    "west ham united"): it follows the other articles instead of taking one of the early slots."""
    m = _SEASON.match(r.get("title") or "")
    return bool(m) and int(m.group(1)) < (year or time.gmtime().tm_year) - 1


def mix_wikipedia(res: list[dict], card: dict | None, list_first: bool | None = None) -> list[dict]:
    """The list as shown: the card's article, and the articles the card already lists as
    related, are not repeated; of the remaining articles at most WIKI_MIX_MAX stay near the
    top, each between websites; the rest follow the websites. With no card, a leading exact
    article is the answer itself and stays first. With list_first (SEARCH_INSTANT_LIST_FIRST,
    ?ilf=), a leading article stays first whatever its score, and the card's own article stays
    as the first row flagged in_card instead of leaving the list."""
    if not any(r.get("source") == "wikipedia" for r in res):
        return res
    if list_first is None:
        list_first = _instant_list_first.get()
    lead = [r for r in res if r.get("stock")]          # a stock answer's three links stay together on top
    if lead and res[0].get("stock"):
        return lead + mix_wikipedia([r for r in res if not r.get("stock")], card, list_first)
    n = 0                                              # nearby places lead as one block: a list-only client
    while n < len(res) and res[n].get("source") == "place":      # (launcher, start page) must not see an article
        n += 1                                                   # between two of them
    if n:
        return res[:n] + mix_wikipedia(res[n:], card, list_first)
    shown = set()
    if card:
        shown = {same_page(card.get("url") or "")} | {same_page(r["url"]) for r in (card.get("related") or []) if r.get("url")}
    first = res[0]
    if list_first and first.get("source") == "wikipedia" and not first.get("namesake"):
        # the ranked list's first result is the answer: it stays first; the card's own article is marked
        lead = [dict(first, in_card=True) if card and same_page(first["url"]) in shown else first]
    else:
        lead = [first] if not card and first.get("source") == "wikipedia" and first["score"] >= WIKI_EXACT_SCORE else []
    rest = [r for r in res[len(lead):] if same_page(r["url"]) not in shown or r.get("source") != "wikipedia"]
    web = [r for r in rest if r.get("source") != "wikipedia"]
    articles = [r for r in rest if r.get("source") == "wikipedia" and not r.get("namesake")]
    old = [r for r in articles if old_season(r)]                #: never in an early slot, even when every
    articles = [r for r in articles if not old_season(r)]       # article left is one ("west ham united")
    articles += [r for r in rest if r.get("namesake")]          # a namesake never takes a slot
    out = list(web)
    for slot, art in zip(WIKI_MIX_SLOTS[:WIKI_MIX_MAX], articles):
        out.insert(min(slot, len(out)), art)
    return lead + out + articles[WIKI_MIX_MAX:] + old


def _account(platform, url, name, desc, value, sitelinks, score) -> dict:
    label = ACCOUNT_LABEL.get(platform, platform)
    handle = f" (@{value})" if platform in ("x", "instagram", "tiktok", "twitch", "github", "bluesky") else ""
    r = {"url": url, "title": f"{name} on {label}{handle}", "content": (desc or "")[:300],
         "source": "account", "score": round(score, 2), "platform": platform}
    if (sitelinks or 0) >= ACCOUNT_POPULAR:
        r["popular"] = True
    return r


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
PLACE_RADIUS_KM = float(os.environ.get("SEARCH_PLACE_RADIUS_KM", "56"))     # 35 miles (was 40 km, 2026-10-11)
_HOME = os.environ.get("SEARCH_HOME", "")
HOME = tuple(float(x) for x in _HOME.split(",")) if _HOME.count(",") == 1 else None
# Where the searcher is ("launcher should use current location for finding
# close physical locations"): the launcher sends near=lat,lon with a search; places are
# measured from there instead of the home point. Per request; never logged.
_near = contextvars.ContextVar("near", default=None)


def parse_near(v: str | None) -> tuple[float, float] | None:
    """"40.597,-105.871" -> (40.597, -105.871), rounded to ~100 m; anything else -> None."""
    m = re.fullmatch(r"\s*(-?\d{1,2}(?:\.\d+)?)\s*,\s*(-?\d{1,3}(?:\.\d+)?)\s*", v or "")
    if not m:
        return None
    lat, lon = float(m.group(1)), float(m.group(2))
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return round(lat, 3), round(lon, 3)


# How far "near me" reaches, per request: the public page's slider in settings (2026-10-11), in miles.
# Anything else -> the default (SEARCH_PLACE_RADIUS_KM).
_reach_km = contextvars.ContextVar("reach_km", default=None)


def parse_reach(v: str | None) -> float | None:
    """"35" -> 56.3 (km); a whole number of miles from 1 to 150 only."""
    m = re.fullmatch(r"\s*(\d{1,3})\s*", v or "")
    if not m or not 1 <= int(m.group(1)) <= 150:
        return None
    return round(int(m.group(1)) / 0.621371, 1)


def place_radius_km() -> float:
    return _reach_km.get() or PLACE_RADIUS_KM


def home_point() -> tuple[float, float] | None:
    """The point places are measured from: the searcher's (launcher) or the home point."""
    return _near.get() or HOME
PLACE_KINDS = {
    "restaurant": ["amenity=restaurant", "amenity=fast_food", "cuisine=pizza"],
    "restaurants": ["amenity=restaurant", "amenity=fast_food", "cuisine=pizza"],
    "food": ["amenity=restaurant", "amenity=fast_food", "cuisine=pizza"], "gym": ["leisure=fitness_centre", "leisure=sports_centre"],
    "gyms": ["leisure=fitness_centre", "leisure=sports_centre"], "fitness": ["leisure=fitness_centre"],
    "pharmacy": ["amenity=pharmacy"], "grocery": ["shop=supermarket", "shop=grocery"],
    "supermarket": ["shop=supermarket"], "gas": ["amenity=fuel"], "gasstation": ["amenity=fuel"],
    "farmersmarket": ["amenity=marketplace"], "hardwarestore": ["shop=hardware", "shop=doityourself"],
    "coffee": ["amenity=cafe"], "cafe": ["amenity=cafe"], "bank": ["amenity=bank"],
    "hospital": ["amenity=hospital"], "urgentcare": ["healthcare=clinic", "amenity=clinic"],
    "library": ["amenity=library"], "park": ["leisure=park"], "parks": ["leisure=park"],
    "postoffice": ["amenity=post_office"], "bar": ["amenity=bar", "amenity=pub"], "bars": ["amenity=bar", "amenity=pub"],
    "pizza": ["cuisine=pizza"], "vet": ["amenity=veterinary"], "dentist": ["amenity=dentist", "healthcare=dentist"],
    "laundromat": ["shop=laundry"], "carwash": ["amenity=car_wash"], "petstore": ["shop=pet"],
    # golf (2026-10-05): 9,914 OSM golf courses were in the table but no word reached them
    "golfcourse": ["leisure=golf_course"], "golfcourses": ["leisure=golf_course"], "golfclub": ["leisure=golf_course"],
    "golfclubs": ["leisure=golf_course"], "minigolf": ["leisure=miniature_golf"], "miniaturegolf": ["leisure=miniature_golf"],
    "discgolf": ["leisure=disc_golf_course"], "discgolfcourse": ["leisure=disc_golf_course"],
    # a kind of place asked in the plural or by its other everyday name is the same question (2026-10-10: a bare
    # "pharmacies" or "drugstore" found no place). Only kinds the places data holds: no malls, no parking yet
    "pharmacies": ["amenity=pharmacy"], "drugstore": ["amenity=pharmacy"], "drugstores": ["amenity=pharmacy"],
    "hospitals": ["amenity=hospital"], "libraries": ["amenity=library"], "cafes": ["amenity=cafe"],
    "coffeeshop": ["amenity=cafe"], "coffeeshops": ["amenity=cafe"], "dentists": ["amenity=dentist", "healthcare=dentist"],
    "vets": ["amenity=veterinary"], "veterinarian": ["amenity=veterinary"], "veterinarians": ["amenity=veterinary"],
    "supermarkets": ["shop=supermarket"], "grocerystore": ["shop=supermarket", "shop=grocery"],
    "grocerystores": ["shop=supermarket", "shop=grocery"], "gasstations": ["amenity=fuel"],
    "laundromats": ["shop=laundry"], "carwashes": ["amenity=car_wash"], "petstores": ["shop=pet"],
    "hardwarestores": ["shop=hardware", "shop=doityourself"], "postoffices": ["amenity=post_office"],
    "pizzeria": ["cuisine=pizza"], "pizzerias": ["cuisine=pizza"],
    # "fast food" by itself is the question of where to eat it (owner 2026-10-11); the rest of the eating words
    # below still need a near-word, or "diner" and "dinner" are the film and the meal
    "fastfood": ["amenity=fast_food"], "fastfoodrestaurant": ["amenity=fast_food"], "fastfoodrestaurants": ["amenity=fast_food"],
    # kinds the places data has held all along with no word that reached them (2026-10-11: "movies near me" found no
    # cinema; rc8 has 5,270 cinemas, 3,082 malls, 41,822 hotels). Only words that mean the place when said alone.
    "movietheater": ["amenity=cinema"], "movietheaters": ["amenity=cinema"], "movietheatre": ["amenity=cinema"], "movietheatres": ["amenity=cinema"], "cinema": ["amenity=cinema"], "cinemas": ["amenity=cinema"],
    "mall": ["shop=mall"], "malls": ["shop=mall"], "shoppingmall": ["shop=mall"], "shoppingmalls": ["shop=mall"], "shoppingcenter": ["shop=mall"], "shoppingcentre": ["shop=mall"],
    "hotel": ["tourism=hotel", "tourism=motel"], "hotels": ["tourism=hotel", "tourism=motel"], "motel": ["tourism=hotel", "tourism=motel"], "motels": ["tourism=hotel", "tourism=motel"],
    "autorepair": ["shop=car_repair"], "carrepair": ["shop=car_repair"], "mechanic": ["shop=car_repair"], "mechanics": ["shop=car_repair"], "autoshop": ["shop=car_repair"], "autorepairshop": ["shop=car_repair"],
    "barber": ["shop=hairdresser"], "barbers": ["shop=hairdresser"], "barbershop": ["shop=hairdresser"], "barbershops": ["shop=hairdresser"], "hairsalon": ["shop=hairdresser"], "hairsalons": ["shop=hairdresser"], "hairdresser": ["shop=hairdresser"], "hairdressers": ["shop=hairdresser"],
    "bakery": ["shop=bakery"], "bakeries": ["shop=bakery"],
    "conveniencestore": ["shop=convenience"], "conveniencestores": ["shop=convenience"],
    "clothingstore": ["shop=clothes"], "clothingstores": ["shop=clothes"], "clothesstore": ["shop=clothes"],
    "phonestore": ["shop=mobile_phone"], "phonestores": ["shop=mobile_phone"], "cellphonestore": ["shop=mobile_phone"],
    "departmentstore": ["shop=department_store"], "departmentstores": ["shop=department_store"],
    "electronicsstore": ["shop=electronics"], "electronicsstores": ["shop=electronics"],
    "liquorstore": ["shop=alcohol"], "liquorstores": ["shop=alcohol"],
    "cardealer": ["shop=car"], "cardealers": ["shop=car"], "cardealership": ["shop=car"], "cardealerships": ["shop=car"],
    "carrental": ["amenity=car_rental"], "carrentals": ["amenity=car_rental"], "rentalcar": ["amenity=car_rental"], "rentalcars": ["amenity=car_rental"],
    "firestation": ["amenity=fire_station"], "firestations": ["amenity=fire_station"],
    "policestation": ["amenity=police"], "policestations": ["amenity=police"],
    "icecreamshop": ["amenity=ice_cream"], "icecreamshops": ["amenity=ice_cream"],
    "nailsalon": ["shop=beauty"], "nailsalons": ["shop=beauty"], "beautysalon": ["shop=beauty"], "beautysalons": ["shop=beauty"],
    "furniturestore": ["shop=furniture"], "furniturestores": ["shop=furniture"],
    "shoestore": ["shop=shoes"], "shoestores": ["shop=shoes"],
    "florist": ["shop=florist"], "florists": ["shop=florist"], "flowershop": ["shop=florist"], "flowershops": ["shop=florist"],
    "bookstore": ["shop=books"], "bookstores": ["shop=books"], "bookshop": ["shop=books"], "bookshops": ["shop=books"],
    "museum": ["tourism=museum"], "museums": ["tourism=museum"],
    "playground": ["leisure=playground"], "playgrounds": ["leisure=playground"],
    "evcharging": ["amenity=charging_station"], "evcharger": ["amenity=charging_station"], "evchargers": ["amenity=charging_station"], "chargingstation": ["amenity=charging_station"], "chargingstations": ["amenity=charging_station"],
    "tireshop": ["shop=tyres"], "tireshops": ["shop=tyres"],
    "autoparts": ["shop=car_parts"], "autopartsstore": ["shop=car_parts"],
    "daycare": ["amenity=childcare", "amenity=kindergarten"], "daycares": ["amenity=childcare", "amenity=kindergarten"], "childcare": ["amenity=childcare", "amenity=kindergarten"], "preschool": ["amenity=childcare", "amenity=kindergarten"], "preschools": ["amenity=childcare", "amenity=kindergarten"],
    "optician": ["shop=optician"], "opticians": ["shop=optician"], "eyedoctor": ["shop=optician"], "eyedoctors": ["shop=optician"],
    "jewelrystore": ["shop=jewelry"], "jewelrystores": ["shop=jewelry"], "jeweler": ["shop=jewelry"], "jewelers": ["shop=jewelry"],
    "sportinggoods": ["shop=sports"], "sportinggoodsstore": ["shop=sports"],
    "bikeshop": ["shop=bicycle"], "bikeshops": ["shop=bicycle"], "bicycleshop": ["shop=bicycle"],
    "toystore": ["shop=toys"], "toystores": ["shop=toys"],
    "bowling": ["leisure=bowling_alley"], "bowlingalley": ["leisure=bowling_alley"], "bowlingalleys": ["leisure=bowling_alley"],
    "dollarstore": ["shop=variety_store"], "dollarstores": ["shop=variety_store"],
    "drycleaner": ["shop=dry_cleaning"], "drycleaners": ["shop=dry_cleaning"], "drycleaning": ["shop=dry_cleaning"],
    "courthouse": ["amenity=courthouse"], "courthouses": ["amenity=courthouse"],
    "townhall": ["amenity=townhall"], "cityhall": ["amenity=townhall"],
    "zoo": ["tourism=zoo"], "zoos": ["tourism=zoo"],
    "aquarium": ["tourism=aquarium"], "aquariums": ["tourism=aquarium"],
    "themepark": ["tourism=theme_park"], "themeparks": ["tourism=theme_park"], "amusementpark": ["tourism=theme_park"], "amusementparks": ["tourism=theme_park"],
    "smokeshop": ["shop=tobacco"], "smokeshops": ["shop=tobacco"], "tobaccoshop": ["shop=tobacco"],
    "giftshop": ["shop=gift"], "giftshops": ["shop=gift"],
    "tattooshop": ["shop=tattoo"], "tattooshops": ["shop=tattoo"], "tattooparlor": ["shop=tattoo"],
    "selfstorage": ["shop=storage_rental"], "storageunits": ["shop=storage_rental"], "storageunit": ["shop=storage_rental"],
    "massage": ["shop=massage"],
}
# A category with no place of its own kind nearby asks again with these (OSM-only data has no
# cuisine=pizza kind: "pizza" then means any restaurant, as before 2026-10-02).
PLACE_KIND_FALLBACK = {"pizza": ["amenity=restaurant"]}
# Phrases that mean a kind of place only beside a near-word ("fast food near me"); alone they are
# an article, a film or a site ("diner", "dinner"). Without these "fast food near me" matched a
# business NAMED "Fast Food, Inc" 21 mi away and "takeout near me" no place at all.
_EAT = ["amenity=restaurant", "amenity=fast_food", "cuisine=pizza"]
PLACE_KINDS_NEAR = {
    "burger": ["amenity=fast_food"], "burgers": ["amenity=fast_food"],
    "drivethru": ["amenity=fast_food"], "drivethrough": ["amenity=fast_food"],
    "takeout": _EAT, "takeaway": _EAT, "carryout": _EAT, "delivery": _EAT, "diner": ["amenity=restaurant"],
    "breakfast": _EAT, "brunch": _EAT, "lunch": _EAT, "dinner": _EAT, "cheapeats": _EAT, "placetoeat": _EAT,
    "placestoeat": _EAT, "somewheretoeat": _EAT, "eat": _EAT, "eats": _EAT, "wheretoeat": _EAT,
    "golf": ["leisure=golf_course"],               # "golf" alone is the sport; "golf near me" is the courses
    # alone these are a film, a subject or a thing to buy; beside a near-word they are where to go (2026-10-11)
    "movies": ["amenity=cinema"], "movie": ["amenity=cinema"], "film": ["amenity=cinema"], "films": ["amenity=cinema"], "showtimes": ["amenity=cinema"],
    "theater": ["amenity=cinema", "amenity=theatre"], "theaters": ["amenity=cinema", "amenity=theatre"], "theatre": ["amenity=cinema", "amenity=theatre"], "theatres": ["amenity=cinema", "amenity=theatre"],
    "school": ["amenity=school"], "schools": ["amenity=school"],
    "church": ["amenity=place_of_worship"], "churches": ["amenity=place_of_worship"], "mosque": ["amenity=place_of_worship"], "mosques": ["amenity=place_of_worship"], "synagogue": ["amenity=place_of_worship"], "synagogues": ["amenity=place_of_worship"], "temple": ["amenity=place_of_worship"], "temples": ["amenity=place_of_worship"],
    "doctor": ["amenity=doctors", "healthcare=clinic", "amenity=clinic"], "doctors": ["amenity=doctors", "healthcare=clinic", "amenity=clinic"],
    "icecream": ["amenity=ice_cream"],
    "police": ["amenity=police"],
    "storage": ["shop=storage_rental"],
    "tires": ["shop=tyres"], "tire": ["shop=tyres"],
    "shopping": ["shop=mall", "shop=department_store", "shop=clothes"],
    "haircut": ["shop=hairdresser"], "haircuts": ["shop=hairdresser"], "hair": ["shop=hairdresser"],
    "nails": ["shop=beauty"],
    "flowers": ["shop=florist"],
    "books": ["shop=books"],
    "shoes": ["shop=shoes"],
    "clothes": ["shop=clothes"], "clothing": ["shop=clothes"],
    "furniture": ["shop=furniture"],
    "jewelry": ["shop=jewelry"],
    "toys": ["shop=toys"],
    "electronics": ["shop=electronics"],
    "liquor": ["shop=alcohol"], "beer": ["shop=alcohol"], "wine": ["shop=alcohol"],
    "convenience": ["shop=convenience"],
    "attractions": ["tourism=attraction", "tourism=museum", "tourism=zoo", "tourism=theme_park"], "thingstodo": ["tourism=attraction", "tourism=museum", "tourism=zoo", "tourism=theme_park"],
    "stadium": ["leisure=stadium"], "stadiums": ["leisure=stadium"],
    "college": ["amenity=university", "amenity=college"], "colleges": ["amenity=university", "amenity=college"], "university": ["amenity=university", "amenity=college"], "universities": ["amenity=university", "amenity=college"],
}
_NEAR = re.compile(r"\b(near me|nearby|near by|close to me|open now|around me|nearest|closest)\b")


def place_query(q: str) -> tuple[str, str] | None:
    """-> ("kinds", key) for a category word, ("name", text) otherwise; None = not a place query shape."""
    t = _NEAR.sub(" ", q.lower()).strip()
    key = norm_key(t)
    if key in PLACE_KINDS:
        return "kinds", key
    if key in PLACE_KINDS_NEAR and _NEAR.search(q.lower()):
        return "kinds", key
    words = t.split()
    if not words or len(words) > 4 or len(key) < 3:
        return None
    return "name", " ".join(words)


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


# Words that follow a business's name without being part of it: "t mobile store", "verizon locations".
PLACE_TAIL = frozenset({"store", "stores", "shop", "shops", "location", "locations", "hours", "branch", "branches"})


def place_name_core(name: str) -> str:
    """"t mobile store" -> "t mobile"; a name that is only such words, or has none at its end, is unchanged."""
    words = name.split()
    while len(words) > 1 and words[-1] in PLACE_TAIL:
        words = words[:-1]
    return " ".join(words)


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


# A place searched by its whole name ("parx casino", 2026-09-25): found wherever it is within
# PLACE_EXACT_KM (a named place is worth a drive; the 40 km radius is for "gym"), the
# nearest first. Exact name only — the trigram index answers equality too.
PLACE_EXACT_KM = float(os.environ.get("SEARCH_PLACE_EXACT_KM", "200"))
PLACE_EXACT_SQL = """
SELECT osm, name, kind, street, city, hours, website, phone, lat, lon, d_km FROM (
    SELECT p.*, 111.32 * sqrt(power(p.lat - %(lat)s, 2)
                              + power((p.lon - %(lon)s) * cos(radians(%(lat)s)), 2)) AS d_km
    FROM places p
    WHERE regexp_replace(lower(p.name), '[^a-z0-9]', '', 'g') = ANY(%(keys)s)
       OR (p.brand <> '' AND regexp_replace(lower(p.brand), '[^a-z0-9]', '', 'g') = ANY(%(keys)s))) x
WHERE d_km <= %(r)s ORDER BY d_km LIMIT 40
"""
# Where nobody goes to shop ("amazon" answered with an Amazon delivery depot 39 mi off,
# ): never a place answer.
PLACE_BACKROOM = re.compile(r"^(amenity=post_depot|office=|industrial=|landuse=|man_made=|building=|craft=)")
# A store's departments, mapped as their own points beside it with their own hours: the
# Walmart Pharmacy's Mon-Sat 8-7 became the Twin Falls Walmart's hours.
# They never lend the store their hours, and never stand as a branch where a store is found.
PLACE_DEPT_KINDS = frozenset({"amenity=pharmacy", "shop=optician", "shop=garden_centre", "amenity=fuel",
                              "shop=car_repair", "shop=tyres", "shop=photo", "shop=bakery", "amenity=atm",
                              "amenity=car_wash", "amenity=vending_machine", "amenity=parcel_locker"})
PLACE_DEPT_WORDS = frozenset("pharmacy vision visioncenter optical optician garden auto autocare tire tires "
                             "photo photocenter bakery deli gas gasoline fuel pickup atm carwash".split())


def place_keys(name: str) -> list[str]:
    """Letters and digits only, with and without a leading "the": "dunkin" finds Dunkin' and
    brand=Dunkin'; "home depot" finds The Home Depot (both indexed, 2026-09-25)."""
    k = norm_key(name)
    keys = [k, "the" + k]
    if name.strip().lower().startswith("the ") and len(k) > 6:     # the word, not "theater"
        keys.append(k[3:])
    return keys
PLACE_SAME_KM = 0.5          # rows of one name this close are one place (OSM + a business listing)


def place_dept(name: str, kind: str, keys: list[str]) -> bool:
    """A store's department ("Walmart Pharmacy", "Walmart VisionCenter"), unless it is what
    was searched ("walmart pharmacy", or "cvs" -> CVS Pharmacy is the store itself)."""
    if norm_key(name or "") in keys:
        return False
    return (kind or "") in PLACE_DEPT_KINDS or bool(set(re.split(r"[^a-z0-9]+", (name or "").lower())) & PLACE_DEPT_WORDS)


def place_order(rows: list[tuple], keys: list[str], backroom: bool = False) -> list[tuple]:
    """PLACE_EXACT_SQL rows -> the same, nearest location first, each location's rows best
    first: the store before its departments, and the business's own listing (Web Data
    Commons, from its site) before the volunteer map's hours, which run years stale for
    chains. Back-room kinds go — unless backroom: asked for by name and town, a law office or a
    builder's yard is the place meant; so do department-only locations when a store was found."""
    rows = [r for r in rows if backroom or not PLACE_BACKROOM.match(r[2] or "")]
    groups: list[list[tuple]] = []
    for r in rows:
        home = next((g for g in groups if 111.32 * math.hypot(r[8] - g[0][8], (r[9] - g[0][9]) * math.cos(math.radians(r[8])))
                     <= PLACE_SAME_KM), None)
        (home.append(r) if home is not None else groups.append([r]))
    dept = lambda r: place_dept(r[1], r[2], keys)
    if any(not dept(r) for r in rows):
        groups = [g for g in groups if any(not dept(r) for r in g)]
    rank = lambda r: (dept(r), not (r[0].startswith("wdc:") and hours_sig(r[5] or "")), float(r[10]))
    return [r for g in groups for r in sorted(g, key=rank)]


def _web(url: str) -> str:
    """A place's own website: http(s), and not a map link standing in for one."""
    return url if re.match(r"^https?://", url or "") and "openstreetmap.org" not in url else ""


def place_card(rows: list[tuple], q: str) -> dict | None:
    """PLACE_EXACT_SQL rows -> the nearest one, gaps filled from its duplicates:
    {name, what, street, city, phone, hours, website, own_site, map, osm, miles}."""
    if not rows:
        return None
    osm, name, kind, street, city, hours, website, phone, lat, lon, d_km = rows[0]
    fill = {"street": street, "city": city, "hours": hours, "website": _web(website), "phone": phone}
    src = ("wdc" if osm.startswith("wdc:") else "osm") if hours else ""
    osm_id = osm if not osm.startswith("wdc:") else ""
    for o, _n, _k, st, ci, ho, we, ph, la, lo, _d in rows[1:]:
        if 111.32 * math.hypot(la - lat, (lo - lon) * math.cos(math.radians(lat))) > PLACE_SAME_KM:
            continue
        if not fill["hours"] and ho:
            src = "wdc" if o.startswith("wdc:") else "osm"
        for key, v in (("street", st), ("city", ci), ("hours", ho), ("website", _web(we)), ("phone", ph)):
            fill[key] = fill[key] or v or ""
        osm_id = osm_id or (o if not o.startswith("wdc:") else "")
    what = kind.split("=", 1)[-1]
    what = "" if what == "LocalBusiness" else re.sub(r"(?<=[a-z])(?=[A-Z])", " ", what).replace("_", " ").lower()
    host = (urlsplit(fill["website"]).hostname or "").lower().removeprefix("www.") if fill["website"] else ""
    label = norm_key(host.split(".")[0]) if host else ""
    own = len(label) >= 3 and label in (norm_key(q), norm_key(name))
    return {"name": name, "what": what, "kind": kind, "street": fill["street"] or "", "city": fill["city"] or "",
            "phone": fill["phone"] or "", "hours": fill["hours"] or "", "hours_src": src, "website": fill["website"] or "",
            "own_site": f"https://{host}/" if own else "",
            "map": f"https://www.openstreetmap.org/?mlat={lat:.5f}&mlon={lon:.5f}#map=17/{lat:.5f}/{lon:.5f}",
            "osm": f"https://www.openstreetmap.org/{osm_id}" if osm_id else "",
            "miles": round(float(d_km) * 0.621371, 1), "lat": round(float(lat), 5), "lon": round(float(lon), 5),
            # three or more of that name around: a chain, and this is the nearest branch
            "chain": sum(1 for r in rows if 111.32 * math.hypot(r[8] - lat, (r[9] - lon) * math.cos(math.radians(lat)))
                         > PLACE_SAME_KM) >= 2,
            "branches": place_branches(rows)}


BRANCH_MAX, BRANCH_KM = 3, 48.28     # up to 3 locations within 30 miles


def place_branches(rows: list[tuple]) -> list[dict]:
    """PLACE_EXACT_SQL rows (nearest first) -> up to BRANCH_MAX distinct locations — the
    nearest always, others within BRANCH_KM — each with its address, hours, phone, point;
    duplicates of one location (OSM + a business listing) fill each other's gaps."""
    out: list[dict] = []
    for o, _n, _k, st, ci, ho, _we, ph, la, lo, d in rows:
        src = ("wdc" if o.startswith("wdc:") else "osm") if ho else ""
        same = next((b for b in out if 111.32 * math.hypot(la - b["lat"], (lo - b["lon"]) * math.cos(math.radians(la)))
                     <= PLACE_SAME_KM), None)
        if same:
            if not same["hours"] and ho:
                same["hours_src"] = src
            for key, v in (("street", st), ("city", ci), ("hours", ho), ("phone", ph)):
                same[key] = same[key] or v or ""
            continue
        if len(out) >= BRANCH_MAX or (out and float(d) > BRANCH_KM):
            continue
        out.append({"street": st or "", "city": ci or "", "hours": ho or "", "hours_src": src, "phone": ph or "",
                    "lat": round(float(la), 5), "lon": round(float(lo), 5), "miles": round(float(d) * 0.621371, 1),
                    "map": f"https://www.openstreetmap.org/?mlat={la:.5f}&mlon={lo:.5f}#map=17/{la:.5f}/{lo:.5f}"})
    return out


# A chain's usual hours, from its own pages (2026-09-25, "this says open 24 hours but it's
# closed": OpenStreetMap still tags 158 Walmarts 24/7 — pre-2020, one "checked" in Oct 2025
# — while every one of Walmart's own 28 listings with hours says 6 AM-11 PM). The chain's own
# pages are the norm: since 2026-10-03 the hours its site publishes per branch (hours_web,
# Common Crawl — Web Data Commons, the old source, is research-only). US-only data for now
# (owner, 2026-10-03): a whole store locator is a database-right question in the EU
# (docs/legal/PLACES-AND-HOURS-LICENSING.md §5).
CHAIN_HOURS_SQL = """SELECT w.hours FROM hours_web w JOIN places p ON p.osm = w.osm
WHERE regexp_replace(lower(p.name), '[^a-z0-9]', '', 'g') = ANY(%(keys)s) AND w.hours <> ''
LIMIT 400"""
CHAIN_MIN, CHAIN_SHARE = 5, 0.6
# Agreement strong enough to overrule a branch's map hours too, not only a stale 24/7: the
# Twin Falls Walmart is mapped 7 AM-11 PM, and it opens at 6 like the chain's 24 own
# listings say. A branch's own listing still beats the chain's norm.
CHAIN_STRONG_MIN, CHAIN_STRONG_SHARE = 10, 0.8
_RANGE = re.compile(r"(\d{1,2})(?::(\d{2}))?\s*([ap])?\.?\s*m?\.?\s*[-\u2013\u2014]\s*(\d{1,2})(?::(\d{2}))?\s*([ap])?", re.I)


def hours_sig(h: str) -> str:
    """Hours in any spelling -> what they say, comparable: "24h", or the distinct time
    ranges ("06:00-23:00"); "" for nothing readable ("_:n21…" is a crawl artefact)."""
    t = (h or "").strip()
    if not t or t.startswith("_:"):
        return ""
    if re.search(r"24/7|24 ?hours|00:00\s*-\s*24:00", t, re.I):
        return "24h"

    def hm(hh, mm, ap):
        v = int(hh) % 12 + (12 if ap and ap.lower() == "p" else 0) if ap else int(hh)
        return f"{v:02d}:{mm or '00'}"
    ranges = {f"{hm(a, b, c)}-{hm(d, e, f)}" for a, b, c, d, e, f in _RANGE.findall(t)}
    return ",".join(sorted(ranges))


def chain_hours(listed: list[str], strong: bool = False) -> str | None:
    """The chain's usual hours (one representative spelling) when its own listings agree:
    at least CHAIN_MIN of them, CHAIN_SHARE saying the same (strong: CHAIN_STRONG_*); else None."""
    lo_n, lo_share = (CHAIN_STRONG_MIN, CHAIN_STRONG_SHARE) if strong else (CHAIN_MIN, CHAIN_SHARE)
    sigs = [(hours_sig(h), h) for h in listed]
    sigs = [(g, h) for g, h in sigs if g]
    if len(sigs) < lo_n:
        return None
    counts: dict[str, int] = {}
    for g, _ in sigs:
        counts[g] = counts.get(g, 0) + 1
    best, n = max(counts.items(), key=lambda kv: kv[1])
    if n / len(sigs) < lo_share:
        return None
    return next(h for g, h in sigs if g == best)


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


def chain_check(pl: dict, usual: str | None, strong: bool = False) -> dict:
    """A branch whose own entry says 24 hours (or nothing) against its chain's usual hours —
    or, when the chain agrees strongly, any map hours that differ — gets the usual hours,
    marked as such, with what the entry claimed; for the place and each listed branch. The
    branch's own listing on the chain's site (hours_src "wdc") is never overruled."""
    if not usual or not pl.get("chain"):
        return pl
    norm = hours_sig(usual)

    def fix(b: dict) -> dict:
        mine = hours_sig(b.get("hours") or "")
        if (mine == "" or (mine == "24h" and norm != "24h")
                or (strong and b.get("hours_src") == "osm" and mine != norm)):
            return dict(b, hours=usual, hours_basis="chain", listed_hours=b.get("hours") or "")
        return b
    out = fix(pl)
    if pl.get("branches"):
        out["branches"] = [fix(dict(b, chain=True)) for b in pl["branches"]]
    return out


# A place asked for with its town ("sun spa cedar mills", "wawa twin falls nj": the open place-town set stood
# at 10.9% hit@1 on 2026-10-02 because the whole string went to the name match, and a name of more than four words
# was no place query at all). The last one to three words are tried as the town: the answer is a place of exactly the
# remaining name whose own address names that town, wherever it is from home. Nothing is guessed: a split answers
# only when such a place exists, so "pizza hut menu" and "paris texas" fall through as before.
# A place with no city in its address (over half of OpenStreetMap's) is found by where it lies instead, on the release
# engine: inside the circle its town's addressed places draw (tools/sqlite_lookups/towns.py), and only when no place
# of that name says the town outright. Limit: of a chain's branches in one town only the 40 nearest home are read.
PLACE_TOWN_SQL = """
SELECT osm, name, kind, street, city, hours, website, phone, lat, lon, d_km FROM (
    SELECT p.*, 111.32 * sqrt(power(p.lat - %(lat)s, 2)
                              + power((p.lon - %(lon)s) * cos(radians(%(lat)s)), 2)) AS d_km
    FROM places p
    WHERE (regexp_replace(lower(p.name), '[^a-z0-9]', '', 'g') = ANY(%(keys)s)
           OR (p.brand <> '' AND regexp_replace(lower(p.brand), '[^a-z0-9]', '', 'g') = ANY(%(keys)s)))
      AND regexp_replace(lower(p.city), '[^a-z0-9]', '', 'g') = ANY(%(towns)s)) x
ORDER BY d_km LIMIT 40
"""
PLACE_TOWN_WORDS = 3         # "south harbor park": the longest town tried
PLACE_TOWN_MAX_WORDS = 8     # name (4) + town (3) + a state code; longer is a sentence, not a place
_TOWN_TYPES = ("township", "city", "borough", "village", "town")


def town_keys(town: str) -> list[str]:
    """The keys an address may spell a town by: "riverside" also finds city = "Riverside Township",
    "riverside twp" finds "Riverside"."""
    bare = norm_key(_TOWN_TYPE.sub("", place_expand(town.lower())))
    return list(dict.fromkeys([norm_key(town), bare] + [bare + t for t in _TOWN_TYPES])) if len(bare) >= 3 else []


def _stated(t: str) -> tuple[str, bool]:
    """"wawa twin falls nj" -> ("wawa twin falls", True): the query without the state it ends in, and whether it
    named one. A state code that is also a word ("sign in", "call me") is no state."""
    tq = town_query(t)
    return (tq[0], True) if tq and not tq[2] and len(tq[0].split()) >= 2 else (t, False)


def place_town_splits(q: str) -> list[tuple[str, str]]:
    """"sun spa cedar mills" -> [("sun spa cedar", "mills"), ("sun spa", "cedar mills"),
    ("king", "spa cedar mills")]: every (name, town) the query could mean, the longest name first. A state at the
    end ("… nj", "…, new jersey") is dropped first — unless it is also a word ("sign in"). [] = not that shape."""
    t = " ".join(q.lower().replace(",", " ").split())
    if _NEAR.search(t) or len(t.split()) > PLACE_TOWN_MAX_WORDS:
        return []
    words, out = _stated(t)[0].split(), []
    for n in range(1, PLACE_TOWN_WORDS + 1):
        name, town = " ".join(words[:-n]), " ".join(words[-n:])
        if not name or len(words) - n > 4:
            continue
        if len(norm_key(name)) >= 3 and len(norm_key(town)) >= 3:
            out.append((name, town))
    return out


def place_town(q: str) -> tuple[str, list[tuple]] | None:
    """(name, PLACE_EXACT_SQL-shaped rows nearest first) for "<name> <town>", or None."""
    here = home_point()
    if here is None:
        return None
    lat, lon = here
    # near_km / far: read by the release engine only (lookups_release.place_town) — a place with no city in its address
    # is taken when it lies where the town is, if the town is within PLACE_EXACT_KM or the query named a state
    far = _stated(" ".join(q.lower().replace(",", " ").split()))[1]
    for name, town in place_town_splits(q):
        towns = town_keys(town)
        rows = _rows(PLACE_TOWN_SQL, {"lat": lat, "lon": lon, "keys": place_keys(name), "towns": towns,
                                      "near_km": PLACE_EXACT_KM, "far": far}) if towns else []
        if rows:
            return name, rows
    return None


def town_reading_far(rows: list[tuple], near_km: float | None, stated: bool) -> bool:
    """"<name> <town>" is not what was asked (2026-10-11): "walmart center" from New Jersey answered with the
    Walmart Supercenter of Center, Texas, above the Walmart Garden Center 6 miles off. The town reading is dropped
    when a place nearby is named by the whole query (near_km: the nearest such), every place it found is farther than
    a named place is looked for (PLACE_EXACT_KM), and the query names no state. "wawa orlando" and "sun spa cedar
    mills" asked from far away keep theirs: nothing nearby is called that. rows: place_town()'s, d_km last."""
    return (not stated and near_km is not None and near_km <= PLACE_EXACT_KM
            and bool(rows) and min(r[10] for r in rows) > PLACE_EXACT_KM)


def place_exact(q: str) -> dict | None:
    """The place named exactly q — or named by q's first words in the town its last words name — or None
    (no home point, not a name, nothing that close)."""
    here = home_point()
    if here is None:
        return None
    pq = place_query(q)
    if pq and pq[0] != "name":
        return None
    lat, lon = here
    rows, keys = [], []
    if pq:
        keys = place_keys(pq[1])
        rows = _rows(PLACE_EXACT_SQL, {"lat": lat, "lon": lon, "keys": keys, "r": PLACE_EXACT_KM})
        if not rows and len(pq[1].split()) >= 2:
            # a name that specific ("parx casino shippensburg") means that place, however far
            rows = _rows(PLACE_EXACT_SQL, {"lat": lat, "lon": lon, "keys": keys, "r": 1e6})
    town = False
    if not rows:
        pt = place_town(q)                                  # "<name> <town>": that place, in that town
        if pt:
            keys, rows, town = place_keys(pt[0]), pt[1], True
    pl = place_card(place_order(rows, keys, backroom=town), q)
    if pl and pl.get("chain"):
        ck = list(dict.fromkeys(keys + [norm_key(pl["name"])]))
        try:
            listed = [h for (h,) in _rows(CHAIN_HOURS_SQL, {"keys": ck})]
        except Exception:                                   # no hours_web in this database yet
            listed = []
        strong = chain_hours(listed, strong=True)
        pl = chain_check(pl, strong or chain_hours(listed), strong=bool(strong))
    return pl


# The middle of the lower 48: only a point to measure from, for a question that has none of its own.
_NOWHERE = (39.83, -98.58)
NAMES_PLACES_MIN = 20         # at 3, "python" was a business with branches (2026-10-11)


def names_places(q: str) -> bool:
    """Does q name a business with branches — NAMES_PLACES_MIN or more places of that name or brand, anywhere?
    Asked for a search that came with no position: the public page then knows the answer depends on where
    the searcher is and may send its area ("t mobile store", "autozone": owner 2026-10-11, a fixed list of
    chains on the page missed them). Exact names only (letters and digits), with and without a trailing
    "store" / "locations"; never a category word, never more than four words."""
    pq = place_query(q)
    if not pq or pq[0] != "name":
        return False
    keys = list(dict.fromkeys(place_keys(pq[1]) + place_keys(place_name_core(pq[1]))))
    try:
        rows = _rows(PLACE_EXACT_SQL, {"lat": _NOWHERE[0], "lon": _NOWHERE[1], "keys": keys, "r": 1e6})
    except Exception:                                       # no places in this database
        return False
    return len({(round(r[8], 3), round(r[9], 3)) for r in rows}) >= NAMES_PLACES_MIN


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


def place_in_card(card: dict | None, pl: dict | None) -> bool:
    """The card is about the place ("Parx Casino and Racing" / PARX Casino), not a namesake
    ("trump" is not a business called Trump)."""
    if not card or not pl:
        return False
    strip = lambda t: re.sub(r"^the\s+", "", t.strip(), flags=re.I)        # "The Home Depot"
    a, b = norm_key(strip(card["title"])), norm_key(strip(pl["name"]))
    return len(b) >= 3 and (a.startswith(b) or b.startswith(a))


PERSON_SQL = "SELECT 1 FROM wiki_facts WHERE title = %(t)s AND fact = 'born' LIMIT 1"


LONE_PLACE_MI = 10           # a one-word name with no card answers with a place this close
ORG_PLACE_MI = 25            # an organisation's lone location farther than this is not "its" place (SoFi, Delaware)
CARD_PLACE_MI = 30           # a card's chain branch farther than this is no help ("apple": a store 66 mi off)
# A practice named after a person ("james bond" got a doctor's office in
# Oklahoma): it joins a card only when the card is about an organisation.
PLACE_PRACTICE = re.compile(r"^(amenity=(doctors|dentist|clinic)|healthcare=|office=|craft=|shop=hairdresser"
                            r"|schema=(Physician|Dentist|Attorney|LegalService|InsuranceAgency|RealEstateAgent"
                            r"|AccountingService|MedicalBusiness|MedicalClinic|FinancialService))")


def place_answer(q: str, card: dict | None, pl: dict | None, person: bool,
                 org: bool = False) -> tuple[dict | None, dict | None]:
    """(card, place) for the page. A person's card never takes a place (a law office called
    "John Smith"); a card about the place takes it; with no card the place is its own card;
    a place named exactly by several words ("parx casino shippensburg") replaces a card
    about something else (the Bensalem casino's article)."""
    if not pl or person or (org and not pl.get("chain") and (pl.get("miles") or 0) > ORG_PLACE_MI):
        return card, None
    if card is not None and ((not org and PLACE_PRACTICE.match(pl.get("kind") or ""))
                             or (pl.get("chain") and (pl.get("miles") or 0) > CARD_PLACE_MI)):
        return card, None
    if card is None:
        # a place as the whole answer: named by several words ("parx casino shippensburg"),
        # or a chain, or close by ("wawa") — not a business called Sun 85 miles off for "sun"
        if len(q.split()) >= 2 or pl.get("chain") or (pl.get("miles") or 0) <= LONE_PLACE_MI:
            return None, pl
        return None, None
    if place_in_card(card, pl) or org:            # an organisation's card keeps its nearest branch
        card["place"] = pl
        if pl["own_site"] and not any(l["title"] == "Official site" for l in card["links"]):
            card["links"].insert(0, {"title": "Official site", "url": pl["own_site"]})
        return card, None
    if len(q.split()) >= 2:
        return None, pl
    return card, None


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


# ---------------------------------------------------------------------------
# Latency (2026-09-24). Profiled cold (40 never-seen queries): p50 247 ms, of which
# typo lookup 67, site keyword 53, semantic wiki 48, wiki keyword 39, two new connections
# 15, places 14 — one after another. Now: pooled connections, the table list cached, the
# independent lists run side by side, and a short result cache for repeated queries
# (the start page asks again on every keystroke).
# ---------------------------------------------------------------------------
POOL_MAX = int(os.environ.get("SEARCH_POOL_MAX", "12"))
_pool: list = []
_pool_lock = threading.Lock()
_EXEC = ThreadPoolExecutor(max_workers=int(os.environ.get("SEARCH_WORKERS", "12")), thread_name_prefix="q")


# 2 s serves people; the box bench (deploy.sh box-api-up) raises it so a cold candidate
# database answers slowly instead of failing the eval — without bulk-warming it, which
# evicts the live index from indexhost's shared cache (live search broke that way, 2026-09-26).
STATEMENT_TIMEOUT_MS = int(os.environ.get("SEARCH_STATEMENT_TIMEOUT_MS", "2000"))


# SEARCH_POSTGRES=off (ADR-063, the home engine): the engine opens no database. Needs the release
# lookups and the Tantivy Wikipedia list; the site/page/anchor lists come from the release only with
# SEARCH_WEB_BACKEND=tantivy-release, meaning search only with SEARCH_SEM_BACKEND=ivf.
POSTGRES = os.environ.get("SEARCH_POSTGRES", "on").strip().lower()
if POSTGRES not in ("on", "off"):
    raise SystemExit(f"SEARCH_POSTGRES={POSTGRES!r}: want on or off")
if POSTGRES == "off" and (os.environ.get("SEARCH_LOOKUP_BACKEND", "postgres").strip().lower() != "release"
                          or WIKI_BACKEND != "tantivy-release"):
    raise SystemExit("SEARCH_POSTGRES=off needs SEARCH_LOOKUP_BACKEND=release and SEARCH_WIKI_BACKEND=tantivy-release")


class NoPostgres(getattr(psycopg, "Error", Exception)):
    """A statement with no release port while SEARCH_POSTGRES=off: handled where a database error is."""


NO_POSTGRES_MISSES: set = set()          # statements with no release port (logged once, answered empty; tests assert none)


def _no_postgres(sql: str) -> NoPostgres:
    key = " ".join(sql.split())[:100]
    if key not in NO_POSTGRES_MISSES:
        NO_POSTGRES_MISSES.add(key)
        print(f"no postgres: statement without a release port: {key}", flush=True)
    return NoPostgres(key)


def release_text_tables() -> frozenset:
    """With no Postgres: the text lists the release serves, under the table names _search asks for.
    `anchors` (the exact-anchor list) only when the release carries anchor-phrases.sqlite."""
    t = {"wiki_docs"}
    if SITES_BACKEND == "tantivy-release":
        t |= {"docs", "serve_sites", "page_docs", "anchor_docs"}
        if getattr(lookups(), "has_anchor_phrases", False):
            t.add("anchors")
    return frozenset(t)


def _new_conn():
    c = psycopg.connect(connect_timeout=2, autocommit=True, options=f"-c statement_timeout={STATEMENT_TIMEOUT_MS}")
    if OWN_RANK_TSV:
        _temp_own_rank(c)
    return c


# SEARCH_SITE_RANK=own on the POSTGRES engine is a compatibility shim: the serving database has no own rank, and
# none is added to it. SEARCH_OWN_RANK_TSV (own_rank.py's own_rank.tsv) is loaded into TEMP tables of every pooled
# connection (own_rank, nav_sites_own, host_nav_own), which vanish with the connection. The release engine needs
# none of this: it reads site.own_rank from signals.bin.
OWN_RANK_TSV = os.environ.get("SEARCH_OWN_RANK_TSV", "")
if SITE_RANK == "own" and os.environ.get("SEARCH_LOOKUP_BACKEND", "postgres").strip().lower() == "postgres" and not OWN_RANK_TSV:
    raise SystemExit("SEARCH_SITE_RANK=own on the Postgres engine needs SEARCH_OWN_RANK_TSV=<own_rank.tsv>")
if SITE_RANK == "own":
    CONFIDENT_RANK = confident_rank_for(True)


def _temp_own_rank(c) -> None:
    c.execute("CREATE TEMP TABLE own_rank (host text PRIMARY KEY, domain text, rank integer, cc_pos integer, basis text)")
    with open(OWN_RANK_TSV, "rb") as f, c.cursor().copy("COPY own_rank FROM STDIN") as cp:
        while chunk := f.read(1 << 20):
            cp.write(chunk)
    c.execute("""CREATE TEMP TABLE nav_sites_own AS
        SELECT o.host, k AS key, o.rank, o.domain FROM own_rank o
        CROSS JOIN LATERAL (SELECT replace(left(o.host, greatest(0, length(o.host) - length(o.domain) - 1)), '.', '') AS sub,
                                   split_part(o.domain, '.', 1) AS dl) x
        CROSS JOIN LATERAL unnest(CASE WHEN o.host = o.domain THEN ARRAY[x.dl] ELSE ARRAY[x.sub || x.dl, x.dl || x.sub] END) k""")
    c.execute("CREATE INDEX ON nav_sites_own (key, rank)")
    c.execute("CREATE INDEX ON nav_sites_own (host)")
    if c.execute("SELECT to_regclass('public.host_nav')").fetchone()[0]:
        c.execute("""CREATE TEMP TABLE host_nav_own AS SELECT h.key, h.url, h.title, h.description, o.rank
            FROM host_nav h JOIN own_rank o ON o.host = split_part(split_part(h.url, '//', 2), '/', 1)""")
    else:
        c.execute("CREATE TEMP TABLE host_nav_own (key text, url text, title text, description text, rank integer)")


# ---------------------------------------------------------------------------
# Lookup backend (T4, ADR-063), beside the text-list interface: every exact / structured
# lookup (places, suggestions, typo correction, cards, facts, accounts, nav, kinds, stocks,
# the tier-4 fuzzy fallbacks) is answered by one backend.
#   SEARCH_LOOKUP_BACKEND=postgres | sqlite   SEARCH_LOOKUP_DB=<lookup.sqlite>
#   SEARCH_TYPO_INDEX=<Tantivy index of the suggestion keys, built beside the file>
# postgres (default, unchanged): the SQL constants below, on the serving database.
# sqlite: lookups_sqlite.SqliteLookups answers the same constants with the same rows; a
# pooled connection becomes a LookupRouter that sends lookups there and everything else (text
# and vector lists) to Postgres. A lookup with no SQLite port raises, never quietly asks
# Postgres (an A/B would compare A with A). sqlite3/tantivy are imported for that backend only.
# ---------------------------------------------------------------------------
LOOKUP_BACKEND = os.environ.get("SEARCH_LOOKUP_BACKEND", "postgres").strip().lower()
# release: a release folder in format v1 (SEARCH-RELEASE-FORMAT.md) — lookup.sqlite with names /
# cards keyed by doc_id, places-osm.sqlite, registry.sqlite, signals.bin, text/typo
RELEASE_DIR = _release_root(os.environ.get("SEARCH_RELEASE_DIR", "/data/release"))   # BASE or BASE/current: the release `current` names at start
LOOKUP_DB = os.environ.get("SEARCH_LOOKUP_DB", "/data/lookup/lookup.sqlite")
TYPO_INDEX = os.environ.get("SEARCH_TYPO_INDEX", "/data/lookup/typo-keys")
_lookups = None
_lookups_lock = threading.Lock()


def lookups():
    """The SQLite lookup backend, opened once; None for Postgres. A file or typo index that
    will not open stops the start."""
    global _lookups, CONFIDENT_RANK
    if LOOKUP_BACKEND == "postgres":
        return None
    if LOOKUP_BACKEND not in ("sqlite", "release"):
        raise ValueError(f"SEARCH_LOOKUP_BACKEND={LOOKUP_BACKEND!r}: want postgres, sqlite or release")
    if _lookups is None:
        with _lookups_lock:
            if _lookups is None:
                if LOOKUP_BACKEND == "release":
                    import lookups_release
                    b = lookups_release.ReleaseLookups(RELEASE_DIR, sys.modules[__name__])
                    print(f"lookups: release {RELEASE_DIR} ({b.release_id}), {len(b.tables)} tables, "
                          f"typo index {b.typo_keys} keys", flush=True)
                    own = bool(getattr(b.sig, "own", False))
                    CONFIDENT_RANK = confident_rank_for(own)
                    print(f"site rank: {'own' if own else 'tranco'}, popular up to rank {CONFIDENT_RANK}", flush=True)
                else:
                    import lookups_sqlite
                    b = lookups_sqlite.SqliteLookups(LOOKUP_DB, sys.modules[__name__], TYPO_INDEX)
                    print(f"lookups: sqlite {LOOKUP_DB}, {len(b.tables)} tables, typo index {TYPO_INDEX} "
                          f"({b.typo_keys} keys)", flush=True)
                _lookups = b
    return _lookups


@contextlib.contextmanager
def _conn():
    """A pooled connection. Session settings (SET pg_trgm…, hnsw.ef_search) are reset on
    return so no query sees another's; a connection that errored is closed, not reused.
    SEARCH_POSTGRES=off: no Postgres at all — every statement goes to the release."""
    if POSTGRES == "off":
        import lookups_sqlite
        yield lookups_sqlite.LookupRouter(None, lookups(), _TABLES_SQL, release_text_tables(),
                                          {_STORED_SQL: [], _GEN_SQL: [(lookups().release_id,)]}, _no_postgres)
        return
    with _pool_lock:
        c = _pool.pop() if _pool else None
    if c is None or c.closed or c.broken:
        c = _new_conn()
    try:
        lk = lookups()
        if lk is None:
            yield c
        else:
            import lookups_sqlite
            yield lookups_sqlite.LookupRouter(c, lk, _TABLES_SQL)
        c.execute("RESET ALL")
    except BaseException:
        c.close()
        raise
    with _pool_lock:
        if len(_pool) < POOL_MAX:
            _pool.append(c)
            return
    c.close()


# Where a request's time goes (2026-09-26): each step adds its milliseconds to the request's
# dict, logged beside ms= as stages=sites:19,ce:41,… — names and numbers only, never the
# query. Steps on worker threads see the dict through a copied context (sub() in _search).
_stages: contextvars.ContextVar = contextvars.ContextVar("stages", default=None)
# ... and the CPU each step's own thread spent (2026-11-03): wall time overlaps across the parallel lists and could
# not say which lists burned a search's 1.1-2.8 CPU seconds on the public server's 4 cores. Logged as cpu=; a step's
# own thread only (work it hands to other threads is theirs, under their own names).
_stages_cpu: contextvars.ContextVar = contextvars.ContextVar("stages_cpu", default=None)


@contextlib.contextmanager
def stage(name: str):
    st, t, sc, c = _stages.get(), time.monotonic(), _stages_cpu.get(), time.thread_time()
    try:
        yield
    finally:
        if st is not None:
            st[name] = st.get(name, 0.0) + (time.monotonic() - t) * 1000
        if sc is not None:
            sc[name] = sc.get(name, 0.0) + (time.thread_time() - c) * 1000


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


def _staged(name: str, fn, *args):
    with stage(name):
        return fn(*args)


def stages_text(st: dict | None) -> str:
    """{"sites": 19.4, "ce": 41.2} -> "ce:41,sites:19", slowest first; "cached" when the
    request did no timed work (a cache hit)."""
    return ",".join(f"{k}:{v:.0f}" for k, v in sorted((st or {}).items(), key=lambda kv: -kv[1])) or "cached"


# How a list was put together, on request (the page's "How this was ranked", 2026-10-08). /explain runs the
# search once more with a list in this variable and each step below adds what it produced. Every other
# request sees None: a step costs it one variable read, and its answer and the result cache are untouched.
_trace: contextvars.ContextVar = contextvars.ContextVar("trace", default=None)
TRACE_ROWS = 10                 # results kept per step
_trace_n: contextvars.ContextVar = contextvars.ContextVar("trace_n", default=TRACE_ROWS)   # /candidates asks for more (sq_candidates.py)
_trace_more: contextvars.ContextVar = contextvars.ContextVar("trace_more", default=None)    # /candidates?text=1: more fields per row (sq_candidates.text_fields)
TRACE_FLAGS = ("official", "popular", "famous", "kind", "namesake", "related", "profile", "stock", "reranked_top")


def trace_rows(rows: list[dict]) -> dict:
    """{n, rows}: how many results a step produced and its first TRACE_ROWS, each cut to what the page draws."""
    more = _trace_more.get()
    return {"n": len(rows), "rows": [
        dict({"url": r["url"], "title": (r.get("title") or "")[:90], "source": r.get("source", ""), "score": r.get("score")},
             **{f: r[f] for f in ("lr", "ce", "pos0") if f in r},
             **({"flags": fl} if (fl := [f for f in TRACE_FLAGS if r.get(f)]) else {}),
             **(more(r) if more else {}))
        for r in rows[:_trace_n.get()]]}


def _tr(step: str, rows: list[dict] | None = None, **info) -> None:
    tr = _trace.get()
    if tr is not None:
        tr.append(dict(info, step=step, **(trace_rows(rows) if rows is not None else {})))


def _tl(name: str, lst: list[dict]) -> list[dict]:
    """One of the lists the fusion blends, noted under its name when a trace is on."""
    _tr("list", lst, name=name)
    return lst


def trace_run(tr: list[dict], q: str) -> list[dict]:
    """The steps of the search that answered: a corrected spelling is searched a second time, and the list
    shown is that run's when it was accepted (its q), else the first."""
    starts = [i for i, s in enumerate(tr) if s["step"] == "start"]
    pick = next((i for i in reversed(starts) if tr[i].get("q") == q), starts[0] if starts else 0)
    end = next((i for i in starts if i > pick), len(tr))
    return tr[pick + 1:end]


def _drop_pool() -> None:
    """After a serve swap every pooled connection is dead (pg_terminate_backend)."""
    global _gen
    with _pool_lock:
        dead, _pool[:] = list(_pool), []
    for c in dead:
        c.close()
    _gen = (_NEVER, None)                        # the new snapshot: cached results are stale


# ---------------------------------------------------------------------------
# The news box (2026-09-25): headlines from the approved feeds (news/poller.py keeps them
# in the `news` database). "iran" -> the latest stories about Iran, one line per story
# with every outlet that ran it. Only when the query really is in the news: all its
# words in the headline or summary, NEWS_MIN_ITEMS of them in NEWS_HOURS.
# ---------------------------------------------------------------------------
NEWS_DB = os.environ.get("SEARCH_NEWS_DB", "news")
NEWS_HOURS = 48
NEWS_HALF_LIFE_H = 12.0        # a story's weight halves every 12 hours
NEWS_MIN_ITEMS = 1              # one story is a box ("include latest even if they don't meet
                               # threshold"); the meaning gate (news_near) still keeps stray headlines out
NEWS_N = 5
NEWS_SAME_STORY = 0.5          # headline word overlap (Jaccard) that makes two items one story
NEWS_SAME_SHARED = 4           # ... or this many shared words that are most (NEWS_SAME_SHORT) of
NEWS_SAME_SHORT = 0.6          # the shorter headline: a long headline and a short one about the
                               # same death (Lakers announcer, 2026-09-25) share 7 of 9 words
# Local outlets (feeds.tsv sections): a mild lift in any news box, and the only source of a
# weather search's stories ("if I type in weather I get local weather, but I
# get rss latest weather stories from all over").
NEWS_LOCAL_SECTIONS = ("local-home", "local-region", "weather")
NEWS_LOCAL_BOOST = 1.5
NEWS_SQL = """
SELECT i.title, i.url, i.summary, i.published, f.name, f.lean, f.section,
       -(i.search_text <@> to_bm25query(%(q)s, 'items_bm25')) AS bm25,
       extract(epoch FROM now() - i.published) / 3600 AS age_h
FROM items i JOIN feeds f ON f.url = i.feed_url
WHERE i.published > now() - make_interval(hours => %(h)s)
ORDER BY i.search_text <@> to_bm25query(%(q)s, 'items_bm25') LIMIT %(n)s
"""
# A person's own sources (⚙ feeds, section "mine"; owner 2026-11-13: "their own rss feeds should go back as far as
# they can"): matched over everything kept, not NEWS_HOURS — a weekly newsletter is never in a 48-hour window.
MINE_SQL = """
SELECT i.title, i.url, i.summary, i.published, f.name, f.lean, f.section,
       -(i.search_text <@> to_bm25query(%(q)s, 'items_bm25')) AS bm25,
       extract(epoch FROM now() - i.published) / 3600 AS age_h
FROM items i JOIN feeds f ON f.url = i.feed_url
WHERE f.section = 'mine'
ORDER BY i.search_text <@> to_bm25query(%(q)s, 'items_bm25') LIMIT %(n)s
"""
MINE_BOX = os.environ.get("SEARCH_MINE_BOX", "1") != "0"
MINE_N = 5
MINE_ROWS = 200
MINE_HALF_LIFE_H = 24.0 * 365      # of two equal matches the newer leads; an old exact match still shows
_news_pool: list = []
# The weather tables (wx_*, the home forecast) live in the news database on a box; a dev engine whose
# NEWS_DB is its own (news_mine: only the feeds ⚙ configures) points SEARCH_WX_DB at the shared one
WX_DB = os.environ.get("SEARCH_WX_DB") or NEWS_DB
_wx_pool: list = []
# A host without Postgres (search.logout.cloud, ADR-063) keeps news + wx_* in one SQLite file that its
# poller and wxall write (NEWS_SQLITE there); every query below has its SQLite form in news_sqlite.
NEWS_BACKEND = os.environ.get("SEARCH_NEWS_BACKEND", "postgres")
NEWS_SQLITE = os.environ.get("SEARCH_NEWS_SQLITE", "")
WX_SQLITE = os.environ.get("SEARCH_WX_SQLITE") or NEWS_SQLITE      # like SEARCH_WX_DB: weather from a shared file
if NEWS_BACKEND not in ("postgres", "sqlite") or (NEWS_BACKEND == "sqlite" and not NEWS_SQLITE):
    raise SystemExit(f"SEARCH_NEWS_BACKEND={NEWS_BACKEND!r}: want postgres, or sqlite with SEARCH_NEWS_SQLITE=<file>")
# A second news file read beside the first: the user's own feeds and personal sources (⚙), when their
# pollers keep them apart from the approved list's file (indexhost's dev engine: state/news-mine.sqlite beside
# indexhost's news.sqlite). Headlines and games only. Unset (the public site, a box with one file): never opened.
# SQLite only: nothing polls into a second Postgres news database any more.
NEWS_EXTRA = os.environ.get("SEARCH_NEWS_SQLITE_EXTRA", "") if NEWS_BACKEND == "sqlite" else ""
_news_extra_pool: list = []


def _news_connect(dbname: str, wx: bool = False):
    """A connection to the news database: Postgres `dbname`, or the news file (one file holds both)."""
    if NEWS_BACKEND == "sqlite":
        import news_sqlite
        return news_sqlite.NewsSqlite(WX_SQLITE if wx else NEWS_SQLITE)
    if POSTGRES == "off":                 # no database at all: no news store unless the news file is set
        raise NoPostgres("news: SEARCH_POSTGRES=off and no SEARCH_NEWS_BACKEND=sqlite")
    return psycopg.connect(dbname=dbname, connect_timeout=2, autocommit=True, options="-c statement_timeout=1500")


def _news_extra_connect():
    import news_sqlite
    return news_sqlite.NewsSqlite(NEWS_EXTRA)


def _news_extra_rows(sql: str, params: dict) -> list:
    """One query on the second news file (NEWS_EXTRA); [] when there is none, or it is absent (no ⚙ feed
    polled yet), older than a table, or failing — the first file's rows then stand alone."""
    if not NEWS_EXTRA:
        return []
    with _pool_lock:
        c = _news_extra_pool.pop() if _news_extra_pool else None
    try:
        if c is None or c.closed or c.broken:
            c = _news_extra_connect()
        rows = c.execute(sql, params).fetchall()
    except psycopg.Error:
        if c is not None:
            c.close()
        return []
    with _pool_lock:
        if len(_news_extra_pool) < 2:
            _news_extra_pool.append(c)
        else:
            c.close()
    return rows


def merge_news_rows(rows: list, extra: list) -> list:
    """NEWS_SQL rows of both files as one list, best BM25 first, one row per item URL: a feed that is in the
    approved list and in ⚙ is stored in both files, and its items must count once (the first file's row stays)."""
    seen = {r[1] for r in rows}
    own = [r for r in extra if not (r[1] in seen or seen.add(r[1]))]
    return sorted([*rows, *own], key=lambda r: -float(r[7])) if own else list(rows)


def merge_game_rows(rows: list, extra: list) -> list:
    """SPORTS_SQL rows of both files, soonest first. Where both answer for a league, the user's own source does
    alone (sports.py does the same in one file: a personal source replaces the built-in one for its league)."""
    own = {r[0] for r in extra}
    return sorted([*(r for r in rows if r[0] not in own), *extra], key=lambda r: r[2]) if extra else list(rows)


def _news_item_rows(q: str, hours: int, n: int) -> list | None:
    """NEWS_SQL rows for q from the news store, with the second file's (NEWS_EXTRA) merged in when one is set."""
    p = {"q": q, "h": hours, "n": n}
    rows = _news_rows(NEWS_SQL, p)
    extra = _news_extra_rows(NEWS_SQL, p)
    return merge_news_rows(rows or [], extra) if extra else rows


_NEWS_STOP = frozenset("a an and the of in on at to for from by with as is are was were be it its this that "
                       "after over into about says said will new how why what who".split())


def _words(text: str) -> list[str]:
    return [w for w in re.split(r"[^\w]+", (text or "").lower()) if w]


def _fold(w: str) -> str:
    """days -> day, talks -> talk: headlines vary the plural."""
    return w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w


# Words that ask FOR news rather than name what it is about ("hurricane news", "election
# results", "iran latest"): headlines rarely contain them, and requiring them emptied the box
# while the database held 80 hurricane and 307 election items (homeserver, 2026-09-30).
_NEWS_ASK = frozenset("news latest today todays tonight breaking headline headlines update updates live "
                      "results result current recent now".split())


_NEWS_ASKS = frozenset("news latest headline headlines breaking".split())


def asks_for_news(q: str) -> bool:
    """"hurricane news", "latest iran": the stories go first on the page, whatever the volume
    (news_hot alone needs 6 items from 3 outlets in 24 h )."""
    return any(w in _NEWS_ASKS for w in _words(q))


def news_want(q: str) -> list[str]:
    """The query words a story must contain, plural-folded; [] = nothing to match on."""
    ws = _words(q)
    want = [w for w in ws if w not in _NEWS_STOP and w not in _NEWS_ASK]
    return [_fold(w) for w in want]


# A query word that headlines say another way: companies announce "Meta Reports Third Quarter
# 2026 Results", never "earnings" (2026-09-30).
# ("results" alone is too loose: "Google tests paying publishers for AI search results").
_NEWS_ALT = {"earning": frozenset({"earning", "quarter", "quarterly", "fiscal"})}


def _news_has(want: list[str], title: str, summary: str) -> bool:
    words = {_fold(w) for w in _words(f"{title} {summary}")}
    return bool(want) and all(w in words or bool(_NEWS_ALT.get(w, frozenset()) & words) for w in want)


def news_story_key(title: str) -> frozenset:
    return frozenset(_fold(w) for w in _words(title) if w not in _NEWS_STOP and len(w) > 1)


def same_story(a: frozenset, b: frozenset) -> bool:
    if not (a and b):
        return False
    shared = len(a & b)
    return (shared / len(a | b) >= NEWS_SAME_STORY
            or (shared >= NEWS_SAME_SHARED and shared / min(len(a), len(b)) >= NEWS_SAME_SHORT))


def outlet(feed_name: str) -> str:
    """"CBS News — Politics" -> "CBS News": one outlet however many of its feeds ran a story."""
    return feed_name.split(" — ", 1)[0].strip()


# The news box shows only when some matched story is near the query in meaning (2026-09-30).
# Word matching still picks and orders the stories. On 61 news queries (indexhost, last 48 h,
# Jev-judged) a 0.60 cosine gate kept every news box (top story right 37/61, as without it)
# and cut boxes on 50 non-news queries from 3 to 1. Filtering stories one by one, or
# ordering them by meaning, lost right top stories (33-36) on broad queries ("nyc news").
NEWS_SEM_GATE = float(os.environ.get("SEARCH_NEWS_SEM_GATE", "0.60"))
NEWS_SEM_TOP = 40                # word matches looked at per query (cached by URL after)
NEWS_SEM_BATCH = 6               # embedded per step; the gate stops at the first near story


# The news window's headlines embedded ahead of searches (2026-11-04): every NEWS_FILL_EVERY_S, the items of the last
# NEWS_HOURS not yet in _news_vecs are embedded in batches, the same text news_near embeds, pausing while a visitor's
# search is in flight. Off: SEARCH_NEWS_FILL_EVERY_S=0.
NEWS_FILL_EVERY_S = float(os.environ.get("SEARCH_NEWS_FILL_EVERY_S", "120"))
NEWS_RECENT_SQL = """
SELECT i.url, i.title, i.summary FROM items i
WHERE i.published > now() - make_interval(hours => %(h)s) ORDER BY i.published DESC LIMIT %(n)s
"""


def news_text(title: str, summary) -> str:
    """What a headline is embedded as (news_near and the fill: one text, so one cache entry)."""
    return f"{title}. {(summary or '')[:300]}"


def news_vec_fill_once(batch: int = 32) -> int:
    """Embed the window's headlines not cached yet; how many were embedded."""
    rows = _news_rows(NEWS_RECENT_SQL, {"h": NEWS_HOURS, "n": _NEWS_VEC_MAX})
    if not rows or embed("warm") is None:
        return 0
    with _news_vec_lock:
        todo = [(u, news_text(t, sm)) for u, t, sm in rows if u not in _news_vecs]
    done = 0
    for i in range(0, len(todo), batch):
        while _busy[0] > 0:                                # a visitor first
            time.sleep(0.05)
        news_vectors(todo[i:i + batch])
        done += len(todo[i:i + batch])
    return done


def news_vec_fill_loop() -> None:
    time.sleep(30)                                         # after the engine's own start-up work
    while True:
        try:
            t = time.monotonic()
            n = news_vec_fill_once()
            if n:
                print(f"news vectors: {n} headlines embedded in {time.monotonic() - t:.1f} s, {len(_news_vecs)} cached", flush=True)
        except Exception as e:                             # the next round tries again
            print(f"news vectors: fill failed: {type(e).__name__}", flush=True)
        time.sleep(NEWS_FILL_EVERY_S)


def news_near(q: str, rows) -> bool:
    """Is any of these rows' headlines near the query in meaning? True when there is no model."""
    if not rows or NEWS_SEM_GATE <= 0:
        return True
    words = [w for w in _words(q) if w not in _NEWS_ASK] or _words(q)
    e = embed(" ".join(words))
    if e is None:
        return True
    np = _embedder.np
    qv = np.asarray(e, dtype=np.float32)
    # small batches, best BM25 first, stop at the first near story: embedding 40 headlines cold
    # took 2.8-3.5 s on homeserver's N95, and one near story is all the gate needs
    for i in range(0, len(rows), NEWS_SEM_BATCH):
        vecs = news_vectors([(r[1], news_text(r[0], r[2])) for r in rows[i:i + NEWS_SEM_BATCH]])
        if not vecs:
            return True
        if max(float(v.astype(np.float32) @ qv) for v in vecs.values()) >= NEWS_SEM_GATE:
            return True
    return False


def rank_news(q: str, rows, n: int = NEWS_N) -> list[dict]:
    """NEWS_SQL rows -> stories, best first: items that name every query word, weighted
    by BM25 and freshness, same-story headlines merged (their outlets listed)."""
    want = news_want(q)
    matched = [r for r in rows if _news_has(want, r[0], r[2])]
    if not news_near(q, matched[:NEWS_SEM_TOP]):              # rows come BM25-ordered
        return []
    items = []
    for title, url, summary, published, source, lean, section, bm25, age_h in matched:
        score = float(bm25) * 0.5 ** (max(0.0, float(age_h)) / NEWS_HALF_LIFE_H)
        if section in NEWS_LOCAL_SECTIONS:
            score *= NEWS_LOCAL_BOOST
        items.append({"title": title, "url": url, "source": outlet(source), "lean": lean, "section": section,
                      "published": published.isoformat(), "score": score, "key": news_story_key(title)})
    if len(items) < NEWS_MIN_ITEMS:
        return []
    stories: list[dict] = []
    for it in sorted(items, key=lambda x: -x["score"]):
        home = next((st for st in stories if same_story(st["key"], it["key"])), None)
        if home is None:
            stories.append(dict(it, sources=[it["source"]]))
        elif it["source"] not in home["sources"]:
            home["sources"].append(it["source"])
    out = []
    for st in stories[:n]:
        st.pop("key")
        st["score"] = round(st["score"], 3)
        out.append(st)
    return out


# In the news right now ("if iran is big in the news right now … latest
# first, but something like heinz ketchup … results first"): this many matching items in the
# last NEWS_HOT_HOURS, from at least NEWS_HOT_OUTLETS different outlets.
NEWS_HOT_ITEMS = 6
NEWS_HOT_OUTLETS = 3
NEWS_HOT_HOURS = 24


def news_hot(q: str, rows) -> bool:
    """NEWS_SQL rows -> whether the query is big in the news now (the page then puts the
    news above the results)."""
    want = news_want(q)
    n, outlets = 0, set()
    for title, url, summary, published, source, lean, section, bm25, age_h in rows:
        if float(age_h) > NEWS_HOT_HOURS:
            continue
        if _news_has(want, title, summary):
            n += 1
            outlets.add(outlet(source))
    return n >= NEWS_HOT_ITEMS and len(outlets) >= NEWS_HOT_OUTLETS


def _news_rows(sql: str, params: dict) -> list | None:
    """Rows from the news database (search-news keeps it; a search only reads it, never
    fetches a feed), or None when it is absent or failing."""
    with _pool_lock:
        c = _news_pool.pop() if _news_pool else None
    try:
        if c is None or c.closed or c.broken:
            c = _news_connect(NEWS_DB)
        rows = c.execute(sql, params).fetchall()
    except psycopg.Error:
        if c is not None:
            c.close()
        return None
    with _pool_lock:
        if len(_news_pool) < 4:
            _news_pool.append(c)
        else:
            c.close()
    return rows


# A weather search's stories: local outlets only, about the weather — the storm on the shore,
# not a delayed race in Kansas. Nothing local = no stories, never the national ones.
WEATHER_NEWS_SQL = """
SELECT i.title, i.url, i.summary, i.published, f.name, f.lean, f.section, 0.0,
       extract(epoch FROM now() - i.published) / 3600 AS age_h
FROM items i JOIN feeds f ON f.url = i.feed_url
WHERE f.section = ANY(%(sec)s) AND i.published > now() - make_interval(hours => %(h)s)
ORDER BY i.published DESC LIMIT 800
"""
WEATHER_WORDS = frozenset("""weather forecast forecasts storm storms stormy rain rainy rainfall snow snowfall
    snowstorm flood floods flooding flooded noreaster wind winds windy gust gusts hurricane tropical
    tornado tornadoes thunderstorm thunderstorms heat heatwave cold freeze freezing frost fog drought
    temperature temperatures humid humidity hail sleet ice icy blizzard lightning tide tides surf erosion
    downpour downpours showers drizzle meteorologist nws""".split())


# The area a regional outlet has to name for its story to count as local: big-city stations also
# run the wire's national weather. Home-area outlets (local-home, weather) need no mention.
# SEARCH_LOCAL_AREA is a regular expression of the names of the box's own area, for example
# \b(springfield|example county)\b; unset, no story counts as local by a name in it.
LOCAL_AREA = re.compile(os.environ.get("SEARCH_LOCAL_AREA") or r"(?!x)x", re.I)


def rank_weather_news(rows, n: int = NEWS_N, town: str = "") -> list[dict]:
    """WEATHER_NEWS_SQL rows -> local weather stories, best first: weather words in the
    headline or summary; a regional outlet's story only when it names the area; naming it
    (or the home town) lifts a story; freshness-weighted, same-story headlines merged."""
    items = []
    town = town.strip().lower()
    for title, url, summary, published, source, lean, section, _bm25, age_h in rows:
        text = f"{title} {summary}"
        named = bool(LOCAL_AREA.search(text)) or bool(town and town in text.lower())
        if section == "local-region" and not named:
            continue
        words = set(_words(text))
        hits = words & WEATHER_WORDS
        if {"nor", "easter"} <= words:                     # nor'easter splits into two words
            hits = hits | {"noreaster"}
        if not hits:
            continue
        score = (1 + len(hits) + (1 if section == "weather" else 0) + (2 if named else 0)) \
            * 0.5 ** (max(0.0, float(age_h)) / NEWS_HALF_LIFE_H)
        items.append({"title": title, "url": url, "source": outlet(source), "lean": lean, "section": section,
                      "published": published.isoformat(), "score": score, "key": news_story_key(title)})
    stories: list[dict] = []
    for it in sorted(items, key=lambda x: -x["score"]):
        home = next((st for st in stories if same_story(st["key"], it["key"])), None)
        if home is None:
            stories.append(dict(it, sources=[it["source"]]))
        elif it["source"] not in home["sources"]:
            home["sources"].append(it["source"])
    out = []
    for st in stories[:n]:
        st.pop("key")
        st["score"] = round(st["score"], 3)
        out.append(st)
    return out


def weather_news(more: bool = False, town: str = "") -> list[dict]:
    rows = _news_rows(WEATHER_NEWS_SQL, {"sec": list(NEWS_LOCAL_SECTIONS), "h": NEWS_MORE_HOURS if more else NEWS_HOURS})
    return rank_weather_news(rows or [], NEWS_MORE_N if more else NEWS_N, town)


# "Latest" opened on its own ("results and latest headers should be
# clickable and show a lot more"): a week of stories, up to this many.
NEWS_MORE_N, NEWS_MORE_HOURS, NEWS_MORE_ROWS = 30, 168, 300
# "More news" under the box (2026-10-01): the same week of stories, fetched page by page as the
# list scrolls — never on the instant path. /more?what=news&offset=N slices one cached ranking.
NEWS_PAGE_N, NEWS_PAGED_MAX = 10, 100


def news_week_has_more(q: str, shown: list[dict]) -> bool:
    """Any story in the week's rows that names the query and is not one already shown? Word match
    and story merging only — the meaning gate already passed for this query (the box has a story),
    and one more BM25 read beside the suggestions costs far less than embedding a week."""
    rows = _news_item_rows(q, NEWS_MORE_HOURS, NEWS_MORE_ROWS)
    if not rows:
        return False
    want = news_want(q)
    keys = [news_story_key(s["title"]) for s in shown]
    for title, url, summary, *_ in rows:
        if not _news_has(want, title, summary):
            continue
        k = news_story_key(title)
        if not any(same_story(k, s) for s in keys):
            return True
    return False


def news_week(q: str) -> list[dict]:
    """The "More news" list: the week's stories for q (the box's ranking picks which), newest
    first — the page labels them by day (2026-10-01), and a ranked order made the days jump."""
    every = news(q, more=True, n=NEWS_PAGED_MAX)["stories"]
    return sorted(every, key=lambda st: st.get("published") or "", reverse=True)


MINE_SLOW_S, MINE_OFF_S = 0.4, 600.0
_mine_off = [0.0]                   # monotonic time until which the lookup is off (it was slow, or failed)
_has_mine = [0.0, False]            # [checked at, answer]: asked of the store at most once a minute
HAS_MINE_SQL = "SELECT count(*) FROM feeds WHERE section = 'mine'"


def has_mine() -> bool:
    """Has this engine any feed of the person's own? Most engines (the public site) have none, and must not pay
    a lookup per keystroke to learn it."""
    if time.monotonic() - _has_mine[0] > 60 or not _has_mine[0]:
        n = sum(int(r[0]) for r in [*(_news_rows(HAS_MINE_SQL, {}) or []), *_news_extra_rows(HAS_MINE_SQL, {})])
        _has_mine[:] = [time.monotonic(), n > 0]
    return _has_mine[1]


def mine_items(q: str, skip=()) -> list[dict]:
    """"From your sources": the person's own feed items that hold every query word, any age, best first.
    skip: URLs the news box already shows. [] when there are no own feeds or nothing matches."""
    want = news_want(q)
    if not want or time.monotonic() < _mine_off[0] or not has_mine():
        return []
    p = {"q": q, "n": MINE_ROWS}
    t0 = time.monotonic()
    rows = merge_news_rows(_news_rows(MINE_SQL, p) or [], _news_extra_rows(MINE_SQL, p))
    if time.monotonic() - t0 > MINE_SLOW_S:       # an extra must never slow every search: off for a while, and said
        _mine_off[0] = time.monotonic() + MINE_OFF_S
        print(f"mine: lookup took {time.monotonic() - t0:.2f}s, off for {MINE_OFF_S:.0f}s", flush=True)
    out, seen = [], set(skip)
    for title, url, summary, published, source, _lean, _section, bm25, age_h in rows:
        if url in seen or not _news_has(want, title, summary):
            continue
        seen.add(url)
        out.append({"title": title, "url": url, "source": outlet(source), "published": published.isoformat(),
                    "score": round(float(bm25) * 0.5 ** (max(0.0, float(age_h)) / MINE_HALF_LIFE_H), 3)})
    return sorted(out, key=lambda x: -x["score"])[:MINE_N]


def news(q: str, more: bool = False, n: int | None = None) -> dict:
    """The news box for a query: {stories, hot, more}; no stories when the news database is
    absent or has nothing. more: the longer list for the Latest page (n: how many, default
    NEWS_MORE_N). The box's "more" = the week holds stories the box does not show — the list that
    "More news" opens (one story shown, four more that week, and no button)."""
    q = " ".join(q.split())[:100]
    if len(q) < 3 or len(q.split()) > 6:
        return {"stories": [], "hot": False}
    rows = _news_item_rows(q, NEWS_MORE_HOURS if more else NEWS_HOURS, NEWS_MORE_ROWS if more else 80)
    if rows is None:
        return {"stories": [], "hot": False}
    stories = rank_news(q, rows, (n or NEWS_MORE_N) if more else NEWS_N + 1)
    extra = not more and len(stories) > NEWS_N
    stories = stories if more else stories[:NEWS_N]
    if stories and not more and not extra:          # fewer than a full box: does the week hold others?
        extra = news_week_has_more(q, stories)
    out = {"stories": stories, "hot": bool(stories) and (asks_for_news(q) or news_hot(q, rows)), "more": extra}
    if not more and MINE_BOX:
        try:
            out["mine"] = mine_items(q, [s["url"] for s in stories])
        except Exception as e:                      # an extra: never costs the news box
            _mine_off[0] = time.monotonic() + MINE_OFF_S
            print(f"mine: {type(e).__name__}, off for {MINE_OFF_S:.0f}s", flush=True)
    return out


# ---------------------------------------------------------------------------
# The answer box (2026-09-25, "how old is Jennifer aniston how tall is dean withers"):
# a question about a named thing, answered from its Wikidata facts (wiki_facts, facts.py).
# The name must resolve to an article exactly (the same aliases as the exact-name tier);
# with several, the most searched one that has the fact wins.
# ---------------------------------------------------------------------------
_Q = r"(?P<x>.+?)"
_S = r"(?:'?s)?"
QUESTIONS = [                       # (fact kind, pattern) — first match wins
    ("alive", rf"^is {_Q} (?:still )?(?:alive|dead|living)$"),
    ("age", rf"^(?:how old is|how old was|what age is|what is the age of|age of) {_Q}$"),
    ("age", rf"^{_Q}{_S} age$"),
    ("height", rf"^(?:how tall is|how tall was|how high is|what is the (?:height|elevation) of|height of|elevation of) {_Q}$"),
    ("height", rf"^{_Q}{_S} (?:height|elevation)$"),
    ("birthplace", rf"^where (?:was|is) {_Q} (?:born|from)$"),
    ("birthplace", rf"^{_Q}{_S} (?:birthplace|hometown)$"),
    ("born", rf"^when was {_Q} born$"),
    ("born", rf"^when is {_Q}{_S} birthday$"),
    ("born", rf"^{_Q}{_S} (?:birthday|birth date|date of birth|dob)$"),
    ("died", rf"^when did {_Q} die$"),
    ("died", rf"^{_Q}{_S} (?:death date|date of death)$"),
    ("spouse", rf"^who (?:is|was) {_Q} married to$"),
    ("spouse", rf"^(?:who is )?{_Q}{_S} (?:wife|husband|spouse|partner)$"),
    ("founded", rf"^when (?:was|were) {_Q} (?:founded|established|formed|created)$"),
    ("founded", rf"^{_Q}{_S} (?:founding date|founded)$"),
    ("founder", rf"^who (?:founded|started|created) {_Q}$"),
    ("founder", rf"^{_Q}{_S} founders?$"),
    ("ceo", rf"^(?:who is the ceo of|ceo of|who runs) {_Q}$"),
    ("ceo", rf"^{_Q}{_S} ceo$"),
    ("headquarters", rf"^where (?:is|are) {_Q} (?:headquartered|based)$"),
    ("headquarters", rf"^{_Q}{_S} (?:headquarters|hq)$"),
    ("population", rf"^(?:what is the population of|population of|how many people live in) {_Q}$"),
    ("population", rf"^{_Q}{_S} population$"),
    ("capital", rf"^(?:what is the capital of|capital of) {_Q}$"),
    ("capital", rf"^{_Q}{_S} capital(?: city)?$"),
    ("area", rf"^(?:how big is|what is the area of|area of|size of) {_Q}$"),
    ("released", rf"^when did {_Q} come out$"),
    ("released", rf"^when was {_Q} (?:released|published)$"),
    ("released", rf"^{_Q}{_S} release date$"),
    ("director", rf"^who directed {_Q}$"),
    ("author", rf"^who wrote {_Q}$"),
]
_QUESTIONS = [(k, re.compile(p)) for k, p in QUESTIONS]
# What each question needs from wiki_facts.
QUESTION_FACTS = {"alive": ["born", "died"], "age": ["born", "died"], "height": ["height", "elevation"],
                  "birthplace": ["birthplace", "born"], "born": ["born", "birthplace"],
                  "died": ["died", "born", "deathplace"], "spouse": ["spouse"], "founded": ["founded"],
                  "founder": ["founder"], "ceo": ["ceo"], "headquarters": ["headquarters"],
                  "population": ["population"], "capital": ["capital"], "area": ["area"],
                  "released": ["released"], "director": ["director"], "author": ["author"]}
FACTS_SQL = """
SELECT w.title, f.fact, f.value, f.unit, f.precision, f.at, coalesce(max(si.demand), 0) AS d
FROM wiki_aliases a JOIN wiki_docs w USING (page_id)
JOIN wiki_facts f ON f.title = w.title AND f.fact = ANY(%(facts)s)
LEFT JOIN suggest_items si ON si.wiki = w.title
WHERE a.norm = ANY(%(keys)s)
GROUP BY w.title, f.fact, f.value, f.unit, f.precision, f.at
ORDER BY d DESC, w.title LIMIT 40
"""
FACT_TITLES_SQL = "SELECT DISTINCT w.title FROM wiki_aliases a JOIN wiki_docs w USING (page_id) WHERE a.norm = ANY(%(keys)s) LIMIT 3"
_MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September",
           "October", "November", "December"]


def parse_question(q: str) -> tuple[str, str] | None:
    """"how old is jennifer aniston" -> ("age", "jennifer aniston"); a plain name -> None."""
    t = " ".join(q.lower().strip().rstrip("?!. ").split())
    for kind, rx in _QUESTIONS:
        m = rx.match(t)
        if m and 2 <= len(m.group("x")) <= 80:
            return kind, m.group("x")
    return None


def fact_date(value: str, precision: int | None) -> tuple[dt.date | None, str]:
    """"1969-02-11", 11 -> (date, "February 11, 1969"); a year-precision date says the year only."""
    m = re.match(r"^([+-]?\d{1,6})-(\d\d)-(\d\d)$", value or "")
    if not m:
        return None, value
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    era = " BC" if y < 1 else ""
    y_txt = f"{abs(y) + (1 if y < 1 else 0)}{era}"
    if (precision or 0) >= 11 and mo and d:
        text = f"{_MONTHS[mo - 1]} {d}, {y_txt}"
    elif (precision or 0) == 10 and mo:
        text = f"{_MONTHS[mo - 1]} {y_txt}"
    else:
        text = y_txt
    try:
        return (dt.date(y, mo or 1, d or 1) if y >= 1 else None), text
    except ValueError:
        return None, text


def years_between(a: dt.date, b: dt.date) -> int:
    return b.year - a.year - ((b.month, b.day) < (a.month, a.day))


def fmt_height(value: str, unit: str) -> str | None:
    try:
        v = float(value)
    except ValueError:
        return None
    cm = {"cm": v, "m": v * 100, "in": v * 2.54, "ft": v * 30.48}.get(unit)
    if cm is None or not 30 <= cm <= 300:
        return None
    inches = round(cm / 2.54)
    return f"{cm / 100:.2f} m ({inches // 12} ft {inches % 12} in)"


def fmt_elevation(value: str, unit: str) -> str | None:
    """8849 m -> '8,849 m (29,032 ft)'. Metres or feet only; nothing for a value that is not a number."""
    try:
        v = float(value)
    except ValueError:
        return None
    m = {"m": v, "ft": v * 0.3048}.get(unit)
    if m is None or not -500 <= m <= 9000:
        return None
    return f"{m:,.0f} m ({m / 0.3048:,.0f} ft)"


def fmt_number(value: str) -> str:
    try:
        v = float(value)
    except ValueError:
        return value
    return f"{v:,.0f}" if v >= 100 else f"{v:,.1f}".rstrip("0").rstrip(".")


def fmt_area(value: str, unit: str) -> str | None:
    try:
        v = float(value)
    except ValueError:
        return None
    km2 = {"km2": v, "mi2": v * 2.589988, "ha": v / 100, "m2": v / 1e6}.get(unit)
    if km2 is None:
        return None
    return f"{fmt_number(str(km2))} km² ({fmt_number(str(km2 / 2.589988))} sq mi)"


def build_answer(kind: str, title: str, facts: dict, today: dt.date | None = None) -> dict:
    """facts: fact -> [(value, unit, precision, at)]. -> {title, answer, detail}; answer is
    None when the thing is known but the fact is not on record ("no height on record")."""
    today = today or dt.date.today()
    first = lambda f: (facts.get(f) or [None])[0]              # noqa: E731
    out = {"title": title, "answer": None, "detail": ""}
    born, died = first("born"), first("died")
    b_date, b_txt = fact_date(born[0], born[2]) if born else (None, "")
    d_date, d_txt = fact_date(died[0], died[2]) if died else (None, "")
    exact_b = bool(born and (born[2] or 0) >= 11)
    if kind in ("age", "alive"):
        if not born:
            if died:
                out["answer"], out["detail"] = ("No" if kind == "alive" else f"Died {d_txt}"), f"Died {d_txt}"
            else:
                out["detail"] = "No birth date on record"
            return out
        if died:
            age = years_between(b_date, d_date) if b_date and d_date and exact_b else None
            out["answer"] = ("No" if kind == "alive" else
                             (f"Died at {age}" if age is not None else f"Died {d_txt}"))
            out["detail"] = f"{b_txt} – {d_txt}" + (f" (aged {age})" if age is not None and kind == "alive" else "")
            return out
        if b_date:
            age = years_between(b_date, today)
            txt = f"{age} years old" if exact_b else f"about {age} years old"
            out["answer"] = f"Yes — {txt}" if kind == "alive" else txt
            out["detail"] = f"Born {b_txt}"
        return out
    if kind == "height":
        h = first("height")
        out["answer"] = fmt_height(h[0], h[1]) if h else None
        if not out["answer"]:                                    # not a person's height: a mountain's, a place's
            e = first("elevation")
            out["answer"] = fmt_elevation(e[0], e[1]) if e else None
            if out["answer"]:
                out["detail"] = "above sea level"
        if not out["answer"]:
            out["detail"] = "No height on record"
        return out
    if kind in ("born", "birthplace"):
        place = ", ".join(v[0] for v in (facts.get("birthplace") or [])[:2])
        if kind == "born":
            out["answer"] = b_txt or None
            out["detail"] = " · ".join(x for x in (place and f"in {place}",
                                                   b_date and not died and exact_b and f"{years_between(b_date, today)} years old") if x)
        else:
            out["answer"] = place or None
            out["detail"] = f"Born {b_txt}" if b_txt else ""
        if not out["answer"]:
            out["detail"] = "Not on record"
        return out
    if kind == "died":
        if not died:
            out["detail"] = "No date of death on record" + (" — living" if born else "")
            return out
        out["answer"] = d_txt
        place = ", ".join(v[0] for v in (facts.get("deathplace") or [])[:1])
        age = years_between(b_date, d_date) if b_date and d_date and exact_b else None
        out["detail"] = " · ".join(x for x in (place and f"in {place}", age is not None and f"aged {age}") if x)
        return out
    if kind in ("founded", "released"):
        f = first(kind)
        if f:
            fd, txt = fact_date(f[0], f[2])
            out["answer"] = txt
            if fd and kind == "founded":
                out["detail"] = f"{years_between(fd, today)} years ago"
        else:
            out["detail"] = "Not on record"
        return out
    if kind == "population":
        f = first("population")
        if f:
            out["answer"] = fmt_number(f[0])
            out["detail"] = f"as of {f[3][:4]}" if f[3] else ""
        else:
            out["detail"] = "Not on record"
        return out
    if kind == "area":
        f = first("area")
        out["answer"] = fmt_area(f[0], f[1]) if f else None
        if not out["answer"]:
            out["detail"] = "Not on record"
        return out
    vals = [v[0] for v in facts.get(kind) or []]           # people and places: founder, spouse, ceo …
    out["answer"] = ", ".join(vals[:4]) or None
    if not vals:
        out["detail"] = "Not on record"
    return out


def instant_answer(q: str) -> dict | None:
    """instant_answers.instant(q): arithmetic, unit conversion, the time in a place, a currency conversion. Local
    code only; an engine image without the module (an older build context) simply has no such answers. The rates
    for a currency question come from the news file (fx_rates), read at most once per CACHE_TTL_S and only when
    the query is shaped like one; nothing is ever fetched for a search."""
    try:
        import instant_answers
    except ImportError:
        return None
    wants = getattr(instant_answers, "wants_rates", None)       # an older module: no currency, no error
    if wants is None or not wants(q):
        return instant_answers.instant(q)
    return instant_answers.instant(q, rates=cached(("fx_rates",), lambda: [fx_rates()])[0])


def answer(q: str) -> dict | None:
    """The answer box for a question about a named thing, or None."""
    pq = parse_question(q)
    if not pq:
        return None
    kind, name = pq
    nq = norm_key(name)
    if len(nq) < 2:
        return None
    # "the dark knight" / "dark knight", "the united states" / "united states"
    keys = sorted({nq, nq[3:] if nq.startswith("the") and len(nq) > 5 else nq, "the" + nq})
    with _conn() as c:
        has = tables(c)
        if "wiki_facts" not in has or "wiki_aliases" not in has:
            return None
        rows = c.execute(FACTS_SQL, {"keys": keys, "facts": QUESTION_FACTS[kind]}).fetchall()
        if not rows:
            titles = [t for (t,) in c.execute(FACT_TITLES_SQL, {"keys": keys})]
            if not titles:
                return None
            a = build_answer(kind, titles[0], {})
            a["url"] = _wiki(titles[0], "", 0.0)["url"]
            a["question"] = kind
            return a
    # the most searched article that has the primary fact; else the most searched at all
    primary = QUESTION_FACTS[kind][0]
    title = next((r[0] for r in rows if r[1] == primary), rows[0][0])
    facts: dict = {}
    for t, fact, value, unit, prec, at, _d in rows:
        if t == title:
            facts.setdefault(fact, []).append((value, unit, prec, at))
    a = build_answer(kind, title, facts)
    if ANSWER_DEAD_ON and kind == "alive":
        a = dead_question(q, a, facts)
    a["url"] = _wiki(title, "", 0.0)["url"]
    a["question"] = kind
    return a


# "is freddie mercury dead": the pattern above reads alive / dead / living as one kind and build_answer words
# every answer for "alive", so the question that says "dead" got the opposite word: "No" for a man who died in
# 1991, "Yes — 42 years old" for a living one. SEARCH_ANSWER_DEAD (default on in rc9; =0 is off) words it for the question asked.
ANSWER_DEAD_ON = os.environ.get("SEARCH_ANSWER_DEAD", "1") == "1"


def asks_dead(q: str) -> bool:
    """The alive-kind question whose last word is "dead"."""
    return " ".join(q.lower().strip().rstrip("?!. ").split()).endswith(" dead")


def dead_question(q: str, a: dict, facts: dict) -> dict:
    """build_answer's alive-kind answer, worded for "is X dead": a death date on record -> "Yes — died <date>";
    a living person's "Yes — N years old" -> "No — N years old". Anything else (no answer, an "alive" question)
    is returned as it came."""
    if not asks_dead(q) or not a.get("answer"):
        return a
    died = (facts.get("died") or [None])[0]
    if died:
        a["answer"] = f"Yes — died {fact_date(died[0], died[2])[1]}"
    elif a["answer"].startswith("Yes — "):
        a["answer"] = "No — " + a["answer"][len("Yes — "):]
    return a


# The facts layer (facts.py + facts.sqlite in the release): 100 templated properties and the type+year
# lists, behind SEARCH_FACTS (on by default since rc9, with its three careful flags below and in facts.py; =0 turns it off). It fills the answer box only when the box above left it empty, and
# never touches the result list. A release without facts.sqlite, or a non-release backend, leaves it off.
FACTS_ON = os.environ.get("SEARCH_FACTS", "1") == "1"
_facts_store: list = []


def facts_card(q: str) -> dict | None:
    if not FACTS_ON:
        return None
    lk = lookups()
    if lk is None or lk.name != "release":
        return None
    if not _facts_store:
        import facts
        _facts_store.append(facts.Facts.open(lk.root))
    return _facts_store[0].answer(q, lk) if _facts_store[0] else None


# The office a person holds now, and its own site (2026-11-14, owner: "whitehouse.gov should be up there" for "trump").
# From the facts file: Wikidata's position held (P39), the ones with no end date that have begun; each position's
# article and its official site from the release's cards ("President of the United States" -> whitehouse.gov). The
# row goes just under the person's article when the card is that person. A former office gives nothing ("biden").
# SEARCH_OFFICE_SITES=0 turns it off.
OFFICE_SITES = os.environ.get("SEARCH_OFFICE_SITES", "1") == "1"
OFFICE_MAX = 2


def office_rows(t: str, today: dt.date | None = None) -> list[dict]:
    """Rows for the official sites of the offices the article t's subject holds now (release backend only)."""
    if not (OFFICE_SITES and FACTS_ON):
        return []
    lk = lookups()
    if lk is None or lk.name != "release":
        return []
    if not _facts_store:
        import facts
        _facts_store.append(facts.Facts.open(lk.root))
    fx = _facts_store[0]
    if not fx:
        return []
    import lookups_release as lr
    docs = lk.names1(t, lr.N_TITLE)[:1]
    pid = fx.props and next((k for k, v in fx.props.items() if v["prop"] == "P39"), None)
    if not docs or pid is None:
        return []
    today = today or dt.date.today()
    out, seen = [], set()
    for _v, _unit, _prec, at, until, vdoc in fx.facts_of(docs[0], pid):
        if until or vdoc is None:
            continue                                             # an office held before
        if at and str(at)[:10] > today.isoformat():
            continue                                             # elected, not yet in office
        card = lk.card(vdoc) or {}
        url = (card.get("o") or [None])[0]
        office = lk.title(vdoc)
        if not url or not office or url in seen or not official_ok(url):
            continue
        seen.add(url)
        out.append({"url": url, "title": f"{office} — official site", "content": (card.get("l") or "")[:300],
                    "source": "site", "office": True, "score": 0.0})
        if len(out) >= OFFICE_MAX:
            break
    return out


def with_office_rows(res: list[dict], card: dict | None) -> list[dict]:
    """The offices' sites just under the card's article (or first, when the article is not listed); a site already in
    the list moves up instead of showing twice."""
    if not card or not card.get("title") or not res:
        return res
    try:
        rows = cached(("office", card["title"]), lambda: office_rows(card["title"]), static=True)
    except Exception:  # noqa: BLE001 - an office row is extra: never fail the answer
        return res
    if not rows:
        return res
    urls = {r["url"].rstrip("/") for r in rows}
    rest = [r for r in res if (r.get("url") or "").rstrip("/") not in urls]
    art = next((i for i, r in enumerate(rest) if r.get("source") == "wikipedia"
                and r.get("title", "").rsplit(" — Wikipedia", 1)[0] == card["title"]), None)
    at = (art + 1) if art is not None else 0
    return rest[:at] + [dict(r) for r in rows] + rest[at:]


# Where the older box and the facts file both answer and the older box's value is the build's weaker choice:
# wiki_facts keeps ONE date or amount per fact and picks it without Wikidata's rank (importer/facts.py: the latest
# "point in time", else the most precise, else the smallest), so a rescheduled release keeps its first announced
# date and a city with two areas keeps the smaller; the facts file orders by rank first. SEARCH_FACTS_OVER_BOX=1
# (default off, needs SEARCH_FACTS=1): for these kinds only, the facts card for the SAME article and the matching
# property replaces the older box's answer. Every other kind keeps the older box first.
FACTS_OVER_BOX = os.environ.get("SEARCH_FACTS_OVER_BOX", "1") == "1"
FACTS_OVER = {"released": "P577", "area": "P2046"}


def prefer_facts(ans: dict | None, card: dict | None) -> dict | None:
    """The answer box to show: the facts card when the older box answered one of FACTS_OVER's kinds and the card
    is for the same article and that kind's property; otherwise the older box's answer, untouched."""
    if (FACTS_OVER_BOX and ans and card and card.get("answer") and ans.get("question") in FACTS_OVER
            and card.get("property") == FACTS_OVER[ans["question"]] and card.get("url") == ans.get("url")):
        return card
    return ans


# ---------------------------------------------------------------------------
# The scoreboard (2026-09-25, personal box): "yankees", "jaguars score", "devils game" ->
# the team's game on now (score, inning / period / quarter), or the next one (when, where,
# on what), and a result from the last hours. From news/sports.py's `games` table.
# ---------------------------------------------------------------------------
SPORTS_SQL = """
SELECT league, game_id, start_utc, state, detail, away_name, away_nick, away_score,
       home_name, home_nick, home_score, venue, broadcasts, url,
       away_keys && %(k)s::text[] AS away_hit
FROM games WHERE (away_keys && %(k)s::text[] OR home_keys && %(k)s::text[])
  AND start_utc BETWEEN now() - interval '16 hours' AND now() + interval '8 days'
ORDER BY start_utc LIMIT 60
"""
_SPORTS_TAIL = re.compile(r"\s+(?:score|scores|game|games|schedule|tonight|today|live|next game|tickets|tv|on tv|"
                          r"game time|game times|game tonight|game today|game tomorrow|last game|results|fixtures|"
                          r"next match|match|matches|what time|time)$")
RECENT_FINAL = dt.timedelta(hours=14)
GAMES_PER_TEAM, TEAMS_MAX = 2, 3


# How fans shorten a team's city ("ny giants", "la rams", "sf 49ers").
_CITY = {"ny": "new york", "nyc": "new york", "la": "los angeles", "sf": "san francisco", "nj": "new jersey",
         "kc": "kansas city", "tb": "tampa bay", "gb": "green bay", "ne": "new england", "no": "new orleans",
         "lv": "las vegas", "stl": "st louis", "dc": "washington", "okc": "oklahoma city", "philly": "philadelphia",
         "chi": "chicago", "bos": "boston", "det": "detroit", "phx": "phoenix", "sd": "san diego"}


def sports_keys(q: str) -> list[str]:
    """"jaguars score" -> ["jaguars"]; "new york yankees" -> ["newyorkyankees"]; "ny giants" ->
    ["newyorkgiants"] (a shortened city is written out)."""
    t = " ".join(q.lower().strip().rstrip("?!. ").split())
    while True:                                    # "mets game tonight": both words go
        u = _SPORTS_TAIL.sub("", t)
        if u == t:
            break
        t = u
    first, _, rest = t.partition(" ")
    if rest and first in _CITY:
        t = f"{_CITY[first]} {rest}"
    k = norm_key(t)
    return [k] if len(k) >= 3 else []


def pick_games(rows, now: dt.datetime | None = None) -> list[dict]:
    """SPORTS_SQL rows -> per matched team: its game on now, else a result from the last
    RECENT_FINAL, then its next game; at most GAMES_PER_TEAM per team, TEAMS_MAX teams,
    teams with a game on first."""
    now = now or dt.datetime.now(dt.timezone.utc)
    teams: dict = {}
    for (league, gid, start, state, detail, a_name, a_nick, a_score, h_name, h_nick, h_score,
         venue, tv, url, away_hit) in rows:
        team = (league, a_name if away_hit else h_name)
        teams.setdefault(team, []).append({
            "league": league, "id": gid, "start": start.isoformat(), "state": state, "detail": detail,
            "away": {"name": a_name, "nick": a_nick, "score": a_score},
            "home": {"name": h_name, "nick": h_nick, "score": h_score},
            "venue": venue, "broadcasts": list(tv or []), "url": url, "_t": start})
    picked = []
    for team, games in teams.items():
        live = [g for g in games if g["state"] == "live"]
        recent = [g for g in games if g["state"] == "final" and now - g["_t"] <= RECENT_FINAL]
        ahead = [g for g in games if g["state"] in ("pre", "postponed") and g["_t"] >= now - dt.timedelta(hours=3)]
        chosen = (live or recent[-1:]) + ahead[:1]
        chosen = chosen[:GAMES_PER_TEAM]
        if chosen:
            picked.append((0 if live else 1, chosen[0]["_t"], chosen))
    out = []
    for _p, _t, games in sorted(picked, key=lambda x: (x[0], x[1]))[:TEAMS_MAX]:
        for g in games:
            g.pop("_t", None)
            out.append(g)
    return out


# Which NFL/NBA team the query names ("espn" in /instant). It was a hint for the start page,
# whose browser then asked ESPN for that league's scoreboard (2026-09-25: ESPN blocks the box,
# Akamai 403). The start page no longer does that and start-api drops the field (ESPN saw
# who looked up a team, and when). Still read by sports_no_game and utility/search-answer-eval.py.
BROWSER_TEAM_SQL = """
SELECT i.wiki, i.name, ws.box, coalesce(max(i.demand), 0) AS d
FROM suggest_keys k JOIN suggest_items i USING (id)
JOIN wiki_docs w ON w.title = i.wiki JOIN wiki_sites ws USING (page_id)
WHERE k.key = %(k)s AND k.pos IN (0, 1) AND i.kind = 'wiki'
  AND (ws.box = 'infobox nfl team'
       OR (ws.box IN ('infobox gridiron football team', 'infobox american football team')
           AND EXISTS (SELECT 1 FROM wiki_facts f WHERE f.title = i.wiki
               AND f.fact = 'league' AND f.value = 'National Football League'))
       OR (ws.box = 'infobox basketball club' AND EXISTS (SELECT 1 FROM wiki_facts f WHERE f.title = i.wiki
               AND f.fact = 'league' AND f.value = 'National Basketball Association')))
GROUP BY i.wiki, i.name, ws.box ORDER BY d DESC LIMIT 5
"""


def browser_team(key: str, rows) -> dict | None:
    """BROWSER_TEAM_SQL rows -> {"league": "nfl"|"nba", "team": "Jacksonville Jaguars"} for the
    team the typed words end the name of (the major_team rule), else None."""
    wiki = major_team(key, [(w, n, d) for w, n, _box, d in rows])
    if not wiki:
        return None
    box = next(b for w, _n, b, _d in rows if w == wiki)
    return {"league": "nba" if box == "infobox basketball club" else "nfl",
            "team": re.sub(r"\s*\(.*\)$", "", wiki)}


def browser_scores(q: str) -> dict | None:
    keys = sports_keys(q)
    if not keys:
        return None
    with _conn() as c:
        has = tables(c)
        if "wiki_facts" not in has or "wiki_sites" not in has or "suggest_keys" not in has:
            return None
        return browser_team(keys[0], c.execute(BROWSER_TEAM_SQL, {"k": keys[0]}).fetchall())


# ---------------------------------------------------------------------------
# Weather (2026-09-25): the home forecast news/weather.py keeps in the news DB
# (NWS, MET Norway fallback). Only the home town — a search never causes a request.
# ---------------------------------------------------------------------------
_WX_WHEN = r"(?:\s+(?P<when>today|now|right now|tonight|tomorrow|this weekend|weekend|this week|outside|here))?"
WEATHER_QUESTIONS = [
    ("rain", rf"^(?:will it|is it going to|is it gonna|does it|should i expect)\s+(?P<what>rain|snow|storm|thunderstorm|sleet)(?:\s+(?P<when>today|tonight|tomorrow|this weekend|this week|later))?$"),
    ("rain", rf"^(?:is it|is it still)\s+(?P<what>raining|snowing|storming)(?:\s+(?:outside|now))?$"),
    ("now", rf"^(?:how (?:hot|cold|warm|chilly) is it|(?:what'?s|what is) the (?:temperature|temp|weather)|(?:current )?(?:temperature|temp)|is it (?:hot|cold|warm|windy|nice)){_WX_WHEN}$"),
    ("week", r"^(?:7|seven|10|ten|5|five)[\s-]?day(?:\s+forecast)?$|^(?:extended|weekly)\s+(?:forecast|weather)$"),
    ("forecast", rf"^(?:the\s+)?(?:weather|forecast|weather forecast|local weather)(?:\s+forecast)?{_WX_WHEN}$"),
    ("forecast", r"^(?P<when>today'?s|tonight'?s|tomorrow'?s|weekend|this weekend'?s)\s+(?:weather|forecast)$"),
]
_WEATHER_QUESTIONS = [(k, re.compile(p)) for k, p in WEATHER_QUESTIONS]
WEATHER_SQL = "SELECT payload, source, updated FROM weather WHERE place = 'home'"
WEATHER_STALE = dt.timedelta(hours=2)


def weather_query(q: str) -> str | None:
    """"weather" -> "now"; "will it rain tomorrow" -> "tomorrow"; "7 day forecast" -> "week";
    anything else -> None. The focus tells the page what to lead with."""
    t = " ".join(q.lower().strip().rstrip("?!. ").split())
    for kind, rx in _WEATHER_QUESTIONS:
        m = rx.match(t)
        if not m:
            continue
        when = re.sub(r"'?s$", "", (m.groupdict().get("when") or "").replace("this ", ""))
        when = {"today": "today", "tonight": "tonight", "tomorrow": "tomorrow", "weekend": "weekend",
                "week": "week"}.get(when, "")
        if kind == "week":
            return "week"
        if kind == "rain":
            return when or "rain"
        return when or "now"
    return None


# Any US town (2026-09-25): news/wxall.py keeps the whole country — NBM station
# bulletins, METARs, alerts, Census towns — and a question names the town: "weather in
# chicago", "boston forecast", "will it rain in austin tomorrow". A place that is no town
# means the question was not about weather ("fox weather", "the weather channel").
_WX_WORD = r"(?:weather(?: forecast| report)?|forecast|temperature|temp)"
_WX_WHEN2 = r"(?:\s+(?P<when>today|now|right now|tonight|tomorrow|this weekend|weekend|this week))?"
WEATHER_PLACE = [
    ("forecast", rf"^(?:what'?s |what is |how'?s |how is )?(?:the )?{_WX_WORD}(?: like)?(?: (?:in|for|at|near))? (?P<place>.+?){_WX_WHEN2}$"),
    ("forecast", rf"^(?P<place>.+?)(?:'s)? {_WX_WORD}{_WX_WHEN2}$"),
    ("rain", rf"^(?:will it|is it going to|is it gonna) (?:rain|snow|storm) in (?P<place>.+?)(?:\s+(?P<when>today|tonight|tomorrow|this weekend|this week))?$"),
    ("rain", r"^is it (?:raining|snowing) in (?P<place>.+?)$"),
    # "snow forecast chicago", "rain forecast in seattle tomorrow" (answer eval 2026-10-04: both missed)
    ("rain", rf"^(?:rain|snow|storm|snowfall) {_WX_WORD}(?: (?:in|for|at|near))? (?P<place>.+?){_WX_WHEN2}$"),
    ("now", rf"^(?:how (?:hot|cold|warm) is it|is it (?:hot|cold|warm|nice)) in (?P<place>.+?){_WX_WHEN2}$"),
]
_WEATHER_PLACE = [(k, re.compile(p)) for k, p in WEATHER_PLACE]
_NOT_PLACE = re.compile(r"\b(?:weather|forecast|channel|network|app|radar|underground|girl|girls|report|news|station|"
                        r"service|model|map|maps|com|live|today|tomorrow|tonight)\b"
                        # weather brands, not towns ("fox weather" is the channel, not Fox, PA)
                        r"|^(?:fox|accu|abc|nbc|cbs|cnn|msn|yahoo|google|apple|nws|noaa|local|my|the|bing|wunderground)$")
US_STATES = {"al": "AL", "alabama": "AL", "ak": "AK", "alaska": "AK", "az": "AZ", "arizona": "AZ", "ar": "AR",
             "arkansas": "AR", "ca": "CA", "california": "CA", "co": "CO", "colorado": "CO", "ct": "CT",
             "connecticut": "CT", "de": "DE", "delaware": "DE", "dc": "DC", "fl": "FL", "florida": "FL", "ga": "GA",
             "georgia": "GA", "hi": "HI", "hawaii": "HI", "id": "ID", "idaho": "ID", "il": "IL", "illinois": "IL",
             "in": "IN", "indiana": "IN", "ia": "IA", "iowa": "IA", "ks": "KS", "kansas": "KS", "ky": "KY",
             "kentucky": "KY", "la": "LA", "louisiana": "LA", "me": "ME", "maine": "ME", "md": "MD", "maryland": "MD",
             "ma": "MA", "massachusetts": "MA", "mi": "MI", "michigan": "MI", "mn": "MN", "minnesota": "MN",
             "ms": "MS", "mississippi": "MS", "mo": "MO", "missouri": "MO", "mt": "MT", "montana": "MT", "ne": "NE",
             "nebraska": "NE", "nv": "NV", "nevada": "NV", "nh": "NH", "new hampshire": "NH", "nj": "NJ",
             "new jersey": "NJ", "nm": "NM", "new mexico": "NM", "ny": "NY", "new york": "NY", "nc": "NC",
             "north carolina": "NC", "nd": "ND", "north dakota": "ND", "oh": "OH", "ohio": "OH", "ok": "OK",
             "oklahoma": "OK", "or": "OR", "oregon": "OR", "pa": "PA", "pennsylvania": "PA", "ri": "RI",
             "rhode island": "RI", "sc": "SC", "south carolina": "SC", "sd": "SD", "south dakota": "SD", "tn": "TN",
             "tennessee": "TN", "tx": "TX", "texas": "TX", "ut": "UT", "utah": "UT", "vt": "VT", "vermont": "VT",
             "va": "VA", "virginia": "VA", "wa": "WA", "washington": "WA", "wv": "WV", "west virginia": "WV",
             "wi": "WI", "wisconsin": "WI", "wy": "WY", "wyoming": "WY", "pr": "PR", "puerto rico": "PR"}
WX_TOWN_ALIAS = {"nyc": ("new york", "NY"), "new york city": ("new york", "NY"), "la": ("los angeles", "CA"),
                 "philly": ("philadelphia", "PA"), "dc": ("washington", "DC"), "washington dc": ("washington", "DC"),
                 "vegas": ("las vegas", "NV"), "sf": ("san francisco", "CA"), "nola": ("new orleans", "LA"),
                 "atl": ("atlanta", "GA"), "chi": ("chicago", "IL"), "st louis": ("st. louis", "MO"),
                 "saint louis": ("st. louis", "MO"), "st paul": ("st. paul", "MN"), "saint paul": ("st. paul", "MN")}
WX_TOWN_MIN_POP = 2000        # without a state, "fox" must not answer for Fox, OK (pop 107)
_WHEN_OF = {"today": "today", "now": "now", "right now": "now", "tonight": "tonight", "tomorrow": "tomorrow",
            "this weekend": "weekend", "weekend": "weekend", "this week": "week"}


# A town named with its state ("marsh nj", "paris tx", "springfield il", 2026-09-25): the
# Census towns wxall.py keeps say which one (the biggest of that name in that state), and
# its Wikipedia article is found by the titles Wikipedia gives US municipalities.
STATE_NAME = {abbr: name.title() for name, abbr in US_STATES.items() if len(name) > 2}
STATE_NAME["DC"] = "Washington, D.C."
TOWN_SQL = """SELECT name, kind, county, pop FROM wx_towns WHERE key = %(k)s AND state = %(s)s
ORDER BY pop DESC NULLS LAST LIMIT 1"""
TOWN_TITLE_SQL = "SELECT title FROM wiki_docs WHERE title = ANY(%(t)s)"
# State codes that are also words: "sign in", "log in", "call me" are not towns in Indiana
# and Maine; "portland me", "scranton pa", "tulsa ok" are — a real town is big enough.
TOWN_WORD_CODES = {"in", "or", "me", "ok", "hi", "la", "co", "oh", "id", "ma", "pa", "al", "ar", "mo", "ne",
                   "ut", "wa", "mi", "mt", "ms", "de", "ga"}
TOWN_WORD_MIN_POP = 5000
_TOWN_TYPE = re.compile(r"\s+(?:twp|twsp|township|boro|borough|village|city|town|cdp)$")


def town_query(q: str) -> tuple[str, str, bool] | None:
    """"marsh nj" / "paris, texas" -> ("marsh", "NJ", False); the flag says the state was a
    code that is also a word ("sign in"). Anything else -> None."""
    t = " ".join(q.lower().replace(",", " ").split())
    words = t.split()
    for n in (2, 1):
        tail = " ".join(words[-n:])
        if len(words) > n and tail in US_STATES and len(words) - n <= 4:
            return " ".join(words[:-n]), US_STATES[tail], tail in TOWN_WORD_CODES
    return None


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


def _news_rows(sql: str, params: dict) -> list[tuple]:
    """One query on the news database (wx_*, games …), pooled like weather()."""
    with _pool_lock:
        c = _news_pool.pop() if _news_pool else None
    try:
        if c is None or c.closed or c.broken:
            c = _news_connect(NEWS_DB)
        rows = c.execute(sql, params).fetchall()
    except psycopg.Error:
        if c is not None:
            c.close()
        return []
    with _pool_lock:
        if len(_news_pool) < 4:
            _news_pool.append(c)
        else:
            c.close()
    return rows


def weather_place(q: str) -> tuple[str, str, str | None] | None:
    """-> (focus, place text, state or None) for a weather question naming a place;
    None when it names none (the home questions are weather_query's)."""
    t = " ".join(q.lower().strip().rstrip("?!. ").replace(",", " ").split())
    for kind, rx in _WEATHER_PLACE:
        m = rx.match(t)
        if not m:
            continue
        place = m.group("place").strip()
        if not place or len(place) > 40 or _NOT_PLACE.search(place) or place in ("rain", "snow", "storm", "snowfall"):
            return None
        state = None
        for n in (2, 1):                               # "portland oregon", "springfield il", "new york"
            words = place.split()
            tail = " ".join(words[-n:])
            if len(words) > n and tail in US_STATES:
                state, place = US_STATES[tail], " ".join(words[:-n])
                break
        if place in WX_TOWN_ALIAS and state in (None, WX_TOWN_ALIAS[place][1]):
            place, state = WX_TOWN_ALIAS[place]
        when = _WHEN_OF.get(m.groupdict().get("when") or "", "")
        focus = "week" if when == "week" else when or ("rain" if kind == "rain" else "now")
        return focus, place, state
    return None


WX_TOWN_SQL = """SELECT name, state, lat, lon, pop, counties, tz FROM wx_towns
WHERE key = %(k)s AND (%(s)s::text IS NULL OR state = %(s)s) ORDER BY pop DESC LIMIT 1"""
# With the searcher's position (near=lat,lon, per request, never logged): "weather in springfield" is
# the NEAREST Springfield, not the biggest; a bare "weather" away from home is the nearest town's.
WX_TOWN_NEAR_SQL = """SELECT name, state, lat, lon, pop, counties, tz FROM wx_towns
WHERE key = %(k)s AND (%(s)s::text IS NULL OR state = %(s)s)
  AND (%(s)s::text IS NOT NULL OR pop >= %(p)s OR (abs(lat - %(lat)s) < 0.5 AND abs(lon - %(lon)s) < 0.6))
ORDER BY power(lat - %(lat)s, 2) + power((lon - %(lon)s) * cos(radians(%(lat)s)), 2) LIMIT 1"""
WX_NEAREST_SQL = """SELECT name, state, lat, lon, pop, counties, tz FROM wx_towns
WHERE lat BETWEEN %(lat)s - 0.4 AND %(lat)s + 0.4 AND lon BETWEEN %(lon)s - 0.5 AND %(lon)s + 0.5 AND pop >= %(p)s
ORDER BY power(lat - %(lat)s, 2) + power((lon - %(lon)s) * cos(radians(%(lat)s)), 2) LIMIT 1"""
WX_NEAREST_MIN_POP = 1000
# the searcher's town and county (home_local): the nearest Census town of WX_NEAREST_MIN_POP or more
LOCAL_TOWN_SQL = """SELECT name, state, county FROM wx_towns
WHERE lat BETWEEN %(lat)s - 0.4 AND %(lat)s + 0.4 AND lon BETWEEN %(lon)s - 0.5 AND %(lon)s + 0.5 AND pop >= %(p)s
ORDER BY power(lat - %(lat)s, 2) + power((lon - %(lon)s) * cos(radians(%(lat)s)), 2) LIMIT 1"""
# a town the query names (home_gov_state): one of NAMED_TOWN_MIN_POP or more keeps its own place
NAMED_TOWN_SQL = "SELECT 1 FROM wx_towns WHERE key = ANY(%(k)s) AND pop >= %(p)s LIMIT 1"
NAMED_TOWN_MIN_POP = 10000
WX_AWAY_KM = 40               # nearer to home than this, "weather" is the home forecast


def km_between(a: tuple[float, float], b: tuple[float, float]) -> float:
    dy = (a[0] - b[0]) * 111.2
    dx = (a[1] - b[1]) * 111.2 * math.cos(math.radians((a[0] + b[0]) / 2))
    return math.hypot(dx, dy)


def weather_point(near):
    """The point a bare "weather" is about: the searcher's when they are away from home (or the
    box has no home), else None — the home forecast."""
    return near if near and (HOME is None or km_between(near, HOME) > WX_AWAY_KM) else None


WX_FC_SQL = """SELECT f.id, f.hours, f.days, f.cycle FROM wx_fc f JOIN wx_stations s USING (id)
WHERE s.lat BETWEEN %(lat)s - 1 AND %(lat)s + 1 AND s.lon BETWEEN %(lon)s - 1.3 AND %(lon)s + 1.3
  AND f.hours IS NOT NULL AND f.days IS NOT NULL
ORDER BY power(s.lat - %(lat)s, 2) + power((s.lon - %(lon)s) * cos(radians(%(lat)s)), 2) LIMIT 1"""
WX_OBS_SQL = """SELECT o.obs FROM wx_obs o JOIN wx_stations s USING (id)
WHERE s.lat BETWEEN %(lat)s - 0.5 AND %(lat)s + 0.5 AND s.lon BETWEEN %(lon)s - 0.7 AND %(lon)s + 0.7
  AND o.at > now() - interval '3 hours'
ORDER BY power(s.lat - %(lat)s, 2) + power((s.lon - %(lon)s) * cos(radians(%(lat)s)), 2) LIMIT 1"""
WX_ALERTS_SQL = "SELECT event, severity, headline, ends, same, poly FROM wx_alerts WHERE same && %(c)s OR poly IS NOT NULL"
WX_STALE = dt.timedelta(hours=8)


def _register_news_sqlite() -> None:
    """The SQLite form of each news/weather statement (SEARCH_NEWS_BACKEND=sqlite): same columns,
    same Python types back. Timestamps are Unix seconds there, lists/dicts JSON text."""
    import news_sqlite as ns
    pub = ns.row_with(**{"3": ns.epoch_dt})
    age = "(now_s() - i.published) / 3600.0"
    ns.register(NEWS_SQL, f"""
SELECT i.title, i.url, i.summary, i.published, f.name, f.lean, f.section, -bm25(items_fts) AS bm25, {age}
FROM items_fts JOIN items i ON i.id = items_fts.rowid JOIN feeds f ON f.url = i.feed_url
WHERE items_fts MATCH :m AND i.published > now_s() - :h * 3600
ORDER BY bm25(items_fts) LIMIT :n""",
                params=lambda p: (lambda m: m and {"m": m, "h": p["h"], "n": p["n"]})(ns.fts_query(p["q"], _NEWS_STOP)),
                row=pub)
    ns.register(MINE_SQL, f"""
SELECT i.title, i.url, i.summary, i.published, f.name, f.lean, f.section, -bm25(items_fts) AS bm25, {age}
FROM items_fts JOIN items i ON i.id = items_fts.rowid JOIN feeds f ON f.url = i.feed_url
WHERE items_fts MATCH :m AND f.section = 'mine'
ORDER BY bm25(items_fts) LIMIT :n""",
                params=lambda p: (lambda m: m and {"m": m, "n": p["n"]})(ns.fts_query(p["q"], _NEWS_STOP)),
                row=pub)
    ns.register(HAS_MINE_SQL, HAS_MINE_SQL)
    ns.register(NEWS_RECENT_SQL, """SELECT i.url, i.title, i.summary FROM items i
WHERE i.published > now_s() - :h * 3600 ORDER BY i.published DESC LIMIT :n""")
    ns.register(WEATHER_NEWS_SQL, f"""
SELECT i.title, i.url, i.summary, i.published, f.name, f.lean, f.section, 0.0, {age}
FROM items i JOIN feeds f ON f.url = i.feed_url
WHERE f.section IN (SELECT value FROM json_each(:sec)) AND i.published > now_s() - :h * 3600
ORDER BY i.published DESC LIMIT 800""", row=pub)
    ns.register(TOWN_SQL, """SELECT name, kind, county, pop FROM wx_towns WHERE key = :k AND state = :s
ORDER BY pop DESC NULLS LAST LIMIT 1""")
    town = ns.row_with(**{"5": ns.from_json})
    dist = "power(lat - :lat, 2) + power((lon - :lon) * cos(radians(:lat)), 2)"
    ns.register(WX_TOWN_SQL, """SELECT name, state, lat, lon, pop, counties, tz FROM wx_towns
WHERE key = :k AND (:s IS NULL OR state = :s) ORDER BY pop DESC LIMIT 1""", row=town)
    ns.register(WX_TOWN_NEAR_SQL, f"""SELECT name, state, lat, lon, pop, counties, tz FROM wx_towns
WHERE key = :k AND (:s IS NULL OR state = :s)
  AND (:s IS NOT NULL OR pop >= :p OR (abs(lat - :lat) < 0.5 AND abs(lon - :lon) < 0.6))
ORDER BY {dist} LIMIT 1""", row=town)
    ns.register(WX_NEAREST_SQL, f"""SELECT name, state, lat, lon, pop, counties, tz FROM wx_towns
WHERE lat BETWEEN :lat - 0.4 AND :lat + 0.4 AND lon BETWEEN :lon - 0.5 AND :lon + 0.5 AND pop >= :p
ORDER BY {dist} LIMIT 1""", row=town)
    ns.register(NAMED_TOWN_SQL, "SELECT 1 FROM wx_towns WHERE key IN (SELECT value FROM json_each(:k)) AND pop >= :p LIMIT 1")
    ns.register(LOCAL_TOWN_SQL, f"""SELECT name, state, county FROM wx_towns
WHERE lat BETWEEN :lat - 0.4 AND :lat + 0.4 AND lon BETWEEN :lon - 0.5 AND :lon + 0.5 AND pop >= :p
ORDER BY {dist} LIMIT 1""")
    sdist = "power(s.lat - :lat, 2) + power((s.lon - :lon) * cos(radians(:lat)), 2)"
    ns.register(WX_FC_SQL, f"""SELECT f.id, f.hours, f.days, f.cycle FROM wx_fc f JOIN wx_stations s USING (id)
WHERE s.lat BETWEEN :lat - 1 AND :lat + 1 AND s.lon BETWEEN :lon - 1.3 AND :lon + 1.3
  AND f.hours IS NOT NULL AND f.days IS NOT NULL
ORDER BY {sdist} LIMIT 1""", row=ns.row_with(**{"1": ns.from_json, "2": ns.from_json, "3": ns.epoch_dt}))
    ns.register(WX_OBS_SQL, f"""SELECT o.obs FROM wx_obs o JOIN wx_stations s USING (id)
WHERE s.lat BETWEEN :lat - 0.5 AND :lat + 0.5 AND s.lon BETWEEN :lon - 0.7 AND :lon + 0.7
  AND o.at > now_s() - 3 * 3600
ORDER BY {sdist} LIMIT 1""", row=ns.row_with(**{"0": ns.from_json}))
    overlap = "EXISTS (SELECT 1 FROM json_each({col}) a JOIN json_each(:k) b ON a.value = b.value)"
    ns.register(SPORTS_SQL, f"""
SELECT league, game_id, start_utc, state, detail, away_name, away_nick, away_score,
       home_name, home_nick, home_score, venue, broadcasts, url, {overlap.format(col="away_keys")} AS away_hit
FROM games WHERE ({overlap.format(col="away_keys")} OR {overlap.format(col="home_keys")})
  AND start_utc BETWEEN now_s() - 16 * 3600 AND now_s() + 8 * 86400
ORDER BY start_utc LIMIT 60""", row=ns.row_with(**{"2": ns.epoch_dt, "12": ns.from_json, "14": bool}))
    ns.register(TEAM_SQL, f"SELECT DISTINCT league, name, nick FROM teams WHERE {overlap.format(col='keys')} LIMIT 3")
    ns.register(STANDINGS_SQL, """SELECT grp, rank, team, nick, played, won, drawn, lost, for_, against, points, extra, as_of
FROM standings WHERE league = :l AND (:g IS NULL OR grp = :g) ORDER BY grp, rank""",
                row=ns.row_with(**{"11": ns.from_json}))
    ns.register(FX_SQL, FX_SQL)
    ns.register(WEATHER_SQL, "SELECT payload, source, updated FROM weather WHERE place = 'home'",
                row=ns.row_with(**{"0": ns.from_json, "2": ns.epoch_dt}))
    ns.register(WX_ALERTS_SQL, """SELECT event, severity, headline, ends, same, poly FROM wx_alerts
WHERE EXISTS (SELECT 1 FROM json_each(same) a JOIN json_each(:c) b ON a.value = b.value) OR poly IS NOT NULL""",
                row=ns.row_with(**{"4": ns.from_json, "5": ns.from_json}))

_SEVERITY = {"Extreme": 0, "Severe": 1, "Moderate": 2, "Minor": 3, "Unknown": 4}


def _sky_words(sky, night=False) -> str:
    if sky is None:
        return ""
    for top, day_w, night_w in ((10, "Sunny", "Clear"), (30, "Mostly sunny", "Mostly clear"),
                                (60, "Partly cloudy", "Partly cloudy"), (85, "Mostly cloudy", "Mostly cloudy")):
        if sky < top:
            return night_w if night else day_w
    return "Cloudy"


def wx_words(sky, pop, thunder=None, snow=None, night=False) -> str:
    """Cloud cover and chances -> "Rain likely", "Chance of snow", "Partly cloudy"…"""
    if pop is not None and pop >= 30:
        what = ("thunderstorms" if thunder is not None and thunder >= 30
                else "snow" if snow is not None and snow >= 50 else "rain")
        return f"{what.capitalize()} likely" if pop >= 60 else f"Chance of {what}"
    return _sky_words(sky, night)


def _col(st: dict, lab: str, k: int):
    v = (st.get("rows") or {}).get(lab) or []
    return v[k] if k < len(v) else None


def nbm_hours(st: dict, now: dt.datetime, n: int = 24) -> list[dict]:
    out = []
    for k, iso in enumerate(st.get("valid") or []):
        v = dt.datetime.fromisoformat(iso)
        if v < now - dt.timedelta(hours=1):
            continue
        out.append({"time": iso, "temp_f": _col(st, "TMP", k), "pop": _col(st, "P01", k),
                    "text": wx_words(_col(st, "SKY", k), _col(st, "P01", k), _col(st, "T01", k), _col(st, "PSN", k))})
        if len(out) >= n:
            break
    return out


def nbm_days(st: dict, tz, now: dt.datetime) -> list[dict]:
    """12-hourly columns -> local days: TXN at 00 UTC is the high of the local day it
    ends (evening in the US), at 12 UTC that morning's low; P12 at 00 UTC is the day's
    chance, at 12 UTC the night before's."""
    days: dict[str, dict] = {}
    for k, iso in enumerate(st.get("valid") or []):
        v = dt.datetime.fromisoformat(iso)
        local = v.astimezone(tz) if tz else v
        g = lambda lab: _col(st, lab, k)
        if v.hour == 0:
            d = (local - dt.timedelta(hours=3)).date().isoformat()
            days.setdefault(d, {"date": d}).update(high_f=g("TXN"), pop=g("P12"),
                                                  text=wx_words(g("SKY"), g("P12"), g("T12"), g("PSN")))
        elif v.hour == 12:
            d = local.date().isoformat()
            days.setdefault(d, {"date": d})["low_f"] = g("TXN")
            prev = days.get((local.date() - dt.timedelta(days=1)).isoformat())
            if prev is not None:
                prev["night_text"] = wx_words(g("SKY"), g("P12"), g("T12"), g("PSN"), night=True)
                if g("P12") is not None and (prev.get("pop") is None or g("P12") > prev["pop"]):
                    prev["pop"] = g("P12")
    today = (now.astimezone(tz) if tz else now).date().isoformat()
    out = []
    for d, e in sorted(days.items()):
        if d < today or e.get("high_f") is None:
            continue
        out.append({"name": "Today" if d == today else dt.date.fromisoformat(d).strftime("%A"), "date": d,
                    "high_f": e["high_f"], "low_f": e.get("low_f"), "text": e.get("text", ""),
                    "night_text": e.get("night_text", ""), "pop": e.get("pop"), "detail": ""})
    return out[:7]


def in_polygon(lat: float, lon: float, rings) -> bool:
    """Ray casting on a GeoJSON Polygon ([lon, lat] pairs; holes ignored)."""
    ring = (rings or [[]])[0]
    inside = False
    for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1]):
        if (y1 > lat) != (y2 > lat) and lon < (x2 - x1) * (lat - y1) / ((y2 - y1) or 1e-12) + x1:
            inside = not inside
    return inside


def wx_alerts(rows, lat: float, lon: float, counties: list[str]) -> list[dict]:
    out, seen = [], set()
    hits = [r for r in rows if set(r[4] or []) & set(counties) or (r[5] and in_polygon(lat, lon, r[5]))]
    for event, sev, headline, ends, _same, _poly in sorted(hits, key=lambda r: _SEVERITY.get(r[1], 9)):
        if event not in seen:
            seen.add(event)
            out.append({"event": event, "severity": sev, "headline": headline or "", "ends": ends})
    return out


def town_weather(c, focus: str, place: str, state: str | None, now: dt.datetime, near=None) -> dict | None:
    if near:
        town = c.execute(WX_TOWN_NEAR_SQL, {"k": place, "s": state, "p": WX_TOWN_MIN_POP, "lat": near[0], "lon": near[1]}).fetchone()
    else:
        town = c.execute(WX_TOWN_SQL, {"k": place, "s": state}).fetchone()
    bare = _TOWN_TYPE.sub("", place).strip()
    if not town and bare and bare != place:             # "example township nj": the table says Example
        return town_weather(c, focus, bare, state, now, near)
    # a small place needs its state named — unless it is in the searcher's own neighbourhood (Census keeps
    # no population for unincorporated places: "weather in bayhollow" near Bayhollow is Bayhollow)
    local = bool(near) and town is not None and abs(town[2] - near[0]) < 0.5 and abs(town[3] - near[1]) < 0.6
    if not town or (state is None and (town[4] or 0) < WX_TOWN_MIN_POP and not local):
        return None
    return town_forecast(c, town, focus, now)


def point_weather(c, focus: str, near, now: dt.datetime) -> dict | None:
    """The town nearest to a point (the searcher's position)."""
    town = c.execute(WX_NEAREST_SQL, {"p": WX_NEAREST_MIN_POP, "lat": near[0], "lon": near[1]}).fetchone()
    return town_forecast(c, town, focus, now) if town else None


# Radar: the news poller keeps each NWS radar's last pictures as files beside a station list
# (wxall.py, WX_RADAR_DIR). A town's forecast names the nearest station and nothing else: the page reads
# that station's list of frames itself, so an answer kept in a cache never points at a deleted picture.
RADAR_DIR = os.environ.get("SEARCH_RADAR_DIR", "")          # unset = no radar on the forecast
RADAR_MAX_KM = 230                                          # the picture reaches about this far from its station
_radar_list: dict = {"read": 0.0, "mtime": None, "stations": []}


def radar_station(lat: float, lon: float) -> dict | None:
    """-> {"id", "name"} of the nearest radar within RADAR_MAX_KM, or None (no radar folder, none in reach)."""
    if not RADAR_DIR:
        return None
    if time.time() - _radar_list["read"] > 300:             # the list changes weekly: look at the file every 5 minutes
        _radar_list["read"] = time.time()
        path = os.path.join(RADAR_DIR, "stations.json")
        try:
            mtime = os.path.getmtime(path)
            if mtime != _radar_list["mtime"]:
                with open(path, encoding="utf-8") as f:
                    _radar_list["stations"], _radar_list["mtime"] = json.load(f)["stations"], mtime
        except (OSError, ValueError, KeyError):
            pass
    best, best_km = None, RADAR_MAX_KM
    for s in _radar_list["stations"]:
        try:
            km = 111.2 * math.hypot(s["lat"] - lat, (s["lon"] - lon) * math.cos(math.radians(lat)))
        except (KeyError, TypeError):
            continue
        if km <= best_km and re.fullmatch(r"[A-Z]{4}", str(s.get("id") or "")):
            best, best_km = s, km
    return {"id": best["id"], "name": str(best.get("name") or "")[:40]} if best else None


def town_forecast(c, town, focus: str, now: dt.datetime) -> dict | None:
    name, st, lat, lon, _pop, counties, tzname = town
    fc = c.execute(WX_FC_SQL, {"lat": lat, "lon": lon}).fetchone()
    if not fc:
        return None
    _sid, hours, days, cycle = fc
    obs = c.execute(WX_OBS_SQL, {"lat": lat, "lon": lon}).fetchone()
    alerts = wx_alerts(c.execute(WX_ALERTS_SQL, {"c": list(counties or [])}).fetchall(), lat, lon, list(counties or []))
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(tzname)
    except Exception:
        tz = None
    return {"now": dict(obs[0]) if obs else {"temp_f": None, "text": ""},
            "days": nbm_days(days, tz, now), "hours": nbm_hours(hours, now), "alerts": alerts,
            "place": f"{name}, {st}", "tz": tzname, "source": "nbm",
            "link": f"https://forecast.weather.gov/MapClick.php?lat={lat:.4f}&lon={lon:.4f}",
            "updated": cycle.isoformat(), "focus": focus, "stale": now - cycle > WX_STALE,
            **({"radar": r} if (r := radar_station(lat, lon)) else {})}


def weather(q: str) -> dict | None:
    focus = weather_query(q)
    where = None if focus else weather_place(q)
    if not focus and not where:
        return None
    pool = _news_pool if (WX_SQLITE == NEWS_SQLITE if NEWS_BACKEND == "sqlite" else WX_DB == NEWS_DB) else _wx_pool
    with _pool_lock:
        c = pool.pop() if pool else None
    out = None
    try:
        if c is None or c.closed or c.broken:
            c = _news_connect(WX_DB, wx=True)
        away = weather_point(_near.get())
        if where:
            out = town_weather(c, where[0], where[1], where[2], dt.datetime.now(dt.timezone.utc), _near.get())
        elif away:
            out = point_weather(c, focus, away, dt.datetime.now(dt.timezone.utc))
        else:
            row = c.execute(WEATHER_SQL).fetchone()
            if row:
                payload, _source, updated = row
                out = dict(payload)
                out["focus"] = focus
                out["stale"] = dt.datetime.now(dt.timezone.utc) - updated > WEATHER_STALE
    except psycopg.Error:
        if c is not None:
            c.close()
        return None
    with _pool_lock:
        if len(pool) < 4:
            pool.append(c)
        else:
            c.close()
    return out


# "mets game tonight" when the Mets have nothing in the next 7 days (2026-09-30): the page says so
# instead of showing junk. Only for a query that asks about a game (score / game / schedule /
# tonight …), names exactly one team of a built-in league, and is not an NFL/NBA team the
# browser fetches ("giants game" stays the football Giants' ESPN scores).
TEAM_SQL = "SELECT DISTINCT league, name, nick FROM teams WHERE keys && %(k)s::text[] LIMIT 3"
# League tables (2026-10-05): sports.py computes them into the news file's `standings` table. SQLite only:
# this statement is registered for the file and nowhere else, and standings() never asks Postgres.
STANDINGS_SQL = """SELECT grp, rank, team, nick, played, won, drawn, lost, for_, against, points, extra, as_of
FROM standings WHERE league = %(l)s AND (%(g)s::text IS NULL OR grp = %(g)s) ORDER BY grp, rank"""
# The ECB's daily euro reference rates (2026-10-06): the news poller (news/fx.py) keeps them in the news
# file's fx_rates table. SQLite only, like the standings: with SEARCH_NEWS_BACKEND=postgres there are no rates
# and so no currency answer.
FX_SQL = "SELECT currency, per_eur, as_of, fetched FROM fx_rates"
if NEWS_BACKEND == "sqlite":          # every news/weather/sports statement is defined by here
    _register_news_sqlite()


def fx_rates() -> dict | None:
    """{"date": "2026-10-06", "checked": "2026-10-06T15:12:00Z", "per_eur": {"USD": 1.1269, ...}} from the news
    file, or None: no file, no table yet (an older poller), an empty table, or the Postgres backend. "checked" is
    the poller's last successful fetch of the ECB file (None if the table has none). Never raises, never fetches."""
    if NEWS_BACKEND != "sqlite":
        return None
    try:
        rows = _news_rows(FX_SQL, {})
        per = {str(c): float(r) for c, r, _d, _f in rows if r}
        if not per:
            return None
        at = max((float(f) for _c, _r, _d, f in rows if f), default=None)
        checked = None if at is None else dt.datetime.fromtimestamp(at, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return {"date": max(str(d) for _c, _r, d, _f in rows), "checked": checked, "per_eur": per}
    except Exception:                   # an answer is extra
        return None
NO_GAME_DAYS = 7


def sports_no_game(q: str, games: list, espn) -> dict | None:
    if games or espn:
        return None
    t = " ".join(q.lower().strip().rstrip("?!. ").split())
    if not _SPORTS_TAIL.search(t):
        return None
    keys = sports_keys(q)
    if not keys:
        return None
    rows = _sports_rows(TEAM_SQL, {"k": keys})
    if not rows or len(rows) != 1:
        return None
    league, name, nick = rows[0]
    return {"league": league, "team": name, "nick": nick, "days": NO_GAME_DAYS}


def _sports_rows(sql: str, params: dict) -> list:
    """One query on the news database through the scoreboard's pool; [] when it is unavailable."""
    with _pool_lock:
        c = _news_pool.pop() if _news_pool else None
    try:
        if c is None or c.closed or c.broken:
            c = _news_connect(NEWS_DB)
        rows = c.execute(sql, params).fetchall()
    except psycopg.Error:
        if c is not None:
            c.close()
        return []
    with _pool_lock:
        if len(_news_pool) < 4:
            _news_pool.append(c)
        else:
            c.close()
    return rows


def sports(q: str) -> list[dict]:
    keys = sports_keys(q)
    if not keys:
        return []
    with _pool_lock:
        c = _news_pool.pop() if _news_pool else None
    try:
        if c is None or c.closed or c.broken:
            c = _news_connect(NEWS_DB)
        rows = c.execute(SPORTS_SQL, {"k": keys}).fetchall()
    except psycopg.Error:
        if c is not None:
            c.close()
        return pick_games(_news_extra_rows(SPORTS_SQL, {"k": keys}))     # the user's own sources still answer
    with _pool_lock:
        if len(_news_pool) < 4:
            _news_pool.append(c)
        else:
            c.close()
    return pick_games(merge_game_rows(rows, _news_extra_rows(SPORTS_SQL, {"k": keys})))


# "premier league table", "epl standings", "afc east standings", "nfl standings" -> a league table.
# The whole query must be a league + a table word (and nothing else but "the / current / latest / live /
# now / today"): "premier league tickets", "nfl draft", "table tennis" and "2019 premier league table"
# (a season we do not hold) get no table.
_ST_FILLER = {"the", "current", "latest", "live", "now", "today", "full", "complete"}
_ST_TABLE = r"(?:standings?|table|league table|league standings|rankings?)"
_ST_LEAGUES = {"epl": r"(?:english )?premier league|epl", "championship": r"(?:efl |english )?championship"}
_ST_DIV = r"(?:nfl )?(?P<conf>afc|nfc) (?P<div>east|north|south|west)(?: division)?"
_ST_RX = [(lg, re.compile(rf"^(?:(?:{names}) {_ST_TABLE}|{_ST_TABLE} (?:for )?(?:{names}))$")) for lg, names in _ST_LEAGUES.items()]
_ST_NFL = re.compile(rf"^(?:nfl (?:division )?{_ST_TABLE}|{_ST_TABLE} (?:for )?nfl)$")
_ST_NFL_DIV = [re.compile(rf"^{_ST_DIV} {_ST_TABLE}$"), re.compile(rf"^{_ST_TABLE} (?:for )?{_ST_DIV}$")]
STANDINGS_CAP = {"epl": 20, "championship": 24, "nfl": 4}        # rows per group


def standings_query(q: str) -> tuple[str, str | None] | None:
    """-> (league, group) or None. group is a division ("AFC East") or None for the whole league."""
    t = re.sub(r"[^a-z0-9 ]", " ", q.lower())
    t = " ".join(w for w in t.split() if w not in _ST_FILLER)
    if not t:
        return None
    for lg, rx in _ST_RX:
        if rx.match(t):
            return lg, None
    if _ST_NFL.match(t):
        return "nfl", None
    for rx in _ST_NFL_DIV:
        m = rx.match(t)
        if m:
            return "nfl", f"{m.group('conf').upper()} {m.group('div').title()}"
    return None


def standings(q: str) -> dict | None:
    """The league table the query asks for, from the news file (sports.py computed it from the
    season's results; nothing is fetched here). None on Postgres, with no table yet, or for any
    other query."""
    want = standings_query(q) if len(q) >= 5 else None
    if want is None or NEWS_BACKEND != "sqlite":
        return None
    league, group = want
    rows = cached(("standings", league, group), lambda: [_sports_rows(STANDINGS_SQL, {"l": league, "g": group})])[0]
    if not rows:
        return None
    groups: dict[str, list] = {}
    for grp, rank, team, nick, played, won, drawn, lost, gf, ga, pts, _extra, _as in rows:
        if rank > STANDINGS_CAP[league]:
            continue
        groups.setdefault(grp, []).append({"rank": rank, "team": team, "nick": nick, "played": played, "won": won,
                                           "drawn": drawn, "lost": lost, "for": gf, "against": ga, "points": pts})
    as_of = max(r[12] or "" for r in rows) or None
    title = {"epl": "Premier League table", "championship": "Championship table"}.get(league) or (f"{group} standings" if group else "NFL standings")
    note = (f"Results through {as_of}" if as_of else "Results to date") + (
        "; the table updates weekly." if league != "nfl" else ".")
    return {"league": league, "title": title, "as_of": as_of, "note": note,
            "groups": [{"name": "" if g == "league" else g, "rows": rs} for g, rs in groups.items()]}


TABLES_TTL_S = 60
_TABLES_SQL = ("SELECT tablename FROM pg_tables WHERE tablename IN "
               "('docs', 'wiki_docs', 'serve_sites', 'wiki_aliases', 'wiki_sites', 'se_docs', 'places', 'wiki_vec', "
               "'page_docs', 'host_nav', 'nav_sites', 'subreddits', 'youtube_channels', 'wiki_links', 'suggest_keys', "
               "'suggest_top', 'suggest_phrases', 'wiki_next', 'suggest_items', 'anchor_docs', 'anchors', 'wiki_facts', 'kind_docs', 'kind_members', 'kind_keys', 'kind_vecs', 'd2q_docs', "
               "'stock_tickers', 'stock_keys', 'card_images')")
_TABLE_NAMES = frozenset(re.findall(r"'([a-z_]+)'", _TABLES_SQL))
_tables = (_NEVER, frozenset())


# Full text (docs/internal/SEARCH-BOX-PLAN.md phase 2). The build side stores each table's
# text as a generated search_text column; a box snapshot drops that copy (~11 GB, the
# same text again) and indexes the expression instead. A query names the column where it
# exists and the expression where it does not — the index answers either way.
TEXT_FORMS = {
    "@docs_text@": ("docs", "d.search_text",
                    "(regexp_replace(d.domain, '[.-]+', ' ', 'g') || ' ' || coalesce(d.title, '') || ' ' || "
                    "coalesce(d.description, '') || ' ' || coalesce(d.h1, '') || ' ' || coalesce(d.body, ''))"),
    "@wiki_text@": ("wiki_docs", "search_text",
                    "(title || ' ' || aliases || ' ' || coalesce(infobox, '') || ' ' || coalesce(headings, '') || ' ' || "
                    "coalesce(lead, ''))"),
    "@pages_text@": ("page_docs", "search_text",
                     "(coalesce(title, '') || ' ' || coalesce(description, '') || ' ' || coalesce(h1, '') || ' ' || "
                     "coalesce(body, ''))"),
}
_STORED_SQL = ("SELECT table_name FROM information_schema.columns WHERE table_schema = 'public' "
               "AND column_name = 'search_text'")
_stored = (_NEVER, frozenset())


def stored_text(c) -> frozenset:
    """Tables that still carry a stored search_text column; asked once per TABLES_TTL_S."""
    global _stored
    if time.monotonic() - _stored[0] > TABLES_TTL_S:
        _stored = (time.monotonic(), frozenset(r[0] for r in c.execute(_STORED_SQL)))
    return _stored[1]


def with_text(sql: str, stored: frozenset) -> str:
    for tok, (table, col, expr) in TEXT_FORMS.items():
        sql = sql.replace(tok, col if table in stored else expr)
    return sql


def parse_pages_fuse(qs: dict) -> tuple[int, float] | None:
    """?pk=2&pw=0.5 -> (2, 0.5): the pages list's cut and weight in the fusion, eval A/B only. Either
    left out keeps its setting; nothing given or anything malformed -> None (the settings)."""
    pk, pw = (qs.get("pk") or [""])[0], (qs.get("pw") or [""])[0]
    if not pk and not pw:
        return None
    try:
        k, w = int(pk) if pk else PAGES_FUSE_K, float(pw) if pw else PAGES_FUSE_W
    except ValueError:
        return None
    return (k, w) if 0 <= k <= 20 and 0 <= w <= 4 else None


def parse_off(v: str) -> frozenset:
    """"page_docs, wiki_links" -> the optional tables named in it; anything else is ignored."""
    return frozenset(t for t in (x.strip() for x in v.split(",")) if t in _TABLE_NAMES)


def tables(c) -> frozenset:
    """Which optional tables the serving database has; asked once a minute, not per search."""
    global _tables
    if time.monotonic() - _tables[0] > TABLES_TTL_S:
        _tables = (time.monotonic(), frozenset(r[0] for r in c.execute(_TABLES_SQL)))
    return _tables[1]


def serving_ready() -> bool:
    """True when the serving tables are visible. Until then a search answers 503 "starting",
    never an empty 200: the page and SearXNG already treat a 503 as "try again"."""
    if time.monotonic() - _tables[0] <= TABLES_TTL_S:
        has = _tables[1]
    else:
        try:
            with _conn() as c:
                has = tables(c)
        except Exception:
            return False
    return "wiki_docs" in has or "docs" in has


# Which serving snapshot is live: its database's oid. Every swap renames a fresh copy into
# place (serve_next -> serve, search_next -> search), so the oid changes with it.
_GEN_SQL = "SELECT oid FROM pg_database WHERE datname = current_database()"
_gen = (_NEVER, None)


def generation():
    """The live snapshot's id, asked at most once per TABLES_TTL_S; None while the
    database does not answer."""
    global _gen
    if time.monotonic() - _gen[0] > TABLES_TTL_S:
        try:
            with _conn() as c:
                _gen = (time.monotonic(), c.execute(_GEN_SQL).fetchone()[0])
        except Exception:
            _gen = (_NEVER, None)                # asked again next time
    return _gen[1]


# Results that depend only on the snapshot (searches, places, organisations) are kept
# CACHE_STATIC_TTL_S and dropped once a swap puts a new snapshot live — within
# TABLES_TTL_S. The rest (news, today's ages in answers) keep the short CACHE_TTL_S.
# The static TTL used to be CACHE_TTL_S too, only so that a swap would show (2026-09-26).
CACHE_TTL_S = int(os.environ.get("SEARCH_CACHE_TTL_S", "300"))
CACHE_STATIC_TTL_S = int(os.environ.get("SEARCH_CACHE_STATIC_TTL_S", "21600"))
CACHE_MAX = int(os.environ.get("SEARCH_CACHE_MAX", "4096"))
_cache: OrderedDict = OrderedDict()
_cache_lock = threading.Lock()
# The head-query warmer (warm.py WARM_HEAD, 2026-11-04) asks /instant?warm=1: a static entry past half its life is
# computed again, so a popular name never expires between the warmer's visits (a hit does not renew an entry).
# Only the engine's own network reaches it: the public gateway forwards q/results/full/near/reach and nothing else.
_warm: contextvars.ContextVar = contextvars.ContextVar("warm", default=False)
# Precomputed results on disk (result_store.py, 2026-11-04): SEARCH_STORE=<file> keeps the warmer's searches (only
# warm=1 writes; a visitor's search is never written) under the engine's fingerprint, read after memory and before
# computing. A warm request recomputes a row older than SEARCH_STORE_REFRESH_S; nothing older than
# SEARCH_STORE_MAX_AGE_S is served. audit=1 (with warm=1) computes afresh and compares with the row.
STORE_PATH = os.environ.get("SEARCH_STORE", "")
STORE_MAX_AGE_S = float(os.environ.get("SEARCH_STORE_MAX_AGE_S", "86400"))
STORE_REFRESH_S = float(os.environ.get("SEARCH_STORE_REFRESH_S", "43200"))
STORE_INBOX_EVERY_S = float(os.environ.get("SEARCH_STORE_INBOX_EVERY_S", "60"))
# After a deploy a visitor is served the previous fingerprint's row until the warmer rebuilds it (result_store.py,
# 2026-11-14); 0 = never (a deploy that fixes what the old results got wrong).
STORE_PREV = os.environ.get("SEARCH_STORE_PREV", "1") != "0"
_store = [None, False]                      # the ResultStore, tried
_store_lock = threading.Lock()
_audit: contextvars.ContextVar = contextvars.ContextVar("audit", default=False)
_store_note: contextvars.ContextVar = contextvars.ContextVar("store_note", default=None)   # "disk" / "prev" / "same" / "diff"
_prev_served: contextvars.ContextVar = contextvars.ContextVar("prev_served", default=None)   # a [bool] cached() reads
_busy = [0]                                 # visitors' /instant requests in flight (the warmer waits for 0)
_busy_lock = threading.Lock()


def result_store():
    """The ResultStore, opened on first use; None when SEARCH_STORE is unset or the file cannot be opened."""
    if _store[1]:
        return _store[0]
    with _store_lock:
        if not _store[1]:
            _store[1] = True
            if STORE_PATH:
                try:
                    import result_store as rs
                    models = [d for d in ("/models", NOMIC_DIR, CE_DIR, CE_GOV_DIR) if d and os.path.exists(d)]
                    fp = rs.fingerprint(os.path.dirname(os.path.abspath(__file__)), RELEASE_DIR, models)
                    st = rs.ResultStore(STORE_PATH, fp, STORE_MAX_AGE_S)
                    st.prune()
                    _store[0] = st
                    print(f"result store {STORE_PATH}: fingerprint {fp}", flush=True)
                except Exception as e:
                    print(f"result store {STORE_PATH} off: {type(e).__name__}: {e}", flush=True)
    return _store[0]


# Curated answers (2026-11-05): for the warmed names, a list chosen offline by a large model (Qwen3-Reranker-4B over
# the engine's own 50+ candidates; tools/curated-build.py) and kept in the store by release. The page's list
# (/instant) shows it instead of the engine's ranked list; the card, places and answers are worked out as always.
# /search, which every eval asks, never reads it. SEARCH_CURATED=1 turns it on; the release is SEARCH_RELEASE_ID.
CURATED_RELEASE = (os.environ.get("SEARCH_RELEASE_ID") or "") if os.environ.get("SEARCH_CURATED", "0") == "1" else ""


# A stop or a car park named after the store is not a branch of it (2026-11-06: "Ikea" listed "IKEA · ferry terminal",
# the Red Hook water-taxi stop, between two stores). Dropped only when real branches are listed beside it.
NOT_BRANCH = frozenset(("ferry terminal", "bus stop", "bus station", "station", "train station", "platform", "stop position",
                        "tram stop", "subway entrance", "halt", "taxi", "parking", "parking entrance", "parking space",
                        "bicycle parking", "motorcycle parking"))


def place_kind(r: dict) -> str:
    return (r.get("content") or "").split(" · ", 1)[0].strip().lower()


def branches_only(res: list[dict]) -> list[dict]:
    places = [r for r in res if r.get("source") == "place"]
    if len(places) < 2 or all(place_kind(r) in NOT_BRANCH for r in places):
        return res
    return [r for r in res if not (r.get("source") == "place" and place_kind(r) in NOT_BRANCH)]


def curated_key(q: str) -> str:
    return " ".join((q or "").lower().split())


# Finished answers (2026-11-12, owner: "build the right architecture and retire the twin"). The twin stored the base
# rows of a search under this engine's fingerprint: every deploy emptied it, and a stored row still left the list
# cleanup, the card and the rest to do (measured on public that day: 1.5 s for a first request, about 0.5 s of it the
# base search). A finished answer is everything in an /instant answer that does not depend on the visitor or the
# hour: the list as shown, the card, the place, kinds, intent, the confidence flag. It is built by a copy of this
# engine at home (twin/ship.sh, ?warm=1&fin=1), keyed by release, and only looked up here.
#   SEARCH_FINISHED=1   serve them (off by default). Only for a plain request: q, results, full and nothing else, no
#                       position. Any other parameter, or a position, takes the live path.
#   A hand-picked list written, changed or removed after the answer was built makes the answer stale: live path.
# Suggestions, phrases, news, scores, weather and the answer box are always computed for the request.
FINISHED = os.environ.get("SEARCH_FINISHED", "").strip() == "1"
FIN_V = 1                                                   # the layout of a stored answer; a change retires them all
FIN_MAX_AGE_S = float(os.environ.get("SEARCH_FINISHED_MAX_AGE_S", str(21 * 86400)))
FIN_PLAIN = frozenset(("q", "results", "full", "warm", "fin"))
FIN_FIELDS = ("results", "confident", "card", "place", "intent", "kinds", "local", "sig")


def fin_key(q: str, full: bool) -> str:
    return curated_key(q) + ("\x1ffull" if full else "")


# The store's own search for names that came from that store's search suggestions (owner 2026-11-12:
# "if keywords are from amazon can we just include the search as https://www.amazon.com/s?k=%s"). Product names are
# where the index is thinnest (Clef, relevant rows in the top 8: Amazon product searches 2.2, Wikipedia names 5.5).
# A plain link: nothing is fetched or sent unless the visitor follows it; no affiliate tag. Added as the answer
# leaves, after the store, so stored and finished answers stay as they were built and unsetting the file removes it.
SHOP_NAMES = os.environ.get("SEARCH_SHOP_NAMES", "")        # a file, one name per line (the queue names whose source is amazonqac); unset = off
_shop_names: dict = {"set": None}


def shop_names() -> frozenset:
    if _shop_names["set"] is None:
        try:
            with open(SHOP_NAMES, encoding="utf-8") as f:
                _shop_names["set"] = frozenset(" ".join(ln.lower().split()) for ln in f if ln.strip())
        except OSError as e:
            print(f"shop names: {type(e).__name__}: {e} (no store row)", flush=True)
            _shop_names["set"] = frozenset()
    return _shop_names["set"]


# Two stores, each an ordinary result row (owner 2026-11-12: "go with A ... amazon and walmart should swap positions
# randomly and evenly"). Which comes first is a coin toss on the name and the day: even across names and over time,
# and the same for every request of one search in a day, so the list does not reshuffle between typing and Enter.
SHOP_STORES = (("Amazon", "https://www.amazon.com/s?k="), ("Walmart", "https://www.walmart.com/search?q="))


def shop_rows(k: str, day: str | None = None) -> list[dict]:
    import hashlib
    day = day or dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    stores = SHOP_STORES[::-1] if hashlib.sha256(f"{k}|{day}".encode()).digest()[0] & 1 else SHOP_STORES
    return [{"url": url + quote(k, safe=""), "title": f"Search {name} for \u201c{k}\u201d",
             "content": f"Opens {name}'s own search results for this.", "source": "shop"} for name, url in stores]


# A name's label from the department sorter (result_store.dept): d3:4b's "is this shopped for" and its
# department, tested on 499 held-back names (2026-11-13: 292 of the 299 unseen right, no store box on a non-product
# at 0.7). A labelled name gets the store rows only at SHOP_MIN or more; a name not labelled yet falls back to the
# list. The plural or singular of a labelled name counts ("end mill" is labelled as "end mills").
SHOP_MIN = float(os.environ.get("SEARCH_SHOP_MIN", "0.7"))
SHOP_NAV = re.compile(r"\b(lists? and registr\w*|wish ?lists?|my orders?|track (my )?order|customer service|return policy|my account|sign in|log ?in)\b")


def shop_label(k: str):
    """(p, department, confidence) for a name, or None when the store holds no label for it."""
    st = result_store()
    if st is None:
        return None
    try:
        for form in (k, k + "s", k[:-1] if k.endswith("s") else ""):
            if form and (r := st.get_dept(form)) is not None:
                return r
    except Exception:  # noqa: BLE001 - a label is extra
        pass
    return None


# The buy section (mockup approved 2026-11-12, owner 2026-11-13: "no card like we mocked up"): for a name
# the sorter labelled a product, buttons to stores' own searches chosen by its department, in place of the two rows.
# Three stores: one that specialises in the department when the table has one switched on, the rest drawn evenly from
# every store that fits, by a coin toss on the name and the day (even over names and days, the same all day). Then
# "Nearby": our own search for that kind of shop. Only stores whose link a person has checked are switched on.
SHOP_BOX = os.environ.get("SEARCH_SHOP_BOX", "") == "1"
_shop_table: dict = {}


def shop_table() -> dict:
    if not _shop_table:
        try:
            with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "shop_stores.json"), encoding="utf-8") as f:
                _shop_table.update(json.load(f))
        except (OSError, ValueError) as e:
            print(f"shop stores: {type(e).__name__}: {e} (no buy section)", flush=True)
            _shop_table.update(stores=[], others=[], near={})
    return _shop_table


def _brand_norm(text: str) -> list[str]:
    """Words of a name as the brand list spells them: lower case, "lowe's" -> "lowes", "up&up" -> "up up"."""
    return re.sub(r"[^a-z0-9]+", " ", str(text).lower().replace("'", "").replace("\u2019", "")).split()


def shop_brand(k: str, t: dict) -> tuple[str, str, list[dict], list[str] | None] | None:
    """(brand name, its default department, the stores that sell it, the departments it is not confused in) for a name
    that contains a brand the table knows, else None. The stores come from the table's "brands" entries that are on, and
    only those stores that are themselves on. Whole words, longest brand first: "great value" is found in "great value
    paper towels", "up up" is not found in "makeup up"."""
    # a possessive ("ryobi's") is read both ways: as the brand and an s ("ryobis"), and as the brand alone
    variants = [w for w in (_brand_norm(k), _brand_norm(re.sub(r"['\u2019]s\b", "", k))) if w]
    if not variants:
        return None
    by_id = {s["id"]: s for s in t.get("stores", []) if s.get("on")}
    best = None
    for e in t.get("brands", []):
        if not e.get("on"):
            continue
        for form in [e["brand"], *e.get("aka", [])]:
            f = _brand_norm(form)
            n = len(f)
            if n and any(w[i:i + n] == f for w in variants for i in range(len(w) - n + 1)) and (best is None or n > best[0]):
                best = (n, e)
    if best is None:
        return None
    e = best[1]
    stores = [by_id[i] for i in e.get("stores", []) if i in by_id][:2]
    if not stores:
        return None
    return e.get("name") or e["brand"].title(), e.get("dept", ""), stores, e.get("depts")


def shop_box(q: str, day: str | None = None) -> dict | None:
    """{"q", "department", "buttons": [{"kind", "name", "sub", "url"}]} for a labelled product name, else None.
    A name that carries a brand the table knows (owner 2026-11-13, "ryobi": Home Depot was never among the four, and
    Lowe's, which does not sell it, was half the time) gets the store that sells the brand first, whatever the toss,
    its small line reading "sells Ryobi"; and a brand name the sorter has not labelled still gets the buttons, in the
    brand's own department."""
    if not SHOP_BOX or _warm.get():
        return None
    k = " ".join(q.lower().split())
    if SHOP_NAV.search(k):
        return None
    import hashlib
    t = shop_table()
    lab = shop_label(k)
    labelled = lab is not None and lab[0] >= SHOP_MIN
    br = shop_brand(k, t)
    if br is not None and br[3] and (not labelled or lab[1] not in br[3]):
        br = None                                           # an ambiguous brand word ("predator") only inside its departments
    if labelled:
        dept = lab[1]
    elif br is not None and br[1]:
        dept = br[1]
    else:
        return None
    day = day or dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    toss = lambda s: hashlib.sha256(f"{k}|{day}|{s['id']}".encode()).digest()
    fits = sorted((s for s in t.get("stores", []) if s.get("on") and (dept in s["depts"] or "*" in s["depts"])), key=toss)
    special = [s for s in fits if dept in s["depts"]]
    # owner 2026-11-13: "sometimes is missing key stores, like this one is missing amazon": the stores marked key are
    # always there, then the department's own stores (any other store if it has none) up to four buttons
    key = [s for s in fits if s.get("key")]
    pins = [s for s in (br[2] if br else []) if s not in key]            # a brand's own store, as well as the key ones
    rest = [s for s in special if s not in key and s not in pins] + [s for s in fits if s not in special and s not in key and s not in pins]
    picks = (key + pins + rest)[:max(4, len(key) + len(pins))]
    if not picks:
        return None
    picks.sort(key=toss)                                    # the specialist is not always first either
    brand_ids = {s["id"] for s in br[2]} if br else set()
    picks.sort(key=lambda s: 0 if s["id"] in brand_ids else 1)           # ... but the store that sells the brand is
    out = []
    for s in picks:
        b = {"kind": "store", "name": s["name"], "sub": s["sub"], "url": s["url"] + quote(k, safe="")}
        if s["id"] in brand_ids:
            b["sub"], b["brand"] = f"sells {br[0]}"[:40], True
        out.append(b)
    ways: dict = {}                                         # the other ways to get it: one per kind (used, borrow, repair), by the same toss
    for o in sorted((o for o in t.get("others", []) if o.get("on") and dept in o["depts"]), key=toss):
        ways.setdefault(o["kind"], o)
    out += [{"kind": o["kind"], "name": o["name"], "sub": o["sub"], "url": o["url"] + quote(k, safe="")} for o in ways.values()]
    if t.get("near", {}).get(dept):
        out.append({"kind": "nearby", "name": "Nearby", "sub": t["near"][dept], "url": "/#q=" + quote(t["near"][dept], safe="")})
    # for the Near you list the page builds: a nearby shop whose own site is one of the switched-on stores gets a
    # "search this shop for the term" button (host -> that store's search link)
    host_of = lambda x: x.get("site") or urlsplit(x["url"]).hostname.removeprefix("www.")
    sites = {host_of(x): x["url"] + quote(k, safe="") for x in t.get("stores", []) if x.get("on")}
    box = {"q": k, "department": dept, "buttons": out, "sites": sites}
    if br:                                                  # the page floats that store's nearest shop to the top of Near you
        box["brand"], box["brand_sites"] = br[0], [host_of(s) for s in br[2]]
    return box


# No store box over a person (2026-11-14, owner: "why does trump get a buying card piece but biden doesnt"; then "yeah i
# think thats a good rule for now"). The department sorter calls "trump" shopped for (p 0.84, books: Amazon's own
# suggestions are full of Trump books and merchandise) and "biden" not (0.40), but a bare name with a person's card is
# a question about the person. The box and the store rows stay off then, unless the words ask to buy something or name
# a thing one buys ("trump book", "taylor swift shirt", "buy ..."). PERSON_SHOP=1 turns the rule off.
PERSON_SHOP = os.environ.get("SEARCH_PERSON_SHOP", "") == "1"
_SHOP_THING = re.compile(r"\b(books?|novels?|hats?|caps?|shirts?|t-?shirts?|hoodies?|merch|merchandise|mugs?|posters?|flags?|"
                         r"toys?|dolls?|figures?|figurines?|cards?|costumes?|masks?|stickers?|vinyl|albums?|cds?|dvds?|"
                         r"perfume|cologne|shoes|sneakers|jerseys?|calendars?|signed|autographed|memorabilia|gifts?)\b")


def card_is_person(card) -> bool:
    """Is the card's subject a person (the release's "born" fact)? Remembered per title."""
    t = (card or {}).get("title")
    if not t:
        return False
    lk = lookups()
    if lk is None or lk.name != "release":
        return False
    try:
        return bool(cached(("person", t), lambda: [lk.card_parts(t, 0)[3]], static=True)[0])
    except Exception:  # noqa: BLE001 - unknown: keep the box as it was
        return False


def shop_wanted_over(q: str, card) -> bool:
    """False for a name whose card is a person, unless the words ask to buy or name a thing one buys."""
    if PERSON_SHOP or not card_is_person(card):
        return True
    ql = (q or "").lower()
    return bool(_SHOP.search(ql) or _SHOP_THING.search(ql))


def shop_list(q: str, res: list[dict]) -> list[dict]:
    """res with the stores' searches as the rows after the best result (places stay first), for a name on the list.
    Unchanged for a builder's or warmer's request: what is stored never holds the rows."""
    if not SHOP_NAMES or not res or _warm.get():
        return res
    k = " ".join(q.lower().split())
    if any(r.get("source") == "shop" for r in res):
        return res
    lab = shop_label(k)
    if lab is not None:                                     # labelled: the label decides, whatever list the name is on
        if lab[0] < SHOP_MIN or SHOP_NAV.search(k) or SHOP_BOX:      # with the buy section on, the buttons replace the rows
            return res
    elif k not in shop_names():
        return res
    first = next((i for i, r in enumerate(res) if r.get("source") != "place"), None)
    if first is None:
        return res
    return res[:first + 1] + shop_rows(k) + res[first + 1:]


def fin_plain(qs) -> bool:
    """A request whose answer does not depend on the visitor: no parameter beyond the plain ones and no position sent
    with it. An engine with a home point of its own (indexhost) may serve stored answers too: fin_get leaves out the
    ones a position would change (2026-11-12, owner: the pipeline feeds indexhost's engine as well as public)."""
    return FIN_PLAIN.issuperset(qs) and _near.get() is None


def fin_get(q: str, full: bool) -> dict | None:
    """The finished answer for q, or None: none stored, too old, another layout, or the hand-picked list of q was
    written, changed or removed since (a removal must win over a stored answer)."""
    st = result_store() if CURATED_RELEASE else None
    if st is None:
        return None
    try:
        b = st.get_finished(CURATED_RELEASE, fin_key(q, full), FIN_MAX_AGE_S)
        if not b or b.get("v") != FIN_V or any(k not in b for k in FIN_FIELDS):
            return None
        if st.curated_at(CURATED_RELEASE, curated_key(q)) != b.get("cur_at"):
            return None
        if HOME and (b.get("local") or b.get("place") or any(r.get("source") == "place" for r in b["results"])):
            return None                                  # built with no position: here a position would change it
        return b
    except Exception:  # noqa: BLE001 - a stored answer is extra
        return None


def fin_put(q: str, full: bool, answer: dict) -> bool:
    """Keep the finished answer for q (the builder only: warm=1&fin=1)."""
    st = result_store() if CURATED_RELEASE else None
    if st is None:
        return False
    body = {"v": FIN_V, "cur_at": st.curated_at(CURATED_RELEASE, curated_key(q)), **{k: answer[k] for k in FIN_FIELDS}}
    return st.put_finished(CURATED_RELEASE, fin_key(q, full), body)


def _compact(s: str) -> str:
    return "".join(ch for ch in (s or "").lower() if ch.isalnum())


def _within(a: str, b: str, k: int) -> bool:
    """Edit distance of a and b at most k (small k: banded)."""
    if abs(len(a) - len(b)) > k:
        return False
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb))
        prev = cur
    return prev[-1] <= k


def curated_by_card(q: str, card: dict | None) -> list[dict] | None:
    """The curated list of the card's own name, when the query IS that name typed another way (owner 2026-11-06:
    "spiderman" has no list, "spider-man" has a good one): the same letters and digits ("spider man", "spiderman"),
    or within two edits for a name of 8 or more ("sarah j mass"). Never for a query that says more ("spiderman
    costume kids" keeps its own list)."""
    title = (card or {}).get("title") or ""
    a, b = _compact(q), _compact(title)
    if not a or not b or curated_key(q) == curated_key(title):
        return None
    if a != b and not (len(b) >= 8 and _within(a, b, 2)):
        return None
    return curated_rows(title)


# SEARCH_CURATED_FILL=1 (or ?cf=0|1; 2026-11-11): a curated list is short by design (3.4 rows on 150 judged names, 80%
# relevant) and it replaced the engine's own list of 8 (44% relevant), so a curated name showed FEWER relevant results
# (2.71 against 3.49, Clef, the same 150 names). With this on the curated rows stay on top, in their order, and the
# engine's own list fills the rest: 3.88 relevant per search, and the first row is still the curated one (88% relevant
# against 82.7%). Off = the curated list alone, as before.
CURATED_FILL = os.environ.get("SEARCH_CURATED_FILL", "").strip() == "1"
_cur_fill: contextvars.ContextVar = contextvars.ContextVar("curated_fill", default=CURATED_FILL)


def usual_gate():
    """The card gate's setting for this request, for the cleaned list's cache key (usual, in the handler)."""
    g = globals().get("_card_gate")
    return g.get() if g is not None else None


def curated_fill(top: list[dict], base: list[dict], skip=()) -> list[dict]:
    """top (the places and the curated rows) followed by the rows of base (the engine's own list) that top does not
    show already, to the length the engine's own list has (never shorter than top). A place in base is in top already.
    skip: pages never added (the card's own article, which the curated list leaves out on purpose)."""
    have = {same_page(r.get("url") or "") for r in top} | set(skip)
    out = list(top)
    for r in base:
        if len(out) >= max(len(top), len(base)):
            break
        k = same_page(r.get("url") or "")
        if k in have or r.get("source") == "place":
            continue
        have.add(k)
        out.append(r)
    return out


def curated_rows(q: str) -> list[dict] | None:
    """The curated list for q under this release, or None."""
    if not CURATED_RELEASE:
        return None
    st = result_store()
    if st is None:
        return None
    try:
        b = st.get_curated(CURATED_RELEASE, curated_key(q))
    except Exception:  # noqa: BLE001 - a curated answer is extra
        return None
    rows = (b or {}).get("rows")
    return [dict(r, curated=True) for r in rows if isinstance(r, dict) and str(r.get("url", "")).startswith(("http://", "https://"))] if rows else None


def store_inbox_once() -> list:
    """Rows another engine computed (ship.sh drops *.sqlite in <store dir>/inbox): taken into the store, then deleted.
    A file that cannot be read is renamed *.bad, so it is not tried again."""
    st = result_store()
    if st is None:
        return []
    inbox = os.path.join(os.path.dirname(os.path.abspath(STORE_PATH)), "inbox")
    done = []
    try:
        names = sorted(f for f in os.listdir(inbox) if f.endswith(".sqlite"))
    except OSError:
        return done
    for f in names:
        p = os.path.join(inbox, f)
        try:
            n = st.import_file(p)
            c = st.import_dept(p)                            # shopping departments (2026-11-13): not tied to a release
            if any(c.values()):
                n = dict(n, **{f"dept_{k}": v for k, v in c.items()})
            if CURATED_RELEASE:                              # curated answers ride the same inbox (2026-11-05)
                c = st.import_curated(p, CURATED_RELEASE)
                if any(c.values()):
                    n = dict(n, **{f"curated_{k}": v for k, v in c.items()})
                c = st.import_finished(p, CURATED_RELEASE, FIN_V)    # finished answers too (2026-11-12)
                if any(c.values()):
                    n = dict(n, **{f"finished_{k}": v for k, v in c.items()})
            os.remove(p)
        except Exception as e:  # noqa: BLE001 - a bad file must not stop the engine
            print(f"store inbox {f}: refused ({type(e).__name__}: {e})", flush=True)
            try:
                os.replace(p, p + ".bad")
            except OSError:
                pass
            continue
        print(f"store inbox {f}: " + ", ".join(f"{k} {v}" for k, v in n.items()), flush=True)
        done.append(n)
    return done


def store_inbox_loop() -> None:
    while True:
        time.sleep(STORE_INBOX_EVERY_S)
        try:
            store_inbox_once()
        except Exception as e:  # noqa: BLE001
            print(f"store inbox: {type(e).__name__}: {e}", flush=True)


def stored(kind: str, parts, compute):
    """compute() through the disk store: a row of this fingerprint and snapshot is served; only warm=1 writes."""
    st = result_store()
    if st is None:
        return compute()
    import result_store as rs
    key = rs.norm_key((generation(), parts))
    warm = _warm.get()
    got = st.get(kind, key)
    if got is not None:
        val, age = got
        if warm and _audit.get() and kind == "search":
            fresh = compute()
            same = fresh == val
            _store_note.set("same" if same else "diff")
            st.put(kind, key, fresh)
            return fresh
        if not (warm and age > STORE_REFRESH_S):
            if kind == "search":
                _store_note.set("disk")
            return val
    if not warm and STORE_PREV:
        got = st.get_prev(kind, key)
        if got is not None:
            _store_note.set("prev")
            box = _prev_served.get()
            if box is not None:
                box[0] = True                   # cached() keeps it out of memory, so the rebuilt row shows at once
            return got[0]
    val = compute()
    if warm:
        st.put(kind, key, val)
    return val


def cached(key, compute, static: bool = False):
    """compute() once per key per CACHE_TTL_S; static: per CACHE_STATIC_TTL_S and snapshot."""
    now = time.monotonic()
    gen = generation() if static else None
    ttl = CACHE_STATIC_TTL_S if static else CACHE_TTL_S
    if static and _warm.get():
        ttl = ttl / 2
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < ttl and hit[1] == gen:
            _cache.move_to_end(key)
            return list(hit[2])
    box = [False]
    tok = _prev_served.set(box)
    try:
        val = compute()
    finally:
        _prev_served.reset(tok)
    if box[0]:                                      # held the old build's row: not kept in memory (the warmer recomputes it)
        outer = _prev_served.get()
        if outer is not None:
            outer[0] = True
        return list(val)
    with _cache_lock:
        _cache[key] = (now, gen, val)
        _cache.move_to_end(key)
        while len(_cache) > CACHE_MAX:
            _cache.popitem(last=False)
    return list(val)


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


def _wiki(title: str, lead: str, score: float) -> dict:
    return {"url": "https://en.wikipedia.org/wiki/" + title.replace(" ", "_"),
            "title": f"{title} — Wikipedia", "content": (lead or "")[:300],
            "source": "wikipedia", "score": round(float(score), 2)}


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


SE_QUESTION = re.compile(r"/questions/[0-9]+")
SE_HOST = re.compile(r"(?:[a-z0-9-]+\.)+[a-z]{2,}")
SE_ROLES = ((1, "author"), (2, "answer_author"))   # registry.authors.role -> the result's field


def se_credit(url: str, credits: dict | None) -> dict:
    """A Stack Exchange question page's credits (registry.authors, {role: (user_id, name)}) -> the result's fields:
    author / author_url (who asked) and answer_author / answer_author_url (who wrote the answer the page's words come
    from). Stack Exchange's terms: every author's name, linked to the author's profile. A field is present for each role
    the release credits; the name is None for a deleted user (the page says so). A profile link is only ever
    https://<the result's own host>/users/<positive integer>, and only on a https question address with no user, password
    or port: never built from anything else."""
    if not credits:
        return {}
    u = urlsplit(url or "")
    host = (u.hostname or "").lower()
    linkable = (u.scheme == "https" and u.netloc.lower() == host and bool(SE_HOST.fullmatch(host))
                and bool(SE_QUESTION.fullmatch(u.path)))
    out = {}
    for role, key in SE_ROLES:
        if role not in credits:
            continue
        uid, name = credits[role]
        out[key] = name.strip() if isinstance(name, str) and name.strip() else None
        ok = linkable and isinstance(uid, int) and not isinstance(uid, bool) and uid > 0
        out[key + "_url"] = f"https://{host}/users/{uid}" if ok else None
    return out


def same_page(url: str) -> str:
    """https://www.costco.com/ and http://costco.com are one result."""
    u = urlsplit(url)
    host = (u.hostname or "").lower()
    host = host[4:] if host.startswith("www.") else host
    return host + (u.path.rstrip("/") or "")


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


def is_popular(tranco_rank, history_visits) -> bool:
    return (tranco_rank is not None and tranco_rank <= CONFIDENT_RANK) or (history_visits or 0) > 0


# Intent: what KIND of answer the query wants, from its wording alone. Used to
# keep `confident` off where the web is needed anyway (news, prices, shopping), and
# reported so the eval can score it against hand labels.
_FRESH = re.compile(r"\b(news|today|tonight|tomorrow|yesterday|latest|score|scores|weather|forecast|stock|"
                    r"price of|release date|live|schedule|when is|when are|earnings|20[2-3][0-9])\b|\bvs\.?\b")
_SHOP = re.compile(r"\b(buy|price|prices|cheap|cheapest|deal|deals|sale|for sale|coupon|review|reviews|"
                   r"best|vs|compare|shop|order|size|sizes|accessories)\b"
                   r"|\b(under|below) \$?\d+\b")     # "kayak under 100" (Jev-free rules, 2026-10-02)
_HOWTO = re.compile(r"^(how|what|why|when|where|which|who|can|could|should|is|are|does|do|will)\b"
                    r"|\b(how to|how do|fix|error|install|setup|set up|tutorial|guide|recipe|convert|replace)\b")


def intent_of(q: str, results: list[dict]) -> str:
    """nav | local | fresh | shop | howto | other."""
    t = q.lower().strip()
    if results and results[0].get("popular") and results[0]["score"] >= NAV_SCORE - 2:
        return "nav"
    if (place_query(t) or ("", ""))[0] == "kinds" or _NEAR.search(t):
        return "local"
    if _FRESH.search(t):
        return "fresh"
    if _HOWTO.search(t):
        return "howto"
    if _SHOP.search(t):
        return "shop"
    return "other"


# Web-only wording (2026-09-24). Shopping, fresh and task queries the index has no
# exact answer for got a tangential Wikipedia article ("boston weather tomorrow" ->
# Tomorrow.io): first-result Jev relevance shop 0.12, fresh 0.15, how-to 0.17 on the
# set, and SearXNG weighs our #1 like a web engine's #1. For those the local engine now
# answers nothing and the web fills the page. On both eval sets (1,167 queries) that
# empties 192 answers averaging 0.15; 7 were good (real sites the web lists too); no
# nav / brand / article / channel / account query is touched. What/why/who questions are
# NOT web-only: "what is X" is a topic the encyclopedia answers.
_TASK = re.compile(r"^(how to|how do i|how do you|how can i|best way to)\b"
                   r"|^change (a|an|the|my|your)\b"
                   r"|\b(fix|install|setup|set up|tutorial|recipe|replace|repair|unclog|clean)\b")


# Learned web intent (2026-09-27): Ettin-32M trained on Jev's query-only answer-type
# labels (16k synthetic queries). It catches web-only wording the regexes above cannot see —
# "patch a hole in drywall", "gaming mouse under 100", "descale a kettle". On the 1,167
# held-out queries, of 977 where local returned results, 51 showed junk (best Jev < 0.3);
# web >= 0.75 sends 23 of them to the web: 17 junk pages replaced, 0 good answers hidden
# (0.70: 19/0; 0.65 hid "sharpen a knife"). fp32 on purpose: int8 drifted up to 0.29 and
# changed decisions. 3.3 ms p50 on one CPU thread-pair. Missing model = regexes only.
# 2026-09-30 retrain (34k queries + the sites label) is more eager on web: at 0.75 it emptied
# "car rental comparison". web >= 0.9 and not a sites-of-a-kind query (sites < 0.7). A lower cut
# (0.6) only swapped junk lists for empty ones — a hand-off is not an answer,
# so it was not kept.
# 2026-10-02: REMOVED from the image — Jev-trained (TypeSafe's terms changed 10-01). Measured
# on the public engine with intent=0: no navigational first result changed (2,403 queries),
# the "sites" signal changed no first result, and on 1,718 informational queries it handed
# 20 to the web that local answered with junk ("kayak under 100" -> kayak.com.au). The rules
# above now catch 19 of those 20 ("under 100", "earnings", "accessories", "change a …") and
# 6 earnings queries it let through as junk; nothing changes on 14,166 training queries.
# The code stays: SEARCH_INTENT_DIR can point at a Jev-free model later. Unset = off.
INTENT_DIR = os.environ.get("SEARCH_INTENT_DIR", "")
WEB_INTENT_MIN = float(os.environ.get("SEARCH_WEB_INTENT_MIN", "0.9"))
SITES_BLOCK_WEB = float(os.environ.get("SEARCH_SITES_BLOCK_WEB", "0.7"))
_intent = None
_intent_lock = threading.Lock()


def _intent_model():
    global _intent
    if _intent is None:
        with _intent_lock:
            if _intent is None:
                try:
                    import numpy as np
                    import onnxruntime as ort
                    from tokenizers import Tokenizer
                    ort.disable_telemetry_events()
                    meta = json.load(open(os.path.join(INTENT_DIR, "labels.json")))
                    tok = Tokenizer.from_file(os.path.join(INTENT_DIR, "tokenizer.json"))
                    tok.enable_truncation(max_length=int(meta.get("max_tokens", 32)))
                    opts = ort.SessionOptions()
                    _ort_lean(opts)
                    opts.intra_op_num_threads = 2
                    sess = ort.InferenceSession(os.path.join(INTENT_DIR, "model_fp32.onnx"), opts,
                                                providers=["CPUExecutionProvider"])
                    _intent = (np, tok, sess, {i.name for i in sess.get_inputs()}, list(meta["labels"]))
                    print(f"web intent: {INTENT_DIR} {meta['labels']} (web >= {WEB_INTENT_MIN})", flush=True)
                except Exception as e:
                    print(f"web intent: off ({type(e).__name__}: {e})", flush=True)
                    _intent = False
    return _intent


@functools.lru_cache(maxsize=4096)
def intent_probs(q: str) -> dict[str, float]:
    """label -> P for the query text alone (site article place account web [sites]); {} when off."""
    m = _intent_model()
    if not m:
        return {}
    np, tok, sess, names, labels = m
    e = tok.encode(q)
    feed = {"input_ids": np.array([e.ids], dtype=np.int64), "attention_mask": np.array([e.attention_mask], dtype=np.int64)}
    if "token_type_ids" in names:
        feed["token_type_ids"] = np.array([e.type_ids], dtype=np.int64)
    z = sess.run(None, feed)[0][0]
    return {k: 1.0 / (1.0 + math.exp(-float(z[i]))) for i, k in enumerate(labels)}


def web_intent(q: str) -> float:
    """P(the ideal answer is fresh/commercial web content); 0.0 when off."""
    return 0.0 if _no_intent.get() or "intent" in _drop.get() else intent_probs(q).get("web", 0.0)


# "Wants sites of a kind" (2026-09-30): the intent model's sixth label, Jev's answer to "is the
# best answer a short list of real websites or apps of a certain kind?" (password manager,
# video sharing site). A query that says so without a site word ("tollywood ott", "discord
# bots") gets its kind's sites above the article, like "... app" does. 0.0 on older models.
SITES_INTENT_MIN = float(os.environ.get("SEARCH_SITES_INTENT_MIN", "0.7"))


def sites_intent(q: str) -> float:
    return 0.0 if _no_intent.get() or "intent" in _drop.get() else intent_probs(q).get("sites", 0.0)


def web_only(q: str, results: list[dict]) -> bool:
    t = q.lower()
    if any(r["score"] >= WIKI_EXACT_SCORE - 3 for r in results):
        return False
    if _SHOP.search(t) or _FRESH.search(t) or _TASK.search(t):
        return True
    # the learned signal never empties a search whose top result is the site it names ("floor &
    # decor" -> flooranddecor.com, "time" -> time.com: 2026-09-30 regression at web >= 0.6)
    if results and (exact_name_top(q, results[0]) or (results[0].get("source") == "site"
                    and query_names_domain(q, _domain_of(results[0].get("url") or "")))):
        return False
    return web_intent(t) >= WEB_INTENT_MIN and sites_intent(t) < SITES_BLOCK_WEB


def is_confident(results: list[dict]) -> bool:
    """Top result is a domain-name match (tier 1) on a popular site — see CONFIDENT_RANK."""
    return (bool(results) and results[0]["score"] >= NAV_SCORE - 2 and bool(results[0].get("popular"))  # incl. official 1000.5
            and not results[0].get("reranked_top") and not results[0].get("corrected"))


def _domain_of(url: str) -> str:
    h = (urlsplit(url).hostname or "").lower()
    return h[4:] if h.startswith("www.") else h


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


def exact_name_top(q: str, r: dict) -> bool:
    """A popular site whose name IS the query ("google" -> google.com) stays first: the
    learned ranker put google.com.ai above it."""
    host = _domain_of(r.get("url", ""))
    # any label but the ending: meraki.com's captured front page is portal.meraki.com, and
    # the learned ranker then put a plain meraki.net link (Tranco 337,692) above it
    labels = {x.replace("-", "") for x in host.split(".")[:-1]}
    return (r.get("source") == "site" and r["score"] >= NAV_SCORE - 1 and bool(r.get("popular"))
            and bool(norm_key(q)) and norm_key(q) in labels)


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


# ---------------------------------------------------------------------------
# Learned ranker. features() turns one (query, result) into numbers; the SAME
# function feeds training (utility/search-ranker.py) and serving, so they cannot drift.
# ranker.json holds logistic-regression weights fitted offline; without the file the
# hand-tuned order stands. Since 2026-10-02 it is Jev-free (utility/ce-train/ranker_jf.py):
# 191,489 rows — answers known by construction + pairs two open judges agreed on. Replayed
# with the Jev-free reranker after it: nav hit@1 0.897 -> 0.905 [+0.003, +0.012] vs the
# Jev-fitted weights, informational within noise; no ranker at all lost 0.042 there.
# ---------------------------------------------------------------------------
RANKER_PATH = os.environ.get("SEARCH_RANKER", os.path.join(os.path.dirname(os.path.abspath(__file__)), "ranker.json"))
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


# ---------------------------------------------------------------------------
# Cross-encoder reranker (2026-09-24). A small model reads the query and each
# result together and scores the pair — the largest single gain in the IR literature
# (Nogueira & Cho 2019, https://arxiv.org/abs/1901.04085). Measured on 1,167 Jev-judged
# queries (798 navigational + 369 informational), reordering the first 8 results BELOW
# the exact tiers (named sites, articles, places keep their hand order on top):
#   first-result relevance 0.639 -> 0.659, nDCG@5 0.361 -> 0.372; 60 queries better, 19
#   worse; topics +0.06, questions +0.07, YouTube +0.03, nav/typo flat.
# Reordering everything instead LOST (0.61): it demotes exact names. Five models tried;
# jina-reranker-v1-tiny-en (Apache-2.0, int8) at 8 pairs x 96 tokens: 42 ms p50 on
# indexhost's CPU, same gain as its full-list run (114 ms). Missing model = off.
#
# 2026-09-27 ( §2): jina-turbo-jev — jina-reranker-v1-turbo-en (Apache-2.0) fine-tuned on
# 294,747 Jev relevance labels for synthetic queries. Top result agrees with Jev on 79.7% of
# held-out queries vs 57.8% for the untrained jina-tiny; hand-label AUC 0.964 vs 0.875
# (docs/internal/search-jev/). On an Intel iGPU it runs as OpenVINO fp16 at a static
# 8 x 96 shape — 57 ms per search on testbox's HD 530 vs ~65 ms for jina-tiny on 2 CPU cores —
# and on the CPU as int8 ONNX (107 ms there). SEARCH_CE_DEVICE=auto tries the iGPU and keeps
# it only if it agrees with the CPU on a built-in check (a GPU that returns garbage while
# looking healthy is a known failure: runbook 20260826).
# ---------------------------------------------------------------------------
# Jev-free rerankers (2026-10-02; Jev's terms changed 10-01: nothing it labelled trains or ships).
# Both were trained on constructed answers + two-judge agreement (utility/ce-train/yardstick_labels.py)
# and read the domain format. One model per device, because the budget differs:
#   CPU   rerank-turbo-jevfree   jina-reranker-v1-turbo-en size: ~85 ms on a 4-core VPS, like before
#   iGPU  rerank-ettin68m-jevfree Ettin-68M: ~180 ms on Gen9 HD 530 (300 ms budget), ~300+ ms on a CPU
CE_DIR = os.environ.get("SEARCH_CE_DIR", "/models/rerank-turbo-jevfree")
CE_DIR_IGPU = os.environ.get("SEARCH_CE_DIR_IGPU", "/models/rerank-ettin68m-jevfree")
CE_DEVICE = os.environ.get("SEARCH_CE_DEVICE", "auto")        # auto | gpu | cpu
CE_TOP = int(os.environ.get("SEARCH_CE_TOP", "8"))   # iGPU compiles this static shape
CE_MAX_TOKENS = int(os.environ.get("SEARCH_CE_MAX_TOKENS", "96"))
# What the model reads per result: "title" = title + text (models trained before
# 2026-09-29); "domain" = the site's domain first. Must match how the models in CE_DIR and
# CE_DIR_IGPU were trained (both Jev-free models: domain).
CE_TEXT = os.environ.get("SEARCH_CE_TEXT", "domain")
CE_EXACT = WIKI_EXACT_SCORE - 3      # tiers 1-2 and places: never moved by the model
# The Jev-free models score a Wikipedia article at their floor (-7.9) unless the query is close to its title or
# worded as a question: their constructed training rows hold an article as the answer only for its own title
# (yardstick "wiki-topic"). At the floor the score says nothing — "illinois foid card": Firearm Owner's
# Identification -7.86, Firearms regulation in France -7.97 — and the article fell under whatever site scored
# higher ("NJ firearms id card": Gun laws in New Jersey from 1st to 6th, Marlin Firearms first; 2026-10-11).
# Such a row keeps the place the ranker gave it; the others are reordered around it. 30 informational queries
# on public: 43 of 99 scored articles at the floor, first result changed for 7 (6 better, 1 no better).
# -99 switches it off; a retrained model that scores articles on any wording makes it unneeded.
CE_WIKI_FLOOR = float(os.environ.get("SEARCH_CE_WIKI_FLOOR", "-7"))
_ce = None
_CE_CHECK = [   # built-in agreement check for the iGPU (query, ce_text) — relevant and not
    ("nytimes", "The New York Times — official site. The New York Times is an American daily newspaper"),
    ("nytimes", "New York Times Building — Wikipedia. The New York Times Building is a skyscraper"),
    ("python", "Python (programming language) — Wikipedia. Python is a high-level programming language"),
    ("python", "Monty Python — Wikipedia. Monty Python were a British comedy troupe"),
    ("vet near me", "Twin Falls Animal Hospital — 6.0 mi. veterinary"),
    ("vet near me", "Vet River — Wikipedia. The Vet River is a tributary of the Vaal River"),
    ("r/woodworking", "r/woodworking — Reddit. A community for woodworkers"),
    ("weather boston", "Boston Globe — official site. News, sports and opinion from Boston"),
]


class _CrossEncoder:
    """CPU: int8 ONNX through onnxruntime (model_int8.onnx, or model.onnx for older dirs)."""
    device = "cpu"

    def __init__(self, model_dir: str):
        import numpy as np
        import onnxruntime as ort
        from tokenizers import Tokenizer
        ort.disable_telemetry_events()
        self.np = np
        self.tok = Tokenizer.from_file(os.path.join(model_dir, "tokenizer.json"))
        self.tok.enable_truncation(max_length=CE_MAX_TOKENS)
        self.tok.enable_padding()
        opts = ort.SessionOptions()
        _ort_lean(opts)
        opts.intra_op_num_threads = int(os.environ.get("SEARCH_CE_THREADS", "2"))   # 2 vs 4 measured on 4 cores
        path = os.path.join(model_dir, "model_int8.onnx")
        if not os.path.exists(path):
            path = os.path.join(model_dir, "model.onnx")
        self.sess = ort.InferenceSession(path, opts, providers=["CPUExecutionProvider"])
        self.inputs = {i.name for i in self.sess.get_inputs()}

    def __call__(self, q: str, texts: list[str]) -> list[float]:
        np = self.np
        enc = self.tok.encode_batch([(q, t) for t in texts])
        feed = {"input_ids": np.array([e.ids for e in enc], dtype=np.int64),
                "attention_mask": np.array([e.attention_mask for e in enc], dtype=np.int64)}
        if "token_type_ids" in self.inputs:
            feed["token_type_ids"] = np.array([e.type_ids for e in enc], dtype=np.int64)
        return self.sess.run(None, feed)[0].reshape(len(texts), -1)[:, 0].tolist()


class _IGPUCrossEncoder:
    """Intel iGPU: OpenVINO fp16 IR compiled for ONE static shape (CE_TOP x CE_MAX_TOKENS),
    so the GPU plugin compiles once — per-shape recompiles starved the iGPU (testbox, 09-26).
    Short batches are padded with empty rows and sliced off. One infer request per thread:
    search-api reranks concurrently and an OpenVINO request is not thread-safe."""
    device = "gpu"

    def __init__(self, model_dir: str):
        import threading
        import numpy as np
        import openvino as ov
        from tokenizers import Tokenizer
        self.np = np
        self.tok = Tokenizer.from_file(os.path.join(model_dir, "tokenizer.json"))
        self.tok.enable_truncation(max_length=CE_MAX_TOKENS)
        self.tok.enable_padding(length=CE_MAX_TOKENS)
        core = ov.Core()
        if "GPU" not in core.available_devices:
            raise RuntimeError("no OpenVINO GPU device")
        core.set_property({"CACHE_DIR": os.environ.get("SEARCH_OV_CACHE", "/tmp/ov-cache")})
        self.cm = core.compile_model(os.path.join(model_dir, "model_fp16.xml"), "GPU",
                                     {"INFERENCE_PRECISION_HINT": "f16"})
        self.inputs = {i.get_any_name() for i in self.cm.inputs}
        self.name = core.get_property("GPU", "FULL_DEVICE_NAME")
        self._local = threading.local()

    def __call__(self, q: str, texts: list[str]) -> list[float]:
        np = self.np
        pairs = [(q, t) for t in texts[:CE_TOP]] + [("", "")] * (CE_TOP - min(len(texts), CE_TOP))
        enc = self.tok.encode_batch(pairs)
        feed = {"input_ids": np.array([e.ids for e in enc], dtype=np.int64),
                "attention_mask": np.array([e.attention_mask for e in enc], dtype=np.int64)}
        if "token_type_ids" in self.inputs:
            feed["token_type_ids"] = np.array([e.type_ids for e in enc], dtype=np.int64)
        req = getattr(self._local, "req", None)
        if req is None:
            req = self._local.req = self.cm.create_infer_request()
        out = req.infer(feed)[self.cm.output(0)].reshape(CE_TOP, -1)[:, 0]
        return out[:len(texts)].tolist()


def _agrees(gpu, cpu) -> tuple[bool, str]:
    """The iGPU must rank the built-in pairs like the CPU does before it is trusted."""
    import math
    bad = []
    for i in range(0, len(_CE_CHECK), 2):
        (q, a), (_, b) = _CE_CHECK[i], _CE_CHECK[i + 1]
        g, c = gpu(q, [a, b]), cpu(q, [a, b])
        if not all(math.isfinite(x) for x in g):
            return False, f"non-finite scores {g}"
        if (g[0] > g[1]) != (c[0] > c[1]) or max(abs(x - y) for x, y in zip(g, c)) > 1.0:
            bad.append(f"{q}: gpu {g[0]:.2f}/{g[1]:.2f} cpu {c[0]:.2f}/{c[1]:.2f}")
    return (not bad), "; ".join(bad) or "ok"


def cross_encoder():
    global _ce
    if _ce is None:
        try:
            cpu = _CrossEncoder(CE_DIR)
        except Exception as e:
            print(f"cross-encoder: off ({type(e).__name__}: {e})", flush=True)
            _ce = False                                     # no model in this image: off
            return _ce
        _ce, where = cpu, CE_DIR
        if CE_DEVICE in ("auto", "gpu"):
            try:
                # the iGPU model is checked against the SAME model on the CPU (its int8 file)
                gpu = _IGPUCrossEncoder(CE_DIR_IGPU)
                ok, why = _agrees(gpu, cpu if CE_DIR_IGPU == CE_DIR else _CrossEncoder(CE_DIR_IGPU))
                if ok:
                    _ce, where = gpu, CE_DIR_IGPU
                print(f"cross-encoder: iGPU {gpu.name} {'IN USE' if ok else 'REJECTED — ' + why}", flush=True)
            except Exception as e:
                print(f"cross-encoder: iGPU unavailable ({type(e).__name__}: {str(e)[:160]})", flush=True)
        print(f"cross-encoder: {where} on {_ce.device}", flush=True)
    return _ce


def require_reranker():
    """SEARCH_REQUIRE_RERANKER=1 (set by the home installer): a reranker that does not load stops the start instead of
    quietly ranking ~15 pt hit@1 worse (cross_encoder() otherwise answers "off" and the engine carries on)."""
    if os.environ.get("SEARCH_REQUIRE_RERANKER", "").strip() == "1" and not cross_encoder():
        raise SystemExit(f"SEARCH_REQUIRE_RERANKER=1 but the reranker in {CE_DIR} does not load (see 'cross-encoder: off' above): refusing to start")


def require_kinds_model():
    """SEARCH_REQUIRE_KINDS_MODEL=1 (set by the home installer when the release ships the model): a model that does not
    load stops the start. Without this embed() answers None and the kinds meaning match is off with nothing logged."""
    if os.environ.get("SEARCH_REQUIRE_KINDS_MODEL", "").strip() == "1" and embed("start") is None:
        raise SystemExit(f"SEARCH_REQUIRE_KINDS_MODEL=1 but the model in {MODEL_DIR} does not load: refusing to start")


def require_filtered_web():
    """The release lookups with the web lists still read from Postgres is refused at start (defect 2). The junk
    filter follows the lookups: on the release JUNK_SQL answers only the UT1-flagged sites of its registry, because the
    release's own web index is built without the blocked sites. Postgres' docs / page_docs / anchors still hold the sites
    that only site_quality.junk hides, so in this state they are shown (public, 2026-10-06 14:48-14:50 ET: an adult site
    led a probe). SEARCH_POSTGRES=off has no Postgres web lists and is fine. SEARCH_ALLOW_MIXED_WEB=1 is for evaluation
    arms on a test box only."""
    if (LOOKUP_BACKEND == "release" and POSTGRES == "on" and SITES_BACKEND != "tantivy-release"
            and os.environ.get("SEARCH_ALLOW_MIXED_WEB", "").strip() != "1"):
        raise SystemExit(f"SEARCH_LOOKUP_BACKEND=release with the web lists from {SITES_BACKEND} (SEARCH_WEB_BACKEND) shows sites "
                         "the junk filter should hide: set SEARCH_WEB_BACKEND=tantivy-release in the same step, or "
                         "SEARCH_ALLOW_MIXED_WEB=1 for an evaluation arm: refusing to start")


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


# A government task's own reranker (2026-11-06, owner: "we should work to make it work not just revert"): st1
# (SEARCH_CE_DIR) gave the government set 45.5 % help@1 where the shipped turbo it was fine-tuned from gives 53.9 %
# (rc14, rule grader; Clef Flash 38.6 vs 44.5). A query the state rule marks as a state task (gov_query_state, or
# the searcher's own state for one, the `gq` of _search) is reranked by SEARCH_CE_GOV_DIR (the release's
# rerank-turbo), every other query by st1. Loaded once beside it; unset, unreadable or ?drop=cegov: the one reranker.
CE_GOV_DIR = os.environ.get("SEARCH_CE_GOV_DIR", "").strip()
_ce_gov = None
_ce_gov_lock = threading.Lock()


def ce_gov():
    """The government-task reranker, or None (unset or failed to load: logged once, the main reranker is used)."""
    global _ce_gov
    if _ce_gov is None:
        with _ce_gov_lock:
            if _ce_gov is None:
                _ce_gov = False
                if CE_GOV_DIR:
                    try:
                        _ce_gov = _CrossEncoder(CE_GOV_DIR)
                        print(f"cross-encoder (government tasks): {CE_GOV_DIR} on cpu", flush=True)
                    except Exception as e:
                        print(f"cross-encoder (government tasks): off ({type(e).__name__}: {e})", flush=True)
    return _ce_gov or None


def ce_text(r: dict) -> str:
    text = (r.get("title") or "") + ". " + (r.get("content") or "")[:240]
    if CE_TEXT == "domain":
        host = (urlsplit(r.get("url") or "").hostname or "").removeprefix("www.")
        return host + " | " + text
    return text


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


# Curated index ( §3, 2026-09-27): `site_quality` holds Jev's judgement of every served
# homepage (useful / parked / spam / unsafe — utility/search-site-quality.py). Sites flagged
# `junk` by its conservative rule are left out of answers. Reversible and optional: no table
# (every snapshot so far) = nothing filtered; DROP TABLE site_quality undoes it. Loaded once
# per hour; web pages and articles are never filtered, only site/page results of a junk domain.
_JUNK_TTL = 3600.0
JUNK_SQL = "SELECT domain FROM site_quality WHERE junk"
_junk = (_NEVER, frozenset())


def junk_domains() -> frozenset:
    global _junk
    at, dom = _junk
    if time.monotonic() - at < _JUNK_TTL:
        return dom
    try:
        dom = frozenset(r[0] for r in _rows(JUNK_SQL, {}))
    except Exception:
        dom = frozenset()                      # no table: nothing is filtered
    _junk = (time.monotonic(), dom)
    return dom


def query_names_domain(q: str, host: str) -> bool:
    """The query spells this site's name: the typed domain, or its label ("playkaro365" ->
    playkaro365.com). Such a site is what was asked for, junk or not (2026-09-27 eval: the
    junk filter would have hidden the gambling site the query named, Jev 0.91)."""
    host = host[4:] if host.startswith("www.") else host
    dq = domain_query(q.lower().strip())
    if dq:
        return dq.removeprefix("www.") == host
    nq = norm_key(q)
    return bool(nq) and any(nq == x.replace("-", "") for x in host.split(".")[:-1])


_junk_ut1 = (_NEVER, frozenset())


def junk_domains_ut1() -> frozenset:
    """?drop=junkut1 (eval only): the junk domains UT1's blocklist flags, without Jev's."""
    global _junk_ut1
    at, dom = _junk_ut1
    if time.monotonic() - at < _JUNK_TTL:
        return dom
    try:
        dom = frozenset(r[0] for r in _rows("SELECT domain FROM site_quality WHERE junk "
                                            "AND source IN ('ut1', 'jev+ut1')", {}))
    except Exception:
        dom = frozenset()
    _junk_ut1 = (time.monotonic(), dom)
    return dom


def drop_junk(res: list[dict], q: str = "") -> list[dict]:
    drop = _drop.get()
    if "junk" in drop:
        return res
    dom = junk_domains_ut1() if "junkut1" in drop else junk_domains()
    if not dom:
        return res
    return [r for r in res if r.get("source") not in ("site", "page")
            or (d := _domain_of(r.get("url") or "")) not in dom or query_names_domain(q, d)]


# Web-only wording ("best running shoes", "lakers score", "how to patch drywall") used to get an EMPTY
# list so the page handed the user to the web. A hand-off is not an answer (owner rule 2026-09-30),
# and on 158 shopping/fresh test queries the index had the right top result for 44 of the 134 it handed
# off (answer eval 2026-10-04). Owner decision 2026-10-04: always show the list. SEARCH_WEB_ONLY_EMPTIES=1
# restores the empty list.
WEB_ONLY_EMPTIES = os.environ.get("SEARCH_WEB_ONLY_EMPTIES", "0").strip() == "1"


# Shopping, fresh and how-to wording: put the result whose title says what was asked first. On the 158
# shopping/fresh test queries the right answer was in the top 5 for 79 but first for only 34 (answer
# eval, Clef-judged, 2026-10-04); a logistic fit on those labels, 5-fold cross-validated BY QUERY, gets
# 60-65 first, the title/query word overlap being the strongest signal. Only web-only wording is
# reordered, and only inside the top WEB_RERANK_N. SEARCH_WEB_RERANK=0 turns it off.
WEB_RERANK = os.environ.get("SEARCH_WEB_RERANK", "1").strip() != "0"
WEB_RERANK_N = 5
_WR_STOP = frozenset("the a an of for to in on best how do i and or with under cheap buy".split())
_WR_LIST = re.compile(r"best|review|guide|top \d|vs\b|compare")
_WR_B = -4.9123                  # intercept
_WR_W = (-0.003, 0.7078, -0.5469,    # was first / second / third before the reorder
         2.0896, 2.233, 0.3482,      # learned-ranker prob, engine score / 1000, reranked_top
         3.2601,                     # share of the query's words that are in the title
         0.6277, 0.5125, 0.8934)     # path depth / 5, a site root, a review / guide / versus word in the title


def web_rerank(q: str, results: list[dict]) -> list[dict]:
    """Reorder the first WEB_RERANK_N results of web-only wording by the fitted score above; the rest keep
    their place. Returns a new list (the cached list is shared)."""
    if not WEB_RERANK or _no_webrr.get() or len(results) < 2:
        return results
    qw = {w for w in re.findall(r"[a-z0-9]+", q.lower()) if w not in _WR_STOP}
    top = results[:WEB_RERANK_N]
    scored = []
    for i, r in enumerate(top):
        # A front page named by its article is read by its address, the title the fit saw: by the name,
        # "nba finals schedule" moved nba.com ("National Basketball Association", no word of the query) from 4th
        # to 5th, the one list of 1,518 brand, product and held-out queries whose order changed (2026-10-09).
        title = (r.get("url") if r.get("named") else r.get("title") or "").lower()
        path = urlsplit(r.get("url") or "").path
        f = (i == 0, i == 1, i == 2, r.get("lr") or 0.0, (r.get("score") or 0.0) / 1000.0, float(bool(r.get("reranked_top"))),
             len(qw & set(re.findall(r"[a-z0-9]+", title))) / max(len(qw), 1), path.count("/") / 5.0,
             path in ("", "/"), bool(_WR_LIST.search(title)))
        scored.append((_WR_B + sum(w * float(x) for w, x in zip(_WR_W, f)), -i, r))
    scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
    return [r for _s, _i, r in scored] + results[WEB_RERANK_N:]


def hands_off(q: str, results: list[dict], quiet: bool) -> bool:
    """Answer nothing (an empty list) for web-only wording without an exact match? Off by default."""
    return bool(quiet and WEB_ONLY_EMPTIES and web_only(q, results))


# Is the first result shown for web-only wording any good? (2026-10-09) Since the list is always shown
# (2026-10-04) many of these answers are wrong: of the 146 held-out queries (answer-heldout.tsv, release rc6,
# an engine without the intent model) 111 are web-only, and one reader marked the first result right for 15,
# unsure for 24 and wrong for 72 ("how to freeze bananas" -> a baseball team). The cross-encoder is a poor
# REORDERER here (see _search) but its score for the result already on top separates them better than
# anything else on hand:
#                                                  fires   right / unsure / wrong   of the 15 right
#   learned-ranker probability >= 0.3                27       6 / 10 / 11                 6
#   top_ce >= 0                                      21       7 /  9 /  5                 7
#   top_ce >= 0 and half the query words in title    16       6 /  8 /  2                 6     <- not weak
#   top_ce >= 1                                      12       6 /  3 /  3                 6
#   best of the first five >= 0                      40      10 / 11 / 19                10
# So `weak` (everything outside the marked row) covers 70 of the 72 wrong and 9 of the 15 right; "right"
# alone never reaches 0.7 precision (6 of 16 = 0.38; 0.75 if the unsure ones are left out). Limits: one
# reader's labels, n=111, thresholds read off the same queries (had already spent this set), so the
# numbers are in-sample and to be redone on open-judge labels. The score also moves with what else is in
# the batch: the same result read among 8 instead of 5 differed by over 0.05 for 42 of the 111, by 0.83 at
# worst ("how to make cold brew coffee" 0.33 / -0.5), so a result near the threshold can flip. The
# best-of-five score is reported but not used: it is high when a better result sits lower ("best bike lock"
# 2.5 under a canal lock), which labels of the first result cannot weigh.
# Off by default: the model costs a median 128 ms for five results (p90 191, worst 272; 111 queries on a
# 16-thread laptop running three other engines), and what the page does with `weak` is not decided.
# SEARCH_WEB_SIGNAL=1 or, for one request, websig=1 adds top_ce / best_ce / weak to the answer; the list and
# its order are never touched.
WEB_SIGNAL = os.environ.get("SEARCH_WEB_SIGNAL", "0").strip() == "1"
WEB_SIGNAL_N = 5                 # what web_rerank reorders
WEB_WEAK_CE = float(os.environ.get("SEARCH_WEB_WEAK_CE", "0.0"))
WEB_WEAK_TITLE = 0.5
_websig: contextvars.ContextVar = contextvars.ContextVar("websig", default=None)       # eval A/B only (websig=1 / 0)


def weak_answer(q: str, title: str, top_ce: float) -> bool:
    """The first result is probably not what was asked: the model scores it under WEB_WEAK_CE, or its title
    holds fewer than WEB_WEAK_TITLE of the query's words (web_rerank's words: "how to" and "best" do not count)."""
    qw = {w for w in re.findall(r"[a-z0-9]+", q.lower()) if w not in _WR_STOP}
    share = len(qw & set(re.findall(r"[a-z0-9]+", (title or "").lower()))) / max(len(qw), 1)
    return top_ce < WEB_WEAK_CE or share < WEB_WEAK_TITLE


def web_signal(q: str, results: list[dict], scorer=None) -> dict:
    """{top_ce, best_ce, weak} for the shown list of web-only wording; {} when the signal is off, there is no
    model or nothing is shown. The model reads the first WEB_SIGNAL_N results as shown and moves none."""
    on = _websig.get()
    if not (WEB_SIGNAL if on is None else on) or not results:
        return {}
    scorer = scorer or cross_encoder()
    if not scorer:
        return {}
    shown = results[:WEB_SIGNAL_N]

    def score():
        with stage("web_ce"):
            sc = [round(float(x), 2) for x in scorer(q, [ce_text(r) for r in shown])]
        return [{"top_ce": sc[0], "best_ce": max(sc), "weak": weak_answer(q, shown[0].get("title"), sc[0])}]
    return cached(("websig", q, tuple(r.get("url") for r in shown)), score)[0]


_cache_state: contextvars.ContextVar = contextvars.ContextVar("cache_state", default="none")   # the log line's cache=
# The store's key ignores case: on 300 warmed names on public the results were identical field for field in any case
# (2026-11-04). Not spacing (13 of 300 differed with doubled spaces), and k=8 is its own row (63 of 300 were k=20[:8]).
STORE_FOLD_CASE = os.environ.get("SEARCH_STORE_FOLD_CASE", "1") == "1"


def search(q: str, k: int, semantic: bool = True, learned: bool = True, correct: bool = True,
           quiet: bool = True, ce: bool = True) -> list[dict]:
    """quiet: with SEARCH_WEB_ONLY_EMPTIES=1, answer nothing for web-only wording without an exact
    match; the eval turns it off (quiet=0) to see what the index would have said."""
    computed = []
    ckey = (q, k, semantic, learned, correct, ce, head_weight(), home_point(), place_radius_km(), _no_anchors.get(), _off.get(),
            _body.get(), _d2q.get(), _no_intent.get(), _drop.get(), _accounts_in_card.get(), _pages_fuse.get())
    skey = ((q.lower() if STORE_FOLD_CASE else q),) + ckey[1:]
    res = cached(ckey, lambda: computed.append(1) or stored("search", skey, lambda: _search_retry(q, k, semantic, learned, correct, ce)),
                 static=True)
    _cache_state.set((_store_note.get() if _store_note.get() in ("disk", "prev") else "miss") if computed else "hit")
    # after the cache: the cached list is the uncapped one, so ?pcap= needs no place in the key
    res = cap_profiles(drop_junk(res, q), _profile_cap.get())
    return [] if hands_off(q, res, quiet) else res


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
# "news" anywhere in the kind's name: "enewspaper" (vox.com), "cable news channel", "news magazine". Not television or
# channel kinds as such: with those "apple latest news" and "iphone news" led with apple.com (Apple TV), 2026-10-09.
_OUTLET_KIND = re.compile(r"news")
_OUTLET_NOT = re.compile(r"\b(?:fake|hoax|satire|satirical)\b|^news media$")
OUTLET_MIN_CONF = 0.5
OUTLET_SCORE = NAV_SCORE - 2          # a named site: the article about a programme (900) goes under it
OUTLET_TRIES = 4


def outlet_kind(key: str) -> bool:
    return bool(_OUTLET_KIND.search(key)) and not _OUTLET_NOT.search(key)


def outlet_medium(key: str) -> bool:
    """A kind that says the site is a channel or a magazine (kinds_release.news_outlet)."""
    return bool(re.search(r"\b(?:channel|magazine)\b", key))


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


def explain(q: str) -> dict:
    """{steps, stages, ms, corrected?}: what each step of the full search for q produced, in order — the lists
    that were searched, the named matches, the fusion, the merged order, then each reordering. Names, numbers
    and the results themselves; nothing here is logged."""
    tr, st, t0 = [], {}, time.monotonic()
    tok_t, tok_s = _trace.set(tr), _stages.set(st)
    try:
        res = _search_retry(q, MAX_K, True, True, True)
    finally:
        _trace.reset(tok_t)             # the native host's worker threads are reused: never leave a trace on
        _stages.reset(tok_s)
    fixed = res[0].get("corrected") if res else None
    steps = trace_run(tr, fixed or q)
    kept = drop_junk(res, q)
    if len(kept) != len(res):
        steps.append({"step": "junk", "dropped": len(res) - len(kept)})
    if web_only(q, kept) and (shop := web_rerank(q, kept)) is not kept:
        steps.append(dict(trace_rows(shop), step="web"))
    body = {"steps": steps, "stages": {k: round(v, 1) for k, v in st.items()}, "ms": int((time.monotonic() - t0) * 1000),
            "origin": answer_origin(q)}
    if fixed:
        body["corrected"] = fixed
    return body


def answer_origin(q: str) -> dict:
    """Where the list shown for q comes from (2026-11-14, owner: "is the how this was ranked explanations accurate for
    pre computed results"; it was not: the steps are the engine's live ranking, and a stored or hand-picked list was
    shown instead). {"kind": "finished" (built ahead and stored; "curated": true when a hand-picked list is in it),
    "curated" (a hand-picked list on the engine's own), or "live"; "at": when it was made, epoch seconds}."""
    st = result_store() if CURATED_RELEASE else None
    if st is None:
        return {"kind": "live"}
    try:
        cur_at = st.curated_at(CURATED_RELEASE, curated_key(q))
        if FINISHED:
            for full in (True, False):
                if fin_get(q, full) is not None:
                    at = st.finished_at(CURATED_RELEASE, fin_key(q, full))
                    return {"kind": "finished", "at": at, "curated": cur_at is not None}
        if curated_rows(q):
            return {"kind": "curated", "at": cur_at}
    except Exception:  # noqa: BLE001 - the explanation is extra
        pass
    return {"kind": "live"}


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


# ?drop=tranco (eval only): every Tranco rank the ranking reads becomes the Common Crawl host
# rank put on Tranco's scale — the site that is n-th by cc_hc_pos among serve_sites gets the
# n-th smallest Tranco rank there — so the formulas, CONFIDENT_RANK and is_popular() keep
# their meaning. Rank-matched at ~400 geometric knots (step <= 3.3% in rank). A domain with
# no CC rank gets none (NULL), as a Tranco-less domain does today.
_TRANCO_MAP = None
_tranco_lock = threading.Lock()


def _tranco_map() -> tuple[str, str]:
    """(thresholds, values) as SQL int[] literals."""
    global _TRANCO_MAP
    if _TRANCO_MAP is None:
        with _tranco_lock:
            if _TRANCO_MAP is None:
                with _conn() as c:
                    cc = [r[0] for r in c.execute("SELECT cc_hc_pos FROM serve_sites WHERE cc_hc_pos IS NOT NULL "
                                                  "ORDER BY cc_hc_pos")]
                    tr = [r[0] for r in c.execute("SELECT tranco_rank FROM serve_sites WHERE tranco_rank IS NOT NULL "
                                                  "ORDER BY tranco_rank")]
                n = min(len(cc), len(tr))
                pos, p = [], 1.0
                while p <= n:
                    if not pos or int(p) != pos[-1]:
                        pos.append(int(p))
                    p = max(p + 1, p * 1.033)
                thr = [cc[i - 1] for i in pos]
                val = [tr[0]] + [tr[i - 1] for i in pos]
                _TRANCO_MAP = ("ARRAY[" + ",".join(map(str, thr)) + "]::int[]",
                               "ARRAY[" + ",".join(map(str, val)) + "]::int[]")
    return _TRANCO_MAP


def cc_as_tranco(col: str) -> str:
    thr, val = _tranco_map()
    return f"(({val})[width_bucket({col}, {thr}) + 1])"


def own_rank_sql(sql: str) -> str:
    """SEARCH_SITE_RANK=own: every Tranco rank the SQL reads becomes our own rank (importer/own_rank.py). Same
    column names come back, so no caller changes: s/ws.tranco_rank -> own_rank, nav_sites -> nav_sites_own,
    host_nav -> host_nav_own, kind_docs.tranco_rank -> the rank of the domain."""
    sql = re.sub(r"\bnav_sites\b", "nav_sites_own", sql)
    sql = re.sub(r"\bhost_nav\b", "host_nav_own", sql)
    own = "(SELECT o.rank FROM own_rank o WHERE o.host = {}.domain)"
    return (sql.replace("ws.tranco_rank,", own.format("ws") + " AS tranco_rank,")
               .replace("s.tranco_rank", own.format("s"))
               .replace("k.tranco_rank", own.format("k")))


def pop_sql(sql: str) -> str:
    """sql unchanged, or with Tranco swapped for our own rank (SEARCH_SITE_RANK=own) or, for eval, for the
    Common Crawl host rank under ?drop=tranco."""
    if SITE_RANK == "own":                       # only SQL that Postgres runs: a lookup backend answers its own
        lk = lookups()                           # constants from the release's signals.bin (site.own_rank)
        return sql if lk is not None and sql in lk.h else own_rank_sql(sql)
    if "tranco" not in _drop.get() or "tranco_rank" not in sql:
        return sql
    ws_cc = "(SELECT x.cc_hc_pos FROM serve_sites x WHERE x.domain = ws.domain)"
    return (sql.replace("ws.tranco_rank,", cc_as_tranco(ws_cc) + " AS tranco_rank,")
               .replace("s.tranco_rank", cc_as_tranco("s.cc_hc_pos"))
               .replace("k.tranco_rank", cc_as_tranco("k.cc_hc_pos")))


def _rows(sql: str, params: dict, setup: str | None = None) -> list[tuple]:
    """One query on its own pooled connection — for the lists _search runs side by side."""
    sql = pop_sql(sql)
    with _conn() as c:
        if setup:
            c.execute(setup)
        return list(c.execute(sql, params))


# A state named in the query orders government pages (gov_state.py): that state's pages lead the page
# list, another state's government results go to the end. Measured on 2,000 government-service queries
# (tools/gov-eval). gov-states.json absent from the release: off.
GOV_PAGES_DEPTH = int(os.environ.get("SEARCH_GOV_PAGES_DEPTH", "400"))
GOV_PAGES_GIVEN = int(os.environ.get("SEARCH_GOV_PAGES_GIVEN", "12"))   # of the pool, to the reranker
GOV_PAGES_POOL = int(os.environ.get("SEARCH_GOV_PAGES_POOL", "40"))
GOV_PAGES_READ = int(os.environ.get("SEARCH_GOV_PAGES_READ", "3"))      # pools' worth read before the tier sort
_gov_states: dict | None = None


def gov_states() -> dict:
    global _gov_states
    if _gov_states is None:
        _gov_states = gov_state.load(os.environ.get("SEARCH_GOV_STATES") or os.path.join(RELEASE_DIR, "gov-states.json"))
        print(f"government sites with a state: {len(_gov_states)}", flush=True)
    return _gov_states


# A two-letter code that is also a word ("co", "ok", "al", "pa") counts as the state when the rest of the query is
# something residents ask of their state: Welch and Cho's localizable query (SIGIR 2008), read from the
# release's own pages instead of a query log. The rest must name a service (_GOV_SERVICE_WORD), its words must sit on
# government pages at least as often as pages in general do (share >= gov_base), and on the government pages of
# GOV_CODE_STATES states or more. The share alone stopped working with the link crawl: rc11's pages were
# 12.7 % government and a task's words 56-96 % ("jury duty" 94 %, "flames" 2.9 %), but rc12's are 55.9 %, and
# "16 april" (74 %), "groups" (56 %) and "the 2024 rose bowl" (86 %) passed the old fixed bars of 55 % and 40 % with a
# service word, which lifted a New Jersey page over 16 open-set answers. Measured on rc12's 1.9M pages
# (2026-10-31): of 5,971 open-set queries the old rule let 617 through and this one 38; of the government set's 20
# home-worded tasks it keeps all. ?drop=govcode: off.
GOV_CODE_STATES = 10
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
_local_maps: dict | None = None
_point_towns: dict = {}


def local_map() -> dict:
    global _local_maps
    if _local_maps is None:
        _local_maps = gov_state.load_local(os.environ.get("SEARCH_GOV_STATES") or os.path.join(RELEASE_DIR, "gov-states.json"))
    return _local_maps


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


_local_set: frozenset | None = None


def is_local_gov(host: str) -> bool:
    """A town's or a county's government site, not the state's own (gov-states.json's "local")."""
    global _local_set
    if _local_set is None:
        m = local_map()
        _local_set = frozenset(d for table in m.values() for ds in table.values() for d in ds)
    labels = host.lower().split(".")
    return any(".".join(labels[i:]) in _local_set for i in range(max(0, len(labels) - 1)))


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


# The same task in a resident's word and the agency's: "nj gun license" has to reach New Jersey's firearms pages.
GOV_SAME_WORDS = {"gun": ("firearm", "firearms", "handgun"), "guns": ("firearm", "firearms", "handgun"),
                  "handgun": ("firearm", "firearms"), "car": ("vehicle", "vehicles"), "auto": ("vehicle", "vehicles"),
                  "id": ("identification",), "ticket": ("traffic", "municipal"), "laws": ("statutes", "legislature"),
                  "bills": ("bill",)}
# A page for one audience, or news about the task, is not the task (the National Guard's licence page and a scam
# warning came first for "nj fishing license" and "nj jury duty"). Not so when the query names that audience.
_NOT_THE_TASK = re.compile(r"\b(njng|guard|veterans?|military|buddy|youth|seniors?|students?|transgender|foster|adoptees?|retired|"
                           r"providers?|stakeholders?|scams?|fraud|news|newsroom|press|announcements?|approved|rfq|bulletins?|"
                           r"hearings?|notices?|sisters|leased|njfoster\w*|njbonds|camera)\b")


def _press_release_title(title: str) -> bool:
    """A task page is named in a few words; a press release in a sentence."""
    return max((len(part.split()) for part in re.split(r"\s[|\u2013\u2014-]\s", title or "")), default=0) >= 10


def not_the_task(url: str, title: str, words) -> bool:
    text = (title or "") + " " + re.sub(r"[\W_]+", " ", urlsplit(url).path)
    named = set(words)
    if _press_release_title(title):
        return True
    return any(m.group(1) not in named for m in _NOT_THE_TASK.finditer(text.lower()))


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


# An agency's front page for a query that names the agency's business: "colorado state parks" ->
# cpw.state.co.us, "Colorado Parks and Wildlife". gov_pages reads pages, and a front page carries few of a task's words
# in its address, so the state's parks pages came back (an archery range, Donate) and never the agency itself. Of the
# sites with two or more of gov_pages' pool, the most frequent whose name holds every word of the query that is not
# a filler: "nj fishing license" keeps the licence page (NJ Fish & Wildlife has no "license"). ?drop=govfront: off.
_FRONT_FILLER = frozenset("a an and the to for of in on my me how get state states online official website site gov "
                          "government department dept office".split())


def names_task(title: str, rest: str) -> bool:
    """Every word of `rest` that is not a filler starts a word of `title`, or a word of it of 4 letters or more starts
    the query's word ("fishing" -> "Fish"); False when no word is left."""
    words = [w for w in re.findall(r"[a-z0-9]+", rest.lower()) if len(w) > 1 and w not in _FRONT_FILLER]
    toks = set(re.findall(r"[a-z0-9]+", (title or "").lower()))
    return bool(words) and all(any(t.startswith(w) or (len(t) >= 4 and w.startswith(t)) for t in toks) for w in words)


def agency_front(lk, rest: str, pages: list[tuple[str, str]]) -> tuple[str, str] | None:
    """(front page url, site name) of the site of gov_pages' pool (url, title) that the query names (above), else
    None. A site's name is the end its pages' titles share ("Donate and Support | Colorado Parks and Wildlife"): the
    release keeps no title for a site's front page itself."""
    import lookups_release as R
    by: dict[str, list[tuple[str, str]]] = {}
    for u, t in pages:
        by.setdefault(_domain_of(u), []).append((u, t or ""))
    words = [w for w in re.findall(r"[a-z0-9]+", rest.lower()) if len(w) > 1 and w not in _FRONT_FILLER]
    for host, rows in sorted(by.items(), key=lambda x: -len(x[1]))[:3]:
        if len(rows) < 2:
            break
        # every page under one folder: the agency is a part of a portal (nj.gov/mvc/...), and the host's front page
        # is the portal's, not the agency's (lv eval 2026-11-04: "nj cdl" lifted www.nj.gov over the CDL manual)
        heads = {(urlsplit(u).path.strip("/").split("/") + [""])[0].lower() for u, _t in rows}
        if len(heads) == 1 and "" not in heads:
            continue
        # the name is the part of the titles their site repeats, at either end: "Donate | Colorado Parks and
        # Wildlife", "NJ MVC | Getting your CDL"; counted once a distinct title
        ends: dict[str, int] = {}
        for t in {t for _u, t in rows}:
            parts = [p.strip() for p in re.split(r"\s+[|\u2013\u2014-]\s+", t)]
            if len(parts) > 1:
                for p in {parts[0], parts[-1]} - {""}:
                    ends[p] = ends.get(p, 0) + 1
        name = max(ends.items(), key=lambda x: x[1])[0] if ends else None
        if not name or ends[name] < 2 or not names_task(name, rest):
            continue
        # a name that is only the query's own words is a section ("Marriage License"), not an agency
        own = [t for t in re.findall(r"[a-z0-9]+", name.lower())
               if len(t) > 1 and not t.isdigit() and t not in _FRONT_FILLER
               and not any(t.startswith(w) or (len(t) >= 4 and w.startswith(t)) for w in words)]
        if not own:
            continue
        for d in lk.names1(host, R.N_DOMAIN)[:1]:
            url = lk.reg(d)[2]
            if url:
                return url, name
    return None


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


# Place abbreviations (2026-09-25: "marsh twp nj" found nothing, "marsh township new jersey"
# both Marsh Townships). Articles spell them out, so the Wikipedia queries do too. A state
# code only at the end, and never one that is also a word ("sign in", "call me", "la").
_PLACE_ABBR = {"twp": "township", "twsp": "township", "boro": "borough", "mt": "mount", "ft": "fort",
               "hts": "heights", "spgs": "springs", "jct": "junction", "cty": "county"}
_STATE_END = {"nj": "new jersey", "ny": "new york", "tx": "texas", "fl": "florida", "nc": "north carolina",
              "sc": "south carolina", "nd": "north dakota", "sd": "south dakota", "nm": "new mexico",
              "nv": "nevada", "nh": "new hampshire", "vt": "vermont", "va": "virginia", "wv": "west virginia",
              "wi": "wisconsin", "wy": "wyoming", "ct": "connecticut", "ak": "alaska", "az": "arizona",
              "ks": "kansas", "ky": "kentucky", "mn": "minnesota", "ri": "rhode island", "tn": "tennessee",
              "md": "maryland", "ia": "iowa"}     # not ms / ne / ga: Microsoft, northeast, Analytics


def place_expand(q: str) -> str:
    """"marsh twp nj" -> "marsh township new jersey"; "mt vernon" -> "mount vernon"; "5 ft"
    stays (a number before it is a measure). Anything else unchanged."""
    words = q.split()
    out = []
    for i, w in enumerate(words):
        lw = w.lower()
        if lw in _PLACE_ABBR and not (i > 0 and words[i - 1][:1].isdigit()) and len(words) > 1:
            out.append(_PLACE_ABBR[lw])
        elif i == len(words) - 1 and i > 0 and lw in _STATE_END:
            out.append(_STATE_END[lw])
        else:
            out.append(w)
    return " ".join(out)


SEM_IVF_DOCS_SQL = "SELECT page_id, title, lead FROM wiki_docs WHERE page_id = ANY(%(ids)s)"


def _sem_rows(q: str) -> list[tuple] | None:
    if SEM_BACKEND == "ivf":
        si, ix, enc = sem_ivf()
        code = query_code(q)
        with stage("sem-ivf"):
            hits = ix.search(code, 5, SEM_PROBES)
        if not hits:
            return []
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


def _search(q: str, k: int, semantic: bool = True, learned: bool = True, ce: bool = True) -> list[dict]:
    out: list[dict] = []
    _tr("start", q=q)
    t_stage = time.monotonic()
    with _conn() as c:
        has = tables(c) - _off.get()
        stored = stored_text(c)
        sites = "docs" in has and "serve_sites" in has
        wiki = "wiki_docs" in has
        # The keyword / semantic lists and places depend only on the query: started now,
        # each on its own connection, collected where the order below uses them.
        def sub(name, fn, *args):
            return _EXEC.submit(contextvars.copy_context().run, _staged, name, fn, *args)
        wq = place_expand(q)                                    # "marsh twp nj": articles spell it out
        bv = _body.get()
        f_wiki = sub("wiki", wiki_rows, wq, min(5, k), stored) if wiki else None
        web = web_text()
        f_sites = sub("sites", web.sites, q, k, head_weight(), stored, bv) if sites else None
        f_se = sub("se", _rows, SE_SQL, {"q": q, "k": min(5, k)}) if "se_docs" in has else None
        f_pages = sub("pages", web.pages, q, min(5, k), stored, bv) if "page_docs" in has else None
        gs = gov_query_state(q)
        gq = gs or home_gov_state(q)                            # named, else the searcher's own
        hl = None if gs else home_local(q)                      # the searcher's town and county
        f_local = (sub("local_pages", web.local_pages, hl[0], hl[1], min(5, k))
                   if hl and f_pages is not None and hasattr(web, "local_pages") else None)
        # the whole government list, whatever k (2026-11-06): k is how many results are returned, not how many
        # candidates the reranker sees; min(12, k) gave it 5 on /search?k=5 and 8 on the page
        f_gov = (sub("gov_pages", web.gov_pages, gq[0], gq[1], gq[2], GOV_PAGES_GIVEN)
                 if gq and f_pages is not None and hasattr(web, "gov_pages") else None)
        f_anchors = (sub("anchors", web.anchors, q, min(5, k))
                     if sites and "anchor_docs" in has and not _no_anchors.get() else None)
        f_d2q = (sub("d2q", _rows, D2Q_SQL, {"q": q, "k": min(5, k)})
                 if sites and "d2q_docs" in has and _d2q.get() else None)
        akey = anchor_key(q)
        f_exact_anchor = (sub("exact_anchor", web.exact_anchor, akey)
                          if sites and "anchors" in has and not _no_anchors.get() and 2 <= len(akey) <= 60 else None)
        f_sem = sub("sem", _sem_rows, wq) if semantic and (SEM_BACKEND == "ivf" or "wiki_vec" in has) and wiki else None
        f_sem_pages = (sub("sem_pages", sem_page_rows, q, min(5, k))
                       if semantic and SEM_BACKEND == "ivf" and "page_docs" in has and sem_pages_dir() else None)
        kq = kind_query(q) if "kind_docs" in has else None
        # a named account / channel / subreddit ("netflix instagram") is not a kind question
        if kq and (any(subreddit_query(q)) or any(youtube_query(q)) or (account_query(q) if "wiki_links" in has else None)):
            kq = None
        f_kind = sub("kind", _rows, kind_bm25_sql(c), {"q": kq, "k": 5}) if kq and kind_worded(q) else None
        f_kind_site = (sub("kind_site", _kind_site_rows, q)
                       if kq and kind_worded(q) and "kind_site_vec" in has else None)
        here = home_point()
        pq = place_query(q) if here and "places" in has else None
        town_shape = bool(here and "places" in has and (pq is None or pq[0] == "name") and place_town_splits(q))
        f_exact = (sub("place_exact", cached, ("place", q.lower(), here), lambda: [place_exact(q)], True)
                   if (pq and pq[0] == "name") or town_shape else None)
        f_town = (sub("place_town", cached, ("place_town", q.lower(), here), lambda: [place_town(q)], True)
                  if town_shape else None)
        f_places = f_places_fb = near_named_km = None
        if pq:
            lat, lon = here
            dlat = place_radius_km() / 111.32
            params = {"lat": lat, "lon": lon, "dlat": dlat, "dlon": dlat / max(0.1, math.cos(math.radians(lat))),
                      "r": place_radius_km()}
            setup = None
            if pq[0] == "kinds":
                sql, place_base = PLACES_SQL.format(match=PLACE_MATCH_KINDS), PLACE_CATEGORY_SCORE
                params["kinds"] = PLACE_KINDS.get(pq[1]) or PLACE_KINDS_NEAR[pq[1]]
                params["hint"], params["hint_kinds"] = PLACE_NAME_HINT.get(pq[1], ("", []))
                if pq[1] in PLACE_KIND_FALLBACK:
                    f_places_fb = sub("places_fb", _rows, sql, dict(params, kinds=PLACE_KIND_FALLBACK[pq[1]]), None)
            else:
                sql, place_base = PLACES_SQL.format(match=PLACE_MATCH_NAME), PLACE_NAME_SCORE
                params["name"] = pq[1]
                setup = PLACE_NAME_SETUP
            f_places = sub("places", _rows, sql, params, setup)
            alt = None
            if pq[0] == "name":
                core = place_name_core(pq[1])
                alt = possessive_variant(pq[1]) or (core if core != pq[1] else None)
            if alt:                                            # "wendys" -> "Wendy's", "verizon store" -> "verizon": only when the name as typed finds none
                f_places_fb = sub("places_alt", _rows, sql, dict(params, name=alt), setup)

        # A place category ("restaurants", "gym") names a kind of place, not a website:
        # restaurants.nl must not beat the restaurants nearby.
        pq_kind = here is not None and (place_query(q) or ("", ""))[0] == "kinds"
        keys = [] if pq_kind else nav_keys(q)
        dq = domain_query(q) if sites or "nav_sites" in has else None
        lk = lookups()
        t_stage = stage_since("setup", t_stage)
        if lk is not None and lk.name == "release":            # one names lookup answers every exact tier below
            with stage("names"):
                lk.prefetch(exact_names(q, keys, dq))
        if dq:                                                 # a typed domain: that site, first
            row = c.execute(pop_sql(DOMAIN_DOC_SQL), {"d": dq}).fetchone() if sites else None
            nav = c.execute(pop_sql(DOMAIN_NAV_SQL), {"d": dq}).fetchone() if "nav_sites" in has else None
            if row or nav:
                url, title, desc, rank = row if row else (f"https://{dq}/", dq, "", nav[0])
                if rank is None and nav:
                    rank = nav[0]
                out.append(_site(url, title, desc, NAV_SCORE + 1, is_popular(rank, 0)))
        if keys and (sites or "nav_sites" in has):             # tier 1
            cands = list(c.execute(pop_sql(NAV_SQL), {"keys": keys, "generic": GENERIC_TLD})) if sites else []
            if "nav_sites" in has:
                have = {cnd[5] for cnd in cands}
                cands += [bare_site(h, r) for h, r in c.execute(pop_sql(NAV_SITES_SQL), {"keys": keys}) if h not in have]
            for i, (url, title, desc, rank, visits, _dom) in enumerate(nav_order(cands)[:3]):
                # A foreign country version is a weaker guess than a subdomain service or the
                # article's official site ("web archive": webarchive.org.uk vs web.archive.org).
                foreign = 0.0 if is_generic(urlsplit(url).hostname or "") else 1.0
                # a plain link loses a tie to the same page with a real title (host_nav: -0.25)
                bare = 0.3 if is_bare((url, title, desc, rank, visits, _dom)) else 0.0
                out.append(_site(url, title, desc, NAV_SCORE - i - foreign - bare, is_popular(rank, visits)))
        sub_key, sub_topic = subreddit_query(q) if "subreddits" in has else (None, None)
        if sub_key:                                            # tier 1: a named subreddit
            for name, title, desc, subs in c.execute(SUB_EXACT_SQL, {"key": sub_key}):
                out.append(_subreddit(name, title, desc, subs, NAV_SCORE + 0.75, subs >= SUB_POPULAR))
        yt_key, yt_topic = youtube_query(q) if "youtube_channels" in has else (None, None)
        if yt_key:                                             # tier 1: a named YouTube channel
            for i, row in enumerate(c.execute(YT_EXACT_SQL, {"key": yt_key})):
                out.append(_youtube(*row, NAV_SCORE + 0.75 - 2 * i))
        acct = account_query(q) if "wiki_links" in has and not yt_key and not sub_key else None
        if acct:                                               # tier 1: a named account (instagram, imdb …)
            for i, row in enumerate(c.execute(ACCOUNT_SQL, {"p": acct[0], "key": acct[1], "keys": the_keys(acct[1])})):
                out.append(_account(*row, NAV_SCORE + 0.75 - 2 * i))
        if "host_nav" in has and keys:                         # tier 1: subdomain services
            for i, (url, title, desc, rank) in enumerate(c.execute(pop_sql(HOST_NAV_SQL), {"keys": [k.replace("-", "") for k in keys]})):
                out.append(_site(url, title, desc, NAV_SCORE - 0.25 - i, is_popular(rank, 0)))
        nq = norm_key(q)
        asked_name = site_ask(q)                               # "x official site": the name x, else None
        exact_titles: list[str] = []
        sited: set[str] = set()
        sigil_titles: set[str] = set()                         # "$Trump" for "trump": see SIGILS
        if wiki and "wiki_aliases" in has and len(nq) >= 2:    # tier 2
            if "wiki_sites" in has:
                rows2 = c.execute(pop_sql(WIKI_EXACT_SITE_SQL), {"nq": nq}).fetchall()
                asked = False
                if not rows2 and asked_name and len(norm_key(asked_name)) >= 2:
                    # nothing is called "x official site": ask for x, and remember the site was asked for
                    rows2, asked = c.execute(pop_sql(WIKI_EXACT_SITE_SQL), {"nq": norm_key(asked_name)}).fetchall(), True
                rows2 = spaced_as_asked(rows2, asked_name if asked else q)
                for i, (title, lead, url, box, rank, visits, own) in enumerate(rows2):
                    out.append(_wiki(title, lead, WIKI_EXACT_SCORE - i))
                    exact_titles.append(title)
                    if untyped_sigil(title, q):
                        sigil_titles.add(title)
                    if url and is_popular(rank, visits):
                        sited.add(title)
                    if url and official_ok(url):
                        pop = is_popular(rank, visits) and not official_deep(url)
                        o = _site(url, f"{title} — official site", (lead or "")[:300],
                                  official_score(box, i, url, bool(own) and pop, bool(own), asked and i == 0), pop)
                        o["official"] = True
                        o["exact_of"] = title                  # the exact title it is the site of (SIGILS)
                        o["of_kind"] = box_kind(box)           # official_top: an organisation's link is not pinned
                                                               # (not "kind": that key marks a kinds-tier result)
                        if box_kind(box) == "place" and OFFICIAL_FIRST != "all" and not asked:
                            o["place"] = True                  # demote_weak_names: a place's site never leads by its name
                        out.append(o)
            else:
                for i, (title, lead) in enumerate(spaced_as_asked(c.execute(WIKI_EXACT_SQL, {"nq": nq}).fetchall(), q)):
                    out.append(_wiki(title, lead, WIKI_EXACT_SCORE - i))
                    exact_titles.append(title)
            if len(exact_titles) > 1 and "suggest_items" in has:
                weak = namesakes(exact_titles, dict(c.execute(WIKI_DEMAND_SQL, {"t": exact_titles}).fetchall()), sited)
                for r in out:
                    if r.get("source") == "wikipedia" and r["title"].rsplit(" — Wikipedia", 1)[0] in weak:
                        r["score"], r["namesake"] = NAMESAKE_SCORE, True
        # a bare surname: the far better-known person it ends the name of leads, with its card
        # (not when the query names a platform: "trump youtube", "r/trump")
        if ("suggest_keys" in has and not acct and not yt_key and not sub_key
                and len(q.split()) <= 3 and 3 <= len(nq) <= 30):
            famous = famous_namesake(nq, c.execute(FAMOUS_SQL, {"k": nq}).fetchall())
            tk = (sports_keys(q) or [nq])[0]                 # "ny giants" -> newyorkgiants
            team = (major_team(tk, c.execute(MAJOR_TEAM_SQL, {"k": tk, "boxes": MAJOR_TEAM_BOXES}).fetchall())
                    if "wiki_facts" in has else None)
            org = org_lookup(c, nq) if "wiki_sites" in has else (None, None)
            org_rank = org[0]
            if not team and famous and org_rank is not None and org_rank <= CONFIDENT_RANK:
                org_lead = (c.execute(FAMOUS_LEAD_SQL, {"t": org[1]}).fetchone() or [""])[0] if org[1] else ""
                if org_blocks_namesake(org_rank, famous, org_lead):
                    famous = None                          # "chase" is Chase Bank, not Daveigh Chase
                else:
                    org = (None, None)                     # "trump" is Donald Trump, not his Trump Organization
            if org[1]:
                org_first(out, exact_titles, org[1],
                          lambda t: (c.execute(FAMOUS_LEAD_SQL, {"t": t}).fetchone() or [""])[0])
            famous = team or famous
            town = town_article(q, c) if not famous and "wiki_docs" in has else None
            famous = famous or town                        # "marsh nj": Marsh Township, Marlow County
            if famous:
                lift_famous(out, exact_titles, famous,
                            lambda t: (c.execute(FAMOUS_LEAD_SQL, {"t": t}).fetchone() or [""])[0])
                for r in out:                              # "$Trump" and its site: behind the person "trump" means
                    t = r["title"].rsplit(" — Wikipedia", 1)[0] if r.get("source") == "wikipedia" else r.get("exact_of")
                    if t in sigil_titles and t != famous:
                        r["score"], r["namesake"] = NAMESAKE_SCORE, True
                        r.pop("official", None)
            if team:
                site_over_article(out, team)
            if "wiki_sites" in has and not team and (len(q.split()) == 1 or PRODUCT_TIER_WIDE):
                lead_of = lambda t: (c.execute(FAMOUS_LEAD_SQL, {"t": t}).fetchone() or [""])[0]
                product_results(maker_products(nq, c.execute(PRODUCT_SQL, {"k": nq}).fetchall(), lead_of,
                                               PRODUCT_TIER_WIDE), out, famous, lead_of)
        if (PRODUCTS and _pt.get() is not False and not acct and not yt_key and not sub_key and 1 <= len(q.split()) <= 6
                and not product_leads_list(out) and (pt := products_table()) is not None):
            with stage("product"):
                ph = pt.lookup(q)
            if ph:
                product_candidates(ph, q, out, SITE_PAGES)
        if PRODUCT_SITE_FIRST != "0" and wiki and not acct:
            product_site_lead(nq, out, PRODUCT_SITE_FIRST)
        # a kind of site ("password manager"): its best-known sites above the article
        sk = stock_query(q) if "stock_keys" in has else None
        if sk:
            cls = re.search(r"\bclass ([abc])\b", q.lower())       # "berkshire class b stock" -> BRK-B
            row = c.execute(STOCK_SQL, {"k": sk, "cls": cls.group(1).upper() if cls else "#"}).fetchone()
            if row:
                out.extend(stock_results(row))
        elif "stock_tickers" in has and _BARE_TICKER.fullmatch(" ".join(q.lower().split())):
            row = c.execute(STOCK_BARE_SQL, {"k": q.strip()}).fetchone()
            if bare_ticker_ok(q, row) and not named_site(q, out):
                out.extend(stock_results(row[:6]))
        kind_lifted: set[str] = set()
        if kq and "kind_keys" in has and not acct and not yt_key and not sub_key and kind_worded(q):
            kkeys = [r[0] for r in c.execute(KIND_KEYS_SQL, {"key": kq, "w": kq.split(), "min": KIND_MIN_MEMBERS})]
            tasks = task_kinds(q)
            for tk in tasks:          # "merge pdf files" -> "pdf merger": exact names only
                kkeys += [r[0] for r in c.execute(KIND_KEY_EXACT_SQL, {"k": tk, "m": KIND_MIN_MEMBERS}) if r[0] not in kkeys]
            if "kind_vecs" in has:
                with stage("kind_vec"):
                    for probe in [kq] + tasks[:1]:
                        kkeys += [x for x in near_kinds(c, probe) if x not in kkeys]
            if news_only(q):                                   #: the kinds news sites are actually under
                kkeys = [r[0] for nk in NEWS_KINDS
                         for r in c.execute(KIND_KEY_EXACT_SQL, {"k": nk, "m": KIND_MIN_MEMBERS})] or kkeys
            rows = (c.execute(pop_sql(kind_key_sql(c)), {"keys": kkeys, "k": KIND_NAMED_POOL, "exact": kq,
                                                "exact_w": KIND_EXACT_W}).fetchall() if kkeys else [])
            good = [r for r in rows if r[4] >= KIND_GOOD][:KIND_TOP + 8]
            if len(good) >= KIND_MIN_GOOD:
                with stage("kind_fit"):                        #: what the sites are, and the one the query names
                    rows = [r for r in rows if not kind_unfit(r[0])]
                    good = kind_named_first(q, [r for r in good if r in rows], rows)
            if len(good) >= KIND_MIN_GOOD:
                base = kind_base(q, bool(exact_titles))
                for i, (url, title, descr, dom, _sc) in enumerate(good[:KIND_TOP]):
                    r = _kind(url, title, descr, base - i)
                    r["kind_top"] = i == 0 and base == KIND_SCORE
                    if r["kind_top"]:
                        r["kind_card"] = kind_card(q, good[:KIND_CARD_N])
                    out.append(r)
                    kind_lifted.add(dom)
        # a site named after the query with an add-on word (playmeadowlands.com)
        if (sites and not acct and not yt_key and not sub_key and len(q.split()) <= 3 and len(nq) >= 5
                and not any(r["score"] >= NAV_SCORE - 1 and r.get("source") == "site" for r in out)):
            sn = self_named(q, c.execute(pop_sql(SELF_NAMED_SQL), {"like": f"%{nq}%"}).fetchall())
            if sn:
                r = _site(sn[0], sn[1], sn[2], NAV_SCORE - 1, True)
                r["famous"] = True
                out.append(r)
        if "wiki_links" in has and not acct and exact_titles:  # the named thing's own accounts
            out.extend(profile_results(c.execute(PROFILES_SQL, {"t": exact_titles[0], "p": PROFILE_PLATFORMS}),
                                       WIKI_EXACT_SCORE,
                                       _wiki(exact_titles[0], "", 0.0)["url"]))
        if "wiki_next" in has and exact_titles:                # what its readers open next
            out.extend(related_results(c.execute(RELATED_SQL, {"t": exact_titles[0], "n": RELATED_N})))
        # a name of four or more words gets no domain keys (nav_keys) and was never asked here: 136 of 750 site-nav dev
        # queries (rc5 plan W6). The exact name is evidence enough; an asked name ("x website") is asked without the tail.
        link_key = norm_key(asked_name) if asked_name else nq
        long_name = len([t for t in _TOKEN.findall(q.lower()) if t not in _NAV_NOISE]) > 3
        if ("wiki_links" in has and not acct and (keys or long_name) and 3 <= len(link_key) <= 40
                and not any(r["score"] >= WIKI_EXACT_SCORE - 3 for r in out)):
            for url, name, desc, links in c.execute(LINK_SITE_SQL, {"key": link_key}):
                if not official_ok(url):
                    continue
                o = _site(url, f"{name} — official site", desc, WIKI_EXACT_SCORE - 1)
                o["official"] = True
                if link_low(links):
                    o["link_low"] = True                       #: brand_site_first, link_low_yields
                out.append(o)
            if BRAND_SITES and not any(r["score"] >= WIKI_EXACT_SCORE - 3 for r in out) and (b := brand_sites().get(link_key)):
                if official_ok(b[0]):
                    o = _site(b[0], f"{b[1]} — official site", None, WIKI_EXACT_SCORE - 1)
                    o["official"] = True
                    out.append(o)
        if "youtube_channels" in has and not yt_key and not yt_topic and not sub_key and not acct and 3 <= len(nq) <= 40:
            for row in c.execute(YT_BARE_SQL, {"key": nq, "min": YT_BARE_MIN}):
                ch = bare_channel_row(_youtube(*row, bare_channel_score(nq, out, len(q.split()), row[1], row[3])), row[3], out)
                if ch:
                    out.append(ch)
        t_stage = stage_since("exact", t_stage)                # the named tiers above, names included
        # a one-off place nearby named exactly this: sites merely named like it go under it. Not when an article
        # has the name too — there the article leads and demote_weak_names() places the sites
        pl_named = f_exact.result()[0] if f_exact is not None and pq and pq[0] == "name" else None
        if pl_named and not exact_titles:
            name_sites_under_place(pq[1], pl_named, out)
        if f_places is not None:                               # places (tier 1.5)
            # Local intent — a category word ("gym", "park") or a near-word ("dollar general near
            # me", "nearest pharmacy"): the nearby places lead. At 950 they lost to anchor and
            # namesake hits at 998-1001 ("gym" -> a WordPress theme, "park" -> Linkin Park), and a
            # chain's site beat its own locations for "… near me" (local yardstick v3, 2026-10-02).
            if pq and (pq[0] == "kinds" or _NEAR.search(q.lower())):
                place_base = NAV_SCORE + PLACE_LEAD
            # a name that is also an article or a site ("wells fargo"): the thing first, its
            # nearby branches under it; a place known only as a place ("joe's pizza") leads
            elif pq and pq[0] == "name" and (exact_titles or any(
                    r.get("source") == "site" and r["score"] >= NAV_SCORE - 1 for r in out)):
                place_base = WIKI_EXACT_SCORE - PLACE_UNDER_THING
            rows = f_places.result()
            if pq[0] == "name" and _NEAR.search(q.lower()):
                # "… near me" with a loose name match: a far business that merely sounds like the
                # words is not what was asked for; an exact name or brand is kept at any range
                rows = [r for r in rows if r[8] <= NEAR_FUZZY_KM or r[1].lower() == pq[1]]
            if not rows and f_places_fb is not None:
                rows = f_places_fb.result()
            if pq[0] == "name":
                rows = exact_name_first(rows, pq[1])
                near_named_km = min((row[8] for row in rows), default=None)
            info = place_hours_info([row[0] for row in rows])
            listed = [with_hours(_place(*row[:9], score=place_base - i, lat=row[9], lon=row[10]), row[5], row[2], info.get(row[0]))
                      for i, row in enumerate(rows)]
            chain_fill(listed, {row[0]: row[1] for row in rows})       # a branch with none: its chain's usual hours
            out += listed
        pt = f_town.result()[0] if f_town is not None else None
        if pt and town_reading_far(pt[1], near_named_km, _stated(" ".join(q.lower().replace(",", " ").split()))[1]):
            pt = None                                          #: a place nearby has the whole name
        if pt:                                                 # "<name> <town>": that place leads
            # as a named place does: under an article or a site the whole query names, else first — and above the
            # loose whole-string matches of the list above, which it replaces when they are the same place
            under = exact_titles or any(r.get("source") == "site" and r["score"] >= NAV_SCORE - 1 for r in out)
            base = WIKI_EXACT_SCORE - PLACE_UNDER_THING if under else PLACE_NAME_SCORE
            rows = place_order(pt[1], place_keys(pt[0]), backroom=True)[:3]
            got = {r[0] for r in rows}
            out[:] = [r for r in out if not (r.get("source") == "place" and r.get("pid") in got)]
            for r in out:
                if r.get("source") == "place":
                    r["score"] = round(min(r["score"], base - len(rows)), 2)
            info = place_hours_info([r[0] for r in rows])
            for i, r in enumerate(rows):
                out.append(dict(with_hours(_place(*r[:8], r[10], score=base - i, lat=r[8], lon=r[9]), r[5], r[2],
                                           info.get(r[0])), town_name=pt[0]))
        if f_exact_anchor is not None:                         # what many sites call one site
            ea = exact_anchor(f_exact_anchor.result())
            same = [r for r in out if same_page(r["url"]).split("/")[0] == same_page(ea["url"]).split("/")[0]] if ea else []
            if ea and not same:
                out.append(ea)
            for r in same:
                # many sites call the maker's site by this word and this is the maker's own page for it: the page
                # takes the place the site would have had ("snapdragon": qualcomm.com/snapdragon, not under an
                # article about a flower; "edge": microsoft.com/edge)
                if r.get("product") and r["score"] < ea["score"]:
                    r["score"], r["popular"], r["anchor_named"] = ea["score"], True, True
        if f_exact is not None:                                # a named place's own site
            own = place_site(f_exact.result()[0], out)
            if own:
                out.append(own)
        best = 0.0                                             # tier 3: keyword + semantic, fused
        lists: list[list[dict]] = []
        # an entity's name typed in two words or more: word lists keep sites holding two of its title's words
        # (a one-word query is left alone: "qashqai" -> Nissan Qashqai must keep its model pages)
        npairs = name_pairs(exact_titles[0]) if exact_titles and len(name_pairs(q)) >= 1 else []
        if f_wiki is not None:
            lw = []
            for title, lead, bm25 in f_wiki.result():
                lw.append(_wiki(title, lead, bm25))
                best = max(best, float(bm25))
            lists.append(_tl("wiki", lw))
        if f_sites is not None:
            ls = []
            for url, title, desc, bm25, score in f_sites.result():
                ls.append(_site(url, title, desc, score))
                best = max(best, float(bm25))
            lists.append(_tl("sites", named_only(sorted(ls, key=lambda r: r["score"], reverse=True), npairs)))
        if f_se is not None:
            lq = []
            for site, qid, title, answer, bm25, votes in f_se.result():
                score = float(bm25) * (1 + 0.05 * math.log1p(max(0, votes)))
                lq.append({"url": f"https://{site}/questions/{qid}", "title": f"{title} — {site}",
                           "content": (answer or "")[:300], "source": "stackexchange", "score": round(score, 2)})
                best = max(best, float(bm25))
            lists.append(_tl("se", sorted(lq, key=lambda r: r["score"], reverse=True)))
        if sub_topic:                                          # subreddits about the topic
            found = [(n, t, d, sb, float(b) * (1 + 0.1 * math.log1p(sb)))
                     for n, t, d, sb, b in c.execute(SUB_SEARCH_SQL, {"q": sub_topic})]
            lists.append(_tl("subreddits", [_subreddit(n, t, d, sb, sc) for n, t, d, sb, sc in sorted(found, key=lambda x: -x[4])[:5]]))
        if yt_topic:                                           # channels about the topic
            found = [(row[:6], float(row[6]) * (1 + 0.05 * math.log1p(row[4] or 0)))
                     for row in c.execute(YT_SEARCH_SQL, {"q": yt_topic})]
            lists.append(_tl("youtube", [_youtube(*r, sc) for r, sc in sorted(found, key=lambda x: -x[1])[:5]]))
        if f_anchors is not None:
            lists.append(_tl("anchors", named_only(anchor_list(f_anchors.result()), npairs)))
        if f_d2q is not None:
            lists.append(_tl("d2q", anchor_list(f_d2q.result())))
        weights: list[float] = [1.0] * len(lists)
        if f_pages is not None:
            pk, pw = _pages_fuse.get() or (PAGES_FUSE_K, PAGES_FUSE_W)
            rows, had = f_pages.result(), set()
            if f_gov is not None:                              # the named state's government pages, at full weight
                first = f_gov.result()
                had = {r[0] for r in first}
                lg = [_site(url, title, desc, bm25) for url, title, desc, bm25 in first]
                best = max([best] + [float(r[3]) for r in first])
                lists.append(_tl("gov_pages", other_states_last(gq, lg)))
                weights.append(1.0)
            if f_local is not None:                            # the searcher's own town's and county's pages
                loc = f_local.result()
                had |= {r[0] for r in loc}
                lists.append(_tl("local_pages", [_site(url, title, desc, bm25) for url, title, desc, bm25 in loc]))
                weights.append(1.0)
            lp = []
            for r in (r for r in rows if r[0] not in had):
                url, title, desc, bm25 = r[:4]
                lp.append(dict(_site(url, title, desc, bm25), **(se_credit(url, r[4]) if len(r) > 4 else {})))
                best = max(best, float(bm25))
            if pk > 0 and pw > 0:
                lists.append(_tl("pages", other_states_last(gq, lp)[:pk]))
                weights.append(pw)
        if "wiki_links" in has and not acct and 3 <= len(nq) <= 40:
            lists.append(_tl("described", [_described(*row) for row in c.execute(DESCRIBED_SQL, {"key": nq})]))
        if f_kind is not None and not kind_lifted:      # sites of the kind the words describe
            lists.append(_tl("kind", named_only([_kind(url, title, descr, sc) for url, title, descr, _d, sc in f_kind.result()], npairs)))
        if f_kind_site is not None and not kind_lifted:  # ... and the sites whose meaning is closest
            lists.append(_tl("kind_site", named_only([_kind(url, title, descr, sim) for url, title, descr, _d, sim in f_kind_site.result()], npairs)))
        sem = f_sem.result() if f_sem is not None else None
        if sem is not None:
            lists.append(_tl("sem", [_wiki(title, lead, sim) for title, lead, sim in sem]))
        if f_sem_pages is not None:
            sp = f_sem_pages.result()
            if sp:                                             # the pages nearest in meaning
                weights += [1.0] * (len(lists) - len(weights))
                lists.append(_tl("sem_pages", [dict(_site(url, title, desc, sim), sem_page=True) for url, title, desc, sim in sp]))
                weights.append(SEM_PAGES_W)
        sp_rows = site_page_rows(q, out)                       #: a named site's own pages; off unless SEARCH_SITE_PAGES
        if sp_rows:
            weights += [1.0] * (len(lists) - len(weights))
            lists.append(_tl("site_pages", sp_rows))
            weights.append(SITE_PAGES_W)
        link_low_yields(out, lists, link_key)                  #: before the trace, so "named" is what is ranked
        if _trace.get() is not None:
            _tr("named", sorted(out, key=lambda r: r["score"], reverse=True))
        weights += [1.0] * (len(lists) - len(weights))
        fused = rrf(lists, weights=weights)
        _tr("fused", fused)
        out.extend(fused)
        stage_since("lists_wait", t_stage)                     # mostly waiting for the lists started at the top
        if not out or (out[0]["score"] < WIKI_EXACT_SCORE and best < STRONG_BM25):   # tier 4
            n_out = len(out)
            c.execute(f"SET pg_trgm.similarity_threshold = {FUZZY_SIM}")
            if sites and keys:
                with stage("fuzzy_site"):
                    for url, title, desc, sim in c.execute(FUZZY_SITE_SQL, {"key": keys[0].replace("-", "")}):
                        out.append(_site(url, title, desc, 5 + 10 * float(sim)))
            if wiki:
                with stage("fuzzy_wiki"):
                    for title, lead, sim in c.execute(FUZZY_WIKI_SQL, {"q": q}):
                        out.append(_wiki(title, lead, 5 + 10 * float(sim)))
            _tr("fuzzy", out[n_out:])
    demote_weak_names(out)
    if kind_lifted:
        demote_name_matches(q, out, kind_lifted)
    root_beats_inner_pages(out)
    official_subdomain_yields(out)
    if pq and pq[0] == "name":
        brand_site_first(q, out, pl_named)
    # Highest tier first; one entry per URL (a site can come back from two tiers).
    out.sort(key=lambda r: r["score"], reverse=True)
    seen, uniq = {}, []
    for r in out:
        key = result_key(r)
        if key not in seen:
            seen[key] = len(uniq)
            uniq.append(r)
        elif r.get("kind_top"):         # the lifted kind's first site also came back as a name
            uniq[seen[key]] = dict(uniq[seen[key]], kind_top=True,   # match: keep it pinned
                                   **({"kind_card": r["kind_card"]} if r.get("kind_card") else {}))
    _tr("merged", uniq)
    if learned and uniq and not keep_top(q, uniq[0]):
        hand_top = uniq[0]
        with stage("ranker"):
            uniq = rerank(q, uniq[:20])
        if promoted(hand_top, uniq[0]):
            # The model may reorder, never make an answer `confident`: skipping the web stays
            # the hand rules' call ("sport" -> sport.ro was promoted and marked confident).
            uniq[0] = dict(uniq[0], reranked_top=True)
        _tr("ranker", uniq)
    # Web-only wording is not reordered by this model. It used to be skipped because such a query was answered
    # with nothing; since 2026-10-04 the list is shown, and switched on for the 146 held-out queries the
    # model changed the first result of 56 with no gain (first result holding every query word: 25% -> 23%,
    # 2026-10-08): the right page is not among the candidates ("how to boil eggs" -> a Japanese music site).
    # Its score for the result shown first is still worth having: web_signal().
    if ce and uniq and not web_only(q, uniq):
        with stage("ce"):
            scorer = (ce_alt() if "cealt" in _drop.get()
                      else ce_gov() if gq and CE_GOV_DIR and "cegov" not in _drop.get() else None)
            uniq = ce_rerank(q, uniq, scorer)
        _tr("ce", uniq)
    uniq = one_home_page(uniq)                                 #
    uniq = other_endings_last(profiles_follow_article(local_page_first(hl, home_page_up(None if gs else gq, state_anchor_first(gs, state_page_first(gs, other_states_last(gq, uniq)))))))[:k]   # one site, one row
    with stage("site_names"):
        uniq = name_front_pages(uniq)                          #: after the ranking, so the rankers see what they were fitted on
    # after, in that order: a front page the release can name is no longer "a name and nothing else"
    return untitled_last(q, uniq)


# ---------------------------------------------------------------------------
# Prefix completion (2026-09-24): what the start page / launcher shows while
# someone types. suggest_* tables are built by serve/suggest.sql. A 3-4 character
# prefix reads its precomputed top list; a longer one scans suggest_keys' range.
# ---------------------------------------------------------------------------
SUGGEST_MIN = 3
SUGGEST_N = 8


def _weights(raw: str) -> dict[str, float]:
    """"site=1,wiki=0.5" -> {"site": 1.0, "wiki": 0.5}; malformed parts are ignored."""
    out = {}
    for part in raw.split(","):
        k, _, v = part.partition("=")
        try:
            out[k.strip()] = float(v)
        except ValueError:
            pass
    return out


# How kinds weigh against each other (suggest.sql stores popularity WITHIN a kind).
# Tuned with tools/eval-completion.py --suggester tune; override without a rebuild via
# SEARCH_SUGGEST_WEIGHTS. hist = the browser-history lift; exact / exact_wiki = the lift
# for a key typed out in full ("cnn") — small for articles, or "bra" means the Bra article.
# 2026-09-24 on 75 real labels: top 3 at 3/4/5 chars 37%/52%/55% (sites 45%/64%/62%)
# vs the mockup lookup's 25%/31%/27%. hist is nearly inert today (17 sites carry visits).
SUGGEST_WEIGHTS = {"site": 1.0, "wiki": 0.2, "youtube": 0.6, "service": 0.95, "subreddit": 0.7,
                   "hist": 0.5, "exact": 0.2, "exact_wiki": 0.1, "foreign": 0.2}
# Plumbing, not a destination: costco-static.com, fbcdn.net, twimg.com-style hosts, and
# ad / tag / hosting back ends that crawl pages of their own (amazon-adsystem.com,
# amazonaws.com). suggest.sql drops the ones with no pages at all.
_SUGGEST_PLUMBING = re.compile(r"(^|-)(static|cdn|img|images|assets|api|edge|media|cache|s3)(-|$)|cdn|img$"
                               r"|adsystem|adservice|adserver|syndication|tagmanager|analytics|amazonaws|usercontent")
SUGGEST_WEIGHTS.update(_weights(os.environ.get("SEARCH_SUGGEST_WEIGHTS", "")))
_ITEM = "i.id, i.kind, i.name, i.url, i.domain, i.wiki, i.pop, i.hist"
SUGGEST_TOP_SQL = (f"SELECT {_ITEM} FROM suggest_top t CROSS JOIN LATERAL unnest(t.ids) u(id) "
                   "JOIN suggest_items i ON i.id = u.id WHERE t.prefix = %(p)s")
# From 5 characters: the best 12 of each kind in the key range (same shape as suggest_top).
SUGGEST_SCAN_SQL = (f"SELECT {_ITEM} FROM (SELECT id FROM (SELECT id, row_number() OVER "
                    "(PARTITION BY kind ORDER BY max(score) DESC) AS rn FROM suggest_keys WHERE key LIKE %(like)s "
                    "GROUP BY id, kind) x WHERE rn <= 12) k JOIN suggest_items i USING (id)")
SUGGEST_EXACT_SQL = f"SELECT {_ITEM} FROM suggest_keys k JOIN suggest_items i USING (id) WHERE k.key = %(p)s"

# Demand-ranked completion: suggest_items.demand = estimated monthly searches on one
# scale for every kind, descr = what the item is, and how the prefix matched (pos 0 = the
# name, 1 = a later word, 2 = an alias). A serving database built before lacks them
# and keeps the per-kind weighting above.
_ITEM2 = _ITEM + ", i.demand, i.descr"
SUGGEST_TOP2_SQL = (f"SELECT {_ITEM2}, u.pos FROM suggest_top t CROSS JOIN LATERAL unnest(t.ids, t.pos) u(id, pos) "
                    "JOIN suggest_items i ON i.id = u.id WHERE t.prefix = %(p)s")
SUGGEST_SCAN2_SQL = (f"SELECT {_ITEM2}, k.pos FROM (SELECT id, pos FROM (SELECT id, min(pos) AS pos, row_number() OVER "
                     "(PARTITION BY kind ORDER BY max(score) - min(pos) DESC) AS rn FROM suggest_keys WHERE key LIKE %(like)s "
                     "GROUP BY id, kind) x WHERE rn <= 12) k JOIN suggest_items i USING (id)")
SUGGEST_EXACT2_SQL = f"SELECT {_ITEM2}, k.pos FROM suggest_keys k JOIN suggest_items i USING (id) WHERE k.key = %(p)s"
SUGGEST_V2_SQL = ("SELECT count(*) = 2 FROM information_schema.columns WHERE table_name = 'suggest_items' "
                  "AND column_name IN ('demand', 'descr')")
# log10(monthly searches) is the score; these nudge it. k_* = a small prior per kind; hist =
# browser history (0..1); exact = the key typed in full; later / alias = the prefix matched a
# later word / an alias, not the name; foreign = a country-code site. Tuned with
# tools/eval-completion.py --suggester tune; override with SEARCH_SUGGEST_WEIGHTS2.
# Tuned 2026-09-25 over 1,481 labels (eval-completion.py tune, 240 weightings). Interleaving
# this list with the old site-leaning one scored no better at any length on any label set,
# so a site bonus here is how sites keep their place beside famous names.
SUGGEST_WEIGHTS2 = {"k_site": 1.3, "k_wiki": 0.0, "k_youtube": -0.5, "k_service": 0.0, "k_subreddit": -0.3,
                    "hist": 1.5, "exact": 0.3, "later": 1.5, "alias": 1.0, "foreign": 1.0,
                    # Half an alias typed is weak evidence: "yankees" is the start of Freddie
                    # Freeman's Wikidata alias "yankees father" and out-scored the New York
                    # Yankees, whose own name has the word (2026-09-25). The whole alias typed
                    # ("satchmo") is not penalised further.
                    "alias_partial": 2.0}
SUGGEST_WEIGHTS2.update(_weights(os.environ.get("SEARCH_SUGGEST_WEIGHTS2", "")))
_suggest_v2 = (_NEVER, None)


def suggest_score2(kind: str, demand: float, hist: float, exact: bool, pos: int, w: dict | None = None) -> float:
    w = w or SUGGEST_WEIGHTS2
    return (math.log10(1 + max(0.0, demand or 0.0)) + w.get("k_" + kind, 0.0) + w["hist"] * (hist or 0.0)
            + (w["exact"] if exact else 0.0) - (w["later"] if pos == 1 else 0.0) - (w["alias"] if pos == 2 else 0.0)
            - (w.get("alias_partial", 0.0) if pos == 2 and not exact else 0.0))


def tight(s: str) -> str:
    """The key suggest.sql stores: letters and digits only, lowercased ("Wells Fargo" -> wellsfargo)."""
    return "".join(ch for ch in s.lower() if ch.isalnum())


def suggest_score(kind: str, pop: float, hist: float, exact: bool, w: dict | None = None) -> float:
    w = w or SUGGEST_WEIGHTS
    bonus = (w["exact_wiki"] if kind == "wiki" else w["exact"]) if exact else 0.0
    return w.get(kind, 0.5) * pop + w["hist"] * hist + bonus


def rank_suggestions(rows: list[tuple], exact_ids: set, n: int = SUGGEST_N, w: dict | None = None) -> list[dict]:
    """rows: (id, kind, name, url, domain, wiki, pop, hist). Best weighted score first,
    one entry per page."""
    best: dict[int, tuple] = {}
    for r in rows:
        # v2 rows carry pos last: an item matched two ways keeps its best (lowest) pos
        if r[0] in best and len(r) > 8 and len(best[r[0]]) > 8 and best[r[0]][10] <= r[10]:
            continue
        best[r[0]] = r
    v2 = bool(rows) and len(rows[0]) > 8
    ww = w or (SUGGEST_WEIGHTS2 if v2 else SUGGEST_WEIGHTS)

    def score(r):
        if v2:
            sc = suggest_score2(r[1], r[8], r[7], r[0] in exact_ids, r[10], ww)
        else:
            sc = suggest_score(r[1], r[6], r[7], r[0] in exact_ids, w)
        # A country-code site (costcojapan.jp) is a weaker guess for a US household
        # than a generic one — the same call the nav tier makes with GENERIC_TLD.
        if r[1] == "site" and r[4] and not is_generic(r[4]):
            sc -= ww.get("foreign", 0.0)
        return sc
    scored = sorted(((score(r), r) for r in best.values()), key=lambda x: (-x[0], x[1][0]))
    out, seen, brands, names = [], set(), set(), set()
    for sc, r in scored:
        i, kind, name, url, domain, wiki, pop, hist = r[:8]
        descr = r[9] if len(r) > 9 else None
        if kind == "site" and domain and _SUGGEST_PLUMBING.search(domain.split(".")[0]):
            continue
        key = same_page(url)
        if key in seen:
            continue
        # One site per brand: costco.com, not also costco.ca / costco.co.uk /
        # costco.com.mx (the start-page review, 2026-09-24). The best-scored one stays.
        brand = domain.split(".")[0] if kind == "site" and domain else None
        if brand and brand in brands:
            continue
        # One entry per thing: Taylor Swift was a site, an article and a channel.
        # The best-scored stays; subreddits and services are other things by nature.
        nk = tight(re.sub(r"\s*\(.*\)$", "", name)) if kind in ("site", "wiki", "youtube") else None
        if nk and nk in names:
            continue
        seen.add(key)
        if brand:
            brands.add(brand)
        if nk:
            names.add(nk)
        d = {"url": url, "title": name, "source": kind, "score": round(sc, 3)}
        if wiki:
            d["wiki"] = wiki
        if descr:
            d["subtitle"] = descr
        out.append(d)
        if len(out) == n:
            break
    return out


def suggest_candidates(q: str) -> tuple[list[tuple], set]:
    t = tight(q)
    if len(t) < SUGGEST_MIN:
        return [], set()
    global _suggest_v2
    with _conn() as c:
        if "suggest_top" not in tables(c):
            return [], set()
        if time.monotonic() - _suggest_v2[0] > TABLES_TTL_S:
            _suggest_v2 = (time.monotonic(), bool(c.execute(SUGGEST_V2_SQL).fetchone()[0]))
        if _suggest_v2[1]:
            rows = list(c.execute(SUGGEST_TOP2_SQL if len(t) <= 4 else SUGGEST_SCAN2_SQL, {"p": t, "like": t + "%"}))
            exact = list(c.execute(SUGGEST_EXACT2_SQL, {"p": t}))
        else:
            rows = list(c.execute(SUGGEST_TOP_SQL if len(t) <= 4 else SUGGEST_SCAN_SQL, {"p": t, "like": t + "%"}))
            exact = list(c.execute(SUGGEST_EXACT_SQL, {"p": t}))
    return rows + exact, {r[0] for r in exact}


def suggest_drop_junk(rows: list[tuple], q: str) -> list[tuple]:
    """Results hid a junk site unless the query named it; suggestions never looked (rc5 W3): typing "por" offered what
    searching hides. The same rule here, on what has been typed SO FAR: a flagged site is offered only once the
    typed text is its whole name or its domain."""
    dom = junk_domains()
    if not dom:
        return rows
    return [r for r in rows if r[1] != "site" or not r[4] or r[4] not in dom or query_names_domain(q, r[4])]


def suggest(q: str) -> list[dict]:
    rows, exact = suggest_candidates(q)
    return rank_suggestions(suggest_drop_junk(rows, q), exact)


# Phrase completions (step 4, serve/phrases.sql): searched phrases that extend what
# was typed ("air fr" -> "air fryer"). Keyed on the first PHRASE_KEY_MAX characters; a
# longer query is matched by filtering that prefix's phrases.
PHRASE_N = 3
PHRASE_KEY_MAX = 20
PHRASE_MIN_SEARCHES = float(os.environ.get("SEARCH_PHRASE_MIN", "200"))
SUGGEST_PHRASES_SQL = "SELECT phrases, counts FROM suggest_phrases WHERE prefix = %(p)s"


def phrase_key(q: str) -> str:
    """How phrases.sql keys a prefix: lowercased, spaces collapsed, trailing space dropped."""
    return " ".join(q.lower().split())


def pick_phrases(q: str, phrases: list[str], counts: list[float], items: list[dict], n: int = PHRASE_N) -> list[str]:
    """The phrases worth offering: they extend what was typed (never the typed text itself),
    were searched often enough, and do not just repeat a destination already listed
    ("amazon" beside the Amazon site)."""
    k = phrase_key(q)
    shown = {tight(re.sub(r"\s*\(.*\)$", "", d["title"])) for d in items}
    out = []
    for ph, c in zip(phrases or [], counts or []):
        if not ph.startswith(k) or ph == k or (c or 0) < PHRASE_MIN_SEARCHES or tight(ph) in shown:
            continue
        out.append(ph)
        if len(out) == n:
            break
    return out


def suggest_phrases(q: str, items: list[dict]) -> list[str]:
    k = phrase_key(q)
    if len(k) < 3:
        return []
    with _conn() as c:
        if "suggest_phrases" not in tables(c):
            return []
        row = c.execute(SUGGEST_PHRASES_SQL, {"p": k[:PHRASE_KEY_MAX].rstrip()}).fetchone()
    return pick_phrases(q, row[0], row[1], items) if row else []


def wordy(q: str) -> bool:
    """Full results are worth showing once the query looks finished: 5+ characters or a
    space. A bare prefix ("lin") only matches whole words by accident (Lin Tsai-pei)."""
    q = " ".join(q.split())
    return len(q) >= 5 or " " in q


def org_named(q: str) -> bool:
    """The query is an organisation's exact name (ORG_SQL), looked up once per query."""
    nq = norm_key(q)
    if not 3 <= len(nq) <= 40:
        return False

    def look():
        with _conn() as c:
            return [org_lookup(c, nq)[1] is not None]
    try:
        return cached(("org", nq), look, static=True)[0]
    except psycopg.Error:
        return False


def short_org(q: str) -> bool:
    """"sofi", "ikea", "hulu": a short query that is an organisation's exact name gets its
    full results (and card) at once; "lin" (a surname, a site) still waits for more letters."""
    nq = norm_key(q)
    return 3 <= len(nq) <= 4 and " " not in q.strip() and org_named(q)


class Handler(BaseHTTPRequestHandler):
    def log_request(self, code="-", size="-"):  # the default access log line contains the URL, i.e. the query
        pass

    def finish(self):
        """After the response: this request thread's SQLite connection goes back to the release lookups' free list
        instead of lingering in a reference cycle until a full GC (43-50 open connections, SQLite heap 27 -> 380 MB)."""
        try:
            super().finish()
        finally:
            if _lookups is not None and hasattr(_lookups, "release"):
                _lookups.release()

    def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
        """A client that hung up before the reply (a /readyz probe that gave up, a closed tab) is not an
        engine error: no traceback, and the connection is closed instead of read again."""
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except ConnectionError:                     # BrokenPipe, ConnectionReset, ConnectionAborted (Windows)
            self.close_connection = True

    def do_GET(self):  # noqa: N802
        if not client_allowed(self.client_address[0]):
            return self._send(403, b'{"error":"forbidden"}')
        u = urlsplit(self.path)
        if u.path == "/memz" and os.environ.get("SEARCH_MEMSTAT") == "1":     # box measurements only (memstat.py)
            return self._send(200, __import__("memstat").report(globals(), parse_qs(u.query)))
        if u.path == "/healthz":
            try:
                with _conn() as c:
                    c.execute("SELECT 1")
                return self._send(200, b"ok", "text/plain")
            except Exception as e:
                return self._send(503, f"db unavailable: {type(e).__name__}".encode(), "text/plain")
        if u.path == "/readyz":
            # /healthz = the process and the database are up. /readyz = searches can answer
            #: the serving tables are visible and a fixed query returns a result.
            try:
                with _conn() as c:
                    has = tables(c)
                if "wiki_docs" not in has and "docs" not in has:
                    return self._send(503, b"starting: no serving tables visible", "text/plain")
                if not search("wikipedia", 1, quiet=False):
                    return self._send(503, b"starting: the probe search answered nothing", "text/plain")
                return self._send(200, b"ready", "text/plain")
            except Exception as e:
                return self._send(503, f"starting: {type(e).__name__}".encode(), "text/plain")
        if u.path in ("/instant", "/more", "/search", "/explain") and not serving_ready():
            return self._send(503, b'{"error":"starting"}')
        if u.path == "/storez":                          # the warmer: rows, fingerprint, visitors in flight (back network only)
            st = result_store()
            z = {"busy": _busy[0], **(st.counts() if st else {"off": True})}
            sample = parse_qs(u.query or "").get("sample")
            if st and sample and sample[0].isdigit():     # the warmer audits rows another engine computed
                z["sample"] = st.sample_queries(min(int(sample[0]), 200))
            return self._send(200, json.dumps(z).encode())
        if u.path == "/instant":
            visitor = "warm=1" not in (u.query or "")
            if visitor:
                with _busy_lock:
                    _busy[0] += 1
            try:
                return self._instant(parse_qs(u.query))
            finally:
                if visitor:
                    with _busy_lock:
                        _busy[0] -= 1
        if u.path == "/more":
            return self._more(parse_qs(u.query))
        if u.path == "/explain":
            return self._explain(parse_qs(u.query))
        if u.path == "/candidates":                      # dev tooling: the lists and each reordering, with scores
            return self._send(*__import__("sq_candidates").handle(parse_qs(u.query)))
        if u.path != "/search":
            return self._send(404, b'{"error":"not found"}')
        qs = parse_qs(u.query)
        q = (qs.get("q") or [""])[0].strip()[:200]
        try:
            k = max(1, min(MAX_K, int((qs.get("k") or ["10"])[0])))
        except ValueError:
            k = 10
        if len(q) < 2:
            return self._send(200, b'{"results":[]}')
        t0 = time.monotonic()
        try:
            hw = float((qs.get("hw") or [""])[0])
            _head_w.set(hw if 0 <= hw <= 20 else None)      # eval sweeps only
        except ValueError:
            _head_w.set(None)
        _no_anchors.set((qs.get("anc") or ["1"])[0] == "0")
        _pages_fuse.set(parse_pages_fuse(qs))
        _off.set(parse_off((qs.get("off") or [""])[0]))
        _drop.set(parse_drop(qs))
        st = {}
        _stages.set(st)
        _stages_cpu.set({})
        _d2q.set((qs.get("d2q") or ["0"])[0] == "1")
        _no_intent.set((qs.get("intent") or ["1"])[0] == "0")
        _no_webrr.set((qs.get("webrr") or ["1"])[0] == "0")
        _pt.set({"1": True, "0": False}.get((qs.get("pt") or [""])[0]))
        _warm.set(False)
        _websig.set({"1": True, "0": False}.get((qs.get("websig") or [""])[0]))
        _profile_cap.set(parse_profile_cap((qs.get("pcap") or [""])[0], PROFILE_CAP))      # eval A/B only
        _accounts_in_card.set(parse_aic((qs.get("aic") or [""])[0], ACCOUNTS_IN_CARD))     # eval A/B only
        # the searcher's point (the public page's browser location, rounded to ~100 m; never
        # logged) — places are measured from it instead of SEARCH_HOME (2026-10-02)
        _near.set(parse_near((qs.get("near") or [""])[0]))
        _reach_km.set(parse_reach((qs.get("reach") or [""])[0]))
        bv = (qs.get("body") or ["full"])[0]
        _body.set(bv if bv in BODY_VARIANTS else "full")
        try:
            res = search(q, k, semantic=(qs.get("sem") or ["1"])[0] != "0",
                         learned=(qs.get("rank") or ["1"])[0] != "0",
                         correct=(qs.get("fix") or ["1"])[0] != "0",
                         quiet=(qs.get("quiet") or ["1"])[0] != "0",
                         ce=(qs.get("ce") or ["1"])[0] != "0")
        except Exception as e:
            # SearXNG treats an engine error like a timeout and shows the web results.
            # Never log the query text: container logs ship to VictoriaLogs (14 days),
            # and a private search engine must not keep a search history by accident.
            self.log_message("search failed: %s: %s", type(e).__name__, e)
            return self._send(503, json.dumps({"error": type(e).__name__}).encode())
        sig = {}
        if web_only(q, res):
            res = web_rerank(q, res)
            sig = web_signal(q, res)
        intent = intent_of(q, res)
        conf = is_confident(res) and intent == "nav"
        self.log_message("results=%d confident=%d intent=%s ms=%d stages=%s", len(res), conf, intent,
                         (time.monotonic() - t0) * 1000, stages_text(st))
        res = [{k: v for k, v in r.items() if k != "kind_card"} if "kind_card" in r else r for r in res]
        body = {"results": res, "confident": conf, "intent": intent}
        body.update(sig)                                   # top_ce / best_ce / weak; nothing when off
        if res and res[0].get("corrected"):
            body["corrected"] = res[0]["corrected"]        # "Showing results for …"
        self._send(200, json.dumps(body).encode())


    def _more(self, qs) -> None:
        """One section of the start page opened on its own: what=results,
        the full results (MAX_K); what=news, a week of stories."""
        q = (qs.get("q") or [""])[0].strip()[:200]
        what = (qs.get("what") or [""])[0]
        if len(q) < 2 or what not in ("results", "news"):
            return self._send(400, b'{"error":"q and what=results|news"}')
        t0 = time.monotonic()
        _near.set(parse_near((qs.get("near") or [""])[0]))
        _reach_km.set(parse_reach((qs.get("reach") or [""])[0]))
        _profile_cap.set(parse_profile_cap((qs.get("pcap") or [""])[0], PROFILE_CAP))      # eval A/B only
        _accounts_in_card.set(parse_aic((qs.get("aic") or [""])[0], ACCOUNTS_IN_CARD))     # eval A/B only
        st = {}
        _stages.set(st)
        _stages_cpu.set({})
        try:
            with stage(what):
                if what == "results":
                    body = {"results": search(q, MAX_K)}
                elif "offset" in qs:                        # "More news", page by page (one cached ranking)
                    try:
                        off = min(max(int((qs.get("offset") or ["0"])[0]), 0), NEWS_PAGED_MAX)
                    except ValueError:
                        off = 0
                    every = cached(("newsmore", q.lower()), lambda: [news_week(q)])[0]
                    body = {"news": every[off:off + NEWS_PAGE_N], "more": off + NEWS_PAGE_N < len(every)}
                else:
                    body = {"news": weather_news(more=True) if weather_query(q) else news(q, more=True)["stories"]}
        except Exception as e:
            self.log_message("more failed: %s: %s", type(e).__name__, e)   # never the query text
            return self._send(503, json.dumps({"error": type(e).__name__}).encode())
        ms = int((time.monotonic() - t0) * 1000)
        self.log_message("more what=%s n=%d ms=%d stages=%s", what, len(next(iter(body.values()))), ms, stages_text(st))
        body.update(ms=ms, stages=st)                   # the page's timing line (names and numbers only)
        self._send(200, json.dumps(body).encode())

    def _explain(self, qs) -> None:
        """How the full results for q were put together (the page's "How this was ranked", 2026-10-08): the
        search run once more with a trace on — never from the result cache and never into it, so no other
        request waits for this or is changed by it. Asked for only when someone presses the button."""
        q = (qs.get("q") or [""])[0].strip()[:200]
        if len(q) < 2:
            return self._send(400, b'{"error":"q"}')
        _near.set(parse_near((qs.get("near") or [""])[0]))
        _reach_km.set(parse_reach((qs.get("reach") or [""])[0]))
        try:
            body = cached(("explain", q, home_point(), place_radius_km()), lambda: [explain(q)], static=True)[0]
        except Exception as e:
            self.log_message("explain failed: %s: %s", type(e).__name__, e)   # never the query text
            return self._send(503, json.dumps({"error": type(e).__name__}).encode())
        self.log_message("explain steps=%d ms=%d", len(body["steps"]), body["ms"])
        self._send(200, json.dumps(body).encode())

    def _instant(self, qs) -> None:
        """As-you-type: prefix suggestions on every keystroke, plus the full results
        once the query looks finished (unless results=0). ?all=1 lists every candidate with
        its raw signals, for tools/eval-completion.py --suggester tune."""
        q = (qs.get("q") or [""])[0].strip()[:200]
        t0 = time.monotonic()
        _near.set(parse_near((qs.get("near") or [""])[0]))
        _reach_km.set(parse_reach((qs.get("reach") or [""])[0]))
        _no_webrr.set((qs.get("webrr") or ["1"])[0] == "0")      # eval A/B only
        _warm.set((qs.get("warm") or ["0"])[0] == "1")          # warm.py's head-query refresh (2026-11-04)
        _audit.set(_warm.get() and (qs.get("audit") or ["0"])[0] == "1")
        _store_note.set(None)
        _cache_state.set("none")
        _websig.set({"1": True, "0": False}.get((qs.get("websig") or [""])[0]))      # eval A/B only
        _profile_cap.set(parse_profile_cap((qs.get("pcap") or [""])[0], PROFILE_CAP))      # eval A/B only
        _accounts_in_card.set(parse_aic((qs.get("aic") or [""])[0], ACCOUNTS_IN_CARD))     # eval A/B only
        _instant_list_first.set(parse_aic((qs.get("ilf") or [""])[0], INSTANT_LIST_FIRST))      # eval A/B only
        _score_all.set(parse_aic((qs.get("sa") or [""])[0], SCORE_ALL))                      # eval A/B only
        _se_on.set((qs.get("se") or ["1"])[0] != "0")                                         # eval A/B only (2026-11-10)
        _cur_fill.set(CURATED_FILL if "cf" not in qs else (qs.get("cf") or ["1"])[0] != "0")   # eval A/B (2026-11-11)
        _breadth.set((qs.get("bl") or [BREADTH])[0].strip().upper())                         # eval A/B only (2026-11-07)
        try:                                                                                  # eval A/B only (2026-11-07)
            _breadth_floor.set(float((qs.get("blf") or [""])[0] or BREADTH_FLOOR))
        except ValueError:
            _breadth_floor.set(BREADTH_FLOOR)
        try:                                             # eval A/B only: ?cg=<threshold>, 0 off (2026-11-05)
            _card_gate.set(float((qs.get("cg") or [""])[0]) if qs.get("cg") else CARD_GATE)
        except ValueError:
            _card_gate.set(CARD_GATE)
        _pages_fuse.set(parse_pages_fuse(qs))                                                    # eval A/B only
        if (qs.get("all") or ["0"])[0] == "1":
            # Every candidate with its raw signals, for tools/eval-completion.py --suggester tune.
            rows, exact = suggest_candidates(q)
            cands = [dict({"url": r[3], "title": r[2], "source": r[1], "wiki": r[5], "pop": r[6], "hist": r[7],
                           "exact": r[0] in exact, "id": r[0], "domain": r[4]},
                          **({"demand": r[8], "descr": r[9], "pos": r[10]} if len(r) > 8 else {})) for r in rows]
            return self._send(200, json.dumps({"candidates": cands}).encode())
        st = {}
        _stages.set(st)
        _stages_cpu.set({})
        try:
            # News rides every answer from 3 characters (a 4-letter "iran" never counts as
            # finished typing); its lookup runs beside the suggestions.
            f_news = (_EXEC.submit(contextvars.copy_context().run, _staged, "news", cached, ("news", q.lower()),
                                   lambda: [news(q)]) if len(q) >= 3 else None)
            want = (qs.get("results") or ["1"])[0] != "0"
            # a finished answer (fin_get): the list, card and place were built ahead; nothing below computes them
            full = (qs.get("full") or ["0"])[0] == "1"
            plain = fin_plain(qs)
            fin_full = full          # kept under its own name: "full" is reused below for an organisation's full name
            fin = (fin_get(q, full) if FINISHED and want and plain and not _warm.get() and (full or wordy(q) or short_org(q))
                   else None)
            # local (does q name a business with branches?) beside the search, not after it (2026-11-03: a whole-world
            # places lookup, 0.15-0.4 s for "home depot", ran after the clock stopped). Only when results are wanted.
            f_local = (_EXEC.submit(contextvars.copy_context().run, _staged, "names_places",
                                    lambda: cached(("names_places", q.lower()),
                                                   lambda: stored("names_places", (q.lower(),), lambda: [names_places(q)]), static=True)[0])
                       if want and q and home_point() is None and fin is None else None)
            with stage("suggest"):
                sug = suggest(q)
            with stage("phrases"):
                phrases = suggest_phrases(q, sug)
            # results=0: suggestions only — the page asks for full results separately, so a
            # slow full search (worst ~1.3 s cold) never holds up the as-you-type list (want, above).
            # full=1: a submitted search (address bar, search.logout.cloud#q=...) — results
            # whatever the length, and more of them; as-you-type keeps the wordy gate
            full = (qs.get("full") or ["0"])[0] == "1"
            with stage("results"):
                res = [] if fin is not None else search(q, MAX_K if full else 8) if want and (full or wordy(q) or short_org(q)) else []
            with stage("news_wait"):
                got_news = f_news.result()[0] if f_news is not None else {"stories": [], "hot": False}
            try:                                            # live scores: never cached
                with stage("sports"):
                    games = sports(q) if len(q) >= 3 else []
            except Exception as e:
                self.log_message("sports failed: %s", type(e).__name__)
                games = []
            try:                                            # a league table, from the news file
                with stage("standings"):
                    table = standings(q)
            except Exception as e:
                self.log_message("standings failed: %s", type(e).__name__)
                table = None
            try:                                            # the home forecast (never cached here)
                with stage("weather"):
                    wx = weather(q) if len(q) >= 3 else None
            except Exception as e:
                self.log_message("weather failed: %s", type(e).__name__)
                wx = None
            try:                                            # NFL/NBA team named (no page asks ESPN)
                with stage("espn"):
                    espn = cached(("espn", q.lower()), lambda: [browser_scores(q) if len(q) >= 3 else None])[0]
            except Exception as e:
                self.log_message("browser team failed: %s", type(e).__name__)
                espn = None
            try:
                with stage("no_game"):
                    no_game = sports_no_game(q, games, espn) if len(q) >= 3 else None
            except Exception as e:
                self.log_message("no-game failed: %s", type(e).__name__)
                no_game = None
            latest = got_news["stories"]
            if wx:
                if weather_query(q):                        # the home forecast: local weather stories
                    with stage("wx_news"):
                        home_town = (wx.get("place") or "").split(",")[0]
                        latest = cached(("wxnews", home_town), lambda: [weather_news(town=home_town)])[0]
                else:                                       # another town's: only stories naming it
                    town = (wx.get("place") or "").split(",")[0].strip().lower()
                    latest = [st for st in latest if town and town in st["title"].lower()]
                got_news = dict(got_news, hot=False, more=False)
            try:
                with stage("answer"):
                    # a sum, a unit conversion or the time somewhere: computed here, never cached (the
                    # time moves), and it takes the answer box before a Wikidata fact
                    ans = instant_answer(q) or cached(("answer", q.lower()), lambda: [answer(q)])[0]
                    ans = ans if ans and ans.get("answer") else (cached(("facts", q.lower()), lambda: [facts_card(q)])[0] or ans)  #
                    if FACTS_OVER_BOX and ans and ans.get("question") in FACTS_OVER:                                          #
                        ans = prefer_facts(ans, cached(("facts", q.lower()), lambda: [facts_card(q)])[0])
            except Exception as e:                          # an answer is extra: never fail the rest
                self.log_message("answer failed: %s", type(e).__name__)
                ans = None
        except Exception as e:
            self.log_message("instant failed: %s: %s", type(e).__name__, e)   # never the query text
            return self._send(503, json.dumps({"error": type(e).__name__}).encode())
        def emit(res, conf, card, place, intent, kinds, local, sig):     # the answer goes out: built here, or stored (fin)
            ms = int((time.monotonic() - t0) * 1000)
            note = _store_note.get()
            self.log_message("instant suggest=%d phrases=%d results=%d ms=%d cache=%s%s%s stages=%s cpu=%s", len(sug), len(phrases),
                             len(res), ms, _cache_state.get(), " warm" if _warm.get() else "",
                             f" audit={note}" if note in ("same", "diff") else "", stages_text(st), stages_text(_stages_cpu.get()))
            if _warm.get():
                sig = dict(sig, store={"cache": _cache_state.get(), "note": note})
            # ms / stages / intent: the page's timing line and "why" tooltips (2026-09-26) —
            # names and numbers only, never the query
            return self._send(200, json.dumps({"suggest": sug, "phrases": phrases, "results": shop_list(q, res) if shop_wanted_over(q, card) else res,
                                              "shop": shop_box(q) if shop_wanted_over(q, card) else None, "confident": conf, "card": card,
                                              "news": latest, "news_hot": got_news["hot"], "news_more": got_news.get("more", False), "mine": got_news.get("mine") or [], "answer": ans,
                                              "games": games, "standings": table, "no_game": no_game, "espn": espn, "weather": wx, "place": place, "kinds": kinds,
                                              "ms": ms, "stages": st, "intent": intent, "local": local, **sig}).encode())

        if fin is not None:
            _cache_state.set("fin")
            return emit(*(fin[k] for k in FIN_FIELDS))
        conf = bool(res) and is_confident(res) and intent_of(q, res) == "nav"
        card = None
        with stage("card"):
            try:
                if card_article(res) is None:
                    mark_respelled(q, res)                          # "sarah j mass": Sarah J. Maas's card
                top = card_article(res)
                if top is not None:
                    t = top["title"].rsplit(" — Wikipedia", 1)[0]
                    lk = lookups()
                    if lk is not None and lk.name == "release":     # the card is one precomputed record
                        descr, rel_rows, img_row, _person = lk.card_parts(t, RELATED_N)
                        row, rel, img = (descr,) if descr else None, related_results(rel_rows), card_image(img_row)
                        site_row = (lk.h[CARD_SITE_SQL]({"t": t}) or [None])[0]
                    else:
                        with _conn() as c:
                            has = tables(c)
                            row = c.execute(CARD_DESCR_SQL, {"t": t}).fetchone() if "suggest_items" in has else None
                            rel = (related_results(c.execute(RELATED_SQL, {"t": t, "n": RELATED_N}))
                                   if "wiki_next" in has else None)
                            img = (card_image(c.execute(CARD_IMAGE_SQL, {"t": t}).fetchone())
                                   if "card_images" in has else None)
                            site_row = c.execute(CARD_SITE_SQL, {"t": t}).fetchone() if "wiki_sites" in has else None
                    card = card_of(res, row[0] if row else None, rel, img, site_row[0] if site_row else None)
            except Exception as e:                                  # a card is extra: never fail the answer
                self.log_message("card failed: %s", type(e).__name__)
        place = None
        with stage("place"):
            try:                                                    # a place searched by name
                pl = cached(("place", q.lower(), home_point()), lambda: [place_exact(q)], static=True)[0] if want and len(q) >= 3 else None
                person = False
                if pl and card is not None:
                    with _conn() as c:
                        person = "wiki_facts" in tables(c) and c.execute(PERSON_SQL, {"t": card["title"]}).fetchone() is not None
                org = org_named(q) if (pl or card) else False
                if org and card is not None:
                    # branches go by the organisation's full name too: "cvs" -> CVS Pharmacy
                    full = re.sub(r"\s*\(.*\)$", "", card["title"])
                    alt = cached(("place", full.lower(), home_point()), lambda: [place_exact(full)], static=True)[0] if full.lower() != q.lower() else None
                    if alt and (not pl or (alt.get("miles") or 1e9) < (pl.get("miles") or 1e9)):
                        pl = alt
                card, place = place_answer(q, card, pl, person, org)
            except Exception as e:
                self.log_message("place failed: %s", type(e).__name__)
        intent = intent_of(q, res) if res else ""        # judged on the list as ranked, before the mix
        kinds = res[0].get("kind_card") if res and res[0].get("kind_top") else None
        res = [{k: v for k, v in r.items() if k != "kind_card"} if "kind_card" in r else r for r in res]
        if _accounts_in_card.get():                      #: the card carries the accounts, the list keeps sites and the article
            res = fold_accounts(res, card)
        res = branches_only(res)
        if card:                                         # an account the card leaves out is not a result either ("IKEA on Discogs")
            what = card.get("description") or ""
            key = same_page(card.get("url") or "")
            res = [r for r in res if not (r.get("profile") and r.get("of") == key and not account_fits(r.get("platform", ""), what))]
        cur = (curated_rows(q) or curated_by_card(q, card)) if res and not (qs.get("cur") or [""])[0] == "0" else None
        def usual(rows):                                 # the list as it is without a curated one
            # kept per search for the cache's time (2026-11-11): the links of the card's article are scored by the
            # reranker in breadth_list, about 0.6 s on the public engine, and that was paid again on EVERY request of
            # a carded name, cached base rows or not. The key is everything the steps below read.
            key = ("usual", q, _breadth.get(), _breadth_floor.get(), bool(_score_all.get()), usual_gate(), same_page((card or {}).get("url") or ""),
                   tuple(r.get("url") for r in rows))
            return cached(key, lambda: usual_now(rows))

        def usual_now(rows):
            rows = mix_wikipedia(rows, card)
            if card:
                rows = card_gate(q, rows)                # off unless SEARCH_CARD_GATE or ?cg= (2026-11-05)
            if _score_all.get() and rows:                # off unless SEARCH_SCORE_ALL or ?sa=1
                with stage("score_all"):
                    rows = score_unscored(q, rows[:SCORE_ALL_TOP] if SCORE_ALL_TOP > 0 else rows)
            return breadth_list(q, rows, card, _breadth.get())   # off unless SEARCH_BREADTH or ?bl= (2026-11-07)
        if cur:
            # the offline list as chosen, in its order (no Wikipedia mix, no gate: the large model already decided);
            # the engine's places stay on top and the card's own article is not repeated under it
            places = [r for r in res if r.get("source") == "place"]
            shown = {same_page(card.get("url") or "")} if card else set()
            top = places + [r for r in cur if r.get("source") != "place" and same_page(r["url"]) not in shown]
            res = curated_fill(top, usual(res), shown) if _cur_fill.get() else top
        else:
            res = usual(res)
        res = with_office_rows(res, card)      # "trump": whitehouse.gov just under the card (after the list is final)
        sig = {}
        if web_only(q, res):           # on the shown list, after the Wikipedia mix: that is what the fit saw
            res = web_rerank(q, res)
            sig = web_signal(q, res)
        if not cur:                    # after the web rerank (it must not move the row); a curated list is the large model's, left as chosen
            res = site_intent_rows(q, res, card)       # off unless SEARCH_SITE_SLOTS / SEARCH_SITE_BRIDGE
            res = se_howto(q, res)     # off unless SEARCH_SE_INDEX; how-to searches only (2026-11-10)
        # local: this search names a business with branches and came with no position (names_places, started above)
        with stage("local_wait"):
            try:
                local = bool(f_local.result()) if f_local is not None and res else False
            except Exception as e:                         # a lookup that failed says nothing, as before it ran
                self.log_message("names_places failed: %s", type(e).__name__)
                local = False
        if _warm.get() and plain and (qs.get("fin") or [""])[0] == "1":     # the builder (twin/ship.sh) keeps what it computed
            fin_put(q, fin_full, {"results": res, "confident": conf, "card": card, "place": place, "intent": intent, "kinds": kinds,
                              "local": local, "sig": sig})
        return emit(res, conf, card, place, intent, kinds, local, sig)


def warm_kinds() -> None:
    """Load the kind-name vectors at start, not on the first kind search (that search took
    2.7 s on testbox, 2026-09-28). Best effort: no table or no database yet = loaded later."""
    try:
        with _conn() as c:
            if "kind_vecs" in tables(c):
                keys, _m = kind_vectors(c)
                print(f"kind vectors: {len(keys)} kind names loaded", flush=True)
    except Exception as e:
        print(f"kind vectors: not warmed ({type(e).__name__})", flush=True)


def warm_models() -> None:
    """Load the query embedder, the web-intent classifier and the cross-encoder at start: the
    first search after a restart took 7.3 s on homeserver while they loaded (2026-09-30). Each
    is best effort and already lazy — this only moves the cost off the first search."""
    t0 = time.time()
    for name, fn in (("embedder", lambda: embed("warm up")), ("intent", _intent_model),
                     ("cross-encoder", cross_encoder), ("cross-encoder (government tasks)", ce_gov)):
        try:
            fn()
        except Exception as e:
            print(f"warm {name}: {type(e).__name__}", flush=True)
    print(f"models warmed in {time.time() - t0:.1f} s", flush=True)


def warm_lookups() -> None:
    """Build at start what the release lookups build on first use: the page-id map (3.6 s on indexhost, paid by the
    first meaning search after a restart), the YouTube channel index (3.3 s, the first channel search) and the
    flagged-sites list (2026-10-11). Best effort, each already lazy."""
    if LOOKUP_BACKEND != "release":
        return
    t0 = time.time()
    lk = lookups()
    for name, fn in (("page ids", lk.pid_map if SEM_BACKEND == "ivf" else None), ("channel index", lk.yt_index),
                     ("flagged sites", junk_domains)):
        try:
            if fn:
                fn()
        except Exception as e:
            print(f"warm {name}: {type(e).__name__}", flush=True)
    print(f"lookups warmed in {time.time() - t0:.1f} s", flush=True)


if __name__ == "__main__":
    # one after the other: loaded together, the cross-encoder's iGPU compile and the kind
    # vectors went over testbox's 1 GB limit at start (OOM-killed once, 2026-09-30)
    require_filtered_web()  # release lookups + Postgres web lists: refused
    require_reranker()  # SEARCH_REQUIRE_RERANKER=1: loads the CPU model once, here, before the warm thread
    require_kinds_model()  # SEARCH_REQUIRE_KINDS_MODEL=1: the same for the kinds query model
    threading.Thread(target=lambda: (warm_kinds(), warm_models(), warm_lookups()), daemon=True).start()
    if NEWS_FILL_EVERY_S > 0:
        threading.Thread(target=news_vec_fill_loop, daemon=True, name="news-vec-fill").start()
    if STORE_PATH and STORE_INBOX_EVERY_S > 0:
        threading.Thread(target=store_inbox_loop, daemon=True, name="store-inbox").start()
    sem_ivf()          # a configured IVF folder / nomic model that does not load stops the start, loudly
    wiki_text()        # a configured Tantivy index that does not open stops the start, loudly
    web_text()
    lookups()          # a configured SQLite lookup file that does not open stops the start, loudly
    ThreadingHTTPServer((BIND, PORT), Handler).serve_forever()
