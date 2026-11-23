-- The news file (NEWS_SQLITE): schema.sql's feeds/items plus wxall's wx_* tables, for a host
-- without Postgres (search.logout.cloud, ADR-063). Timestamps are Unix seconds (REAL); jsonb and
-- text[] columns are JSON text. items_fts is the headline search (Postgres: pg_textsearch bm25).
CREATE TABLE IF NOT EXISTS feeds (
    url          TEXT PRIMARY KEY,
    section      TEXT NOT NULL,
    name         TEXT NOT NULL,
    lean         TEXT NOT NULL DEFAULT '-',
    kind         TEXT NOT NULL,
    poll_min     INTEGER NOT NULL,
    resolved_url TEXT,
    etag         TEXT,
    modified     TEXT,
    next_due     REAL NOT NULL DEFAULT 0,
    last_ok      REAL,
    last_status  INTEGER,
    last_error   TEXT,
    fails        INTEGER NOT NULL DEFAULT 0,
    fails_4xx    INTEGER NOT NULL DEFAULT 0,
    idle_polls   INTEGER NOT NULL DEFAULT 0,
    items_total  INTEGER NOT NULL DEFAULT 0,
    disabled     INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS items (
    id          INTEGER PRIMARY KEY,
    feed_url    TEXT NOT NULL REFERENCES feeds(url) ON DELETE CASCADE,
    guid        TEXT NOT NULL,
    url         TEXT NOT NULL,
    title       TEXT NOT NULL,
    summary     TEXT NOT NULL DEFAULT '',
    author      TEXT,
    published   REAL NOT NULL,
    fetched     REAL NOT NULL DEFAULT (unixepoch('subsec')),
    UNIQUE (feed_url, guid)
);
CREATE INDEX IF NOT EXISTS items_published ON items (published DESC);
CREATE INDEX IF NOT EXISTS items_feed ON items (feed_url);
-- English stemming like Postgres' 'english' config; stop words are dropped from the QUERY
-- (search_api) since FTS5 has no stop list.
CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5(
    title, summary, content='items', content_rowid='id', tokenize='porter unicode61 remove_diacritics 2');
CREATE TRIGGER IF NOT EXISTS items_ai AFTER INSERT ON items BEGIN
    INSERT INTO items_fts (rowid, title, summary) VALUES (new.id, new.title, new.summary);
END;
CREATE TRIGGER IF NOT EXISTS items_ad AFTER DELETE ON items BEGIN
    INSERT INTO items_fts (items_fts, rowid, title, summary) VALUES ('delete', old.id, old.title, old.summary);
END;
CREATE TRIGGER IF NOT EXISTS items_au AFTER UPDATE ON items BEGIN
    INSERT INTO items_fts (items_fts, rowid, title, summary) VALUES ('delete', old.id, old.title, old.summary);
    INSERT INTO items_fts (rowid, title, summary) VALUES (new.id, new.title, new.summary);
END;

CREATE TABLE IF NOT EXISTS wx_towns (key TEXT, name TEXT, state TEXT, lat REAL, lon REAL, pop INTEGER,
    counties TEXT, tz TEXT, kind TEXT, county TEXT);
CREATE INDEX IF NOT EXISTS wx_towns_key ON wx_towns (key);
CREATE INDEX IF NOT EXISTS wx_towns_lat ON wx_towns (lat);
CREATE TABLE IF NOT EXISTS wx_stations (id TEXT PRIMARY KEY, lat REAL, lon REAL);
CREATE INDEX IF NOT EXISTS wx_stations_lat ON wx_stations (lat);
CREATE TABLE IF NOT EXISTS wx_obs (id TEXT PRIMARY KEY, obs TEXT, at REAL);
CREATE TABLE IF NOT EXISTS wx_fc (id TEXT PRIMARY KEY, hours TEXT, days TEXT, cycle REAL, days_cycle REAL);
CREATE TABLE IF NOT EXISTS wx_alerts (id TEXT PRIMARY KEY, event TEXT, severity TEXT, headline TEXT,
    ends TEXT, same TEXT, poly TEXT);
-- (wxall.py): US airports and the FAA's airport status. Times are Unix seconds, item is a JSON object.
CREATE TABLE IF NOT EXISTS airports (code TEXT PRIMARY KEY, icao TEXT, name TEXT, city TEXT, state TEXT,
    lat REAL, lon REAL, kind TEXT, sched INTEGER);
CREATE TABLE IF NOT EXISTS airport_status (code TEXT, n INTEGER, item TEXT, PRIMARY KEY (code, n));
CREATE TABLE IF NOT EXISTS airport_feed (id INTEGER PRIMARY KEY, updated REAL, fetched REAL);
-- the month's on-time record by airline, flight number and route (BTS); dep/arr are the usual scheduled times, local HHMM
CREATE TABLE IF NOT EXISTS flight_ontime (carrier TEXT, num INTEGER, origin TEXT, dest TEXT, n INTEGER, done INTEGER,
    ontime INTEGER, cancelled INTEGER, dep INTEGER, arr INTEGER, PRIMARY KEY (carrier, num, origin, dest));
CREATE TABLE IF NOT EXISTS flight_ontime_feed (id INTEGER PRIMARY KEY, year INTEGER, month INTEGER, fetched REAL);

-- sports.py (personal box only) and weather.py (the home forecast): keys/broadcasts are JSON arrays
CREATE TABLE IF NOT EXISTS games (league TEXT NOT NULL, game_id TEXT NOT NULL, start_utc REAL NOT NULL,
    state TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '',
    away_name TEXT NOT NULL, away_nick TEXT NOT NULL, away_abbr TEXT, away_score INTEGER,
    home_name TEXT NOT NULL, home_nick TEXT NOT NULL, home_abbr TEXT, home_score INTEGER,
    away_keys TEXT NOT NULL, home_keys TEXT NOT NULL, venue TEXT, broadcasts TEXT NOT NULL DEFAULT '[]',
    url TEXT, updated REAL NOT NULL, PRIMARY KEY (league, game_id));
CREATE INDEX IF NOT EXISTS games_start ON games (start_utc);
CREATE TABLE IF NOT EXISTS teams (league TEXT NOT NULL, name TEXT NOT NULL, nick TEXT NOT NULL, keys TEXT NOT NULL,
    updated REAL NOT NULL, PRIMARY KEY (league, name));
CREATE TABLE IF NOT EXISTS weather (place TEXT PRIMARY KEY, payload TEXT NOT NULL, source TEXT NOT NULL, updated REAL NOT NULL);

-- sports.py: league tables computed from the season's results (NFL divisions, EPL, Championship).
-- Replaced per league on every fetch; `extra` is JSON ({"pct", "diff"} NFL, {"gd"} football); for NFL
-- `points` is win% x 1000. SQLite only: nothing like it exists in the Postgres schema.
CREATE TABLE IF NOT EXISTS standings (league TEXT NOT NULL, grp TEXT NOT NULL, rank INTEGER NOT NULL, team TEXT NOT NULL,
    nick TEXT NOT NULL, played INTEGER NOT NULL, won INTEGER NOT NULL, drawn INTEGER NOT NULL, lost INTEGER NOT NULL,
    for_ INTEGER NOT NULL, against INTEGER NOT NULL, points INTEGER NOT NULL, extra TEXT, updated REAL NOT NULL, as_of TEXT,
    PRIMARY KEY (league, grp, rank));

-- fx.py: the ECB's daily euro reference rates, units of each currency per euro. One date for the whole
-- table (as_of, the ECB's own); replaced in one transaction on every successful fetch; fetched is the time of
-- that fetch (Unix seconds), moved also when the file was unchanged and never by a failed one: "checked N h ago".
-- SQLite only: nothing like it exists in the Postgres schema.
CREATE TABLE IF NOT EXISTS fx_rates (currency TEXT PRIMARY KEY, per_eur REAL NOT NULL, as_of TEXT NOT NULL,
    fetched REAL NOT NULL);
