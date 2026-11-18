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

_PROC_T0 = time.monotonic()      # for the start-up log: seconds since this file began (the interpreter's own start is before it)

import start_trace      # the startup trace (SEARCH_START_TRACE=1): a no-op unless it is on
import unicodedata
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlsplit

import psycopg

import gov_state

try:                                  # builder-side provenance (phase 4); a missing file is never fatal to serving
    from provenance import cand_summary
except ImportError:
    cand_summary = None

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

_TOKEN = re.compile(r"[a-z0-9]+")


def norm_key(s: str) -> str:
    """Letters and digits only, lowercase — the same key the build side stores."""
    return "".join(_TOKEN.findall(s.lower()))


# A typed domain ("costco.co.uk", "drive.google.com", "https://www.bbc.com/") is that site.
_DOMAIN_Q = re.compile(r"^(?:https?://)?(?:www\.)?((?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,})/?$")
def domain_query(q: str) -> str | None:
    m = _DOMAIN_Q.match(q.strip().lower())
    return m.group(1) if m else None


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

# Hosts that are never an "official site" answer, whatever Wikipedia lists: a social
# profile or link hub. "youth reform movement" -> https://www.fb.com/YRMPH was marked
# sure (2026-09-24 gate check); fb.com / youtu.be slipped past facebook.com / youtube.com.
_SOCIAL_HOSTS = frozenset("""facebook.com fb.com fb.me m.facebook.com instagram.com instagr.am twitter.com x.com t.co
youtube.com youtu.be linkedin.com lnkd.in tiktok.com linktr.ee threads.net""".split())


# Press-release wires are nobody's official site (Home Depot's article linked a
# prnewswire.com release, which then outranked homedepot.com, 2026-09-25).
_WIRE_HOSTS = frozenset("prnewswire.com businesswire.com globenewswire.com prweb.com newswire.com "
                        "accesswire.com einpresswire.com".split())


# Which of an article and its official site leads when the query is the article's own name. Owner, 2026-10-07 (final):
# "site first then article on everything ... always showing wikipedia as first result really doesn't make us much of a
# search engine" - so the SITE, whatever the thing is and whatever the site's rank ("snoop dogg" -> snoopdogg.com,
# "italy" -> italia.it), with the article directly under it; on the page the article is the card anyway.
# "people-orgs" keeps the article first for a place (a country's site is a tourism board): the option not taken.
OFFICIAL_FIRST = os.environ.get("SEARCH_OFFICIAL_FIRST", "all").strip().lower()
if OFFICIAL_FIRST not in ("people-orgs", "all"):
    raise SystemExit(f"SEARCH_OFFICIAL_FIRST={OFFICIAL_FIRST!r}: use 'people-orgs' or 'all'")
def official_ok(url: str) -> bool:
    return _domain_of(url) not in _SOCIAL_HOSTS and _domain_of(url) not in _WIRE_HOSTS


_LOCALE_SEG = re.compile(r"[a-z]{2}(?:[-_][a-z]{2,4})?$", re.I)


def official_inner_page(url: str) -> bool:
    """The stored official link is a page deep inside a site, not anyone's home: defense.gouv.fr/terre/presentation/
    organisation-des-forces/... for a regiment, msccruises.de/unsere-kreuzfahrten/kreuzfahrtschiffe/msc-virtuosa for a
    ship (rc5 acceptance, 2026-10-08: "13 dble" and "imo 9803625" led with those over the article the alias names).
    Three or more path segments, language segments not counted. A home one or two levels down still leads:
    mlb.com/yankees, workspace.google.com/products/gmail, ikea.com/us/en."""
    segs = [x for x in urlsplit(url or "").path.split("/") if x and not _LOCALE_SEG.match(x)]
    return len(segs) >= 3


# A news article, an archive page, a PDF or a forum/board post is never anybody's official site: Wikidata and the registry
# sometimes store one as "official website" (cbsnews.com/news/grandcentral-becomes-google-voice-... for Google Voice,
# newyorker.com/magazine/2002/10/14/... for Christopher Columbus). The test is the shape of the address, not a list of
# outlets: a dated path, a news/article/press/archive/magazine segment, a document file. A product, show, series, person or
# team page on the owner's own domain (tv.apple.com/us/show/ted-lasso, records.nhl.com/awards/trophies/stanley-cup,
# meta.com/quest/products/quest-2) has none of them and stays.
_NEWS_SEGS = frozenset("""news article articles story stories press pressroom press-release press-releases newsroom archive
archives magazine magazines bitstream mediacentre media-centre media-center entertainment bbs forum forums thread threads
search recipe recipes guide guides review reviews opinion features lifestyle""".split())
_DATED_PATH = re.compile(r"/(?:19|20)\d\d/(?:0?[1-9]|1[0-2])(?:/|$)")      # /2019/04/ , not an event page like /racing/2026
_DOC_EXT = re.compile(r"\.(?:pdf|docx?|pptx?|xlsx?)$", re.I)


def official_news_like(url: str) -> bool:
    u = urlsplit(url or "")
    path = u.path or ""
    if _DATED_PATH.search(path) or _DOC_EXT.search(path):
        return True
    segs = [x.lower() for x in path.split("/") if x]
    if any(x in _NEWS_SEGS or x.endswith("-news") or x.startswith(("news-", "how-to", "howto")) for x in segs):
        return True
    return u.netloc.lower().startswith(("news.", "press.", "blog.", "archive."))


def official_link_ok(url: str) -> bool:
    """A stored official link the card may show: not a social/wire host, not a news/archive/document page."""
    return official_ok(url) and not official_news_like(url)


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
# The query IS what many sites call one site when they link to it ("wayback machine": 150
# sites say it of archive.org, 14 of the next) — as good as its name (2026-09-26). The
# weighting above keeps a one-word match on an obscure site (papermag.com for "the paper
# of record") under the known sites, as SITES_SQL does.
EXACT_ANCHOR_SQL = """
SELECT a.domain, a.sources, d.url, d.title, d.description FROM anchors a
JOIN docs d ON d.domain = a.domain AND d.error IS NULL
WHERE a.anchor = %(q)s ORDER BY a.sources DESC LIMIT 2"""
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
_wiki_text_lock = start_trace.lock("wiki_text")


# Wikipedia meaning search (T3b, ADR-063): Postgres (wiki_vec, bge-small, HNSW) or the release's
# IVF folder over nomic 1-bit codes (sem_ivf.py) — no database, low memory.
#   SEARCH_SEM_BACKEND=postgres | ivf    SEARCH_SEM_IVF=<vectors/ivf2600>    SEARCH_NOMIC_DIR=<model>
#   SEARCH_SEM_PROBES=200 (lists compared per search; 0.935 of the exact top 10 on testbox)
SEM_BACKEND = os.environ.get("SEARCH_SEM_BACKEND", "postgres").strip().lower()
SEM_IVF = _release_root(os.environ.get("SEARCH_SEM_IVF", "/data/vectors/ivf2600"))
NOMIC_DIR = os.environ.get("SEARCH_NOMIC_DIR", "/models/nomic-embed-text-v1.5")
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


_compute_cache_state: list = []


def _compute_cache():
    """The model-output cache (compute_cache.py), or None when SEARCH_COMPUTE_CACHE is unset (the module is then never
    imported)."""
    if not _compute_cache_state:
        if os.environ.get("SEARCH_COMPUTE_CACHE", "").strip():
            import compute_cache as _cc
            _compute_cache_state.append(_cc.cache())
        else:
            _compute_cache_state.append(None)
    return _compute_cache_state[0]


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
        opts.intra_op_num_threads = min(2, _CPUS)
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


_embedder_lock = start_trace.lock("embedder")
_embedder_retry = [0.0]                                    # monotonic time before which a failed load is not retried
EMBED_RETRY_S = float(os.environ.get("SEARCH_EMBED_RETRY_S", "30"))


def embedder_ready() -> bool:
    """Is the query embedder loaded? No inference (CORE-A-03: embed("warm") ran the model to ask this)."""
    return bool(_load_embedder())


def _load_embedder():
    """The embedder, built once under a lock (CORE-A-07: four concurrent first calls built four 130 MB sessions). A model
    that is not in this image turns the tier off for good; any other failure (an I/O error while a release swaps, memory) is
    printed and retried after EMBED_RETRY_S instead of silently turning the semantic tiers off until restart."""
    global _embedder
    if _embedder is None or (_embedder is False and _embedder_retry[0] and time.monotonic() >= _embedder_retry[0]):
        with _embedder_lock:
            if _embedder is None or (_embedder is False and _embedder_retry[0] and time.monotonic() >= _embedder_retry[0]):
                try:
                    if not os.path.exists(os.path.join(MODEL_DIR, "model.onnx")):
                        raise FileNotFoundError(MODEL_DIR)
                    _embedder = _Embedder(MODEL_DIR)
                except (FileNotFoundError, ImportError):
                    _embedder = False                       # no model in this image: tier off
                    _embedder_retry[0] = 0.0
                except Exception as e:  # noqa: BLE001
                    print(f"embedder: not loaded ({type(e).__name__}), retry in {EMBED_RETRY_S:.0f} s", flush=True)
                    _embedder = False
                    _embedder_retry[0] = time.monotonic() + EMBED_RETRY_S
    return _embedder


def embed(text: str, prefix: str = QUERY_PREFIX) -> list[float] | None:
    if _load_embedder() is False or not _embedder:
        return None
    cc = _compute_cache()
    if cc is None:
        return _embedder(text, prefix)
    import array
    import compute_cache as _cc
    ns = f"bge|{_cc.file_sha256(os.path.join(MODEL_DIR, 'model.onnx'))}|128|{_embedder.sess.get_providers()[0]}"
    item = prefix + "\x00" + text
    v = cc.get_many(ns, [item])[0]
    if v is not None:
        return array.array("f", v).tolist()
    out = _embedder(text, prefix)
    cc.put_many(ns, [item], [array.array("f", out).tobytes()])
    return out


# News headlines by meaning (2026-09-30): one vector per item, cached by URL so a headline is
# embedded once however many searches match it (float16, bounded). 2026-11-04: 30,000 (~23 MB) so the
# background fill (news_vec_fill) can hold the whole NEWS_HOURS window (~20,000 headlines on public): a first
# news lookup for a popular name took 140-700 ms embedding its headlines, a repeat 41 ms.
_NEWS_VEC_MAX = int(os.environ.get("SEARCH_NEWS_VEC_MAX", "30000"))
_news_vecs: OrderedDict = OrderedDict()
_news_vec_lock = start_trace.lock("news_vec")


def news_vectors(items: list[tuple[str, str]]) -> dict | None:
    """[(url, "headline. summary")] -> {url: unit vector}; None when the model is off."""
    if not embedder_ready():
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


def major_team(key: str, rows) -> str | None:
    """MAJOR_TEAM_SQL rows -> the team article the typed nickname names: the query must be
    the END of the team's name ("devils" / "new jersey devils", not "new jersey"), most
    searched first."""
    for wiki, name, _d in rows:
        words = re.sub(r"\s*\(.*\)$", "", name).split()
        if any(norm_key(" ".join(words[i:])) == key for i in range(len(words))):
            return wiki
    return None


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
_WIKI_SUFFIX = " — Wikipedia"


_pt: contextvars.ContextVar = contextvars.ContextVar("pt", default=None)     # eval A/B only (pt=0 / 1)
# Two things the ordering lacked (2026-10-10, rc6):
# - a site's votes were SUMMED over the kind names a query matched, and one Wikidata class comes under several names
#   ("breaking news", "latest news", "news update", "news report", "current event", "commentary"): a site of that one
#   class got 5 or 6 votes, bbc.com 1. The most votes under any one name is the count of independent sources.
# - no site rank: the Tranco term left with Tranco and nothing took its place (the Common Crawl position is missing
#   for most sites and "only nudges"). Our own rank now has that term's weight. Over the 11,095 kinds with a list the
#   median own rank of the three listed sites goes from 94,151 to 68,843.
KIND_RANK_W_OPEN = float(os.environ.get("SEARCH_KIND_RANK_W_OPEN", "0.15"))
_kvec = (None, None, None)          # (generation, keys, unit-row matrix)
_kvec_lock = start_trace.lock("kind_vec")


KIND_VECS_SQL = "SELECT key, v FROM kind_vecs"
KVEC_RETRY_S = float(os.environ.get("SEARCH_KVEC_RETRY_S", "60"))
_kvec_retry = [0.0]                  # monotonic time before which a failed read is not tried again


def kind_vectors(c):
    """(keys, matrix) of the kind-name vectors, read once per data snapshot. A missing table or numpy is "off" for the
    snapshot; any other failure (a timeout, a broken connection) is asked again after KVEC_RETRY_S (CORE-A-06)."""
    global _kvec
    gen = generation()
    if _kvec[0] != gen or (_kvec[1] is None and time.monotonic() >= _kvec_retry[0]):
        with _kvec_lock:
            if _kvec[0] != gen or (_kvec[1] is None and time.monotonic() >= _kvec_retry[0]):
                try:
                    import numpy as np
                    rows = c.execute(KIND_VECS_SQL).fetchall()
                    _kvec = (gen, [r[0] for r in rows], np.array([r[1] for r in rows], dtype=np.float32))
                except Exception as e:
                    print(f"kind vectors: off ({type(e).__name__}: {e})", flush=True)
                    permanent = isinstance(e, ImportError) or type(e).__name__ in ("UndefinedTable", "NoPostgres") or "no such table" in str(e)
                    _kvec = (gen, [], None) if permanent else (gen, None, None)
                    _kvec_retry[0] = time.monotonic() + KVEC_RETRY_S
    return (_kvec[1] if _kvec[1] is not None else []), _kvec[2]


def related_results(rows) -> list[dict]:
    out = []
    for i, (title, lead) in enumerate(rows):
        r = _wiki(title, lead, RELATED_SCORE - i)
        r["related"] = True
        out.append(r)
    return out


