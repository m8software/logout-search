"""The open kinds build (ADR-065, 2026-10-01): kinds of site without Jev, Tranco or WDC.

    search_import.py kinds-build --open [--import DIR]   every source, then this step
    search_import.py kinds-open                          this step alone, over an existing site_kinds_new

The old build kept each category-like phrase by an offline Jev verdict, judged each kind's
best-known members with Jev, ordered them by a Jev "first choice" verdict, and added Jev-approved
(site, kind) pairs from homepages (`jevhome`). None of that may ship. This step replaces each one
with evidence the sources themselves carry, so anyone with the same dumps gets the same tables.

Sources and families. A family is one independent editorial effort; two rows from one family
are one vote, however many `source` strings they come under:
    wikidata    wikidata (P31 instance of, P366 use, P136 genre, P452 industry, P1056 product,
                P400 platform, P306 OS), wikidata-parent                               CC0
    wikipedia   wikicat, wikicat-parent, wikilist, wikilead (first sentence), infobox  CC BY-SA 4.0
    dmoz        Open Directory 2017                                                    CC BY 3.0
    awesome     permissively licensed awesome lists                                    per list (CC0/MIT/CC BY)
    ut1         Université Toulouse 1 blacklist categories (not the unsafe lists)      CC BY-SA 4.0
    homebrew / flathub / debian / publicapis — each its own family                     BSD-2 / facts / facts / MIT
Left out: jevhome (Jev), wdcplace (Web Data Commons, D3), anchor (Common Crawl anchor text) and
appmeta (homepage schema.org types) — the last two are Common Crawl ToU material, not on the
open-source list in docs/legal/SEARCH-DATA-LICENSES.md.

1. Kind names (replaces the Jev phrase filter). Serving matches normalised kind names
   (kinds.kind_key), so the filter works on keys, not raw phrases. A key is kept when
       it passes the junk rules (junk_reason), and
       KEY_MIN_SITES <= sites naming it <= KEY_MAX_SITES, and
       (a Wikidata class that >= WD_CLASS_MIN sites are directly an instance/use/genre of)
       or (>= FAMILY_MIN independent families name it).
2. Members (replaces the Jev member verdicts): conf in [0, 1] per (kind, site), from
       direct Wikidata statement P31/P136 to a class with that name — the strongest (P366 use is a
       full vote but not direct: v1 lifted chatgpt.com for "language learning app");
       votes = families naming the pair, where a Wikidata vote that is only P452 industry /
       P1056 product / P400 / P306 / a parent class counts 0.5, and so does a Wikipedia vote
       that is only a parent category. Industry and product are never enough alone: doc 11
       found microsoft.com in "budgeting app" and deloitte.com in "accounting software"
       exactly that way.
   conf >= LIFT_CONF is what serving may lift above the article (search_api KIND_LIFT_CONF).
3. Lead order (replaces the Jev "first choice" verdict): lead in [0, 1], how central the kind
   is to the site — 0.5 when it is the site's own Wikidata class (P31/P136), 0.3 when the
   site's Wikipedia first sentence names it, 0.2 x agreement of further families. Popularity
   (Common Crawl host rank, Wikidata sitelinks, Wikipedia pageviews — no Tranco) is added at
   serving time (search_api KIND_POP_OPEN).

Thresholds are written down with their reasons next to each constant, and the audit
(docs/internal/audits/2026-10-01-kinds-nojev.md) records what each one removed.
"""

from __future__ import annotations

import re
import time

from kinds import kind_key, log

# Extra-source files (<import>/kinds-extra/<source>.tsv.gz) the open build reads; anything else is
# skipped. "infobox" = wikiinfobox-raw (Wikipedia infobox type/genre/services).
EXTRA_ALLOW = frozenset({"ut1", "homebrew", "flathub", "debian", "publicapis", "infobox"})

