#!/usr/bin/env python3
"""Our own site-popularity rank (G3 D2): site.own_rank, from open sources only.

    own_rank.py fetch-cc   --release R --domains-out F --hosts-out F [--domain-top N] [--host-top M] [--keep-from DIR]
    own_rank.py blocklist  --out blocked.tsv            (the importer's public block lists -> domain TAB list)
    own_rank.py export     --dsn DSN --dir D            (BRIDGE: reads the serving/build Postgres, writes TSV only)
    own_rank.py compute    --dir D --out own_rank.tsv [--blocked blocked.tsv]   (the rank; pure, deterministic)

Files in, files out: nothing here creates a Postgres table or column. `export` is the one read of Postgres (the
open inputs live there today) and is meant to disappear when those inputs are staged as files. own_rank.tsv is
host TAB domain TAB own_rank TAB cc_pos TAB lists; the lookup builder (sqlite_lookups/build.py --own-rank) stages it
into SQLite and release_v1.py (registry, signals) reads it as a file.

THE RECIPE (docs/legal/SEARCH-DATA-LICENSES.md 2.6, docs/internal/SEARCH-RELEASE-FORMAT.md):

  universe  every registrable domain of the Common Crawl web graph's top DOMAIN_TOP by harmonic
            centrality, plus every served site (serve_sites) and every official site of a Wikipedia
            article (wiki_sites); plus the subdomain hosts among the graph's top HOST_TOP host ranks whose
            registrable domain is in that set and that are not plumbing (ccpages.infra_host).
  lists     up to four ranked lists, each a rank 1..n over the universe hosts that have the signal. A host
            enters the pv, sl and in lists only if it also has a Common Crawl position: a domain the web
            graph has never seen is no destination, however famous the article that names it (a fan page
            that is the "official site" of a very popular article took rank 223 without this rule):
              cc  Common Crawl harmonic-centrality position (host rank for a subdomain, domain rank for a
                  registrable domain)                                     [Common Crawl terms of use]
              pv  monthly Wikipedia pageviews of the article whose official site it is      [CC0]
              sl  Wikidata sitelinks of that article                                         [CC0]
              in  distinct sites linking to it in the pages we parsed (site_inlinks)        [CC-derived]
                  OFF by default (build-only table, G3 D10): compute --with-inlinks turns it on
            pv and sl count only an ORGANISATION article (ORG_BOX: the infobox types search_api._ORG_BOX and
            suggest.sql already call "the article's subject IS the site"). The official site of "Italy",
            "Cadiz" or "Snoop Dogg" is a tourism, city or fan site that borrows the fame of a place or a
            person (suggest.sql: "gov.br took Brazil's fame and outranked Google"). Recipe v2; v1 counted
            every article and measured -1.5 pt on wiki-redirect (docs/internal/audits/2026-10-04-own-rank.md).
            And only when the article's official site is a FRONT PAGE (official_key, suggest.sql's own rule):
            the Tomsk administration's site tomsk.gov.ru has the registrable domain gov.ru, and every article
            about a Russian region lifted gov.ru to rank 10. Recipe v3. A front page of a HOST under the
            domain (tomsk.gov.ru, a court under uscourts.gov) lifts that host, not the domain, and every such
            official host that has a Common Crawl host rank, at any position, joins the universe: recipe v4.
            Recipe v6 (default): such a host also takes its PARENT domain's rank (or its own if better), as Tranco's
            domain-level rank did for every site under the domain (inherit_parent_rank).
            Equal values share the best rank of their group (competition ranking).
  fusion    reciprocal-rank fusion, score = sum over lists of 1 / (K + rank), K = 60 (the textbook value,
            no weights, nothing fitted to a label set).
  own_rank  the 1-based position of the host when sorted by score descending, then host ascending
            (COLLATE "C"). Same direction as Tranco's rank: 1 is the most popular, 0 (NULL) means no
            signal at all. The scale is a dense rank over our own universe, so the popularity formulas
            written for a Tranco-sized scale (14 - ln(rank), CONFIDENT_RANK) keep their meaning.

Nothing here reads, derives from, or is tuned on the Tranco list, Jev, or browser history.
"""
import argparse
import csv
import gzip
import os
import re
import sys
import urllib.request

