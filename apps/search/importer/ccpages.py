"""Page-level captures for the most popular sites (2026-09-24).

Homepages answer "which site"; a how-to query wants a page inside one (support,
docs, help). For the top-N not-blocked domains by Tranco rank this scans one crawl's
URL index and keeps up to PER_DOMAIN English HTML pages per domain, any subdomain
(support.apple.com counts for apple.com): status 200, no query string, at most three
path levels, shortest paths first (CC crawls important pages; short paths are the
site's own sections), and not account/cart/search/tag/legal pages.

    ccpages --crawl C [--top 10000] [--per-domain 15]   -> cc_pages
    ccfetch --pages                                     -> page_docs

Answer sites (2026-10-08): a question ("how to freeze bananas") wants one article out of
thousands on a recipe, how-to or review site, and 15 short paths are that site's section pages,
not its articles. With --answer-per-domain N a site whose kind says it publishes answers
(ANSWER_KINDS over kind_members) also gets up to N pages picked by answer_score(), kind 'answer'.

Government sites (2026-10-11): "NJ firearms id card" wants the State Police's firearms page, and
a state's whole government sits under one domain (nj.gov), so its 15 shortest paths are section fronts.
With --gov-per-unit N a government site (gov_domain) also gets up to N pages for each of its agencies
(a host plus a first path level), picked by gov_score(), kind 'gov', at most --gov-per-domain a site.
ccgov-scan writes the candidates to a file and changes nothing: the count comes before the fetch.
"""

from __future__ import annotations

import heapq
import os
import re
import tempfile
import time

from ccscan import _duck, archived, log

# Paths that are never the answer to a search.
SKIP_PATH = (r"(^|/)(login|log-in|signin|sign-in|signup|sign-up|register|cart|checkout|basket|account|"
             r"my-account|search|tag|tags|author|feed|rss|wp-admin|wp-login|wp-json|cdn-cgi|privacy|"
             r"privacy-policy|terms|terms-of-service|terms-of-use|cookie|cookies|legal|sitemap)(/|$|\.)")

# Subdomains that are plumbing, not somewhere a person goes (first label, before the
# registrable domain). Common Crawl having an English HTML front page for the exact
# host is the real filter; this just skips the obvious ones before the scan.
_INFRA = re.compile(r"^(api|apis|cdn\d*|static\d*|img\d*|images?|media|assets?|s3|ocsp|crl|update|updates|"
                    r"telemetry|events?|data|config|clients?\d*|edge|ns\d*|dns\d*|mx\d*|smtp|imap|pop3?|"
                    r"autodiscover|analytics|metrics|tracking|track|pixel|ads?|adserver|stats?|logs?|collector|"
                    r"beacon|push|notify|sync|gateway|gw|proxy|lb|elb|origin|cache|fonts?|prod|stage|staging|"
                    r"dev|test|qa|infra|svc|internal|mail|webmail|email|login|auth|sso|secure|accounts?|"
                    r"\d+|[a-z]\d*|[a-z]{1,2}-[a-z0-9-]+)$")


def infra_host(host: str, domain: str) -> bool:
    """drive.google.com / google.com -> False; cdn.x.com, api.x.com, 6.x.com -> True."""
    if not host.endswith("." + domain):
        return True
    labels = host[: -len(domain) - 1].split(".")
    return any(_INFRA.match(lbl) for lbl in labels)


PAGES_SQL = f"""SELECT d, h, p, pr, l, wf, wo, wl
        FROM j WHERE s = 200 AND m = 'text/html' AND q IS NULL AND l LIKE '%eng%'
          AND d IN (SELECT domain FROM top_d)
          AND p <> '/' AND length(p) <= 80
          AND NOT regexp_matches(p, '^(/[^/]+){{4,}}')
          AND NOT regexp_matches(lower(p), '{SKIP_PATH}')
        QUALIFY row_number() OVER (PARTITION BY d ORDER BY length(p), h, p) <= $per"""


