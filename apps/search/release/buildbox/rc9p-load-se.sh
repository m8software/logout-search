#!/bin/bash
# rc9p (2026-10-23): rc9 corrected for publishing (audit 2026-10-22 G1-G6 + Stack Exchange credits), --previous rc9. Same inputs as rc9; places release 2026-10-23-us (rc9's, re-read with Overture's provider names).
# rc9p: add ONLY se_authors to buildbox's Postgres copy (rc9's other tables stay as rc9 loaded them, so rc9p = rc9 corrected).
# indexhost serves the dump over plain HTTP on the home network (unit rc9p-http); se_authors holds no page text.
set -eo pipefail
. /mnt/c/logout-wsltest/rc9p-common.sh
SRC=http://index-host:8766; L=$R/rc9p-load; mkdir -p $L
grep -qx buildbox-pg <<<"$(docker ps --format '{{.Names}}')" || pg_up
Q(){ docker exec buildbox-pg psql -U search -d search -h 127.0.0.1 -Atc "$1"; }
curl -sf $SRC/dump.counts -o $L/dump.counts; curl -sf $SRC/SHA256SUMS -o $L/SHA256SUMS
curl -sf $SRC/se_authors.tsv.zst -o $L/se_authors.tsv.zst; (cd $L && sha256sum -c SHA256SUMS)
want=$(awk '$1=="se_authors"{print $2}' $L/dump.counts)
Q "CREATE TABLE IF NOT EXISTS se_authors (site text NOT NULL, qid integer NOT NULL, role smallint NOT NULL, user_id integer, display_name text, PRIMARY KEY (site, qid, role))"
Q "TRUNCATE se_authors"
zstd -dc $L/se_authors.tsv.zst | docker exec -i buildbox-pg psql -U search -d search -h 127.0.0.1 -q -c "COPY se_authors (site, qid, role, user_id, display_name) FROM STDIN"
got=$(Q "select count(*) from se_authors"); echo "se_authors loaded=$got want=$want"; [ "$got" = "$want" ] || { echo "COUNT MISMATCH"; exit 1; }
Q "ANALYZE se_authors"
pg_down
log "LOAD-DONE"
