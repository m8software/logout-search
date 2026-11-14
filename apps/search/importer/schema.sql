-- apps/search schema. Idempotent: applied by `search_import.py migrate` on every
-- import, so a column added here lands on the next run without a migration tool.
CREATE EXTENSION IF NOT EXISTS pg_textsearch;
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- One row per registrable domain.
CREATE TABLE IF NOT EXISTS sites (
    domain         text PRIMARY KEY,
    tranco_rank    integer,                 -- legacy name; nothing fills it any more (the Tranco import was removed), so NULL
    blocked_by     text,                    -- NULL = eligible; else the blocklist that matched
    history_visits integer NOT NULL DEFAULT 0,
    history_last   timestamptz,
    first_seen     timestamptz NOT NULL DEFAULT now(),
    -- filled by the phase-2 crawler
    crawled_at     timestamptz,
    http_status    integer,
    final_url      text,
    lang           text,
    title          text,
    description    text,
    h1             text,
    -- what BM25 ranks: the domain split into words plus whatever the crawl found
    search_text    text GENERATED ALWAYS AS (
        regexp_replace(domain, '[.-]+', ' ', 'g') || ' ' ||
        coalesce(title, '') || ' ' || coalesce(description, '') || ' ' || coalesce(h1, '')
    ) STORED
);
CREATE INDEX IF NOT EXISTS sites_rank_idx   ON sites (tranco_rank);
CREATE INDEX IF NOT EXISTS sites_domain_trgm ON sites USING gin (domain gin_trgm_ops);
-- Typo lookups (`ORDER BY domain <-> 'githb.com'`): the GIN `%` filter took
-- 897 ms at 1M rows, this KNN index ~215 ms (indexhost, 2026-09-23). Still too
-- slow for the gate's budget, and raw similarity ranks gith5b.com above
-- github.com — popularity has to be blended in.
CREATE INDEX IF NOT EXISTS sites_domain_trgm_knn ON sites USING gist (domain gist_trgm_ops);
-- 'simple' until per-language columns exist: english stemming would mangle the
-- ~half of the list that is not English (300-site sample, 2026-09-23).
CREATE INDEX IF NOT EXISTS sites_bm25 ON sites USING bm25 (search_text) WITH (text_config = 'simple');

-- Hostnames below a site: subdomains the crawler finds linked from a homepage,
-- and hosts from browser history. Visit counts only — never URLs or paths.
CREATE TABLE IF NOT EXISTS hosts (
    host        text PRIMARY KEY,
    domain      text NOT NULL,
    source      text NOT NULL,              -- 'history' | 'homepage-link'
    visits      integer NOT NULL DEFAULT 0,
    last_visit  timestamptz
);
CREATE INDEX IF NOT EXISTS hosts_domain_idx ON hosts (domain);

-- Common Crawl link authority, from the host/domain web graph release
-- named in cc_graph (e.g. cc-main-2026-jun-jul-aug). *_pos = rank position,
-- 1 = best; harmonic centrality is the primary signal (harder to game).
ALTER TABLE sites ADD COLUMN IF NOT EXISTS cc_graph     text;
ALTER TABLE sites ADD COLUMN IF NOT EXISTS cc_hc_pos    integer;
ALTER TABLE sites ADD COLUMN IF NOT EXISTS cc_hc_val    real;
ALTER TABLE sites ADD COLUMN IF NOT EXISTS cc_pr_pos    integer;
ALTER TABLE sites ADD COLUMN IF NOT EXISTS cc_pr_val    double precision;
ALTER TABLE sites ADD COLUMN IF NOT EXISTS cc_n_hosts   integer;
-- Evidence the domain serves a website at all (vs CDN/API endpoints):
-- successful page fetches Common Crawl logged in the host index crawls named
-- in cc_hostindex_crawls. NULL = never measured; 0 = measured, none.
ALTER TABLE sites ADD COLUMN IF NOT EXISTS cc_fetch_200        bigint;
ALTER TABLE sites ADD COLUMN IF NOT EXISTS cc_robots_200       integer;
ALTER TABLE sites ADD COLUMN IF NOT EXISTS cc_hostindex_crawls text;
CREATE INDEX IF NOT EXISTS sites_cc_hc_idx ON sites (cc_hc_pos);