# ---------------------------------------------------------------------------------------------
# Answer sites. Which kinds mark a site as one, and the least confidence that counts:
# kind key -> (group, min conf). Measured on rc6's kinds.sqlite, 2026-10-08. One directory row is
# conf 0.5 (kinds_open MEMBERS_SQL), and for a topical name that is not enough: at 0.5 "tutorial"
# and "guide" lead with youtube.com and europa.eu, "cooking" with cnn.com, "consumer information"
# with google.com, and "review" with amazon.com. So a name that is also a topic needs a second
# vote (0.55) or a direct statement (0.75); only names that describe what a site is are taken at
# 0.5. Forums are left out on purpose: at 0.75 "internet forum" leads with kiwifarms.net and
# stormfront.org. The rule finds 715 ranked sites (reference 305, review 184, howto 85, qa 53,
# health 37, recipe 27, money 24): it is not a list of thousands, the kinds data holds none. With
# every name at 0.5 it is 1,589 sites, about half of the first 70 wrong.
ANSWER_KINDS = {
    "cooking": ("recipe", 0.55), "cookbook": ("recipe", 0.5), "food blog": ("recipe", 0.5),
    "do it yourself": ("howto", 0.5), "study guide": ("howto", 0.5), "advice": ("howto", 0.5),
    "tutorial": ("howto", 0.55), "guide": ("howto", 0.55),
    "review": ("review", 0.75), "consumer organization": ("review", 0.75), "computer magazine": ("review", 0.75),
    "technology news": ("review", 0.5), "technology journalism": ("review", 0.5), "product testing": ("review", 0.5),
    "rating": ("review", 0.5), "consumer information": ("review", 0.55),
    "question answer": ("qa", 0.55), "knowledge market": ("qa", 0.5),
    "encyclopedia": ("reference", 0.75), "dictionary": ("reference", 0.75), "reference work": ("reference", 0.75),
    "reference": ("reference", 0.75), "documentation": ("reference", 0.55),
    "health": ("health", 0.75), "medical": ("health", 0.55), "medical specialty": ("health", 0.55), "e health": ("health", 0.5),
    "personal finance education": ("money", 0.5), "personal finance": ("money", 0.75),
}


def answer_group(kinds, groups=None) -> str | None:
    """(kind key, conf) rows of one site -> the answer group of its most confident answer kind, or None.
    groups, when given, limits which groups count (the owner may want recipe/howto/review only)."""
    best = None
    for key, conf in kinds:
        g, need = ANSWER_KINDS.get(key, (None, 0))
        if g and (conf or 0) >= need - 1e-6 and (groups is None or g in groups) and (best is None or (-conf, g) < best):
            best = (-conf, g)
    return best[1] if best else None


# What a URL alone says about a page answering a question (the URL index has no title). Checked against
# the 82,602 pages of rc6 by their titles (how to / recipe / review / best, 867 of them), 2026-10-08:
# a path scoring 3 or more is answer-titled 9.2 % of the time against 1.1 % overall, and 25 % of all
# pages score below 0 (listings, ids, dates). It is a weak signal (AUC 0.61 on that shortest-path
# sample): its job is to put articles ahead of listings inside a site already known to publish answers.
_ANSWER_WORD = re.compile(r"(^|[/_.-])(how-?to|how-(do|does|can|long|much|many)|what-(is|are|does)|why-(is|are|do|does)|"
                          r"ways-to|recipes?|reviews?|reviewed|best|top-\d+|vs|versus|guides?|tutorials?|tips|faqs?|fix|diy)"
                          r"([/_.-]|$)")
_LISTING = re.compile(r"(^|/)(category|categories|cat|topic|topics|archive|archives|page|pages|tagged|label|labels|browse|"
                      r"index|users?|profiles?|members?|authors|contributors?|collections?|search-results|print|amp|"
                      r"comments?|replies|attachment|gallery|galleries|videos?|photos?|events?|jobs|careers|about|contact|"
                      r"press|newsletter|subscribe|shop|store|products?|\d{4}(/\d{1,2}){0,2})(/|$)")
_PAGE_EXT = re.compile(r"\.(html?|php|aspx?)$")