def _label(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower().removeprefix("www.")
    return host.split(".")[0]


def _host_key(url: str) -> str:
    h = (urlsplit(url or "").hostname or "").lower()
    return h[4:] if h.startswith("www.") else h


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


_HOST_OK = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")


def optional(name: str, fn, fallback, log=None):
    """An optional block of an answer (audit CORE-B-04, the 2026-11-14 outage class): fn() or, when it raises, the
    fallback with the exception's TYPE logged (never the query: logs ship to VictoriaLogs). One failing extra degrades
    that extra; it never fails the search or drops the reply. PartStale (the builder's own question) goes through."""
    try:
        return fn()
    except PartStale:
        raise
    except Exception as e:  # noqa: BLE001
        (log or (lambda m, *a: print(m % a, file=sys.stderr, flush=True)))("%s failed: %s", name, type(e).__name__)
        return fallback


def _host_of(url: str) -> str:
    """The url's host, lower case; "" for an address urlsplit rejects ("http://[abc/x")."""
    try:
        return (urlsplit(url or "").hostname or "").lower()
    except ValueError:
        return ""


def url_host_ok(url: str) -> bool:
    """A link whose host is a host name: "https://1=microsoft.com/en-us/microsoft-365/word" (Microsoft Word's stored
    official site, 2026-11-14) is not, and was the card's first link."""
    try:
        return bool(_HOST_OK.match((urlsplit(url or "").hostname or "")))
    except ValueError:
        return False


def _plain_name(title: str) -> bool:
    """A title whose letters are all the name: "Google" and "Microsoft Outlook", not "Google+" (norm_key drops the
    plus, and "google" then matched Google+, 2026-11-15)."""
    return bool(title) and norm_key(title) == re.sub(r"\s+", "", title.lower())


def name_bare_rows(res: list[dict], card: dict | None) -> list[dict]:
    """A site row titled with nothing but its address ("https://www.gmail.com/", "outlook.com") is named after the
    card it belongs to: the card's official site, or the .com of the card's name or its last word ("outlook.com" under
    Microsoft Outlook; gmail.dev is someone else's). Copies; the rows are shared with the cache."""
    if not card or not res:
        return res
    title = re.sub(r"\s*\(.*\)$", "", card.get("title") or "")
    words = title.split()
    names = {norm_key(title): title} if _plain_name(title) else {}     # never "Google+" for google.com
    if len(words) >= 2 and _plain_name(words[-1]):
        names.setdefault(norm_key(words[-1]), words[-1])
    off = next((l["url"] for l in card.get("links", []) if l.get("title") == "Official site"), None)
    out = []
    for r in res:
        t, u = (r.get("title") or "").strip(), r.get("url") or ""
        host = _host_of(u)
        bare = r.get("source") == "site" and (t.rstrip("/") in (u.rstrip("/"), host, host.removeprefix("www.")) or t == "")
        if off and t == f"{title} — official site" and _domain_of(off) != _domain_of(u):
            r = dict(r, title=f"{title} — {_domain_of(u)}")     # the stored site the card's own-name site replaced
        elif bare:
            if off and _domain_of(off) == _domain_of(u):
                r = dict(r, title=f"{title} — official site")
            elif _label(u) in names and _domain_of(u) == _label(u) + ".com":     # gmail.dev is someone else's
                r = dict(r, title=names[_label(u)])
        out.append(r)
    return out


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
_breadth: contextvars.ContextVar = contextvars.ContextVar("breadth", default=BREADTH)
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
_score_all: contextvars.ContextVar = contextvars.ContextVar("score_all", default=SCORE_ALL)


_se_on: contextvars.ContextVar = contextvars.ContextVar("se_index_on", default=True)
def _account(platform, url, name, desc, value, sitelinks, score) -> dict:
    label = ACCOUNT_LABEL.get(platform, platform)
    handle = f" (@{value})" if platform in ("x", "instagram", "tiktok", "twitch", "github", "bluesky") else ""
    r = {"url": url, "title": f"{name} on {label}{handle}", "content": (desc or "")[:300],
         "source": "account", "score": round(score, 2), "platform": platform}
    if (sitelinks or 0) >= ACCOUNT_POPULAR:
        r["popular"] = True
    return r


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


# Words that follow a business's name without being part of it: "t mobile store", "verizon locations".
PLACE_TAIL = frozenset({"store", "stores", "shop", "shops", "location", "locations", "hours", "branch", "branches"})


def place_name_core(name: str) -> str:
    """"t mobile store" -> "t mobile"; a name that is only such words, or has none at its end, is unchanged."""
    words = name.split()
    while len(words) > 1 and words[-1] in PLACE_TAIL:
        words = words[:-1]
    return " ".join(words)


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


# ---------------------------------------------------------------------------
# Latency (2026-09-24). Profiled cold (40 never-seen queries): p50 247 ms, of which
# typo lookup 67, site keyword 53, semantic wiki 48, wiki keyword 39, two new connections
# 15, places 14 — one after another. Now: pooled connections, the table list cached, the
# independent lists run side by side, and a short result cache for repeated queries
# (the start page asks again on every keystroke).
# ---------------------------------------------------------------------------
POOL_MAX = int(os.environ.get("SEARCH_POOL_MAX", "12"))
_pool: list = []
_pool_lock = start_trace.lock("conn_pool")
START_TRACE = start_trace.ON
_CPUS = (len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else os.cpu_count()) or 2      # the CPUs this process may use (taskset, cpuset), not the machine's
import warmplan as _warmplan
_WORKERS = int(os.environ.get("SEARCH_WORKERS") or (4 if _warmplan.small_machine() else min(12, max(4, 2 * _CPUS))))     # a small machine: 4 (minspec F: -104 MB, latency unchanged)
_EXEC = start_trace.pool(_WORKERS, "q")      # follows the CPU: 12 workers on 2 cores only queue behind each other      # a plain ThreadPoolExecutor unless SEARCH_START_TRACE=1


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
_lookups_lock = start_trace.lock("lookups")


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
    if _warm.get():
        _warm_yield()                               # the warmer pauses between stages while a visitor's search is in flight (RT-01)
    st, t, sc, c = _stages.get(), time.monotonic(), _stages_cpu.get(), time.thread_time()
    tok = start_trace.stage_begin(name) if START_TRACE else None
    try:
        yield
    finally:
        if START_TRACE:
            start_trace.stage(name, (time.monotonic() - t) * 1000, (time.thread_time() - c) * 1000, tok)
        if st is not None:
            st[name] = st.get(name, 0.0) + (time.monotonic() - t) * 1000
        if sc is not None:
            sc[name] = sc.get(name, 0.0) + (time.thread_time() - c) * 1000


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


def news_vec_fill_once(batch: int = 8) -> int:
    """Embed the window's headlines not cached yet; how many were embedded."""
    rows = _news_rows(NEWS_RECENT_SQL, {"h": NEWS_HOURS, "n": _NEWS_VEC_MAX})
    if not rows or not embedder_ready():
        return 0
    with _news_vec_lock:
        todo = [(u, news_text(t, sm)) for u, t, sm in rows if u not in _news_vecs]
    done = 0
    for i in range(0, len(todo), batch):
        bg_wait_idle()                                     # a visitor first, and a moment after (8 texts: well under a second a batch)
        t = time.monotonic()
        news_vectors(todo[i:i + batch])
        done += len(todo[i:i + batch])
        time.sleep(min(2.0, 2 * (time.monotonic() - t)))   # a duty cycle of a third: the cores are shared with the searches
    return done


def news_vec_fill_loop() -> None:
    for _ in range(int(WARMUP_MAX_S) + 30):                # after the engine's own start-up work and the warm-up
        if _warmup["state"] == "done":
            break
        time.sleep(1)
    bg_wait_idle()
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
       away_keys && %(k)s::text[] AS away_hit, updated
FROM games WHERE (away_keys && %(k)s::text[] OR home_keys && %(k)s::text[])
  AND start_utc BETWEEN now() - interval '8 days' AND now() + interval '15 days'
ORDER BY start_utc LIMIT 60
"""
_SPORTS_TAIL = re.compile(r"\s+(?:score|scores|game|games|schedule|tonight|today|live|next game|tickets|tv|on tv|"
                          r"game time|game times|game tonight|game today|game tomorrow|last game|results|fixtures|"
                          r"next match|match|matches|what time|time)$")
RECENT_FINAL = dt.timedelta(hours=14)
# a team whose next game is more than QUIET away keeps its last result on the card, up to LAST_FINAL old (2026-11-14: "giants"
# two days after a Thursday game, the next one 8 days off, showed no game at all)
QUIET, LAST_FINAL = dt.timedelta(days=2), dt.timedelta(days=8)
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
         venue, tv, url, away_hit, *checked) in rows:
        upd = checked[0] if checked else None           # when the collector last stored this game: the box's "as of"
        team = (league, a_name if away_hit else h_name)
        teams.setdefault(team, []).append({
            "league": league, "id": gid, "start": start.isoformat(), "state": state, "detail": detail,
            "away": {"name": a_name, "nick": a_nick, "score": a_score},
            "home": {"name": h_name, "nick": h_nick, "score": h_score},
            "venue": venue, "broadcasts": list(tv or []), "url": url, "_t": start,
            "as_of": upd.isoformat() if isinstance(upd, dt.datetime) else None})
    picked = []
    for team, games in teams.items():
        live = [g for g in games if g["state"] == "live"]
        finals = [g for g in games if g["state"] == "final" and now - g["_t"] <= LAST_FINAL]
        recent = [g for g in finals if now - g["_t"] <= RECENT_FINAL]
        ahead = [g for g in games if g["state"] in ("pre", "postponed") and g["_t"] >= now - dt.timedelta(hours=3)]
        if not recent and finals and (not ahead or ahead[0]["_t"] - now > QUIET):
            recent = finals[-1:]
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
# State codes that are also words: "sign in", "log in", "call me" are not towns in Indiana
# and Maine; "portland me", "scranton pa", "tulsa ok" are — a real town is big enough.
TOWN_WORD_CODES = {"in", "or", "me", "ok", "hi", "la", "co", "oh", "id", "ma", "pa", "al", "ar", "mo", "ne",
                   "ut", "wa", "mi", "mt", "ms", "de", "ga"}
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
       home_name, home_nick, home_score, venue, broadcasts, url, {overlap.format(col="away_keys")} AS away_hit, updated
FROM games WHERE ({overlap.format(col="away_keys")} OR {overlap.format(col="home_keys")})
  AND start_utc BETWEEN now_s() - 8 * 86400 AND now_s() + 15 * 86400
ORDER BY start_utc LIMIT 60""", row=ns.row_with(**{"2": ns.epoch_dt, "12": ns.from_json, "14": bool, "15": ns.epoch_dt}))
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
RADAR_ID_RE = re.compile(r"^[A-Z]{4}$")
_radar_list: dict = {"read": 0.0, "mtime": None, "stations": []}
_radar_lock = threading.Lock()
_radar_recorded: dict[str, float] = {}


def record_radar_interest(sid: str) -> None:
    """Note that this station's radar was asked for (wxall.py fetches only watched/home stations)."""
    if not RADAR_DIR or not RADAR_ID_RE.fullmatch(sid or ""):
        return
    now = time.time()
    with _radar_lock:
        if now - _radar_recorded.get(sid, 0.0) < 60:
            return
        _radar_recorded[sid] = now
        path = os.path.join(RADAR_DIR, "interest.json")
        try:
            os.makedirs(RADAR_DIR, exist_ok=True)
            try:
                with open(path, encoding="utf-8") as f:
                    raw = json.load(f)
                stations = raw.get("stations") if isinstance(raw, dict) and isinstance(raw.get("stations"), dict) else (raw if isinstance(raw, dict) else {})
            except (OSError, ValueError):
                stations = {}
            clean = {k: float(v) for k, v in stations.items()
                     if isinstance(k, str) and RADAR_ID_RE.fullmatch(k) and isinstance(v, (int, float)) and not isinstance(v, bool)
                     and now - float(v) <= 24 * 3600}
            clean[sid] = round(now, 1)
            tmp = f"{path}.tmp{os.getpid()}"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"stations": clean}, f, separators=(",", ":"))
            os.replace(tmp, path)
        except OSError:
            pass


def radar_read(sid: str, ts: int | None = None) -> tuple[int, bytes, str]:
    """Read a locally cached NWS radar manifest or frame GIF; 404 when absent or disabled."""
    if not RADAR_DIR or not RADAR_ID_RE.fullmatch(sid or ""):
        return 404, b'{"error":"not found"}', "application/json"
    if ts is None:
        record_radar_interest(sid)
        path = os.path.join(RADAR_DIR, f"{sid}.json")
        ctype = "application/json"
    else:
        if not isinstance(ts, int) or isinstance(ts, bool) or ts <= 0:
            return 404, b'{"error":"not found"}', "application/json"
        path = os.path.join(RADAR_DIR, sid, f"{ts}.gif")
        ctype = "image/gif"
    try:
        with open(path, "rb") as f:
            return 200, f.read(3 * 1024 * 1024), ctype
    except OSError:
        return 404, b'{"error":"not found"}', "application/json"


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
    if best:
        record_radar_interest(best["id"])
        return {"id": best["id"], "name": str(best.get("name") or "")[:40]}
    return None


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
                if "radar" not in out and HOME and (r := radar_station(HOME[0], HOME[1])):
                    out["radar"] = r
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
_cache_lock = start_trace.lock("result_cache")
# The head-query warmer (warm.py WARM_HEAD, 2026-11-04) asks /instant?warm=1: a static entry past half its life is
# computed again, so a popular name never expires between the warmer's visits (a hit does not renew an entry).
# Only the engine's own network reaches it: the public gateway forwards q/results/full/near/reach and nothing else.
_warm: contextvars.ContextVar = contextvars.ContextVar("warm", default=False)
# The builder (warm=1&fin=1) collects the candidate lists the retrieval returned, for the stored answer's provenance (phase 4).
_prov: contextvars.ContextVar = contextvars.ContextVar("prov", default=None)
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
_store_tried = [False]                      # a first attempt was made (by a request, or by store_open_loop at start)
_store_state = {"error": None, "tries": 0, "gave_up": False}    # why it is not open, how often tried, whether the wait ran out
STORE_OPEN_WAIT_S = float(os.environ.get("SEARCH_STORE_OPEN_WAIT_S", "60"))   # /readyz holds for the store this long, then says so and goes on
_store_lock = start_trace.lock("store_open")
_audit: contextvars.ContextVar = contextvars.ContextVar("audit", default=False)
_store_note: contextvars.ContextVar = contextvars.ContextVar("store_note", default=None)   # "disk" / "prev" / "same" / "diff"
_prev_served: contextvars.ContextVar = contextvars.ContextVar("prev_served", default=None)   # a [bool] cached() reads
_busy = [0]                                 # visitors' search requests in flight (/instant, /more, /explain, /search; the warmer waits for 0)
_last_visitor = [0.0]                       # monotonic time a visitor request last started or ended (the warmer backs off for a while after)
# After a start, searches come first (2026-11-16, buildbox: a restart on a busy disk left searches at 6-88 s for eight minutes while the news
# headlines were embedded in the same process). One gate for everything that is not a search: it waits while a visitor request is in flight
# and for BG_QUIET_S after the last one. And a cold engine (the warm-up below not finished, at most WARMUP_MAX_S) gives each optional stage
# COLD_BUDGET_S: what is not ready is left out of the answer, the stage keeps running and fills the caches.
BG_QUIET_S = float(os.environ.get("SEARCH_BG_QUIET_S", "1.0"))
COLD_BUDGET_S = float(os.environ.get("SEARCH_COLD_BUDGET_S", "3.0"))
WARMUP_MAX_S = float(os.environ.get("SEARCH_WARMUP_MAX_S", "150"))
WARMUP_MAX_BYTES = int(float(os.environ.get("SEARCH_WARMUP_MAX_BYTES", "1.5e9")))
_cold = [False]                             # True from the server's start until the warm-up is done (or its time is up); read without a clock. Only a running engine is ever cold: a library import is not
_warmup = {"state": "waiting", "done": 0, "total": 0, "skipped": 0}   # waiting | running | done


def bg_wait_idle(max_s: float | None = None) -> None:
    """Background work calls this between small pieces: it returns when no visitor request is in flight and none ended in BG_QUIET_S, or
    after max_s when given: work that must progress under a steady load (a take-in's pause) passes one."""
    t0 = time.monotonic()
    while _busy[0] > 0 or time.monotonic() - _last_visitor[0] < BG_QUIET_S:
        if max_s is not None and time.monotonic() - t0 >= max_s:
            return
        time.sleep(0.02)


def fut_result(f, default):
    """f.result(), but on a cold engine only for COLD_BUDGET_S: then `default` (the stage goes on in its thread and warms what it reads)."""
    if not _cold[0]:
        return f.result()
    try:
        return f.result(timeout=COLD_BUDGET_S)
    except TimeoutError:
        _warmup["skipped"] += 1
        return default


TYPING_WAIT_S = float(os.environ.get("SEARCH_TYPING_WAIT_S", "0.08"))     # as-you-type (results=0): the news, sports and team boxes get this long


def typing_wait(f, fallback):
    """f.result() for at most TYPING_WAIT_S: a typed prefix's answer is its suggestions; the news and team boxes ride along only when they are
    ready (90-270 ms on a prefix nobody had typed). The call keeps running and fills its cache for the next keystroke and the full search."""
    from concurrent.futures import TimeoutError as _FutTimeout
    try:
        return f.result(timeout=TYPING_WAIT_S)
    except (TimeoutError, _FutTimeout):
        return fallback


def cold_typing(fn, fallback):
    """fn() in its own worker for a typed prefix, waited for TYPING_WAIT_S (typing_wait)."""
    return typing_wait(_EXEC.submit(contextvars.copy_context().run, fn), fallback)


def cold_call(name, fn, fallback):
    """fn() for a request; on a cold engine in its own thread for at most COLD_BUDGET_S, else `fallback`."""
    if not _cold[0]:
        return fn()
    f = _EXEC.submit(contextvars.copy_context().run, fn)
    return fut_result(f, fallback)
_busy_lock = threading.Lock()
# RT-01: a warm request (the warmer, the builder's own engine) gives way to visitors. Between its stages it waits while any
# visitor search is in flight (WARM_YIELD_STEP_S steps, at most WARM_YIELD_TOTAL_S per request so it still ends), and while a
# visitor was seen in the last WARM_VISITOR_WINDOW_S only WARM_SLOTS_WITH_VISITORS warm requests run at once. A box nobody
# visits (the builders) is unaffected: both rules need a visitor.
WARM_YIELD_STEP_S = float(os.environ.get("SEARCH_WARM_YIELD_STEP_S", "0.05"))
WARM_YIELD_TOTAL_S = float(os.environ.get("SEARCH_WARM_YIELD_TOTAL_S", "15"))
WARM_VISITOR_WINDOW_S = float(os.environ.get("SEARCH_WARM_VISITOR_WINDOW_S", "60"))
WARM_SLOTS_WITH_VISITORS = int(os.environ.get("SEARCH_WARM_SLOTS_WITH_VISITORS", "1"))
_warm_slots = threading.Semaphore(max(1, WARM_SLOTS_WITH_VISITORS))
_warm_waited: contextvars.ContextVar = contextvars.ContextVar("warm_waited", default=None)   # [seconds waited] of this request


