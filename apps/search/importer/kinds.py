"""What KIND of site each site is — for descriptive searches ("password manager", "video
sharing site", "online code editor") that name a kind of site, not a site (follow-up,
2026-09-28). Before this, those searches returned the Wikipedia article about the kind or a
tiny domain that spells the words (usedcars.com, recipes.net).

Sources, each row keeping its `source` so any one can be dropped for licensing:
  wikidata   instance of / has use / industry / genre (P31 P366 P452 P136) of every item with
             an official website and an English article; class labels, aliases and one level
             of parent classes. CC0. QLever, paged, one query at a time.
  wikicat    the article's Wikipedia categories ("Password managers"). CC BY-SA.
  wikilist   "List of … / Comparison of …" pages that link the article. CC BY-SA.
  dmoz       the Open Directory's category path (2017 dump). CC BY 3.0.
  awesome    section headings of permissively licensed awesome lists (CC0/MIT/CC BY …).
Category-like phrases are noisy ("Companies based in Seattle", "2005 establishments").

The OPEN build (`--open`, 2026-10-01, ADR-065 — the one that may ship) keeps or drops them with
rules over the sources themselves (kinds_open.py): a kind name is kept when it is a Wikidata class
that enough sites with an official website are an instance of, or when two independent source
families name it, and it passes the junk rules. Members get a confidence from the same open
evidence, and the lead order from open popularity. No Jev, no Tranco, no WDC, no Common Crawl
anchor text or homepage schema types.

The OLD build (`--phrases FILE`, history only) kept each phrase by an offline Jev verdict
(the deleted utility/search-kind-phrases.py) — none of its output may ship (ADR-065).

    search_import.py kinds-wikidata              -> kw_items, kw_edges, kw_classes
    search_import.py kinds-build --open [--import DIR]
                                                 -> site_kinds, kind_docs (+ BM25), kind_members, kind_keys
    search_import.py kinds-build [--import DIR] [--phrases FILE]     (old, Jev)
"""

from __future__ import annotations

import gzip
import io
import os
import re
import time
import urllib.parse

from wikilinks import fetch as _fetch
from wikisites import normalize

PAGE_GAP_S = 15         # between queries: QLever answered 429 at 5 s (2026-09-25)
BATCH = 2000            # classes per VALUES query

PFX = """PREFIX wd: <http://www.wikidata.org/entity/>
PREFIX wdt: <http://www.wikidata.org/prop/direct/>
PREFIX wikibase: <http://wikiba.se/ontology#>
PREFIX schema: <http://schema.org/>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX skos: <http://www.w3.org/2004/02/skos/core#>
"""
ENWIKI = "?art schema:about ?item ; schema:isPartOf <https://en.wikipedia.org/> ."
PROPS = {"P31": "is", "P366": "use", "P452": "industry", "P136": "genre",
         # 2026-09-29: what a company makes, and where an app runs ("android app", "mac app")
         "P1056": "product", "P400": "platform", "P306": "os"}

# One unpaged query per table, each checked against its own COUNT: QLever truncates big
# answers silently, and ORDER BY + OFFSET paging over the edge join timed out (2026-09-28:
# "Sort operation was canceled"). A numeric FILTER on wikibase:sitelinks returned 0 rows,
# so fame is applied in Postgres, not here.
ITEMS = """SELECT ?item ?art ?site ?sl ?d WHERE {
  ?item wdt:P856 ?site . """ + ENWIKI + """
  OPTIONAL { ?item wikibase:sitelinks ?sl }
  OPTIONAL { ?item schema:description ?d FILTER(LANG(?d) = "en") } }"""
EDGES = """SELECT DISTINCT ?item ?k WHERE { ?item wdt:P856 ?site . """ + ENWIKI + """ ?item wdt:%s ?k . }"""
# Items with an official site but NO English article (2026-09-29): 1.22M exist, mostly obscure
# organisations; only software / websites / online services / apps are taken (82.6k). Their
# kw_items.title is "wd:<qid>" — never an article title, so categories and lists skip them.
APPISH = "wd:Q7397 wd:Q35127 wd:Q1668024 wd:Q620615 wd:Q193424 wd:Q19967801 wd:Q166142"
NOWIKI = ("VALUES ?c { " + APPISH + " } ?item wdt:P856 ?site ; wdt:P31/wdt:P279* ?c . "
          "FILTER NOT EXISTS { ?art schema:about ?item ; schema:isPartOf <https://en.wikipedia.org/> }")
ITEMS_NOWIKI = """SELECT DISTINCT ?item ?site ?sl ?d ?l WHERE { """ + NOWIKI + """
  ?item rdfs:label ?l FILTER(LANG(?l) = "en")
  OPTIONAL { ?item wikibase:sitelinks ?sl }
  OPTIONAL { ?item schema:description ?d FILTER(LANG(?d) = "en") } }"""
EDGES_NOWIKI = """SELECT DISTINCT ?item ?k WHERE { """ + NOWIKI + """ ?item wdt:%s ?k . }"""
CLASSES = """SELECT ?k ?l (GROUP_CONCAT(DISTINCT ?al; SEPARATOR="|") AS ?als) WHERE {
  VALUES ?k { %s }
  ?k rdfs:label ?l FILTER(LANG(?l) = "en")
  OPTIONAL { ?k skos:altLabel ?al FILTER(LANG(?al) = "en") } } GROUP BY ?k ?l"""