FAMILY = {
    "wikidata": "wikidata", "wikidata-parent": "wikidata",
    "wikicat": "wikipedia", "wikicat-parent": "wikipedia", "wikilist": "wikipedia",
    "wikilead": "wikipedia", "infobox": "wikipedia",
    "dmoz": "dmoz", "awesome": "awesome", "ut1": "ut1",
    "homebrew": "homebrew", "flathub": "flathub", "debian": "debian", "publicapis": "publicapis",
}
PARENT_SOURCES = ("wikidata-parent", "wikicat-parent")      # half a vote on their own

# --- thresholds (why: see the audit; all are counts of distinct sites or families) -------------
WD_CLASS_MIN = 3      # a Wikidata class with 1-2 member sites is mostly a one-off class
                      # ("Bitwarden-like service") — 3 is the smallest list worth lifting from
FAMILY_MIN = 2        # "appears in >= 2 independent sources" (the task's rule)
KEY_MIN_SITES = 2     # serving's KIND_MIN_MEMBERS: a kind with one site is never looked up
KEY_MAX_SITES = 5000  # above this a name is a sector or a legal form, not a kind people search
                      # for ("nonprofit", "publication", "corporate body", "united state" — the
                      # largest names left after the not-a-site rules); see the audit's size table
LIFT_CONF = 0.7       # serving lifts members at or above this (search_api KIND_LIFT_CONF)

# Wikidata trees whose classes name things that HAVE websites but are not kinds of website people
# search for: a query "film", "university" or "united states" wants the article, not three random
# official sites (the old Jev phrase filter dropped these). A key is dropped when every class of
# that name is under a NOT_SITE root and not under a SITE root (website / software / online service /
# app / mass media — "video game" is a creative work AND software, so it stays). From
# `search_import.py kinds-roots` (kw_class_roots, CC0).
NOT_SITE_ROOTS = frozenset({
    "Q5",          # human
    "Q2221906",    # geographic location
    "Q56061",      # administrative territorial entity
    "Q17537576",   # creative work (film, album, book, TV series ...)
    "Q215380",     # musical group
    "Q2385804",    # educational institution
    "Q847017",     # sports club
    "Q7278",       # political party
    "Q618779",     # award
    "Q16521",      # taxon
    "Q11173",      # chemical compound
    "Q41176",      # building
    "Q327333",     # government agency
})
# For kind NAMES the building root is too wide: "home improvement" (store), "hotel" are aliases of
# shop / building classes yet name kinds people search for. Sites owned by a building or an agency
# still leave (NOT_SITE_DOMAINS_SQL); the name stays.
NOT_SITE_KEY_ROOTS = NOT_SITE_ROOTS - {"Q41176", "Q327333"}
# Place names are not kinds ("canada", "united kingdom": DMOZ's Regional tree and category parents).
# Countries come from the build's own Wikidata country labels (kw_country, CC0); US states by hand.
US_STATES = frozenset("""alabama alaska arizona arkansas california colorado connecticut delaware florida georgia
hawaii idaho illinois indiana iowa kansas kentucky louisiana maine maryland massachusetts michigan minnesota
mississippi missouri montana nebraska nevada ohio oklahoma oregon pennsylvania tennessee texas utah vermont
virginia washington wisconsin wyoming""".split()) | frozenset({"new hampshire", "new jersey", "new mexico",
    "new york", "north carolina", "north dakota", "rhode island", "south carolina", "south dakota",
    "west virginia", "united state", "united state america", "usa", "uk"})
SITE_ROOTS = frozenset({
    "Q35127",      # website
    "Q7397",       # software
    "Q19967801",   # online service
    "Q1668024",    # service on internet
    "Q620615",     # mobile app
    "Q193424",     # web service
    "Q166142",     # application
    "Q11033",      # mass media (TV and radio stations, online newspapers)
    "Q11032",      # newspaper     } print forms sit under creative work only, but a newspaper's
    "Q41298",      # magazine      } site is what "tech news site" / "the paper of record" want
    "Q1002697",    # periodical    }
})

