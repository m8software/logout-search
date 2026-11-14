-- Simulated full-signals month (T5) — THROWAWAY CLONE ONLY: the search database's pageviews.
UPDATE wiki_pop SET pageviews = coalesce(pageviews, 0) + 1;