def answer_score(path: str) -> int:
    """/how-to-freeze-bananas -> 5, /recipes -> 3, /category/desserts -> -3, /node/202 -> -2."""
    p = _PAGE_EXT.sub("", path.lower()).rstrip("/")
    words = [w for w in re.split(r"[-_]+", p.rsplit("/", 1)[-1]) if w.isalpha() and len(w) > 1]
    s = 0
    if _ANSWER_WORD.search(p):
        s += 3
    if len(words) >= 3:      # a slug of three words or more is an article, not a section
        s += 2
    elif _LISTING.search(p):
        s -= 3
    if not words:            # an id, a date or a page number
        s -= 2
    return s


def pick_answer(rows, budget: int) -> list[tuple]:
    """Index rows (domain, host, path, ...) -> each domain's best `budget` by answer_score, then the shortest
    path, with the score appended. Takes any iterable and holds budget rows per domain, never a whole site
    (the index has millions of rows for some)."""
    by: dict[str, list] = {}
    for r in rows:
        key = (answer_score(r[2]), -len(r[2]), r[1], r[2])
        heap = by.setdefault(r[0], [])
        if len(heap) < budget:
            heapq.heappush(heap, (key, r))
        elif key > heap[0][0]:
            heapq.heapreplace(heap, (key, r))
    return [(*r, key[0]) for d in sorted(by) for key, r in sorted(by[d], reverse=True)]


def answer_sites(conn, groups=None) -> dict[str, str]:
    """Not-blocked sites the kinds data calls answer sites -> their group. No rank is read: the kinds are
    the evidence, and Tranco must not choose what is crawled for them."""
    by: dict[str, list] = {}
    for dom, key, conf in conn.execute(
            "SELECT m.domain, m.key, m.conf FROM kind_members m JOIN sites s ON s.domain = m.domain "
            "WHERE s.blocked_by IS NULL AND m.key = ANY(%s)", (sorted(ANSWER_KINDS),)):
        by.setdefault(dom, []).append((key, conf))
    out = {d: answer_group(k, groups) for d, k in by.items()}
    return {d: g for d, g in out.items() if g}


# Candidates on the answer sites, uncapped: pick_answer() chooses among them. Wider than PAGES_SQL where an
# article needs it (four path levels, 120 characters: /recipe/12345/banana-bread-with-walnuts is three
# and long) and stricter on language: English must be the page's main language, its words are what is kept.
ANSWER_SQL = f"""SELECT d, h, p, pr, l, wf, wo, wl
        FROM j WHERE s = 200 AND m = 'text/html' AND q IS NULL AND l LIKE 'eng%'
          AND d IN (SELECT domain FROM answer_d)
          AND p <> '/' AND length(p) <= 120
          AND NOT regexp_matches(p, '^(/[^/]+){{5,}}')
          AND NOT regexp_matches(lower(p), '{SKIP_PATH}')"""


# ---------------------------------------------------------------------------------------------
# Government sites. Which sites: United States federal, state and local government under .gov, and
# the older state.<xx>.us names. A state's application portal on a commercial domain (njportal.com) is
# not found by its name and is left to a list from the kinds data, not guessed here.
_GOV_DOMAIN = re.compile(r"\.gov$|^state\.[a-z]{2}\.us$")
# ... and since step 2 the states' own sites beyond .gov, listed by hand in release/gov-state-domains.tsv
# (ctparks.com, idmedicaid.com, myflorida.com): the same list gov_states.py builds into the release, so the
# selection, the engine's state rule and the grader agree on whose page it is.
_EXTRA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "release", "gov-state-domains.tsv")
_extra_domains: dict[str, str] | None = None


def listed_gov_domains(path: str = _EXTRA_PATH) -> dict[str, str]:
    """{domain: STATE} from gov-state-domains.tsv; {} when the file is not there (an older checkout)."""
    global _extra_domains
    if _extra_domains is None or path != _EXTRA_PATH:
        out: dict[str, str] = {}
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as f:
                for line in f:
                    if line.strip() and not line.lstrip().startswith("#"):
                        parts = line.rstrip("\n").split("\t")
                        if len(parts) >= 2:
                            out[parts[1].strip().lower()] = parts[0].strip()
        if path != _EXTRA_PATH:
            return out
        _extra_domains = out
    return _extra_domains