# Words that are never a kind of site on their own (a one-word key equal to one is dropped) and,
# for GENERIC_ANY, anywhere in the key. Written by hand from the most-membered keys.
GENERIC_ONE = frozenset("""
business enterprise organization organisation organizational entity corporation firm brand trademark
subsidiary conglomerate holding public private privately held listed group division unit agency
institution foundation association society club legal person juridical juridic thing object item
product products work project concept class type kind category system technology industry sector
media content information internet web world digital international national global local
american british canadian english german french united states america europe european
""".split())
GENERIC_ANY = frozenset("""
establishment establishments disestablishment disestablishments defunct former founded dissolved
based headquartered alumni people person births deaths century stub stubs article articles
wikipedia wikimedia wikiproject redirect redirects template templates category categories
listed traded acquired acquisition acquisitions subsidiaries
""".split())
_YEAR = re.compile(r"\b(1[0-9]{3}|20[0-9]{2})s?\b|\b[0-9]+(st|nd|rd|th)\b")
_PLACE_PREP = re.compile(r"\b(in|of|from|by)\b")     # "hotels in paris", "companies of japan", "by country"
_ONLY_DIGITS = re.compile(r"^[0-9 ]+$")


def junk_reason(key: str) -> str | None:
    """Why a kind name is not a kind of site, or None. Rules only — no model, no verdict file."""
    words = key.split()
    if not (3 <= len(key) <= 60) or not (1 <= len(words) <= 5):
        return "length"
    if _ONLY_DIGITS.match(key) or _YEAR.search(key):
        return "year/number"
    if _PLACE_PREP.search(key):
        return "preposition"           # category-shaped: "<x> in <place>", "<x> by country"
    if GENERIC_ANY & set(words):
        return "category word"
    if all(w in GENERIC_ONE for w in words):
        return "generic"
    return None


def short_reason(key: str, wd_sites: int) -> str | None:
    """"app", "web", "dvd": a one-word key of <= 3 letters collides with too much — unless it is
    a Wikidata class with members of its own ("vpn")."""
    return "short" if len(key.split()) == 1 and len(key) <= 3 and wd_sites < WD_CLASS_MIN else None