def _warm_yield() -> None:
    w = _warm_waited.get()
    if w is None or _busy[0] <= 0:
        return
    t = time.monotonic()
    while _busy[0] > 0 and w[0] < WARM_YIELD_TOTAL_S:
        time.sleep(WARM_YIELD_STEP_S)
        w[0] += WARM_YIELD_STEP_S


def _open_store() -> bool:
    """One attempt to open the ResultStore; the caller holds _store_lock. True when it is open (or none is configured)."""
    try:
        if STORE_PATH:
            import result_store as rs
            models = [d for d in ("/models", NOMIC_DIR, CE_DIR, CE_GOV_DIR) if d and os.path.exists(d)]
            fp = rs.fingerprint(os.path.dirname(os.path.abspath(__file__)), RELEASE_DIR, models)
            st = rs.ResultStore(STORE_PATH, fp, STORE_MAX_AGE_S)
            st.prune()
            _store[0] = st
            print(f"result store {STORE_PATH}: fingerprint {fp}", flush=True)
        _store[1] = True        # set when the store is READY (audit B-10), never before: a request arriving while another thread was
        _store_state["error"] = None    # opening (and pruning) must not see "done, none"
        return True
    except Exception as e:
        _store_state["tries"] += 1
        _store_state["error"] = f"{type(e).__name__}: {e}"
        print(f"result store {STORE_PATH} NOT OPEN (try {_store_state['tries']}): {_store_state['error']}", flush=True)
        return False


def result_store():
    """The ResultStore, opened on first use (one attempt: store_open_loop keeps trying in the background); None when SEARCH_STORE
    is unset or the file cannot be opened (yet)."""
    if _store[1]:
        return _store[0]
    with _store_lock:
        if not _store[1] and not _store_tried[0]:
            _store_tried[0] = True
            _open_store()
    return _store[0]


def store_open_loop() -> None:
    """Started with the server: open the store now and, while it will not open, try again every 2 s growing to 15 s. After
    STORE_OPEN_WAIT_S it says plainly that it is still closed (store_ready() then stops holding /readyz) and keeps trying."""
    delay, waited = 0.0, 0.0
    while not _store[1]:
        if delay:
            time.sleep(delay)
            waited += delay
        with _store_lock:
            _store_tried[0] = True
            ok = _store[1] or _open_store()
        if ok:
            if _store_state["tries"]:
                print(f"result store open after {_store_state['tries']} failed tries, {waited:.0f} s", flush=True)
            return
        if waited >= STORE_OPEN_WAIT_S and not _store_state["gave_up"]:
            _store_state["gave_up"] = True
            print(f"result store STILL NOT OPEN after {waited:.0f} s ({_store_state['error']}): readiness no longer waits for it; "
                  f"searches run without stored answers and curated lists until it opens (retrying every 15 s)", flush=True)
        delay = min(max(delay * 1.5, 2.0), 15.0)


def store_ready() -> bool:
    """False while a configured result store has not opened and the wait (STORE_OPEN_WAIT_S) has not run out: /readyz holds the
    safe deploy's switch for it. True when none is configured, it is open, or the wait is over."""
    return (not STORE_PATH) or _store[1] or _store_state["gave_up"]


# An answer as parts (phase 2, 2026-11-15; parts.py, docs/internal/precompute-plan.md). SEARCH_PARTS=card turns the
# listed stages on (default off): the stage's output is kept in the result store under a key made of the stage's code
# version, the settings and data it reads and its inputs, so a deploy that did not touch the stage finds its parts again.
# Only the builder (warm=1) writes; ?parts=0 computes live (the shadow compare's old path). Any trouble = computed live.
PARTS_ON = frozenset(x.strip() for x in os.environ.get("SEARCH_PARTS", "").split(",") if x.strip())
# Settings that change speed, capacity or logging, never an answer: named so a part's key is the same on every machine
PARTS_SPEED_ONLY = ("SEARCH_START_TRACE", "SEARCH_BG_QUIET_S", "SEARCH_COLD_BUDGET_S", "SEARCH_CACHE_MAX", "SEARCH_CACHE_STATIC_TTL_S", "SEARCH_CACHE_TTL_S", "SEARCH_CE_THREADS", "SEARCH_COMPUTE_CACHE",
                    "SEARCH_FINISHED", "SEARCH_FINISHED_MAX_AGE_S", "SEARCH_OV_CACHE", "SEARCH_ORT_PREPACK", "SEARCH_ORT_SPIN",
                    "SEARCH_PARTS", "SEARCH_POOL_MAX", "SEARCH_STATEMENT_TIMEOUT_MS", "SEARCH_STORE", "SEARCH_STORE_FOLD_CASE",
                    "SEARCH_STORE_MAX_AGE_S", "SEARCH_STORE_PREV", "SEARCH_STORE_REFRESH_S", "SEARCH_WORKERS", "SEARCH_LOG",
                    "SEARCH_SQLITE_CACHE_KB", "SEARCH_MEMSTAT", "SEARCH_CACHE_FLIGHT_WAIT_S", "SEARCH_WARM_YIELD_STEP_S",
                    "SEARCH_WARM_YIELD_TOTAL_S", "SEARCH_WARM_VISITOR_WINDOW_S", "SEARCH_WARM_SLOTS_WITH_VISITORS",
                    "SEARCH_KVEC_RETRY_S", "SEARCH_EMBED_RETRY_S")
PARTS_CLOCK_OK = ("cached", "_warm_yield", "_load_embedder", "kind_vectors", "generation", "tables", "stage", "stage_since", "stored_text", "junk_domains", "junk_domains_ut1", "suggest_candidates")   # cache expiry and timing only
PART_STAGES = {
    # card_build reaches no search code: its code version moves only with the card's own code and what it calls
    "card": dict(roots=("card_build",), settings=(
                           "SEARCH_ACCOUNTS_IN_CARD", "SEARCH_CARDLINKS", "SEARCH_CONFIDENT_RANK", "SEARCH_CONFIDENT_RANK_OWN",
                           "SEARCH_LOOKUP_BACKEND", "SEARCH_LOOKUP_DB", "SEARCH_OWN_RANK_TSV", "SEARCH_POSTGRES",
                           "SEARCH_RELEASE_DIR", "SEARCH_SHORT_ENTITY_VIEWS", "SEARCH_SHORT_FLOOR", "SEARCH_SHORT_NAMES", "SEARCH_SHORT_RATIO",
                           "SEARCH_SITE_RANK", "SEARCH_SITENAV", "SEARCH_SITES_BACKEND", "SEARCH_TYPO_INDEX", "SEARCH_WEB_BACKEND"), not_results=PARTS_SPEED_ONLY, clock_ok=PARTS_CLOCK_OK),
    # the ranked candidates of one search: retrieval, fusion, the models (their real devices are in the key)
    "base": dict(roots=("_search_retry",), settings=(
                           "SEARCH_ACCOUNTS_IN_CARD", "SEARCH_ANSWER_DEAD", "SEARCH_BRAND_SITES",
                           "SEARCH_BRAND_SITES_FILE", "SEARCH_CE_ALT_DIR", "SEARCH_CE_DEVICE", "SEARCH_CE_DIR",
                           "SEARCH_CE_DIR_IGPU", "SEARCH_CE_GOV_DIR", "SEARCH_CE_MAX_TOKENS", "SEARCH_CE_TEXT",
                           "SEARCH_CE_TOP", "SEARCH_CE_WIKI_FLOOR", "SEARCH_CONFIDENT_RANK",
                           "SEARCH_CONFIDENT_RANK_OWN", "SEARCH_COUNTRY", "SEARCH_FUZZY_SIM",
                           "SEARCH_GOV_PAGES_DEPTH", "SEARCH_GOV_PAGES_GIVEN", "SEARCH_GOV_PAGES_POOL",
                           "SEARCH_GOV_PAGES_READ", "SEARCH_GOV_STATES", "SEARCH_HEAD_WEIGHT", "SEARCH_HOME",
                           "SEARCH_HOME_RANK", "SEARCH_HOURS_STALE_DAYS", "SEARCH_INTENT_DIR", "SEARCH_KIND_CONF_W",
                           "SEARCH_KIND_EXACT_W", "SEARCH_KIND_HOME_W", "SEARCH_KIND_LEAD_W",
                           "SEARCH_KIND_LEAD_W_OPEN", "SEARCH_KIND_LIFT_CONF", "SEARCH_KIND_SIM",
                           "SEARCH_KIND_SITE_SIM", "SEARCH_LINK_SITE_LOW_GUARD", "SEARCH_LINK_SITE_MIN_SITELINKS",
                           "SEARCH_LOOKUP_BACKEND", "SEARCH_LOOKUP_DB", "SEARCH_MODEL_DIR", "SEARCH_NEWS_BACKEND",
                           "SEARCH_NEWS_DB", "SEARCH_NEWS_SQLITE", "SEARCH_NOMIC_DIR", "SEARCH_OFFICIAL_FIRST",
                           "SEARCH_OFFICIAL_LEAD", "SEARCH_OWN_RANK_TSV", "SEARCH_PAGES_FUSE_K",
                           "SEARCH_PAGES_FUSE_W", "SEARCH_PIN_EXACT_ARTICLE", "SEARCH_PLACE_EXACT_KM",
                           "SEARCH_PLACE_RADIUS_KM", "SEARCH_PLACE_UNDER_THING", "SEARCH_POSTGRES", "SEARCH_PRODUCTS",
                           "SEARCH_PRODUCT_MAKER_MIN", "SEARCH_PRODUCT_SITE_FIRST", "SEARCH_PRODUCT_TIER_WIDE",
                           "SEARCH_RANKER", "SEARCH_RELEASE_DIR", "SEARCH_RELEASE_ID", "SEARCH_RRF_K",
                           "SEARCH_SEM_BACKEND", "SEARCH_SEM_IVF", "SEARCH_SEM_PAGES_IVF",
                           "SEARCH_SEM_PAGES_MAX_BITS", "SEARCH_SEM_PAGES_PROBES", "SEARCH_SEM_PAGES_W",
                           "SEARCH_SEM_PROBES", "SEARCH_SITES_BACKEND", "SEARCH_SITES_BLOCK_WEB",
                           "SEARCH_SITES_INDEX", "SEARCH_SITES_INTENT_MIN", "SEARCH_SITE_PAGES",
                           "SEARCH_SITE_PAGES_N", "SEARCH_SITE_PAGES_W", "SEARCH_SITE_RANK", "SEARCH_STATE_ANCHORS",
                           "SEARCH_STATE_ANCHOR_MIN", "SEARCH_STATE_ANCHOR_SOURCES", "SEARCH_STRONG_BM25",
                           "SEARCH_TYPO_EDITS2_LEN", "SEARCH_TYPO_INDEX", "SEARCH_TYPO_KNOWN_DF",
                           "SEARCH_TYPO_KNOWN_RATIO", "SEARCH_TYPO_MIN", "SEARCH_TYPO_NAMES", "SEARCH_TYPO_POP",
                           "SEARCH_TYPO_SIM", "SEARCH_TYPO_WIDE", "SEARCH_TYPO_WORDS", "SEARCH_TYPO_WORDS_FILE",
                           "SEARCH_TYPO_WORD_DF", "SEARCH_WEB_BACKEND", "SEARCH_WEB_INTENT_MIN",
                           "SEARCH_WIKI_BACKEND", "SEARCH_WIKI_INDEX", "SEARCH_WX_SQLITE", "SEARCH_YT_BARE_MIN"), not_results=PARTS_SPEED_ONLY, clock_ok=PARTS_CLOCK_OK + ("_search",),
                 runtime=lambda: base_runtime()),
    # the rows as shown: mix, gate, breadth cleanup, curated fill, web rerank, site rows
    "list": dict(roots=("list_build",), settings=(
                           "SEARCH_BREADTH", "SEARCH_BREADTH_CITE", "SEARCH_BREADTH_FLOOR", "SEARCH_BREADTH_WIKI",
                           "SEARCH_BREADTH_WIKI_FROM", "SEARCH_BREADTH_WIKI_MIN", "SEARCH_CARD_GATE",
                           "SEARCH_CE_DEVICE", "SEARCH_CE_DIR", "SEARCH_CE_DIR_IGPU", "SEARCH_CE_MAX_TOKENS",
                           "SEARCH_CE_TEXT", "SEARCH_CE_TOP", "SEARCH_CONFIDENT_RANK", "SEARCH_CONFIDENT_RANK_OWN",
                           "SEARCH_CURATED_FILL", "SEARCH_FACTS", "SEARCH_HOME", "SEARCH_INSTANT_LIST_FIRST", "SEARCH_INTENT_DIR",
                           "SEARCH_LOOKUP_BACKEND", "SEARCH_LOOKUP_DB", "SEARCH_OFFICE_SITES", "SEARCH_OWN_RANK_TSV",
                           "SEARCH_PLACE_RADIUS_KM", "SEARCH_POSTGRES", "SEARCH_RELEASE_DIR", "SEARCH_SCORE_ALL", "SEARCH_SCORE_ALL_TOP",
                           "SEARCH_SE_INDEX", "SEARCH_SE_MIN", "SEARCH_SE_POOL", "SEARCH_SE_REUSE", "SEARCH_SE_ROWS",
                           "SEARCH_SITES_BACKEND", "SEARCH_SITES_BLOCK_WEB", "SEARCH_SITE_BRIDGE", "SEARCH_SITE_RANK",
                           "SEARCH_SITE_SLOTS", "SEARCH_SITE_SLOTS_LEAD", "SEARCH_TYPO_INDEX", "SEARCH_WEB_BACKEND",
                           "SEARCH_WEB_INTENT_MIN", "SEARCH_WEB_RERANK", "SEARCH_WEB_SIGNAL", "SEARCH_WEB_WEAK_CE",
                           "SEARCH_WIKILINKS"), not_results=PARTS_SPEED_ONLY, clock_ok=PARTS_CLOCK_OK + ("_search",),
                 runtime=lambda: base_runtime()),
    # the place searched by name and what the card says of it
    "place": dict(roots=("place_build",), settings=(
                           "SEARCH_CONFIDENT_RANK", "SEARCH_CONFIDENT_RANK_OWN", "SEARCH_HOME",
                           "SEARCH_LOOKUP_BACKEND", "SEARCH_LOOKUP_DB", "SEARCH_OWN_RANK_TSV",
                           "SEARCH_PLACE_EXACT_KM", "SEARCH_POSTGRES", "SEARCH_RELEASE_DIR", "SEARCH_SITES_BACKEND",
                           "SEARCH_SITE_RANK", "SEARCH_TYPO_INDEX", "SEARCH_WEB_BACKEND"), not_results=PARTS_SPEED_ONLY, clock_ok=PARTS_CLOCK_OK),
    # confidence, intent and kind card: judged by the web-intent model and the kinds data
    "verdict": dict(roots=("verdict_build",), settings=(), not_results=PARTS_SPEED_ONLY, clock_ok=PARTS_CLOCK_OK + ("_search",),
                    runtime=lambda: base_runtime()),
    # does the name have branches (names_places)
    "local": dict(roots=("local_build",), settings=(
                           "SEARCH_CE_DIR", "SEARCH_CE_GOV_DIR", "SEARCH_CONFIDENT_RANK", "SEARCH_CONFIDENT_RANK_OWN",
                           "SEARCH_LOOKUP_BACKEND", "SEARCH_LOOKUP_DB", "SEARCH_NOMIC_DIR", "SEARCH_OWN_RANK_TSV",
                           "SEARCH_POSTGRES", "SEARCH_RELEASE_DIR", "SEARCH_SITES_BACKEND", "SEARCH_SITE_RANK",
                           "SEARCH_TYPO_INDEX", "SEARCH_WEB_BACKEND"), not_results=PARTS_SPEED_ONLY, clock_ok=PARTS_CLOCK_OK),
    # the order of the stages and which field comes from which: finished_answer's own body, nothing it calls. Not stored; its
    # key stamps an answer. Every function it calls is either a stage above or a step whose effect is in the content the
    # next stage is keyed by (card_pre, glue_build, the curated lookup), which a re-stamp runs again.
    "rest": dict(roots=("finished_answer",), settings=(), not_results=PARTS_SPEED_ONLY, clock_ok=PARTS_CLOCK_OK, stop="*"),
}
if PARTS_ON - PART_STAGES.keys():
    raise SystemExit(f"SEARCH_PARTS names a stage that does not exist: {sorted(PARTS_ON - PART_STAGES.keys())}")
