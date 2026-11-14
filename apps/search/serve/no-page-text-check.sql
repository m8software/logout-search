-- Prints how many sampled page bodies are still readable text (0 = safe to ship). Fails
-- outright when page_words() is missing, i.e. box-profile.sql never ran (ADR-059).
-- Used by tools/box-bench.sh dump and infra/search-public/server.sh adopt.
SELECT (SELECT count(*) FROM (SELECT body FROM docs WHERE body IS NOT NULL ORDER BY random() LIMIT 2000) s
         WHERE body IS DISTINCT FROM page_words(body))
     + (SELECT count(*) FROM (SELECT body FROM page_docs WHERE body IS NOT NULL ORDER BY random() LIMIT 2000) s
         WHERE body IS DISTINCT FROM page_words(body));