# One row per (kind, site, family): the evidence everything below counts. Built in one sequential
# pass; the build clone has no data cache (primarycache=metadata), so index-driven random reads
# over 22M rows ran for over an hour and are switched off in cmd_kinds_open.
ROWS_SQL = """
DROP TABLE IF EXISTS kinds_open_rows;
CREATE TABLE kinds_open_rows AS
SELECT p.key, k.domain, f.family,
       bool_or(k.source NOT IN ('wikidata-parent', 'wikicat-parent')) AS full_row,
       bool_or(k.source = 'wikilead') AS lead_row
FROM site_kinds_new k JOIN kinds_open_family f USING (source) JOIN kinds_open_pkeys p USING (phrase)
WHERE p.key <> '' AND NOT EXISTS (SELECT 1 FROM kinds_open_nsd n WHERE n.domain = k.domain)
GROUP BY 1, 2, 3;
ANALYZE kinds_open_rows;
"""
# Direct Wikidata statements from each domain's owner item to a class, by the class's name (label or
# alias -> key): strength 2 = P31 / P136 (what it is, its genre) — "direct"; 1.5 = P366 use (a full
# vote, not direct: ChatGPT's uses include "language learning", 2026-10-02 v1 eval); 1 = P452
# industry / P1056 product / P400 platform / P306 OS (said about the owner, not the site).
WD_SQL = """
DROP TABLE IF EXISTS kinds_open_wd;
CREATE TABLE kinds_open_wd AS
WITH cn AS (SELECT DISTINCT c.k, p.key
            FROM kw_classes c, LATERAL unnest(array_prepend(c.label, coalesce(c.aliases, '{}'))) AS u(ph),
                 kinds_open_pkeys p
            WHERE p.phrase = u.ph AND p.key <> '')
SELECT o.domain, cn.key, max(CASE WHEN e.prop IN ('is', 'genre') THEN 2 WHEN e.prop = 'use' THEN 1.5 ELSE 1 END) AS strength
FROM kinds_owner o JOIN kw_items i ON i.title = o.title AND i.domain = o.domain
     JOIN kw_edges e USING (qid) JOIN cn ON cn.k = e.k
WHERE EXISTS (SELECT 1 FROM kinds_domains d WHERE d.domain = o.domain)
  AND NOT EXISTS (SELECT 1 FROM kinds_open_nsd n WHERE n.domain = o.domain)
GROUP BY 1, 2;
ANALYZE kinds_open_wd;
"""
# Sites whose owner IS one of those things (Wikidata P31 under a NOT_SITE root, none under a SITE
# root): a singer's, a town's, a band's or a university's own site is not an example of any kind
# of site a descriptive query asks for, and these owners brought "singer", "jazz", "canada" in.
NOT_SITE_DOMAINS_SQL = """
DROP TABLE IF EXISTS kinds_open_nsd;
CREATE TABLE kinds_open_nsd AS
SELECT o.domain FROM kinds_owner o JOIN kw_items i ON i.title = o.title AND i.domain = o.domain
     JOIN kw_edges e USING (qid) JOIN kw_class_roots r ON r.k = e.k
WHERE e.prop = 'is'
GROUP BY 1 HAVING bool_or(r.root = ANY(%(bad)s)) AND NOT bool_or(r.root = ANY(%(good)s));
ANALYZE kinds_open_nsd;
"""
NOT_SITE_SQL = """
WITH cn AS (SELECT DISTINCT c.k, p.key
            FROM kw_classes c, LATERAL unnest(array_prepend(c.label, coalesce(c.aliases, '{}'))) AS u(ph),
                 kinds_open_pkeys p
            WHERE p.phrase = u.ph AND p.key <> '')
SELECT key FROM (
  SELECT cn.key, cn.k, coalesce(bool_or(r.root = ANY(%(bad)s)), false) AND NOT coalesce(bool_or(r.root = ANY(%(good)s)), false) AS bad
  FROM cn LEFT JOIN kw_class_roots r ON r.k = cn.k GROUP BY 1, 2) x
GROUP BY key HAVING bool_and(bad)        -- EVERY class of that name: "home improvement" is also a TV genre
"""
KEY_STATS_SQL = """
WITH s AS (SELECT key, count(*) AS sites FROM (SELECT DISTINCT key, domain FROM kinds_open_rows) x GROUP BY 1),
     f AS (SELECT key, count(*) AS families FROM (SELECT DISTINCT key, family FROM kinds_open_rows) x GROUP BY 1),
     w AS (SELECT key, count(*) AS n FROM kinds_open_wd WHERE strength = 2 GROUP BY 1)
SELECT s.key, s.sites, f.families, coalesce(w.n, 0) FROM s JOIN f USING (key) LEFT JOIN w USING (key)
"""

