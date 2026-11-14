-- Release profile — runs on the THROWAWAY CLONE's `serve` database (run-proto.sh), never on
-- the live one. It makes the source pass export.py's guard (check_source):
--   * no readable page text (ADR-059): docs.body / page_docs.body -> page_words(), the same
--     function and rewrite as serve/box-profile.sql;
--   * no browsing history: history_visits = 0 in every table that has the column.
-- Unlike box-profile.sql it neither VACUUM FULLs nor rebuilds the BM25 indexes: the clone's
-- data files are never shipped (only export.py's output is), and the export does not use
-- those indexes. They are dropped so the 626k-row rewrite does not update them.
CREATE OR REPLACE FUNCTION page_words(t text) RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
  SELECT coalesce(string_agg(w, ' ' ORDER BY w COLLATE "C"), '')
  FROM (SELECT lower(regexp_replace(x, '^\W+|\W+$', '', 'g')) AS w
          FROM regexp_split_to_table(t, '\s+') AS x) s
  WHERE w <> ''
$$;
DO $$
DECLARE r record;
BEGIN
  FOR r IN SELECT indexname FROM pg_indexes
           WHERE schemaname = 'public' AND tablename IN ('docs', 'page_docs') AND indexdef ~ 'USING bm25' LOOP
    EXECUTE format('DROP INDEX %I', r.indexname);
  END LOOP;
END $$;
UPDATE docs SET body = page_words(body) WHERE body IS NOT NULL AND body <> page_words(body);
UPDATE page_docs SET body = page_words(body) WHERE body IS NOT NULL AND body <> page_words(body);
DO $$
DECLARE r record;
BEGIN
  FOR r IN SELECT table_name FROM information_schema.columns
           WHERE table_schema = 'public' AND column_name = 'history_visits' LOOP
    EXECUTE format('UPDATE %I SET history_visits = 0 WHERE history_visits <> 0', r.table_name);
  END LOOP;
END $$;