PARENTS = """SELECT DISTINCT ?k ?parent WHERE { VALUES ?k { %s } ?k wdt:P279 ?parent . }"""


def log(msg: str) -> None:
    print(f"[kinds] {msg}", flush=True)


def _qid(iri: str) -> str:
    return iri.strip("<>").rsplit("/", 1)[-1]


def _lit(v: str) -> str:
    """'"text"@en' / '"91"^^<…int>' / '91' -> text."""
    v = v.strip()
    if v.startswith('"'):
        v = v[1:v.rfind('"')] if v.rfind('"') > 0 else v[1:]
    return v.replace("\\t", " ").replace("\\n", " ").replace('\\"', '"')


def _title(article_iri: str) -> str | None:
    s = article_iri.strip("<>")
    prefix = "https://en.wikipedia.org/wiki/"
    return urllib.parse.unquote(s[len(prefix):]).replace("_", " ") if s.startswith(prefix) else None


def _rows(name: str, select: str, check: bool = True) -> list[list[str]]:
    """Every row of one query (header skipped), refused if short of the query's COUNT."""
    rows = []
    with _fetch(name, PFX + select) as resp:
        for line in io.TextIOWrapper(resp, encoding="utf-8", errors="replace"):
            if not line.startswith("?"):
                rows.append(line.rstrip("\n").split("\t"))
    if check:
        time.sleep(PAGE_GAP_S)
        with _fetch(name, PFX + f"SELECT (COUNT(*) AS ?n) WHERE {{ {select} }}") as resp:
            want = int(_lit(resp.read().decode().splitlines()[1]))
        # A live Wikidata edit between the two queries moves the count by a row or two
        # (834,713 of 834,714 on 2026-09-29 failed a whole import); truncation loses chunks.
        if abs(len(rows) - want) > max(5, want // 100_000):
            raise RuntimeError(f"{name}: QLever returned {len(rows)} of {want} rows — truncated, refusing")
    log(f"{name}: {len(rows)} rows")
    time.sleep(PAGE_GAP_S)
    return rows


def cmd_kinds_wikidata(conn) -> None:
    t0 = time.time()
    conn.execute("DROP TABLE IF EXISTS kw_items, kw_edges, kw_classes, kw_parents")
    conn.execute("CREATE TABLE kw_items (qid text, title text, url text, domain text, sitelinks integer, descr text, "
                 "label text)")
    conn.execute("CREATE TABLE kw_edges (qid text, prop text, k text)")
    conn.execute("CREATE TABLE kw_classes (k text PRIMARY KEY, label text, aliases text[])")
    conn.execute("CREATE TABLE kw_parents (k text, parent text)")
    with conn.cursor().copy("COPY kw_items FROM STDIN") as cp:
        for p in _rows("items", ITEMS):
            title = _title(p[1]) if len(p) >= 3 else None
            got = normalize(p[2].strip("<>"), title) if title else None
            if not got:
                continue
            sl = _lit(p[3]) if len(p) > 3 else ""
            cp.write_row((_qid(p[0]), title, got[0], got[1], int(sl) if sl.isdigit() else None,
                          _lit(p[4])[:300] if len(p) > 4 and p[4] else None, None))
        seen_nw: set[tuple[str, str]] = set()
        for p in _rows("items without an English article", ITEMS_NOWIKI):
            if len(p) < 5:
                continue
            got = normalize(p[1].strip("<>"), _lit(p[4]))
            if not got or (_qid(p[0]), got[1]) in seen_nw:
                continue
            seen_nw.add((_qid(p[0]), got[1]))
            sl = _lit(p[2]) if p[2] else ""
            cp.write_row((_qid(p[0]), "wd:" + _qid(p[0]), got[0], got[1], int(sl) if sl.isdigit() else None,
                          _lit(p[3])[:300] if p[3] else None, _lit(p[4])[:200]))
    ks: set[str] = set()
    with conn.cursor().copy("COPY kw_edges FROM STDIN") as cp:
        for prop, name in PROPS.items():
            for query in (EDGES % prop, EDGES_NOWIKI % prop):
                for p in _rows(f"edges {prop}", query):
                    if len(p) >= 2:
                        ks.add(_qid(p[1]))
                        cp.write_row((_qid(p[0]), name, _qid(p[1])))
    kl = sorted(ks)
    parents: set[str] = set()
    with conn.cursor().copy("COPY kw_parents FROM STDIN") as cp:
        for i in range(0, len(kl), BATCH):
            for p in _rows("parents", PARENTS % " ".join("wd:" + k for k in kl[i:i + BATCH]), check=False):
                if len(p) >= 2:
                    parents.add(_qid(p[1]))
                    cp.write_row((_qid(p[0]), _qid(p[1])))
    allk = sorted(ks | parents)
    seen: set[str] = set()
    with conn.cursor().copy("COPY kw_classes FROM STDIN") as cp:
        for i in range(0, len(allk), BATCH):
            for p in _rows("classes", CLASSES % " ".join("wd:" + k for k in allk[i:i + BATCH]), check=False):
                if len(p) >= 2 and _qid(p[0]) not in seen:
                    seen.add(_qid(p[0]))
                    als = [_lit(a) for a in _lit(p[2]).split("|")] if len(p) > 2 and p[2] else []
                    cp.write_row((_qid(p[0]), _lit(p[1]), [a for a in als if a][:20]))
    conn.commit()
    n = {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
         for t in ("kw_items", "kw_edges", "kw_classes", "kw_parents")}
    log(f"done in {time.time() - t0:.0f} s: {n}")


# ---------------------------------------------------------------------------
# kinds-build: every source -> site_kinds (domain, source, phrase) -> kind_docs
# ---------------------------------------------------------------------------
# A domain can be answered when the box has its homepage (docs), a bare link (nav_sites) or
# an article's official site (wiki_sites). Popularity comes from what the serving tables
# already hold: Tranco / Common Crawl host rank (serve_sites), Wikidata sitelinks (kw_items)
# and English Wikipedia pageviews (wiki_pop).
# Each domain takes its kinds from ONE owner: the best-known article/item whose official site
# is the domain's front page. Without this youtube.com collected "high school", "cat" and
# "town in the United States" from every item whose official site is a YouTube channel
# (2026-09-28), and every Wikipedia-derived source would inherit the same noise.
OWNER_SQL = r"""
DROP TABLE IF EXISTS kinds_owner;
CREATE TABLE kinds_owner AS
SELECT DISTINCT ON (domain) domain, title FROM (
  SELECT i.domain, i.title, coalesce(i.sitelinks, 0) AS sl FROM kw_items i
  WHERE i.url ~* ('^https?://(www\.)?' || replace(i.domain, '.', '\.') || '/?$')
  UNION ALL
  SELECT s.domain, w.title, coalesce(p.sitelinks, 0) FROM wiki_sites s JOIN wiki_docs w USING (page_id)
  LEFT JOIN wiki_pop p USING (page_id)
  WHERE s.url ~* ('^https?://(www\.)?' || replace(s.domain, '.', '\.') || '/?$')
) x ORDER BY domain, sl DESC, title;
CREATE INDEX ON kinds_owner (title);
"""

SITE_KINDS_SQL = """
DROP TABLE IF EXISTS site_kinds_new;
CREATE TABLE site_kinds_new (domain text, source text, phrase text);
-- Wikidata: the owner's classes, their aliases, and (for 'is' / 'use') their parents
INSERT INTO site_kinds_new
SELECT DISTINCT o.domain, 'wikidata', ph FROM kinds_owner o JOIN kw_items i ON i.title = o.title AND i.domain = o.domain
       JOIN kw_edges e USING (qid) JOIN kw_classes c ON c.k = e.k,
       LATERAL unnest(array_prepend(c.label, coalesce(c.aliases, '{}'))) ph
WHERE ph <> '';
INSERT INTO site_kinds_new
SELECT DISTINCT o.domain, 'wikidata-parent', pc.label FROM kinds_owner o
       JOIN kw_items i ON i.title = o.title AND i.domain = o.domain JOIN kw_edges e USING (qid)
       JOIN kw_parents p ON p.k = e.k JOIN kw_classes pc ON pc.k = p.parent
WHERE e.prop IN ('is', 'use');
"""

WIKICAT_SQL = """
INSERT INTO site_kinds_new
SELECT DISTINCT t.domain, 'wikicat', c.category FROM kinds_wikicat c JOIN kinds_owner t ON t.title = c.title;
"""
WIKILIST_SQL = """
INSERT INTO site_kinds_new
SELECT DISTINCT t.domain, 'wikilist',
       regexp_replace(regexp_replace(l.list, '^(Lists? of|Comparison of) ', ''), ' \\(.*\\)$', '')
FROM kinds_wikilist l JOIN kinds_owner t ON t.title = l.title;
"""
# DMOZ: the last two path segments ("Computers/Security/Password_Managers" -> "Password
# Managers", "Security"); single letters are the directory's A-Z buckets.
DMOZ_SQL = """
INSERT INTO site_kinds_new
SELECT DISTINCT d.domain, 'dmoz', replace(seg, '_', ' ')
FROM kinds_dmoz d, LATERAL (SELECT s FROM unnest(string_to_array(d.path, '/')) WITH ORDINALITY AS u(s, n)
                            ORDER BY n DESC LIMIT 2) x(seg)
WHERE length(seg) > 1;
"""
# awesome lists: the section heading the link sits under, and the list's own name
AWESOME_SQL = """
INSERT INTO site_kinds_new
SELECT DISTINCT a.domain, 'awesome', ph FROM kinds_awesome a,
       LATERAL unnest(ARRAY[regexp_replace(a.section, '^.*> *', ''),
                            regexp_replace(split_part(a.repo, '/', 2), '^awesome-?', '')]) ph
WHERE a.domain NOT IN ('github.com', 'gitlab.com', 'bitbucket.org', 'codeberg.org', 'sr.ht', 'npmjs.com', 'pypi.org')
  AND length(ph) > 2;
"""

# A domain the box can answer: a homepage, a bare link or an article's official site.
DOMAINS_SQL = """
DROP TABLE IF EXISTS kinds_domains;
CREATE TABLE kinds_domains AS
SELECT domain FROM docs WHERE error IS NULL
UNION SELECT domain FROM nav_sites UNION SELECT domain FROM wiki_sites;
CREATE UNIQUE INDEX ON kinds_domains (domain);
ANALYZE kinds_domains;
DELETE FROM site_kinds_new k WHERE NOT EXISTS (SELECT 1 FROM kinds_domains d WHERE d.domain = k.domain);
ANALYZE site_kinds_new;
"""

# The phrases Jev kept (the deleted utility/search-kind-phrases.py; history): source, phrase, p(kind of site)
KEEP_MIN = 0.5

KIND_DOCS_SQL = """
DELETE FROM site_kinds_new k WHERE k.source NOT IN ('jevhome') AND NOT EXISTS
  (SELECT 1 FROM kinds_phrase_keep p WHERE p.source = k.source AND p.phrase = k.phrase AND p.p >= %(keep)s);
DROP TABLE IF EXISTS site_kinds;
ALTER TABLE site_kinds_new RENAME TO site_kinds;
CREATE INDEX ON site_kinds (domain);
ANALYZE site_kinds;

DROP TABLE IF EXISTS kind_docs_new;
CREATE TABLE kind_docs_new AS
WITH k AS (SELECT domain, string_agg(phrase, ' | ' ORDER BY source, phrase) AS kinds,
                  count(DISTINCT source) AS sources
           FROM site_kinds GROUP BY domain),
wd AS (SELECT DISTINCT ON (i.domain) i.domain, coalesce(i.label, i.title) AS title, i.url, i.descr, i.sitelinks, i.qid
       FROM kinds_owner o JOIN kw_items i ON i.title = o.title AND i.domain = o.domain
       ORDER BY i.domain, i.sitelinks DESC NULLS LAST),
own AS (SELECT o.domain, max(p.pageviews) AS pageviews, max(p.sitelinks) AS sitelinks
        FROM kinds_owner o JOIN wiki_docs w ON w.title = o.title JOIN wiki_pop p USING (page_id) GROUP BY 1),
dm AS (SELECT DISTINCT ON (domain) domain, title, description FROM kinds_dmoz ORDER BY domain, length(path))
SELECT k.domain,
       coalesce(d.url, wd.url, 'https://' || k.domain || '/') AS url,
       coalesce(nullif(d.title, ''), wd.title, dm.title, nullif(xd.name, ''), k.domain) AS title,
       coalesce(wd.descr, dm.description, nullif(xd.description, ''), d.description, '') AS descr,
       k.kinds, k.sources,
       ss.tranco_rank, ss.cc_hc_pos, coalesce(own.sitelinks, wd.sitelinks) AS sitelinks, own.pageviews,
       il.sources AS inlinks,
       (SELECT min(c.country) FROM kw_country c WHERE c.qid = wd.qid) AS country
FROM k LEFT JOIN docs d ON d.domain = k.domain AND d.error IS NULL
LEFT JOIN wd ON wd.domain = k.domain LEFT JOIN dm ON dm.domain = k.domain
LEFT JOIN kinds_extra_descr xd ON xd.domain = k.domain
LEFT JOIN own ON own.domain = k.domain LEFT JOIN serve_sites ss ON ss.domain = k.domain
LEFT JOIN site_inlinks il ON il.domain = k.domain;
ALTER TABLE kind_docs_new ADD COLUMN search_text text;
UPDATE kind_docs_new SET search_text = kinds || ' ' || descr;
DROP TABLE IF EXISTS kind_docs;
ALTER TABLE kind_docs_new RENAME TO kind_docs;
CREATE UNIQUE INDEX kind_docs_domain ON kind_docs (domain);
CREATE INDEX kind_docs_bm25 ON kind_docs USING bm25 (search_text) WITH (text_config = 'english');
"""


# Kind names are matched after this normalisation, on both sides. It must stay identical to
# search_api.kind_key() (apps/search/serve/api) — tests/unit/test_search_kinds.py checks.
KIND_FILLER = frozenset("a an the best top good great free cheap online site sites website websites "
                        "webpage app apps application applications service services platform platforms tool tools "
                        "software program programs provider providers company companies store stores for to with "
                        "of and or my".split())
_TOKEN = re.compile(r"[a-z0-9]+")


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
    return " ".join(_singular(w) for w in _TOKEN.findall(text.lower()) if w not in KIND_FILLER)


MEMBERS_SQL = """
DROP TABLE IF EXISTS kind_members;
CREATE TABLE kind_members AS
SELECT p.key, k.domain, count(*)::int AS votes
FROM site_kinds k JOIN kinds_phrase_keys p USING (phrase)
WHERE length(p.key) >= 2 GROUP BY 1, 2;
CREATE INDEX ON kind_members (key);
ANALYZE kind_members;
DROP TABLE IF EXISTS kind_keys;
CREATE TABLE kind_keys AS
SELECT key, string_to_array(key, ' ') AS words, count(*)::int AS n FROM kind_members GROUP BY 1;
CREATE UNIQUE INDEX ON kind_keys (key);
CREATE INDEX kind_keys_words ON kind_keys USING gin (words);
"""
# Jev's verdict per (kind, site) for each kind's best-known members
# (the deleted utility/search-kind-members.py; history): ranking puts judged-good sites first.
MEMBER_JEV_SQL = """
ALTER TABLE kind_members ADD COLUMN jev real;
UPDATE kind_members m SET jev = j.p FROM kinds_member_jev j WHERE j.key = m.key AND j.domain = m.domain;
"""


def _load_tsv(conn, table: str, cols: str, path: str) -> int:
    conn.execute(f"DROP TABLE IF EXISTS {table}")
    conn.execute(f"CREATE TABLE {table} ({cols})")
    if not os.path.exists(path):
        log(f"{path}: missing — source skipped")
        return 0
    n = 0
    ncol = cols.count(",") + 1
    with gzip.open(path, "rt", encoding="utf-8", errors="replace") as f, \
            conn.cursor().copy(f"COPY {table} FROM STDIN") as cp:
        for line in f:
            p = line.rstrip("\n").split("\t")
            if len(p) >= ncol:
                cp.write_row(p[:ncol])
                n += 1
    log(f"{table}: {n} rows from {path}")
    return n


# More sources, one file each in <import>/kinds-extra/ (2026-09-29): `<source>.tsv.gz` with
# domain, url, phrase, name, description — UT1 categories, Homebrew, Flathub, public-apis …
# (licences: docs/legal/SEARCH-DATA-SOURCES.md). The file name is the `source`, so dropping a
# source for licensing is deleting its file and rebuilding. `*-unsafe.*` files (UT1's
# adult / malware lists) are for a junk filter, never kinds. wikiinfobox-raw.tsv.gz is
# title-keyed (article, infobox, field, value) and joins through the owner like categories.
# jevhome.tsv.gz (utility/search-kind-homepages.py) holds (site, kind name) pairs Jev already
# approved from the homepage itself, so the phrase filter does not apply to it.
EXTRA_SKIP = ("-unsafe", "-raw")


# More kinds from data already in the build database (2026-09-29):
#  wikilead   the owner article's first sentence, "Bitwarden is a freemium open-source password
#             management service" -> its last 2 and 3 words ("management service", "password
#             management service"): kind names end in their head noun.
#  anchor     what other sites call the site when they link to it (Common Crawl anchor text):
#             2-4 words, >= 5 linking sites, not the site's own name, not "click here".
#  wdcplace   schema.org / OSM-style business types of Web Data Commons listings whose website is
#             the domain, for chains (>= 3 listings of that type). OSM rows are NOT used (ODbL).
_LEAD = re.compile(r"\b(?:is|was|are|were)\s+(?:an?|the|one of the)\s+(.{3,160}?)"
                   r"(?:[.,;:(]| that | which | who | with | for | founded | based | owned | operated | run | "
                   r"developed | launched | created | headquartered | in | on | from | by |$)")
_LEAD_SKIP = frozenset("american british canadian indian chinese japanese german french global international "
                       "multinational private public free online".split())


def lead_phrases(lead: str) -> list[str]:
    """First sentence -> the kind it names: the last 2 and 3 words of "is a ..."."""
    m = _LEAD.search((lead or "")[:400])
    if not m:
        return []
    words = [w for w in re.findall(r"[A-Za-z0-9-]+", m.group(1).lower())]
    while words and words[0] in _LEAD_SKIP:
        words.pop(0)
    out = []
    for n in (2, 3):
        if len(words) >= n:
            out.append(" ".join(words[-n:]))
    if len(words) == 1:
        out.append(words[0])
    return list(dict.fromkeys(out))


ANCHOR_SQL = r"""
INSERT INTO site_kinds_new
SELECT DISTINCT a.domain, 'anchor', lower(a.anchor) FROM anchors a
WHERE a.sources >= 5
  AND array_length(regexp_split_to_array(trim(a.anchor), '\s+'), 1) BETWEEN 2 AND 4
  AND position(split_part(a.domain, '.', 1) IN lower(replace(a.anchor, ' ', ''))) = 0
  AND lower(a.anchor) !~ '(click|here|home ?page|visit|official|website|web site|link|read more|learn more|http|www|\.com|\.org|\.net|download now|sign up|log ?in)'
"""
WDCPLACE_SQL = r"""
INSERT INTO site_kinds_new
SELECT d, 'wdcplace', replace(split_part(kind, '=', 2), '_', ' ') FROM (
  SELECT lower(regexp_replace(regexp_replace(regexp_replace(website, '^https?://', ''), '/.*$', ''), '^www\.', '')) AS d, kind
  FROM places WHERE osm LIKE 'wdc%' AND coalesce(website, '') <> ''
    AND kind NOT IN ('schema=LocalBusiness', 'shop=yes')) x
WHERE d <> '' GROUP BY d, kind HAVING count(*) >= 3
"""


def _load_inhouse(conn, open_build: bool = False) -> None:
    rows = conn.execute("SELECT o.domain, left(w.lead, 400) FROM kinds_owner o JOIN wiki_docs w ON w.title = o.title "
                        "WHERE w.lead IS NOT NULL").fetchall()
    n = 0
    with conn.cursor().copy("COPY site_kinds_new FROM STDIN") as cp:
        for dom, lead in rows:
            for ph in lead_phrases(lead):
                cp.write_row((dom, "wikilead", ph))
                n += 1
    log(f"wikilead: {n} phrases from {len(rows)} first sentences")
    if open_build:          # anchor text (Common Crawl ToU) and WDC types (D3) are not open sources
        return
    if conn.execute("SELECT to_regclass('anchors')").fetchone()[0]:
        conn.execute(ANCHOR_SQL)
    if conn.execute("SELECT to_regclass('places')").fetchone()[0]:
        conn.execute(WDCPLACE_SQL)


# Wikipedia's category tree (wikicats.py --tree): a site in "Password managers" is also in its
# parent "Security software" — one level only (two levels reach "Software", "Technology").
def _load_cattree(conn, path: str) -> None:
    if _load_tsv(conn, "kinds_cattree", "child text, parent text", path):
        conn.execute("CREATE INDEX ON kinds_cattree (child)")
        conn.execute("ANALYZE kinds_cattree")
        conn.execute("""INSERT INTO site_kinds_new
            SELECT DISTINCT k.domain, 'wikicat-parent', t.parent
            FROM site_kinds_new k JOIN kinds_cattree t ON t.child = k.phrase
            WHERE k.source = 'wikicat' AND length(t.parent) BETWEEN 3 AND 80""")


def _load_extra(conn, folder: str, allow: frozenset | None = None) -> None:
    if not os.path.isdir(folder):
        log(f"{folder}: missing — no extra sources")
        return
    for fn in sorted(os.listdir(folder)):
        if not fn.endswith(".tsv.gz"):
            continue
        src = fn[:-len(".tsv.gz")]
        path = os.path.join(folder, fn)
        if src == "wikiinfobox-raw" and (allow is None or "infobox" in allow):
            _load_tsv(conn, "kinds_infobox", "title text, infobox text, field text, value text", path)
            conn.execute("""INSERT INTO site_kinds_new
                SELECT DISTINCT o.domain, 'infobox', i.value FROM kinds_infobox i JOIN kinds_owner o ON o.title = i.title
                WHERE length(i.value) BETWEEN 3 AND 80""")
            continue
        if src.endswith(EXTRA_SKIP) or not re.fullmatch(r"[a-z0-9_]+", src):
            continue
        if allow is not None and src not in allow:
            log(f"{fn}: not an open source — skipped")
            continue
        _load_tsv(conn, "kinds_extra", "domain text, url text, phrase text, name text, description text", path)
        conn.execute("""INSERT INTO site_kinds_new
            SELECT DISTINCT lower(regexp_replace(domain, '^www\\.', '')), %s, phrase FROM kinds_extra
            WHERE length(phrase) BETWEEN 3 AND 80 AND domain <> ''""", (src,))
        # a description for sites no other source describes
        conn.execute("""INSERT INTO kinds_extra_descr SELECT DISTINCT ON (d) lower(regexp_replace(domain, '^www\\.', '')) d,
                   name, description FROM kinds_extra WHERE description <> '' ORDER BY d, length(description) DESC
            ON CONFLICT (domain) DO NOTHING""")


def cmd_kinds_build(conn, import_dir: str, phrases: str | None, members: str | None = None,
                    leaders: str | None = None, open_build: bool = False) -> None:
    """--open: every open source -> site_kinds_new -> the open filter, members and lead order
    (kinds_open.py) -> kind_docs, kind_members, kind_keys. Old path, history only: without
    --phrases build site_kinds_new and list its phrases in kinds_phrase_todo for Jev; with
    --phrases keep only Jev's kept phrases -> kind_docs."""
    t0 = time.time()
    if phrases or members or leaders:
        # the tools that wrote these Jev verdict files were deleted (2026-10-05, ADR-065)
        raise SystemExit("kinds-build: --phrases/--members/--leaders were Jev verdict files (ADR-065, tools deleted); "
                         "build the open kinds with --open (kinds_open.py)")
    _load_tsv(conn, "kinds_wikicat", "title text, category text", os.path.join(import_dir, "wiki-cats.tsv.gz"))
    _load_tsv(conn, "kinds_wikilist", "list text, title text", os.path.join(import_dir, "wiki-listlinks.tsv.gz"))
    _load_tsv(conn, "kinds_dmoz", "url text, domain text, path text, title text, description text",
              os.path.join(import_dir, "dmoz.tsv.gz"))
    _load_tsv(conn, "kinds_awesome", "url text, domain text, repo text, license text, section text, "
              "link_text text, description text", os.path.join(import_dir, "awesome.tsv.gz"))
    # fresh tables have no statistics: without ANALYZE the planner picks nested loops over
    # a million edges (stage 1 ran 9+ minutes on 2026-09-28 before this)
    conn.execute("CREATE INDEX IF NOT EXISTS kw_edges_qid ON kw_edges (qid)")
    conn.execute("CREATE INDEX IF NOT EXISTS kw_items_domain ON kw_items (domain, title)")
    conn.execute("CREATE INDEX IF NOT EXISTS kw_parents_k ON kw_parents (k)")
    conn.execute("SET enable_nestloop = off")      # join estimates are off by 10^5 (rows=1)
    for t in ("kw_items", "kw_edges", "kw_classes", "kw_parents", "kinds_wikicat", "kinds_wikilist",
              "kinds_dmoz", "kinds_awesome"):
        conn.execute(f"ANALYZE {t}")
    conn.execute(OWNER_SQL)
    conn.execute("ANALYZE kinds_owner")
    conn.execute(SITE_KINDS_SQL)
    for sql in (WIKICAT_SQL, WIKILIST_SQL, DMOZ_SQL, AWESOME_SQL):
        conn.execute(sql)
    conn.execute("DROP TABLE IF EXISTS kinds_extra_descr")
    conn.execute("CREATE TABLE kinds_extra_descr (domain text PRIMARY KEY, name text, description text)")
    import kinds_open
    _load_extra(conn, os.path.join(import_dir, "kinds-extra"), kinds_open.EXTRA_ALLOW if open_build else None)
    _load_inhouse(conn, open_build)
    _load_cattree(conn, os.path.join(import_dir, "kinds-extra-staging", "wiki-cattree.tsv.gz"))
    conn.execute(DOMAINS_SQL)
    for src, n in conn.execute("SELECT source, count(*) FROM site_kinds_new GROUP BY 1 ORDER BY 1"):
        log(f"site_kinds_new {src}: {n}")
    if open_build:
        conn.commit()
        kinds_open.cmd_kinds_open(conn)
        log(f"open build done in {time.time() - t0:.0f} s")
        return
    if not phrases:
        # one row per distinct (source, phrase): how many domains, and the best-known three
        conn.execute("DROP TABLE IF EXISTS kinds_phrase_todo")
        conn.execute("""CREATE TABLE kinds_phrase_todo AS
            SELECT source, phrase, count(*) AS n,
                   (array_agg(k.domain ORDER BY coalesce(s.tranco_rank, 9999999)))[1:3] AS examples
            FROM site_kinds_new k LEFT JOIN serve_sites s USING (domain)
            GROUP BY 1, 2""")
        n = conn.execute("SELECT count(*) FROM kinds_phrase_todo").fetchone()[0]
        log(f"kinds_phrase_todo: {n} distinct phrases — judge them, then re-run with --phrases")
        return
    conn.execute("DROP TABLE IF EXISTS kinds_phrase_keep")
    conn.execute("CREATE TABLE kinds_phrase_keep (source text, phrase text, p real)")
    with open(phrases, encoding="utf-8") as f, conn.cursor().copy("COPY kinds_phrase_keep FROM STDIN") as cp:
        for line in f:
            p = line.rstrip("\n").split("\t")
            if len(p) >= 3:
                cp.write_row((p[0], p[1], float(p[2])))
    conn.execute("CREATE TABLE IF NOT EXISTS kw_country (qid text, country text)")   # optional source
    conn.execute("ANALYZE kinds_phrase_keep")
    conn.execute(KIND_DOCS_SQL.replace("%(keep)s", str(KEEP_MIN)))
    conn.execute("DROP TABLE IF EXISTS kinds_phrase_keys")
    conn.execute("CREATE TABLE kinds_phrase_keys (phrase text PRIMARY KEY, key text)")
    phrases_all = [r[0] for r in conn.execute("SELECT DISTINCT phrase FROM site_kinds")]
    with conn.cursor().copy("COPY kinds_phrase_keys FROM STDIN") as cp:
        for ph in phrases_all:
            cp.write_row((ph, kind_key(ph)))
    conn.execute(MEMBERS_SQL)
    if members:
        conn.execute("DROP TABLE IF EXISTS kinds_member_jev")
        conn.execute("CREATE TABLE kinds_member_jev (key text, domain text, p real)")
        with open(members, encoding="utf-8") as f, conn.cursor().copy("COPY kinds_member_jev FROM STDIN") as cp:
            for line in f:
                p = line.rstrip("\n").split("\t")
                if len(p) >= 3:
                    cp.write_row((p[0], p[1], float(p[2])))
        conn.execute("CREATE INDEX ON kinds_member_jev (key, domain)")
        conn.execute(MEMBER_JEV_SQL)
        j = conn.execute("SELECT count(jev), count(*) FILTER (WHERE jev >= 0.7) FROM kind_members").fetchone()
        log(f"kind_members: {j[0]} judged by Jev, {j[1]} good")
    else:
        conn.execute("ALTER TABLE kind_members ADD COLUMN jev real")
    # Which approved sites people mean first (the deleted utility/search-kind-leaders.py; history): orders the lift.
    conn.execute("ALTER TABLE kind_members ADD COLUMN IF NOT EXISTS lead real")
    if leaders:
        _load_tsv_plain(conn, "kinds_member_lead", "key text, domain text, p real", leaders)
        conn.execute("CREATE INDEX ON kinds_member_lead (key, domain)")
        conn.execute("UPDATE kind_members m SET lead = l.p FROM kinds_member_lead l WHERE l.key = m.key AND l.domain = m.domain")
        n = conn.execute("SELECT count(lead) FROM kind_members").fetchone()[0]
        log(f"kind_members: {n} with a lead verdict")
    m = conn.execute("SELECT count(*), count(DISTINCT key) FROM kind_members").fetchone()
    log(f"kind_members: {m[0]} (kind, site) pairs over {m[1]} kind names")
    n = conn.execute("SELECT count(*), count(*) FILTER (WHERE tranco_rank IS NOT NULL) FROM kind_docs").fetchone()
    log(f"kind_docs: {n[0]} sites ({n[1]} with a Tranco rank) in {time.time() - t0:.0f} s")


def _load_tsv_plain(conn, table: str, cols: str, path: str) -> None:
    """An uncompressed TSV (the Jev verdict files) -> table."""
    conn.execute(f"DROP TABLE IF EXISTS {table}")
    conn.execute(f"CREATE TABLE {table} ({cols})")
    n = cols.count(",") + 1
    with open(path, encoding="utf-8") as f, conn.cursor().copy(f"COPY {table} FROM STDIN") as cp:
        for line in f:
            p = line.rstrip("\n").split("\t")
            if len(p) >= n:
                cp.write_row(p[:n])


def cmd_kinds_vecs(conn, path: str) -> None:
    """kind name -> embedding, from apps/search/tools/embed-kinds.py -> kind_vecs."""
    conn.execute("DROP TABLE IF EXISTS kind_vecs_new")
    conn.execute("CREATE TABLE kind_vecs_new (key text PRIMARY KEY, v real[])")
    n = 0
    with open(path, encoding="utf-8") as f, conn.cursor().copy("COPY kind_vecs_new FROM STDIN") as cp:
        for line in f:
            key, _, vec = line.rstrip("\n").partition("\t")
            if key and vec:
                cp.write_row((key, [float(x) for x in vec.split(",")]))
                n += 1
    conn.execute("DROP TABLE IF EXISTS kind_vecs")
    conn.execute("ALTER TABLE kind_vecs_new RENAME TO kind_vecs")
    log(f"kind_vecs: {n} kind names")


def cmd_kinds_sitevecs(conn, path: str) -> None:
    """Site vectors (bge-small, CLS, unit, no instruction; title + approved kinds + description,
    embedded on buildbox's GPU — identical to search-api's ONNX, cosine 1.0 on probes) -> kind_site_vec
    with an HNSW cosine index. search-api's meaning search over sites (2026-09-30)."""
    conn.execute("DROP TABLE IF EXISTS kind_site_vec_new")
    conn.execute("CREATE TABLE kind_site_vec_new (domain text PRIMARY KEY, v halfvec(384))")
    n = 0
    with open(path, encoding="utf-8") as f, conn.cursor().copy("COPY kind_site_vec_new (domain, v) FROM STDIN") as cp:
        for line in f:
            dom, _, vec = line.rstrip("\n").partition("\t")
            if dom and vec:
                cp.write_row((dom, "[" + vec + "]"))
                n += 1
    conn.execute("CREATE INDEX kind_site_vec_new_hnsw ON kind_site_vec_new USING hnsw (v halfvec_cosine_ops) "
                 "WITH (m = 16, ef_construction = 64)")
    conn.execute("DROP TABLE IF EXISTS kind_site_vec")
    conn.execute("ALTER TABLE kind_site_vec_new RENAME TO kind_site_vec")
    conn.execute("ALTER INDEX kind_site_vec_new_hnsw RENAME TO kind_site_vec_hnsw")
    log(f"kind_site_vec: {n} sites")


# Which broad trees each kind class sits under (wdt:P279*), for the open build's "not a kind of
# site" rule (kinds_open.NOT_SITE_ROOTS / SITE_ROOTS, 2026-10-01). One query per batch of classes,
# both ends bound, so the path search stays small. CC0.
ROOTS = """SELECT DISTINCT ?k ?root WHERE { VALUES ?k { %s } VALUES ?root { %s } ?k wdt:P279* ?root . }"""


def cmd_kinds_roots(conn) -> None:
    import kinds_open
    roots = " ".join("wd:" + q for q in sorted(kinds_open.NOT_SITE_ROOTS | kinds_open.SITE_ROOTS))
    ks = [r[0] for r in conn.execute("SELECT k FROM kw_classes ORDER BY k")]
    conn.execute("DROP TABLE IF EXISTS kw_class_roots_new")
    conn.execute("CREATE TABLE kw_class_roots_new (k text, root text)")
    n = 0
    with conn.cursor().copy("COPY kw_class_roots_new FROM STDIN") as cp:
        for i in range(0, len(ks), BATCH):
            for p in _rows("roots", ROOTS % (" ".join("wd:" + k for k in ks[i:i + BATCH]), roots), check=False):
                if len(p) >= 2:
                    cp.write_row((_qid(p[0]), _qid(p[1])))
                    n += 1
    conn.execute("DROP TABLE IF EXISTS kw_class_roots")
    conn.execute("ALTER TABLE kw_class_roots_new RENAME TO kw_class_roots")
    log(f"kw_class_roots: {n} (class, root) rows over {len(ks)} classes")


# Country of each site's owner (Wikidata P17), for search-api's home-country preference among
# the approved sites of a kind ("supermarket chain" from a US box: US chains first).
COUNTRY = """SELECT DISTINCT ?item ?cl WHERE { ?item wdt:P856 ?site . ?item wdt:P17 ?c .
  ?c rdfs:label ?cl FILTER(LANG(?cl) = "en") }"""


def cmd_kinds_country(conn) -> None:
    conn.execute("DROP TABLE IF EXISTS kw_country_new")
    conn.execute("CREATE TABLE kw_country_new (qid text, country text)")
    with conn.cursor().copy("COPY kw_country_new FROM STDIN") as cp:
        for p in _rows("country", COUNTRY):
            if len(p) >= 2:
                cp.write_row((_qid(p[0]), _lit(p[1])))
    conn.execute("CREATE INDEX ON kw_country_new (qid)")
    conn.execute("DROP TABLE IF EXISTS kw_country")
    conn.execute("ALTER TABLE kw_country_new RENAME TO kw_country")
    log(f"kw_country: {conn.execute('SELECT count(*) FROM kw_country').fetchone()[0]} rows")