# site_kinds: only rows whose name was kept (all its spellings, every source).
SITE_KINDS_SQL = """
DROP TABLE IF EXISTS site_kinds;
CREATE TABLE site_kinds AS
SELECT k.domain, k.source, k.phrase FROM site_kinds_new k
JOIN kinds_open_pkeys p USING (phrase) JOIN kinds_open_keys x ON x.key = p.key AND x.keep;
CREATE INDEX ON site_kinds (domain);
ANALYZE site_kinds;
"""
# kind_docs as in kinds.KIND_DOCS_SQL, with tranco_rank always NULL (D2: Tranco does not ship; the
# column stays so older search-api builds read the table) and no Jev keep list.
KIND_DOCS_SQL = """
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
dm AS (SELECT DISTINCT ON (domain) domain, title, description FROM kinds_dmoz ORDER BY domain, length(path)),
cty AS (SELECT qid, min(country) AS country FROM kw_country GROUP BY 1)
SELECT k.domain,
       coalesce(d.url, wd.url, 'https://' || k.domain || '/') AS url,
       coalesce(nullif(d.title, ''), wd.title, dm.title, nullif(xd.name, ''), k.domain) AS title,
       coalesce(wd.descr, dm.description, nullif(xd.description, ''), d.description, '') AS descr,
       k.kinds, k.sources,
       NULL::integer AS tranco_rank, ss.cc_hc_pos, coalesce(own.sitelinks, wd.sitelinks) AS sitelinks, own.pageviews,
       il.sources AS inlinks,
       cty.country
FROM k LEFT JOIN docs d ON d.domain = k.domain AND d.error IS NULL
LEFT JOIN wd ON wd.domain = k.domain LEFT JOIN cty ON cty.qid = wd.qid LEFT JOIN dm ON dm.domain = k.domain
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
# kind_members: one row per (kind, site) with the open evidence. votes = family votes (real),
# conf = membership confidence, lead = how central the kind is to the site (module docstring).
MEMBERS_SQL = """
DROP TABLE IF EXISTS kind_members;
CREATE TABLE kind_members AS
WITH r AS (SELECT r.key, r.domain, r.family, r.full_row, r.lead_row
           FROM kinds_open_rows r JOIN kinds_open_keys x ON x.key = r.key AND x.keep
           WHERE EXISTS (SELECT 1 FROM kind_docs d WHERE d.domain = r.domain)),
v AS (SELECT r.key, r.domain,
             sum(CASE WHEN r.family = 'wikidata' THEN CASE WHEN w.strength >= 1.5 THEN 1.0 ELSE 0.5 END
                      WHEN r.full_row THEN 1.0 ELSE 0.5 END) AS votes,
             coalesce(max(w.strength), 0) AS direct,
             bool_or(r.lead_row) AS in_lead
      FROM r LEFT JOIN kinds_open_wd w ON w.key = r.key AND w.domain = r.domain
      GROUP BY 1, 2)
SELECT key, domain, votes::real AS votes,
       (CASE WHEN direct = 2 THEN least(0.95, 0.85 + 0.05 * (votes - 1))
             WHEN votes >= 3 THEN 0.85
             WHEN votes >= 2 THEN 0.75
             WHEN votes >= 1.5 THEN 0.55
             WHEN votes >= 1 THEN 0.5
             ELSE 0.35 END)::real AS conf,
       (0.5 * (direct = 2)::int + 0.3 * in_lead::int + 0.2 * least(1.0, greatest(0.0, votes - 1) / 2))::real AS lead
FROM v WHERE length(key) >= 2;
CREATE INDEX ON kind_members (key);
ANALYZE kind_members;
DROP TABLE IF EXISTS kind_keys;
CREATE TABLE kind_keys AS
SELECT key, string_to_array(key, ' ') AS words, count(*)::int AS n FROM kind_members GROUP BY 1;
CREATE UNIQUE INDEX ON kind_keys (key);
CREATE INDEX kind_keys_words ON kind_keys USING gin (words);
"""


# The optional judge hook (utility/search-kinds-judge.py; the constants must equal its MEMBER_HI/LO, CONF_UP/DOWN,
# LEAD_MIX — tests/unit/test-search-kinds.sh checks). `--judge FILE...` takes judge output rows
# {"stage","key","domain","p"} (any number of models: p is their mean). Nothing here needs a judge: without files
# the build is the plain open build.
JUDGE_HI, JUDGE_LO, JUDGE_CONF_UP, JUDGE_CONF_DOWN, JUDGE_LEAD_MIX = 0.8, 0.2, 0.75, 0.45, 0.5


def load_judge(conn, files: list[str]) -> bool:
    import json
    acc: dict[tuple, list[float]] = {}
    for path in files:
        with open(path, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    r = json.loads(line)
                    acc.setdefault((r["stage"], r["key"], r["domain"]), []).append(float(r["p"]))
    conn.execute("DROP TABLE IF EXISTS kinds_judge")
    conn.execute("CREATE TABLE kinds_judge (stage text, key text, domain text, p real)")
    with conn.cursor().copy("COPY kinds_judge FROM STDIN") as cp:
        for (st, k, d), v in acc.items():
            cp.write_row((st, k, d, sum(v) / len(v)))
    conn.execute("CREATE INDEX ON kinds_judge (stage, key, domain)")
    conn.execute("ANALYZE kinds_judge")
    log(f"kinds_judge: {len(acc)} judged rows from {len(files)} file(s)")
    return bool(acc)


JUDGE_MEMBERS_SQL = f"""
UPDATE kind_members m SET
  conf = CASE WHEN j.p >= {JUDGE_HI} THEN greatest(m.conf, {JUDGE_CONF_UP})
              WHEN j.p <= {JUDGE_LO} THEN least(m.conf, {JUDGE_CONF_DOWN}) ELSE m.conf END
