-- Box profile, phase 2 (docs/internal/SEARCH-BOX-PLAN.md): LOSSLESS size cuts. Results
-- must be identical to the unpruned box baseline, not merely close.
--
-- 1. Stored search_text columns: the build side keeps each table's full text a second time
--    as a generated column (~1.5 KB per Wikipedia article). The BM25 index is built on the
--    same expression instead, under the same name; search-api names the expression where the
--    column is gone (TEXT_FORMS). DROP COLUMN alone frees nothing — VACUUM FULL rewrites the
--    table with the dropped values gone.
-- 2. Build-only columns nobody reads when serving: wiki_docs.dump, wiki_docs.rev_id.
ALTER TABLE wiki_docs DROP COLUMN search_text, DROP COLUMN IF EXISTS dump, DROP COLUMN IF EXISTS rev_id;
ALTER TABLE docs DROP COLUMN search_text;
ALTER TABLE page_docs DROP COLUMN search_text;
VACUUM FULL wiki_docs;
VACUUM FULL docs;
VACUUM FULL page_docs;
-- The expressions below must stay identical to search_api.TEXT_FORMS and to the generated
-- columns they replace (importer/schema.sql, importer/wikiparse.py).
CREATE INDEX wiki_docs_bm25 ON wiki_docs USING bm25
  ((title || ' ' || aliases || ' ' || coalesce(infobox, '') || ' ' || coalesce(headings, '') || ' ' || coalesce(lead, '')))
  WITH (text_config = 'english');
CREATE INDEX docs_bm25 ON docs USING bm25
  ((regexp_replace(domain, '[.-]+', ' ', 'g') || ' ' || coalesce(title, '') || ' ' || coalesce(description, '') || ' ' ||
    coalesce(h1, '') || ' ' || coalesce(body, '')))
  WITH (text_config = 'english');
CREATE INDEX page_docs_bm25 ON page_docs USING bm25
  ((coalesce(title, '') || ' ' || coalesce(description, '') || ' ' || coalesce(h1, '') || ' ' || coalesce(body, '')))
  WITH (text_config = 'english');
VACUUM (ANALYZE) wiki_docs;
VACUUM (ANALYZE) docs;
VACUUM (ANALYZE) page_docs;
