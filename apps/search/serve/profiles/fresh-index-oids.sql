-- Give every BM25 index this database inherited from its template a new OID.
--
-- pg_textsearch keeps each BM25 index's in-memory state (its memtable) under the index
-- OID, and CREATE DATABASE ... TEMPLATE keeps every OID. So a file-copied clone and its
-- source share one slot per index: after writes in the source (the build database gets
-- them all the time), a query on the clone read the source's memtable and failed with
-- "pg_textsearch memtable page at block 40 in index docs_bm25 has invalid magic"
-- (serve_box, twice on 2026-09-26; OIDs checked equal across search / serve / serve_box).
-- REINDEX keeps the OID and does not help; DROP + CREATE gets a new one.
--
-- An index older than its own database (OID below the database's OID) came from the
-- template; one built after the copy is left alone. Run after the last write.
DO $$
DECLARE
  r record;
  db_oid oid := (SELECT oid FROM pg_database WHERE datname = current_database());
BEGIN
  FOR r IN SELECT c.relname, c.oid, pg_get_indexdef(c.oid) AS def
           FROM pg_class c JOIN pg_am a ON a.oid = c.relam
           JOIN pg_namespace n ON n.oid = c.relnamespace
           WHERE a.amname = 'bm25' AND n.nspname = 'public' AND c.oid < db_oid
           ORDER BY pg_relation_size(c.oid) LOOP
    RAISE NOTICE 'new OID for % (was %)', r.relname, r.oid;
    EXECUTE format('DROP INDEX %I', r.relname);
    EXECUTE r.def;
  END LOOP;
END $$;
