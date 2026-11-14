-- Box profile, phase 3 (docs/internal/SEARCH-BOX-PLAN.md): keep the most-read Wikipedia
-- articles only. LOSSY — ships only if the accuracy gate says the loss is within noise.
--
-- keep_n        articles kept, by last month's pageviews, then language editions (sitelinks)
-- keep_official 1 = also keep every article that names an official site (wiki_sites):
--               "costco" -> Costco's article -> costco.com runs through them
-- A variant is a copy of this file with other values below (bench-run: variant_profile).
-- Needs wiki_pop (page_id, sitelinks, pageviews), which the bench base keeps for this.
--
-- What follows the article set: vectors, aliases, official sites, facts, clickstream
-- "next" links. What does not: suggestions (they link to Wikipedia itself, not to a row
-- here) and wiki_links (Wikidata accounts, measured on their own).
--
-- Each table is COPIED (kept rows only) and its indexes rebuilt, not DELETEd + VACUUM
-- FULLed: the delete of 4.15M of 6.79M articles ran 41+ min on its own (every row's
-- out-of-line text too), and VACUUM FULL would then read and write the table again
-- (2026-09-26). The copy writes the kept third once.
\set keep_n 1000000
\set keep_official 1
SET maintenance_work_mem = '2GB';   -- the HNSW rebuild (was 4GB: indexhost froze 2026-09-26 with several heavy jobs at once)

CREATE TEMP TABLE keep AS
  SELECT page_id FROM (
    SELECT w.page_id FROM wiki_docs w LEFT JOIN wiki_pop p USING (page_id)
    ORDER BY coalesce(p.pageviews, 0) DESC, coalesce(p.sitelinks, 0) DESC, w.page_id
    LIMIT :keep_n) top
  UNION
  SELECT page_id FROM wiki_sites WHERE :keep_official = 1 AND page_id IS NOT NULL;
ALTER TABLE keep ADD PRIMARY KEY (page_id);
-- DISTINCT: wiki_docs titles are not unique (the unique index failed, 2026-09-26)
CREATE TEMP TABLE keep_title AS SELECT DISTINCT w.title FROM wiki_docs w JOIN keep USING (page_id);
ALTER TABLE keep_title ADD PRIMARY KEY (title);
ANALYZE keep; ANALYZE keep_title;
SELECT 'keeping ' || count(*) || ' articles' FROM keep;

-- prune(table, condition on alias t): copy the kept rows, swap the table, rebuild every
-- index it had under the same name (unique ones stay unique; constraints become indexes,
-- which is all serving needs).
CREATE FUNCTION pg_temp.prune(tbl text, cond text) RETURNS void LANGUAGE plpgsql AS $$
DECLARE
  defs text[];
  d text;
  n_before bigint;
  n_after bigint;
BEGIN
  SELECT array_agg(pg_get_indexdef(i.indexrelid) ORDER BY pg_relation_size(i.indexrelid))
    INTO defs FROM pg_index i WHERE i.indrelid = tbl::regclass;
  EXECUTE format('SELECT count(*) FROM %I', tbl) INTO n_before;
  EXECUTE format('CREATE TABLE %I AS SELECT t.* FROM %I t WHERE %s', tbl || '_kept', tbl, cond);
  EXECUTE format('DROP TABLE %I', tbl);
  EXECUTE format('ALTER TABLE %I RENAME TO %I', tbl || '_kept', tbl);
  FOREACH d IN ARRAY coalesce(defs, '{}') LOOP
    EXECUTE d;
  END LOOP;
  EXECUTE format('SELECT count(*) FROM %I', tbl) INTO n_after;
  RAISE NOTICE '%: % -> % rows, % indexes rebuilt', tbl, n_before, n_after, coalesce(array_length(defs, 1), 0);
END $$;

SELECT pg_temp.prune('wiki_docs', 'EXISTS (SELECT 1 FROM keep k WHERE k.page_id = t.page_id)');
SELECT pg_temp.prune('wiki_vec', 'EXISTS (SELECT 1 FROM keep k WHERE k.page_id = t.id)');
SELECT pg_temp.prune('wiki_aliases', 'EXISTS (SELECT 1 FROM keep k WHERE k.page_id = t.page_id)');
SELECT pg_temp.prune('wiki_sites', 'EXISTS (SELECT 1 FROM keep k WHERE k.page_id = t.page_id)');
SELECT pg_temp.prune('wiki_facts', 'EXISTS (SELECT 1 FROM keep_title k WHERE k.title = t.title)');
SELECT pg_temp.prune('wiki_next', 'EXISTS (SELECT 1 FROM keep_title k WHERE k.title = t.title) '
                                  'AND EXISTS (SELECT 1 FROM keep_title k WHERE k.title = t.next_title)');
DO $$ BEGIN   -- card pictures (card_images): only for the articles kept; absent before its first import
  IF to_regclass('card_images') IS NOT NULL THEN
    PERFORM pg_temp.prune('card_images', 'EXISTS (SELECT 1 FROM keep_title k WHERE k.title = t.title)');
  END IF;
END $$;
DROP TABLE IF EXISTS wiki_pop;   -- bench-only: nothing serves from it
VACUUM (ANALYZE);
