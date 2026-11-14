-- Box baseline (docs/internal/SEARCH-BOX-PLAN.md): what a customer box would be
-- served — no personal data. Runs on a COPY of the build database (`deploy.sh box-build`)
-- BEFORE serve-prep.sql and suggest.sql, so nothing downstream is built from personal
-- signals: the browser-history boost and history-only suggestions (suggest.sql admits a
-- site that is only in the history) both come from history_visits.
DO $$
DECLARE r record;
BEGIN
  FOR r IN SELECT table_name FROM information_schema.columns
           WHERE table_schema = 'public' AND column_name = 'history_visits' LOOP
    -- 0, not NULL: sites.history_visits is NOT NULL DEFAULT 0 (0 = never visited everywhere)
    EXECUTE format('UPDATE %I SET history_visits = 0 WHERE history_visits <> 0', r.table_name);
    RAISE NOTICE 'history_visits cleared in %', r.table_name;
  END LOOP;
END $$;
-- Reddit metadata never ships.
DROP TABLE IF EXISTS subreddits;
-- No readable page text ships (DECISIONS ADR-059). docs.body / page_docs.body hold
-- the first ~2,000 characters of other sites' pages; search-api only matches them, never
-- shows them. What ships is each page's words sorted, with their counts (page_words):
-- BM25 sees the same terms, a reader gets no sentences. Measured 2026-09-29: no labelled
-- query lost its answer, 198/200 topic results and every #1 unchanged. search_text is
-- generated from body, so it follows. Idempotent: page_words(page_words(x)) = page_words(x).
-- The check that refuses anything else: serve/no-page-text-check.sql.
CREATE OR REPLACE FUNCTION page_words(t text) RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
  SELECT coalesce(string_agg(w, ' ' ORDER BY w COLLATE "C"), '')
  FROM (SELECT lower(regexp_replace(x, '^\W+|\W+$', '', 'g')) AS w
          FROM regexp_split_to_table(t, '\s+') AS x) s
  WHERE w <> ''
$$;
-- BM25 indexes are dropped for the rewrite and rebuilt from their own definitions after it:
-- 626k row updates through them would take far longer than one rebuild.
CREATE TEMP TABLE page_text_idx AS
  SELECT indexname, indexdef FROM pg_indexes
  WHERE schemaname = 'public' AND tablename IN ('docs', 'page_docs') AND indexdef ~ 'USING bm25';
DO $$
DECLARE r record;
BEGIN
  FOR r IN SELECT indexname FROM page_text_idx LOOP
    EXECUTE format('DROP INDEX %I', r.indexname);
  END LOOP;
END $$;
UPDATE docs SET body = page_words(body) WHERE body IS NOT NULL AND body <> page_words(body);
UPDATE page_docs SET body = page_words(body) WHERE body IS NOT NULL AND body <> page_words(body);
-- VACUUM FULL, not VACUUM: the old row versions (the readable text) stay in the data files
-- until the table is rewritten, and a box is shipped as those files.
VACUUM FULL docs;
VACUUM FULL page_docs;
DO $$
DECLARE r record;
BEGIN
  FOR r IN SELECT indexdef FROM page_text_idx LOOP
    EXECUTE r.indexdef;
  END LOOP;
END $$;
DROP TABLE page_text_idx;