K_RRF = 60
DOMAIN_TOP = 1_000_000
HOST_TOP = 1_000_000
CC_BASE = "https://data.commoncrawl.org/projects/hyperlinkgraph"
USER_AGENT = "logout-search-import/1 (personal search index)"
LISTS = ("cc", "pv", "sl", "in")
# search_api._ORG_BOX, verbatim: an infobox type whose article IS the organisation its official site belongs to
ORG_BOX = re.compile(r"^(?!.*\b(season|biography|match|competition|tournament|championship)\b)"
                     r"(?=.*(company|website|organi[sz]ation|airline|bank|retail|agency|software|brand|"
                     r"service|channel|network|university|college|school|club|restaurant|store|"
                     r"magazine|newspaper|government|political party|non-profit|charity|"
                     r"\bteam\b|\bmlb\b|\bnhl\b|\bnba\b|\bnfl\b|\bmls\b|minor league|\bleague\b))")


def log(msg):
    print(f"[own-rank] {msg}", file=sys.stderr, flush=True)


def competition_ranks(values):
    """{host: value} (bigger = better) -> {host: rank}; ties share the best rank of their group."""
    out, rank, prev, n = {}, 0, None, 0
    for h, v in sorted(values.items(), key=lambda kv: (-kv[1], kv[0])):
        n += 1
        if v != prev:
            rank, prev = n, v
        out[h] = rank
    return out


# ORG_BOX_WIDE: the same plus the infobox types of a court, a museum and an officeholder (recipe v6 candidate; whether
# it ships is decided on the dev halves, see the audit)
ORG_BOX_WIDE = re.compile(ORG_BOX.pattern.replace("(company|", "(company|court|museum|officeholder|", 1))


def fuse(universe, cc, pageviews, sitelinks, inlinks, k=K_RRF):
    """universe: iterable of (host, domain); cc: {host: position, lower = better};
    pageviews / sitelinks / inlinks: {domain: n}. Returns [(host, domain, own_rank, basis)] by own_rank."""
    uni = sorted(set(universe))
    in_uni = {h for h, _d in uni}
    # a list holds only universe hosts that have the signal; the wiki and inlink signals describe a site
    # (its bare domain), never one of its subdomains
    cc_v = {h: -p for h, p in cc.items() if h in in_uni and p > 0}          # position -> bigger = better
    own_dom = {h: d for h, d in uni if h == d and h in cc_v}                # the other lists need a graph position
    pv_v = {h: pageviews[h] for h in own_dom if pageviews.get(h, 0) > 0}
    sl_v = {h: sitelinks[h] for h in own_dom if sitelinks.get(h, 0) > 0}
    in_v = {h: inlinks[h] for h in own_dom if inlinks.get(h, 0) > 0}
    ranked = {"cc": competition_ranks(cc_v), "pv": competition_ranks(pv_v),
              "sl": competition_ranks(sl_v), "in": competition_ranks(in_v)}
    scored = []
    for h, d in uni:
        score, basis = 0.0, []
        for name in LISTS:               # fixed order: the float sum is the same on every run
            r = ranked[name].get(h)
            if r is not None:
                score += 1.0 / (k + r)
                basis.append(name)
        if basis:
            scored.append((-score, h, d, ",".join(basis)))
    scored.sort(key=lambda t: (t[0], t[1].encode("utf-8")))
    return [(h, d, i, b) for i, (_s, h, d, b) in enumerate(scored, 1)]


# --------------------------------------------------------------------------- Common Crawl web graph

RETRY_WAITS = (30, 60, 120, 240, 480)      # Common Crawl answers 503 "Slow Down" to a third full download in an hour