ALTER TABLE hosts ADD COLUMN IF NOT EXISTS cc_hc_pos integer;
ALTER TABLE hosts ADD COLUMN IF NOT EXISTS cc_pr_pos integer;

-- ---------------------------------------------------------------------------
-- Common Crawl URL-index scan. Our own 2026 "host index": CC's official
-- one is frozen at CC-MAIN-2025-18 (see).
-- ---------------------------------------------------------------------------
-- Every index file we know of, with its SURT range (parquet footer) and scan
-- result. scanned_at is written in the SAME transaction as the file's rows, so
-- a scan interrupted anywhere resumes at exactly the first unscanned file.
CREATE TABLE IF NOT EXISTS cc_index_files (
    path        text PRIMARY KEY,
    crawl       text NOT NULL,
    subset      text NOT NULL,            -- warc | crawldiagnostics | robotstxt
    surt_min    text NOT NULL,
    surt_max    text NOT NULL,
    scanned_at  timestamptz,
    rx_bytes    bigint,                   -- bytes actually downloaded for this file
    seconds     real,
    domains     integer                   -- our domains seen in this file
);
CREATE INDEX IF NOT EXISTS cc_index_files_crawl_idx ON cc_index_files (crawl, subset);

-- Per registrable domain, per crawl. Domains that straddle two files are summed,
-- so `hosts` can over-count by the few hosts seen in both files.
CREATE TABLE IF NOT EXISTS cc_domain_crawl (
    domain        text NOT NULL,
    crawl         text NOT NULL,
    ok_200        integer NOT NULL DEFAULT 0,   -- successful captures (warc subset)
    html_200      integer NOT NULL DEFAULT 0,   -- ... of which HTML
    hosts         integer NOT NULL DEFAULT 0,
    redirects     integer NOT NULL DEFAULT 0,   -- crawldiagnostics 3xx
    err_4xx       integer NOT NULL DEFAULT 0,
    err_5xx       integer NOT NULL DEFAULT 0,
    robots_status integer,                      -- most common robots.txt status
    PRIMARY KEY (domain, crawl)
);

-- Best homepage capture per domain per crawl: the root page if CC has one,
-- else a page one level down (/en/, /about). quality orders root > shallow,
-- https > http, bare host > www > other hosts; newest capture breaks ties.
CREATE TABLE IF NOT EXISTS cc_captures (
    domain        text NOT NULL,
    crawl         text NOT NULL,
    quality       integer NOT NULL,
    kind          text NOT NULL,                -- root | shallow
    host          text NOT NULL,
    path          text NOT NULL,
    protocol      text NOT NULL,
    fetch_time    timestamptz NOT NULL,
    languages     text,                         -- CC's detected content languages
    warc_filename text NOT NULL,
    warc_offset   bigint NOT NULL,
    warc_length   integer NOT NULL,
    PRIMARY KEY (domain, crawl)
);

