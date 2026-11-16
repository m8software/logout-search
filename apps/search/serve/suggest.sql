-- Prefix completion for instant search (step 2, 2026-09-24). Run on the BUILD
-- database after serve-prep.sql, so serve-refresh / snapshot carry the result.
--
-- suggest_items  one row per destination: a site, a Wikipedia article, a YouTube channel,
--                a subdomain service or a subreddit, with a display name, its popularity
--                within its own kind (pop, 0..1) and a browser-history signal (hist, 0..1)
-- suggest_keys   every lookup key of an item (letters and digits only, lowercased): its
--                name, the name without "The", a site's domain label, a channel's handle
-- suggest_top    for every 3- and 4-character prefix, the best 12 items OF EACH KIND —
--                those prefixes match thousands of keys; from 5 characters an index
--                range scan is cheap
--
-- pop, within each kind:
--   site      1 - ln(Tranco rank) / ln(100001)          rank 1 -> 1.0, rank 100k -> 0
--   article   pg_temp.article_pop: a blend of fame (ln(sitelinks) / ln(250): 250+
--             languages -> 1.0) and readership (ln(1 + monthly views) / ln(1 + 500k):
--             500k+ views -> 1.0), PV_MIX of the latter
--   channel   (log10(followers) - 5) / 4                100k -> 0, 1B -> 1.0
--   service   the site formula on its host rank
--   subreddit (log10(members) - 4) / 4                  10k -> 0
-- hist: ln(1 + browser visits) / ln(100), capped (web results are signed-in only)
-- How kinds weigh against each other is search-api's call (SUGGEST_WEIGHTS), tuned with
-- tools/eval-completion.py — not baked in here, so retuning needs no rebuild. `score` is
-- only the default weighting, used to pick each kind's top 12.
-- A site whose Wikipedia article is its official-site owner is ONE item: the article's
-- name ("Costco"), the site's URL; popularity as explained at the site INSERT.
--
-- demand (2026-09-25): ESTIMATED MONTHLY SEARCHES, one scale for every kind, so a
-- famous Louis can outrank a Louisiana university: an article's search-engine referrals
-- (clickstream, wiki_search_refs); a site's Tranco rank converted by a curve fitted on
-- organisations that have both (ln searches = a + b ln rank); channels and subreddits
-- from followers / members. descr: what the item IS ("musician", "company"), from the
-- Wikidata entity lists. suggest_keys.pos: 0 = the name (or its first word on), 1 = a
-- later word ("armstrong" -> Louis Armstrong), 2 = an alias ("satchmo").

CREATE OR REPLACE FUNCTION pg_temp.tight(s text) RETURNS text LANGUAGE sql IMMUTABLE AS
  $$ SELECT lower(regexp_replace(coalesce(s, ''), '[^[:alnum:]]', '', 'g')) $$;

-- "Franklin D. Roosevelt" -> "fdr": the first letter of each word.
CREATE OR REPLACE FUNCTION pg_temp.initials(title text) RETURNS text LANGUAGE sql IMMUTABLE AS
  $$ SELECT lower(string_agg(left(w, 1), '' ORDER BY o)) FROM regexp_split_to_table(
       regexp_replace(title, '\s*\(.*\)$', ''), '[^[:alnum:]]+') WITH ORDINALITY AS t(w, o) WHERE w <> '' $$;

-- How well known an article is, 0..1. Sitelinks alone favoured what is famous
-- worldwide (Costa Rica, 302 languages) over what is read in English (Costco, 44
-- languages but as many views). PV_MIX of the score comes from August's pageviews;
-- an article with no pageview row falls back to sitelinks alone.
CREATE OR REPLACE FUNCTION pg_temp.article_pop(sl integer, pv bigint) RETURNS double precision LANGUAGE sql IMMUTABLE AS $$
  SELECT CASE WHEN pv IS NULL THEN least(1, ln(greatest(sl, 1)) / ln(250))
              ELSE (1 - 0.5) * least(1, ln(greatest(sl, 1)) / ln(250))
                 + 0.5 * least(1, ln(1 + pv) / ln(1 + 500000)) END
$$;

-- Tranco rank -> 0..1 (rank 1 -> 1, 100k -> 0; unranked or beyond -> 0, history lifts those).
CREATE OR REPLACE FUNCTION pg_temp.rank_pop(r integer) RETURNS double precision LANGUAGE sql IMMUTABLE AS
  $$ SELECT CASE WHEN r IS NULL OR r > 100000 THEN 0 ELSE 1 - ln(r) / ln(100001) END $$;