def gov_domain(domain: str) -> bool:
    """nj.gov, irs.gov, state.nj.us -> True, and a listed state site (ctparks.com); gov.uk, governor.com -> False."""
    return bool(_GOV_DOMAIN.search(domain or "")) or (domain or "").lower() in listed_gov_domains()


# What a URL alone says about a page being where a resident gets something done. Not yet checked against
# titles (the answer-site score was, on rc6): ccgov-scan's file is what that check reads. "services" is
# not a service word: it names divisions (/njsp/marine-services/view-our-fleet scored 3, 2026-10-12).
_GOV_SERVICE = re.compile(r"(^|[/_.-])(apply|applying|applications?|renew|renewals?|renewing|replace|replacement|"
                          r"licen[cs]es?|licensing|permits?|register|registration|forms?|faqs?|how-?to|how-do-i|"
                          r"eligibility|requirements?|fees?|appointments?|benefits|certificates?|records|"
                          r"id|identification|cards?|pay|payments?|file|filing|claims?|complaints?|request)([/_.-]|$)")
_GOV_NEWS = re.compile(r"(^|/)(news|newsroom|press|press-?releases?|releases?|media|events?|calendar|meetings?|minutes|"
                       r"agendas?|archives?|blog|speeches|testimony|budget|bids|rfps?|procurement|jobs|careers|"
                       r"employment|about|contact|staff|directory|photos?|videos?|gallery|\d{4}(/\d{1,2}){0,2})(/|$)")
_GOV_PAGE_EXT = re.compile(r"\.(s?html?|php|aspx?|cfm|jsp)$")
_GOV_INDEX = re.compile(r"/(index|default|home)$")


def gov_score(path: str) -> int:
    """/njsp/firearms/firearms-faqs.shtml -> 3, /njsp/firearms/index.shtml -> 0, /njsp/news/2024/ -> -5."""
    p = _GOV_INDEX.sub("", _GOV_PAGE_EXT.sub("", path.lower()).rstrip("/"))
    words = [w for w in re.split(r"[-_]+", p.rsplit("/", 1)[-1]) if w.isalpha() and len(w) > 1]
    s = 0
    if _GOV_SERVICE.search(p):
        s += 3
    if _GOV_NEWS.search(p):
        s -= 3
    if not words:            # an id, a date or a page number
        s -= 2
    return s


def gov_unit(host: str, path: str) -> tuple[str, str]:
    """An agency: its host and its first path level. (www.nj.gov, /njsp/firearms/x) -> (www.nj.gov, njsp);
    a page at the top of a host belongs to the host itself: (dmv.ca.gov, /renewal) -> (dmv.ca.gov, "").
    www.nj.gov and nj.gov are one host: the crawl holds both, and they split /njsp in two (2026-10-12)."""
    parts = path.strip("/").split("/")
    return host.removeprefix("www."), (parts[0] if len(parts) > 1 else "")


def pick_gov(rows, budget: int) -> list[tuple]:
    """Index rows (domain, host, path, ...) -> each agency's best `budget` by gov_score, then the shortest
    path, with the score appended. The cap for a whole site is cut after every index file is read."""
    by: dict[tuple, list] = {}
    for r in rows:
        key = (gov_score(r[2]), -len(r[2]), r[1], r[2])
        heap = by.setdefault((r[0], *gov_unit(r[1], r[2])), [])
        if len(heap) < budget:
            heapq.heappush(heap, (key, r))
        elif key > heap[0][0]:
            heapq.heapreplace(heap, (key, r))
    return [(*r, key[0]) for u in sorted(by) for key, r in sorted(by[u], reverse=True)]