def _open_url(url, sleep=None):
    """urlopen that waits and tries again on 429 / 503 (Retry-After when given), five times."""
    import time
    import urllib.error
    for wait in RETRY_WAITS + (None,):
        try:
            return urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": USER_AGENT}), timeout=300)
        except urllib.error.HTTPError as e:
            if e.code not in (429, 503) or wait is None:
                raise
            after = e.headers.get("Retry-After") if e.headers else None
            wait = int(after) if after and after.isdigit() else wait
            log(f"HTTP {e.code} from {url.split('/')[2]}: waiting {wait} s")
            (sleep or time.sleep)(wait)


def _stream_gz(url):
    with _open_url(url) as r, gzip.GzipFile(fileobj=r) as gz:
        for raw in gz:
            yield raw.decode("utf-8", "replace").rstrip("\n")


def wanted_names(d):
    """(domains, hosts) whose graph position the rank needs at ANY depth: every served site and every article's
    official site (a universe member with no position enters no list, so its article's fame is lost), and every
    official host under a domain (recipe v4). From served.tsv and wiki_sites.tsv in d (own_rank.py export)."""
    domains = set()
    p = os.path.join(d, "served.tsv")
    if os.path.exists(p):
        domains |= {ln.strip() for ln in open(p) if ln.strip()}
    p = os.path.join(d, "wiki_sites.tsv")
    if os.path.exists(p):
        domains |= {f[1] for f in (ln.rstrip("\n").split("\t") for ln in open(p)) if len(f) > 1 and f[1]}
    hosts = set(official_hosts(d)[0])
    return frozenset(domains), frozenset(hosts | {"www." + h for h in hosts})      # build_universe reads either form


def fetch_cc(release, domains_out, hosts_out, domain_top=DOMAIN_TOP, host_top=HOST_TOP, keep_domains=frozenset(), keep_hosts=frozenset()):
    """The best-first head of the graph's domain and host rank files: one GET each, stopped early
    (the files are sorted by harmonic centrality, so the first lines are the top). With keep_* (--keep-from DIR)
    the whole file is read instead and the lines of those names are kept too, whatever their position: rc4's rank
    was built that way (a pass over the 4.7 GB host file, audit 2026-10-04), and a rank from the head alone has
    680,000 fewer hosts and half the pageview list (rc5 step 0, first run 2026-10-07)."""
    for kind, out, top, keep in (("domain", domains_out, domain_top, keep_domains), ("host", hosts_out, host_top, keep_hosts)):
        n = kept = 0
        final, out = out, out + ".part"         # a failed download must not leave an empty or half rank file behind
        with open(out, "w") as o:
            for line in _stream_gz(f"{CC_BASE}/{release}/{kind}/{release}-{kind}-ranks.txt.gz"):
                if line.startswith("#"):
                    continue
                f = line.split("\t")
                if len(f) < 5:
                    continue
                n += 1
                name = ".".join(reversed(f[4].split(".")))
                if n <= top:
                    o.write(f"{name}\t{int(f[0])}\n")
                elif name in keep:
                    kept += 1
                    o.write(f"{name}\t{int(f[0])}\n")
                if n >= top and not keep:
                    break
        os.replace(out, final)
        out = final
        log(f"{kind} ranks: {min(n, top):,} head lines" + (f" + {kept:,} of {len(keep):,} asked names below them ({n:,} lines read)" if keep else "") + f" -> {out}")


def official_key(url, domain):
    """The site an article's official URL stands for, as an own_rank host: the registrable `domain` when the URL is
    the front page of the domain itself (http(s)://domain/ or www.domain); the HOST (a leading www. dropped) when it
    is the front page of a host under the domain (www.tomsk.gov.ru under gov.ru, a court under uscourts.gov): that
    site has its own rank, the shared domain's is nobody's. A deep link (/people/...), a port, credentials or a host
    outside the domain stand for nothing: None."""
    m = re.fullmatch(r"https?://([^/?#:@]+)/?", (url or "").strip().lower())
    if not m or not domain:
        return None
    h = m.group(1)
    if h in (domain, "www." + domain):
        return domain
    if h.startswith("www.") and h[4:].endswith("." + domain):
        h = h[4:]
    return h if h.endswith("." + domain) else None