-- ---------------------------------------------------------------------------
-- Parsed homepage content. Kept out of `sites` on purpose: a bulk update
-- of `sites` rewrites its trigram/BM25 indexes for every row (audit 2026-09-23).
-- One row per domain per crawl; error set = the fetch or parse failed.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS docs (
    domain      text NOT NULL,
    crawl       text NOT NULL,
    url         text NOT NULL,
    fetched_at  timestamptz NOT NULL DEFAULT now(),
    http_status integer,
    title       text,
    description text,
    h1          text,
    lang        text,
    body        text,                    -- first ~2,000 chars of visible text
    error       text,
    search_text text GENERATED ALWAYS AS (
        regexp_replace(domain, '[.-]+', ' ', 'g') || ' ' || coalesce(title, '') || ' ' ||
        coalesce(description, '') || ' ' || coalesce(h1, '') || ' ' || coalesce(body, '')
    ) STORED,
    PRIMARY KEY (domain, crawl)
);
-- English only (2026-09-23), so English stemming is right here.
CREATE INDEX IF NOT EXISTS docs_bm25 ON docs USING bm25 (search_text) WITH (text_config = 'english');
-- what the capture said to search engines (ccfetch.robots_of): '' = read, nothing said; NULL = never read for it.
-- A published release shows no description for a NULL row and uses no noindex capture (importer/docs_policy.py, G3 D4c).
ALTER TABLE docs ADD COLUMN IF NOT EXISTS robots text;

-- ---------------------------------------------------------------------------
-- English Wikipedia. The loader fills the *_stage tables, then builds
-- wiki_docs (aliases = redirect titles) and swaps it in, so searches never see
-- a half-loaded dump.
-- ---------------------------------------------------------------------------
CREATE UNLOGGED TABLE IF NOT EXISTS wiki_articles_stage (
    page_id  bigint,
    rev_id   bigint,
    title    text,
    headings text,
    lead     text,
    infobox  text
);
CREATE UNLOGGED TABLE IF NOT EXISTS wiki_redirects_stage (
    title  text,
    target text
);

-- ---------------------------------------------------------------------------
-- Page-level captures for the most popular sites (ccpages.py, 2026-09-24): up to
-- --per-domain pages inside each of the top-N domains, and their fetched text.
-- ---------------------------------------------------------------------------
CREATE UNLOGGED TABLE IF NOT EXISTS cc_pages_stage (
    domain text, host text, path text, protocol text, languages text,
    warc_filename text, warc_offset bigint, warc_length integer
);
CREATE TABLE IF NOT EXISTS cc_pages (
    url           text PRIMARY KEY,
    domain        text NOT NULL,
    crawl         text NOT NULL,
    host          text NOT NULL,
    path          text NOT NULL,
    protocol      text NOT NULL,
    languages     text,
    warc_filename text NOT NULL,
    warc_offset   bigint NOT NULL,
    warc_length   integer NOT NULL
);
CREATE TABLE IF NOT EXISTS page_docs (
    url         text PRIMARY KEY,
    domain      text NOT NULL,
    crawl       text NOT NULL,
    fetched_at  timestamptz NOT NULL DEFAULT now(),
    http_status integer,
    title       text,
    description text,
    h1          text,
    lang        text,
    body        text,
    error       text,
    search_text text GENERATED ALWAYS AS (
        coalesce(title, '') || ' ' || coalesce(description, '') || ' ' || coalesce(h1, '') || ' ' ||
        coalesce(body, '')) STORED
);
CREATE INDEX IF NOT EXISTS page_docs_bm25 ON page_docs USING bm25 (search_text) WITH (text_config = 'english');

-- Subdomain hosts of our not-blocked sites, plumbing skipped (ccpages.infra_host). Their front pages
-- go into cc_pages (kind 'host'). The command that filled this table from the Tranco subdomain list was
-- removed; nothing in this repo fills it now, and ccpages --hosts and serve-prep.sql (host_nav) still read it.
CREATE TABLE IF NOT EXISTS tranco_hosts (
    host   text PRIMARY KEY,
    rank   integer NOT NULL,
    domain text NOT NULL
);
ALTER TABLE cc_pages ADD COLUMN IF NOT EXISTS kind text NOT NULL DEFAULT 'page';   -- page | host | answer | gov
ALTER TABLE cc_pages_stage ADD COLUMN IF NOT EXISTS kind text NOT NULL DEFAULT 'page';
-- ccpages.answer_score of an answer-site page; orders the cut across index files. 0 for page and host rows.
ALTER TABLE cc_pages_stage ADD COLUMN IF NOT EXISTS score smallint NOT NULL DEFAULT 0;
