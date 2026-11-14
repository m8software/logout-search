-- The local news box (poller.py). Its own database, so the serve swaps never touch it.
CREATE EXTENSION IF NOT EXISTS pg_textsearch;
CREATE TABLE IF NOT EXISTS feeds (
    url          text PRIMARY KEY,           -- as listed in feeds.tsv
    section      text NOT NULL,
    name         text NOT NULL,
    lean         text NOT NULL DEFAULT '-',
    kind         text NOT NULL,
    poll_min     integer NOT NULL,
    resolved_url text,                       -- sitemap-discover: the news sitemap robots.txt named
    etag         text,
    modified     text,
    next_due     timestamptz NOT NULL DEFAULT now(),
    last_ok      timestamptz,
    last_status  integer,
    last_error   text,
    fails        integer NOT NULL DEFAULT 0,
    fails_4xx    integer NOT NULL DEFAULT 0,
    idle_polls   integer NOT NULL DEFAULT 0,
    items_total  bigint NOT NULL DEFAULT 0,
    disabled     boolean NOT NULL DEFAULT false
);
CREATE TABLE IF NOT EXISTS items (
    id          bigserial PRIMARY KEY,
    feed_url    text NOT NULL REFERENCES feeds(url) ON DELETE CASCADE,
    guid        text NOT NULL,
    url         text NOT NULL,
    title       text NOT NULL,
    summary     text NOT NULL DEFAULT '',
    author      text,
    published   timestamptz NOT NULL,
    fetched     timestamptz NOT NULL DEFAULT now(),
    search_text text GENERATED ALWAYS AS (title || ' ' || summary) STORED,
    UNIQUE (feed_url, guid)
);
CREATE INDEX IF NOT EXISTS items_published ON items (published DESC);
CREATE INDEX IF NOT EXISTS items_bm25 ON items USING bm25 (search_text) WITH (text_config = 'english');

-- Commits never wait on the disk (2026-09-26): homeserver's btrfs took 420 ms per fsync and
-- the pollers' ~5 commits a second kept the search database's disk busy under every search.
-- A crash can lose the last moments of polled headlines; the next poll fetches them again.
DO $$ BEGIN
    EXECUTE format('ALTER DATABASE %I SET synchronous_commit = off', current_database());
EXCEPTION WHEN insufficient_privilege THEN NULL;      -- not the owner: PGOPTIONS still sets it
END $$;