-- A homepage title is "Home | Costco Travel" or "COS – Modern Style … Arrow pointing right":
-- keep the segment that names the site (contains its domain label, or is contained in it).
CREATE OR REPLACE FUNCTION pg_temp.site_name(title text, domain text) RETURNS text LANGUAGE sql STABLE AS $$
  SELECT seg FROM (
    SELECT btrim(seg) AS seg, ord FROM regexp_split_to_table(coalesce(title, ''), '\s+[|–—:·•-]\s+|\s*\|\s*')
      WITH ORDINALITY AS t(seg, ord)) x
  WHERE length(btrim(seg)) BETWEEN 2 AND 40
    AND length(pg_temp.tight(seg)) >= 3
    AND (strpos(pg_temp.tight(seg), pg_temp.tight(split_part(domain, '.', 1))) > 0
         OR strpos(pg_temp.tight(split_part(domain, '.', 1)), pg_temp.tight(seg)) > 0)
  ORDER BY ord LIMIT 1
$$;

DROP TABLE IF EXISTS suggest_top, suggest_keys, suggest_items;
-- The entity lists (entities.py) are optional: an empty table keeps the alias keys valid.
CREATE TABLE IF NOT EXISTS entities (category text, qid text, name text, aliases text[],
                                     sitelinks integer, extra text, PRIMARY KEY (category, qid));
CREATE TABLE suggest_items (id serial PRIMARY KEY, kind text NOT NULL, name text NOT NULL,
                            url text NOT NULL, domain text, wiki text,
                            pop real NOT NULL, hist real NOT NULL DEFAULT 0, score real,
                            demand real NOT NULL DEFAULT 0, descr text);

-- Search-engine referrals per article (clickstream); empty on a box without them.
CREATE TEMP TABLE sg_refs (title text PRIMARY KEY, searches integer);
DO $$ BEGIN
  IF to_regclass('wiki_search_refs') IS NOT NULL THEN
    INSERT INTO sg_refs SELECT title, searches FROM wiki_search_refs;
  END IF;
END $$;

-- What an article's subject IS, from the Wikidata entity lists: Wikidata's own English
-- description ("American astronaut"), joined on the entity's English Wikipedia article —
-- a name join gave the king "Louis XIV" the band's "band". Parentheticals and
-- anything past 240 characters are trimmed (it was 60 until the audit of 2026-11-15, DB-13: "home depot" and "bank of america" showed
-- descriptions cut mid-word; the engine shortens a long one at a word for the card itself, DESCR_MAX); without a description, the
-- category word.
CREATE TEMP TABLE sg_descr (title text PRIMARY KEY, descr text);
DO $$ BEGIN
  IF to_regclass('entities') IS NOT NULL AND EXISTS (SELECT 1 FROM information_schema.columns
      WHERE table_name = 'entities' AND column_name = 'wiki') THEN
    INSERT INTO sg_descr
      SELECT DISTINCT ON (wiki) wiki, left(btrim(regexp_replace(coalesce(nullif(descr, ''), CASE category
          WHEN 'pro_athlete' THEN 'athlete' WHEN 'musician' THEN 'musician' WHEN 'band' THEN 'band'
          WHEN 'celebrity' THEN 'person' WHEN 'public_company' THEN 'company' WHEN 'restaurant_chain' THEN 'restaurant chain'
          WHEN 'video_game' THEN 'video game' WHEN 'country' THEN 'country' WHEN 'state' THEN 'state'
          WHEN 'vehicle_model' THEN 'vehicle' WHEN 'motorcycle_model' THEN 'motorcycle' WHEN 'news_org' THEN 'news'
          ELSE category END), '\s*\([^)]*\)', '', 'g')), 240)
      FROM entities WHERE coalesce(wiki, '') <> '' ORDER BY wiki, sitelinks DESC;
  END IF;
END $$;

