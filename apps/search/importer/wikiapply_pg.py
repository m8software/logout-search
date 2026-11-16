#!/usr/bin/env python3
"""Apply a wikidelta.py change set to the build database's Wikipedia tables (owner 2026-11-15: catch-up and daily refresh).

    wikiapply_pg.py DSN DELTA_DIR --dump enwiki-20261101 [--drop-bm25] [--dry-run]

DELTA_DIR is wikidelta.py's output (changed.tsv.gz, deleted.tsv, DONE). The release pipeline reads the articles from a
throwaway Postgres over a COPY of the build database (bridge-postgres.sh), so rc16's Wikipedia comes from running this on
that copy, never on indexhost's live build database:
  wiki_docs      upsert by page_id (title, headings, lead, infobox, aliases, rev_id, dump); deleted ids removed
  wiki_aliases   the touched articles' rows rebuilt the way wikiparse._build_wiki_docs makes them (normalised title and
                 every alias)
  wiki_bit, wiki_vec  rows of deleted articles removed (new vectors are loaded by the embedding step)
One transaction. --dry-run rolls it back after printing the counts; --drop-bm25 drops wiki_docs's BM25 index first (a
throwaway copy needs no search index, and 2 M updates against it are slow, month-text.sql does the same).
Counts printed must equal the change set's (changed rows upserted, deleted rows removed): a difference aborts.
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
import time

import psycopg


def log(msg: str) -> None:
    print(f"[wikiapply-pg] {msg}", flush=True)


def copy_file(conn, table_cols: str, path: str, opener) -> None:
    with conn.cursor().copy(f"COPY {table_cols} FROM STDIN") as cp, opener(path) as f:
        while chunk := f.read(1 << 20):
            cp.write(chunk)


def apply(dsn: str, delta: str, dump: str, drop_bm25: bool, dry: bool) -> dict:
    t0 = time.time()
    if not os.path.exists(os.path.join(delta, "DONE")):
        sys.exit(f"{delta}: no DONE marker, the change set is not finished")
    summ = json.load(open(os.path.join(delta, "summary.json")))
    conn = psycopg.connect(dsn, autocommit=True)
    conn.execute("SET statement_timeout = 0")
    out: dict = {}
    try:
      with conn.transaction():
          if drop_bm25:
              for (name,) in conn.execute("SELECT indexname FROM pg_indexes WHERE tablename = 'wiki_docs' AND indexdef ~ 'USING bm25'").fetchall():
                  conn.execute(f'DROP INDEX "{name}"')
                  log(f"dropped {name}")
          conn.execute("CREATE TEMP TABLE wd_changed (op text, page_id bigint, rev_id bigint, title text, headings text, lead text, "
                       "infobox text, aliases text) ON COMMIT DROP")
          conn.execute("CREATE TEMP TABLE wd_deleted (page_id bigint, title text) ON COMMIT DROP")
          copy_file(conn, "wd_changed (op, page_id, rev_id, title, headings, lead, infobox, aliases)",
                    os.path.join(delta, "changed.tsv.gz"), lambda p: gzip.open(p, "rb"))
          copy_file(conn, "wd_deleted (page_id, title)", os.path.join(delta, "deleted.tsv"), lambda p: open(p, "rb"))
          n_ch = conn.execute("SELECT count(*) FROM wd_changed").fetchone()[0]
          n_del = conn.execute("SELECT count(*) FROM wd_deleted").fetchone()[0]
          want_ch = summ.get("new", 0) + summ.get("change", 0) + summ.get("alias_only", 0)
          if n_ch != want_ch or n_del != summ.get("deleted", 0):
              sys.exit(f"the files hold {n_ch} changed and {n_del} deleted rows, summary.json says {want_ch} and {summ.get('deleted')}")
          conn.execute("CREATE INDEX ON wd_changed (page_id)")
          out["upserted"] = conn.execute(
              "WITH u AS (INSERT INTO wiki_docs (page_id, rev_id, title, headings, lead, infobox, dump, aliases) "
              "SELECT page_id, rev_id, title, coalesce(headings, ''), coalesce(lead, ''), coalesce(infobox, ''), %s, coalesce(aliases, '') FROM wd_changed "
              "ON CONFLICT (page_id) DO UPDATE SET rev_id = EXCLUDED.rev_id, title = EXCLUDED.title, headings = EXCLUDED.headings, "
              "lead = EXCLUDED.lead, infobox = EXCLUDED.infobox, dump = EXCLUDED.dump, aliases = EXCLUDED.aliases RETURNING 1) "
              "SELECT count(*) FROM u", (dump,)).fetchone()[0]
          out["deleted"] = conn.execute("WITH d AS (DELETE FROM wiki_docs w USING wd_deleted x WHERE w.page_id = x.page_id RETURNING 1) "
                                        "SELECT count(*) FROM d").fetchone()[0]
          conn.execute("DELETE FROM wiki_aliases a USING (SELECT page_id FROM wd_changed UNION SELECT page_id FROM wd_deleted) t "
                       "WHERE a.page_id = t.page_id")
          out["alias_rows"] = conn.execute(
              "WITH i AS (INSERT INTO wiki_aliases (norm, page_id) SELECT DISTINCT lower(regexp_replace(t, '[^[:alnum:]]', '', 'g')), page_id "
              "FROM (SELECT page_id, title AS t FROM wd_changed UNION ALL "
              "      SELECT page_id, unnest(string_to_array(aliases, ' | ')) FROM wd_changed WHERE coalesce(aliases, '') <> '') x "
              "WHERE t <> '' RETURNING 1) SELECT count(*) FROM i").fetchone()[0]
          for table in ("wiki_bit", "wiki_vec"):
              if conn.execute("SELECT to_regclass(%s)", (table,)).fetchone()[0]:
                  out[table + "_removed"] = conn.execute(
                      f"WITH d AS (DELETE FROM {table} v USING wd_deleted x WHERE v.id = x.page_id RETURNING 1) SELECT count(*) FROM d").fetchone()[0]
          if out["upserted"] != n_ch or out["deleted"] > n_del:
              sys.exit(f"upserted {out['upserted']} of {n_ch}, deleted {out['deleted']} of {n_del}: rolled back")
          out["wiki_docs_after"] = conn.execute("SELECT count(*) FROM wiki_docs").fetchone()[0]
          if dry:
              raise psycopg.Rollback()
    except psycopg.Rollback:
        pass
    out.update(seconds=round(time.time() - t0), dry_run=dry)
    log(json.dumps(out, sort_keys=True))
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("dsn"); ap.add_argument("delta")
    ap.add_argument("--dump", required=True)
    ap.add_argument("--drop-bm25", action="store_true"); ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    apply(a.dsn, a.delta, a.dump, a.drop_bm25, a.dry_run)


if __name__ == "__main__":
    main()