def base_runtime() -> dict:
    """What only run time knows: the device each model really runs on (a GPU that failed to load falls back to the CPU
    whatever the setting says). The cross-encoder's int8 CPU scores and its fp32 CUDA scores differ, so the device is in
    the key of every stage that scores."""
    def prov(o):
        sess = getattr(o, "sess", None)
        return sess.get_providers()[0] if sess is not None else None
    try:
        ce = cross_encoder()
        inner = getattr(ce, "inner", ce)
        out = {"ce": [getattr(ce, "device", None), getattr(ce, "name", None), prov(inner)] if ce else None}
    except Exception as e:  # noqa: BLE001
        out = {"ce": f"unavailable {type(e).__name__}"}
    try:
        emb = _embedder if _embedder is not None else (embed("warm up") and _embedder)
        out["bge"] = prov(emb) if emb else None
    except Exception as e:  # noqa: BLE001
        out["bge"] = f"unavailable {type(e).__name__}"
    try:
        sem = sem_ivf()
        out["nomic"] = prov(sem[2]) if sem else None
    except Exception as e:  # noqa: BLE001
        out["nomic"] = f"unavailable {type(e).__name__}"
    return out


def parts_for(stage: str):
    """The Parts object when SEARCH_PARTS has this stage and the store is open, else None (the stage runs live)."""
    if stage not in PARTS_ON:
        return None
    with _parts_lock:
        if not _parts_state:
            _parts_state.append(None)
            st = result_store()
            if st is None:
                print("parts off: the result store is not open (SEARCH_STORE unset or the file failed to open)", flush=True)
            if st is not None:
                try:
                    import parts as _p
                    P = _p.Parts(st, os.path.abspath(__file__), [_p.Stage(n, **c) for n, c in PART_STAGES.items()],
                                 {k: v for k, v in globals().items() if isinstance(v, contextvars.ContextVar)})
                    print("parts on: " + ", ".join(f"{n} {P.info(n)['version']}" for n in sorted(PARTS_ON)), flush=True)
                    _parts_state[0] = P
                except Exception as e:  # noqa: BLE001
                    print(f"parts off: {type(e).__name__}: {e}", flush=True)
    return _parts_state[0]


# Curated answers (2026-11-05): for the warmed names, a list chosen offline by a large model (Qwen3-Reranker-4B over
# the engine's own 50+ candidates; tools/curated-build.py) and kept in the store by release. The page's list
# (/instant) shows it instead of the engine's ranked list; the card, places and answers are worked out as always.
# /search, which every eval asks, never reads it. SEARCH_CURATED=1 turns it on; the release is SEARCH_RELEASE_ID.
CURATED_RELEASE = (os.environ.get("SEARCH_RELEASE_ID") or "") if os.environ.get("SEARCH_CURATED", "0") == "1" else ""


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
    """False for a name whose card is a person, or a named thing that is not a product (card_sells), unless the words
    ask to buy or name a thing one buys."""
    ql = (q or "").lower()
    asks = bool(_SHOP.search(ql) or _SHOP_THING.search(ql))
    if not card_sells(card) and not asks:
        return False
    if PERSON_SHOP or not card_is_person(card):
        return True
    return asks


# No store box over a named thing that is not sold (2026-11-14, owner: "gmail" got Target/Amazon/Walmart, "obviously not a
# store product"; "keep" over the KEEP radio station, "definitely not something to buy"). A card with an official site
# is a named thing: an organisation, a service, a product. The box stays only when its Wikidata description (else its
# lead's first sentence) says it is a product, and not a service, software or medium. A card without an official site
# (a word: "watch", "wallet") and no card at all are left to the department label as before. SEARCH_CARD_SELLS=0: off.
CARD_SELLS = os.environ.get("SEARCH_CARD_SELLS", "1") == "1"
_NOT_SOLD = re.compile(r"\b(services?|software|program|app|apps|application|platform|website|web site|web-based|online|"
                       r"search engine|social (media|network\w*)|streaming|messag\w*|webmail|e-?mail|cloud|assistant|"
                       r"radio station|television station|tv station|broadcast\w*|channel|newspaper|magazine|film|"
                       r"band|musical group|political|university|school|college|government|agency|city|town|county|"
                       r"editor|suite|operating system|browser|exchange|bank|airline|restaurant|retailer|seller)\b", re.I)
_SOLD = re.compile(r"\b(products?|brand|line of|series of|models?|devices?|consoles?|smartphones?|phones?|tablets?|"
                   r"laptops?|notebooks?|computers?|e-?readers?|speakers?|headphones?|earbuds?|earphones?|cameras?|"
                   r"watch(es)?|smartwatch\w*|trackers?|televisions?|monitors?|printers?|routers?|media player|"
                   r"appliances?|vehicles?|cars?|automobiles?|trucks?|motorcycles?|bicycles?|shoes?|sneakers?|clothing|"
                   r"apparel|footwear|toys?|dolls?|video games?|board games?|beverages?|drinks?|soda|beer|wine|snacks?|"
                   r"cereals?|cand(y|ies)|chocolates?|cosmetics?|fragrances?|perfumes?|tools?|furniture|mattress\w*|"
                   r"guitars?|instruments?|manufacturer|manufactures|"
                   r"cookies?|biscuits?|crackers?|juices?|detergents?|soaps?|sauces?|condiments?|foods?|electronics?|"       # CORE-B-12
                   r"household (goods|products)|cleaning|toothpaste|shampoos?|coffee|tea|chips|pastas?|noodles?|cheeses?|"
                   r"sweeteners?|spirits?|liquors?|vodka|whisk(e)?y|energy drinks?)\b", re.I)


def card_sells(card) -> bool:
    if not CARD_SELLS or not card or not any(l.get("title") == "Official site" for l in card.get("links", [])):
        return True
    text = card.get("description") or re.split(r"(?<=[.!?])\s", card.get("lead") or "", maxsplit=1)[0]
    # what it is comes first: "The iPhone is a line of smartphones ..., that run iOS, the company's own mobile
    # operating system" is a product though the sentence goes on to software
    head = re.split(r",|;| that | which | developed | owned ", text, maxsplit=1)[0]
    if _SOLD.search(head) and not _NOT_SOLD.search(head):
        return True
    return bool(text) and not _NOT_SOLD.search(text) and bool(_SOLD.search(text))


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


_fin_miss = threading.local()


def fin_get(q: str, full: bool) -> dict | None:
    """The finished answer for q, or None: none stored, too old, another layout, or the hand-picked list of q was
    written, changed or removed since (a removal must win over a stored answer)."""
    def _miss(why: str):
        _fin_miss.why = why
        return None
    st = result_store() if CURATED_RELEASE else None
    if st is None:
        return _miss("miss:nostore")
    try:
        why_m = []
        b = st.get_finished(CURATED_RELEASE, fin_key(q, full), FIN_MAX_AGE_S, miss=why_m)
        if not b:
            return _miss("miss:" + (why_m[0] if why_m else "none"))
        if b.get("v") != FIN_V or any(k not in b for k in FIN_FIELDS):
            return _miss("miss:v")
        if st.curated_at(CURATED_RELEASE, curated_key(q)) != b.get("cur_at"):
            return _miss("miss:cur")
        if "cur_at_card" in b:                           # the list of the card's own name too (audit B-07; rows built before it have none)
            t = (b.get("card") or {}).get("title")
            if isinstance(t, str) and st.curated_at(CURATED_RELEASE, curated_key(t)) != b["cur_at_card"]:
                return _miss("miss:cur")
        if HOME and (b.get("local") or b.get("place") or any(r.get("source") == "place" for r in b["results"])):
            return _miss("miss:pos")                     # built with no position: here a position would change it
        _fin_miss.why = "hit"
        return b
    except Exception:  # noqa: BLE001 - a stored answer is extra
        return _miss("miss:err")


PROVCHECK = os.environ.get("SEARCH_PROVCHECK", "0") == "1"     # off unless the builder's engine sets it: the call is not for visitors


def provcheck(calls_json: str) -> dict:
    """Run the base searches a stored answer ran (its prov row's calls) under THIS release and return their hash, the same
    one cand_summary stored: equal = the retrieval and ranking of the answer is the same under this release. Retrieval, ranking
    and the cross-encoder (compute cache) only; no card, place, news or list work."""
    _warm.set(True)
    calls = json.loads(calls_json)
    if not isinstance(calls, list) or len(calls) > 8 or cand_summary is None:
        return {"error": "bad calls"}
    done = []
    for a in calls:
        if not (isinstance(a, list) and len(a) == 6 and isinstance(a[0], str) and len(a[0]) <= 200):
            return {"error": "bad call"}
        done.append((a, base_part(a[0], int(a[1]), bool(a[2]), bool(a[3]), bool(a[4]), bool(a[5]))))
    h, _t, _c = cand_summary(done)
    return {"hash": h}


def prov_put(q: str, full: bool) -> bool:
    """Keep what this request's retrieval returned (provenance.cand_summary), for release deltas (phase 4). Extra: never fails."""
    st = result_store() if CURATED_RELEASE else None
    _p = _prov.get()
    if st is None or not _p or cand_summary is None:
        return False
    try:
        st.put_prov(CURATED_RELEASE, fin_key(q, full), *cand_summary(_p))
        return True
    except Exception:  # noqa: BLE001 - provenance is extra
        return False


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
            r = st.reset_lists(p, CURATED_RELEASE, FIN_V) if CURATED_RELEASE else {}   # a lists base replaces what came before (install.py lists)
            if "reset_refused" in r:
                raise ValueError(r["reset_refused"])
            n = dict(st.import_file(p), **r)
            reset_done = "reset_finished" in r
            if not reset_done:
                c = st.import_dept(p)                            # shopping departments (2026-11-13): not tied to a release
                if any(c.values()):
                    n = dict(n, **{f"dept_{k}": v for k, v in c.items()})
            if CURATED_RELEASE:                              # curated answers ride the same inbox (2026-11-05)
                if not reset_done:
                    c = st.import_curated(p, CURATED_RELEASE)
                    if any(c.values()):
                        n = dict(n, **{f"curated_{k}": v for k, v in c.items()})
                    c = st.import_finished(p, CURATED_RELEASE, FIN_V)    # finished answers too (2026-11-12)
                    if any(c.values()):
                        n = dict(n, **{f"finished_{k}": v for k, v in c.items()})
                c = st.import_verified(p, CURATED_RELEASE)   # answers the builder rebuilt unchanged (audit DB-03)
                if c.get("verified"):
                    n = dict(n, verified=c["verified"])
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


_CONTAINERS = (dict, list, tuple, set)


def _clone(v):
    """A private copy of a cached value: dicts, lists, tuples and sets are rebuilt, every other leaf (str, number, None,
    an object of another type) is shared as it was. About 0.12 ms for a 20-row result list (copy.deepcopy: 0.41 ms)."""
    t = type(v)
    if t is dict:
        d = v.copy()
        for k, x in d.items():
            if type(x) in _CONTAINERS:
                d[k] = _clone(x)
        return d
    if t is list:
        return [_clone(x) if type(x) in _CONTAINERS else x for x in v]
    if t is tuple:
        return tuple([_clone(x) if type(x) in _CONTAINERS else x for x in v])
    if t is set:
        return set(v)
    return v


# One computation per key at a time (audit CORE-B-05, RT-06): the first caller of an uncached key computes, the others
# wait for it up to CACHE_FLIGHT_WAIT_S and take its result; a follower whose leader failed, timed out or kept nothing
# (a "prev" row) computes for itself, once, so a slow failure is never queued N times behind each other.
CACHE_FLIGHT_WAIT_S = float(os.environ.get("SEARCH_CACHE_FLIGHT_WAIT_S", "20"))
_inflight: dict = {}
_MISS = object()


def cached(key, compute, static: bool = False):
    """compute() once per key per CACHE_TTL_S; static: per CACHE_STATIC_TTL_S and snapshot. Every caller gets its own
    copy of the value: rows are never shared between requests (audit DB-12, CORE-A-04), so an edit of a row by
    one request cannot show in another, and what the cache holds is never exposed."""
    now = time.monotonic()
    gen = generation() if static else None
    ttl = CACHE_STATIC_TTL_S if static else CACHE_TTL_S
    if static and _warm.get():
        ttl = ttl / 2
    flight = None
    for _round in (0, 1):
        with _cache_lock:
            hit = _cache.get(key)
            if hit and now - hit[0] < ttl and hit[1] == gen:
                _cache.move_to_end(key)
                held = hit[2]
                break
            fl = _inflight.get(key)
            if fl is None:
                flight = _inflight[key] = (threading.Event(), threading.get_ident())
                held = _MISS
                break
            held = _MISS
        if fl[1] == threading.get_ident() or _round:     # our own compute asks for its own key, or already waited once
            break
        if START_TRACE:
            _t = time.monotonic()
            fl[0].wait(CACHE_FLIGHT_WAIT_S)
            start_trace.lock_wait("single-flight (cached)", (time.monotonic() - _t) * 1000)
        else:
            fl[0].wait(CACHE_FLIGHT_WAIT_S)
        now = time.monotonic()                           # the leader's entry is dated after our first look
    if held is not _MISS:
        return _clone(held)
    box = [False]
    tok = _prev_served.set(box)
    try:
        val = compute()
        if box[0]:                                  # held the old build's row: not kept in memory (the warmer recomputes it)
            outer = _prev_served.get()
            if outer is not None:
                outer[0] = True
            return val
        with _cache_lock:
            _cache[key] = (now, gen, _clone(val))
            _cache.move_to_end(key)
            while len(_cache) > CACHE_MAX:
                _cache.popitem(last=False)
        return _clone(val)
    finally:
        _prev_served.reset(tok)
        if flight is not None:                      # after the entry is in: a woken follower must find it
            with _cache_lock:
                if _inflight.get(key) is flight:
                    del _inflight[key]
            flight[0].set()


def _wiki(title: str, lead: str, score: float) -> dict:
    return {"url": "https://en.wikipedia.org/wiki/" + title.replace(" ", "_"),
            "title": f"{title} — Wikipedia", "content": (lead or "")[:300],
            "source": "wikipedia", "score": round(float(score), 2)}


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
_intent_lock = start_trace.lock("intent")


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
                    opts.intra_op_num_threads = min(2, _CPUS)
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


def exact_name_top(q: str, r: dict) -> bool:
    """A popular site whose name IS the query ("google" -> google.com) stays first: the
    learned ranker put google.com.ai above it."""
    host = _domain_of(r.get("url", ""))
    # any label but the ending: meraki.com's captured front page is portal.meraki.com, and
    # the learned ranker then put a plain meraki.net link (Tranco 337,692) above it
    labels = {x.replace("-", "") for x in host.split(".")[:-1]}
    return (r.get("source") == "site" and r["score"] >= NAV_SCORE - 1 and bool(r.get("popular"))
            and bool(norm_key(q)) and norm_key(q) in labels)


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
        opts.intra_op_num_threads = int(os.environ.get("SEARCH_CE_THREADS", str(min(2, _CPUS))))   # 2 vs 4 measured on 4 cores
        path = os.path.join(model_dir, "model_int8.onnx")
        if not os.path.exists(path):
            path = os.path.join(model_dir, "model.onnx")
        self.sess = ort.InferenceSession(path, opts, providers=["CPUExecutionProvider"])
        self.inputs = {i.name for i in self.sess.get_inputs()}
        self.path = path

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
        _ce = _ce_cached(_ce)
    return _ce


def _ce_cached(m):
    """A CPU cross-encoder whose scores go through the compute cache when SEARCH_COMPUTE_CACHE is set; the
    namespace names the model file, the token limit and the device that ran it."""
    cc = _compute_cache()
    if cc is None or getattr(m, "device", "") != "cpu" or not hasattr(m, "path"):
        return m
    import compute_cache as _cc
    ns = f"ce|{_cc.file_sha256(m.path)}|{CE_MAX_TOKENS}|{m.sess.get_providers()[0]}"
    print(f"cross-encoder: scores kept in the compute cache ({ns[:20]}...)", flush=True)
    return _cc.CachedScorer(m, ns, cc)


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


# A government task's own reranker (2026-11-06, owner: "we should work to make it work not just revert"): st1
# (SEARCH_CE_DIR) gave the government set 45.5 % help@1 where the shipped turbo it was fine-tuned from gives 53.9 %
# (rc14, rule grader; Clef Flash 38.6 vs 44.5). A query the state rule marks as a state task (gov_query_state, or
# the searcher's own state for one, the `gq` of _search) is reranked by SEARCH_CE_GOV_DIR (the release's
# rerank-turbo), every other query by st1. Loaded once beside it; unset, unreadable or ?drop=cegov: the one reranker.
CE_GOV_DIR = os.environ.get("SEARCH_CE_GOV_DIR", "").strip()
_ce_gov = None
_ce_gov_lock = start_trace.lock("ce_gov")


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
                        _ce_gov = _ce_cached(_ce_gov)
                    except Exception as e:
                        print(f"cross-encoder (government tasks): off ({type(e).__name__}: {e})", flush=True)
    return _ce_gov or None