def gov_cut(rows, per_unit: int, per_domain: int) -> list[tuple]:
    """cmd_pages' cut for a government site, on rows already best-first inside each agency (ccgov-scan's
    file: domain, host, agency, score, url, ...): each agency's first per_unit, then at most per_domain a
    site, every agency's first choice before any agency's second. A smaller budget's pages are the first of a
    larger one's, so one fetch serves both."""
    by: dict[str, list] = {}
    seen: dict[tuple, int] = {}
    for r in rows:
        unit = (r[0], r[1].removeprefix("www."), r[2])
        k = seen[unit] = seen.get(unit, 0) + 1
        if k <= per_unit:
            by.setdefault(r[0], []).append(((k, -int(r[3]), len(r[4]), r[4]), r))
    out = []
    for d in sorted(by):
        out += [r for _key, r in sorted(by[d])[:per_domain or None]]
    return out


def gov_sites(conn) -> list[str]:
    """Not-blocked government sites. No rank is read: the name is the evidence. A listed state site is one even when
    the sites table never saw it (43 of 122 on 2026-10-28, agfc.com among them), unless the table blocks it."""
    listed = sorted(listed_gov_domains())
    known = dict(conn.execute(
        "SELECT domain, blocked_by IS NULL FROM sites WHERE domain LIKE %s OR domain LIKE %s OR domain = ANY(%s)",
        ("%.gov", "state.__.us", listed)).fetchall())
    return sorted({d for d, ok in known.items() if ok and gov_domain(d)} | {d for d in listed if d not in known})


# Candidates on the government sites, uncapped: pick_gov() chooses among them. The answer sites' limits.
GOV_SQL = f"""SELECT d, h, p, pr, l, wf, wo, wl
        FROM j WHERE s = 200 AND m = 'text/html' AND q IS NULL AND l LIKE 'eng%'
          AND d IN (SELECT domain FROM gov_d)
          AND p <> '/' AND length(p) <= 120
          AND NOT regexp_matches(p, '^(/[^/]+){{5,}}')
          AND NOT regexp_matches(lower(p), '{SKIP_PATH}')"""

# A government site's front page, for the scan's file only (cmd_gov_scan): linkcrawl.py starts each site from it and
# follows the site's own links. Never a page of the release by itself (the site row is the front page).
GOV_ROOT_SQL = """SELECT d, h, p, pr, l, wf, wo, wl
        FROM j WHERE s = 200 AND m = 'text/html' AND q IS NULL AND d IN (SELECT domain FROM gov_d) AND p = '/'
          AND (l LIKE 'eng%' OR coalesce(l, '') = '')"""
GOV_ROOT_SCORE = 99

# gov_unit's agency in SQL, for the cut across index files.
GOV_UNIT_SQL = "CASE WHEN path ~ '^/[^/]+/.' THEN split_part(path, '/', 2) ELSE '' END"

# The index files that can hold a government site: the files are sorted by reversed host name
# (gov,nj)/..., us,nj,state)/...), so two short ranges out of the whole crawl. Compared byte by byte: the
# database's own collation skips the commas, and found 2 of the 5 files (97 of 1,918 sites, 2026-10-12).
GOV_FILES_SQL = ("SELECT path FROM cc_index_files WHERE crawl = %s AND subset = 'warc' "
                 "AND ((surt_max COLLATE \"C\" >= 'gov,' AND surt_min COLLATE \"C\" < 'gov-') "
                 "OR (surt_max COLLATE \"C\" >= 'us,' AND surt_min COLLATE \"C\" < 'us-')) "
                 "ORDER BY surt_min")


def surt_range(domain: str) -> tuple[str, str]:
    """agfc.com -> ("com,agfc)", "com,agfc-"): the index keys of the site and its subdomains ("com,agfc,www)/…") lie
    between the two, compared byte by byte (")" < "," < "-")."""
    key = ",".join(reversed(domain.lower().split(".")))
    return key + ")", key + "-"