FROM kinds_judge j WHERE j.stage = 'members' AND j.key = m.key AND j.domain = m.domain;
UPDATE kind_members m SET lead = {1 - JUDGE_LEAD_MIX} * m.lead + {JUDGE_LEAD_MIX} * j.p
FROM kinds_judge j WHERE j.stage = 'lead' AND j.key = m.key AND j.domain = m.domain;
"""


def cmd_kinds_open(conn, judge: list[str] | None = None) -> None:
    t0 = time.time()
    judged = load_judge(conn, judge) if judge else False
    for setting in ("enable_nestloop = off", "enable_indexscan = off", "enable_bitmapscan = off",
                    "work_mem = '512MB'", "max_parallel_workers_per_gather = 0"):
        conn.execute(f"SET {setting}")
    conn.execute("CREATE TABLE IF NOT EXISTS kw_country (qid text, country text)")   # optional source
    bad = [r[0] for r in conn.execute("SELECT DISTINCT source FROM site_kinds_new")
           if r[0] not in FAMILY]
    if bad:
        log(f"open build: dropping non-open sources {sorted(bad)}")
        conn.execute("DELETE FROM site_kinds_new WHERE source <> ALL(%s)", (list(FAMILY),))
    conn.execute("DROP TABLE IF EXISTS kinds_open_family")
    conn.execute("CREATE TABLE kinds_open_family (source text PRIMARY KEY, family text)")
    with conn.cursor().copy("COPY kinds_open_family FROM STDIN") as cp:
        for s, f in FAMILY.items():
            cp.write_row((s, f))
    # phrase -> key, for every phrase a site row or a Wikidata class name carries
    conn.execute("DROP TABLE IF EXISTS kinds_open_pkeys")
    conn.execute("CREATE TABLE kinds_open_pkeys (phrase text PRIMARY KEY, key text)")
    phrases = {r[0] for r in conn.execute("SELECT DISTINCT phrase FROM site_kinds_new")}
    for label, aliases in conn.execute("SELECT label, aliases FROM kw_classes"):
        phrases.add(label)
        phrases.update(aliases or [])
    with conn.cursor().copy("COPY kinds_open_pkeys FROM STDIN") as cp:
        for ph in phrases:
            if ph:
                cp.write_row((ph, kind_key(ph)))
    conn.execute("ANALYZE kinds_open_pkeys")
    log(f"kinds_open_pkeys: {len(phrases)} phrases")
    # sites whose owner is not a kind of site leave first (kinds_open_nsd), then the evidence rows
    not_site: set[str] = set()
    conn.execute("CREATE TABLE IF NOT EXISTS kinds_open_nsd (domain text)")
    if conn.execute("SELECT to_regclass('kw_class_roots')").fetchone()[0]:
        roots = {"bad": sorted(NOT_SITE_ROOTS), "good": sorted(SITE_ROOTS)}
        lit = {k: "ARRAY[" + ",".join(f"'{q}'" for q in v) + "]" for k, v in roots.items()}   # QIDs: constants
        conn.execute(NOT_SITE_DOMAINS_SQL.replace("%(bad)s", lit["bad"]).replace("%(good)s", lit["good"]))
        log(f"sites whose owner is not a kind of site: {conn.execute('SELECT count(*) FROM kinds_open_nsd').fetchone()[0]}")
        not_site = {r[0] for r in conn.execute(NOT_SITE_SQL, {"bad": sorted(NOT_SITE_KEY_ROOTS),
                                                              "good": sorted(SITE_ROOTS)})}
    else:
        conn.execute("TRUNCATE kinds_open_nsd")
        log("kw_class_roots missing (search_import.py kinds-roots): the not-a-kind-of-site rules are off")
    conn.execute(ROWS_SQL)
    conn.execute(WD_SQL)
    # 1. the kind-name filter
    places = set(US_STATES) | {kind_key(r[0]) for r in conn.execute("SELECT DISTINCT country FROM kw_country")}
    judged_names = {r[0] for r in conn.execute("SELECT key FROM kinds_judge WHERE stage = 'phrases' AND p >= %s",
                                               (JUDGE_HI,))} if judged else set()
    reasons: dict[str, int] = {}
    keys = []
    for key, sites, fams, wd in conn.execute(KEY_STATS_SQL):
        why = junk_reason(key) or short_reason(key, wd)
        if why is None and key in places:
            why = "place name"
        if why is None and key in not_site:
            why = "not a kind of site (Wikidata tree)"
        if why is None:
            if sites < KEY_MIN_SITES:
                why = "too few sites"
            elif sites > KEY_MAX_SITES:
                why = "too many sites"
            elif wd < WD_CLASS_MIN and fams < FAMILY_MIN:
                why = "one source, not a Wikidata class"
        if why == "one source, not a Wikidata class" and key in judged_names:
            why = None                          # the judge says it is a kind of site: counts as a second family
        reasons[why or "kept"] = reasons.get(why or "kept", 0) + 1
        keys.append((key, why is None, why, sites, fams, wd))
    conn.execute("DROP TABLE IF EXISTS kinds_open_keys")
    conn.execute("CREATE TABLE kinds_open_keys (key text PRIMARY KEY, keep boolean, reason text, sites int, "
                 "families int, wd_sites int)")
    with conn.cursor().copy("COPY kinds_open_keys FROM STDIN") as cp:
        for row in keys:
            cp.write_row(row)
    conn.execute("ANALYZE kinds_open_keys")
    for why, n in sorted(reasons.items(), key=lambda x: -x[1]):
        log(f"kind names: {n} {why}")
    # 2-3. site_kinds, kind_docs, members with conf and lead
    conn.execute(SITE_KINDS_SQL)
    conn.execute(KIND_DOCS_SQL)
    conn.execute("DROP TABLE IF EXISTS kinds_phrase_keys")
    conn.execute("CREATE TABLE kinds_phrase_keys AS SELECT DISTINCT p.phrase, p.key FROM kinds_open_pkeys p "
                 "JOIN site_kinds s USING (phrase)")
    conn.execute(MEMBERS_SQL)
    if judged:
        conn.execute(JUDGE_MEMBERS_SQL)
    m = conn.execute(f"SELECT count(*), count(DISTINCT key), count(*) FILTER (WHERE conf >= {LIFT_CONF}) "
                     "FROM kind_members").fetchone()
    d = conn.execute("SELECT count(*) FROM kind_docs").fetchone()[0]
    log(f"kind_docs: {d} sites; kind_members: {m[0]} pairs over {m[1]} kind names, {m[2]} at conf >= "
        f"{LIFT_CONF}; {time.time() - t0:.0f} s")