def ce_text(r: dict) -> str:
    text = (r.get("title") or "") + ". " + (r.get("content") or "")[:240]
    if CE_TEXT == "domain":
        host = (urlsplit(r.get("url") or "").hostname or "").removeprefix("www.")
        return host + " | " + text
    return text


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
    if _pcheck.get():                          # ?pcheck=1: is the stored base part current? (no memory cache, no row store)
        res = base_part(q, k, semantic, learned, correct, ce)
    else:
        res = cached(ckey, lambda: computed.append(1) or stored("search", skey, lambda: base_part(q, k, semantic, learned, correct, ce)),
                     static=True)
    _cache_state.set((_store_note.get() if _store_note.get() in ("disk", "prev") else "miss") if computed else "hit")
    # after the cache: the cached list is the uncapped one, so ?pcap= needs no place in the key
    _p = _prov.get()
    if _p is not None:
        _p.append(([q, k, semantic, learned, correct, ce], res))
    res = cap_profiles(drop_junk(res, q), _profile_cap.get())
    return [] if hands_off(q, res, quiet) else res


# "news" anywhere in the kind's name: "enewspaper" (vox.com), "cable news channel", "news magazine". Not television or
# channel kinds as such: with those "apple latest news" and "iphone news" led with apple.com (Apple TV), 2026-10-09.
_OUTLET_KIND = re.compile(r"news")
_OUTLET_NOT = re.compile(r"\b(?:fake|hoax|satire|satirical)\b|^news media$")
OUTLET_MIN_CONF = 0.5
def outlet_kind(key: str) -> bool:
    return bool(_OUTLET_KIND.search(key)) and not _OUTLET_NOT.search(key)


def outlet_medium(key: str) -> bool:
    """A kind that says the site is a channel or a magazine (kinds_release.news_outlet)."""
    return bool(re.search(r"\b(?:channel|magazine)\b", key))


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
_local_maps: dict | None = None
def local_map() -> dict:
    global _local_maps
    if _local_maps is None:
        _local_maps = gov_state.load_local(os.environ.get("SEARCH_GOV_STATES") or os.path.join(RELEASE_DIR, "gov-states.json"))
    return _local_maps


_local_set: frozenset | None = None


def is_local_gov(host: str) -> bool:
    """A town's or a county's government site, not the state's own (gov-states.json's "local")."""
    global _local_set
    if _local_set is None:
        m = local_map()
        _local_set = frozenset(d for table in m.values() for ds in table.values() for d in ds)
    labels = host.lower().split(".")
    return any(".".join(labels[i:]) in _local_set for i in range(max(0, len(labels) - 1)))


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
        # sem and sem_pages are the slowest lists in 99% of computed searches (RT-02): submitted first, so a busy pool cannot delay them
        f_sem = sub("sem", _sem_rows, wq) if semantic and (SEM_BACKEND == "ivf" or "wiki_vec" in has) and wiki else None
        f_sem_pages = (sub("sem_pages", sem_page_rows, q, min(5, k))
                       if semantic and SEM_BACKEND == "ivf" and "page_docs" in has and sem_pages_dir() else None)
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
            is_pers = (lambda t: c.execute(PERSON_SQL, {"t": t}).fetchone() is not None) if "wiki_facts" in has else None
            famous = famous_namesake(nq, c.execute(FAMOUS_SQL, {"k": nq}).fetchall(), is_pers)
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
            rows = fut_result(f_places, [])
            if pq[0] == "name" and _NEAR.search(q.lower()):
                # "… near me" with a loose name match: a far business that merely sounds like the
                # words is not what was asked for; an exact name or brand is kept at any range
                rows = [r for r in rows if r[8] <= NEAR_FUZZY_KM or r[1].lower() == pq[1]]
            if not rows and f_places_fb is not None:
                rows = fut_result(f_places_fb, [])
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
            for site, qid, title, answer, bm25, votes in fut_result(f_se, []):
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
            rows, had = fut_result(f_pages, []), set()
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
        sem = fut_result(f_sem, None) if f_sem is not None else None
        if sem is not None:
            lists.append(_tl("sem", [_wiki(title, lead, sim) for title, lead, sim in sem]))
        if f_sem_pages is not None:
            sp = fut_result(f_sem_pages, [])
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
        if not _cold[0] and (not out or (out[0]["score"] < WIKI_EXACT_SCORE and best < STRONG_BM25)):   # tier 4 (not on a cold engine: one fuzzy_wiki took 70 s)
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
                    "pop": 1.0, "exact_domain": 0.15, "non_name_prefix": 1.5, "raw_domain": 0.45,
                    "root_wiki_site": 1.35, "root_site": 0.35, "root_site_wiki": 0.16,
                    "root_wiki_sequel": 0.85, "root_wiki_franchise": 0.5,
                    # Half an alias typed is weak evidence: "yankees" is the start of Freddie
                    # Freeman's Wikidata alias "yankees father" and out-scored the New York
                    # Yankees, whose own name has the word (2026-09-25). The whole alias typed
                    # ("satchmo") is not penalised further.
                    "alias_partial": 2.0}
SUGGEST_WEIGHTS2.update(_weights(os.environ.get("SEARCH_SUGGEST_WEIGHTS2", "")))
_suggest_v2 = (_NEVER, None)


def suggest_score2(kind: str, demand: float, hist: float, exact: bool, pos: int, w: dict | None = None, pop: float = 0.0) -> float:
    w = w or SUGGEST_WEIGHTS2
    return (math.log10(1 + max(0.0, demand or 0.0)) + w.get("k_" + kind, 0.0) + w["hist"] * (hist or 0.0)
            + w.get("pop", 0.0) * (pop or 0.0)
            + (w["exact"] if exact else 0.0) - (w["later"] if pos == 1 else 0.0) - (w["alias"] if pos == 2 else 0.0)
            - (w.get("alias_partial", 0.0) if pos == 2 and not exact else 0.0))


def tight(s: str) -> str:
    """The key suggest.sql stores: letters and digits only, lowercased ("Wells Fargo" -> wellsfargo)."""
    return "".join(ch for ch in s.lower() if ch.isalnum())


def suggest_score(kind: str, pop: float, hist: float, exact: bool, w: dict | None = None) -> float:
    w = w or SUGGEST_WEIGHTS
    bonus = (w["exact_wiki"] if kind == "wiki" else w["exact"]) if exact else 0.0
    return w.get(kind, 0.5) * pop + w["hist"] * hist + bonus


# --- typing suggestions, ranking layer (owner 2026-11-15, v0.4.23 screenshots: "starb" led with shopping phrases, then
# "Starbucks", "Starbreeze Studios", ... "Star Bonifacio Echeverria"; "goog" listed Google Search, googleapis.com, Google
# (about.google) and a tagline as a name). The SQL only supplies candidates; this layer cleans, collapses, floors and orders them.
SUGGEST_FLOOR_ABS = float(os.environ.get("SEARCH_SUGGEST_FLOOR_ABS", "100"))     # estimated monthly searches under which an item is obscure ...
SUGGEST_FLOOR_REL = float(os.environ.get("SEARCH_SUGGEST_FLOOR_REL", "0.002"))   # ... or under this share of the best candidate's
SUGGEST_KEEP_MIN = 3                                                             # the floors never leave fewer than this
# A site's stored name is often its page title: "Google Books: Find, Preview, and Read Books Online.", "Google Assistant, your own
# personal Google". A suggestion is a name: cut at the title's separator.
_NAME_CUT = re.compile(r"\s+[:|–—-]\s+|:\s+|\s+\|\s+|,\s+(?=[a-z])|\s+[-–—]\s+")
_GENERIC_TAIL = frozenset("search inc corporation corp company co ltd llc official site website homepage home page online store shop com".split())
_SUGGEST_INFRA = re.compile(r"(apis?|gstatic|ggpht|googleusercontent|googlesyndication|akamai\w*|cloudfront|fastly\w*|doubleclick|2mdn|"
                            r"edgekey|edgesuite|azureedge|appspot|herokuapp|cdn\w*|static\w*|metrics?|telemetry|tracking|tracker)$")


def suggest_name(name: str) -> str:
    """A suggestion's text: a name, never a page title or a tagline."""
    n = " ".join((name or "").split())
    cut = _NAME_CUT.split(n, maxsplit=1)[0].strip(" ,:;|–—-")
    if len(cut) >= 2:
        n = cut
    words = n.split()
    if len(words) > 6:
        n = " ".join(words[:6]).strip(" ,:;|–—-")
    return re.sub(r"(?<=[a-z]{4})\.$", "", n)


_RAW_DOMAIN_RE = re.compile(r"^[a-z0-9.-]+\.[a-z]{2,}$")


def _suggest_words(name: str) -> list[str]:
    b = re.sub(r"\s*\([^)]*\)$", "", name or "").strip()
    b = re.sub(r"^(?:the|a|an)\s+", "", b, flags=re.I)
    return re.findall(r"[a-z0-9]+", b.lower())


def _suggest_matches_typed(title: str, typed: str) -> bool:
    """True when the displayed title starts with the typed prefix (and respects spaces: 'toy s' != 'Toys for Bob')."""
    if not typed:
        return True
    tws = re.findall(r"[a-z0-9]+", typed.lower())
    if not tws:
        return True
    b = re.sub(r"\s*\([^)]*\)$", "", title or "").strip()
    b_nothe = re.sub(r"^(?:the|a|an)\s+", "", b, flags=re.I)
    for cand in (b, b_nothe):
        cws = re.findall(r"[a-z0-9]+", cand.lower())
        if len(tws) == 1:
            if cws and cws[0].startswith(tws[0]):
                return True
        elif len(cws) >= len(tws) and cws[:len(tws) - 1] == tws[:-1] and cws[len(tws) - 1].startswith(tws[-1]):
            return True
    return False


def _rerank_suggest_entries(entries: list[tuple[float, str, str | None, str]], typed: str, w: dict) -> tuple[list[float], list[bool]]:
    """Apply post-dedup prefix alignment and hierarchical root-entity adjustments to
    [(score, kind, domain, label), ...]. Returns (adjusted_scores, name_prefix_flags)."""
    t = tight(typed)
    has_ext = bool(re.search(r"\.[a-z]{2,}$", (typed or "").lower()))
    parsed = []
    scores = []
    for sc, kind, dom, label in entries:
        wk = _suggest_words(label)
        name_pfx = _suggest_matches_typed(label, typed)
        adj = sc
        if typed and not name_pfx:
            adj -= w.get("non_name_prefix", 1.5)
        if kind == "site" and _RAW_DOMAIN_RE.fullmatch((label or "").lower()) and not has_ext:
            adj -= w.get("raw_domain", 0.45)
        parsed.append({"sc": adj, "kind": kind, "dom": dom, "wk": wk, "name_pfx": name_pfx})
        scores.append(adj)

    # Hierarchical root promotion (London vs London Stock Exchange, Sony vs Sony Music, Toy Story vs Toy Story 5):
    # evaluated BEFORE exact_domain so an exact domain like oprah.com does not pull close to Oprah Winfrey.
    roots = [x for x in parsed if x["name_pfx"] and len("".join(x["wk"])) >= 4]
    penalize: dict[int, float] = {}
    for idx_b, b in enumerate(parsed):
        if not b["name_pfx"]:
            continue
        bw = b["wk"]
        for a in roots:
            if a["wk"] == bw or len(a["wk"]) >= len(bw) or bw[:len(a["wk"])] != a["wk"]:
                continue
            aw = a["wk"]
            gap = b["sc"] - a["sc"]
            if a["kind"] == "wiki" and b["kind"] == "site" and gap <= w.get("root_wiki_site", 1.35):
                penalize[idx_b] = max(penalize.get(idx_b, 0.0), gap + 0.05)
            elif a["kind"] == "site" and b["kind"] == "site" and gap <= w.get("root_site", 0.35):
                penalize[idx_b] = max(penalize.get(idx_b, 0.0), gap + 0.05)
            elif a["kind"] == "site" and b["kind"] == "wiki" and gap <= w.get("root_site_wiki", 0.16):
                penalize[idx_b] = max(penalize.get(idx_b, 0.0), gap + 0.05)
            elif a["kind"] == "wiki" and b["kind"] == "wiki":
                sequel = bw[len(aw)].isdigit()
                if (len(aw) >= 2 and gap <= w.get("root_wiki_franchise", 0.5)) or (sequel and gap <= w.get("root_wiki_sequel", 0.85)):
                    penalize[idx_b] = max(penalize.get(idx_b, 0.0), gap + 0.05)

    for idx_b, p in penalize.items():
        scores[idx_b] -= p
    for idx, x in enumerate(parsed):
        if x["kind"] == "site" and x["dom"] and t and _apex_label(x["dom"]) == t and x["wk"]:
            scores[idx] += w.get("exact_domain", 0.15)
    return scores, [x["name_pfx"] for x in parsed]


def rescore_suggest_response(sug: list[dict], typed: str = "", w: dict | None = None) -> tuple[list[dict], int]:
    """Replay already-built suggestion dicts (such as staging HTTP JSON) through the post-dedup
    prefix-alignment and root-entity ranking rules."""
    if not sug:
        return [], 0
    ww = w or SUGGEST_WEIGHTS2
    entries = []
    for it in sug:
        src = it.get("source") or ""
        dom = (urlsplit(it.get("url") or "").hostname or "").lower().removeprefix("www.") if src == "site" else None
        entries.append((float(it.get("score") or 0.0), src, dom, it.get("title") or ""))
    adj, name_pfx = _rerank_suggest_entries(entries, typed, ww)
    order = sorted(range(len(sug)), key=lambda i: (-adj[i], i))
    out = []
    for pos_out, i in enumerate(order):
        d = dict(sug[i], score=round(adj[i], 3))
        d.pop("lead", None)
        if pos_out == 0 and name_pfx[i] and d.get("source") in ("site", "wiki"):
            d["lead"] = True
        out.append(d)
    lead = 1 if out and out[0].get("lead") else 0
    return out, lead


def _entity_key(name: str) -> str:
    """The key that makes "Google", "Google Search" and "Google (company)" one thing."""
    w = re.sub(r"\s*\(.*\)$", "", name or "").lower().split()
    if len(w) == 1 and re.fullmatch(r"[a-z0-9-]{3,}\.(com|net|org)", w[0]):
        w = [w[0].rsplit(".", 1)[0]]                              # "Amazon.com" is Amazon
    if w and w[0] == "the" and len(w) > 1:
        w = w[1:]
    while len(w) > 1 and w[-1].strip(".") in _GENERIC_TAIL:
        w.pop()
    return tight(" ".join(w))


def _apex_label(domain: str | None) -> str:
    """The name part of a front-page host: "starbucks.com" and "www.starbucks.com" -> "starbucks"; "books.google.com" -> ""
    (a section of someone else's site is not that name's front page)."""
    parts = (domain or "").lower().split(".")
    if parts and parts[0] == "www":
        parts = parts[1:]
    return tight(parts[0]) if len(parts) == 2 or (len(parts) == 3 and len(parts[2]) == 2 and parts[1] in ("co", "com", "org", "ac", "gov", "net", "edu")) else ""


def _is_infra(domain: str | None, typed: str) -> bool:
    """googleapis.com-style hosts are plumbing, shown only when typed as a domain."""
    label = (domain or "").split(".")[0]
    if not label or not _SUGGEST_INFRA.search(label.replace("-", "")):
        return False
    return not (typed and ("." in typed or tight(typed).startswith(tight(label))))


