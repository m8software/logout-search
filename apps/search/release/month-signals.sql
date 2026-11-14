-- Simulated full-signals month (T5) — THROWAWAY CLONE ONLY, after month-text.sql: every
-- popularity number changes, no text does. serve database.
UPDATE serve_sites SET tranco_rank = tranco_rank + 1 WHERE tranco_rank IS NOT NULL;
UPDATE serve_sites SET cc_hc_pos = cc_hc_pos + 1 WHERE cc_hc_pos IS NOT NULL;
UPDATE wiki_sites SET tranco_rank = tranco_rank + 1 WHERE tranco_rank IS NOT NULL;
UPDATE suggest_items SET pop = pop * 1.01, score = score * 1.01, demand = demand * 1.01;
UPDATE suggest_keys SET score = score * 1.01;