-- Sites: all of Tranco's top 100k that are not blocked and not junk — NOT only the
-- homepages we parsed (costco.com #1,015 and wellsfargo.com #3,152 were never parsed).
CREATE TEMP TABLE sg_cc AS
  SELECT domain, sum(html_200 + redirects) AS pages FROM cc_domain_crawl GROUP BY domain;
ALTER TABLE sg_cc ADD PRIMARY KEY (domain);
CREATE TEMP TABLE sg_cc_any AS SELECT EXISTS (SELECT 1 FROM sg_cc) AS seen;   -- no crawl table rows: no gate
-- From the anchors job, when loaded: how many sites link to each domain with
-- text, and the name a site gives itself (og:site_name). Empty without it.
CREATE TEMP TABLE sg_linked (domain text PRIMARY KEY, sources integer);
CREATE TEMP TABLE sg_self_name (domain text PRIMARY KEY, name text);
DO $$
BEGIN
  IF to_regclass('anchor_docs') IS NOT NULL THEN
    INSERT INTO sg_linked SELECT domain, sources FROM anchor_docs;
  END IF;
  IF to_regclass('site_meta') IS NOT NULL THEN
    INSERT INTO sg_self_name SELECT DISTINCT ON (site) site, value FROM site_meta
      WHERE kind = 'site_name' AND files >= 2 ORDER BY site, files DESC;
  END IF;
END $$;
CREATE TEMP TABLE sg_wiki_domains AS SELECT DISTINCT domain FROM wiki_sites;
ALTER TABLE sg_wiki_domains ADD PRIMARY KEY (domain);
ANALYZE sg_cc; ANALYZE sg_wiki_domains;
CREATE TEMP TABLE sg_site AS
  SELECT s.domain, s.tranco_rank, s.history_visits, d.url, d.title
  FROM sites s
  LEFT JOIN LATERAL (SELECT url, title, error FROM docs WHERE docs.domain = s.domain
                     ORDER BY (error IS NULL) DESC LIMIT 1) d ON true
  -- ... plus every site in the browser history, however small (real destinations that
  -- Tranco's top 100k never lists).
  WHERE (s.tranco_rank <= 100000 OR coalesce(s.history_visits, 0) > 0) AND s.blocked_by IS NULL
    AND coalesce(d.error, '') NOT LIKE 'junk:%'
    -- Plumbing, not a destination: a ranked domain with no Wikipedia article, no visit
    -- in the history and (almost) no web page in Common Crawl's sample — akamai.net,
    -- googletagmanager.com, amazonvideo.com, ytimg.com. Top 120 of those checked
    -- 2026-09-25: all DNS, CDN, ad or telemetry hosts. Not applied while the crawl table is empty.
    -- (NOT EXISTS, an anti-join: NOT IN over 700k domains ran row by row, 12+ min)
    AND NOT (coalesce(s.history_visits, 0) = 0 AND (SELECT seen FROM sg_cc_any)
             AND NOT EXISTS (SELECT 1 FROM sg_wiki_domains wd WHERE wd.domain = s.domain)
             -- linked to by name from 3+ other sites: a destination after all
             AND NOT EXISTS (SELECT 1 FROM sg_linked l WHERE l.domain = s.domain AND l.sources >= 3)
             AND coalesce((SELECT pages FROM sg_cc WHERE sg_cc.domain = s.domain), 0) < 50);

-- The Wikipedia article whose official site is this domain's root (most-known first).
CREATE TEMP TABLE sg_site_wiki AS
  SELECT DISTINCT ON (ws.domain) ws.domain, w.title, coalesce(p.sitelinks, 0) AS sitelinks,
         pg_temp.article_pop(coalesce(p.sitelinks, 0), p.pageviews) AS apop,
         -- the same organisation kinds as search-api's _ORG_BOX: the article's subject IS the site
         coalesce(ws.box, '') ~* 'company|website|organi[sz]ation|airline|bank|retail|agency|software|brand|service|channel|network|university|college|school|club|restaurant|store|magazine|newspaper|sports team|non-profit|charity' AS org
  FROM wiki_sites ws JOIN wiki_docs w USING (page_id) LEFT JOIN wiki_pop p USING (page_id)
  WHERE ws.url ~ '^https?://[^/]+/?$' AND w.title NOT LIKE 'List of%' AND w.title NOT LIKE '.%'
    -- the domain's own front page, not a site hosted under it: jontthomas.s3.amazonaws.com
    -- named amazonaws.com "Jon T. Thomas" (2026-09-25)
    AND lower(substring(ws.url from '^https?://([^/:]+)')) IN (ws.domain, 'www.' || ws.domain)
  ORDER BY ws.domain, pg_temp.article_pop(coalesce(p.sitelinks, 0), p.pageviews) DESC, length(w.title);

-- Tranco rank -> monthly searches: fitted on organisations with both a rank and an
-- article's search referrals. Falls back to a fixed curve (1e7 / rank^0.9) without them.
CREATE TEMP TABLE sg_fit AS
  SELECT coalesce(regr_intercept(ln(r.searches), ln(s.tranco_rank)), ln(1e7)) AS a,
         coalesce(regr_slope(ln(r.searches), ln(s.tranco_rank)), -0.9) AS b,
         count(*) AS n
  FROM sg_site s JOIN sg_site_wiki sw USING (domain) JOIN sg_refs r ON r.title = sw.title
  WHERE sw.org AND s.tranco_rank <= 100000 AND r.searches > 0;

INSERT INTO suggest_items (kind, name, url, domain, wiki, pop, hist)
  SELECT 'site',
         -- the article's name, else the name the site gives itself (og:site_name, when it
         -- names the domain), else its homepage title's naming segment, else the domain
         coalesce(regexp_replace(sw.title, '\s*\(.*\)$', ''), pg_temp.site_name(sn.name, s.domain),
                  pg_temp.site_name(s.title, s.domain), s.domain),
         CASE WHEN s.url ~ '^https?://[^/]+/?$' THEN s.url ELSE 'https://' || s.domain || '/' END,
         s.domain, sw.title,
         -- An organisation's own site: the better of its rank and its article's fame (Costco,
         -- Tranco #1,015, is better known than its rank says). Anything else — a country's
         -- government site, a person's homepage — keeps its own rank plus a small lift: gov.br
         -- took Brazil's fame and outranked Google (2026-09-24).
         CASE WHEN sw.org THEN greatest(pg_temp.rank_pop(s.tranco_rank), coalesce(sw.apop, 0))
              ELSE least(1, pg_temp.rank_pop(s.tranco_rank) + 0.1 * coalesce(sw.apop, 0)) END,
         least(1, ln(1 + coalesce(s.history_visits, 0)) / ln(100))
  FROM sg_site s LEFT JOIN sg_site_wiki sw USING (domain) LEFT JOIN sg_self_name sn USING (domain);
-- A site's demand: its rank on the fitted curve; an organisation's own site takes the
-- better of that and its article's referrals (the same rule as pop above: gov.br must
-- not take Brazil's searches).
UPDATE suggest_items i SET
  demand = CASE WHEN sw.org THEN greatest(coalesce(exp(f.a + f.b * ln(s.tranco_rank)), 0), coalesce(r.searches, 0))
                ELSE coalesce(exp(f.a + f.b * ln(s.tranco_rank)), 0) END,
  descr = d.descr
FROM sg_site s LEFT JOIN sg_site_wiki sw USING (domain) LEFT JOIN sg_refs r ON r.title = sw.title
     LEFT JOIN sg_descr d ON d.title = sw.title CROSS JOIN sg_fit f
WHERE i.kind = 'site' AND i.domain = s.domain;

-- Articles known in 5+ languages, or read 3,000+ times a month (a new US show
-- has few translations), that are not already a site's name (the kind weight keeps the
-- obscure ones low).
INSERT INTO suggest_items (kind, name, url, wiki, pop)
  SELECT 'wiki', w.title, 'https://en.wikipedia.org/wiki/' || replace(w.title, ' ', '_'), w.title,
         pg_temp.article_pop(p.sitelinks, p.pageviews)
  FROM wiki_pop p JOIN wiki_docs w USING (page_id)
  WHERE (p.sitelinks >= 5 OR p.pageviews >= 3000
         OR EXISTS (SELECT 1 FROM sg_refs r WHERE r.title = w.title AND r.searches >= 300))
    AND w.title NOT LIKE 'List of%' AND w.title NOT LIKE '.%'
    -- An organisation's site IS its article (one item). A person's or a place's homepage
    -- is not: "taylor s" put taylorswift.com (a rank-only site) below a university while
    -- the article, with the searches, was left out — both stay, deduped by name.
    AND NOT EXISTS (SELECT 1 FROM suggest_items si JOIN sg_site_wiki sw ON sw.domain = si.domain
                    WHERE si.kind = 'site' AND si.wiki = w.title AND sw.org);
UPDATE suggest_items i SET demand = coalesce(r.searches, 0), descr = d.descr
FROM suggest_items x LEFT JOIN sg_refs r ON r.title = x.wiki LEFT JOIN sg_descr d ON d.title = x.wiki
WHERE i.id = x.id AND i.kind = 'wiki';

DO $$
BEGIN
  IF to_regclass('youtube_channels') IS NOT NULL THEN
    INSERT INTO suggest_items (kind, name, url, pop)
      SELECT 'youtube', name,
             CASE WHEN handle IS NOT NULL THEN 'https://www.youtube.com/@' || handle
                  ELSE 'https://www.youtube.com/channel/' || channel_id END,
             greatest(0, least(1, (log(followers) - 5) / 4))
      FROM youtube_channels WHERE followers >= 100000;
  END IF;
  IF to_regclass('host_nav') IS NOT NULL THEN
    INSERT INTO suggest_items (kind, name, url, domain, pop)
      SELECT DISTINCT ON (url) 'service', coalesce(nullif(btrim(split_part(title, ' - ', 1)), ''), url), url,
             split_part(url, '/', 3), 1 - ln(rank) / ln(100001)
      FROM host_nav WHERE rank <= 100000 ORDER BY url, rank;
  END IF;
  IF to_regclass('subreddits') IS NOT NULL THEN
    INSERT INTO suggest_items (kind, name, url, pop)
      SELECT 'subreddit', 'r/' || name, 'https://www.reddit.com/r/' || name || '/',
             greatest(0, least(1, (log(subscribers) - 4) / 4))
      FROM subreddits WHERE subscribers >= 50000;
  END IF;
END $$;
-- Channels: their article's searches when the name is an article, else followers / 2000;
-- services: the rank curve at a third; subreddits: members / 500 (rough, and marked so).
UPDATE suggest_items i SET demand = greatest(coalesce(r.searches, 0),
         coalesce((SELECT followers FROM youtube_channels y WHERE i.url LIKE '%' || coalesce(y.handle, y.channel_id)
                   LIMIT 1), 0) / 2000.0), descr = coalesce(d.descr, 'YouTube channel')
FROM suggest_items x LEFT JOIN sg_refs r ON r.title = x.name LEFT JOIN sg_descr d ON d.title = x.name
WHERE i.id = x.id AND i.kind = 'youtube';
UPDATE suggest_items SET demand = exp(f.a + f.b * ln(greatest(1, round(exp((1 - pop) * ln(100001)))))) / 3.0
FROM sg_fit f WHERE kind = 'service';
UPDATE suggest_items SET demand = power(10, pop * 4 + 4) / 500.0, descr = 'subreddit' WHERE kind = 'subreddit';
-- score: ONE scale now (log10 of monthly searches) plus the history lift; it only picks
-- each kind's top 12 per prefix — search-api does the final ranking.
UPDATE suggest_items SET score = log(1 + demand) + 1.5 * hist;

-- Keys. Every item: its name, and the name without a leading "The". Articles: their
-- redirect names (below). Sites: the domain
-- label too (typing "cost" must find Costco even when the name is "Costco Wholesale").
-- Channels: the handle. Services: host_nav's keys ("youtubetv"). Subreddits: "r" + name.
CREATE TABLE suggest_keys AS
  SELECT key, id, kind, score, min(pos)::smallint AS pos FROM (
    SELECT pg_temp.tight(regexp_replace(name, '\s*\(.*\)$', '')) AS key, id, kind, score, 0 AS pos FROM suggest_items
    UNION ALL
    SELECT pg_temp.tight(regexp_replace(regexp_replace(name, '\s*\(.*\)$', ''), '^the\s+', '', 'i')), id, kind, score, 0
      FROM suggest_items WHERE name ~* '^the\s'
    UNION ALL
    SELECT pg_temp.tight(split_part(domain, '.', 1)), id, kind, score, 0 FROM suggest_items WHERE kind = 'site'
    UNION ALL
    SELECT pg_temp.tight(substring(url FROM '/@([^/]+)$')), id, kind, score, 0 FROM suggest_items WHERE kind = 'youtube'
    UNION ALL
    SELECT h.key, i.id, i.kind, i.score, 0 FROM suggest_items i JOIN host_nav h ON h.url = i.url WHERE i.kind = 'service'
    UNION ALL
    -- Later words of a well-known name: "armstrong" -> Louis Armstrong, "ck" ->
    -- Louis C.K., "swift" -> Taylor Swift. 500+ searches a month only: a later word of
    -- every name would bury first-word matches under noise.
    SELECT pg_temp.tight(array_to_string(w[k:], ' ')), id, kind, score, 1
    FROM (SELECT id, kind, score, regexp_split_to_array(btrim(regexp_replace(name, '\s*\(.*\)$', '')), '\s+') AS w
          FROM suggest_items WHERE demand >= 500 AND kind IN ('wiki', 'site', 'youtube')) x
    CROSS JOIN LATERAL generate_series(2, coalesce(array_length(w, 1), 1)) k
    UNION ALL
    -- A well-known entity's English aliases from Wikidata: "satchmo" -> Louis
    -- Armstrong, "kfc" -> KFC's article. Aliases matched by the item's article title.
    SELECT pg_temp.tight(al), i.id, i.kind, i.score, 2
    FROM suggest_items i JOIN entities e ON e.name = coalesce(i.wiki, i.name)
    CROSS JOIN LATERAL unnest(e.aliases[1:8]) al
    WHERE i.demand >= 500 AND i.kind IN ('wiki', 'site', 'youtube') AND length(pg_temp.tight(al)) >= 3
    UNION ALL
    -- An article's redirect names, only when the redirect is PART of the title ("cowboys"
    -- -> Dallas Cowboys) or its initials ("fdr" -> Franklin D. Roosevelt). Redirects in
    -- general are anything: "Craig Crimmins" -> Metropolitan Opera, "Thinkstock" -> Getty
    -- Images, "CR Airways" -> Hong Kong Airlines hijacked "crai" / "thin" (2026-09-24).
    -- Articles known in ~16+ languages (pop >= 0.5) and sites named after their article.
    SELECT pg_temp.tight(al), i.id, i.kind, i.score, CASE WHEN pg_temp.tight(al) = pg_temp.initials(w.title) THEN 0 ELSE 1 END
    FROM suggest_items i JOIN wiki_docs w ON w.title = i.wiki
    CROSS JOIN LATERAL unnest(string_to_array(w.aliases, ' | ')) al
    WHERE w.aliases <> '' AND (i.kind = 'site' OR i.pop >= 0.5)
      AND length(pg_temp.tight(al)) >= 3
      AND (strpos(pg_temp.tight(regexp_replace(w.title, '\s*\(.*\)$', '')), pg_temp.tight(al)) > 0
           OR pg_temp.tight(al) = pg_temp.initials(w.title))
  ) k
  WHERE length(key) >= 2
  GROUP BY key, id, kind, score;
