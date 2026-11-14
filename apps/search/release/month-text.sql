-- Simulated 2% text month (T5 delta measurement) — THROWAWAY CLONE ONLY (export.py profile
-- refuses any other host; run-proto.sh snapshots the clone first and rolls it back after).
-- 2% of articles get new lead text, 0.2% are renamed, 0.2% deleted, 0.2% new. month_changes
-- lists every touched page_id for the Tantivy delta (release_v1.py tvmonth).
DO $$ DECLARE r record; BEGIN
  FOR r IN SELECT indexname FROM pg_indexes WHERE tablename = 'wiki_docs' AND indexdef ~ 'USING bm25' LOOP
    EXECUTE format('DROP INDEX %I', r.indexname);      -- not used by the export; spares 136k BM25 updates
  END LOOP;
END $$;
DROP TABLE IF EXISTS month_changes;
CREATE TABLE month_changes (page_id bigint PRIMARY KEY, op text NOT NULL);
INSERT INTO month_changes SELECT page_id, 'change' FROM wiki_docs WHERE page_id % 50 = 7;
INSERT INTO month_changes SELECT page_id, 'change' FROM wiki_docs WHERE page_id % 500 = 11;
INSERT INTO month_changes SELECT page_id, 'delete' FROM wiki_docs WHERE page_id % 500 = 13;
INSERT INTO month_changes SELECT page_id + 900000000, 'new' FROM wiki_docs WHERE page_id % 500 = 17;
UPDATE wiki_docs SET lead = coalesce(lead, '') || ' Updated in the simulated month.' WHERE page_id % 50 = 7;
UPDATE wiki_docs SET title = title || ' (renamed)' WHERE page_id % 500 = 11;
INSERT INTO wiki_docs (page_id, rev_id, title, headings, lead, infobox, dump, aliases)
  SELECT page_id + 900000000, rev_id, title || ' (new article)', headings, lead, infobox, dump, aliases
  FROM wiki_docs WHERE page_id % 500 = 17;
DELETE FROM wiki_docs WHERE page_id % 500 = 13;