def rank_suggestions(rows: list[tuple], exact_ids: set, n: int = SUGGEST_N, w: dict | None = None, typed: str = "") -> list[dict]:
    """rows: (id, kind, name, url, domain, wiki, pop, hist [, demand, descr, pos]). Best weighted score first, one row per
    thing (Google, Google Search and about.google are one, shown with the site the card would call official), names not
    titles, no infrastructure hosts, obscure matches dropped while popular ones exist. The first row is the best entity whose
    NAME the prefix completes; the first row carries "lead": True then (the page puts it above the searched phrases)."""
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
            sc = suggest_score2(r[1], r[8], r[7], r[0] in exact_ids, r[10], ww, r[6] or 0.0)
        else:
            sc = suggest_score(r[1], r[6], r[7], r[0] in exact_ids, w)
        # A country-code site (costcojapan.jp) is a weaker guess for a US household
        # than a generic one — the same call the nav tier makes with GENERIC_TLD.
        if r[1] == "site" and r[4] and not is_generic(r[4]):
            sc -= ww.get("foreign", 0.0)
        return sc
    scored = sorted(((score(r), r) for r in best.values()), key=lambda x: (-x[0], x[1][0]))
    groups: list[list] = []                                  # one per thing, in the order of its best member
    by_key: dict[str, list] = {}
    seen, brands = set(), set()
    for sc, r in scored:
        i, kind, name, url, domain, wiki, pop, hist = r[:8]
        if kind == "site" and domain and (_SUGGEST_PLUMBING.search(domain.split(".")[0]) or _is_infra(domain, typed)):
            continue
        key = same_page(url)
        if key in seen:
            continue
        # One site per brand: costco.com, not also costco.ca / costco.co.uk /
        # costco.com.mx (the start-page review, 2026-09-24). The best-scored one stays.
        brand = domain.split(".")[0] if kind == "site" and domain else None
        if brand and brand in brands:
            continue
        seen.add(key)
        if brand:
            brands.add(brand)
        label = suggest_name(name)
        # One entry per thing: Taylor Swift was a site, an article and a channel; Google, Google Search, about.google.
        # The best-scored leads the group; subreddits and services are other things by nature.
        ek = _entity_key(label) if kind in ("site", "wiki", "youtube") else None
        if kind == "site" and domain and ek:
            # a site's own front page named after its page title: starbucks.com "Starbucks Coffee Company" is Starbucks
            lab = _apex_label(domain)
            if lab and len(lab) >= 3 and tight(label) != lab and tight(label).startswith(lab):
                acc, cut, ws = "", None, label.split()
                for wi, wd in enumerate(ws):
                    acc += tight(wd)
                    if acc == lab:
                        cut = wi + 1
                        break
                if cut:
                    label, ek = " ".join(ws[:cut]), lab
        if ek and ek in by_key:
            by_key[ek].append((sc, r, label))
            continue
        g = [(sc, r, label)]
        groups.append(g)
        if ek:
            by_key[ek] = g
    reps = []
    for g in groups:
        sc, r, label = g[0]
        # the site the card's own-site rule would show: the entity's own name as the domain, a generic ending, then the score
        ek = _entity_key(label)

        def own(m):
            mr = m[1]
            dom = mr[4] if mr[1] == "site" else None
            lab = tight((dom or "").split(".")[0]) if dom else ""
            return (bool(lab) and lab == ek, bool(dom) and is_generic(dom), m[0])
        rep = max((m for m in g if m[1][1] == "site"), key=own, default=None) if len(g) > 1 and r[1] == "site" else None
        if rep is not None and own(rep)[0]:
            r = rep[1]
        # the shortest name in the group: "Google", not "Google Search"
        label = min((m[2] for m in g), key=lambda x: (len(x), x)) if len(g) > 1 else label
        reps.append((sc, r, label))
    if v2 and reps:
        adj, pfx_by_idx = _rerank_suggest_entries([(sc, r[1], r[4], label) for sc, r, label in reps], typed, ww)
        order = sorted(range(len(reps)), key=lambda idx: (-adj[idx], reps[idx][1][0]))
        reps = [(adj[idx], reps[idx][1], reps[idx][2]) for idx in order]
        pfx_map = {id(reps[new_i][1]): pfx_by_idx[old_i] for new_i, old_i in enumerate(order)}
    else:
        pfx_map = {id(r): True for _, r, _ in reps}
    # the popularity floor: obscure matches (an unknown person, a defunct company) do not fill the list while popular ones exist
    if v2 and reps:
        top_d = max((x[1][8] or 0.0) for x in reps[:SUGGEST_KEEP_MIN + 5])
        kept = []
        for x in reps:
            d = x[1][8]
            if d is not None and len(kept) >= SUGGEST_KEEP_MIN and (d < SUGGEST_FLOOR_ABS or d < top_d * SUGGEST_FLOOR_REL):
                continue
            kept.append(x)
        reps = kept
    out = []
    for sc, r, label in reps[:n]:
        i, kind, name, url, domain, wiki, pop, hist = r[:8]
        descr = r[9] if len(r) > 9 else None
        d = {"url": url, "title": label, "source": kind, "score": round(sc, 3)}
        if wiki:
            d["wiki"] = wiki
        if descr:
            d["subtitle"] = descr
        out.append(d)
    # the first row leads (the page puts it above the searched phrases) when the typed text is the start of its NAME
    # (pos 0) AND matches the displayed title, not of a later word, alias, or unrelated domain: "starb" -> Starbucks;
    # "son" -> never Columbia Pictures; "air fr" -> phrases first
    if (out and reps and reps[0][1][1] in ("site", "wiki")
            and (len(reps[0][1]) <= 10 or reps[0][1][10] == 0)
            and pfx_map.get(id(reps[0][1]), True)):
        out[0]["lead"] = True
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
    return rank_suggestions(suggest_drop_junk(rows, q), exact, typed=q)


# Phrase completions (step 4, serve/phrases.sql): searched phrases that extend what
# was typed ("air fr" -> "air fryer"). Keyed on the first PHRASE_KEY_MAX characters; a
# longer query is matched by filtering that prefix's phrases.
PHRASE_N = 3
PHRASE_N_LEAD = 2                 # after a leading entity ("starb" -> Starbucks) at most this many searched phrases
# SEARCH_PHRASES=0 offers no searched phrases at all. serve/phrases.sql builds them from AmazonQAC (source 'amazonqac', Amazon's
# own search-box completions, CDLA-Permissive-2.0); the owner decided Amazon-suggestion-derived text must not be published to
# installs, so the release the installs get should be built without them (or run with SEARCH_PHRASES=0) until he says otherwise.
PHRASES_ON = os.environ.get("SEARCH_PHRASES", "1") != "0"
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
    if len(k) < 3 or not PHRASES_ON:
        return []
    with _conn() as c:
        if "suggest_phrases" not in tables(c):
            return []
        row = c.execute(SUGGEST_PHRASES_SQL, {"p": k[:PHRASE_KEY_MAX].rstrip()}).fetchone()
    return pick_phrases(q, row[0], row[1], items, PHRASE_N_LEAD if items and items[0].get("lead") else PHRASE_N) if row else []


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
            for _m in ("facts", "site_pages"):     # their per-thread connections too (audit DB-08)
                try:
                    sys.modules[_m].release_thread() if _m in sys.modules else None
                except Exception:  # noqa: BLE001
                    pass

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
        u = urlsplit(self.path)
        # a visitor is any request that costs a search: /instant, /more, /explain, /search (RT-08), by the parsed warm value,
        # not a substring of the raw query (CORE-B-13: ?warm=%31 is a warm request)
        visitor = u.path in ("/instant", "/more", "/explain", "/search") and not _warm_q(parse_qs(u.query or ""))
        if visitor:
            with _busy_lock:
                _busy[0] += 1
                _last_visitor[0] = time.monotonic()
        if visitor and STORE_PATH:
            import result_store
            result_store.skip_box.set([False])     # one wait for the store lock per request, not one per read
        rid = start_trace.req_begin() if START_TRACE and visitor else 0
        try:
            return self._do_get()
        except PartStale:
            raise
        except Exception as e:  # noqa: BLE001 - audit CORE-B-04: whatever escapes a route is an error reply, never an empty one
            self.log_message("request failed: %s", type(e).__name__)
            return self._send(503, json.dumps({"error": type(e).__name__}).encode())
        finally:
            if rid:
                start_trace.req_end(rid)
            if visitor:
                with _busy_lock:
                    _busy[0] -= 1
                    _last_visitor[0] = time.monotonic()

    def _do_get(self):
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
                if not store_ready():
                    return self._send(503, f"starting: result store not open ({_store_state['error']})".encode(), "text/plain")
                with _conn() as c:
                    has = tables(c)
                if "wiki_docs" not in has and "docs" not in has:
                    return self._send(503, b"starting: no serving tables visible", "text/plain")
                if not search("wikipedia", 1, quiet=False):
                    return self._send(503, b"starting: the probe search answered nothing", "text/plain")
                note = warming_note()
                return self._send(200, (f"ready; {note}" if note else "ready").encode(), "text/plain")
            except Exception as e:
                return self._send(503, f"starting: {type(e).__name__}".encode(), "text/plain")
        if u.path in ("/instant", "/more", "/search", "/explain", "/provcheck") and not serving_ready():
            return self._send(503, b'{"error":"starting"}')
        if u.path == "/storez":                          # the warmer: rows, fingerprint, visitors in flight (back network only)
            st = result_store()
            z = {"busy": _busy[0], **(st.counts() if st else {"off": True, "store": "not open" if STORE_PATH else "none",
                                                                "error": _store_state["error"], "tries": _store_state["tries"]})}
            z["warmup"] = {**_warmup, "cold": _cold[0]}
            if _parts_state and _parts_state[0]:
                z["parts"] = {**_parts_state[0].stats, "by": _parts_state[0].by}
            sample = parse_qs(u.query or "").get("sample")
            if st and sample and sample[0].isascii() and sample[0].isdigit():     # the warmer audits rows another engine computed
                z["sample"] = st.sample_queries(min(int(sample[0]), 200))
            return self._send(200, json.dumps(z).encode())
        if u.path == "/provcheck" and PROVCHECK:        # the builder: replay the base searches of a stored answer (release_delta.py recheck)
            return self._send(200, json.dumps(provcheck((parse_qs(u.query or "").get("calls") or ["[]"])[0])).encode())
        if u.path == "/instant":
            try:                                      # counted as busy by do_GET, for every kind of visitor request
                return self._instant(parse_qs(u.query))
            except PartStale as e:                    # ?pcheck=1 only: a part of the answer is not stored under its current key
                return self._send(200, json.dumps({"stale": str(e)}).encode())
        if u.path == "/more":
            return self._more(parse_qs(u.query))
        if u.path == "/explain":
            return self._explain(parse_qs(u.query))
        if u.path == "/candidates":                      # dev tooling: the lists and each reordering, with scores
            return self._send(*__import__("sq_candidates").handle(parse_qs(u.query)))
        if u.path.startswith("/radar/"):
            m = re.fullmatch(r"/radar/([A-Z]{4})\.json", u.path)
            if m:
                return self._send(*radar_read(m.group(1)))
            m = re.fullmatch(r"/radar/([A-Z]{4})/(\d+)\.gif", u.path)
            if m:
                return self._send(*radar_read(m.group(1), int(m.group(2))))
            return self._send(404, b'{"error":"not found"}')
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
        if optional("web_only", lambda: web_only(q, res), False, self.log_message):
            res = optional("web_rerank", lambda: web_rerank(q, res), res, self.log_message)
            sig = optional("web_signal", lambda: web_signal(q, res), {}, self.log_message)
        intent = optional("intent", lambda: intent_of(q, res), "", self.log_message)
        conf = optional("confident", lambda: is_confident(res) and intent == "nav", False, self.log_message)
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
                    body = {"results": [{k: v for k, v in r.items() if k != "kind_card"} for r in search(q, MAX_K)]}   # as /search and the glue do (CORE-B-13)
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
        slot = False
        if _warm_q(qs):                                  # RT-01: the warmer gives way to visitors
            _warm_waited.set([0.0])
            if _last_visitor[0] and time.monotonic() - _last_visitor[0] < WARM_VISITOR_WINDOW_S:
                slot = _warm_slots.acquire(timeout=30)   # visitors about: one warm request at a time
        try:
            return self._instant_run(qs)
        finally:
            if slot:
                _warm_slots.release()

    def _instant_run(self, qs) -> None:
        """As-you-type: prefix suggestions on every keystroke, plus the full results
        once the query looks finished (unless results=0). ?all=1 lists every candidate with
        its raw signals, for tools/eval-completion.py --suggester tune."""
        q = (qs.get("q") or [""])[0].strip()[:200]
        t0 = time.monotonic()
        _near.set(parse_near((qs.get("near") or [""])[0]))
        _reach_km.set(parse_reach((qs.get("reach") or [""])[0]))
        _no_webrr.set((qs.get("webrr") or ["1"])[0] == "0")      # eval A/B only
        _parts_off.set((qs.get("parts") or ["1"])[0] == "0")      # eval/shadow A/B only
        _pcheck.set((qs.get("pcheck") or ["0"])[0] == "1")        # the builder: is every part of this answer current?
        _part_keys.set({} if (_warm_q(qs) or _pcheck.get()) else None)               # the keys this answer is made from (a finished answer's provenance)
        _warm.set((qs.get("warm") or ["0"])[0] == "1")          # warm.py's head-query refresh (2026-11-04)
        _audit.set(_warm.get() and (qs.get("audit") or ["0"])[0] == "1")
        _prov.set([] if _warm.get() and ((qs.get("fin") or [""])[0] == "1" or (qs.get("prov") or [""])[0] == "1") else None)
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
            try:
                rows, exact = suggest_candidates(q)
            except Exception as e:
                self.log_message("candidates failed: %s", type(e).__name__)
                return self._send(503, json.dumps({"error": type(e).__name__}).encode())
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
            # why a request was not served from a stored answer (RT-09): a code, never the text
            fin_why = ("off" if not FINISHED else "warm" if _warm.get() else "noresults" if not want else "position" if not plain
                       else "gate" if not (full or wordy(q) or short_org(q)) else "")
            fin = fin_get(q, full) if not fin_why else None
            fin_why = fin_why or ("hit" if fin is not None else getattr(_fin_miss, "why", "miss"))
            if _warm.get() and (qs.get("prov") or [""])[0] == "1":      # the builder records the retrieval of an answer it already holds (release_delta.py backfill)
                finished_answer(q, qs, want, full, plain, fin_full, self.log_message)
                return self._send(200, json.dumps({"prov": prov_put(q, fin_full)}).encode())
            if _pcheck.get():                # the builder asks only whether every part of this answer is current (parts_restamp.py)
                finished_answer(q, qs, want, full, plain, fin_full, self.log_message)
                return self._send(200, json.dumps({"pk": part_stamp()}).encode())
            with stage("suggest"):
                sug = optional("suggest", lambda: cold_call("suggest", lambda: suggest(q), []), [], self.log_message)
            with stage("phrases"):
                phrases = optional("phrases", lambda: cold_call("phrases", lambda: suggest_phrases(q, sug), []), [], self.log_message)
            lead = 1 if sug and sug[0].get("lead") else 0     # the first suggestion goes above the phrases (page: suggest_lead)
            # results=0: suggestions only — the page asks for full results separately, so a
            # slow full search (worst ~1.3 s cold) never holds up the as-you-type list (want, above).
            # full=1: a submitted search (address bar, search.logout.cloud#q=...) — results
            # whatever the length, and more of them; as-you-type keeps the wordy gate
            full = (qs.get("full") or ["0"])[0] == "1"
            with stage("results"):          # the answer's stages (finished_answer): the search, card, place, verdict, list, local
                built = None if fin is not None else finished_answer(q, qs, want, full, plain, fin_full, self.log_message)
            with stage("news_wait"):
                got_news = optional("news", lambda: (f_news.result() if want else typing_wait(f_news, [{"stories": [], "hot": False}]))[0], {"stories": [], "hot": False}, self.log_message) if f_news is not None else {"stories": [], "hot": False}
                if not isinstance(got_news.get("stories"), list):
                    got_news = {"stories": [], "hot": False}
            try:                                            # live scores: never cached
                with stage("sports"):
                    games = (sports(q) if want else cold_typing(lambda: sports(q), [])) if len(q) >= 3 else []
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
                    espn = (cached(("espn", q.lower()), lambda: [browser_scores(q) if len(q) >= 3 else None])[0] if want
                            else cold_typing(lambda: cached(("espn", q.lower()), lambda: [browser_scores(q) if len(q) >= 3 else None])[0], None))
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
                def _wx_stories():
                    if weather_query(q):                    # the home forecast: local weather stories
                        with stage("wx_news"):
                            home_town = (wx.get("place") or "").split(",")[0]
                            return cached(("wxnews", home_town), lambda: [weather_news(town=home_town)])[0]
                    town = (wx.get("place") or "").split(",")[0].strip().lower()      # another town's: only stories naming it
                    return [x for x in latest if town and town in (x.get("title") or "").lower()]
                latest = optional("wx_news", _wx_stories, [], self.log_message)
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
        except PartStale:
            raise
        except Exception as e:
            self.log_message("instant failed: %s: %s", type(e).__name__, e)   # never the query text
            return self._send(503, json.dumps({"error": type(e).__name__}).encode())
        def emit(res, conf, card, place, intent, kinds, local, sig):     # the answer goes out: built here, or stored (fin)
            ms = int((time.monotonic() - t0) * 1000)
            note = _store_note.get()
            if START_TRACE:
                start_trace.request_done(ms, st)
            self.log_message("instant suggest=%d phrases=%d results=%d ms=%d cache=%s%s%s stages=%s cpu=%s full=%d fin=%s", len(sug), len(phrases),
                             len(res), ms, _cache_state.get(), " warm" if _warm.get() else "",
                             f" audit={note}" if note in ("same", "diff") else "", stages_text(st), stages_text(_stages_cpu.get()),
                             int(fin_full), fin_why)
            if _warm.get():
                sig = dict(sig, store={"cache": _cache_state.get(), "note": note})
            # ms / stages / intent: the page's timing line and "why" tooltips (2026-09-26) —
            # names and numbers only, never the query
            lg = self.log_message
            res = optional("name_bare", lambda: name_bare_rows(res, card), res, lg)              # a hand-picked or stored list too
            over = optional("shop_wanted", lambda: shop_wanted_over(q, card), False, lg)
            shown = optional("shop_list", lambda: shop_list(q, res), res, lg) if over else res
            shop = optional("shop_box", lambda: shop_box(q), None, lg) if over else None
            body = {"suggest": sug, "phrases": phrases, "results": shown,
                    "shop": shop, "confident": conf, "card": card,
                    "news": latest, "news_hot": got_news.get("hot", False), "news_more": got_news.get("more", False), "mine": got_news.get("mine") or [], "answer": ans,
                    "games": games, "standings": table, "no_game": no_game, "espn": espn, "weather": wx, "place": place, "kinds": kinds,
                    "ms": ms, "stages": st, "intent": intent, "local": local, "suggest_lead": lead, **sig}
            try:
                data = json.dumps(body).encode()
            except (TypeError, ValueError) as e:         # an extra the encoder cannot write: the reply without the extras
                lg("reply encode failed: %s", type(e).__name__)
                core = {k: body[k] for k in ("suggest", "phrases", "results", "confident", "card", "ms", "stages", "intent", "local")}
                data = json.dumps(core, default=str).encode()
            return self._send(200, data)

        try:
            if fin is not None:
                _cache_state.set("fin")
                return emit(*(fin[k] for k in FIN_FIELDS))
            res, conf, card, place, intent, kinds, local, sig = built
            return emit(res, conf, card, place, intent, kinds, local, sig)
        except Exception as e:                           # nothing escapes to the socket: an error reply, not an empty one
            self.log_message("instant emit failed: %s", type(e).__name__)
            return self._send(503, json.dumps({"error": type(e).__name__}).encode())