def blocklist(out):
    """The importer's public block lists (search_import.BLOCKLISTS: hagezi tif/pro/hoster/nsfw/gambling, nocoin,
    windows-spy) -> `domain TAB list`. Only an entry naming a registrable domain itself blocks it (a list entry
    for ads.x.com says nothing about x.com), exactly as search_import.cmd_blocklists."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import search_import as si
    marks = {}
    for name, url in si.BLOCKLISTS:
        try:
            entries = si.parse_blocklist(si.fetch(url).decode("utf-8", "replace"))
        except Exception as e:  # one dead list must not unblock everything else
            sys.exit(f"blocklist {name} ({url}) failed to download: {e} - nothing written")
        for d in entries:
            marks.setdefault(d, name)
        log(f"{name}: {len(entries):,} entries")
    with open(out, "w") as o:
        for d in sorted(marks):
            o.write(f"{d}\t{marks[d]}\n")
    log(f"{len(marks):,} blocked domains -> {out}")


def nav_keys(host, domain):
    """The names a host answers to: a registrable domain its first label ("costco"); a subdomain host the labels
    run together both ways ("tv.youtube.com" -> youtubetv, tvyoutube). The rule of serve-prep.sql's nav_sites."""
    dl = domain.split(".")[0]
    if host == domain:
        return [dl]
    sub = host[: max(0, len(host) - len(domain) - 1)].replace(".", "")
    return [sub + dl, dl + sub]


# --------------------------------------------------------------------------- inputs and outputs

EXPORT_SQL = {
    # the universe's non-graph members and the wiki / inlink signals
    "served.tsv": "SELECT domain FROM serve_sites ORDER BY domain COLLATE \"C\"",
    "wiki_sites.tsv": "SELECT page_id, domain, coalesce(box, ''), coalesce(url, '') FROM wiki_sites WHERE domain IS NOT NULL ORDER BY page_id",
    "wiki_pop.tsv": "SELECT page_id, coalesce(pageviews, 0), coalesce(sitelinks, 0) FROM wiki_pop ORDER BY page_id",
    "inlinks.tsv": "SELECT domain, sources FROM site_inlinks WHERE sources > 0 ORDER BY domain COLLATE \"C\"",
}


def export_inputs(dsn, d):
    import psycopg
    os.makedirs(d, exist_ok=True)
    with psycopg.connect(dsn) as conn:
        for name, sql in EXPORT_SQL.items():
            have = True
            if name == "wiki_pop.tsv":
                have = conn.execute("SELECT to_regclass('wiki_pop')").fetchone()[0] is not None
            if not have:
                log(f"{name}: no wiki_pop table here; supply {d}/{name} from the build database")
                continue
            with open(os.path.join(d, name), "w") as o, conn.cursor().copy(f"COPY ({sql}) TO STDOUT") as cp:
                for chunk in cp:
                    o.write(bytes(chunk).decode("utf-8"))
            log(f"exported {name}")


def _read_two(path, cast=int):
    out = {}
    with open(path) as f:
        for line in f:
            p = line.rstrip("\n").split("\t")
            if len(p) >= 2 and p[1] not in ("", "\\N"):
                out[p[0]] = cast(p[1])
    return out


_CCPAGES = None


def _plumbing(host, domain):
    """ccpages.infra_host (api.x.com, cdn.x.com, 6.x.com): imported once, on first use."""
    global _CCPAGES
    if _CCPAGES is None:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import ccpages
        _CCPAGES = ccpages
    return _CCPAGES.infra_host(host, domain)