def gov_files(conn, crawl: str) -> list[str]:
    """GOV_FILES_SQL's files plus the ones holding a listed state site beyond .gov (gov-state-domains.tsv step 2):
    agfc.com sorts under "com," and was never read by a government-only cut."""
    files = [r[0] for r in conn.execute(GOV_FILES_SQL, (crawl,))]
    listed = sorted(listed_gov_domains())
    if listed:
        lo, hi = zip(*(surt_range(d) for d in listed))
        more = conn.execute(
            "SELECT DISTINCT f.path, f.surt_min FROM cc_index_files f, unnest(%s::text[], %s::text[]) AS r(lo, hi) "
            "WHERE f.crawl = %s AND f.subset = 'warc' AND f.surt_max COLLATE \"C\" >= r.lo AND f.surt_min COLLATE \"C\" < r.hi "
            "ORDER BY f.surt_min", (list(lo), list(hi), crawl)).fetchall()
        files += [m[0] for m in more if m[0] not in files]
    return files


# The front page of each kept subdomain host (tranco_hosts): its root, else an
# obvious English landing path. One per host.
HOSTS_SQL = """SELECT d, h, p, pr, l, wf, wo, wl
        FROM j WHERE s = 200 AND m = 'text/html' AND q IS NULL AND l LIKE '%eng%'
          AND h IN (SELECT host FROM hostset)
          AND p IN ('/', '/index.html', '/en', '/en/', '/en-us', '/en-us/', '/home', '/home/')
        QUALIFY row_number() OVER (PARTITION BY h ORDER BY (p = '/') DESC, (pr = 'https') DESC, p) = 1"""


def _table(d, name: str, col: str, values: list[str]) -> None:
    """One-column DuckDB table from a list, via a temp file (row inserts are slow)."""
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
        f.write("".join(v + "\n" for v in values))
    try:
        d.execute(f"CREATE OR REPLACE TABLE {name} AS SELECT * FROM read_csv('{f.name}', header = false, "
                  f"delim = '\t', quote = '', columns = {{'{col}': 'VARCHAR'}})")
    finally:
        os.unlink(f.name)


def _answer_rows(cur, budget: int) -> list[tuple]:
    """ANSWER_SQL's cursor, read in batches -> pick_answer's rows as stage rows (kind 'answer', score last)."""
    return [(*r[:-1], "answer", r[-1]) for r in pick_answer(_batches(cur), budget)]


def _batches(cur):
    while True:
        chunk = cur.fetchmany(50000)
        if not chunk:
            return
        yield from chunk


def _gov_rows(cur, budget: int) -> list[tuple]:
    """GOV_SQL's cursor, read in batches -> pick_gov's rows as stage rows (kind 'gov', score last)."""
    return [(*r[:-1], "gov", r[-1]) for r in pick_gov(_batches(cur), budget)]


def _scan_file(d, src: str, per: int, answer_per: int = 0, gov_per: int = 0, roots: bool = False) -> list[tuple]:
    d.execute(f"""CREATE OR REPLACE TEMP TABLE j AS
        SELECT x.url_host_registered_domain d, x.url_host_name h, x.url_path p, x.url_query q,
               x.url_protocol pr, x.fetch_status s, x.content_mime_detected m,
               x.content_languages l, x.warc_filename wf, x.warc_record_offset wo, x.warc_record_length wl
        FROM read_parquet('{src}') x SEMI JOIN ours o ON x.url_host_registered_domain = o.domain""")
    rows = [(*r, "page", 0) for r in d.execute(PAGES_SQL, {"per": per}).fetchall()]
    rows += [(*r, "host", 0) for r in d.execute(HOSTS_SQL).fetchall()]
    if answer_per:
        rows += _answer_rows(d.execute(ANSWER_SQL), answer_per)
    if gov_per:
        rows += _gov_rows(d.execute(GOV_SQL), gov_per)
    if roots:
        rows += [(*r, "root", GOV_ROOT_SCORE) for r in d.execute(GOV_ROOT_SQL).fetchall()]
    d.execute("DROP TABLE j")
    return rows