WARM_MAX_S = float(os.environ.get("SEARCH_WARM_MAX_S", "180"))        # the start-up load stops here and the port opens anyway (a slow disk: stage budgets apply for a while)
WARM_POSTINGS = os.environ.get("SEARCH_WARM_POSTINGS", "1") == "1"    # the two big Tantivy postings files (2.3 GB on rc15) too, when the room allows
SELFCHECK_STAGE_MS = float(os.environ.get("SEARCH_SELFCHECK_STAGE_MS", "1500"))
WARM_READ_S = float(os.environ.get("SEARCH_WARM_READ_S", "12"))        # the bulk reads stop here: on a disk that delivers 25-120 MB/s (buildbox, Windows: 3.8 GB took 32 s) the rest is left to the first searches
_warm_deadline = [float("inf")]
_warm_steps: list[dict] = []


def _tr_step(name: str, fn):
    """One named start step: run it, print its time and the bytes the process read meanwhile (Linux /proc, Windows GetProcessIoCounters),
    keep it for the trace table. A step that fails is logged and the next one runs (the ones that must succeed are required elsewhere)."""
    io0 = start_trace.io_counters()
    t0, c0 = time.monotonic(), time.process_time()
    err = ""
    try:
        r = start_trace.step(name, fn) if START_TRACE else fn()
    except Exception as e:  # noqa: BLE001
        r, err = None, f"{type(e).__name__}: {e}"
    dt, cpu = time.monotonic() - t0, time.process_time() - c0
    io1 = start_trace.io_counters()
    mb = (io1.get("read_bytes", 0) - io0.get("read_bytes", 0)) / (1 << 20)
    _warm_steps.append({"step": name, "s": round(dt, 2), "cpu_s": round(cpu, 2), "read_mb": round(mb)})
    print(f"warm step {name}: {dt:.1f} s wall, {cpu:.1f} s cpu, {mb:.0f} MB read, ended {time.monotonic() - _PROC_T0:.1f} s after search_api.py began" + (f", FAILED {err}" if err else ""), flush=True)
    return r


_read_deadline = [float("inf")]


def _read_step(rows: list, label: str) -> None:
    """Large sequential reads of release files, one logged step per file (rows are [relative path, offset, length]); none after the read
    phase's time box (WARM_READ_S): what is skipped is logged and read by the searches that need it."""
    import warmplan
    by_file: dict[str, list] = {}
    for r in rows:
        by_file.setdefault(r[0], []).append(r)
    for rel, rs in by_file.items():
        if sum(r[2] for r in rs) < (1 << 20):
            continue                                   # a file under 1 MB is read by its first search; not worth a line
        if time.monotonic() >= _read_deadline[0]:
            print(f"warm: {label} {rel} ({sum(r[2] for r in rs) >> 20} MB) skipped: the {WARM_READ_S:.0f} s read time box is used up", flush=True)
            continue
        _tr_step(f"{label} {rel}", lambda rs=rs: warmplan.read_ranges(RELEASE_DIR, rs, 1 << 62, min(_warm_deadline[0], _read_deadline[0]), lambda k: None, None))


def _models_parallel() -> bool:
    """Load the three models at once only when the machine can hold their start peak and has the cores: 4+ CPUs and 6+ GB free. A small
    machine loads them one at a time (the 3 GB cap of the minimum machine)."""
    import warmplan
    return _CPUS >= 4 and warmplan.available_ram() > (6 << 30)


def startup_warm() -> bool:
    """Everything a search needs, loaded by name, in order, BEFORE the port opens: nothing is served meanwhile, so nothing yields and nothing
    can stall at 75%. 1 the result store; 2 the bulk files by large sequential reads (warmplan.py: IVF codes, signals, kinds, the Tantivy files,
    the hot ranges of the lookup database and the registry); 3 the indexes, the lookups, the page-id map (a file, mapped); 4 each model,
    loaded and run once (one at a time on a small machine); 5 the kind vectors and the other dictionaries; 6 a self-check: one end-to-end search
    and one typed request, every stage under SELFCHECK_STAGE_MS or the miss is logged. Returns True when it ran inside WARM_MAX_S."""
    t0 = time.monotonic()
    _warm_deadline[0] = t0 + WARM_MAX_S
    _read_deadline[0] = t0 + WARM_READ_S
    import warmplan
    if STORE_PATH:
        _tr_step("result store (open)", result_store)
        _tr_step("result store (index pages)", store_index_walk)
    state_dir = os.path.dirname(STORE_PATH) if STORE_PATH else ""
    rid = os.path.basename(os.path.realpath(RELEASE_DIR)) + "-" + str(os.path.getsize(os.path.join(RELEASE_DIR, "registry.sqlite")) if os.path.exists(os.path.join(RELEASE_DIR, "registry.sqlite")) else 0) if RELEASE_DIR else ""
    # a start never runs the table pass (dbstat reads 1.4 GB in 4 KB syscalls: 10 s on NVMe, minutes on indexhost's ZFS and on a slow disk): it uses the kept
    # plan, or the files-only plan when none was kept; the install-time prepare step (SEARCH_PREPARE_ONLY=1) builds and keeps the full one
    plan = _tr_step("warm plan", lambda: warmplan.load_plan(RELEASE_DIR, state_dir, rid, tables=False)) if RELEASE_DIR else None
    if plan and not plan.get("tier2"):
        print("warm: no kept warm plan with the hot table ranges: they are skipped (the prepare step at install builds it)", flush=True)
    if plan:
        room = warmplan.file_budget() or (1 << 62)          # what the cache can hold beside the engine's own memory (cgroup limit, Windows free RAM)
        small = [r for r in plan["tier1"] if "codes.u8" not in r[0]]
        codes = [r for r in plan["tier1"] if "codes.u8" in r[0]]
        # in value order: the small files every search touches, then the IVF codes (50 MB of every semantic search), then the hot ranges
        # of the lookup database and the registry; each only while it fits: reading more than fits evicts what was read first (3 GB cap)
        t2 = plan["tier2"]
        t2_lookup, t2_rest = [r for r in t2 if r[0] == "lookup.sqlite"], [r for r in t2 if r[0] != "lookup.sqlite"]
        # cold pages of the big postings files cost the +142-198 s requests of 1.8-5 s on buildbox (wiki 1.8 s, sites 2.0 s: T1, 2026-11-17)
        small_a = [r for r in small if r[2] <= (100 << 20)]
        small_b = [r for r in small if r[2] > (100 << 20)]       # signals.bin, the wiki term dictionary: after the codes when the room is short
        for rows, label in ((small_a, "read"), (codes, "read"), (small_b, "read"), (t2_lookup, "read hot ranges"), (plan.get("postings", []) if WARM_POSTINGS else [], "read postings"), (t2_rest, "read hot ranges")):
            by_file: dict[str, list] = {}
            for r in rows:
                by_file.setdefault(r[0], []).append(r)
            for rel, rs in sorted(by_file.items(), key=lambda kv: sum(r[2] for r in kv[1])) if label == "read" and rows is small_a else by_file.items():
                need = sum(r[2] for r in rs)
                if need > room:
                    print(f"warm: {label} {rel} ({need >> 20} MB) skipped: {room >> 20} MB of room left", flush=True)
                    continue
                _read_step(rs, label)
                room -= need
    _tr_step("semantic index (sem_ivf)", sem_ivf)
    _tr_step("wiki text index", wiki_text)
    _tr_step("web text indexes", web_text)
    _tr_step("lookups", lookups)
    if LOOKUP_BACKEND == "release" and SEM_BACKEND == "ivf":
        _tr_step("page-id map (wikipedia page id to doc id)", lambda: lookups().pid_map())
    models = [("model: query embedder", lambda: embed("warm up")), ("model: web intent", _intent_model),
              ("model: cross-encoder", lambda: cross_encoder()("warm up", ["a short title. a sentence", "another title. another sentence"])),
              ("model: cross-encoder (government tasks)", lambda: (ce_gov() and ce_gov()("warm up", ["a short title. a sentence"])))]
    if _models_parallel():
        with ThreadPoolExecutor(max_workers=len(models)) as ex:
            list(ex.map(lambda m: _tr_step(m[0], m[1]), models))
    else:
        for name, fn in models:
            _tr_step(name, fn)
    _tr_step("kind vectors", warm_kinds)
    _tr_step("channel index, flagged sites", warm_lookups)
    in_time = time.monotonic() - t0 < WARM_MAX_S
    _tr_step("self-check", selfcheck)
    print(f"warm-up: {time.monotonic() - t0:.1f} s in {len(_warm_steps)} steps; " +
          ", ".join(f"{x['step']} {x['s']}" for x in sorted(_warm_steps, key=lambda x: -x["s"])[:6]), flush=True)
    _warmup.update(state="done", done=0, total=0)
    return in_time


def prepare_release() -> None:
    """SEARCH_PREPARE_ONLY=1: build what a start would otherwise build once per release, then exit: the warm plan (10 s of dbstat reads), the page-id
    map (3.8 s CPU) and the channel index (3-7 s), kept beside the result store (SEARCH_STORE's folder must exist). The installer runs it after
    it unpacks a release and before the `current` pointer moves (and after any data update), so even the first start finds them."""
    import warmplan
    state_dir = os.path.dirname(STORE_PATH) if STORE_PATH else ""
    rid = os.path.basename(os.path.realpath(RELEASE_DIR)) + "-" + str(os.path.getsize(os.path.join(RELEASE_DIR, "registry.sqlite")) if os.path.exists(os.path.join(RELEASE_DIR, "registry.sqlite")) else 0)
    _tr_step("warm plan", lambda: warmplan.load_plan(RELEASE_DIR, state_dir, rid))
    if LOOKUP_BACKEND == "release":
        _tr_step("lookups", lookups)
        if SEM_BACKEND == "ivf":
            _tr_step("page-id map", lambda: lookups().pid_map())
        _tr_step("channel index", lambda: lookups().yt_index())
    print("prepared", flush=True)


def store_index_walk() -> None:
    """The root and interior pages of the stored answers' and hand-picked lists' b-trees into the cache: 300 lookups of keys that are not there
    each walk the tree from the root, so the first real stored answer is not the first to read them (200-440 ms on buildbox, 2026-11-17)."""
    st = result_store()
    if st is None or not CURATED_RELEASE:
        return
    for i in range(300):
        st.curated_at(CURATED_RELEASE, f"warm{i}")
        st.get_finished(CURATED_RELEASE, fin_key(f"warm{i}", True), FIN_MAX_AGE_S)


def selfcheck() -> bool:
    """The final check that warm is warm, not the way things get loaded: one full search (through the base search, its semantic part, the
    cross-encoder and the list stages) twice and one typed request, each in the real code path with its stage times; every stage must be
    under SELFCHECK_STAGE_MS. A miss is logged with the stage; it does not stop the engine."""
    bad = []
    for q, kind in (("facebook", "search"), ("taylor swift", "search"), ("fac", "typing")):
        st = {}
        _stages.set(st)
        _stages_cpu.set({})
        t = time.monotonic()
        try:
            if kind == "search":
                search(q, 8)
            else:
                suggest(q)
                news(q)
        except Exception as e:  # noqa: BLE001
            bad.append(f"{q}: {type(e).__name__}")
            continue
        for name, ms in st.items():
            if ms > SELFCHECK_STAGE_MS:
                bad.append(f"{q}/{name} {ms:.0f} ms")
        print(f"warm self-check {kind} {q!r}: {(time.monotonic() - t) * 1000:.0f} ms; stages " +
              ",".join(f"{k}:{v:.0f}" for k, v in sorted(st.items(), key=lambda kv: -kv[1])[:6]), flush=True)
    if bad:
        print("warm self-check FAILED (a stage was still cold): " + "; ".join(bad), flush=True)
    return not bad


def warming_note() -> str:
    """"" when the warm-up is over, else "warming N%" (the page can answer from the public site meanwhile)."""
    if _warmup["state"] == "done":
        return ""
    tot = _warmup["total"]
    return f"warming {int(100 * _warmup['done'] / tot) if tot else 0}%"


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
            _tr_step("model " + name, fn)
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
                bg_wait_idle()                    # a search first
                fn()
        except Exception as e:
            print(f"warm {name}: {type(e).__name__}", flush=True)
    print(f"lookups warmed in {time.time() - t0:.1f} s", flush=True)



# Run as `python search_api.py` this file is `__main__`: the stage modules below `from search_api import ...`, which would load a
# second copy of it half-way. Register the running module under its own name before the first stage import.
if __name__ == "__main__":
    sys.modules.setdefault("search_api", sys.modules["__main__"])

# The stage modules: each stage's private code, moved out of this file unchanged. They import what they need from here, so
# they are loaded after everything above is defined; their names come back into this namespace for the code that calls them.
from stage_parts import (
    PartStale,
    _part_keys,
    _parts_lock,
    _parts_off,
    _parts_state,
    _pcheck,
    _warm_q,
    note_part,
    part_run,
    part_stamp,
)


