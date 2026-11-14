-- Run on the BUILD database before anything is served from it — by `deploy.sh
-- snapshot` (the portable pg_dump) and `deploy.sh serve-refresh` (indexhost's file copy).
-- Never ship a blocked site (ads/trackers/threats/adult/gambling), even if a fetch
-- that started before the block stored its homepage.
DELETE FROM docs d USING sites s WHERE s.domain = d.domain AND s.blocked_by IS NOT NULL;
-- Same for an article's official website (wiki_sites is built before a blocklist refresh).
DO $$
BEGIN
  IF to_regclass('wiki_sites') IS NOT NULL THEN
    DELETE FROM wiki_sites w USING sites s WHERE s.domain = w.domain AND s.blocked_by IS NOT NULL;
  END IF;
  IF to_regclass('page_docs') IS NOT NULL THEN
    DELETE FROM page_docs p USING sites s WHERE s.domain = p.domain AND s.blocked_by IS NOT NULL;
  END IF;
END $$;
DROP TABLE IF EXISTS serve_sites;
CREATE TABLE serve_sites AS
  SELECT s.domain, s.tranco_rank, s.cc_hc_pos, s.history_visits
  FROM sites s WHERE s.blocked_by IS NULL
    AND EXISTS (SELECT 1 FROM docs d WHERE d.domain = s.domain AND d.error IS NULL);
ALTER TABLE serve_sites ADD PRIMARY KEY (domain);

-- Navigation for EVERY Tranco domain and subdomain host ("typing in the
-- name of each of the top million domains and subdomains from tranco should have its
-- domain as the top result"). 63% of Tranco domains have no parsed homepage, so
-- serve_sites (indexed sites only) could never put them first. key = the domain's first
-- label ("costco"), or for a subdomain host its labels run together both ways
-- ("youtubetv", "tvyoutube"). Blocked domains (ads/trackers/threats/adult/gambling) never.
DROP TABLE IF EXISTS nav_sites;
CREATE TABLE nav_sites AS
  SELECT s.domain AS host, split_part(s.domain, '.', 1) AS key, s.tranco_rank AS rank, s.domain
  FROM sites s WHERE s.blocked_by IS NULL AND s.tranco_rank IS NOT NULL;
DO $$
BEGIN
  IF to_regclass('tranco_hosts') IS NOT NULL THEN
    INSERT INTO nav_sites
      SELECT th.host, k, th.rank, th.domain
      FROM tranco_hosts th JOIN sites s ON s.domain = th.domain AND s.blocked_by IS NULL
      CROSS JOIN LATERAL (SELECT replace(left(th.host, length(th.host) - length(th.domain) - 1), '.', '') AS sub,
                                 split_part(th.domain, '.', 1) AS dl) x
      CROSS JOIN LATERAL unnest(ARRAY[x.sub || x.dl, x.dl || x.sub]) k;
  END IF;
END $$;
CREATE INDEX nav_sites_key ON nav_sites (key, rank);
CREATE INDEX nav_sites_host ON nav_sites (host);

-- Navigation to subdomain services (Tranco WITH subdomains, 2026-09-24): "youtube tv" ->
-- tv.youtube.com, "google drive" -> drive.google.com. Keys = the subdomain labels
-- joined with the domain's label, both orders. Only hosts whose front page parsed.
DO $$
BEGIN
  IF to_regclass('cc_pages') IS NOT NULL AND to_regclass('page_docs') IS NOT NULL
     AND to_regclass('tranco_hosts') IS NOT NULL THEN
    DROP TABLE IF EXISTS host_nav;
    CREATE TABLE host_nav AS
      SELECT DISTINCT ON (k, p.url) k AS key, p.url, pd.title, pd.description, th.rank
      FROM cc_pages p
      JOIN page_docs pd ON pd.url = p.url AND pd.error IS NULL
      JOIN tranco_hosts th ON th.host = p.host
      CROSS JOIN LATERAL (SELECT replace(left(p.host, length(p.host) - length(p.domain) - 1), '.', '') AS sub,
                                 split_part(p.domain, '.', 1) AS dl) x
      CROSS JOIN LATERAL unnest(ARRAY[x.sub || x.dl, x.dl || x.sub]) k
      WHERE p.kind = 'host'
      ORDER BY k, p.url;
    CREATE INDEX host_nav_key ON host_nav (key);
  END IF;
END $$;

-- Exact-title lookups (search-api RELATED_SQL joins wiki_next to wiki_docs by title): the
-- trigram index answered equality in 274 ms warm and ran past the 2 s statement limit cold,
-- which failed the whole "paul rudd" answer (2026-09-25).
CREATE INDEX IF NOT EXISTS wiki_docs_title ON wiki_docs (title);