def build_universe(d, registrable, domain_top=DOMAIN_TOP, host_top=HOST_TOP, plumbing=None, blocked=frozenset()):
    """(universe [(host, domain)], cc {host: pos}) from the files in d. `registrable(host)` is the public-suffix
    rule (tldextract's top_domain_under_public_suffix), injected so the tests need no network or package; `plumbing` is
    ccpages.infra_host unless given (importing ccpages needs duckdb, so a test passes its own)."""
    plumbing = plumbing or _plumbing
    dom_pos = _read_two(os.path.join(d, "cc-domains.tsv"))
    host_pos = _read_two(os.path.join(d, "cc-hosts.tsv"))
    domains = {dm for dm, p in dom_pos.items() if p <= domain_top}
    for name in ("served.tsv",):
        p = os.path.join(d, name)
        if os.path.exists(p):
            domains |= {ln.strip() for ln in open(p) if ln.strip()}
    if os.path.exists(os.path.join(d, "wiki_sites.tsv")):
        domains |= {ln.rstrip("\n").split("\t")[1] for ln in open(os.path.join(d, "wiki_sites.tsv"))
                    if "\t" in ln and ln.rstrip("\n").split("\t")[1]}
    domains -= blocked                      # ads, trackers, threats, adult, gambling: never ranked, never navigable
    universe, cc = {(dm, dm) for dm in domains}, {}
    for dm in domains:
        if dm in dom_pos:
            cc[dm] = dom_pos[dm]
    wp = os.path.join(d, "wiki_sites.tsv")
    if os.path.exists(wp):                     # official sites on a host under a shared domain (recipe v4)
        for ln in open(wp):
            f = ln.rstrip("\n").split("\t")
            if len(f) != 4 or f[1] not in domains:
                continue
            h = official_key(f[3], f[1])
            if h is None or h == f[1] or (h, f[1]) in universe:
                continue
            pos = [host_pos[x] for x in (h, "www." + h) if x in host_pos]
            if pos:
                universe.add((h, f[1]))
                cc[h] = min(pos)
    for h, p in host_pos.items():
        if p > host_top:
            continue
        dm = registrable(h)
        if not dm or dm not in domains or h in (dm, "www." + dm) or plumbing(h, dm):
            continue
        universe.add((h, dm))
        cc[h] = p
    return sorted(universe), cc


def wiki_signals(d, box=None):
    """{domain: max pageviews}, {domain: max sitelinks} over the ORGANISATION articles (ORG_BOX) whose official site is
    the front page of the domain (official_key). wiki_sites.tsv is page_id, domain, box, url."""
    pop = {}
    p = os.path.join(d, "wiki_pop.tsv")
    if os.path.exists(p):
        for line in open(p):
            f = line.rstrip("\n").split("\t")
            pop[int(f[0])] = (int(f[1]), int(f[2]))
    pv, sl = {}, {}
    p = os.path.join(d, "wiki_sites.tsv")
    if os.path.exists(p):
        for line in open(p):
            f = line.rstrip("\n").split("\t")
            if len(f) != 4:
                sys.exit(f"{p}: a line is not 'page_id domain box url' (export writes the infobox type and the official url) - refusing")
            key = official_key(f[3], f[1])
            if key is None or not (box or ORG_BOX).search(f[2]) or int(f[0]) not in pop:
                continue
            v, s = pop[int(f[0])]
            pv[key] = max(pv.get(key, 0), v)
            sl[key] = max(sl.get(key, 0), s)
    return pv, sl


def official_hosts(d):
    """({official host: its registrable domain}, {domains that are themselves the official front page of an article}).
    The first: the front pages of hosts UNDER a domain (tomsk.gov.ru under gov.ru, a court under uscourts.gov,
    parks.ca.gov), the official sites of every Wikipedia article. The second: uscourts.gov, house.gov, asu.edu,
    ca.gov (each an article's official site) but not gov.ru (a shared registry nobody's site)."""
    out, parents = {}, set()
    p = os.path.join(d, "wiki_sites.tsv")
    if os.path.exists(p):
        for line in open(p):
            f = line.rstrip("\n").split("\t")
            if len(f) == 4:
                h = official_key(f[3], f[1])
                if h is not None and h != f[1]:
                    out[h] = f[1]
                elif h is not None:
                    parents.add(h)
    return out, parents


