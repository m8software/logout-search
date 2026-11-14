-- Page-text variants for the A/B (search-api ?body=bow|none, eval-queries.py --body).
-- Run against the serve database; search-api reads these only when asked.
--   bow  = each page's words, lower-cased, stripped of edge punctuation, sorted: the same
--          words and counts for BM25, no sentences a reader could recover.
--   none = no page text: domain words, title, description and h1 only.
-- Failed fetches are left out, so the tables need no error column.
\timing on
SET maintenance_work_mem = '1GB';

DROP TABLE IF EXISTS docs_body_bow, docs_body_none, page_docs_body_bow, page_docs_body_none;

CREATE TABLE docs_body_bow AS
SELECT d.domain, d.url, d.title, d.description,
       regexp_replace(d.domain, '[.-]+', ' ', 'g') || ' ' || coalesce(d.title, '') || ' ' ||
       coalesce(d.description, '') || ' ' || coalesce(d.h1, '') || ' ' ||
       coalesce((SELECT string_agg(w, ' ' ORDER BY w)
                   FROM (SELECT lower(regexp_replace(x, '^\W+|\W+$', '', 'g')) AS w
                           FROM regexp_split_to_table(d.body, '\s+') x) t
                  WHERE w <> ''), '') AS search_text
FROM docs d WHERE d.error IS NULL;

CREATE TABLE docs_body_none AS
SELECT d.domain, d.url, d.title, d.description,
       regexp_replace(d.domain, '[.-]+', ' ', 'g') || ' ' || coalesce(d.title, '') || ' ' ||
       coalesce(d.description, '') || ' ' || coalesce(d.h1, '') AS search_text
FROM docs d WHERE d.error IS NULL;

CREATE TABLE page_docs_body_bow AS
SELECT p.url, p.title, p.description,
       coalesce(p.title, '') || ' ' || coalesce(p.description, '') || ' ' || coalesce(p.h1, '') || ' ' ||
       coalesce((SELECT string_agg(w, ' ' ORDER BY w)
                   FROM (SELECT lower(regexp_replace(x, '^\W+|\W+$', '', 'g')) AS w
                           FROM regexp_split_to_table(p.body, '\s+') x) t
                  WHERE w <> ''), '') AS search_text
FROM page_docs p WHERE p.error IS NULL;

CREATE TABLE page_docs_body_none AS
SELECT p.url, p.title, p.description,
       coalesce(p.title, '') || ' ' || coalesce(p.description, '') || ' ' || coalesce(p.h1, '') AS search_text
FROM page_docs p WHERE p.error IS NULL;

CREATE INDEX docs_body_bow_bm25 ON docs_body_bow USING bm25 (search_text) WITH (text_config = 'english');
CREATE INDEX docs_body_none_bm25 ON docs_body_none USING bm25 (search_text) WITH (text_config = 'english');
CREATE INDEX page_docs_body_bow_bm25 ON page_docs_body_bow USING bm25 (search_text) WITH (text_config = 'english');
CREATE INDEX page_docs_body_none_bm25 ON page_docs_body_none USING bm25 (search_text) WITH (text_config = 'english');
ANALYZE docs_body_bow, docs_body_none, page_docs_body_bow, page_docs_body_none;

SELECT 'docs full/bow/none rows', (SELECT count(*) FROM docs WHERE error IS NULL),
       (SELECT count(*) FROM docs_body_bow), (SELECT count(*) FROM docs_body_none);
SELECT 'page_docs full/bow/none rows', (SELECT count(*) FROM page_docs WHERE error IS NULL),
       (SELECT count(*) FROM page_docs_body_bow), (SELECT count(*) FROM page_docs_body_none);
SELECT 'done';
