-- Phrase completions (step 4): as you type "air fr", the phrases people actually
-- searched that start with it ("air fryer", "air fryer oven"). Destinations (sites,
-- articles, channels) stay in suggest.sql; these are the words to search with.
--
-- suggest_phrases  prefix (lowercased, spaces collapsed, 3-20 characters) -> the 5 most
--                  searched phrases starting with it, and how often each was searched
--
-- Source: query_list rows from AmazonQAC (real US searches with counts, Sept 2023,
-- CDLA-Permissive-2.0). Shopping-heavy by nature: good for "air fr", silent for "how to".
-- A phrase is never completed when it carries an adult, hateful or drug word — these
-- boxes ship to families. (Weapon and "weed" words are NOT listed: on Amazon they are
-- glue guns, massage guns and weed killers.) It can still be typed and searched; it is just never offered.
--
-- Run after serve/querylist (it reads query_list):
--   psql -U search -d search -v ON_ERROR_STOP=1 -f phrases.sql
DROP TABLE IF EXISTS suggest_phrases;
CREATE TABLE suggest_phrases (prefix text PRIMARY KEY, phrases text[] NOT NULL, counts real[] NOT NULL);

DO $$
BEGIN
  IF to_regclass('query_list') IS NULL THEN
    RAISE NOTICE 'no query_list: suggest_phrases left empty';
    RETURN;
  END IF;
  CREATE TEMP TABLE sg_phrase AS
    SELECT q, max(weight) AS n FROM (
      SELECT btrim(regexp_replace(lower(query), '\s+', ' ', 'g')) AS q, weight
      FROM query_list WHERE source = 'amazonqac' AND weight >= 200) x   -- = search-api's PHRASE_MIN_SEARCHES
    WHERE length(q) BETWEEN 4 AND 60
      AND q ~ '^[a-z0-9]'
      AND q !~ ('\m(' || array_to_string(ARRAY[
        'sex', 'sexy', 'sexual', 'porn', 'porno', 'xxx', 'nude', 'nudes', 'naked', 'nsfw', 'erotic', 'erotica',
        'dildo', 'dildos', 'vibrator', 'vibrators', 'masturbat\w*', 'orgasm\w*', 'fleshlight', 'butt ?plugs?',
        'anal', 'bdsm', 'fetish', 'bondage', 'kinky', 'lingerie', 'thong', 'thongs', 'g-?string',
        'condoms?', 'penis', 'vagina', 'clit\w*', 'nipples?', 'boobs?', 'tits',
        'strap-?on', 'cock', 'pussy', 'hentai', 'stripper', 'pole dance',
        'nazi', 'kkk', 'swastika', 'n[i1]gg\w*', 'fag\w*', 'retard\w*',
        'marijuana', 'bong', 'bongs', 'dab rig', 'kratom', 'delta 8', 'thc'], '|') || ')\M')
    GROUP BY q;
  INSERT INTO suggest_phrases (prefix, phrases, counts)
    SELECT prefix, (array_agg(q ORDER BY n DESC, q))[1:5], (array_agg(n ORDER BY n DESC, q))[1:5]
    FROM (SELECT DISTINCT rtrim(left(q, l)) AS prefix, q, n     -- "air" and "air " are one prefix
          FROM sg_phrase CROSS JOIN LATERAL generate_series(3, least(length(q) - 1, 20)) l) p
    WHERE length(prefix) >= 3
    GROUP BY prefix;
END $$;
ANALYZE suggest_phrases;
SELECT 'phrase prefixes ' || count(*) FROM suggest_phrases;