def inherit_parent_rank(rows, hosts, parents):
    """A site on a host under a domain stands in its parent's name when the parent is itself somebody's site (uscourts.gov:
    its courts; house.gov: its members): each such official host takes its parent domain's own_rank, or its own rank
    when that is better, whether or not the web graph ranks the host itself. Under a shared domain that is nobody's site
    (gov.ru: every Russian region's government) it keeps only its own rank, else every region would inherit gov.ru's
    rank and lead its own article. A host whose parent has no rank gets none. Ranks are then not unique: the host
    shares its parent's. Basis 'inh' marks a host that has no list of its own."""
    rk = {h: r for h, _d, r, _b in rows}
    have = {h: (dm, r, b) for h, dm, r, b in rows}
    out = {h: (dm, r, b) for h, (dm, r, b) in have.items()}
    for h, dm in hosts.items():
        if dm not in rk or dm not in parents:
            continue
        own = have.get(h)
        r = min(rk[dm], own[1]) if own else rk[dm]
        out[h] = (dm, r, own[2] if own else "inh")
    return sorted(((h, dm, r, b) for h, (dm, r, b) in out.items()), key=lambda t: (t[2], t[0].encode("utf-8")))


def read_blocked(path):
    return frozenset(ln.split("\t")[0] for ln in open(path) if ln.strip()) if path else frozenset()


def compute(d, out, registrable, blocked_file=None, wide=False, inlinks=False, inherit=True):
    universe, cc = build_universe(d, registrable, blocked=read_blocked(blocked_file))
    pv, sl = wiki_signals(d, ORG_BOX_WIDE if wide else None)
    inl = (_read_two(os.path.join(d, "inlinks.tsv")) if inlinks and os.path.exists(os.path.join(d, "inlinks.tsv")) else {})
    rows = fuse(universe, cc, pv, sl, inl)
    if inherit:
        hosts, parents = official_hosts(d)
        rows = inherit_parent_rank(rows, hosts, parents)
    with open(out, "w", newline="") as o:
        w = csv.writer(o, delimiter="\t", lineterminator="\n")
        for h, dm, r, basis in rows:
            w.writerow((h, dm, r, cc.get(h, "\\N"), basis))      # \N: COPY text format's NULL
    by = {n: sum(1 for r in rows if n in r[3].split(",")) for n in LISTS}
    log(f"universe {len(universe):,} hosts; ranked {len(rows):,}; per list {by}")
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1], formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch-cc")
    f.add_argument("--release", required=True)
    f.add_argument("--domains-out", required=True)
    f.add_argument("--hosts-out", required=True)
    f.add_argument("--domain-top", type=int, default=DOMAIN_TOP)
    f.add_argument("--host-top", type=int, default=HOST_TOP)
    f.add_argument("--keep-from", help="DIR with served.tsv / wiki_sites.tsv (export): read the whole files and keep those sites' lines at any position")
    e = sub.add_parser("export")
    e.add_argument("--dsn", required=True)
    e.add_argument("--dir", required=True)
    c = sub.add_parser("compute")
    c.add_argument("--dir", required=True)
    c.add_argument("--out", required=True)
    c.add_argument("--blocked", help="blocked.tsv: domains that never get a rank")
    c.add_argument("--wide-box", action="store_true", help="count court, museum and officeholder articles as organisations too")
    c.add_argument("--no-inlinks", action="store_true", help="leave the inlink list out of the fusion (the default: site_inlinks is build-only, G3 D10)")
    c.add_argument("--with-inlinks", action="store_true", help="fuse the inlink list too; signals refuses such a rank without --allow-inlinks")
    c.add_argument("--no-inherit", action="store_true", help="an official host under a shared domain keeps only its own rank (recipe v4)")
    b = sub.add_parser("blocklist")
    b.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    if a.cmd == "fetch-cc":
        fetch_cc(a.release, a.domains_out, a.hosts_out, a.domain_top, a.host_top, *(wanted_names(a.keep_from) if a.keep_from else ()))
    elif a.cmd == "export":
        export_inputs(a.dsn, a.dir)
    elif a.cmd == "compute":
        import tldextract
        ext = tldextract.TLDExtract(suffix_list_urls=())      # the bundled snapshot, never a network fetch
        compute(a.dir, a.out, lambda h: ext(h).top_domain_under_public_suffix, a.blocked, a.wide_box, a.with_inlinks and not a.no_inlinks, not a.no_inherit)
    elif a.cmd == "blocklist":
        blocklist(a.out)


if __name__ == "__main__":
    main()
