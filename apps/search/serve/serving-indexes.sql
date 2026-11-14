-- Indexes only the SERVING side needs (search-api's ranking), built after every
-- restore so the build side's snapshot stays generic. Used by serve/deploy.sh (restore on
-- the SearXNG box) and ../deploy.sh serve-restore (indexhost). See serve/deploy.sh for what
-- each index is for.
CREATE INDEX IF NOT EXISTS docs_label ON docs (split_part(domain, '.', 1));
CREATE INDEX IF NOT EXISTS docs_label_trgm ON docs USING gin (split_part(domain, '.', 1) gin_trgm_ops);
CREATE INDEX IF NOT EXISTS docs_head_bm25 ON docs USING bm25
  ((regexp_replace(domain, '[.-]+', ' ', 'g') || ' ' || coalesce(title, '') || ' ' || coalesce(h1, '')))
  WITH (text_config = 'english');
DO $$
BEGIN
  IF to_regclass('wiki_docs') IS NOT NULL AND to_regclass('wiki_aliases') IS NULL THEN
    CREATE TABLE wiki_aliases AS
      SELECT DISTINCT lower(regexp_replace(t, '[^[:alnum:]]', '', 'g')) AS norm, page_id
      FROM (SELECT page_id, title AS t FROM wiki_docs
            UNION ALL SELECT page_id, unnest(string_to_array(aliases, ' | ')) FROM wiki_docs WHERE aliases <> '') x
      WHERE t <> '';
    CREATE INDEX wiki_aliases_norm ON wiki_aliases (norm);
  END IF;
END $$;
-- Places near the searcher (search-api PLACES_SQL, 2026-09-26). A brand index, so a name
-- query is answered by the name / brand / trigram indexes together (PLACE_MATCH_NAME) instead
-- of testing every place in the box; and the table in map order, so the places inside one
-- 40 km box sit on a few thousand pages instead of spread across the whole table.
-- Measured on a copy of indexhost's places (4.59M): "wawa"-style names p50 300 -> 30 ms,
-- p95 1,093 -> 44 ms; "restaurants"-style kinds p95 380 -> 63 ms; the same rows returned.
-- The GiST index only gives CLUSTER its map order — no query uses it, so it is dropped.
DO $$
BEGIN
  IF to_regclass('places') IS NOT NULL THEN
    CREATE INDEX IF NOT EXISTS places_brand_lower ON places (lower(brand)) WHERE brand <> '';
    CREATE INDEX places_geo_order ON places USING gist (point(lon, lat));
    CLUSTER places USING places_geo_order;
    DROP INDEX places_geo_order;
    ANALYZE places;
  END IF;
END $$;
ANALYZE docs;