def cmd_pages(conn, crawl: str, top: int, per: int, hosts: int = 0, answer_per: int = 0, answer_groups=None,
              gov_per: int = 0, gov_per_domain: int = 0) -> None:
    t0 = time.time()
    # A government-only cut (no top sites, no hosts, no answer sites) reads only the index files that can hold a
    # government site (GOV_FILES_SQL, as cmd_gov_scan does): 5 of 300 files instead of the whole crawl's index.
    gov_only = bool(gov_per) and not top and not hosts and not answer_per
    files = (gov_files(conn, crawl) if gov_only else [r[0] for r in conn.execute(
        "SELECT path FROM cc_index_files WHERE crawl = %s AND subset = 'warc' ORDER BY surt_min", (crawl,))])
    if not files:
        raise SystemExit(f"no mapped warc index files for {crawl} — run ccmap first")
    if gov_only:
        log(f"pages: government-only cut, {len(files)} index files that can hold a .gov or state.xx.us site")
    d = _duck()
    doms = [r[0] for r in conn.execute(
        "SELECT domain FROM sites WHERE blocked_by IS NULL AND tranco_rank IS NOT NULL "
        "ORDER BY tranco_rank LIMIT %s", (top,))]
    hs = conn.execute("SELECT host, domain FROM tranco_hosts ORDER BY rank LIMIT %s", (hosts,)).fetchall() if hosts else []
    _table(d, "top_d", "domain", doms)
    _table(d, "hostset", "host", [h for h, _ in hs])
    ans = answer_sites(conn, answer_groups) if answer_per else {}
    _table(d, "answer_d", "domain", sorted(ans))
    gov = gov_sites(conn) if gov_per else []
    _table(d, "gov_d", "domain", gov)
    # The index is filtered by registrable domain first: the top domains plus the
    # domains of the kept hosts, the answer sites and the government sites.
    _table(d, "ours", "domain", sorted(set(doms) | {dm for _, dm in hs} | set(ans) | set(gov)))
    conn.execute("DELETE FROM cc_pages WHERE crawl = %s", (crawl,))
    total = 0
    for i, path in enumerate(files, 1):
        rows = _scan_file(d, archived(d, path, "warc"), per, answer_per, gov_per)
        with conn.transaction(), conn.cursor().copy(
                "COPY cc_pages_stage (domain, host, path, protocol, languages, warc_filename, warc_offset, "
                "warc_length, kind, score) FROM STDIN") as cp:
            for r in rows:
                cp.write_row(r)
        total += len(rows)
        if i % 25 == 0 or i == len(files):
            log(f"pages: {i}/{len(files)} files, {total:,} candidate pages, {(time.time() - t0) / 60:.1f} min")
    # A domain can straddle files: keep its per shortest across all of them, and an answer site's best
    # answer_per by score (page and host rows score 0, so their order is the shortest path as before;
    # a URL picked both ways stays a 'page': kind DESC).
    with conn.transaction():
        conn.execute(
            "INSERT INTO cc_pages (url, domain, crawl, host, path, protocol, languages, warc_filename, "
            "warc_offset, warc_length, kind) "
            "SELECT protocol || '://' || host || path, domain, %s, host, path, protocol, languages, "
            "       warc_filename, warc_offset, warc_length, kind FROM ("
            "  SELECT *, row_number() OVER (PARTITION BY kind, CASE WHEN kind = 'host' THEN host ELSE domain END "
            "                               ORDER BY score DESC, length(path), host, path) AS rn "
            "  FROM (SELECT DISTINCT ON (host, path) * FROM cc_pages_stage ORDER BY host, path, kind DESC) u) x "
            "WHERE kind <> 'gov' AND rn <= CASE WHEN kind = 'host' THEN 1 WHEN kind = 'answer' THEN %s ELSE %s END "
            "ON CONFLICT (url) DO NOTHING", (crawl, answer_per, per))
        # A government site: each agency's best gov_per (GOV_UNIT_SQL is gov_unit), then the site's cap, every
        # agency's first choice before any agency's second.
        conn.execute(
            "INSERT INTO cc_pages (url, domain, crawl, host, path, protocol, languages, warc_filename, "
            "warc_offset, warc_length, kind) "
            "SELECT protocol || '://' || host || path, domain, %s, host, path, protocol, languages, "
            "       warc_filename, warc_offset, warc_length, kind FROM ("
            "  SELECT *, row_number() OVER (PARTITION BY domain ORDER BY rn_unit, score DESC, length(path), host, path) AS rn "
            "  FROM (SELECT *, row_number() OVER (PARTITION BY regexp_replace(host, '^www\\.', ''), " + GOV_UNIT_SQL + " "
            "                                     ORDER BY score DESC, length(path), host, path) AS rn_unit "
            "        FROM (SELECT DISTINCT ON (host, path) * FROM cc_pages_stage ORDER BY host, path, kind DESC) u "
            "        WHERE kind = 'gov') v WHERE rn_unit <= %s) x "
            "WHERE rn <= %s ON CONFLICT (url) DO NOTHING", (crawl, gov_per, gov_per_domain or 2147483647))
        conn.execute("TRUNCATE cc_pages_stage")
    n = conn.execute("SELECT count(*) FILTER (WHERE kind = 'page'), count(DISTINCT domain) FILTER (WHERE kind = 'page'), "
                     "count(*) FILTER (WHERE kind = 'host'), count(*) FILTER (WHERE kind = 'answer'), "
                     "count(DISTINCT domain) FILTER (WHERE kind = 'answer'), count(*) FILTER (WHERE kind = 'gov'), "
                     "count(DISTINCT domain) FILTER (WHERE kind = 'gov') FROM cc_pages WHERE crawl = %s", (crawl,)).fetchone()
    log(f"cc_pages: {n[0]:,} pages on {n[1]:,} of the top {top:,} domains + {n[2]:,} of {len(hs):,} subdomain "
        f"front pages + {n[3]:,} answer pages on {n[4]:,} of {len(ans):,} answer sites + {n[5]:,} government pages on "
        f"{n[6]:,} of {len(gov):,} government sites in {(time.time() - t0) / 60:.1f} min")