# The stage modules: each stage's private code, moved out of this file unchanged. They import what they need from here, so
# they are loaded after everything above is defined; their names come back into this namespace for the code that calls them.
from stage_base import (
    ACCOUNT_SQL,
    ALWAYS_OPEN_IF_STATED,
    BRAND_SITES,
    CE_ALT_DIR,
    CE_WIKI_FLOOR,
    D2Q_SQL,
    DESCRIBED_SQL,
    DOMAIN_DOC_SQL,
    DOMAIN_NAV_SQL,
    EXACT_ANCHOR_LEAD,
    EXACT_ANCHOR_MIN,
    EXTRA_FEATURES,
    FAMOUS_MIN,
    FAMOUS_RATIO,
    FAMOUS_SQL,
    FEATURES,
    FUZZY_SIM,
    FUZZY_SITE_SQL,
    FUZZY_WIKI_SQL,
    GOV_PAGES_GIVEN,
    GUTENBERG,
    HOME_RANK,
    HOST_NAV_SQL,
    HOURS_INFO_SQL,
    HOURS_STALE_DAYS,
    KIND_CARD_N,
    KIND_CONF_W,
    KIND_COUNTRY,
    KIND_EXACT_W,
    KIND_FILLER,
    KIND_GOOD,
    KIND_HOME_W,
    KIND_JEV_BAND,
    KIND_KEYS_SQL,
    KIND_KEY_EXACT_SQL,
    KIND_KEY_SQL,
    KIND_KEY_SQL_OPEN,
    KIND_LEAD_W,
    KIND_LEAD_W_OPEN,
    KIND_LIFT_CONF,
    KIND_MIN_GOOD,
    KIND_MIN_MEMBERS,
    KIND_NAMED_POOL,
    KIND_POP,
    KIND_POP_OPEN,
    KIND_SCORE,
    KIND_SIM,
    KIND_SITE_SIM,
    KIND_SITE_SQL,
    KIND_SITE_WORDS,
    KIND_SQL,
    KIND_TOP,
    KIND_UNDER_ARTICLE,
    LATE_HOURS_KINDS,
    LINK_SITE_LOW_GUARD,
    LINK_SITE_MIN_SITELINKS,
    LINK_SITE_SQL,
    LINK_SITE_TRUSTED,
    MAJOR_TEAM_BOXES,
    MAJOR_TEAM_SQL,
    NAMED_TOWN_MIN_POP,
    NAMESAKE_RATIO,
    NAMESAKE_SCORE,
    NAV_SITES_SQL,
    NAV_SQL,
    NEAR_FUZZY_KM,
    NEWS_KINDS,
    OFFICIAL_LEAD,
    OUTLET_SCORE,
    OUTLET_TRIES,
    PIN_EXACT_ARTICLE,
    PLACES_SQL,
    PLACE_CATEGORY_SCORE,
    PLACE_KIND_FALLBACK,
    PLACE_LEAD,
    PLACE_MATCH_KINDS,
    PLACE_MATCH_NAME,
    PLACE_NAME_HINT,
    PLACE_NAME_SCORE,
    PLACE_NAME_SETUP,
    PLACE_UNDER_THING,
    PRODUCTS,
    PRODUCT_MAKER_MIN,
    PRODUCT_SCORE,
    PRODUCT_SQL,
    PRODUCT_TIER_WIDE,
    PRODUCT_TOP,
    RRF_K,
    SELF_NAMED_PREFIX,
    SELF_NAMED_SQL,
    SELF_NAMED_SUFFIX,
    SEM_IVF_DOCS_SQL,
    SEM_PAGES_MAX_BITS,
    SEM_PAGES_PROBES,
    SEM_PAGES_W,
    SEM_PROBES,
    SEM_WIKI_SQL,
    SE_SQL,
    SIGILS,
    SITES_INTENT_MIN,
    SITE_NAME_SITELINKS,
    SITE_PAGES,
    SITE_PAGES_N,
    SITE_PAGES_PATH,
    SITE_PAGES_W,
    STATE_ANCHORS,
    STATE_ANCHOR_MIN,
    STATE_ANCHOR_SOURCES,
    STOCK_BARE_SQL,
    STOCK_SCORE,
    STOCK_SQL,
    SUB_EXACT_SQL,
    SUB_POPULAR,
    SUB_SEARCH_SQL,
    SUSPECT_ALWAYS_OPEN,
    TASK_GENERIC,
    TASK_TOOL,
    TOWN_TITLE_SQL,
    TOWN_WORD_MIN_POP,
    TYPO_EDITS2_LEN,
    TYPO_KNOWN_DF,
    TYPO_KNOWN_RATIO,
    TYPO_MAX_WORDS,
    TYPO_MIN,
    TYPO_NAMES,
    TYPO_NAME_CLASSES,
    TYPO_NEAR_MORE_SQL,
    TYPO_NEAR_SQL,
    TYPO_POP,
    TYPO_SIM,
    TYPO_SQL,
    TYPO_WIDE,
    TYPO_WORDS,
    TYPO_WORD_DF,
    TYPO_WORD_USES,
    WIKI_DEMAND_SQL,
    WIKI_EXACT_SQL,
    YT_BARE_MIN,
    YT_BARE_SQL,
    YT_EXACT_SQL,
    YT_POPULAR,
    YT_SEARCH_SQL,
    _ACCOUNT_NOT_A_NAME,
    _ACCOUNT_TAIL,
    _ACCOUNT_WORDS,
    _ANCHOR_NONWORD,
    _ASSET_HOST,
    _BARE_TICKER,
    _CARD_AS_TYPED,
    _CARD_HEAD,
    _EDIT_LETTERS,
    _GOOGLE_EX,
    _GOV_HOST,
    _GOV_PATH_NEWS,
    _GOV_PATH_SERVICE,
    _GOV_SERVICE_WORD,
    _HOME_GENERIC,
    _HOME_NEAR,
    _HOME_NOT,
    _KIND_QUESTION,
    _LANG_ROOT,
    _LOCAL_TASK,
    _NAME_QUALIFIER,
    _NAV_NOISE,
    _NOT_PLURAL,
    _NOT_RECOMMENDED,
    _OFFICIAL_SUFFIX,
    _ORG_BOX,
    _OUTLET_TAIL,
    _PERSON_BOX,
    _PLACE_BOX,
    _RANKER,
    _R_SLASH,
    _SENTENCE_END,
    _SITE_ASK,
    _SPAN,
    _STOCK_GENERIC,
    _STOCK_SUFFIX,
    _STOCK_WORDS,
    _SUB_WORDS,
    _TASK_SKIP,
    _TITLE_QUALIFIER,
    _TITLE_SPLIT,
    _WAS,
    _WORDS,
    _YT_CREATOR,
    _YT_HANDLE,
    _YT_HEAD,
    _YT_LIST,
    _YT_PRODUCTS,
    _YT_TAIL,
    _YT_WORDS,
    _brand_sites,
    _ce_alt,
    _ce_alt_lock,
    _code_evidence,
    _days_since,
    _described,
    _feat_ctx,
    _kind,
    _kind_cols,
    _kind_site_rows,
    _place,
    _plain_words,
    _plural,
    _point_states,
    _point_towns,
    _products,
    _qcode_busy,
    _qcode_cache,
    _qcode_compute,
    _qcode_lock,
    _query_ctx,
    _ranker,
    _search_corrected,
    _search_retry,
    _sem_pages_dir,
    _sem_rows,
    _singular,
    _site,
    _site_pages_lock,
    _site_pages_state,
    _state_anchor_db,
    _state_anchors,
    _stored,
    _subreddit,
    _task_words_hit,
    _tl,
    _tr,
    _typo_words,
    _youtube,
    account_query,
    anchor_key,
    anchor_list,
    bare_channel_row,
    bare_channel_score,
    bare_site,
    bare_ticker_ok,
    base_part,
    best_state_page,
    box_kind,
    brand_site_first,
    brand_sites,
    ce_alt,
    ce_rerank,
    chain_fill,
    choose_hours,
    code_is_state,
    correct_query,
    correct_wide,
    correct_words,
    corrects_state_away,
    demote_name_matches,
    demote_weak_names,
    dictionary_word,
    discontinued,
    edits,
    exact_anchor,
    exact_article_top,
    exact_name_first,
    exact_names,
    extra_features,
    famous_namesake,
    features,
    first_sentence,
    front_host,
    gov_query_state,
    holds_a_pair,
    home_gov_state,
    home_key,
    home_local,
    home_page_up,
    hours_plausible,
    is_bare,
    is_country_ending,
    is_news_outlet,
    is_untitled,
    keep_top,
    kind_base,
    kind_bm25_sql,
    kind_card,
    kind_card_title,
    kind_cols,
    kind_key,
    kind_key_sql,
    kind_named_first,
    kind_open,
    kind_query,
    kind_unfit,
    kind_worded,
    lift_famous,
    link_low,
    link_low_yields,
    local_domains,
    local_page_first,
    maker_in_host,
    maker_products,
    max_edits,
    model_features,
    more_names,
    name_fits,
    name_front_pages,
    name_pairs,
    name_sans_ending,
    name_sites_under_place,
    named_only,
    named_place_leads,
    named_site,
    names_place,
    names_platform_page,
    namesakes,
    nav_keys,
    nav_order,
    near_kinds,
    needs_correction,
    news_only,
    not_recommended,
    official_deep,
    official_score,
    official_subdomain_yields,
    official_top,
    one_edit,
    one_home_page,
    org_first,
    other_endings_last,
    other_states_last,
    outlet_first,
    outlet_names,
    pick_correction,
    place_hours_info,
    place_site,
    pm_misread,
    point_state,
    point_town,
    possessive_variant,
    product_candidates,
    product_leads_list,
    product_results,
    product_site_lead,
    product_tail,
    products_table,
    profiles_follow_article,
    promoted,
    query_code,
    rerank,
    respells_known_words,
    result_key,
    root_beats_inner_pages,
    rrf,
    self_named,
    sem_page_rows,
    sem_pages_dir,
    shown_by_address,
    site_ask,
    site_name,
    site_over_article,
    site_page_rows,
    site_pages_table,
    spaced_as_asked,
    stage_since,
    state_anchor_first,
    state_page_first,
    state_service_query,
    stock_key,
    stock_query,
    stock_results,
    stored_text,
    subreddit_query,
    task_kinds,
    the_keys,
    town_article,
    town_reading_far,
    town_titles,
    typo_names_setting,
    typo_shape,
    typo_words,
    typo_words_setting,
    unaccent,
    untitled_last,
    untyped_sigil,
    wiki_rows,
    with_hours,
    youtube_query,
    youtube_url,
)


# The stage modules: each stage's private code, moved out of this file unchanged. They import what they need from here, so
# they are loaded after everything above is defined; their names come back into this namespace for the code that calls them.
from stage_card import (
    CARDLINKS_MAIN,
    CARDLINKS_MORE,
    CARDLINKS_PATH,
    CARD_DESCR_SQL,
    CARD_IMAGE_SQL,
    CARD_SITE_SQL,
    CARD_WITHIN,
    DESCR_MAX,
    DESCR_STORED_CUT,
    PLATFORM_HOME,
    SHORT_ENTITY_VIEWS,
    SHORT_FLOOR,
    SHORT_MAKERS,
    SHORT_NAMES,
    SHORT_QUALIFIERS,
    SHORT_RATIO,
    SHORT_SUFFIXES,
    SITENAV_MAX,
    SITENAV_PATH,
    TWO_LETTER_KEEP,
    _CARDLINK_KIND,
    _EMPTY_PARENS,
    _LOGIN_TAIL,
    _PLATFORM_IS,
    _PLATFORM_NOT,
    _RW,
    _cardlinks_db,
    _name_words,
    _one_off,
    _short_plain_current,
    _sitenav,
    _sitenav_lock,
    _title_views,
    account_base,
    card_article,
    card_build,
    card_description,
    card_image,
    card_links,
    card_of,
    card_pre,
    card_stage,
    clean_lead,
    lead_with_official,
    mark_respelled,
    own_name_site,
    plain_title_first,
    platform_home,
    respells_name,
    short_entity,
    short_name_title,
    site_tree,
    sitenav,
    top_is,
    whole_description,
    wiki_top,
    with_own_accounts,
)


# The stage modules: each stage's private code, moved out of this file unchanged. They import what they need from here, so
# they are loaded after everything above is defined; their names come back into this namespace for the code that calls them.
from stage_list import (
    BREADTH_CITE,
    BREADTH_LINKS,
    BREADTH_WIKI,
    BREADTH_WIKI_FROM,
    BREADTH_WIKI_MIN,
    OFFICE_MAX,
    OFFICE_SITES,
    SCORE_ALL_TOP,
    SE_INDEX_MIN,
    SE_INDEX_PATH,
    SE_INDEX_POOL,
    SE_INDEX_REUSE,
    SE_INDEX_ROWS,
    SITE_BRIDGE_PATH,
    SITE_SLOTS_LEAD,
    SITE_SLOTS_PATH,
    WIKILINKS_PATH,
    WIKI_MIX_MAX,
    WIKI_MIX_SLOTS,
    _BREADTH_QUESTION,
    _BREADTH_SKIP,
    _BREADTH_STOP,
    _BREADTH_UNCAPPED,
    _CG_MU,
    _CG_SD,
    _CG_STOP,
    _CG_W,
    _GENERIC_LABEL,
    _SEASON,
    _SE_LICENCES,
    _SE_STOP,
    _TENANT_HOSTS,
    _WIKI_SKIP_DOMAINS,
    _cg_toks,
    _digest_of,
    _link_rows,
    _merge_wiki,
    _official_hosts_of_name,
    _scores,
    _scores_reuse,
    _se_ix_credit,
    _se_ix_db,
    _site_of,
    _site_table,
    _site_tables,
    _site_tables_lock,
    _uncapped_row,
    _wiki_rows,
    _wikilinks_db,
    breadth_list,
    card_gate,
    card_gate_p,
    curated_fill,
    list_build,
    list_stage,
    mix_wikipedia,
    office_rows,
    old_season,
    score_unscored,
    se_howto,
    se_ix_licence,
    se_ix_lookup,
    se_ix_row,
    site_intent_rows,
    wiki_links,
    with_office_rows,
)


# The stage modules: each stage's private code, moved out of this file unchanged. They import what they need from here, so
# they are loaded after everything above is defined; their names come back into this namespace for the code that calls them.
from stage_place import (
    CARD_PLACE_MI,
    LONE_PLACE_MI,
    ORG_PLACE_MI,
    PERSON_SQL,
    PLACE_PRACTICE,
    place_answer,
    place_build,
    place_in_card,
    place_stage,
)


# The stage modules: each stage's private code, moved out of this file unchanged. They import what they need from here, so
# they are loaded after everything above is defined; their names come back into this namespace for the code that calls them.
from stage_verdict import (
    conf_of,
    intent_kinds,
    verdict_build,
    verdict_stage,
)


# The stage modules: each stage's private code, moved out of this file unchanged. They import what they need from here, so
# they are loaded after everything above is defined; their names come back into this namespace for the code that calls them.
from stage_local import (
    NAMES_PLACES_MIN,
    _NOWHERE,
    local_build,
    local_part,
    names_places,
)


# The stage modules: each stage's private code, moved out of this file unchanged. They import what they need from here, so
# they are loaded after everything above is defined; their names come back into this namespace for the code that calls them.
from stage_assembly import (
    NOT_BRANCH,
    _compact,
    _within,
    branches_only,
    curated_by_card,
    fin_put,
    finished_answer,
    fold_accounts,
    glue_build,
    place_kind,
)

if __name__ == "__main__":
    # `import search_api` (sq_candidates' /candidates) must find this running module, not load a second copy of the engine
    # with its own caches, pools and models (CORE-B-11)
    sys.modules.setdefault("search_api", sys.modules["__main__"])
    # one after the other: loaded together, the cross-encoder's iGPU compile and the kind
    # vectors went over testbox's 1 GB limit at start (OOM-killed once, 2026-09-30)
    if os.environ.get("SEARCH_PREPARE_ONLY") == "1":
        prepare_release()
        sys.exit(0)
    start_trace.start(RELEASE_DIR)          # the report thread (SEARCH_START_TRACE=1) and the Windows-like test aid (SEARCH_TEST_NO_READAHEAD=1)
    require_filtered_web()  # release lookups + Postgres web lists: refused
    require_reranker()  # SEARCH_REQUIRE_RERANKER=1: loads the CPU model once, here, before the warm thread
    require_kinds_model()  # SEARCH_REQUIRE_KINDS_MODEL=1: the same for the kinds query model
    if STORE_PATH:
        import result_store as _rs
        _rs.ResultStore.yield_hook = staticmethod(lambda: bg_wait_idle(0.5))    # an intake of a lists base pauses for every search in flight, but not for good
        _rs.ResultStore.is_builder = staticmethod(lambda: _warm.get())   # the warmer and the builders never skip a store read
        if START_TRACE:
            _rs.ResultStore.trace_hook = staticmethod(start_trace.lock_wait)    # time waiting for the store lock, in the trace table
        threading.Thread(target=store_open_loop, daemon=True, name="store-open").start()
    if os.environ.get("SEARCH_START_WARM", "1") != "0":
        if not startup_warm():
            _cold[0] = True                 # the load ran out of time: optional stages keep their budget for a while
            threading.Timer(WARM_MAX_S, lambda: _cold.__setitem__(0, False)).start()
    else:                                   # the old order: only what must open, the rest lazily
        for _n, _f in (("sem_ivf", sem_ivf), ("wiki_text", wiki_text), ("web_text", web_text), ("lookups", lookups)):
            _tr_step(_n, _f)
    if STORE_PATH and STORE_INBOX_EVERY_S > 0:    # the answers take-in starts after the load: it read 2.4 GB at the same time as the load's 3.8 GB (buildbox S3: port at 43 s, 9.5 s search)
        threading.Thread(target=store_inbox_loop, daemon=True, name="store-inbox").start()
    if NEWS_FILL_EVERY_S > 0:               # genuinely background work, after the port is warm: it yields to searches (bg_wait_idle)
        threading.Thread(target=news_vec_fill_loop, daemon=True, name="news-vec-fill").start()
    print("ready", flush=True)
    ThreadingHTTPServer((BIND, PORT), Handler).serve_forever()