CREATE INDEX suggest_keys_key ON suggest_keys (key text_pattern_ops);
-- the card over an exact answer looks its description up by article (search-api CARD_DESCR_SQL)
CREATE INDEX suggest_items_wiki ON suggest_items (wiki) WHERE wiki IS NOT NULL;
-- Typo correction: the closest key by trigram similarity ("decathhlon" -> decathlon).
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE INDEX suggest_keys_trgm ON suggest_keys USING gin (key gin_trgm_ops);

-- ids and, aligned with them, how each matched (pos). A later-word or alias match counts
-- one point (a factor of ten in searches) less when picking each kind's top 12.
CREATE TABLE suggest_top AS
  SELECT prefix, array_agg(id ORDER BY score - pos DESC, id) AS ids, array_agg(pos ORDER BY score - pos DESC, id) AS pos FROM (
    SELECT prefix, id, score, pos, row_number() OVER (PARTITION BY prefix, kind ORDER BY score - pos DESC, id) AS rn FROM (
      SELECT DISTINCT ON (left(key, n), id) left(key, n) AS prefix, id, kind, score, pos
      FROM suggest_keys CROSS JOIN (VALUES (3), (4)) v(n)
      WHERE length(key) >= n
      ORDER BY left(key, n), id, pos, score DESC) x) y
  WHERE rn <= 12
  GROUP BY prefix;
ALTER TABLE suggest_top ADD PRIMARY KEY (prefix);
ANALYZE suggest_items; ANALYZE suggest_keys; ANALYZE suggest_top;

SELECT kind, count(*) FROM suggest_items GROUP BY kind ORDER BY 2 DESC;
SELECT 'keys ' || count(*) FROM suggest_keys;
SELECT 'prefixes ' || count(*) FROM suggest_top;