def cmd_gov_scan(conn, crawl: str, per_unit: int, out: str, roots: bool = False) -> None:
    """The government candidates of one crawl -> a tab-separated file (domain, host, agency, score, url, then
    where the capture is: languages, warc file, offset, length), each agency's best per_unit, best first. Reads the database (the mapped index files, the not-blocked sites) and writes
    nothing to it: what a budget would fetch is counted from the file before anything is fetched."""
    t0 = time.time()
    files = gov_files(conn, crawl)
    if not files:
        raise SystemExit(f"no mapped warc index files for {crawl} — run ccmap first")
    d = _duck()
    gov = gov_sites(conn)
    _table(d, "gov_d", "domain", gov)
    _table(d, "ours", "domain", gov)
    for name in ("top_d", "hostset", "answer_d"):
        _table(d, name, "domain" if name != "hostset" else "host", [])
    best: dict[tuple, list] = {}
    for i, path in enumerate(files, 1):
        for r in _scan_file(d, archived(d, path, "warc"), 0, 0, per_unit, roots):
            if r[8] in ("gov", "root"):               # a front page is its host's, scored GOV_ROOT_SCORE: first
                best.setdefault((r[0], *gov_unit(r[1], r[2])), []).append(r)
        log(f"gov scan: {i}/{len(files)} files, {sum(map(len, best.values())):,} candidates, {(time.time() - t0) / 60:.1f} min")
    n = 0
    with open(out, "w") as f:
        for unit in sorted(best):
            # an agency can straddle two index files: its best per_unit across them, in pick_gov's order
            rows = sorted(best[unit], key=lambda r: (-r[9], len(r[2]), r[1], r[2]))[:per_unit]
            for r in rows:
                f.write("\t".join((r[0], r[1], unit[2], str(r[9]), f"{r[3]}://{r[1]}{r[2]}",
                                   r[4] or "", r[5], str(r[6]), str(r[7]))) + "\n")
            n += len(rows)
    log(f"gov scan: {n:,} candidate pages in {len(best):,} agencies on {len({u[0] for u in best}):,} of {len(gov):,} "
        f"government sites -> {out} in {(time.time() - t0) / 60:.1f} min")

